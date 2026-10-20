#!/usr/bin/env python3
"""edit-failures.py: how often do edits fail in your real Codex sessions, per edit attempt? Read-only.

    python3 edit-failures.py [FOLDER ...] [--since YYYY-MM-DD] [--until YYYY-MM-DD]
                                               default folders: ~/agent/logs and ~/agent/codex-home/sessions
                                               --since/--until use each file's last-modified time, to compare before and after a change.
                                               Give a date or a date and time: 2026-10-19  or  "2026-10-19 22:30"

Reads the session files (rollout-*.jsonl), pairs every tool call with its output by call_id (so a failure that is logged two or three times
counts once), and reports: tool calls, edit attempts (apply_patch, or hl edit), how many failed and why, split into interactive sessions and
`codex loop` sessions, and the sessions with most failures. It cannot see edits that applied but were wrong, and a failed edit that the
model never retried looks the same as one it fixed.
Use it before deciding whether a better edit tool is worth trying, and again after, on the same kind of work.
"""
import collections, datetime, json, os, re, sys

CAUSES = [  # first match wins
    ("bad patch format (e.g. missing *** End Patch)", r"(invalid hunk|The (first|last) line of the patch must be|Invalid patch|is not a valid hunk header|Invalid (Add|Update|Delete) File)"),
    ("lines not found (context does not match the file)", r"Failed to find expected lines"),
    ("file problem (read/write/create)", r"(Failed to read file to update|Failed to (write|create|delete) |No such file)"),
    ("patch rejected, other", r"apply_patch verification failed"),
    ("hl rejected (stale or wrong anchor)", r"REJECTED, nothing was changed"),
]
EDIT = re.compile(r"(apply_patch|\*\*\* Begin Patch|\bhl edit\b)")


def rollouts(roots):
    for r in roots:
        r = os.path.expanduser(r)
        if os.path.isfile(r):
            yield r
        for d, _, fs in os.walk(r):
            for f in fs:
                if f.endswith(".jsonl"):
                    yield os.path.join(d, f)


def walk(o):
    if isinstance(o, dict):
        yield o
        for v in o.values():
            yield from walk(v)
    elif isinstance(o, list):
        for v in o:
            yield from walk(v)


def scan(path):
    calls = {}
    with open(path, errors="replace") as fh:
        for line in fh:
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            for d in walk(obj):
                cid = d.get("call_id")
                if not cid:
                    continue
                c = calls.setdefault(cid, {"edit": False, "causes": set(), "snip": ""})
                text = json.dumps(d, ensure_ascii=False)
                if d.get("type") in ("function_call", "custom_tool_call", "local_shell_call") or "arguments" in d or "input" in d:
                    if EDIT.search(json.dumps([d.get("arguments"), d.get("input"), d.get("name")], ensure_ascii=False)):
                        c["edit"] = True
                else:
                    for name, rx in CAUSES:
                        m = re.search(rx, text)
                        if m:
                            c["causes"].add(name)
                            if not c["snip"]:
                                c["snip"] = re.sub(r"\s+", " ", text[max(0, m.start() - 40): m.end() + 150].replace("\\n", " "))
                            break
    return calls


def main(argv):
    args, since, until, i = [], None, None, 1
    while i < len(argv):
        if argv[i] in ("--since", "--until") and i + 1 < len(argv):
            try:
                t = datetime.datetime.fromisoformat(argv[i + 1])
            except ValueError:
                sys.exit(f"cannot read the date {argv[i + 1]!r}: use 2026-10-19 or '2026-10-19 22:30'")
            d = t.timestamp()
            if argv[i] == "--since":
                since = d
            else:
                until = d + (86400 if len(argv[i + 1]) <= 10 else 0)
            i += 2
        else:
            args.append(argv[i]); i += 1
    roots = args or ["~/agent/logs", "~/agent/codex-home/sessions"]
    groups = collections.defaultdict(lambda: collections.Counter())
    causes, examples, per_session = collections.Counter(), {}, []
    nfiles = 0
    for p in rollouts(roots):
        mt = os.path.getmtime(p)
        if (since and mt < since) or (until and mt >= until):
            continue
        calls = scan(p)
        if not calls:
            continue
        nfiles += 1
        g = "loop" if "/logs/loop-" in p else "interactive"
        edits = [c for c in calls.values() if c["edit"]]
        failed = [c for c in edits if c["causes"]]
        groups[g]["sessions"] += 1
        groups[g]["calls"] += len(calls)
        groups[g]["edits"] += len(edits)
        groups[g]["failed"] += len(failed)
        for c in failed:
            name = sorted(c["causes"], key=[n for n, _ in CAUSES].index)[0]
            causes[name] += 1
            examples.setdefault(name, c["snip"])
        if failed:
            per_session.append((len(failed), len(edits), p))
    print(f"read {nfiles} session files under {', '.join(roots)}")
    tot_e = sum(g["edits"] for g in groups.values()); tot_f = sum(g["failed"] for g in groups.values())
    for g, c in sorted(groups.items()):
        rate = 100 * c["failed"] / c["edits"] if c["edits"] else 0
        print(f"  {g:12s} {c['sessions']:4d} sessions  {c['calls']:6d} tool calls  {c['edits']:5d} edit attempts  {c['failed']:4d} failed  ({rate:.1f}%)")
    print(f"  {'all':12s} {'':4s}          {'':6s}            {tot_e:5d} edit attempts  {tot_f:4d} failed  ({(100 * tot_f / tot_e if tot_e else 0):.1f}%)")
    if causes:
        print("why they failed:")
        for name, c in causes.most_common():
            print(f"  {c:4d}  {name}\n        e.g. ...{examples[name][:190]}")
        print("sessions with most failures (failed / edit attempts):")
        for f, e, p in sorted(per_session, reverse=True)[:5]:
            print(f"  {f:3d} / {e:<4d} {p}")
    print("Rule of thumb for deciding whether a better edit tool (for example hash-anchored edits) is worth trying: under about 5% failed and under one per task: not worth it; over about 10%: try it.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
