#!/usr/bin/env bash
# strata-agent-setup.sh: set up a sandboxed Codex CLI agent that talks to the local Strata server.
#
# What it creates (nothing outside ~/agent is changed, and Strata's config is NOT touched here):
#   ~/agent/Dockerfile                 image with Codex CLI, git, python3, build tools
#   ~/agent/codex-home/config.toml     Codex settings pointing at http://127.0.0.1:8085/v1
#   ~/agent/bin/agent-run.sh           safe wrapper: checks Strata, then runs Codex in a limited container
#   ~/agent/bin/strata-agent-config.sh the one-off edit of the Strata config (backup first)
#   ~/agent/work/textstats             a small first task (4 functions, tests that must pass)
#
# Usage:   bash strata-agent-setup.sh            (builds the Docker image; takes a few minutes)
#          SKIP_BUILD=1 bash strata-agent-setup.sh   (write the files only)
set -euo pipefail

AGENT="${AGENT_DIR:-$HOME/agent}"
mkdir -p "$AGENT"/{bin,codex-home,work,logs}

command -v docker >/dev/null || { echo "docker is not installed or not on PATH"; exit 1; }

# ---------------------------------------------------------------- 1. image
cat > "$AGENT/Dockerfile" <<'DOCKERFILE'
FROM ubuntu:24.04
ARG CODEX_URL=https://github.com/openai/codex/releases/latest/download/codex-x86_64-unknown-linux-musl.tar.gz
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl git python3 python3-pip python3-venv build-essential jq ripgrep nodejs \
    && rm -rf /var/lib/apt/lists/*
# the official static (musl) binary: no Node.js needed
RUN mkdir /tmp/cx && curl -fsSL "$CODEX_URL" | tar -xz -C /tmp/cx \
    && f="$(find /tmp/cx -type f -name 'codex*' | head -1)" \
    && install -m 0755 "$f" /usr/local/bin/codex && rm -rf /tmp/cx \
    && codex --version
# ubuntu:24.04 ships a user 'ubuntu' with uid 1000: replace it with 'agent' (same uid as the host user)
RUN userdel -r ubuntu 2>/dev/null || true; useradd -m -u 1000 -s /bin/bash agent
ENV HOME=/home/agent
WORKDIR /work
DOCKERFILE

# ---------------------------------------------------------------- 2. Codex config
if [ ! -f "$AGENT/codex-home/config.toml" ] || [ "${FORCE:-0}" = 1 ]; then
cat > "$AGENT/codex-home/config.toml" <<'TOML'
model = "strata"                        # any name; Strata answers with its own model
model_provider = "strata"
model_context_window = 32768            # the context Strata runs with (--max-context)
show_raw_agent_reasoning = true         # show the model's thinking in the log
model_reasoning_effort = "low"          # none, low, medium or high

[model_providers.strata]
name = "Strata (local)"
base_url = "http://127.0.0.1:8085/v1"
wire_api = "responses"
stream_idle_timeout_ms = 600000         # the first prompt (about 9,400 tokens) can take minutes to read
TOML
else
  echo "keeping your existing $AGENT/codex-home/config.toml (FORCE=1 overwrites it)"
fi

# ---------------------------------------------------------------- 3. run wrapper
cat > "$AGENT/bin/agent-run.sh" <<'RUNSH'
#!/usr/bin/env bash
# agent-run.sh PROJECT "task text"   run one task unattended; the log goes to ~/agent/logs/
# agent-run.sh PROJECT -i            interactive Codex session (asks before risky commands)
# agent-run.sh smoke                 end-to-end test: Strata -> Codex -> one shell command
# Env: CODEX_SANDBOX=danger-full-access (default: the container is the sandbox, because Codex's own bubblewrap sandbox
#      cannot run inside Docker) or workspace-write
#      FORCE=1 runs even if the GPU cache is not all-resident
set -euo pipefail
AGENT="${AGENT_DIR:-$HOME/agent}"
STRATA="${STRATA_DIR:-$HOME/github/strata-work/Strata}"
LOG="$STRATA/strata-swift-iq3_xxs.log"
URL="http://127.0.0.1:8085"
SANDBOX="${CODEX_SANDBOX:-danger-full-access}"

proj="${1:?usage: agent-run.sh PROJECT \"task\" | agent-run.sh PROJECT -i | agent-run.sh smoke}"; shift || true
if [ "$proj" = smoke ]; then
  mkdir -p "$AGENT/work/smoke"
  set -- "Run the shell command: echo sandbox-ok   Then tell me exactly what it printed."
fi
[ -d "$AGENT/work/$proj" ] || { echo "no such project folder: $AGENT/work/$proj"; exit 1; }
[ -n "${1:-}" ] || { echo "give a task in quotes, or -i for interactive"; exit 1; }

# 1. Strata must be answering
code="$(curl -s -o /dev/null -w '%{http_code}' "$URL/v1/models" || true)"
[ "$code" = 200 ] || { echo "Strata is not answering on $URL (start it with ~/start-swift.sh)"; exit 1; }

# 2. batched prompt reading is only safe while every expert is in the GPU cache
last="$(grep -E 'expert cache [0-9]+ slots' "$LOG" 2>/dev/null | tail -1 || true)"
case "$last" in
  *"24576 slots"*) ;;
  *) echo "The GPU cache is not all-resident: ${last:-no cache line found}"
     echo "Set --short-read 4096 in the Strata config or restart it. FORCE=1 runs anyway."
     [ "${FORCE:-0}" = 1 ] || exit 1 ;;
esac

# 3. leave memory for the model
avail="$(awk '/MemAvailable/ {print int($2/1048576)}' /proc/meminfo)"
[ "$avail" -ge 3 ] || { echo "only ${avail} GiB of memory available; not starting the agent"; exit 1; }

docker image inspect codex-sandbox >/dev/null 2>&1 || { echo "image codex-sandbox is missing: run strata-agent-setup.sh"; exit 1; }
docker rm -f strata-agent >/dev/null 2>&1 || true

common=(--rm --name strata-agent --network host
        --user "$(id -u):$(id -g)" -e HOME=/home/agent
        --memory 4g --memory-swap 4g --cpus 2 --pids-limit 512
        --cap-drop ALL --security-opt no-new-privileges
        -v "$AGENT/work:/work" -v "$AGENT/codex-home:/home/agent/.codex"
        -w "/work/$proj")

if [ "$1" = "-i" ]; then
  exec docker run "${common[@]}" -it codex-sandbox codex -s "$SANDBOX" -c 'approval_policy="on-request"'
fi

# no -i and stdin from /dev/null: codex exec waits for stdin to close when it is attached ("Reading additional input from stdin...")
mkdir -p "$AGENT/logs"
out="$AGENT/logs/$proj-$(date +%F-%H%M%S).log"
echo "Running in a container (4 GiB memory, 2 CPUs, sees only $AGENT/work, host network). Sandbox mode: $SANDBOX. Log: $out   Stop with: docker stop strata-agent"
docker run "${common[@]}" codex-sandbox \
  codex exec --skip-git-repo-check -s "$SANDBOX" -c 'approval_policy="never"' "$1" < /dev/null 2>&1 | tee "$out"
RUNSH
chmod +x "$AGENT/bin/agent-run.sh"

# ---------------------------------------------------------------- 4. Strata config helper
cat > "$AGENT/bin/strata-agent-config.sh" <<'CFGSH'
#!/usr/bin/env bash
# One-off edit of the Strata config for agent use. Makes a backup first. Restart Strata afterwards.
#   tool_call_recovery: true     repairs tool calls the model writes in the wrong format (Strata opt-in)
#   reasoning_effort:  "low"     default thinking level for requests that don't name one
set -euo pipefail
STRATA="${STRATA_DIR:-$HOME/github/strata-work/Strata}"
cfg="$STRATA/strata-swift-iq3_xxs.json"
[ -f "$cfg" ] || { echo "no config at $cfg"; exit 1; }
n=1; while [ -e "$cfg.bak$n" ]; do n=$((n+1)); done
cp "$cfg" "$cfg.bak$n"; echo "backup: $cfg.bak$n"
python3 - "$cfg" <<'PY'
import json, sys
p = sys.argv[1]; c = json.load(open(p))
for k, v in (("tool_call_recovery", True), ("reasoning_effort", "low")):
    print(f"{k}: {c.get(k)!r} -> {v!r}")
    c[k] = v
json.dump(c, open(p, "w"), indent=2)
PY
echo "Now restart Strata: Ctrl+C in its window, then ~/start-swift.sh"
echo "To undo: cp $cfg.bak$n $cfg"
CFGSH
chmod +x "$AGENT/bin/strata-agent-config.sh"

# ---------------------------------------------------------------- 5. first task
proj="$AGENT/work/textstats"
if [ ! -d "$proj" ]; then
  mkdir -p "$proj"
  cat > "$proj/textstats.py" <<'PYSTUB'
"""Small text statistics helpers. Implement every function."""


def word_count(text: str) -> int:
    """Number of whitespace-separated words."""
    raise NotImplementedError


def most_common_word(text: str):
    """Most frequent word, case-insensitive, returned in lower case.
    Ties go to the alphabetically first word. Returns None for empty text."""
    raise NotImplementedError


def average_word_length(text: str) -> float:
    """Mean number of characters per word, or 0.0 for empty text."""
    raise NotImplementedError


def is_palindrome(text: str) -> bool:
    """True if text reads the same backwards, ignoring case and anything that is not a letter or digit."""
    raise NotImplementedError
PYSTUB
  cat > "$proj/test_textstats.py" <<'PYTEST'
import unittest

from textstats import average_word_length, is_palindrome, most_common_word, word_count


class TestTextStats(unittest.TestCase):
    def test_word_count(self):
        self.assertEqual(word_count("the quick brown fox"), 4)
        self.assertEqual(word_count("  spaced   out  "), 2)
        self.assertEqual(word_count(""), 0)

    def test_most_common_word(self):
        self.assertEqual(most_common_word("a b b c c c"), "c")
        self.assertEqual(most_common_word("The the THE cat"), "the")
        self.assertEqual(most_common_word("b a"), "a")
        self.assertIsNone(most_common_word(""))

    def test_average_word_length(self):
        self.assertEqual(average_word_length("aa bbbb"), 3.0)
        self.assertEqual(average_word_length("a bb ccc"), 2.0)
        self.assertEqual(average_word_length(""), 0.0)

    def test_is_palindrome(self):
        self.assertTrue(is_palindrome("A man, a plan, a canal: Panama"))
        self.assertFalse(is_palindrome("hello"))
        self.assertTrue(is_palindrome(""))


if __name__ == "__main__":
    unittest.main()
PYTEST
  printf '__pycache__/\n' > "$proj/.gitignore"
  ( cd "$proj" && git init -q && git config user.email agent@localhost && git config user.name agent \
      && git add -A && git commit -q -m "starting point: stubs and tests" ) || echo "(git setup skipped)"
fi

# ---------------------------------------------------------------- 6. build
if [ "${SKIP_BUILD:-0}" = 1 ]; then
  echo "SKIP_BUILD=1: files written, image not built"
else
  echo "Building the image (a few minutes)..."
  docker build -t codex-sandbox "$AGENT"
  echo "Codex in the image: $(docker run --rm codex-sandbox codex --version)"
fi

cat <<EOF

Done. Next steps:
  1. Edit the Strata config (backup is automatic), then restart Strata:
       $AGENT/bin/strata-agent-config.sh
       (Ctrl+C in Strata's window, then ~/start-swift.sh)
  2. Smoke test (about 3 to 6 minutes; the first prompt is about 9,400 tokens):
       $AGENT/bin/agent-run.sh smoke
  3. First real task (about 20 to 60 minutes):
       $AGENT/bin/agent-run.sh textstats "Implement the four functions in textstats.py so that python3 -m unittest -v passes. Do not edit test_textstats.py. Run the tests and fix any failure."
  Logs: $AGENT/logs/    Stop a run: docker stop strata-agent
EOF
