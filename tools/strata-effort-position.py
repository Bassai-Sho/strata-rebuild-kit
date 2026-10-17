#!/usr/bin/env python3
"""strata-effort-position.py: make changing the reasoning level cheap (or put it back).

    python3 strata-effort-position.py show            what the config says now
    python3 strata-effort-position.py end             changing the level no longer makes Strata re-read the conversation
    python3 strata-effort-position.py start           the default: the level's sentence is the first thing in the prompt
        [--config PATH]   default: ~/github/strata-work/Strata/strata-swift-iq3_xxs.json

Why: Strata writes the reasoning level (low, medium, ...) as a sentence at the very START of the prompt. Alt+. and Alt+, in Codex change the
level, so by default every press changes the first lines of the prompt and Strata has to read the whole conversation again (at about 42
tokens a second: about 3 minutes for a small context, 12 minutes at 30K tokens). With "effort_position": "end" Strata puts a low or
medium level in a short turn right before the answer instead, keeps its prompt cache in front of that turn, and a change costs nothing.
The catch, in Strata's own documentation: the model sees the level in a place it was not trained to see it, and how well it follows
it there has not been measured. Try it, and use `start` to undo it.

The config is read when Strata starts: restart it afterwards (`codex stop`, then `codex`). It needs engine 0.1.39 or newer (you have
0.1.44.1); the server checks and prints `effort_position end: ...` in its log when it takes effect. A timestamped backup is made first.
"""
import argparse, datetime, json, os, shutil, sys


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("action", choices=("show", "end", "start"))
    ap.add_argument("--config", default=os.path.expanduser("~/github/strata-work/Strata/strata-swift-iq3_xxs.json"))
    a = ap.parse_args()
    try:
        with open(a.config) as fh:
            cfg = json.load(fh)
    except (OSError, ValueError) as e:
        print(f"cannot read {a.config}: {e}")
        return 2
    cur = cfg.get("effort_position", "start")
    print(f"{a.config}\n  effort_position is now: {cur!r} ({'changing the level re-reads the whole conversation' if cur != 'end' else 'changing the level is free'})")
    if a.action == "show":
        return 0
    want = "end" if a.action == "end" else "start"
    if cur == want:
        print("  already set: nothing to change")
        return 0
    backup = f"{a.config}.bak-effort-{datetime.datetime.now():%Y%m%d-%H%M%S}"
    shutil.copy(a.config, backup)
    if want == "end":
        cfg["effort_position"] = "end"
    else:
        cfg.pop("effort_position", None)
    tmp = a.config + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(cfg, fh, indent=2)
        fh.write("\n")
    with open(tmp) as fh:
        json.load(fh)                                  # never leave a file that does not parse
    os.replace(tmp, a.config)
    print(f"  set to {want!r}. Backup: {backup}")
    print("  Restart Strata for it to take effect:  codex stop   then   codex")
    if want == "end":
        print("  Check the log afterwards:  grep effort_position ~/github/strata-work/Strata/strata-swift-iq3_xxs.log | tail -2")
    return 0


if __name__ == "__main__":
    sys.exit(main())
