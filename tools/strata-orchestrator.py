#!/usr/bin/env python3
"""strata-orchestrator.py v1.0: unattended tuning run for Strata on the NUC (D-A-C audit, Tier 1).

  python3 strata-orchestrator.py plan                  print the plan and a time estimate; changes nothing
  python3 strata-orchestrator.py run [options]         the full run (about 5-6 hours); run it inside tmux
  python3 strata-orchestrator.py run --resume DIR      continue an interrupted run
  python3 strata-orchestrator.py report DIR            rebuild the matrix from a run folder
  python3 strata-orchestrator.py revert DIR            put the system settings and config back after a crash
  python3 strata-orchestrator.py enginetest            stop Strata, free memory, start it again, report (about 5 min); run this first
  python3 strata-orchestrator.py canarytest            run one canary problem end to end and show what happens (Strata must be up)
  python3 strata-orchestrator.py confirm --combo L1+W1 [--pairs 2]   ABBA confirmation of a combination with a restart before every batch

What it does: measures your CURRENT config as a baseline, then for each experiment changes ONE thing (a config flag, an
environment variable, a request setting, or a CPU/platform setting), restarts Strata where needed, benchmarks it, puts
everything back, and repeats. A fresh baseline run (with a restart) comes every third experiment so drift can be removed.
It then stacks the winners, re-tests them with thinking on, and writes a ranked matrix (best first) with a verdict per row.
It NEVER adopts a winner: your live config is restored to the baseline at the end, and the stacked config is saved beside the
matrix for you to review.

Experiments (Tier 1)   S1 sampling qwen-think | W1 no_turbo (wildcard) | W2 performance profile (wildcard)
                       L1 immediate command lists | P1/P2 pool-workers 4/12 | F1/F2 prefill 4096/8192 | K1 spec 6
                       D1/D2 lookup-chain 3/6 | Q1 mtp-q4 all | R1 STRATA_MIRROR_MIB 1024 (memory)
Privileged commands (printed at start, nothing else uses sudo): drop_caches, and writes to no_turbo / energy_performance_preference /
platform_profile under /sys. Model-written canary code runs only inside `docker run --network none --read-only` on codex-sandbox.
Options: --runs N (decode repeats, 5)  --edit-runs N (4)  --prefill-runs N (2)  --no-canary  --only IDS  --skip IDS
         --seed N  --max-hours H (8)  --force  --out DIR
Environment overrides (mostly for testing): STRATA_URL STRATA_DIR STRATA_CFG STRATA_LOG ORCH_START_CMD ORCH_STOP_CMD ORCH_SUDO
         ORCH_TIME_SCALE ORCH_MIN_AVAIL_GIB ORCH_CANARY_LOCAL ORCH_INHIBITED XE_FREQ_DIR PKG_TEMP_FILE
"""
import argparse, copy, csv, datetime, glob, hashlib, itertools, json, math, os, random, re, shlex, shutil, signal, statistics
import subprocess, sys, tempfile, threading, time, urllib.request, uuid

HOME = os.path.expanduser("~")
URL = os.environ.get("STRATA_URL", "http://127.0.0.1:8085")
STRATA_DIR = os.environ.get("STRATA_DIR", f"{HOME}/github/strata-work/Strata")
CFG = os.environ.get("STRATA_CFG", f"{STRATA_DIR}/strata-swift-iq3_xxs.json")
START_SCRIPT = os.environ.get("ORCH_START_SCRIPT", "./run-swift-iq3_xxs.sh")
TS = float(os.environ.get("ORCH_TIME_SCALE", "1"))
SUDO = shlex.split(os.environ.get("ORCH_SUDO", "sudo -n"))
MIN_AVAIL_GIB = float(os.environ.get("ORCH_MIN_AVAIL_GIB", "50"))
LOCK = f"{HOME}/.strata-orch.lock"
STOP_FLAG = {"stop": False}

SAMPLING = {"greedy": {"temperature": 0},
            "qwen-think": {"temperature": 1.0, "top_p": 0.95, "top_k": 20, "presence_penalty": 0.0}}
FLAG_NAMES = ["--max-context", "--prefill", "--short-read", "--spec", "--spec-min-p", "--pool-workers", "--lookup-chain",
              "--mtp-q4", "--mtp-window", "--suffix-draft", "--kv", "--kv-resident", "--kv-grow", "--vram-reserve-mib"]

# ------------------------------------------------------------------------------------------------------------------
# Experiments. Each changes exactly one thing relative to the baseline config.
# kind: runtime (request setting), system (sysfs), config (needs a Strata restart)
# ------------------------------------------------------------------------------------------------------------------
EXPERIMENTS = [
    dict(id="S1", kind="runtime", name="sampling qwen-think (t1.0 p0.95 k20)", sampling="qwen-think", canary=False, wildcard=False,
         why="prices Qwen's recommended sampling; a workload change, not a tuning change"),
    dict(id="W1", kind="system", name="WILDCARD intel_pstate no_turbo=1", sys={"no_turbo": "1"}, wildcard=True, canary=False,
         why="package-level limit (PL4) plus 100 C: CPU turbo may eat the shared peak budget"),
    dict(id="W2", kind="system", name="WILDCARD EPP + platform_profile = performance", sys={"epp": "performance", "profile": "performance"},
         wildcard=True, canary=False, why="cheapest system-level lever"),
    dict(id="L1", kind="config", name="UR_L0_USE_IMMEDIATE_COMMANDLISTS=1", env={"UR_L0_USE_IMMEDIATE_COMMANDLISTS": "1"}, canary=True,
         why="Intel: Core Ultra 100 iGPUs do not get immediate command lists by default; decode tracks the clock"),
    dict(id="P1", kind="config", name="--pool-workers 4", args={"--pool-workers": "4"}, canary=False,
         why="docs: on hybrid CPUs fewer workers can be faster; all experts are resident"),
    dict(id="P2", kind="config", name="--pool-workers 12", args={"--pool-workers": "12"}, canary=False, why="upper bracket (default is every physical core)"),
    dict(id="F1", kind="config", name="--prefill 4096", args={"--prefill": "4096"}, canary=False,
         why="setup writes 4096 for contexts above 32K; targets the prefill figure"),
    dict(id="F2", kind="config", name="--prefill 8192", args={"--prefill": "8192"}, canary=False, why="upper bracket; watch the memory reserve"),
    dict(id="K1", kind="config", name="--spec 6", args={"--spec": "6"}, canary=True, why="docs report gains on Intel cards for 6 vs 4"),
    dict(id="D1", kind="config", name="--lookup-chain 3", args={"--lookup-chain": "3"}, canary=True,
         why="opt-in prompt-lookup chaining; docs' fast config uses 3; helps copy-heavy output"),
    dict(id="D2", kind="config", name="--lookup-chain 6", args={"--lookup-chain": "6"}, canary=True, why="upper bracket (maximum is 7)"),
    dict(id="Q1", kind="config", name="--mtp-q4 all", args={"--mtp-q4": "all"}, canary=True, why="4-bit draft layer: fewer bytes per draft step; acceptance may drop"),
    dict(id="R1", kind="config", name="STRATA_MIRROR_MIB=1024 (memory test)", env={"STRATA_MIRROR_MIB": "1024"}, memory=True, canary=False,
         why="config reserves 12 GiB for a mirror nothing needs while every expert is resident"),
]
for e in EXPERIMENTS:
    e.setdefault("wildcard", False)
    e.setdefault("memory", False)
    e.setdefault("canary", False)

CANARY = [
    ("is_palindrome", "Write a Python function is_palindrome(s) that returns True if s reads the same forwards and backwards, ignoring case and non-alphanumeric characters. Output only a python code block.",
     "assert is_palindrome('A man, a plan, a canal: Panama') is True\nassert is_palindrome('hello') is False\nassert is_palindrome('') is True\nassert is_palindrome('No lemon, no melon') is True"),
    ("merge_intervals", "Write a Python function merge_intervals(intervals) that takes a list of [start, end] lists and returns the merged overlapping intervals sorted by start. Output only a python code block.",
     "assert merge_intervals([[1,3],[2,6],[8,10],[15,18]])==[[1,6],[8,10],[15,18]]\nassert merge_intervals([[1,4],[4,5]])==[[1,5]]\nassert merge_intervals([])==[]"),
    ("roman_to_int", "Write a Python function roman_to_int(s) that converts a Roman numeral string to an integer. Output only a python code block.",
     "assert roman_to_int('III')==3\nassert roman_to_int('LVIII')==58\nassert roman_to_int('MCMXCIV')==1994"),
    ("flatten", "Write a Python function flatten(x) that flattens arbitrarily nested lists of integers into one flat list, preserving order. Output only a python code block.",
     "assert flatten([1,[2,[3,[4]],5]])==[1,2,3,4,5]\nassert flatten([])==[]\nassert flatten([[[]]])==[]"),
]


def nap(s):
    time.sleep(max(0.0, s * TS))


def say(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def rd(path, default=""):
    try:
        return open(path).read().strip()
    except Exception:
        return default


# --- sensors --------------------------------------------------------------------------------------------------------
def xe_dir():
    if os.environ.get("XE_FREQ_DIR"):
        return os.environ["XE_FREQ_DIR"]
    for c in sorted(glob.glob("/sys/class/drm/card*")):
        if re.search(r"card\d+-", os.path.basename(c)) or not os.path.exists(f"{c}/device/driver"):
            continue
        if os.path.basename(os.path.realpath(f"{c}/device/driver")) != "xe":
            continue
        d = sorted(glob.glob(f"{c}/device/tile*/gt0/freq0"))
        if d:
            return d[0]
    return ""


def pkg_temp():
    if os.environ.get("PKG_TEMP_FILE"):
        try:
            return int(rd(os.environ["PKG_TEMP_FILE"])) // 1000
        except ValueError:
            return None
    for z in glob.glob("/sys/class/thermal/thermal_zone*"):
        if rd(f"{z}/type") == "x86_pkg_temp":
            try:
                return int(rd(f"{z}/temp")) // 1000
            except ValueError:
                return None
    return None


def meminfo():
    kv = {}
    for line in rd("/proc/meminfo").splitlines():
        p = line.split()
        if len(p) >= 2 and p[1].isdigit():
            kv[p[0].rstrip(":")] = int(p[1])
    return kv.get("MemAvailable", 0) // 1024, (kv.get("SwapTotal", 0) - kv.get("SwapFree", 0)) // 1024


class Sampler(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.d = xe_dir()
        self.stop_evt = threading.Event()
        self.samples = []

    def run(self):
        while not self.stop_evt.is_set():
            act, thr, reasons = 0, False, ()
            if self.d:
                try:
                    act = int(rd(f"{self.d}/act_freq", "0") or 0)
                except ValueError:
                    act = 0
                if act > 0 and rd(f"{self.d}/throttle/status") == "1":
                    thr = True
                    reasons = tuple(os.path.basename(f)[len("reason_"):] for f in glob.glob(f"{self.d}/throttle/reason_*") if rd(f) == "1")
            a, sw = meminfo()
            self.samples.append((time.time(), act, thr, pkg_temp(), a, sw, reasons))
            self.stop_evt.wait(0.5)

    def result(self):
        self.stop_evt.set()
        self.join(timeout=2)
        live = [x for x in self.samples if x[1] > 0]
        temps = sorted(x[3] for x in self.samples if x[3] is not None)
        reasons = {}
        for x in live:
            for r in x[6]:
                reasons[r] = reasons.get(r, 0) + 1
        n = len(live)

        def pct(q):
            return temps[int(q * (len(temps) - 1))] if temps else None
        return {"act_mhz_med": int(statistics.median([x[1] for x in live])) if n else None,
                "throttle_share": round(sum(1 for x in live if x[2]) / n, 2) if n else None,
                "throttle_reasons": " ".join(f"{k}:{v}" for k, v in sorted(reasons.items(), key=lambda kv: -kv[1])[:3]),
                "temp_p50": pct(0.5), "temp_p95": pct(0.95), "temp_max": temps[-1] if temps else None,
                "memavail_min_mib": min(x[4] for x in self.samples) if self.samples else None,
                "swap_max_mib": max(x[5] for x in self.samples) if self.samples else None}


# --- server calls and prompts --------------------------------------------------------------------------------------------
def post(content, max_tokens, sampling="greedy", think="off", timeout=900):
    body = {"model": "x", "messages": [{"role": "user", "content": content}], "max_tokens": max_tokens, **SAMPLING[sampling]}
    if think == "off":
        body["chat_template_kwargs"] = {"enable_thinking": False}
    req = urllib.request.Request(URL + "/v1/chat/completions", json.dumps(body).encode(), {"Content-Type": "application/json"})
    t = time.time()
    r = json.load(urllib.request.urlopen(req, timeout=timeout))
    dt = time.time() - t
    u = r.get("usage", {})
    text = ""
    try:
        text = r["choices"][0]["message"].get("content") or ""
    except Exception:
        pass
    return dt, u.get("prompt_tokens", 0), u.get("completion_tokens", 0), text


def code_listing():
    lines = ["class Inventory:", "    def __init__(self):", "        self.items = {}", ""]
    for i in range(1, 9):
        lines += [f"    def add_{i}(self, name, qty):", "        if qty <= 0:", "            raise ValueError('qty must be positive')",
                  f"        self.items[name] = self.items.get(name, 0) + qty * {i}", "        return self.items[name]", ""]
    return "\n".join(lines)


def real_text(chars=16000):
    lib = os.path.dirname(os.__file__)
    out = ""
    for name in ["argparse.py", "textwrap.py", "heapq.py", "bisect.py", "fnmatch.py", "shlex.py", "csv.py", "glob.py"]:
        out += rd(os.path.join(lib, name)) + "\n"
        if len(out) >= chars:
            break
    return out[:chars] if len(out) > 2000 else "The quick brown fox jumps over the lazy dog. " * (chars // 45)


def http_ok(timeout=5):
    try:
        urllib.request.urlopen(URL + "/v1/models", timeout=timeout).read()
        return True
    except Exception:
        return False


# --- statistics ------------------------------------------------------------------------------------------------------------
def med(v):
    return statistics.median(v) if v else None


def spread(v):
    m = med(v)
    return round(100 * (max(v) - min(v)) / m, 1) if v and m else None


def perm_p(a, b, trials=20000):
    obs = abs(statistics.median(a) - statistics.median(b))
    pool, na = list(a) + list(b), len(a)
    idx = range(len(pool))
    if math.comb(len(pool), na) <= 30000:
        combos = itertools.combinations(idx, na)
    else:
        rnd = random.Random(1)
        combos = (rnd.sample(idx, na) for _ in range(trials))
    hit = tot = 0
    for c in combos:
        cs = set(c)
        x = [pool[i] for i in c]
        y = [pool[i] for i in idx if i not in cs]
        tot += 1
        if abs(statistics.median(x) - statistics.median(y)) >= obs - 1e-12:
            hit += 1
    return hit / tot if tot else 1.0


def component(x, base, kind, thr=None):
    """(delta %, sig, p): sig is +1 / -1 when the change is real: at least `thr` % (never below 3, prefill 4) and p <= 0.05
    (prefill: also outside the baseline range). `thr` is the baseline's between-restart noise floor."""
    if not x or not base:
        return None, 0, None
    lo = 4.0 if kind == "prefill" else 3.0
    thr = max(lo, thr or 0)
    d = 100 * (med(x) - med(base)) / med(base)
    if kind == "prefill":
        if d >= thr and min(x) > max(base):
            return d, 1, None
        if d <= -thr and max(x) < min(base):
            return d, -1, None
        return d, 0, None
    if len(x) < 3 or len(base) < 3:
        return d, 0, None
    p = perm_p(x, base)
    return d, (1 if d >= thr and p <= 0.05 else -1 if d <= -thr and p <= 0.05 else 0), p


def noise_floors(recs):
    """Between-restart noise per metric: range of the baseline batch medians (needs 3+ batches)."""
    b = [r for r in recs if r["step_kind"] == "baseline" and r.get("bracket_group") == "greedy" and r.get("raw", {}).get("decode")]
    out = {}
    for m in ("decode", "edit", "prefill"):
        ms = [med(r["raw"][m]) for r in b if r["raw"].get(m)]
        out[m] = round(100 * (max(ms) - min(ms)) / med(ms), 1) if len(ms) >= 3 else 0.0
    return out


def ref_time(dec, edit, pre):
    """Seconds for the reference agent workload: 12,000 plain tokens, 6,000 copy-heavy tokens, 20,000 prompt tokens."""
    return 12000 / dec + 6000 / edit + 20000 / pre


# --- config helpers -----------------------------------------------------------------------------------------------------------
def load_cfg(path=None):
    return json.load(open(path or CFG))


def save_cfg(c):
    tmp = CFG + ".orch.tmp"
    json.dump(c, open(tmp, "w"), indent=2)
    os.replace(tmp, CFG)


def set_arg(args, name, val):
    if name in args:
        i = args.index(name)
        if i + 1 < len(args) and not str(args[i + 1]).startswith("--"):
            args[i + 1] = val
        else:
            args.insert(i + 1, val)
    else:
        args += [name, val]


def with_changes(base, exp):
    c = copy.deepcopy(base)
    args, env = c.setdefault("args", []), c.setdefault("env", {})
    for k, v in exp.get("args", {}).items():
        set_arg(args, k, v)
    for k, v in exp.get("env", {}).items():
        env[k] = v
    return c


def log_path(cfg):
    return os.environ.get("STRATA_LOG") or cfg.get("log") or f"{STRATA_DIR}/strata-swift-iq3_xxs.log"


# --- privileged helpers ---------------------------------------------------------------------------------------------------------
def sudo_write(paths, value):
    r = subprocess.run([*SUDO, "tee", *paths], input=value + "\n", capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise RuntimeError(f"privileged write failed ({r.stderr.strip()[:120]})")


def drop_caches():
    cmd = os.environ.get("ORCH_DROP_CMD")
    if cmd:
        subprocess.run(cmd, shell=True, timeout=60)
        return
    subprocess.run(["sync"], timeout=120)
    r = subprocess.run([*SUDO, "tee", "/proc/sys/vm/drop_caches"], input="2\n", capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise RuntimeError("drop_caches failed (is sudo still authenticated?)")


class SysKnobs:
    NO_TURBO = os.environ.get("ORCH_NO_TURBO", "/sys/devices/system/cpu/intel_pstate/no_turbo")
    EPP = os.environ.get("ORCH_EPP_GLOB", "/sys/devices/system/cpu/cpu*/cpufreq/energy_performance_preference")
    PROFILE = os.environ.get("ORCH_PROFILE", "/sys/firmware/acpi/platform_profile")
    CHOICES = os.environ.get("ORCH_PROFILE_CHOICES", "/sys/firmware/acpi/platform_profile_choices")

    def snapshot(self):
        epp = sorted(glob.glob(self.EPP))
        return {"no_turbo": rd(self.NO_TURBO, None), "epp": rd(epp[0], None) if epp else None, "profile": rd(self.PROFILE, None)}

    def apply(self, want):
        notes = []
        if "no_turbo" in want:
            if not os.path.exists(self.NO_TURBO):
                raise RuntimeError("no_turbo is not available on this system")
            sudo_write([self.NO_TURBO], want["no_turbo"])
        if "epp" in want:
            files = sorted(glob.glob(self.EPP))
            if not files:
                raise RuntimeError("energy_performance_preference not available")
            sudo_write(files, want["epp"])
        if "profile" in want:
            if want["profile"] in rd(self.CHOICES).split():
                sudo_write([self.PROFILE], want["profile"])
            else:
                notes.append(f"platform_profile '{want['profile']}' not offered (choices: {rd(self.CHOICES)}); EPP only")
        return notes

    def restore(self, orig):
        errs = []
        for key, path_list in (("no_turbo", [self.NO_TURBO]), ("epp", sorted(glob.glob(self.EPP))), ("profile", [self.PROFILE])):
            if orig.get(key) is not None and path_list and os.path.exists(path_list[0]):
                try:
                    sudo_write(path_list, orig[key])
                except Exception as e:
                    errs.append(f"{key}: {e}")
        return errs


# --- engine control ---------------------------------------------------------------------------------------------------------------
def container_ids():
    """Engine containers: by image (like start-swift.sh) and by the name the start script gives it."""
    if shutil.which("docker") is None:
        return []
    ids = set()
    for flt in ("ancestor=strata-sycl-dev", "name=strata-sycl-serve"):
        try:
            r = subprocess.run(["docker", "ps", "--filter", flt, "-q"], capture_output=True, text=True, timeout=30)
            ids.update(r.stdout.split())
        except Exception:
            pass
    return sorted(ids)


def engine_up():
    return http_ok(3) or bool(container_ids())


def wait_down(secs):
    for _ in range(max(1, int(secs / 2))):
        if not engine_up():
            return True
        nap(2)
    return not engine_up()


def api_port():
    m = re.search(r":(\d+)", URL.split("//")[-1])
    return int(m.group(1)) if m else 8085


def listener_pids(port):
    """PIDs of your own processes listening on the port, read from /proc (no ss needed)."""
    inodes = set()
    for f in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            lines = open(f).read().splitlines()[1:]
        except OSError:
            continue
        for ln in lines:
            p = ln.split()
            try:
                if len(p) > 9 and p[3] == "0A" and int(p[1].split(":")[1], 16) == port:
                    inodes.add(p[9])
            except ValueError:
                pass
    pids = set()
    if inodes:
        for fd in glob.glob("/proc/[0-9]*/fd/*"):
            try:
                link = os.readlink(fd)
            except OSError:
                continue
            if link.startswith("socket:[") and link[8:-1] in inodes:
                pids.add(int(fd.split("/")[2]))
    return sorted(pids)


def signal_pids(pids, sig):
    for pid in pids:
        try:
            os.kill(pid, sig)
        except (ProcessLookupError, PermissionError) as e:
            say(f"  (could not signal {pid}: {e})")


def stop_engine(proc):
    """Stop Strata. The API on the port is served by a HOST process that starts the engine container as its child, so the
    server is stopped first (SIGINT = Ctrl+C, then SIGTERM), and only then the container: killing the container alone leaves
    the server answering and lets it restart the engine."""
    if not engine_up():
        return "already stopped"
    t0 = time.time()
    port = api_port()
    say(f"stopping Strata ... server process(es) on port {port}: {listener_pids(port) or 'none found'}; engine container(s): {container_ids() or 'none'}")
    tier = None
    cmd = os.environ.get("ORCH_STOP_CMD")
    if cmd:
        subprocess.run(cmd, shell=True, timeout=120)
        tier = "custom stop command" if wait_down(120) else None
    else:
        def sig_server(sg):
            def go():
                signal_pids(listener_pids(port), sg)
                if proc is not None and proc.poll() is None and sg != signal.SIGKILL:
                    try:
                        os.killpg(proc.pid, sg)
                    except Exception:
                        pass
            return go

        def docker_all(args_fn):
            def go():
                for cid in container_ids():
                    try:
                        subprocess.run(args_fn(cid), capture_output=True, timeout=90)
                    except Exception as e:
                        say(f"  (docker error: {e})")
            return go

        def last_resort():
            signal_pids(listener_pids(port), signal.SIGKILL)
            docker_all(lambda c: ["docker", "rm", "-f", c])()
        steps = [("SIGINT to the server (like Ctrl+C)", sig_server(signal.SIGINT), 60),
                 ("SIGTERM to the server", sig_server(signal.SIGTERM), 30),
                 ("docker stop of the engine container", docker_all(lambda c: ["docker", "stop", "-t", "30", c]), 45),
                 ("SIGKILL to the server + docker rm -f", last_resort, 20)]
        for name, act, wait in steps:
            if not listener_pids(port) and not container_ids():
                break
            say(f"  trying {name} ...")
            act()
            if wait_down(wait):
                tier = name
                break
    if engine_up():
        raise RuntimeError(f"Strata would not stop by any method (server pids {listener_pids(port) or 'none found'}, containers {container_ids() or 'none'}). "
                           f"If the API on port {port} is served by something this user cannot signal, stop it by hand and run again.")
    if shutil.which("docker"):
        subprocess.run(["docker", "rm", "-f", "strata-sycl-serve"], capture_output=True, timeout=60)   # a stopped leftover would block the next start
    if proc is not None:
        try:
            proc.wait(timeout=30)
        except Exception:
            pass
    nap(10)
    say(f"  stopped via {tier or 'its own exit'} in {round(time.time() - t0)} s")
    return tier or "its own exit"


def scan_log(path, offset):
    try:
        with open(path, "rb") as f:
            f.seek(offset)
            return f.read().decode(errors="replace")
    except Exception:
        return ""


def parse_start(text):
    slots = None
    for m in re.finditer(r"->\s*(\d+) slots", text):
        slots = int(m.group(1))
    flags = ""
    lines = [l for l in text.splitlines() if "engine started" in l]
    if lines:
        found = []
        for k in FLAG_NAMES:
            m = re.search(rf"(?<!\S){re.escape(k)}(?: (?!--)(\S+))?(?=\s|$)", lines[-1])
            if m:
                found.append(k + (f" {m.group(1)}" if m.group(1) else ""))
        flags = " ".join(found)
    return slots, flags


def start_engine(workdir, tag, cfg):
    lp = log_path(cfg)
    off = os.path.getsize(lp) if os.path.exists(lp) else 0
    startlog = open(os.path.join(workdir, f"start-{tag}.log"), "w")
    env = dict(os.environ, UR_L0_ENABLE_RELAXED_ALLOCATION_LIMITS="1", STRATA_WINDOW_SYNC="1", STRATA_ENGINE_STALL_S="900")
    cmd = os.environ.get("ORCH_START_CMD") or f"{START_SCRIPT}"
    say(f"starting Strata ({tag}) ...")
    t0 = time.time()
    proc = subprocess.Popen(cmd, shell=True, cwd=STRATA_DIR, env=env, stdout=startlog, stderr=subprocess.STDOUT, start_new_session=True)
    deadline = t0 + 420 * min(TS, 1.0)
    ok = False
    while time.time() < deadline:
        if http_ok(3):
            ok = True
            break
        if proc.poll() is not None:
            break
        nap(3)
    nap(5)
    text = scan_log(lp, off)
    slots, flags = parse_start(text)
    info = {"ok": ok, "start_s": round(time.time() - t0), "slots": slots, "flags": flags,
            "log_offset": off, "reason": "" if ok else ("engine exited" if proc.poll() is not None else "timed out waiting for the API")}
    if "REFUSED" in text:
        info["ok"], info["reason"] = False, "engine REFUSED to start (see start log)"
    return proc, info


def restart_engine(state, workdir, tag, cfg):
    stop_engine(state.get("proc"))
    drop_caches()
    nap(3)
    for _ in range(20):
        a, _s = meminfo()
        if a >= MIN_AVAIL_GIB * 1024:
            break
        nap(6)
        drop_caches()
    a, _s = meminfo()
    if a < MIN_AVAIL_GIB * 1024:
        raise RuntimeError(f"only {a // 1024} GiB available after dropping caches (need {MIN_AVAIL_GIB:.0f}); not starting")
    proc, info = start_engine(workdir, tag, cfg)
    state["proc"] = proc
    return info


# --- environment waits -------------------------------------------------------------------------------------------------------------
def wait_quiet():
    """Wait until Sunshine is not streaming and the package has cooled a little."""
    for i in range(40):
        busy = 0.0
        try:
            out = subprocess.run(["ps", "-C", "sunshine", "-o", "%cpu="], capture_output=True, text=True, timeout=10).stdout.split()
            busy = sum(float(x) for x in out) if out else 0.0
        except Exception:
            pass
        if busy < 5.0:
            break
        if i == 0:
            say("Sunshine looks busy (a stream?); waiting up to 10 minutes ...")
        nap(15)
    for _ in range(12):
        t = pkg_temp()
        if t is None or t <= 62:
            break
        nap(15)


# --- canary ----------------------------------------------------------------------------------------------------------------------
def extract_code(text):
    m = re.search(r"```(?:python)?\n(.*?)```", text, re.S)
    return m.group(1) if m else text


def has_loop(text):
    words = text.split()
    if len(words) < 40:
        return False
    sh = {}
    for i in range(len(words) - 8):
        k = " ".join(words[i:i + 8])
        sh[k] = sh.get(k, 0) + 1
    return max(sh.values()) >= 8


CANARY_NOTE = []


def run_canary(sampling, think):
    CANARY_NOTE.clear()
    passed, loops = 0, 0
    for name, prompt, asserts in CANARY:
        try:
            _dt, _pt, _out, text = post(prompt, 300, sampling, think, timeout=600)
        except Exception:
            continue
        loops += has_loop(text)
        d = tempfile.mkdtemp(prefix="canary-")
        os.chmod(d, 0o755)
        open(os.path.join(d, "check.py"), "w").write(extract_code(text) + "\n\n" + asserts + "\nprint('PASS')\n")
        os.chmod(os.path.join(d, "check.py"), 0o644)
        try:
            if os.environ.get("ORCH_CANARY_LOCAL"):
                r = subprocess.run(["python3", "-I", "-B", os.path.join(d, "check.py")], capture_output=True, text=True, timeout=20)
            else:
                r = subprocess.run(["docker", "run", "--rm", "--network", "none", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                                    "--user", f"{os.getuid()}:{os.getgid()}", "--memory", "256m", "--pids-limit", "64", "--read-only",
                                    "-v", f"{d}:/t:ro", "codex-sandbox", "python3", "-I", "-B", "/t/check.py"], capture_output=True, text=True, timeout=60)
            if "PASS" in r.stdout:
                passed += 1
            elif not CANARY_NOTE:
                CANARY_NOTE.append(f"{name}: {(r.stderr or r.stdout or '(no output)').strip()[-160:]} | response starts: {text.strip()[:80]!r}")
        except Exception as e:
            if not CANARY_NOTE:
                CANARY_NOTE.append(f"{name}: runner error {type(e).__name__}: {str(e)[:100]}")
        shutil.rmtree(d, ignore_errors=True)
    return passed, len(CANARY), loops


# --- one batch of measurements ---------------------------------------------------------------------------------------------------------
def measure(a, label, step_kind, exp, info, cfg, sampling="greedy", think="off", canary=True, bracket_group="greedy"):
    wait_quiet()
    lp = log_path(cfg)
    off = os.path.getsize(lp) if os.path.exists(lp) else 0
    smp = Sampler()
    smp.start()
    rec = {"label": label, "step_kind": step_kind, "id": exp["id"] if exp else "BASE", "name": exp["name"] if exp else "baseline",
           "wildcard": bool(exp and exp["wildcard"]), "memory": bool(exp and exp["memory"]), "sampling": sampling, "think": think,
           "bracket_group": bracket_group, "time": time.strftime("%Y-%m-%d %H:%M:%S"), "restart": info or {}, "errors": []}
    t_start = time.time()
    try:
        post("Name one animal that starts with the letter A.", 24, sampling, think)
        dec_prompt = "Write a detailed explanation of how a refrigerator works."
        post(dec_prompt, 300, sampling, think)
        dec = []
        n_dec, n_edit = a.runs + (3 if think == "on" else 0), a.edit_runs + (2 if think == "on" else 0)
        for _ in range(n_dec):
            dt, _pt, out, _t = post(dec_prompt, 300, sampling, think)
            dec.append(out / dt)
        edit_prompt = ("Here is some code:\n\n" + code_listing() + "\n\nRewrite all of this code exactly as it is, but rename the attribute "
                       "'items' to 'entries'. Output only the code.")
        post(edit_prompt, 1, sampling, think)
        edt = []
        for _ in range(n_edit):
            dt, _pt, out, _t = post(edit_prompt, 450, sampling, think)
            edt.append(out / dt)
        pre = []
        text = real_text()
        for _ in range(a.prefill_runs):
            dt, pt, _o, _t = post(f"run-id: {uuid.uuid4().hex[:12]}\n\n{text}\n\nIn one word: what language is this code written in?", 1, sampling, think)
            pre.append(pt / dt)
        rec["raw"] = {"decode": dec, "edit": edt, "prefill": pre}
        if canary and exp is not None and exp["canary"] or (canary and exp is None and not a.no_canary):
            if not a.no_canary:
                p, n, loops = run_canary(sampling, think)
                rec["canary"] = {"passed": p, "total": n, "loops": loops, "note": CANARY_NOTE[0] if CANARY_NOTE else ""}
    except Exception as e:
        rec["errors"].append(f"{type(e).__name__}: {str(e)[:160]}")
        rec["raw"] = rec.get("raw") or {}
    rec["sensors"] = smp.result()
    rec["wall_s"] = round(time.time() - t_start)
    text = scan_log(lp, off)
    rec["log_flags"] = {"refused": text.count("REFUSED"), "no_progress": text.count("no progress for"), "traceback": text.count("Traceback")}
    return rec


# --- analysis ----------------------------------------------------------------------------------------------------------------------------
def load_results(d):
    out = []
    p = os.path.join(d, "results.jsonl")
    if os.path.exists(p):
        for line in open(p):
            try:
                out.append(json.loads(line))
            except Exception:
                pass
    return out


def pool(recs, metric):
    return [x for r in recs for x in r.get("raw", {}).get(metric, [])]


def baselines_for(recs, i, group):
    base = [(j, r) for j, r in enumerate(recs) if r["step_kind"] == "baseline" and r.get("bracket_group") == group and r.get("raw")]
    before = [r for j, r in base if j < i]
    after = [r for j, r in base if j > i]
    pick = ([before[-1]] if before else []) + ([after[0]] if after else [])
    if len(pick) < 2 and group == "greedy":   # only one side available: borrow the next-nearest baseline for statistical power
        extra = before[-2:-1][::-1] if before and len(before) > 1 else after[1:2]
        pick += extra
    return pick


def analyse(recs):
    rows = []
    floors = noise_floors(recs)
    for i, r in enumerate(recs):
        if r["step_kind"] in ("baseline", "burnin"):
            continue
        row = {"id": r["id"], "label": r["label"], "name": r["name"], "wildcard": r.get("wildcard"), "memory": r.get("memory"), "rec": r}
        raw = r.get("raw", {})
        bl = baselines_for(recs, i, r.get("bracket_group", "greedy"))
        reasons = []
        if r["restart"] and r["restart"].get("ok") is False:
            reasons.append(r["restart"].get("reason") or "engine did not start")
        if r["restart"] and r["restart"].get("applied") is False:
            reasons.append("flag not echoed in the engine start line (unsupported?)")
        if r["errors"]:
            reasons.append("; ".join(r["errors"]))
        if not raw.get("decode") or not bl:
            row.update(verdict="FAILED", index=None, reason="; ".join(reasons) or "no data", comp={})
            rows.append(row)
            continue
        comp, bmeds = {}, {}
        for m in ("decode", "edit", "prefill"):
            bs = pool(bl, m)
            comp[m] = component(raw.get(m, []), bs, m, floors.get(m))
            bmeds[m] = med(bs)
        try:
            idx = 100 * ref_time(bmeds["decode"], bmeds["edit"], bmeds["prefill"]) / ref_time(med(raw["decode"]), med(raw["edit"]), med(raw["prefill"]))
        except Exception:
            idx = None
        exp_slots = next((b["restart"].get("slots") for b in bl if b["restart"].get("slots")), None)
        sl = r["restart"].get("slots") if r["restart"] else None
        if r["restart"] and exp_slots and sl and sl != exp_slots:
            reasons.append(f"expert cache {sl} slots (baseline {exp_slots}): not all-resident")
        if sum(r["log_flags"].values()):
            reasons.append(f"log flags {r['log_flags']}")
        sens, bsens = r["sensors"], [b["sensors"] for b in bl]
        bmem = med([s["memavail_min_mib"] for s in bsens if s.get("memavail_min_mib") is not None])
        bswap = med([s["swap_max_mib"] for s in bsens if s.get("swap_max_mib") is not None])
        if (sens.get("memavail_min_mib") is not None and sens["memavail_min_mib"] < 4096
                and (bmem is None or sens["memavail_min_mib"] < bmem - 512)):
            reasons.append(f"MemAvailable fell to {sens['memavail_min_mib']} MiB (baseline {int(bmem) if bmem else '?'})")
        if bswap is not None and sens.get("swap_max_mib") is not None and sens["swap_max_mib"] - bswap > 2048:
            reasons.append(f"swap +{sens['swap_max_mib'] - bswap} MiB over baseline")
        canary_flag = ""
        if r.get("canary") and r["canary"]["passed"] == 0 and r["canary"].get("note"):
            reasons.append("CANARY RUNNER FAILED (not a quality finding): " + r["canary"]["note"][:200])
        bcan = [b["canary"]["passed"] for b in bl if b.get("canary")]
        if r.get("canary") and bcan and r["canary"]["passed"] < max(bcan):
            canary_flag = f"canary {r['canary']['passed']}/{r['canary']['total']} vs baseline {max(bcan)}"
        if r.get("canary") and r["canary"].get("loops"):
            canary_flag += " repetition loop"
        conf = []
        bclk = med([s["act_mhz_med"] for s in bsens if s.get("act_mhz_med")])
        if bclk and sens.get("act_mhz_med") and abs(100 * (sens["act_mhz_med"] - bclk) / bclk) > 2:
            conf.append(f"clock {sens['act_mhz_med']} vs {int(bclk)} MHz")
        btmp = med([s["temp_p95"] for s in bsens if s.get("temp_p95") is not None])
        if btmp is not None and sens.get("temp_p95") is not None and abs(sens["temp_p95"] - btmp) > 3:
            conf.append(f"temp p95 {sens['temp_p95']} vs {btmp:.0f} C")
        sigs = [c[1] for c in comp.values()]
        worse = any(s < 0 for s in sigs)
        better = any(s > 0 for s in sigs)
        if reasons and any(k in " ".join(reasons) for k in ("did not start", "REFUSED", "not echoed", "not all-resident", "timed out", "exited", "Error", "refused", "no data")) or r["errors"]:
            verdict = "FAILED"
        elif canary_flag:
            verdict = "QUALITY?"
        elif r.get("memory"):
            gain = (sens.get("memavail_min_mib") or 0) - (bmem or 0)
            verdict = "MEMORY WIN" if (gain >= 3072 and (idx or 0) >= 97) else ("LOSS" if (idx or 0) <= 97 else "NEUTRAL")
            reasons.append(f"MemAvailable min {sens.get('memavail_min_mib')} vs {int(bmem or 0)} MiB")
        elif idx is not None and idx >= 101 and better and not worse and not reasons:
            verdict = "WIN"
        elif idx is not None and idx <= 97:
            verdict = "LOSS"
        else:
            verdict = "NEUTRAL"
        row.update(verdict=verdict, index=None if idx is None else round(idx, 1), comp=comp, reason="; ".join(reasons + ([canary_flag] if canary_flag else [])),
                   confound="; ".join(conf), meds={m: (round(med(raw[m]), 2) if raw.get(m) else None) for m in ("decode", "edit", "prefill")},
                   sens=sens, canary=r.get("canary"))
        rows.append(row)
    return rows


def noise_floor(recs):
    b = [r for r in recs if r["step_kind"] == "baseline" and r.get("raw", {}).get("decode") and r.get("bracket_group") == "greedy"]
    ms = [med(r["raw"]["decode"]) for r in b]
    if len(ms) < 2:
        return None
    return {"n": len(ms), "decode_medians": [round(x, 2) for x in ms], "range_pct": round(100 * (max(ms) - min(ms)) / med(ms), 1),
            "within_batch_spread_pct": round(statistics.mean(spread(r["raw"]["decode"]) or 0 for r in b), 1)}


def fmt_c(c):
    return "-" if not c or c[0] is None else f"{c[0]:+.1f}%{'*' if c[1] > 0 else '!' if c[1] < 0 else ''}"


def redact(text):
    u = os.environ.get("USER") or ""
    t = text.replace(HOME, "~")
    return re.sub(rf"\b{re.escape(u)}\b", "USER", t) if u else t


ORDER = {"WIN": 0, "MEMORY WIN": 1, "NEUTRAL": 2, "QUALITY?": 3, "LOSS": 4, "FAILED": 5}


def write_report(d, base_cfg=None, stack_info=None):
    recs = load_results(d)
    rows = analyse(recs)
    nf = noise_floor(recs)
    scored = sorted([r for r in rows if r["verdict"] != "FAILED" and r["index"] is not None], key=lambda r: -r["index"])
    failed = [r for r in rows if r["verdict"] == "FAILED" or r["index"] is None]
    bl = [r for r in recs if r["step_kind"] == "baseline" and r.get("raw", {}).get("decode") and r.get("bracket_group") == "greedy"]
    b_dec, b_edit, b_pre = [med(pool(bl, m)) for m in ("decode", "edit", "prefill")] if bl else (None, None, None)
    L = ["# Strata tuning matrix", "", f"Run folder: {d}", f"Generated: {time.strftime('%Y-%m-%d %H:%M')}", ""]
    if b_dec:
        L += [f"Baseline (pooled over {len(bl)} batches, greedy, thinking off): decode {b_dec:.2f} tok/s, copy-heavy {b_edit:.2f} tok/s, prefill {b_pre:.1f} tok/s."]
    if nf:
        L += [f"Noise floor: baseline decode medians {nf['decode_medians']} (range {nf['range_pct']}%, mean within-batch spread {nf['within_batch_spread_pct']}%). "
              f"Treat any change below the range as noise."]
    fl = noise_floors(recs)
    L += ["", f"Significance threshold per metric = the baseline's between-restart noise floor (never below 3%): decode {max(3, fl['decode'])}%, "
              f"copy-heavy {max(3, fl['edit'])}%, prefill {max(4, fl['prefill'])}%. Within-batch spread is far smaller than restart-to-restart noise, so only changes beyond these thresholds are marked."]
    L += ["", "Index = speed on a reference agent workload vs the nearest baselines (100 = baseline; 12,000 plain + 6,000 copy-heavy output tokens, 20,000 prompt tokens). "
          "`*` = real improvement, `!` = real loss (beyond the threshold above and p <= 0.05; prefill: also outside the baseline range).", "",
          "| Rank | ID | Change | Index | Verdict | Decode | Copy-heavy | Prefill | MHz | Temp p95 | MemAvail min | Swap max | Canary | Notes |",
          "|---:|---|---|---:|---|---:|---:|---:|---:|---:|---:|---:|---|---|"]
    ref_row = {"index": 100.0, "ref": True}
    ordered = sorted(scored + ([ref_row] if b_dec else []), key=lambda r: -r["index"])
    k = 0
    for r in ordered:
        if r.get("ref"):
            L.append(f"| ref | BASE | **baseline (pooled)** | 100 | reference | {b_dec:.2f} | {b_edit:.2f} | {b_pre:.1f} | - | - | - | - | - | everything above beat it, everything below lost to it |")
            continue
        k += 1
        s = r["sens"]
        can = f"{r['canary']['passed']}/{r['canary']['total']}" if r.get("canary") else "-"
        notes = "; ".join(x for x in (r.get("reason"), r.get("confound")) if x)
        L.append(f"| {k} | {r['id']}{' (W)' if r['wildcard'] else ''} | {r['name']} | {r['index']} | {r['verdict']} | {r['meds']['decode']} {fmt_c(r['comp']['decode'])} | "
                 f"{r['meds']['edit']} {fmt_c(r['comp']['edit'])} | {r['meds']['prefill']} {fmt_c(r['comp']['prefill'])} | {s.get('act_mhz_med')} | {s.get('temp_p95')} | "
                 f"{s.get('memavail_min_mib')} | {s.get('swap_max_mib')} | {can} | {notes} |")
    for r in failed:
        L.append(f"| - | {r['id']}{' (W)' if r['wildcard'] else ''} | {r['name']} | - | FAILED | - | - | - | - | - | - | - | - | {r.get('reason', '')} |")
    wins = [r for r in scored if r["verdict"] in ("WIN", "MEMORY WIN")]
    L += ["", "## Verdict counts", ""]
    cnt = {}
    for r in rows:
        cnt[r["verdict"]] = cnt.get(r["verdict"], 0) + 1
    L.append(", ".join(f"{k}: {v}" for k, v in sorted(cnt.items(), key=lambda kv: ORDER.get(kv[0], 9))) or "none")
    if stack_info:
        L += ["", "## Stack", "", stack_info]
    L += ["", "## Apply a result yourself (nothing was adopted)", "",
          "The run restored your baseline config. To try a winner: `python3 ~/strata-tools/strata-tune.py set-arg --FLAG VALUE` (or `set-env`), restart, and run `strata-bench2.py`. "
          "A stacked config, if one was built, is saved next to this file as `stack-config.json`."]
    text = redact("\n".join(L))
    open(os.path.join(d, "matrix.md"), "w").write(text + "\n")
    with open(os.path.join(d, "matrix.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["rank", "id", "change", "index", "verdict", "decode", "edit", "prefill", "mhz", "temp_p95", "memavail_min", "swap_max", "notes"])
        for k, r in enumerate(scored, 1):
            s = r["sens"]
            w.writerow([k, r["id"], r["name"], r["index"], r["verdict"], r["meds"]["decode"], r["meds"]["edit"], r["meds"]["prefill"], s.get("act_mhz_med"),
                        s.get("temp_p95"), s.get("memavail_min_mib"), s.get("swap_max_mib"), redact(r.get("reason", ""))])
    return text, wins


# --- the run --------------------------------------------------------------------------------------------------------------------------------
def build_plan(a, seed):
    ids = set(a.only.split(",")) if a.only else None
    skip = set(a.skip.split(",")) if a.skip else set()
    exps = [e for e in EXPERIMENTS if (not ids or e["id"] in ids) and e["id"] not in skip]
    runtime = [e for e in exps if e["kind"] in ("runtime", "system")]
    cfgs = [e for e in exps if e["kind"] == "config"]
    random.Random(seed).shuffle(cfgs)
    steps = [("restart", "BASE-0", None), ("batch", "BURN-IN", None), ("batch", "BASE-0", None)]
    for e in runtime:
        steps.append(("exp", e["id"], e))
    if runtime:
        steps.append(("batch", "BASE-0b", None))
    n = 1
    for i in range(0, len(cfgs), 3):
        for e in cfgs[i:i + 3]:
            steps.append(("exp", e["id"], e))
        if i + 3 < len(cfgs):   # the final fresh baseline (BASE-end) brackets the last group
            steps += [("restart", f"BASE-{n}", None), ("batch", f"BASE-{n}", None)]
            n += 1
    return steps


def estimate(a, steps):
    batch = 0.7 + a.runs * 0.63 + a.edit_runs * 0.72 + a.prefill_runs * 1.6 + (0 if a.no_canary else 2.5)
    restart = 3.5
    mins = 0
    for kind, _l, e in steps:
        if kind == "restart":
            mins += restart
        elif kind == "batch":
            mins += batch
        elif e["kind"] == "config":
            mins += restart + batch
        else:
            mins += batch
    mins += 2 * restart + 4 * batch + 0.6 * batch   # final stage (plus the short burn-in batch): fresh baseline, baseline think-on, stack, stack think-on
    return mins / 60, batch


def cmd_plan(a):
    steps = build_plan(a, a.seed)
    h, batch = estimate(a, steps)
    print(f"{len(steps)} screening steps, seed {a.seed}, about {batch:.0f} min per batch; estimated total {h:.1f} h (plus the final stack stages)\n")
    for k, (kind, label, e) in enumerate(steps, 1):
        if kind == "exp":
            print(f"{k:2d}. {e['id']:3s} {e['name']:46s} [{e['kind']}{', wildcard' if e['wildcard'] else ''}]  {e['why']}")
        else:
            print(f"{k:2d}. {kind:7s} {label}")
    print("\nFinal stages: fresh baseline, baseline with thinking on, stack of winners, stack with thinking on, then the matrix.")


class Aborted(Exception):
    pass


def check_stop():
    if STOP_FLAG["stop"] or os.path.exists(os.path.join(STOP_FLAG.get("dir", "."), "STOP")):
        raise Aborted()


def append_result(d, rec):
    with open(os.path.join(d, "results.jsonl"), "a") as f:
        f.write(json.dumps(rec) + "\n")


def preflight(a, cfg):
    ok = True

    def chk(name, good, detail=""):
        nonlocal ok
        print(f"  [{'ok' if good else 'FAIL'}] {name} {detail}")
        ok = ok and good
    print("Preflight:")
    chk("Strata folder and config", os.path.isdir(STRATA_DIR) and os.path.exists(CFG), CFG)
    free = shutil.disk_usage(HOME).free / 2 ** 30
    chk("disk space", free >= 10, f"{free:.0f} GiB free (need 10)")
    chk("no other orchestrator running", not (os.path.exists(LOCK) and os.path.exists(f"/proc/{rd(LOCK, '0')}")))
    if os.environ.get("ORCH_START_CMD") is None:
        chk("docker available", shutil.which("docker") is not None)
        chk("codex-sandbox image (canary)", a.no_canary or subprocess.run(["docker", "image", "inspect", "codex-sandbox"], capture_output=True).returncode == 0,
            "(use --no-canary to skip)")
    if SUDO:
        chk("sudo authenticated", subprocess.run([*SUDO, "true"], capture_output=True).returncode == 0)
    t = pkg_temp()
    chk("temperature sensor", t is not None, f"{t} C" if t is not None else "(not found; temperature will not be recorded)")
    chk("GPU clock sensor", bool(xe_dir()), xe_dir() or "(not found)")
    return ok or a.force


def sudo_keepalive():
    def loop():
        while not STOP_FLAG.get("done"):
            time.sleep(60)
            subprocess.run([*SUDO, "-v"], capture_output=True)
    if SUDO:
        threading.Thread(target=loop, daemon=True).start()


def cmd_run(a):
    if not os.environ.get("ORCH_INHIBITED") and shutil.which("systemd-inhibit"):
        env = dict(os.environ, ORCH_INHIBITED="1")
        os.execvpe("systemd-inhibit", ["systemd-inhibit", "--what=sleep:idle", "--who=strata-orchestrator", "--why=benchmark run",
                                       sys.executable, *sys.argv], env)
    base_cfg = load_cfg()
    d = a.resume or a.out or os.path.join(HOME, "strata-orch-" + time.strftime("%Y%m%d-%H%M"))
    os.makedirs(d, exist_ok=True)
    STOP_FLAG["dir"] = d
    if a.resume and os.path.exists(os.path.join(d, "STOP")):
        os.remove(os.path.join(d, "STOP"))
    steps = build_plan(a, a.seed)
    h, _b = estimate(a, steps)
    print(f"Run folder {d}\nEstimated {h:.1f} h. Create the file {os.path.join(d, 'STOP')} to end early (everything is restored).")
    print("Privileged commands used: drop_caches; writes to no_turbo, energy_performance_preference, platform_profile. Canary code runs only in docker --network none --read-only.\n")
    if SUDO and subprocess.run(["sudo", "-v"]).returncode != 0:
        sys.exit("sudo authentication failed")
    if not preflight(a, base_cfg):
        sys.exit("preflight failed (use --force to override)")
    open(LOCK, "w").write(str(os.getpid()))
    sudo_keepalive()
    knobs = SysKnobs()
    state = {"proc": None}
    if a.resume and os.path.exists(os.path.join(d, "state.json")):
        st = json.load(open(os.path.join(d, "state.json")))
        orig_sys = st["sys_original"]
        shutil.copy(os.path.join(d, "baseline-config.json"), CFG) if os.path.exists(os.path.join(d, "baseline-config.json")) else None
        base_cfg = load_cfg(os.path.join(d, "baseline-config.json")) if os.path.exists(os.path.join(d, "baseline-config.json")) else base_cfg
    else:
        orig_sys = knobs.snapshot()
        shutil.copy(CFG, os.path.join(d, "baseline-config.json"))
        shutil.copy(CFG, CFG + ".orch-backup-" + time.strftime("%Y%m%d-%H%M"))
        json.dump({"sys_original": orig_sys, "cfg": CFG, "started": time.time(), "seed": a.seed}, open(os.path.join(d, "state.json"), "w"))
    done = {r["label"] for r in load_results(d)}
    was_running = engine_up()
    reported = {"done": False}
    signal.signal(signal.SIGINT, lambda *_: STOP_FLAG.update(stop=True))
    signal.signal(signal.SIGTERM, lambda *_: STOP_FLAG.update(stop=True))
    t_begin = time.time()
    info_cur = {}

    def restore_all():
        errs = knobs.restore(orig_sys)
        save_cfg(base_cfg)
        return errs

    def run_batch(label, kind, exp, sampling="greedy", think="off", group="greedy", cfg_now=None):
        check_stop()
        if label in done:
            say(f"{label}: already done, skipping")
            return
        say(f"measuring {label} ...")
        rec = measure(a, label, kind, exp, dict(info_cur) if (exp and exp["kind"] == "config") or kind == "baseline" else {}, cfg_now or base_cfg, sampling, think,
                      canary=True, bracket_group=group)
        if exp and exp.get("args"):
            fl = info_cur.get("flags", "")
            rec["restart"]["applied"] = all(f"{k} {v}" in fl for k, v in exp["args"].items()) if fl else False
        append_result(d, rec)
        done.add(label)
        d0 = med(rec.get("raw", {}).get("decode", []))
        say(f"{label}: decode {d0 and round(d0, 2)} tok/s, errors {rec['errors'] or 'none'}")

    try:
        if a.resume and not engine_up():
            info = restart_engine(state, d, "resume", base_cfg)
            info_cur.clear()
            info_cur.update(info)
        for kind, label, exp in steps:
            check_stop()
            if time.time() - t_begin > a.max_hours * 3600:
                say("time limit reached; stopping the screening phase")
                break
            if kind == "restart":
                if label in done:
                    continue
                save_cfg(base_cfg)
                info = restart_engine(state, d, label, base_cfg)
                info_cur.clear()
                info_cur.update(info)
                if not info["ok"]:
                    say(f"baseline start failed: {info['reason']}; retrying once")
                    info = restart_engine(state, d, label + "-retry", base_cfg)
                    info_cur.clear()
                    info_cur.update(info)
                    if not info["ok"]:
                        raise RuntimeError("baseline would not start twice; aborting the run")
            elif kind == "batch" and label == "BURN-IN":
                if label not in done:
                    say("burn-in batch (warms the machine; not counted) ...")
                    sa = argparse.Namespace(**{**vars(a), "runs": 3, "edit_runs": 2, "prefill_runs": 1, "no_canary": True})
                    rec = measure(sa, label, "burnin", None, dict(info_cur), base_cfg, canary=False)
                    append_result(d, rec)
                    done.add(label)
            elif kind == "batch":
                run_batch(label, "baseline", None)
            else:
                if exp["id"] in done:
                    continue
                say(f"=== {exp['id']}: {exp['name']}")
                try:
                    if exp["kind"] == "config":
                        cfg_x = with_changes(base_cfg, exp)
                        save_cfg(cfg_x)
                        info = restart_engine(state, d, exp["id"], cfg_x)
                        info_cur.clear()
                        info_cur.update(info)
                        if not info["ok"]:
                            rec = {"label": exp["id"], "step_kind": "exp", "id": exp["id"], "name": exp["name"], "wildcard": exp["wildcard"], "memory": exp["memory"],
                                   "restart": info, "errors": [], "sensors": {}, "raw": {}, "log_flags": {}, "bracket_group": "greedy"}
                            append_result(d, rec)
                            done.add(exp["id"])
                        else:
                            run_batch(exp["id"], "exp", exp, cfg_now=cfg_x)
                        save_cfg(base_cfg)
                    elif exp["kind"] == "system":
                        notes = knobs.apply(exp["sys"])
                        try:
                            run_batch(exp["id"], "exp", exp)
                        finally:
                            knobs.restore(orig_sys)
                        if notes:
                            say("; ".join(notes))
                    else:
                        run_batch(exp["id"], "exp", exp, sampling=exp["sampling"])
                except Aborted:
                    raise
                except Exception as ex:
                    say(f"{exp['id']} failed: {ex}")
                    append_result(d, {"label": exp["id"], "step_kind": "exp", "id": exp["id"], "name": exp["name"], "wildcard": exp["wildcard"], "memory": exp["memory"],
                                      "restart": {"ok": False, "reason": str(ex)[:160]}, "errors": [str(ex)[:160]], "sensors": {}, "raw": {}, "log_flags": {}, "bracket_group": "greedy"})
                    done.add(exp["id"])
                    save_cfg(base_cfg)
                    knobs.restore(orig_sys)
                    try:
                        info = restart_engine(state, d, "recover", base_cfg)
                        info_cur.clear()
                        info_cur.update(info)
                    except Exception:
                        pass
        # ---- final stages --------------------------------------------------------------------------------------------------
        check_stop()
        stack_text = "No stack was built."
        rows = analyse(load_results(d))
        win_cfg = [r for r in rows if r["verdict"] in ("WIN", "MEMORY WIN") and not r["wildcard"] and r["id"] in {e["id"] for e in EXPERIMENTS if e["kind"] == "config"}]
        win_cfg.sort(key=lambda r: -(r["index"] or 0))
        stack, used_keys, members = copy.deepcopy(base_cfg), set(), []
        for r in win_cfg:
            e = next(x for x in EXPERIMENTS if x["id"] == r["id"])
            keys = set(e.get("args", {})) | set(e.get("env", {}))
            if keys & used_keys:
                continue
            used_keys |= keys
            members.append(e["id"])
            stack = with_changes(stack, e)
        save_cfg(base_cfg)
        save_cfg(base_cfg)
        info = restart_engine(state, d, "BASE-end", base_cfg)
        info_cur.clear()
        info_cur.update(info)
        run_batch("BASE-end", "baseline", None)
        run_batch("BASE-T", "baseline", None, think="on", group="think")
        if members:
            json.dump(stack, open(os.path.join(d, "stack-config.json"), "w"), indent=2)
            save_cfg(stack)
            info = restart_engine(state, d, "STACK", stack)
            info_cur.clear()
            info_cur.update(info)
            sx = {"id": "STACK", "name": "stack of " + "+".join(members), "kind": "config", "wildcard": False, "memory": False, "canary": True,
                  "args": {}, "env": {}}
            run_batch("STACK", "exp", sx, cfg_now=stack)
            tx = dict(sx, id="STACK-T", name="stack with thinking on")
            run_batch("STACK-T", "exp", tx, think="on", group="think", cfg_now=stack)
            save_cfg(base_cfg)
            stack_text = f"Stack members (winners, one per setting): {', '.join(members)}. Config saved as stack-config.json. Compare the STACK row with its parts; a stack that is not better than its best member should not be adopted."
        else:
            stack_text = "No config experiment earned a WIN, so no stack was built. The simplest configuration (your baseline) stands."
        text, _wins = write_report(d, base_cfg, stack_text)
        reported["done"] = True
        print("\n" + text)
    except Aborted:
        say("stop requested: restoring everything")
    except Exception as e:
        say(f"run aborted: {e}")
    finally:
        STOP_FLAG["done"] = True
        errs = restore_all()
        if errs:
            say(f"WARNING: could not restore: {errs}. Run: python3 {sys.argv[0]} revert {d}")
        if was_running or True:
            try:
                info = restart_engine(state, d, "final-baseline", base_cfg)
                say("Strata restarted on your baseline config" if info["ok"] else f"final restart failed: {info['reason']}")
            except Exception as e:
                say(f"could not restart Strata: {e}")
        if not reported["done"] and load_results(d):
            write_report(d, base_cfg, "The run ended early; no stack was built.")
        try:
            os.remove(LOCK)
        except Exception:
            pass
        say(f"Done. Results: {d}/matrix.md (paste it into the audit chat) and {d}/results.jsonl")


def cmd_enginetest(a):
    """Stop Strata, free memory, start it on the current config, and report. About 5 minutes; leaves Strata running."""
    cfg = load_cfg()
    d = os.path.join(HOME, "strata-orch-enginetest")
    os.makedirs(d, exist_ok=True)
    if SUDO and subprocess.run(["sudo", "-v"]).returncode != 0:
        sys.exit("sudo authentication failed")
    state = {"proc": None}
    info = restart_engine(state, d, "enginetest", cfg)
    print(f"\nresult: started={info['ok']} in {info['start_s']} s, expert slots={info['slots']}, reason={info['reason'] or '-'}")
    print(f"flags seen: {info['flags']}")
    print(f"start log: {d}/start-enginetest.log")
    print("Strata is left running. If this looks right, the full run's stop/start machinery works on your system.")


# --- confirmation runs and the canary test ---------------------------------------------------------------------------------------
def merge_combo(ids):
    parts = []
    for i in ids:
        e = next((x for x in EXPERIMENTS if x["id"] == i), None)
        if e is None:
            sys.exit(f"unknown experiment id '{i}' (known: {', '.join(x['id'] for x in EXPERIMENTS)})")
        if e["kind"] == "runtime":
            sys.exit(f"{i} is a request setting and cannot be combined here")
        parts.append(e)
    m = dict(id="+".join(ids), name=" + ".join(p["name"] for p in parts), kind="config", args={}, env={}, sys={}, memory=False,
             wildcard=any(p["wildcard"] for p in parts), canary=any(p["canary"] for p in parts), why="confirmation of a combination")
    for p in parts:
        m["args"].update(p.get("args", {}))
        m["env"].update(p.get("env", {}))
        m["sys"].update(p.get("sys", {}))
    return m


def batch_medians(r):
    raw = r.get("raw", {})
    return [med(raw.get(m, [])) if raw.get(m) else None for m in ("decode", "edit", "prefill")]


def confirm_report(d, combo):
    recs = [r for r in load_results(d) if r["step_kind"] in ("baseline", "exp") and r.get("raw", {}).get("decode")]
    L = [f"# Confirmation: baseline (A) vs {combo['name']} (B)", "", f"Run folder: {d}", f"Generated: {time.strftime('%Y-%m-%d %H:%M')}", "",
         "Order was ABBA blocks with a Strata restart before every batch, so restart-to-restart noise is inside the comparison. Each pair compares two adjacent batches.",
         "", "| Pair | A decode | B decode | A copy-heavy | B copy-heavy | A prefill | B prefill | Index (B vs A) | A MHz / temp p95 | B MHz / temp p95 |", "|---|---:|---:|---:|---:|---:|---:|---:|---|---|"]
    idxs, tdiff, wins = [], [], 0
    for k in range(0, len(recs) - 1, 2):
        x, y = recs[k], recs[k + 1]
        a_, b_ = (x, y) if x["step_kind"] == "baseline" else (y, x)
        if a_["step_kind"] != "baseline" or b_["step_kind"] != "exp":
            continue
        ma, mb = batch_medians(a_), batch_medians(b_)
        if None in ma or None in mb:
            continue
        idx = 100 * ref_time(*ma) / ref_time(*mb)
        idxs.append(idx)
        wins += idx > 100
        sa, sb = a_["sensors"], b_["sensors"]
        if sa.get("temp_p95") is not None and sb.get("temp_p95") is not None:
            tdiff.append(sb["temp_p95"] - sa["temp_p95"])
        L.append(f"| {a_['label']} vs {b_['label']} | {ma[0]:.2f} | {mb[0]:.2f} ({100 * (mb[0] - ma[0]) / ma[0]:+.1f}%) | {ma[1]:.2f} | {mb[1]:.2f} ({100 * (mb[1] - ma[1]) / ma[1]:+.1f}%) | "
                 f"{ma[2]:.1f} | {mb[2]:.1f} ({100 * (mb[2] - ma[2]) / ma[2]:+.1f}%) | {idx:.1f} | {sa.get('act_mhz_med')} / {sa.get('temp_p95')} | {sb.get('act_mhz_med')} / {sb.get('temp_p95')} |")
    n = len(idxs)
    if not n:
        L += ["", "No complete pairs were measured."]
        verdict = "NO DATA"
    else:
        mean_idx = statistics.mean(idxs)
        mean_t = statistics.mean(tdiff) if tdiff else 0.0
        need = math.ceil(0.75 * n)
        adopt = wins >= need and mean_idx >= 104 and mean_t <= 1.0
        verdict = "ADOPT" if adopt else "DO NOT ADOPT"
        L += ["", f"Pairs where B beat A: {wins} of {n} (rule: at least {need}). Mean index {mean_idx:.1f} (rule: at least 104). Mean temperature change {mean_t:+.1f} C (rule: not hotter by more than 1 C).",
              f"**Verdict (rule fixed before the run): {verdict}**",
              "", "ADOPT means the combination beat baseline in most pairs by a margin larger than restart noise. DO NOT ADOPT means any difference is not distinguishable from noise, "
              "so the simpler configuration stands and tuning should stop."]
        if any(r.get("canary") and r["canary"].get("note") and r["canary"]["passed"] == 0 for r in recs):
            L.append("Note: the canary runner failed in some batches; run `canarytest`.")
    text = redact("\n".join(L))
    open(os.path.join(d, "confirm.md"), "w").write(text + "\n")
    return text


def cmd_confirm(a):
    if not os.environ.get("ORCH_INHIBITED") and shutil.which("systemd-inhibit"):
        os.execvpe("systemd-inhibit", ["systemd-inhibit", "--what=sleep:idle", "--who=strata-orchestrator", "--why=confirmation run",
                                       sys.executable, *sys.argv], dict(os.environ, ORCH_INHIBITED="1"))
    base_cfg = load_cfg()
    combo = merge_combo(a.combo.split("+"))
    d = a.out or os.path.join(HOME, "strata-confirm-" + time.strftime("%Y%m%d-%H%M"))
    os.makedirs(d, exist_ok=True)
    STOP_FLAG["dir"] = d
    pattern = ["A", "B", "B", "A"] * a.pairs
    batch = 0.7 + a.runs * 0.63 + a.edit_runs * 0.72 + a.prefill_runs * 1.6 + (0 if a.no_canary else 2.5)
    print(f"Confirmation of {combo['name']}\nSchedule {''.join(pattern)} with a restart before every batch: about {(len(pattern) * (batch + 3.5) + 5) / 60:.1f} h. "
          f"Create {os.path.join(d, 'STOP')} to end early.")
    if SUDO and subprocess.run(["sudo", "-v"]).returncode != 0:
        sys.exit("sudo authentication failed")
    if not preflight(a, base_cfg):
        sys.exit("preflight failed (use --force to override)")
    open(LOCK, "w").write(str(os.getpid()))
    sudo_keepalive()
    knobs = SysKnobs()
    orig_sys = knobs.snapshot()
    shutil.copy(CFG, os.path.join(d, "baseline-config.json"))
    shutil.copy(CFG, CFG + ".orch-backup-" + time.strftime("%Y%m%d-%H%M"))
    json.dump({"sys_original": orig_sys, "cfg": CFG, "started": time.time(), "combo": combo["id"]}, open(os.path.join(d, "state.json"), "w"))
    signal.signal(signal.SIGINT, lambda *_: STOP_FLAG.update(stop=True))
    signal.signal(signal.SIGTERM, lambda *_: STOP_FLAG.update(stop=True))
    state, info_cur, n = {"proc": None}, {}, {"A": 0, "B": 0}
    try:
        save_cfg(base_cfg)
        info = restart_engine(state, d, "burn-in", base_cfg)
        info_cur.update(info)
        if not info["ok"]:
            raise RuntimeError("baseline would not start: " + info["reason"])
        sa = argparse.Namespace(**{**vars(a), "runs": 3, "edit_runs": 2, "prefill_runs": 1, "no_canary": True})
        say("burn-in batch (not counted) ...")
        append_result(d, measure(sa, "BURN-IN", "burnin", None, dict(info_cur), base_cfg, canary=False))
        for arm in pattern:
            check_stop()
            n[arm] += 1
            label = f"{arm}{n[arm]}"
            if arm == "A":
                cfg_x, exp, kind = base_cfg, None, "baseline"
            else:
                cfg_x, exp, kind = with_changes(base_cfg, combo), combo, "exp"
            save_cfg(cfg_x)
            info = restart_engine(state, d, label, cfg_x)
            info_cur.clear()
            info_cur.update(info)
            if not info["ok"]:
                raise RuntimeError(f"{label} would not start: {info['reason']}")
            if arm == "B" and combo["sys"]:
                knobs.apply(combo["sys"])
            try:
                say(f"measuring {label} ...")
                rec = measure(a, label, kind, exp, dict(info_cur), cfg_x, canary=True)
                if exp and exp.get("args"):
                    fl = info_cur.get("flags", "")
                    rec["restart"]["applied"] = all(f"{k} {v}" in fl for k, v in exp["args"].items()) if fl else False
                append_result(d, rec)
                say(f"{label}: decode {med(rec.get('raw', {}).get('decode', [])) and round(med(rec['raw']['decode']), 2)} tok/s, errors {rec['errors'] or 'none'}")
            finally:
                if arm == "B" and combo["sys"]:
                    knobs.restore(orig_sys)
            save_cfg(base_cfg)
        print("\n" + confirm_report(d, combo))
    except Aborted:
        say("stop requested: restoring everything")
    except Exception as e:
        say(f"confirmation aborted: {e}")
    finally:
        STOP_FLAG["done"] = True
        knobs.restore(orig_sys)
        save_cfg(base_cfg)
        try:
            info = restart_engine(state, d, "final-baseline", base_cfg)
            say("Strata restarted on your baseline config" if info["ok"] else f"final restart failed: {info['reason']}")
        except Exception as e:
            say(f"could not restart Strata: {e}")
        if load_results(d):
            confirm_report(d, combo)
        try:
            os.remove(LOCK)
        except Exception:
            pass
        say(f"Done. Results: {d}/confirm.md (paste it into the audit chat) and {d}/results.jsonl")


def cmd_canarytest(a):
    """Run one canary problem end to end and show exactly what happens, so a runner problem can be seen."""
    if not http_ok():
        sys.exit("Strata is not answering")
    name, prompt, asserts = CANARY[0]
    _dt, _pt, _out, text = post(prompt, 300, "greedy", "off", timeout=600)
    print(f"model response ({len(text)} chars), first 500:\n{text[:500]!r}\n")
    d = tempfile.mkdtemp(prefix="canary-")
    os.chmod(d, 0o755)
    fp = os.path.join(d, "check.py")
    open(fp, "w").write(extract_code(text) + "\n\n" + asserts + "\nprint('PASS')\n")
    os.chmod(fp, 0o644)
    cmd = ["docker", "run", "--rm", "--network", "none", "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--user", f"{os.getuid()}:{os.getgid()}",
           "--memory", "256m", "--pids-limit", "64", "--read-only", "-v", f"{d}:/t:ro", "codex-sandbox", "python3", "-I", "-B", "/t/check.py"]
    print("running:", " ".join(cmd), "\n")
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
    print(f"exit code {r.returncode}\nstdout: {r.stdout.strip()[-300:]!r}\nstderr: {r.stderr.strip()[-500:]!r}")
    print("\nRESULT:", "canary runner works" if "PASS" in r.stdout else "the problem failed; see stderr above (a model error shows an assertion or syntax message; a runner error shows a docker or permission message)")
    shutil.rmtree(d, ignore_errors=True)



def cmd_report(a):
    text, _w = write_report(a.dir)
    print(text)


def cmd_revert(a):
    st = json.load(open(os.path.join(a.dir, "state.json")))
    errs = SysKnobs().restore(st["sys_original"])
    shutil.copy(os.path.join(a.dir, "baseline-config.json"), st["cfg"])
    print("restored baseline config" + (f"; problems: {errs}" if errs else " and system settings"))


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd")
    for name in ("plan", "run"):
        r = sub.add_parser(name)
        r.add_argument("--runs", type=int, default=5)
        r.add_argument("--edit-runs", type=int, default=4)
        r.add_argument("--prefill-runs", type=int, default=2)
        r.add_argument("--no-canary", action="store_true")
        r.add_argument("--only", default="")
        r.add_argument("--skip", default="")
        r.add_argument("--seed", type=int, default=20261016)
        r.add_argument("--max-hours", type=float, default=8.0)
        r.add_argument("--force", action="store_true")
        r.add_argument("--out", default="")
        r.add_argument("--resume", default="")
    sub.add_parser("enginetest")
    sub.add_parser("canarytest")
    cf = sub.add_parser("confirm")
    cf.add_argument("--combo", required=True, help="experiment ids joined by +, e.g. L1+W1")
    cf.add_argument("--pairs", type=int, default=2, help="number of ABBA blocks (default 2 = 8 batches)")
    cf.add_argument("--runs", type=int, default=5)
    cf.add_argument("--edit-runs", type=int, default=4)
    cf.add_argument("--prefill-runs", type=int, default=2)
    cf.add_argument("--no-canary", action="store_true")
    cf.add_argument("--force", action="store_true")
    cf.add_argument("--out", default="")
    rp = sub.add_parser("report")
    rp.add_argument("dir")
    rv = sub.add_parser("revert")
    rv.add_argument("dir")
    a = p.parse_args()
    {"plan": cmd_plan, "run": cmd_run, "report": cmd_report, "revert": cmd_revert, "enginetest": cmd_enginetest, "canarytest": cmd_canarytest, "confirm": cmd_confirm}.get(a.cmd, lambda _a: p.print_help())(a)


if __name__ == "__main__":
    main()
