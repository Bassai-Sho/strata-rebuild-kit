#!/usr/bin/env python3
"""codex-stats.py [ROLLOUT.jsonl]: where a Codex session's time and context went. Default: the newest session log in ~/agent/codex-home/sessions.

Per request: tokens newly read (input minus cached), tokens written, seconds since the previous request finished. Then every tool output with its size and command,
and totals. Use it before and after a change: the numbers that matter are the total tool-output tokens, the number of requests and the biggest outputs.
Rule of thumb from this machine's Strata log: a request costs about 18 s + 21 ms per newly read token, plus about 0.1 s per generated token."""
import glob, json, os, sys, datetime

def newest():
    fs = glob.glob(os.path.expanduser("~/agent/codex-home/sessions/**/rollout-*.jsonl"), recursive=True)
    return max(fs, key=os.path.getmtime) if fs else sys.exit("no session logs found")

def ts(d):
    try: return datetime.datetime.fromisoformat(d["timestamp"].replace("Z", "+00:00")).timestamp()
    except Exception: return None

path = sys.argv[1] if len(sys.argv) > 1 else newest()
rows = [json.loads(l) for l in open(path) if l.strip()]
calls, outs, reqs, prev, last_tot, compactions, t_first = {}, [], [], None, None, [], None
for d in rows:
    p = d.get("payload") or {}; t = p.get("type")
    if t == "task_started" and prev is None: prev = ts(d); t_first = prev
    if d.get("type") == "compacted":
        compactions.append((len(reqs), "Automatic handoff" in json.dumps(p)[:4000]))      # after this many requests; True = the filter's instant handoff
    if d.get("type") == "response_item":
        if t in ("function_call", "custom_tool_call"):
            calls[p.get("call_id")] = str(p.get("arguments") or p.get("input"))
        elif t in ("function_call_output", "custom_tool_call_output"):
            o = p.get("output"); s = o if isinstance(o, str) else json.dumps(o)
            c = calls.get(p.get("call_id"), "?")
            try: c = json.loads(c).get("cmd", c)
            except Exception: pass
            outs.append((len(s) // 4, " ".join(str(c).split())[:110]))
    if t == "token_count" and p.get("info"):
        tu = p["info"]["total_token_usage"]; key = (tu["input_tokens"], tu["output_tokens"])
        if key == last_tot: continue                                       # the same request reported twice
        last_tot = key
        u = p["info"]["last_token_usage"]; now = ts(d)
        reqs.append((u["input_tokens"], u["cached_input_tokens"], u["output_tokens"], (now - prev) if now and prev else 0))
        prev = now
print(path)
print(f"{'req':>3} {'in':>6} {'cached':>6} {'new':>5} {'out':>4} {'secs':>5}")
cold_big = [i for i, (a, c, o, sec) in enumerate(reqs, 1) if c == 0 and a > 20000]
for i, (a, c, o, sec) in enumerate(reqs, 1):
    if len(reqs) > 40 and not (i <= 3 or i in cold_big or i - 1 in cold_big or i % 10 == 0 or i == len(reqs)): continue      # long session: show the key rows only
    print(f"{i:>3} {a:>6} {c:>6} {a-c:>5} {o:>4} {sec:>5.0f}" + ("   <== cold read of the whole context (compaction)" if i in cold_big else ""))
print("\ntool outputs (tokens ~ chars/4):")
for n, c in outs:
    print(f"{n:>6}{' !' if n >= 1500 else '  '} {c}")
tot = sum(n for n, _ in outs)
new_tot = sum(a - c for a, c, o, sec in reqs); out_tot = sum(o for a, c, o, sec in reqs)
cold_tok = sum(reqs[i - 1][0] for i in cold_big)
print(f"\nnewly read tokens {new_tot} (compaction cold reads {cold_tok} = {100*cold_tok//max(new_tot,1)}%) | output tokens {out_tot} (decode about {out_tot/9.5/60:.0f} min)")
inst = sum(1 for _, a in compactions if a)
print(f"compactions: {len(compactions)} (instant handoff {inst}, by the model {len(compactions) - inst})" + (f" | slow ones cost about {(cold_tok*0.0206 + 18*len(cold_big))/60:.0f} min of re-reading" if cold_big else ""))
print(f"\nrequests {len(reqs)} | tool calls {len(outs)} | tool-output tokens {tot} | largest {max((n for n,_ in outs), default=0)} | "
      f"context at last request {reqs[-1][0] if reqs else 0} | elapsed {sum(r[3] for r in reqs)/60:.1f} min")
