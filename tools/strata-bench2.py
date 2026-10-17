#!/usr/bin/env python3
"""strata-bench2.py v1.1: noise-aware benchmark for a running Strata server (D-A-C audit, step 2).

  python3 strata-bench2.py check                         preflight only (server, flags, clock sensor, load); changes nothing
  python3 strata-bench2.py run LABEL [options]           measure and append to ~/strata-bench2.csv (+ raw samples in .jsonl)
  python3 strata-bench2.py compare "A*" "B*"             compare two label patterns (pools every batch whose label matches)
  python3 strata-bench2.py show                          last batches, one line each

run options:
  --runs N          decode and edit repeats (default 6; fewer than 5 cannot show a 3% change over the noise)
  --prefill-runs N  long-prompt reads, each unique so the prompt cache cannot help (default 3, about 95 s each; 0 skips; the
                    prefill figure is very steady, so 1 is enough when you are testing decode)
  --prefill-kind code|repeat   'code' (default) reads real Python source; 'repeat' reads the old repeated-sentence prompt
  --no-warm-decode  skip the uncounted 300-token warm-up decode (it removes the slow first run seen on a cold start)
  --think off|on    'off' (default) switches thinking off; 'on' uses the config's own setting (agent-like)
  --sampling greedy|qwen-think|qwen-instruct   (default greedy)
  --note TEXT       free text stored with the batch

Design (why it differs from a single-run benchmark): every figure is the MEDIAN of repeated runs, the spread is stored, and
each batch also records the GPU clock, throttle reasons, package temperature, memory, load and the engine's flags, so a
difference can be attributed to the setting and not to the weather. Compare uses an exact permutation test, and flags a
clock confound when the two arms ran at different GPU frequencies. Use ABBA order (A1 B1 B2 A2) for settings needing a restart.
Reads only: it never edits the config or restarts anything. Environment overrides: STRATA_URL STRATA_LOG STRATA_CFG STRATA_DIR XE_FREQ_DIR
"""
import argparse, csv, fnmatch, glob, hashlib, itertools, json, os, random, re, statistics, subprocess, sys, threading, time, urllib.request, uuid

HOME = os.path.expanduser("~")
URL = os.environ.get("STRATA_URL", "http://127.0.0.1:8085")
STRATA_DIR = os.environ.get("STRATA_DIR", f"{HOME}/github/strata-work/Strata")
LOG = os.environ.get("STRATA_LOG", f"{STRATA_DIR}/strata-swift-iq3_xxs.log")
CFG = os.environ.get("STRATA_CFG", f"{STRATA_DIR}/strata-swift-iq3_xxs.json")
CSV_PATH = f"{HOME}/strata-bench2.csv"
RAW_PATH = f"{HOME}/strata-bench2.jsonl"
SAMPLING = {
    "greedy": {"temperature": 0},
    "qwen-think": {"temperature": 1.0, "top_p": 0.95, "top_k": 20, "presence_penalty": 0.0},
    "qwen-instruct": {"temperature": 0.7, "top_p": 0.8, "top_k": 20, "presence_penalty": 1.5},
}
FLAGS = ["--max-context", "--prefill", "--short-read", "--spec", "--spec-min-p", "--pool-workers", "--lookup-chain", "--mtp-q4",
         "--mtp-window", "--suffix-draft", "--kv", "--kv-resident", "--kv-grow", "--vram-reserve-mib", "--stream-experts",
         "--no-prefill-borrow"]
CSV_COLS = ["time", "label", "version", "git", "cfg_hash", "flags", "env", "sampling", "think", "n", "decode_med", "decode_spread_pct",
            "edit_med", "edit_spread_pct", "prefill_med", "prefill_n", "first_s", "act_mhz_med", "act_mhz_max", "throttle_share",
            "throttle_reasons", "pkg_temp_max", "temp_p50", "temp_p95", "hot_share", "drift_pct", "prefill_kind", "memavail_min_mib", "swap_max_mib", "load1", "epp", "platform_profile", "note"]


def rd(path, default=""):
    try:
        return open(path).read().strip()
    except Exception:
        return default


# --- hardware sensors (all optional, never required) ---------------------------------------------------------
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
    """Samples GPU clock, throttle flags, package temperature and memory twice a second, with timestamps."""

    def __init__(self):
        super().__init__(daemon=True)
        self.d = xe_dir()
        self.stop_evt = threading.Event()
        self.samples = []  # (t, act_mhz, throttled, temp_c, memavail_mib, swap_mib, reasons)

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

    def window(self, t0, t1):
        sel = [x for x in self.samples if t0 <= x[0] <= t1]
        acts = [x[1] for x in sel if x[1] > 0]
        temps = [x[3] for x in sel if x[3] is not None]
        return {"act": int(statistics.median(acts)) if acts else None, "temp_max": max(temps) if temps else None}

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
                "act_mhz_max": max(x[1] for x in live) if n else None,
                "throttle_share": round(sum(1 for x in live if x[2]) / n, 2) if n else None,
                "throttle_reasons": " ".join(f"{k}:{v}" for k, v in sorted(reasons.items(), key=lambda kv: -kv[1])[:3]),
                "pkg_temp_max": temps[-1] if temps else None, "temp_p50": pct(0.5), "temp_p95": pct(0.95),
                "hot_share": round(sum(1 for t in temps if t >= 95) / len(temps), 2) if temps else None,
                "memavail_min_mib": min(x[4] for x in self.samples) if self.samples else None,
                "swap_max_mib": max(x[5] for x in self.samples) if self.samples else None}


# --- server calls -----------------------------------------------------------------------------------------------
def post(content, max_tokens, sampling="greedy", think="off", timeout=3600):
    body = {"model": "x", "messages": [{"role": "user", "content": content}], "max_tokens": max_tokens, **SAMPLING[sampling]}
    if think == "off":
        body["chat_template_kwargs"] = {"enable_thinking": False}
    req = urllib.request.Request(URL + "/v1/chat/completions", json.dumps(body).encode(), {"Content-Type": "application/json"})
    t = time.time()
    r = json.load(urllib.request.urlopen(req, timeout=timeout))
    dt = time.time() - t
    u = r.get("usage", {})
    return dt, u.get("prompt_tokens", 0), u.get("completion_tokens", 0)


def code_listing():
    lines = ["class Inventory:", "    def __init__(self):", "        self.items = {}", ""]
    for i in range(1, 9):
        lines += [f"    def add_{i}(self, name, qty):", "        if qty <= 0:", "            raise ValueError('qty must be positive')",
                  f"        self.items[name] = self.items.get(name, 0) + qty * {i}", "        return self.items[name]", ""]
    return "\n".join(lines)


def real_text(chars=16000):
    """Real source code (same files every time, so arms are comparable); falls back to repeated prose."""
    lib = os.path.dirname(os.__file__)
    out = ""
    for name in ["argparse.py", "textwrap.py", "heapq.py", "bisect.py", "fnmatch.py", "shlex.py", "csv.py", "glob.py"]:
        out += rd(os.path.join(lib, name)) + "\n"
        if len(out) >= chars:
            break
    return out[:chars] if len(out) > 2000 else "The quick brown fox jumps over the lazy dog. " * (chars // 45)


def prefill_text(kind, chars=16000):
    if kind == "repeat":
        return ("Fact: the quick brown fox jumps over the lazy dog. " * (chars // 51 + 1))[:chars]
    return real_text(chars)


# --- engine facts -----------------------------------------------------------------------------------------------------
def engine_facts():
    flags = ""
    version = ""
    try:
        lines = open(LOG, errors="replace").read().splitlines()
        started = [l for l in lines if "engine started" in l]
        up = [l for l in lines if "session is up (engine" in l]
        if started:
            found = []
            for k in FLAGS:
                m = re.search(rf"(?<!\S){re.escape(k)}(?: (?!--)(\S+))?(?=\s|$)", started[-1])
                if m:
                    found.append(k + (f" {m.group(1)}" if m.group(1) else ""))
            flags = " ".join(found)
        if up:
            m = re.search(r"engine ([0-9.]+)", up[-1])
            version = m.group(1) if m else ""
    except Exception:
        pass
    try:
        c = json.load(open(CFG))
        env = ",".join(f"{k}={v}" for k, v in sorted(c.get("env", {}).items()) if not re.search(r"key|token|secret|pass", k, re.I))
        cfg_hash = hashlib.sha256(open(CFG, "rb").read()).hexdigest()[:8]
    except Exception:
        env, cfg_hash = "", ""
    try:
        git = subprocess.run(["git", "-C", STRATA_DIR, "describe", "--tags", "--always"], capture_output=True, text=True, timeout=5).stdout.strip()
    except Exception:
        git = ""
    return {"version": version, "git": git, "flags": flags, "env": env, "cfg_hash": cfg_hash}


def preflight(verbose=True):
    ok = True
    try:
        urllib.request.urlopen(URL + "/v1/models", timeout=5).read()
        up = "reachable"
    except Exception as e:
        up, ok = f"NOT REACHABLE ({e})", False
    f = engine_facts()
    a, s = meminfo()
    load1 = float(rd("/proc/loadavg", "0 0 0").split()[0])
    if verbose:
        print(f"server      {URL}: {up}")
        print(f"engine      {f['version'] or '?'} (git {f['git'] or '?'})  config hash {f['cfg_hash'] or '?'}")
        print(f"flags       {f['flags'] or '(not found in log: check STRATA_LOG)'}")
        print("            ^ these come from the LAST 'engine started' line in the log; confirm they match the start you want to measure")
        print(f"env         {f['env'] or '?'}")
        print(f"gpu clock   {'sensor: ' + xe_dir() if xe_dir() else 'NO xe sensor found (clock will not be recorded)'}")
        print(f"memory      MemAvailable {a} MiB, swap used {s} MiB")
        print(f"load (1m)   {load1}" + ("   WARNING: more than the engine itself is busy (Strata alone is about 1); close it or note it" if load1 > 2.0 else "   (Strata alone accounts for about 1)"))
        print(f"cpu         epp={rd('/sys/devices/system/cpu/cpu0/cpufreq/energy_performance_preference')} "
              f"platform_profile={rd('/sys/firmware/acpi/platform_profile')}")
    return ok, f, load1


# --- statistics ----------------------------------------------------------------------------------------------------------
def summ(v):
    if not v:
        return None, None
    m = statistics.median(v)
    return round(m, 2), round(100 * (max(v) - min(v)) / m, 1) if m else None


def perm_p(a, b, trials=20000):
    """Two-sided permutation p-value for a difference in medians (exact when small)."""
    obs = abs(statistics.median(a) - statistics.median(b))
    pool, na = list(a) + list(b), len(a)
    idx = range(len(pool))
    import math
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


# --- commands -----------------------------------------------------------------------------------------------------------
def cmd_run(a):
    ok, facts, load1 = preflight()
    if not ok:
        sys.exit("Strata is not answering; start it first (~/start-swift.sh).")
    print(f"\nbenchmark '{a.label}'  runs={a.runs} prefill-runs={a.prefill_runs} ({a.prefill_kind}) think={a.think} sampling={a.sampling}")
    smp = Sampler()
    smp.start()

    def timed(content, n):
        t0 = time.time()
        dt, pt, out = post(content, n, a.sampling, a.think)
        w = smp.window(t0, time.time())
        return dt, pt, out, w

    def note(w):
        return f"  clock {w['act']} MHz  temp max {w['temp_max']} C"

    dt, _, _, _ = timed("Name one animal that starts with the letter A.", 24)
    first_s = round(dt, 1)
    print(f"  warm-up request          {first_s:6.1f} s (not counted)")
    dec_prompt = "Write a detailed explanation of how a refrigerator works."
    if not a.no_warm_decode:
        dt, _, out, w = timed(dec_prompt, 300)
        print(f"  warm-up decode           {out / dt:6.2f} tok/s (not counted){note(w)}", flush=True)
    dec, dec_clock, dec_temp = [], [], []
    for i in range(a.runs):
        dt, _, out, w = timed(dec_prompt, 300)
        dec.append(out / dt)
        dec_clock.append(w["act"])
        dec_temp.append(w["temp_max"])
        print(f"  decode {i + 1}/{a.runs}            {out / dt:6.2f} tok/s ({out} tokens){note(w)}", flush=True)
    edit_prompt = "Here is some code:\n\n" + code_listing() + "\n\nRewrite all of this code exactly as it is, but rename the attribute 'items' to 'entries'. Output only the code."
    post(edit_prompt, 1, a.sampling, a.think)
    edt = []
    for i in range(a.runs):
        dt, _, out, w = timed(edit_prompt, 450)
        edt.append(out / dt)
        print(f"  code-edit {i + 1}/{a.runs}         {out / dt:6.2f} tok/s ({out} tokens){note(w)}", flush=True)
    pre = []
    text = prefill_text(a.prefill_kind)
    for i in range(a.prefill_runs):
        nonce = uuid.uuid4().hex[:12]
        dt, pt, _, w = timed(f"run-id: {nonce}\n\n{text}\n\nIn one word: what language is this code written in?", 1)
        pre.append(pt / dt)
        print(f"  prefill {i + 1}/{a.prefill_runs}           {pt / dt:6.1f} tok/s ({pt} tokens, {dt:.0f} s){note(w)}", flush=True)
    sens = smp.result()
    dm, ds = summ(dec)
    em, es = summ(edt)
    pm, _ = summ(pre)
    drift = None
    if len(dec) >= 6:
        h = len(dec) // 2
        drift = round(100 * (statistics.median(dec[-h:]) - statistics.median(dec[:h])) / statistics.median(dec[:h]), 1)
    row = {"time": time.strftime("%Y-%m-%d %H:%M"), "label": a.label, **{k: facts[k] for k in ("version", "git", "cfg_hash", "flags", "env")},
           "sampling": a.sampling, "think": a.think, "n": a.runs, "decode_med": dm, "decode_spread_pct": ds, "edit_med": em,
           "edit_spread_pct": es, "prefill_med": pm, "prefill_n": len(pre), "first_s": first_s, **sens, "drift_pct": drift,
           "prefill_kind": a.prefill_kind, "load1": load1,
           "epp": rd("/sys/devices/system/cpu/cpu0/cpufreq/energy_performance_preference"),
           "platform_profile": rd("/sys/firmware/acpi/platform_profile"), "note": a.note}
    write_csv_row(row)
    with open(RAW_PATH, "a") as f:
        f.write(json.dumps({**row, "raw": {"decode": dec, "edit": edt, "prefill": pre, "decode_clock": dec_clock, "decode_temp": dec_temp}}) + "\n")
    print("\nsummary (paste this block back into the audit chat):")
    print(f"  label={a.label} version={facts['version']} sampling={a.sampling} think={a.think} n={a.runs} prefill={a.prefill_kind}")
    print(f"  decode  median {dm} tok/s  spread {ds}%  drift first-half to second-half {drift}%   |  code-edit median {em} tok/s  spread {es}%   |  prefill median {pm} tok/s")
    print(f"  decode runs {[round(x, 2) for x in dec]}")
    print(f"  decode clock MHz {dec_clock}   decode temp max C {dec_temp}")
    print(f"  GPU clock median {sens['act_mhz_med']} MHz (max {sens['act_mhz_max']}), throttled {sens['throttle_share']} of samples {sens['throttle_reasons']}")
    print(f"  pkg temp: median {sens['temp_p50']} C, p95 {sens['temp_p95']} C, max {sens['pkg_temp_max']} C, share of time at 95 C or more {sens['hot_share']}")
    print(f"  MemAvailable min {sens['memavail_min_mib']} MiB, swap max {sens['swap_max_mib']} MiB, load1 {load1}")
    print(f"  flags: {facts['flags']}")
    print(f"  env:   {facts['env']}")
    print(f"saved: {CSV_PATH}  and  {RAW_PATH}")
    if ds is not None and ds > 6:
        print(f"  NOTE: decode spread {ds}% is wide. Re-run before trusting any difference smaller than that.")
    if drift is not None and drift <= -4:
        print(f"  NOTE: decode fell {abs(drift)}% between the first and second half of the batch. That pattern fits heat soak or memory pressure; "
              f"use ABBA order and compare temperatures.")


def write_csv_row(row):
    if os.path.exists(CSV_PATH):
        with open(CSV_PATH) as f:
            head = f.readline().strip().split(",")
        if head != CSV_COLS:
            os.rename(CSV_PATH, CSV_PATH + ".old")
            print(f"  (older results file kept as {CSV_PATH}.old because the columns changed)")
    new = not os.path.exists(CSV_PATH)
    with open(CSV_PATH, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLS, extrasaction="ignore")
        if new:
            w.writeheader()
        w.writerow(row)


def load_raw():
    if not os.path.exists(RAW_PATH):
        return []
    out = []
    for line in open(RAW_PATH):
        try:
            out.append(json.loads(line))
        except Exception:
            pass
    return out


def cmd_compare(a):
    recs = load_raw()
    A = [r for r in recs if fnmatch.fnmatch(r["label"], a.a)]
    B = [r for r in recs if fnmatch.fnmatch(r["label"], a.b)]
    if not A or not B:
        sys.exit(f"need batches for both patterns (found {len(A)} for '{a.a}', {len(B)} for '{a.b}'). Labels seen: {sorted({r['label'] for r in recs})}")
    print(f"A = '{a.a}' ({len(A)} batches)   B = '{a.b}' ({len(B)} batches)\n")
    print(f"{'metric':12s} {'A median':>9s} {'B median':>9s} {'change':>8s} {'p-value':>8s}  {'verdict'}")
    for m in ("decode", "edit", "prefill"):
        va = [x for r in A for x in r["raw"][m]]
        vb = [x for r in B for x in r["raw"][m]]
        if len(va) < 3 or len(vb) < 3:
            print(f"{m:12s} too few samples (A {len(va)}, B {len(vb)})")
            continue
        ma, mb = statistics.median(va), statistics.median(vb)
        ch = 100 * (mb - ma) / ma
        p = perm_p(va, vb)
        verdict = "REAL" if abs(ch) >= 3 and p <= 0.05 else "LIKELY" if abs(ch) >= 3 and p <= 0.15 else "NOISE"
        print(f"{m:12s} {ma:9.2f} {mb:9.2f} {ch:+7.1f}% {p:8.3f}  {verdict}   (n {len(va)} vs {len(vb)})")
    ca = [r["act_mhz_med"] for r in A if r.get("act_mhz_med")]
    cb = [r["act_mhz_med"] for r in B if r.get("act_mhz_med")]
    if ca and cb:
        fa, fb = statistics.median(ca), statistics.median(cb)
        d = 100 * (fb - fa) / fa
        print(f"\nGPU clock median: A {fa:.0f} MHz, B {fb:.0f} MHz ({d:+.1f}%)" +
              ("   CLOCK CONFOUND: the arms ran at different frequencies, so part of any change is thermal/power, not the setting" if abs(d) > 2 else "   (clocks comparable)"))
    ta = [r["temp_p95"] for r in A if r.get("temp_p95") is not None]
    tb = [r["temp_p95"] for r in B if r.get("temp_p95") is not None]
    if ta and tb:
        da = statistics.median(tb) - statistics.median(ta)
        print(f"Package temp p95: A {statistics.median(ta):.0f} C, B {statistics.median(tb):.0f} C ({da:+.0f} C)" +
              ("   THERMAL CONFOUND: the arms ran at different temperatures; repeat in ABBA order after a cool-down" if abs(da) > 3 else ""))
    fl = {r["flags"] + " | " + r["env"] for r in A}, {r["flags"] + " | " + r["env"] for r in B}
    if fl[0] == fl[1]:
        print("WARNING: A and B recorded identical flags and env. If you changed a setting it did not take effect (or you did not restart).")
    print("\nRule: act on REAL only. LIKELY means repeat in ABBA order. NOISE means keep the simpler configuration.")


def cmd_show(a):
    if not os.path.exists(CSV_PATH):
        sys.exit("no results yet")
    rows = list(csv.DictReader(open(CSV_PATH)))[-10:]
    print(f"{'label':22s} {'dec':>6s} {'spr%':>5s} {'edit':>6s} {'prefill':>7s} {'MHz':>5s} {'thr':>5s}  version  sampling/think")
    for r in rows:
        print(f"{r['label'][:22]:22s} {r['decode_med']:>6s} {r['decode_spread_pct']:>5s} {r['edit_med']:>6s} {r['prefill_med']:>7s} "
              f"{r['act_mhz_med']:>5s} {r['throttle_share']:>5s}  {r['version']}  {r['sampling']}/{r['think']}")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("check")
    r = sub.add_parser("run")
    r.add_argument("label")
    r.add_argument("--runs", type=int, default=6)
    r.add_argument("--prefill-runs", type=int, default=3)
    r.add_argument("--think", choices=["off", "on"], default="off")
    r.add_argument("--sampling", choices=list(SAMPLING), default="greedy")
    r.add_argument("--prefill-kind", choices=["code", "repeat"], default="code")
    r.add_argument("--no-warm-decode", action="store_true")
    r.add_argument("--note", default="")
    c = sub.add_parser("compare")
    c.add_argument("a")
    c.add_argument("b")
    sub.add_parser("show")
    a = p.parse_args()
    if a.cmd == "check":
        preflight()
    elif a.cmd == "run":
        cmd_run(a)
    elif a.cmd == "compare":
        cmd_compare(a)
    elif a.cmd == "show":
        cmd_show(a)
    else:
        p.print_help()


if __name__ == "__main__":
    main()
