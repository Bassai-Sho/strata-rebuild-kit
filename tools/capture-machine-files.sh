#!/usr/bin/env bash
# capture-machine-files.sh: snapshot the files only THIS machine has, so the system can be rebuilt without guessing.
#   bash capture-machine-files.sh [DEST]        default DEST: ~/strata-rebuild-kit/machine-snapshot-<date>
# Read-only toward the system: it copies files OUT and runs read-only commands. It reads no secrets: auth files, environment
# values and chat/session files are not touched. The folder it writes is private to you (mode 700): it holds paths and settings.
S="${STRATA_DIR:-$HOME/github/strata-work/Strata}"
A="${AGENT_DIR:-$HOME/agent}"
D="${1:-$HOME/strata-rebuild-kit/machine-snapshot-$(date +%Y%m%d)}"
mkdir -p "$D"/{strata,home,agent,system} && chmod 700 "$D"
cp_if() { [ -e "$1" ] && cp -p "$1" "$2" 2>/dev/null && echo "  copied $1" || echo "  (missing) $1"; }
echo "Strata folder: $S"
for f in "$S"/run-*.sh "$S"/strata-*.json; do [ -e "$f" ] && cp_if "$f" "$D/strata/"; done
if [ -d "$S/.git" ]; then
  { echo "describe: $(git -C "$S" describe --tags --always 2>/dev/null)"; echo "commit: $(git -C "$S" rev-parse HEAD 2>/dev/null)"
    echo "remote: $(git -C "$S" remote get-url origin 2>/dev/null | sed -E 's#//[^/@]*@#//***@#')"; echo; git -C "$S" status --short 2>/dev/null; } > "$D/strata/git-state.txt"
  git -C "$S" diff -- sycl/include/dpct/device.hpp sycl/setup_intel.py > "$D/strata/mtl-igpu-local-CURRENT.patch" 2>/dev/null
  echo "  local patch: $(grep -c '^diff --git' "$D/strata/mtl-igpu-local-CURRENT.patch") file(s) in mtl-igpu-local-CURRENT.patch"
  git -C "$S" diff -- serve sycl/serve > "$D/strata/serve-changes-CURRENT.patch" 2>/dev/null     # the monitor patches, as applied
fi
ls -l "$S"/../data/models/*/ "$S"/../data/packs/ "$S"/../data/mtp/ 2>/dev/null | head -40 > "$D/strata/model-files.txt"
for f in start-swift.sh chat-save.sh chat-restore.sh; do cp_if "$HOME/$f" "$D/home/"; done
{ echo "== setup.sh invocations in your shell history (how the model, pack and config were made)"; grep -h "setup.sh\|huggingface\|hf download\|wget\|curl .*gguf" "$HOME/.bash_history" 2>/dev/null | tail -40; } > "$D/home/setup-history.txt"
if [ -d "$A" ]; then
  cp_if "$A/Dockerfile" "$D/agent/"; cp_if "$A/codex-home/config.toml" "$D/agent/"; for f in "$A"/bin/*.sh; do [ -e "$f" ] && cp_if "$f" "$D/agent/"; done
fi
cp_if /etc/modprobe.d/ttm-pool.conf "$D/system/"
cp_if /etc/systemd/system/strata-rapl-power.service "$D/system/"
{ echo "kernel: $(uname -r)"; echo "os: $(grep PRETTY_NAME /etc/os-release | cut -d= -f2)"; echo "ttm page_pool_size: $(cat /sys/module/ttm/parameters/page_pool_size 2>/dev/null)"
  echo "bios: $(cat /sys/class/dmi/id/bios_version 2>/dev/null)"; echo "docker: $(docker --version 2>/dev/null)"; echo "python: $(python3 --version 2>&1)"
  echo "gpu: $(ls /sys/class/drm/card0/device/driver -l 2>/dev/null | sed 's/.*-> //')"; echo; echo "== images (digests pin the build environment)"
  docker images --digests --format '{{.Repository}}:{{.Tag}}  {{.Digest}}  {{.CreatedSince}}' 2>/dev/null; } > "$D/system/versions.txt"
{ for i in strata-sycl-dev codex-sandbox; do echo "== $i"; docker image inspect "$i" --format '{{.Id}} created {{.Created}}' 2>/dev/null; docker history --no-trunc --format '{{.CreatedBy}}' "$i" 2>/dev/null | head -8 | cut -c1-160; done; } > "$D/system/image-history.txt"
echo; echo "Snapshot written to $D ($(du -sh "$D" | cut -f1)). Read it once: it is for you, it is not redacted."
