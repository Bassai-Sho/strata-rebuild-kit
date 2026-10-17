#!/usr/bin/env bash
# install-codex-client.sh: one-time setup of the `codex` command. Safe to run again.
#   bash install-codex-client.sh            install (keeps any file you edited)
#   bash install-codex-client.sh --update   also replace the workspace rules and the project template (old ones saved as .bak)
#   bash install-codex-client.sh --check    read-only: say what is up to date and what an update would change; touches nothing
#
# After it, you only ever type:   codex        (new project)    codex NAME    (work on one)    codex help
#
# What it changes, all inside ~/agent plus one link:
#   ~/agent/bin/codex-client                the launcher
#   ~/.local/bin/codex                      a link to it (nothing is installed on the host: Codex itself runs only in the container)
#   ~/agent/bin/strata-front.py             a tiny local filter: Codex's screen asks the model for a session title on every prompt, which costs
#                                           Strata a minute and empties its cache; the filter answers that one request itself
#   ~/agent/bin/codex-loop.py, unix-relay.py   `codex loop`: unattended runs, one task per fresh session, each check run by the loop itself,
#                                           in a container with no network (its only way out is a socket to the filter above)
#                                           (also check_for_update_on_startup = false: a pinned binary in a container should not phone home or nag;
#                                           Codex is updated with tools/codex-update.py)
#   ~/agent/codex-home/model-catalog.json   tells Codex the reasoning levels of `strata`, so Alt+. and Alt+, work (none, low, medium, high)
#   ~/agent/codex-home/AGENTS.md            the rules every session reads: how to start a new project, how to work in one
#   ~/agent/codex-home/templates/project/   the skeleton a new project is copied from (the session does the copying)
#   ~/agent/codex-home/tools/browser-check.py   the real-browser check the sessions run on web projects
#   ~/agent/codex-home/config.toml          backup first (config.toml.bak-client); then the context window matches Strata (65,536),
#                                           and Codex's goal, multi-agent, apps, plugins, image and worktree tools are switched off, and so is its
#                                           background server (which fails with this standalone binary): together the tools cost about 4,500
#                                           tokens of every prompt the local model has to read (about 110 s on a cold start) and it cannot use them
set -euo pipefail
AGENT="${AGENT_DIR:-$HOME/agent}"
HERE="$(cd "$(dirname "$0")" && pwd)"
UPDATE=0; [ "${1:-}" = "--update" ] && UPDATE=1
CHECK=0;  [ "${1:-}" = "--check" ] && CHECK=1
KITVER="$(cat "$HERE/../VERSION" 2>/dev/null || echo unknown)"

[ -d "$AGENT/codex-home" ] || { echo "~/agent is not set up yet: run  bash $HERE/strata-agent-setup.sh  first"; exit 1; }
[ -f "$HERE/codex-client" ] || { echo "codex-client is missing next to this installer"; exit 1; }
[ "$CHECK" = 1 ] || mkdir -p "$AGENT/bin" "$AGENT/work" "$HOME/.local/bin" "$AGENT/codex-home/templates/project/src" "$AGENT/codex-home/templates/project/tests"

# ---- 1. the launcher and its link -------------------------------------------------------------------------------------------
link="$HOME/.local/bin/codex"
if [ "$CHECK" = 1 ]; then
  cmp -s "$HERE/codex-client" "$AGENT/bin/codex-client" && echo "ok       the codex command (launcher is current)" || echo "UPDATE   the codex command: launcher differs or is missing"
  [ "$(readlink -f "$link" 2>/dev/null)" = "$(readlink -f "$AGENT/bin/codex-client" 2>/dev/null)" ] && echo "ok       $link points at the launcher" || echo "UPDATE   $link is missing or something else"
elif [ -e "$link" ] && [ "$(readlink -f "$link")" != "$(readlink -f "$AGENT/bin/codex-client")" ]; then
  install -m 755 "$HERE/codex-client" "$AGENT/bin/codex-client"
  echo "NOT linking: $link already exists and is something else. Use $AGENT/bin/codex-client directly, or remove that file."
else
  install -m 755 "$HERE/codex-client" "$AGENT/bin/codex-client"
  ln -sf "$AGENT/bin/codex-client" "$link"; echo "linked: $link"
fi
case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) echo "NOTE: $HOME/.local/bin is not on your PATH yet. Add this line to ~/.profile and log in again:  export PATH=\"\$HOME/.local/bin:\$PATH\"" ;; esac

# ---- 2. files the sessions read ------------------------------------------------------------------------------------------------
put() {   # put PATH : writes stdin to PATH unless it already exists and was edited; --update replaces it (old copy kept)
  local f="$1" tmp; tmp="$(mktemp)"; cat > "$tmp"
  if [ "$CHECK" = 1 ]; then
    if [ ! -e "$f" ]; then echo "MISSING  $f"; elif cmp -s "$tmp" "$f"; then echo "ok       $f"; else echo "UPDATE   $f differs from the new default (--update replaces it, keeping a .bak)"; fi
    rm -f "$tmp"; return 0
  fi
  if [ -e "$f" ] && ! cmp -s "$tmp" "$f"; then
    if [ "$UPDATE" = 1 ]; then cp "$f" "$f.bak"; cp "$tmp" "$f"; echo "updated: $f (old copy: $f.bak)"
    else echo "kept your edited: $f  (the newer default is in $f.new; --update replaces yours)"; cp "$tmp" "$f.new"; fi
  elif [ ! -e "$f" ]; then cp "$tmp" "$f"; echo "wrote:   $f"; fi
  rm -f "$tmp"
}

put "$AGENT/codex-home/AGENTS.md" <<'EOF'
# Workspace rules

You work inside /work. Every project is its own folder: /work/NAME. Keep answers short. One small step at a time.

## New project
A project is new when you were started in /work itself, or when its folder has no PLAN.md.
1. If you are in /work: if the user's first message does not already say what to build, ask ONE short question and wait. Choose a folder name that does not exist yet (ls /work) and run: mkdir /work/NAME && cd /work/NAME
2. In the project folder run: cp -r --update=none ~/.codex/templates/project/. . ; git init -q
   (this keeps files that are already there, such as SPEC.md)
3. If SPEC.md has content it is the user's design: ask no questions, read it in pieces (sed -n 'A,Bp'), never rewrite it. Write PLAN.md from it: 5 to 8 tasks, one line each:
   - [ ] T1: goal | files: a, b | spec: lines A-B | check: `command` prints OK
   Every task needs files: and a check in backticks that FAILS now and passes only when the task is done (never echo or true).
   The last task is "Acceptance": its check is a command that tests the whole thing. Put the check command in AGENTS.md. Write README.md (3 lines). Commit.
4. Tell the user: "Project ready. Next time: codex NAME". Then do T1.
From then on run every command inside the project: cd /work/NAME && ...

## Working in a project
- Read AGENTS.md and PLAN.md first. Do the first unticked task only. Read only the SPEC.md lines it names. Run its check. Tick it. Commit.
- Read files with head, sed -n and grep -n; never print a whole large file. Edit with small patches (rewrite a whole file only under 80 lines).
- Never touch files outside the project folder. Do not download or install anything unless asked.
- Never ask whether to proceed and never use request_user_input; ask in plain text only when you cannot continue without an answer.
EOF

T="$AGENT/codex-home/templates/project"
put "$T/AGENTS.md" <<'EOF'
# Project rules
- ONE task per session: the first "- [ ]" line in PLAN.md. Read only the SPEC.md lines it names. Then stop.
- Check command (run it after every change): TO_BE_FILLED_IN
  Web page or game: browser-check index.html --keys ArrowLeft,Space  (a REAL browser; --help lists more). Command-line program: run it with real input and compare the output.
- A task is done only when its check prints OK. Never tick it, or say it is done, before that.
- Keep command output short (head, tail -20, grep -n). Edit with small patches.
- When done: tick the task in PLAN.md, add one line to progress.txt, then: git add -A && git commit -qm "<task>". Then stop. No extra polish.
- If the check still fails after 3 attempts: write what you tried in progress.txt, commit, and stop.
EOF
put "$T/PLAN.md" <<'EOF'
# Plan

- [ ] T1: replace this line | files: a, b | spec: lines A-B | check: `command` prints OK
- [ ] T2: acceptance, and README says how to run it | files: README.md | check: `command that tests the whole thing` prints OK
EOF
put "$T/SPEC.md" <<'EOF'
# Spec

The design goes here (the user writes or pastes it). The agent reads it in pieces and never rewrites it.
EOF
put "$T/README.md" <<'EOF'
# New project

What it is, in three lines, and how to run it (written by the first and the last session).
EOF
put "$T/.gitignore" <<'EOF'
__pycache__/
*.pyc
node_modules/
artifacts/
.DS_Store
EOF
if [ "$CHECK" != 1 ]; then
  [ -e "$T/progress.txt" ] || : > "$T/progress.txt"
  [ -e "$T/src/.gitkeep" ] || : > "$T/src/.gitkeep"
  [ -e "$T/tests/.gitkeep" ] || : > "$T/tests/.gitkeep"
fi

# ---- 2a. the title filter (the `codex` command starts it when needed) ---------------------------------------------------------------------
if [ "$CHECK" = 1 ]; then
  cmp -s "$HERE/strata-front.py" "$AGENT/bin/strata-front.py" && echo "ok       the title filter" || echo "UPDATE   the title filter ($AGENT/bin/strata-front.py) differs or is missing"
elif [ -f "$HERE/strata-front.py" ]; then
  if cmp -s "$HERE/strata-front.py" "$AGENT/bin/strata-front.py"; then echo "ok:      $AGENT/bin/strata-front.py"; else install -m 755 "$HERE/strata-front.py" "$AGENT/bin/strata-front.py"; echo "wrote:   $AGENT/bin/strata-front.py"; fi
fi

sync_bin() {   # sync_bin SRC DST LABEL
  if [ "$CHECK" = 1 ]; then
    cmp -s "$1" "$2" && echo "ok       $3" || echo "UPDATE   $3 ($2) differs or is missing"
  elif [ -f "$1" ]; then
    if cmp -s "$1" "$2"; then echo "ok:      $2"; else install -m 755 "$1" "$2"; echo "wrote:   $2"; fi
  fi
}
sync_bin "$HERE/codex-loop.py" "$AGENT/bin/codex-loop.py" "the unattended loop (codex loop)"
sync_bin "$HERE/unix-relay.py" "$AGENT/bin/unix-relay.py" "the in-container relay (the loop's only way out)"
sync_bin "$HERE/make-model-catalog.py" "$AGENT/bin/make-model-catalog.py" "the model-catalog generator (rerun it after updating Codex)"
# the model catalog: without it Codex does not know the reasoning levels of `strata` and the shortcuts Alt+. and Alt+, say "unavailable"
if [ "$CHECK" = 1 ]; then
  cmp -s "$HERE/model-catalog.json" "$AGENT/codex-home/model-catalog.json" && echo "ok       the model catalog (reasoning shortcuts)" || echo "UPDATE   the model catalog ($AGENT/codex-home/model-catalog.json) differs or is missing"
elif [ -f "$HERE/model-catalog.json" ]; then
  if cmp -s "$HERE/model-catalog.json" "$AGENT/codex-home/model-catalog.json"; then echo "ok:      $AGENT/codex-home/model-catalog.json"; else install -m 644 "$HERE/model-catalog.json" "$AGENT/codex-home/model-catalog.json"; echo "wrote:   $AGENT/codex-home/model-catalog.json"; fi
fi
[ "$CHECK" = 1 ] || mkdir -p "$AGENT/run" "$AGENT/logs"

# ---- 2b. the real-browser check (the sessions call it as `browser-check`; it needs the browser layer in the image) -------------------------------------------------
if [ "$CHECK" = 1 ]; then
  cmp -s "$HERE/browser-check.py" "$AGENT/codex-home/tools/browser-check.py" && echo "ok       the browser-check tool" || echo "UPDATE   the browser-check tool ($AGENT/codex-home/tools/browser-check.py) differs or is missing"
else
  mkdir -p "$AGENT/codex-home/tools"
  if [ -f "$HERE/browser-check.py" ]; then
    if cmp -s "$HERE/browser-check.py" "$AGENT/codex-home/tools/browser-check.py"; then echo "ok:      $AGENT/codex-home/tools/browser-check.py"; else install -m 755 "$HERE/browser-check.py" "$AGENT/codex-home/tools/browser-check.py"; echo "wrote:   $AGENT/codex-home/tools/browser-check.py"; fi
  fi
fi
if command -v docker >/dev/null 2>&1 && docker image inspect codex-sandbox >/dev/null 2>&1; then
  if docker run --rm codex-sandbox sh -c 'command -v browser-check' >/dev/null 2>&1; then echo "ok       the image has a real browser (browser-check)"
  else echo "OPTIONAL the image has no browser yet. For real-browser checks of web projects run:  bash $HERE/add-browser-to-image.sh   (about 300 MB, a few minutes)"; fi
fi

# ---- 3. Codex's own settings -------------------------------------------------------------------------------------------------------
CHECK="$CHECK" python3 - "$AGENT/codex-home/config.toml" <<'PY'
import os, re, shutil, sys, tomllib
p = sys.argv[1]
text = open(p).read() if os.path.exists(p) else ""
TOP = {"model_context_window": "65536", "model_auto_compact_token_limit": "48000", "tool_output_token_limit": "4000",
       "model_catalog_json": '"/home/agent/.codex/model-catalog.json"', "check_for_update_on_startup": "false"}
FEAT = {"goals": "false", "multi_agent": "false", "apps": "false", "plugins": "false", "view_image": "false", "worktrees": "false", "daemon_auto_start": "false"}
lines = text.split("\n")
first = next((i for i, l in enumerate(lines) if re.match(r"\s*\[", l)), len(lines))
head, tail = lines[:first], lines[first:]
while head and head[-1].strip() == "":
    head.pop()
for k, v in TOP.items():                      # top-level keys must stay above the first [table], or they land inside it
    for i, l in enumerate(head):
        if re.match(rf"^\s*{k}\s*=", l):
            head[i] = f"{k} = {v}"
            break
    else:
        head.append(f"{k} = {v}")
if head:
    head.append("")
t_idx = next((i for i, l in enumerate(tail) if l.strip() == "[features]"), None)
if t_idx is None:
    while tail and tail[-1].strip() == "":
        tail.pop()
    tail += ["", "[features]"] + [f"{k} = {v}" for k, v in FEAT.items()]
else:
    end = next((j for j in range(t_idx + 1, len(tail)) if re.match(r"\s*\[", tail[j])), len(tail))
    sect = tail[t_idx + 1:end]
    for k, v in FEAT.items():
        for i, l in enumerate(sect):
            if re.match(rf"^\s*{k}\s*=", l):
                sect[i] = f"{k} = {v}"
                break
        else:
            sect.append(f"{k} = {v}")
    tail = tail[:t_idx + 1] + sect + tail[end:]
new = "\n".join(head + tail).rstrip("\n") + "\n"
if "[model_providers.strata_front]" not in new:   # a second provider for the interactive screen: same Strata, through the title filter
    base = tomllib.loads(new).get("model_providers", {}).get("strata", {})
    new += ("\n[model_providers.strata_front]\nname = \"Strata (local, via the title filter)\"\nbase_url = \"http://127.0.0.1:8086/v1\"\n"
            f"wire_api = \"responses\"\nstream_idle_timeout_ms = {int(base.get('stream_idle_timeout_ms', 600000))}\n")
cfg = tomllib.loads(new)                       # refuse to write anything that does not parse
assert all(cfg[k] == tomllib.loads(f"{k} = {v}")[k] for k, v in TOP.items()) and all(cfg["features"][k] is False for k in FEAT)
if os.environ.get("CHECK") == "1":
    print("ok       config.toml" if new == text else "UPDATE   config.toml: settings missing (the installer adds them, after a backup)")
elif new == text:
    print("config.toml already set: nothing to change")
else:
    if os.path.exists(p) and not os.path.exists(p + ".bak-client"):
        shutil.copy(p, p + ".bak-client"); print("backup:", p + ".bak-client")
    open(p, "w").write(new)
    print("config.toml: provider strata_front added (the interactive screen uses it; agent-run.sh still talks to Strata directly)")
    print("config.toml: context window 65536, compaction at 48000, tool output capped at 4000 tokens; goals, multi_agent, apps, plugins, view_image, worktrees and the background server off")
PY

if [ "$CHECK" = 1 ]; then
  if [ -f "$AGENT/.kit-version" ] && [ "$(cat "$AGENT/.kit-version")" = "$KITVER" ]; then echo "ok       installed from kit version $KITVER"; else echo "UPDATE   kit version $KITVER is newer than the installed ${INSTALLED_VER:-$(cat "$AGENT/.kit-version" 2>/dev/null || echo none)}"; fi
  echo; echo "(read-only check: nothing was changed. To apply: bash $0 --update)"; exit 0
fi
printf '%s\n' "$KITVER" > "$AGENT/.kit-version"
echo
echo "Done (kit version $KITVER). Type:   codex          (new project)     codex help"
echo "The first start needs: Strata running (codex starts it for you if it is not) and the image codex-sandbox (strata-agent-setup.sh builds it)."
