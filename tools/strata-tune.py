#!/usr/bin/env python3
"""strata-tune.py: change ONE setting of the Strata config safely.  The first change makes a baseline copy
(<config>.tune-orig); `restore` puts it back.  Restart Strata after every change.
  strata-tune.py show
  strata-tune.py set-arg NAME [VALUE]      e.g. set-arg --lookup-chain 3     set-arg --kv-grow
  strata-tune.py unset-arg NAME
  strata-tune.py set-env NAME VALUE        e.g. set-env SYCL_PI_LEVEL_ZERO_USE_IMMEDIATE_COMMANDLISTS 1
  strata-tune.py unset-env NAME
  strata-tune.py set-key NAME VALUE        e.g. set-key reasoning_budget_tokens 1024   (VALUE is read as JSON)
  strata-tune.py restore"""
import json, os, shutil, sys
CFG = os.environ.get("STRATA_CFG", os.path.expanduser("~/github/strata-work/Strata/strata-swift-iq3_xxs.json"))
ORIG = CFG + ".tune-orig"
if len(sys.argv) < 2 or not os.path.exists(CFG): print(__doc__); sys.exit(1)
cmd, rest = sys.argv[1], sys.argv[2:]
c = json.load(open(CFG)); a = c.setdefault("args", []); env = c.setdefault("env", {})

def show():
    print("args:", " ".join(a)); print("env: ", ", ".join(f"{k}={v}" for k, v in env.items()))
    print("keys:", {k: v for k, v in c.items() if k not in ("args", "env")})

if cmd == "show": show(); sys.exit(0)
if cmd == "restore":
    if not os.path.exists(ORIG): sys.exit("no baseline copy yet (nothing was changed with this tool)")
    shutil.copy(ORIG, CFG); print("restored the baseline from", ORIG); sys.exit(0)
before = json.dumps(c, sort_keys=True)
if cmd == "set-arg" and rest:
    name, val = rest[0], (rest[1] if len(rest) > 1 else None)
    if name in a:
        i = a.index(name)
        if val is not None:
            if i + 1 < len(a) and not a[i + 1].startswith("--"): a[i + 1] = val
            else: a.insert(i + 1, val)
    else:
        a.append(name)
        if val is not None: a.append(val)
elif cmd == "unset-arg" and rest:
    name = rest[0]
    if name not in a: sys.exit(f"{name} is not set")
    i = a.index(name); j = i + 2 if i + 1 < len(a) and not a[i + 1].startswith("--") else i + 1; del a[i:j]
elif cmd == "set-env" and len(rest) == 2: env[rest[0]] = rest[1]
elif cmd == "unset-env" and rest:
    if rest[0] not in env: sys.exit(f"{rest[0]} is not set")
    del env[rest[0]]
elif cmd == "set-key" and len(rest) == 2:
    try: c[rest[0]] = json.loads(rest[1])
    except Exception: c[rest[0]] = rest[1]
else: print(__doc__); sys.exit(1)
if json.dumps(c, sort_keys=True) == before: print("nothing to change"); sys.exit(0)
if not os.path.exists(ORIG): shutil.copy(CFG, ORIG); print("baseline saved:", ORIG)
json.dump(c, open(CFG, "w"), indent=2); show()
print("\nNow restart Strata (Ctrl+C, then ~/start-swift.sh), then run strata-bench.py LABEL.")
