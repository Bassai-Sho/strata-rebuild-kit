#!/usr/bin/env bash
# install-codex-client.sh: one-time setup of the `codex` command. Safe to run again.
#   bash install-codex-client.sh            install (keeps any file you edited)
#   bash install-codex-client.sh --update   the kit's version replaces yours for every file that differs (old copies saved in ~/agent/backups)
#   bash install-codex-client.sh --pull     YOUR live version goes into the kit for every file that differs (the kit's old copy is saved too)
#   bash install-codex-client.sh --no-pull  keep a newer live file but do not copy it back into the kit
#   bash install-codex-client.sh --pull --only=templates/project/AGENTS.md   --update / --pull for the files whose name contains that text only
#   Without a flag each file is decided by what changed since the last install (kept in ~/agent/.kit-baseline): the kit changed -> live is updated;
#   you changed it (or the loop added lessons to the project template) -> live is kept and copied into the kit, so `git diff` there shows it;
#   both changed -> merged line by line (a conflict changes nothing). A file with no history that differs is reported UNKNOWN and left alone.
#   bash install-codex-client.sh --check    read-only: say what is up to date and what an update would change; touches nothing
#
# After it, you only ever type:   codex        (new project)    codex NAME    (work on one)    codex help
#
# What it changes, all inside ~/agent plus one link:
#   ~/agent/bin/codex-client                the launcher
#   ~/.local/bin/codex                      a link to it (nothing is installed on the host: Codex itself runs only in the container)
#   ~/agent/bin/strata-front.py             a tiny local filter: Codex's screen asks the model for a session title on every prompt, which costs
#                                           Strata a minute and empties its cache; the filter answers that one request itself
#                                           It also answers Codex's COMPACTION request at once (~/agent/bin/synth_compact.py): Strata would re-read the whole
#                                           context for it, about 18 minutes at 47K tokens; the filter writes the handoff from PLAN.md, progress.txt and the history.
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
UPDATE=0; CHECK=0; PULL=0; NOPULL=0; ONLY=""
for arg in "$@"; do
  case "$arg" in --update) UPDATE=1;; --check) CHECK=1;; --pull) PULL=1;; --no-pull) NOPULL=1;; --only=*) ONLY="${arg#--only=}";; esac
done
KITVER="$(cat "$HERE/../VERSION" 2>/dev/null || echo unknown)"

# Every file the kit installs is kept in step by kit-sync.py, which remembers what it installed last time (a baseline in ~/agent/.kit-baseline) and so can
# tell WHICH side changed: kit changed -> live is updated; live changed -> live is kept and copied back into the kit; both -> merged line by line.
KS="$HERE/kit-sync.py"; BASE="$AGENT/.kit-baseline"
[ -f "$KS" ] || { echo "kit-sync.py is missing next to this installer"; exit 1; }
ksync() {   # ksync KEY KITFILE LIVEFILE LABEL [MODE]
  local mode=install flags=(); [ "$CHECK" = 1 ] && mode=check
  [ "$UPDATE" = 1 ] && flags+=(--kit-wins); [ "$PULL" = 1 ] && flags+=(--live-wins); [ "$NOPULL" = 1 ] && flags+=(--no-pull)
  # --only=TEXT limits --update / --pull to the files whose key contains TEXT; the others are decided by what changed, as without a flag
  if [ -n "$ONLY" ] && [[ "$1" != *"$ONLY"* ]]; then flags=(); [ "$NOPULL" = 1 ] && flags+=(--no-pull); fi
  python3 "$KS" "$mode" "$1" --kit "$2" --live "$3" --label "$4" --mode "${5:-644}" --base "$BASE" --backups "$AGENT/backups" ${flags[@]+"${flags[@]}"}
}
sync_bin() {   # sync_bin SRC DST LABEL
  local m=644; case "$2" in *.py|*codex-client) m=755 ;; esac
  ksync "bin/$(basename "$2")" "$1" "$2" "$3" "$m"
}

[ -d "$AGENT/codex-home" ] || { echo "~/agent is not set up yet: run  bash $HERE/strata-agent-setup.sh  first"; exit 1; }
[ -f "$HERE/codex-client" ] || { echo "codex-client is missing next to this installer"; exit 1; }
[ "$CHECK" = 1 ] || mkdir -p "$AGENT/bin" "$AGENT/work" "$HOME/.local/bin" "$AGENT/codex-home/templates/project/src" "$AGENT/codex-home/templates/project/tests"

# ---- 1. the launcher and its link -------------------------------------------------------------------------------------------
link="$HOME/.local/bin/codex"
if [ "$CHECK" = 1 ]; then
  ksync bin/codex-client "$HERE/codex-client" "$AGENT/bin/codex-client" "the codex command (launcher)" 755
  [ "$(readlink -f "$link" 2>/dev/null)" = "$(readlink -f "$AGENT/bin/codex-client" 2>/dev/null)" ] && echo "ok       $link points at the launcher" || echo "UPDATE   $link is missing or something else"
elif [ -e "$link" ] && [ "$(readlink -f "$link")" != "$(readlink -f "$AGENT/bin/codex-client")" ]; then
  ksync bin/codex-client "$HERE/codex-client" "$AGENT/bin/codex-client" "the codex command (launcher)" 755
  echo "NOT linking: $link already exists and is something else. Use $AGENT/bin/codex-client directly, or remove that file."
else
  ksync bin/codex-client "$HERE/codex-client" "$AGENT/bin/codex-client" "the codex command (launcher)" 755
  ln -sf "$AGENT/bin/codex-client" "$link"; echo "linked: $link"
fi
case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) echo "NOTE: $HOME/.local/bin is not on your PATH yet. Add this line to ~/.profile and log in again:  export PATH=\"\$HOME/.local/bin:\$PATH\"" ;; esac

# ---- 2. files the sessions read ------------------------------------------------------------------------------------------------
ksync codex-home/AGENTS.md "$HERE/defaults/workspace-AGENTS.md" "$AGENT/codex-home/AGENTS.md" "the workspace rules (codex-home/AGENTS.md)"

T="$AGENT/codex-home/templates/project"
ksync codex-home/templates/project/AGENTS.md "$HERE/defaults/project-AGENTS.md" "$T/AGENTS.md" "the project template AGENTS.md"
ksync codex-home/templates/project/PLAN.md "$HERE/defaults/project-PLAN.md" "$T/PLAN.md" "the project template PLAN.md"
ksync codex-home/templates/project/SPEC.md "$HERE/defaults/project-SPEC.md" "$T/SPEC.md" "the project template SPEC.md"
ksync codex-home/templates/project/README.md "$HERE/defaults/project-README.md" "$T/README.md" "the project template README.md"
ksync codex-home/templates/project/.gitignore "$HERE/defaults/project-gitignore" "$T/.gitignore" "the project template .gitignore"
if [ "$CHECK" != 1 ]; then
  [ -e "$T/progress.txt" ] || : > "$T/progress.txt"
  [ -e "$T/src/.gitkeep" ] || : > "$T/src/.gitkeep"
  [ -e "$T/tests/.gitkeep" ] || : > "$T/tests/.gitkeep"
fi

# ---- 2a. the title filter (the `codex` command starts it when needed) ---------------------------------------------------------------------
sync_bin "$HERE/strata-front.py" "$AGENT/bin/strata-front.py" "the title filter"

sync_bin "$HERE/codex-loop.py" "$AGENT/bin/codex-loop.py" "the unattended loop (codex loop)"
sync_bin "$HERE/unix-relay.py" "$AGENT/bin/unix-relay.py" "the in-container relay (the loop's only way out)"
sync_bin "$HERE/make-model-catalog.py" "$AGENT/bin/make-model-catalog.py" "the model-catalog generator (rerun it after updating Codex)"
sync_bin "$HERE/slim-instructions.md" "$AGENT/bin/slim-instructions.md" "the slim base instructions the generator puts in the catalog"
sync_bin "$HERE/synth_compact.py" "$AGENT/bin/synth_compact.py" "the compaction shortcut (the filter answers Codex's compaction request at once)"
sync_bin "$HERE/apply_patch_rescue.py" "$AGENT/bin/apply_patch_rescue.py" "the apply_patch repair (a call the model wrote as text becomes a real call)"
sync_bin "$HERE/codex-stats.py" "$AGENT/bin/codex-stats.py" "codex-stats.py (where a session's time and context went)"
# the model catalog: without it Codex does not know the reasoning levels of `strata` and the shortcuts Alt+. and Alt+, say "unavailable"
ksync codex-home/model-catalog.json "$HERE/model-catalog.json" "$AGENT/codex-home/model-catalog.json" "the model catalog (reasoning shortcuts)" 644
[ "$CHECK" = 1 ] || mkdir -p "$AGENT/run" "$AGENT/logs"

# ---- 2b. the real-browser check (the sessions call it as `browser-check`; it needs the browser layer in the image) -------------------------------------------------
[ "$CHECK" = 1 ] || mkdir -p "$AGENT/codex-home/tools"
ksync codex-home/tools/browser-check.py "$HERE/browser-check.py" "$AGENT/codex-home/tools/browser-check.py" "the browser-check tool" 755
if command -v docker >/dev/null 2>&1 && docker image inspect codex-sandbox >/dev/null 2>&1; then
  if docker run --rm codex-sandbox sh -c 'command -v browser-check' >/dev/null 2>&1; then echo "ok       the image has a real browser (browser-check)"
  else echo "OPTIONAL the image has no browser yet. For real-browser checks of web projects run:  bash $HERE/add-browser-to-image.sh   (about 300 MB, a few minutes)"; fi
fi

# ---- 3. Codex's own settings -------------------------------------------------------------------------------------------------------
CHECK="$CHECK" COMPACT_FILE="$HERE/compact-prompt.txt" python3 - "$AGENT/codex-home/config.toml" <<'PY'
import os, re, shutil, sys, tomllib
p = sys.argv[1]
text = open(p).read() if os.path.exists(p) else ""
TOP = {"model_context_window": "65536", "model_auto_compact_token_limit": "48000", "tool_output_token_limit": "2500",
       "model_catalog_json": '"/home/agent/.codex/model-catalog.json"', "check_for_update_on_startup": "false"}
FEAT = {"goals": "false", "multi_agent": "false", "apps": "false", "plugins": "false", "view_image": "false", "worktrees": "false", "daemon_auto_start": "false"}
SKILLS_TABLE = "[skills.bundled]"          # Codex's four built-in skills: about 600 tokens in every prompt, unusable by a local model
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
new = re.sub(r"\n{3,}", "\n\n", new)        # a first run on an empty file left a double blank line, so a second run rewrote the file
cf = os.environ.get("COMPACT_FILE", "")          # compact_prompt: inline in config.toml (a file path would break the tools that run Codex on a COPY of the home)
ctext = open(cf).read().strip("\n") if cf and os.path.isfile(cf) else ""
if ctext and "'''" not in ctext:
    new = re.sub(r"^experimental_compact_prompt_file\s*=.*\n", "", new, flags=re.M)
    new = re.sub(r"^compact_prompt\s*=\s*'''.*?'''[ \t]*\n", "", new, flags=re.M | re.S)
    new = re.sub(r"^compact_prompt\s*=.*\n", "", new, flags=re.M)
    m = re.search(r"^model_auto_compact_token_limit\s*=.*\n", new, flags=re.M)
    new = new[:m.end()] + "compact_prompt = '''\n" + ctext + "\n'''\n" + new[m.end():]
if SKILLS_TABLE not in new:
    new += "\n[skills.bundled]\nenabled = false\n"
if "[model_providers.strata_front]" not in new:   # a second provider for the interactive screen: same Strata, through the title filter
    base = tomllib.loads(new).get("model_providers", {}).get("strata", {})
    new += ("\n[model_providers.strata_front]\nname = \"Strata (local, via the title filter)\"\nbase_url = \"http://127.0.0.1:8086/v1\"\n"
            f"wire_api = \"responses\"\nstream_idle_timeout_ms = {int(base.get('stream_idle_timeout_ms', 600000))}\n")
cfg = tomllib.loads(new)                       # refuse to write anything that does not parse
assert all(cfg[k] == tomllib.loads(f"{k} = {v}")[k] for k, v in TOP.items()) and all(cfg["features"][k] is False for k in FEAT)
assert cfg.get("skills", {}).get("bundled", {}).get("enabled") is False
assert not ctext or cfg.get("compact_prompt", "").strip() == ctext.strip()
if os.environ.get("CHECK") == "1":
    print("ok       config.toml" if new == text else "UPDATE   config.toml: settings missing (the installer adds them, after a backup)")
elif new == text:
    print("config.toml already set: nothing to change")
else:
    if os.path.exists(p) and not os.path.exists(p + ".bak-client"):
        shutil.copy(p, p + ".bak-client"); print("backup:", p + ".bak-client")
    open(p, "w").write(new)
    print("config.toml: provider strata_front added (the interactive screen uses it; agent-run.sh still talks to Strata directly)")
    print("config.toml: context window 65536, compaction at 48000, tool output capped at 2500 tokens; goals, multi_agent, apps, plugins, view_image, worktrees and the background server off")
    print("config.toml: Codex's built-in skills off (about 600 tokens of every prompt)")
    if ctext:
        print("config.toml: compact_prompt set (starts with the marker the filter looks for)")
PY

if [ "$CHECK" = 1 ]; then
  if [ -f "$AGENT/.kit-version" ] && [ "$(cat "$AGENT/.kit-version")" = "$KITVER" ]; then echo "ok       installed from kit version $KITVER"; else echo "UPDATE   kit version $KITVER is newer than the installed ${INSTALLED_VER:-$(cat "$AGENT/.kit-version" 2>/dev/null || echo none)}"; fi
  echo; echo "(read-only check: nothing was changed. To apply: bash $0 --update)"; exit 0
fi
printf '%s\n' "$KITVER" > "$AGENT/.kit-version"
echo
echo "Done (kit version $KITVER). Type:   codex          (new project)     codex help"
echo "The first start needs: Strata running (codex starts it for you if it is not) and the image codex-sandbox (strata-agent-setup.sh builds it)."
