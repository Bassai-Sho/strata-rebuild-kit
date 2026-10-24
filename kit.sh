#!/usr/bin/env bash
# kit.sh: what to do after extracting the kit.
#
#   bash ~/strata-rebuild-kit/kit.sh status      read-only: what is installed and what is out of date
#   bash ~/strata-rebuild-kit/kit.sh update      bring the `codex` command, its rules, its template and its tools up to date
#   bash ~/strata-rebuild-kit/kit.sh help
#
# Extracting the kit changes NOTHING on your system: it only unpacks files into ~/strata-rebuild-kit. This script is the step after it.
# `update` touches only ~/agent (the codex command and what its sessions read) and asks before building anything. It never touches
# the Strata source, its config or the patches: those change Strata itself, so the playbook has you do them by hand.
set -u
KIT="$(cd "$(dirname "$0")" && pwd)"
AGENT="${AGENT_DIR:-$HOME/agent}"
STRATA="${STRATA_DIR:-$HOME/github/strata-work/Strata}"
URL="${STRATA_URL:-http://127.0.0.1:8085}"
say() { printf '%s\n' "$*"; }
KITVER="$(cat "$KIT/VERSION" 2>/dev/null || echo unknown)"

patch_state() {   # patch_state NAME STEP : the state of one patch across the kit's files for it (NAME.patch and NAME.SERIES.patch, remade for newer Strata lines)
  local name="$1" step="${2:-8}" stem f s best="" clean=""
  stem="${name%.patch}"
  if [ ! -f "$KIT/patches/$name" ] && ! ls "$KIT/patches/$stem".*.patch >/dev/null 2>&1; then echo "(not in this kit)"; return; fi
  for f in "$name" $(cd "$KIT/patches" 2>/dev/null && ls "$stem".*.patch 2>/dev/null); do
    [ -f "$KIT/patches/$f" ] || continue
    s="$(patch_state_one "$f" "$step")"
    case "$s" in applied*) echo "$s"; return ;; esac
    case "$s" in "not applied; it applies cleanly"*) [ -n "$clean" ] || clean="$s" ;; esac
    [ -n "$best" ] || best="$s"
  done
  echo "${clean:-$best}"
}

patch_state_one() {   # patch_state_one FILE STEP : applied / partly applied / can be applied / does not apply
  local p="$KIT/patches/$1" step="${2:-8}"
  [ -f "$p" ] || { echo "(not in this kit)"; return; }
  [ -d "$STRATA/.git" ] || { echo "(no Strata repository at $STRATA)"; return; }
  # Patches build on each other (the power patch changes lines the dashboard patch added), so `git apply -R --check` cannot recognise the
  # earlier one. Instead: how many of the patch's own added lines are present in the files it targets?
  local r hit tot
  r="$(python3 - "$p" "$STRATA" <<'PY'
import os, sys
patch, repo = sys.argv[1], sys.argv[2]
added, cur = {}, None
for ln in open(patch, errors="replace"):
    if ln.startswith("+++ b/"):
        cur = ln[6:].strip(); added.setdefault(cur, [])
    elif ln.startswith("+") and not ln.startswith("+++") and cur:
        t = ln[1:].strip()
        if len(t) >= 30:                                   # short lines (braces, returns) occur everywhere and prove nothing
            added[cur].append(t)
hit = tot = 0
for f, lines in added.items():
    try:
        have = {x.strip() for x in open(os.path.join(repo, f), errors="replace")}
    except OSError:
        have = set()                                       # a file the patch creates and that is not there yet
    tot += len(lines); hit += sum(1 for t in lines if t in have)
print(hit, tot)
PY
)"
  hit="${r% *}"; tot="${r#* }"
  if [ "${tot:-0}" -gt 0 ] && [ $((hit * 100 / tot)) -ge 80 ]; then echo "applied"
  elif [ "${hit:-0}" -gt 0 ] && [ $((hit * 100 / tot)) -ge 20 ]; then echo "partly applied ($hit of $tot added lines are present): check  git -C $STRATA diff --stat  before applying it"
  elif git -C "$STRATA" apply --check "$p" >/dev/null 2>&1; then echo "not applied; it applies cleanly (playbook Step $step)"
  else echo "not applied, and it does not apply cleanly here (playbook Step $step: apply the earlier patch first, or use --3way)"; fi
}

status_cmd() {
  INSTALLED="$(cat "$AGENT/.kit-version" 2>/dev/null || echo none)"
  say "Kit version: $KITVER      Installed from kit version: $INSTALLED"
  say ""
  say "== the codex command (what 'update' manages) =="
  if [ -d "$AGENT/codex-home" ]; then bash "$KIT/agent/install-codex-client.sh" --check 2>&1 | grep -vE "^\(read-only|^$|^NOTE:" 
  else say "not set up: ~/agent does not exist. Build the image first (playbook Step 9: strata-agent-setup.sh), then run  bash $KIT/kit.sh update"; fi
  say ""
  say "== Strata and its patches (changed by hand; playbook Steps 3 and 8) =="
  if [ "$(curl -s -o /dev/null -m 3 -w '%{http_code}' "$URL/v1/models" 2>/dev/null)" = 200 ]; then say "Strata:                 running on $URL"; else say "Strata:                 not running"; fi
  if [ -d "$STRATA/.git" ]; then
    local ver newest eng
    ver="$(git -C "$STRATA" describe --tags --exact-match HEAD 2>/dev/null || { t="$(git -C "$STRATA" describe --tags --abbrev=0 2>/dev/null)"; echo "${t:-unknown}+"; })"
    newest="$(git -C "$STRATA" tag --list 'v[0-9]*' 2>/dev/null | sort -V | tail -1)"
    eng="$(grep -a -o 'engine=[0-9][0-9.]*' "$STRATA/build-sycl/strata" 2>/dev/null | head -1 | cut -d= -f2 | sed 's/\.*$//')"
    say "Strata version:         $ver (engine binary: ${eng:-not built})"
    if [ -n "$newest" ] && [ "$newest" != "${ver%+}" ] && [ "$(printf '%s\n%s\n' "${ver%+}" "$newest" | sort -V | tail -1)" = "$newest" ]; then
      say "                        newer release known here: $newest. To test your patches against it (read-only):  python3 $KIT/tools/strata-update.py check"
    fi
  fi
  say "Meteor Lake patch:      $(patch_state mtl-igpu-local.patch 3)"
  local dash power quiet
  dash="$(patch_state xe-monitor-v2.patch 8)"; power="$(patch_state xe-power.patch 8)"; quiet="$(patch_state xe-monitor-quiet.patch 8)"
  # the power and quiet patches are made against the dashboard patch's result, so neither can be applied without it (and they rewrite some of its lines)
  [ "$power" = applied ] && [ "$dash" != applied ] && dash="applied (the power patch on top of it is applied)"
  [ "$quiet" = applied ] && [ "$dash" != applied ] && dash="applied (the throttle-note patch on top of it is applied)"
  say "Dashboard readings:     $dash"
  say "Package power:          $power"
  say "Throttle note hidden:   $quiet"
  say ""
  say "== images =="
  if command -v docker >/dev/null 2>&1; then
    for i in strata-sycl-dev codex-sandbox; do docker image inspect "$i" >/dev/null 2>&1 && say "$i: present" || say "$i: MISSING"; done
    if docker image inspect codex-sandbox >/dev/null 2>&1; then
      local cv; cv="$(docker run --rm codex-sandbox codex --version 2>/dev/null | grep -o '[0-9][0-9.]*[0-9]' | head -1)"
      say "Codex in the image: ${cv:-unknown}   (the version the kit was tested with: $(cat "$KIT/agent/CODEX_VERSION" 2>/dev/null || echo ?); to test a newer release:  python3 $KIT/tools/codex-update.py check)"
    fi
  else say "docker not found"; fi
  say ""
  say "Tools are run from the kit, for example:  python3 $KIT/tools/strata-bench2.py check   (playbook section 5)"
  [ "$INSTALLED" = "$KITVER" ] || say "Next step: bash $KIT/kit.sh update"
}

update_cmd() {
  INSTALLED="$(cat "$AGENT/.kit-version" 2>/dev/null || echo none)"
  [ -d "$AGENT/codex-home" ] || { say "~/agent is not set up yet. Build the image first (playbook Step 9), then run this again."; exit 1; }
  say "Updating from kit version $INSTALLED to $KITVER. What is new:"; sed -n '/^## /{p;q}' "$KIT/CHANGES.md" >/dev/null 2>&1; awk '/^## /{n++} n==1' "$KIT/CHANGES.md" | sed 's/^/   /'
  say ""
  say "This changes only files under $AGENT (and the link ~/.local/bin/codex). Your edited rules or template are replaced only after a .bak copy is saved."
  if [ "${1:-}" != "-y" ]; then read -r -p "Go ahead? [Y/n] " a; case "${a:-y}" in [Nn]*) say "Nothing changed."; exit 0 ;; esac; fi
  bash "$KIT/agent/install-codex-client.sh" --update || { say "the installer stopped; nothing further was done"; exit 1; }
  if command -v docker >/dev/null 2>&1 && docker image inspect codex-sandbox >/dev/null 2>&1 && ! docker run --rm codex-sandbox sh -c 'command -v browser-check' >/dev/null 2>&1; then
    say ""; say "The image has no real browser, so web projects can only be checked by the model with fake-browser tricks."
    if [ "${1:-}" = "-y" ]; then say "(skipping the browser: it is a 300 MB build; run  bash $KIT/agent/add-browser-to-image.sh  when you want it)"
    else read -r -p "Add a headless Chromium to the image now? About 300 MB and a few minutes. [y/N] " b; case "${b:-n}" in [Yy]*) bash "$KIT/agent/add-browser-to-image.sh" ;; *) say "Skipped. Later:  bash $KIT/agent/add-browser-to-image.sh" ;; esac; fi
  fi
  say ""; say "Not touched: the Strata source, its config and the patches (they change Strata itself; playbook Steps 3, 6 and 8)."
  say "Running Codex sessions keep the old rules; the next one you start uses the new ones."
  say ""; status_cmd
}

case "${1:-help}" in
  status)  status_cmd ;;
  update)  update_cmd "${2:-}" ;;
  *)       sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//' ;;
esac
