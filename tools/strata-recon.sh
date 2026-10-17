#!/usr/bin/env bash
# strata-recon.sh v1.0: READ-ONLY recon for the Strata NUC audit (D-A-C, Step 1).
#   bash strata-recon.sh            snapshot -> ~/strata-recon-<stamp>.txt  (paste or attach it in the audit chat)
#   bash strata-recon.sh sample N   once a second for N seconds: GPU clock, throttle reasons, temps, memory -> CSV
#   bash strata-recon.sh load       3 x 300-token requests to Strata, to put decode load on the GPU (run beside `sample`)
# Guarantees: changes nothing, needs no sudo, never prints env VALUES or the contents of secret-looking files.
# The snapshot replaces your username, hostname and home path before it is written. Still read it before sharing.
# Overrides: STRATA_DIR STRATA_CFG STRATA_LOG AGENT_DIR STRATA_URL DEC_TPS (default 8.4)

STRATA_DIR="${STRATA_DIR:-$HOME/github/strata-work/Strata}"
CFG="${STRATA_CFG:-$STRATA_DIR/strata-swift-iq3_xxs.json}"
LOG="${STRATA_LOG:-$STRATA_DIR/strata-swift-iq3_xxs.log}"
AGENT_DIR="${AGENT_DIR:-$HOME/agent}"
URL="${STRATA_URL:-http://127.0.0.1:8085}"
export STRATA_DIR CFG LOG AGENT_DIR URL

have() { command -v "$1" >/dev/null 2>&1; }
hdr()  { printf '\n===== %s =====\n' "$*"; }
rd()   { if [ -r "$1" ]; then tr '\n' ' ' < "$1"; echo; else echo "n/a"; fi; }

# --- xe GPU frequency directories (one per GT) -------------------------------------------------
xe_freq_dirs() {
  local c b drv f
  for c in /sys/class/drm/card*; do
    b=$(basename "$c")
    case "$b" in card[0-9]*-*) continue ;; esac
    [ -e "$c/device/driver" ] || continue
    drv=$(basename "$(readlink -f "$c/device/driver")")
    [ "$drv" = xe ] || continue
    for f in "$c"/device/tile*/gt*/freq0; do [ -d "$f" ] && echo "$f"; done
  done
}

throttle_summary() {   # prints "status=<0|1> reasons=<names>" for one freq0 dir
  local d="$1" st reasons="" f n
  st=$(cat "$d/throttle/status" 2>/dev/null || echo n/a)
  if [ -r "$d/throttle/reasons" ]; then
    reasons=$(tr '\n' ' ' < "$d/throttle/reasons")
  else
    for f in "$d"/throttle/reason_*; do
      [ -r "$f" ] || continue
      n=$(basename "$f"); [ "$(cat "$f" 2>/dev/null)" = "1" ] && reasons="$reasons ${n#reason_}"
    done
  fi
  echo "status=$st reasons=[${reasons:-none}]"
}

pkg_temp_c() {
  local z
  for z in /sys/class/thermal/thermal_zone*; do
    if [ "$(cat "$z/type" 2>/dev/null)" = "x86_pkg_temp" ]; then
      echo $(( $(cat "$z/temp") / 1000 )); return
    fi
  done
  echo n/a
}

# --- sample mode ---------------------------------------------------------------------------------
sample() {
  local secs="${1:-60}" d out t act cur mn mx thr temp ma sw mhz
  d=$(xe_freq_dirs | head -1)
  [ -n "$d" ] || { echo "no xe GPU freq directory found under /sys/class/drm (is the xe driver bound?)"; exit 1; }
  out="$HOME/strata-sample-$(date +%Y%m%d-%H%M).csv"
  echo "time,act_mhz,cur_mhz,min_mhz,max_mhz,throttle,pkg_temp_c,memavail_mib,swap_used_mib,cpu_mhz_avg" > "$out"
  echo "sampling $secs s from $d  (start 'load' or your benchmark in another terminal now)"
  for ((t = 0; t < secs; t++)); do
    act=$(cat "$d/act_freq" 2>/dev/null || echo ""); cur=$(cat "$d/cur_freq" 2>/dev/null || echo "")
    mn=$(cat "$d/min_freq" 2>/dev/null || echo ""); mx=$(cat "$d/max_freq" 2>/dev/null || echo "")
    thr=$(throttle_summary "$d" | tr ' ' '_')
    temp=$(pkg_temp_c)
    ma=$(awk '/MemAvailable/ {print int($2/1024)}' /proc/meminfo)
    sw=$(awk '/SwapTotal/ {t=$2} /SwapFree/ {f=$2} END {print int((t-f)/1024)}' /proc/meminfo)
    mhz=$(awk -F: '/cpu MHz/ {s+=$2; n++} END {if (n) printf "%d", s/n}' /proc/cpuinfo)
    echo "$(date +%H:%M:%S),$act,$cur,$mn,$mx,$thr,$temp,$ma,$sw,$mhz" >> "$out"
    sleep 1
  done
  echo "wrote $out"
  python3 - "$out" <<'PY' 2>/dev/null || true
import csv, sys, collections
rows = list(csv.DictReader(open(sys.argv[1])))
acts = [int(r["act_mhz"]) for r in rows if r["act_mhz"].isdigit()]
if acts:
    acts_sorted = sorted(acts)
    print(f"act_freq MHz: min {acts_sorted[0]}  median {acts_sorted[len(acts)//2]}  max {acts_sorted[-1]}  (n={len(acts)})")
c = collections.Counter(r["throttle"] for r in rows)
for k, v in c.most_common(5): print(f"  throttle {k}: {v}s")
PY
}

# --- load mode -----------------------------------------------------------------------------------
load() {
  have python3 || { echo "python3 needed"; exit 1; }
  python3 - <<'PY'
import json, os, time, urllib.request
url = os.environ["URL"] + "/v1/chat/completions"
for i in range(3):
    body = {"model": "x", "messages": [{"role": "user", "content": "Write a detailed explanation of how a refrigerator works."}],
            "max_tokens": 300, "temperature": 0, "chat_template_kwargs": {"enable_thinking": False}}
    req = urllib.request.Request(url, json.dumps(body).encode(), {"Content-Type": "application/json"})
    t = time.time()
    try:
        r = json.load(urllib.request.urlopen(req, timeout=900))
    except Exception as e:
        print("request failed:", e); break
    dt = time.time() - t; n = r.get("usage", {}).get("completion_tokens", 0)
    print(f"load {i}: {n} tokens in {dt:.1f} s = {n/dt:.2f} tok/s", flush=True)
PY
}

# --- snapshot sections -----------------------------------------------------------------------------
sec_system() {
  hdr "SYSTEM"
  echo "recon_version=1.0 date=$(date -Is)"
  uname -srm; grep -E '^(PRETTY_NAME)=' /etc/os-release 2>/dev/null
  grep -m1 'model name' /proc/cpuinfo; lscpu 2>/dev/null | grep -E '^(CPU\(s\)|Thread|Core|Socket)' | tr -s ' ' | tr '\n' ';'; echo
  free -m | head -3
  echo "MemAvailable_kB=$(awk '/MemAvailable/ {print $2}' /proc/meminfo)  swappiness=$(cat /proc/sys/vm/swappiness 2>/dev/null)"
  echo "ttm_page_pool_size=$(rd /sys/module/ttm/parameters/page_pool_size)"
  echo "modprobe_ttm_conf: $(cat /etc/modprobe.d/ttm-pool.conf 2>/dev/null | tr '\n' ' ')"
  echo "bios_version=$(rd /sys/class/dmi/id/bios_version) board=$(rd /sys/class/dmi/id/board_name) product=$(rd /sys/class/dmi/id/product_name)"
  echo "cpu_governor=$(rd /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor) epp=$(rd /sys/devices/system/cpu/cpu0/cpufreq/energy_performance_preference)"
  echo "intel_pstate=$(rd /sys/devices/system/cpu/intel_pstate/status) platform_profile=$(rd /sys/firmware/acpi/platform_profile)"
  echo "-- RAPL power limits (microwatts; may be unreadable without root)"
  local r
  for r in /sys/class/powercap/intel-rapl:0/constraint_*_name; do
    [ -r "$r" ] || continue
    echo "  $(cat "$r") = $(cat "${r/_name/_power_limit_uw}" 2>/dev/null || echo n/a)"
  done
  echo "-- thermal zones"
  local z; for z in /sys/class/thermal/thermal_zone*; do echo "  $(cat "$z/type" 2>/dev/null) $(( $(cat "$z/temp" 2>/dev/null || echo 0) / 1000 ))C"; done | head -12
  echo "-- tools"
  local t; for t in python3 curl jq git docker node sensors sycl-ls clinfo intel_gpu_top ss; do printf '  %-14s %s\n' "$t" "$(command -v $t || echo MISSING)"; done
  echo "python3: $(python3 --version 2>&1)"
}

sec_gpu() {
  hdr "GPU (xe driver)"
  local d
  d=$(xe_freq_dirs)
  if [ -z "$d" ]; then echo "NO xe freq0 directory found. Listing drm cards:"; ls -l /sys/class/drm 2>/dev/null | head; return; fi
  for d in $d; do
    echo "dir=$d"
    local f; for f in act_freq cur_freq min_freq max_freq rp0_freq rpe_freq rpn_freq; do printf '  %-9s %s\n' "$f" "$(rd "$d/$f")"; done
    echo "  throttle: $(throttle_summary "$d")"
    ls "$d/throttle" 2>/dev/null | tr '\n' ' '; echo
  done
  echo "(idle snapshot: re-run 'sample' while decoding to see the loaded values)"
}

sec_strata() {
  hdr "STRATA"
  echo "dir=$STRATA_DIR"
  if [ -d "$STRATA_DIR/.git" ]; then
    echo "describe=$(git -C "$STRATA_DIR" describe --tags --always 2>/dev/null)  head=$(git -C "$STRATA_DIR" rev-parse --short HEAD 2>/dev/null)"
    echo "remote: $(git -C "$STRATA_DIR" remote -v 2>/dev/null | sed -E 's#//[^/@]*@#//***@#' | head -2 | tr '\n' ' ')"
    echo "modified files:"; git -C "$STRATA_DIR" status --short 2>/dev/null | head -12
  fi
  [ -f "$STRATA_DIR/build-sycl/strata" ] && echo "engine_bin sha256=$(sha256sum "$STRATA_DIR/build-sycl/strata" | cut -c1-16)... mtime=$(date -r "$STRATA_DIR/build-sycl/strata" -Is)"
  echo "-- config (secret-looking keys masked)"
  if [ -f "$CFG" ] && have python3; then
    python3 - <<'PY'
import json, os, re
c = json.load(open(os.environ["CFG"]))
bad = re.compile(r"key|token|secret|pass", re.I)
print("args:", " ".join(c.get("args", [])))
print("env :", ", ".join(f"{k}={'***' if bad.search(k) else v}" for k, v in c.get("env", {}).items()))
print("keys:", {k: ('***' if bad.search(k) else v) for k, v in c.items() if k not in ("args", "env")})
PY
  else echo "config not found or python3 missing: $CFG"; fi
  echo "-- log (last engine start)"
  if [ -f "$LOG" ]; then
    grep "engine started" "$LOG" | tail -1 | grep -o -- "--[a-z-]* [0-9a-z_.]*" | tr '\n' ' '; echo
    grep -E "expert cache auto|session is up|REFUSED|thinking level" "$LOG" | tail -4 | cut -c1-140
    echo "sampling lines: $(grep -ci sampling "$LOG")  no-progress events: $(grep -c 'no progress for' "$LOG")  REFUSED: $(grep -c REFUSED "$LOG")"
  else echo "log not found: $LOG"; fi
  echo "-- API (needs a key? reachable?)"
  if have curl; then
    echo "GET /v1/models -> HTTP $(curl -s -o /dev/null -m 5 -w '%{http_code}' "$URL/v1/models" 2>/dev/null)"
  fi
}

sec_network() {
  hdr "LISTENING SOCKETS (what a host-network container can reach)"
  if have ss; then
    ss -ltnH 2>/dev/null | awk '{print $4}' | sort -u | sed 's/^/  tcp /'
    echo "-- non-loopback listeners (exposed to the LAN):"
    ss -ltnH 2>/dev/null | awk '{print $4}' | grep -vE '^(127\.|\[::1\]|::1)' | sort -u | sed 's/^/  EXPOSED /' || true
    echo "-- process names (own processes only):"
    ss -ltnpH 2>/dev/null | sed -E 's/ +/ /g' | cut -d' ' -f4,6 | sort -u | head -25 | sed 's/^/  /'
  else echo "ss missing"; fi
  echo "ufw: $(have ufw && (ufw status 2>&1 | head -1) || echo 'not installed')"
}

sec_docker() {
  hdr "DOCKER"
  have docker || { echo "docker missing"; return; }
  echo "id: $(id -Gn | tr ' ' ',')"
  ls -l /var/run/docker.sock 2>/dev/null
  docker info --format 'security={{.SecurityOptions}} rootless_hint={{.Name}}' 2>&1 | head -2 | sed -E 's/rootless_hint=.*//'
  echo "-- containers (all)"
  docker ps -a --format '  {{.Names}} | {{.Image}} | {{.Status}} | ports={{.Ports}}' 2>&1 | head -20
  echo "-- container security flags"
  local ids tmpj; ids=$(docker ps -aq 2>/dev/null); tmpj=$(mktemp)
  if [ -n "$ids" ] && have python3; then
    # shellcheck disable=SC2086
    docker inspect $ids > "$tmpj" 2>/dev/null
    python3 - "$tmpj" <<'PY'
import json, re, sys
bad = re.compile(r"key|token|secret|pass", re.I)
for c in json.load(open(sys.argv[1])):
    h, cfg = c.get("HostConfig", {}), c.get("Config", {})
    name = c["Name"].lstrip("/")
    mounts = ["%s->%s:%s" % (m.get("Source"), m.get("Destination"), "rw" if m.get("RW") else "ro") for m in c.get("Mounts", [])]
    envn = [e.split("=", 1)[0] for e in (cfg.get("Env") or [])]
    flags = []
    if h.get("NetworkMode") == "host": flags.append("HOST-NETWORK")
    if h.get("Privileged"): flags.append("PRIVILEGED")
    if h.get("PidMode") == "host": flags.append("HOST-PID")
    if any("docker.sock" in m for m in mounts): flags.append("DOCKER-SOCKET")
    if not cfg.get("User"): flags.append("runs-as-root")
    if not (h.get("CapDrop") or []): flags.append("no-cap-drop")
    if "no-new-privileges" not in " ".join(h.get("SecurityOpt") or []): flags.append("no-nnp")
    if h.get("Devices"): flags.append("devices=" + ",".join(d["PathOnHost"] for d in h["Devices"]))
    if any(bad.search(n) for n in envn): flags.append("SECRET-LIKE-ENV-NAME")
    print("[%s] state=%s net=%s user=%s mem=%s pids=%s capdrop=%s" % (name, c["State"].get("Status"), h.get("NetworkMode"), cfg.get("User") or "(default)", h.get("Memory"), h.get("PidsLimit"), h.get("CapDrop")))
    print("   mounts:", mounts or "none")
    print("   env names:", envn)
    print("   FLAGS:", flags or "none")
PY
  fi
  echo "-- images (provenance)"
  if [ -n "$(docker images -q 2>/dev/null)" ] && have python3; then
    # shellcheck disable=SC2046
    docker image inspect $(docker images -q 2>/dev/null | sort -u) > "$tmpj" 2>/dev/null
    python3 - "$tmpj" <<'PY'
import json, sys
for i in json.load(open(sys.argv[1])):
    tags = i.get("RepoTags") or ["<none>"]
    dig = i.get("RepoDigests") or []
    print("  %s | created %s | %d MB | registry-digest=%s" % (tags[0], i.get("Created", "")[:10], i.get("Size", 0) // 1048576, "yes" if dig else "NO (built or loaded locally)"))
PY
  fi
  rm -f "$tmpj"
}

sec_agent() {
  hdr "AGENT (Codex container setup)"
  [ -d "$AGENT_DIR" ] || { echo "no $AGENT_DIR"; return; }
  ls "$AGENT_DIR" "$AGENT_DIR/bin" 2>/dev/null | tr '\n' ' '; echo
  echo "-- docker run lines in the wrapper"
  grep -hE 'docker (run|create)|--network|--cap|--security-opt|--memory|--cpus|--pids|-v |--mount|danger-full-access|approval_policy' "$AGENT_DIR"/bin/agent-run.sh 2>/dev/null | sed 's/^[ \t]*//' | cut -c1-170 | head -20
  echo "-- codex-home contents (names and sizes only)"
  ls -la "$AGENT_DIR/codex-home" 2>/dev/null | awk 'NR>1 {print "  " $5, $9}' | head -15
  if ls "$AGENT_DIR"/codex-home/auth.json >/dev/null 2>&1; then echo "  WARNING: auth.json exists inside the mounted codex-home (the agent can read it)"; fi
  echo "-- secret-looking files in the work area (names only)"
  find "$AGENT_DIR/work" -maxdepth 5 \( -name '.env*' -o -name '*.pem' -o -name 'id_rsa*' -o -name '*.key' -o -name 'credentials*' -o -name '.netrc' \) 2>/dev/null | head -10
  echo "-- secret-looking strings in the work area (file names only)"
  grep -rIlE 'AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY-----|ghp_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9]{20,}' "$AGENT_DIR/work" 2>/dev/null | head -10
  echo "-- work projects"
  ls "$AGENT_DIR/work" 2>/dev/null | tr '\n' ' '; echo
  echo "-- passwordless sudo? $(sudo -n true 2>/dev/null && echo YES || echo no)"
}

sec_sessions() {
  hdr "AGENT SESSIONS: generation share of wall-clock (tests the 'decode-bound' assumption)"
  have python3 || { echo "python3 missing"; return; }
  python3 - <<'PY'
import datetime, glob, json, os
agent = os.environ["AGENT_DIR"]
dec = float(os.environ.get("DEC_TPS", "8.4"))
files = sorted(glob.glob(f"{agent}/codex-home/sessions/**/rollout-*.jsonl", recursive=True), key=os.path.getmtime)[-6:]
if not files: print("no Codex session files found"); raise SystemExit
def walk(o):
    if isinstance(o, dict):
        yield o
        for v in o.values(): yield from walk(v)
    elif isinstance(o, list):
        for v in o: yield from walk(v)
def ts(s):
    try: return datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception: return None
for f in files:
    tot = last = None; t0 = t1 = None; comp = 0; reqs = 0
    for line in open(f, errors="replace"):
        try: o = json.loads(line)
        except Exception: continue
        t = ts(o.get("timestamp") or "")
        if t: t0 = t0 or t; t1 = t
        if o.get("type") == "compacted": comp += 1
        for d in walk(o):
            if isinstance(d.get("total_token_usage"), dict): tot = d["total_token_usage"]; reqs += 1
            if isinstance(d.get("last_token_usage"), dict): last = d["last_token_usage"]
    name = os.path.basename(f)[:40]
    if not tot or not t0: print(f"{name}: token format not recognised (send me 3 lines of this file with values blanked)"); continue
    wall = (t1 - t0).total_seconds()
    out = tot.get("output_tokens", 0); rsn = tot.get("reasoning_output_tokens", 0)
    gen_s = out / dec
    print(f"{name}: wall {wall/60:.1f} min | token events {reqs} | compactions {comp} | output_tokens {out} (reasoning {rsn}) | last-request context {last.get('input_tokens') if last else 'n/a'}")
    print(f"    [ESTIMATE @ {dec} tok/s] generation {gen_s/60:.1f} min = {100*gen_s/wall:.0f}% of wall-clock (if output_tokens excludes reasoning, add it)")
PY
}

sec_history() {
  hdr "BENCHMARK HISTORY / KIT"
  ls -l "$HOME/strata-tools" 2>/dev/null | awk 'NR>1 {print "  " $9}' | tr '\n' ' '; echo
  if [ -f "$HOME/strata-bench.csv" ]; then echo "last rows of ~/strata-bench.csv:"; tail -6 "$HOME/strata-bench.csv" | cut -c1-230; else echo "no ~/strata-bench.csv yet"; fi
  echo "free disk: $(df -h "$HOME" | awk 'NR==2 {print $4 " free of " $2}')"
}

sec_manual() {
  hdr "MANUAL ITEMS (cannot be read from Linux; note them yourself)"
  cat <<'TXT'
  1. BIOS: Performance / power profile name, PL1 and PL2 values if shown, fan profile.
  2. Is anything else heavy running during tests (browser, desktop effects, n8n, SearXNG)? List it.
  3. Which services should NOT be reachable by the agent container? (compare with the LISTENING SOCKETS section)
  4. Privacy boundary: which kinds of files may be pasted to a cloud planner? (public / personal / work-confidential)
TXT
}

snapshot() {
  local out tmp u h
  out="$HOME/strata-recon-$(date +%Y%m%d-%H%M).txt"
  tmp=$(mktemp)
  { sec_system; sec_gpu; sec_strata; sec_network; sec_docker; sec_agent; sec_sessions; sec_history; sec_manual; } > "$tmp" 2>&1
  u=$(id -un); h=$(hostname)
  sed -e "s#$HOME#~#g" -e "s#\b$u\b#USER#g" -e "s#\b$h\b#HOST#g" "$tmp" > "$out"
  rm -f "$tmp"; chmod 600 "$out"
  echo "wrote $out ($(wc -l < "$out") lines). Read it, then paste it (or attach it) in the audit chat."
  echo "Quick view of anything flagged:"
  grep -nE 'FLAGS:|EXPOSED|WARNING|NO xe|MISSING|HOST-NETWORK' "$out" | cut -c1-160 | head -20
}

case "${1:-snapshot}" in
  snapshot) snapshot ;;
  sample)   sample "${2:-60}" ;;
  load)     load ;;
  *) sed -n '2,7p' "$0" ;;
esac
