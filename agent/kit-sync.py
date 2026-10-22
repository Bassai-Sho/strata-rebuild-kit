#!/usr/bin/env python3
"""kit-sync.py: keep ONE file in step between the kit (the git repo) and the live install, like a three-way merge instead of "overwrite".

    kit-sync.py check|install KEY --kit KITFILE --live LIVEFILE --base BASEDIR --backups DIR [--label TEXT] [--mode 644]
                [--kit-wins] [--live-wins] [--no-pull]

Why not "the newer file wins" (rsync -u)? A modification time says that a file changed, not WHICH side changed, and it breaks as soon as both did, or a copy/checkout
touches the time. So every file keeps a BASELINE: the text the kit installed last time (BASEDIR/KEY). Comparing three texts shows what really happened:

  kit == live                      ok (the baseline is refreshed)
  live == baseline, kit differs    the kit changed, you did not: live is UPDATED (old copy saved)
  kit == baseline, live differs    YOU changed it (the loop adds lessons to the project template, you edit scripts): live is KEPT and
                                   PULLED into the kit file, so the next `git diff` in the kit shows it (--no-pull: kept only)
  both differ from the baseline    MERGED line by line with `git merge-file`; a clean merge goes to both sides, a conflict changes nothing and the
                                   merge with markers is saved in BACKUPS
  no baseline and they differ      UNKNOWN (no history to tell who is newer): live is kept; --kit-wins replaces it, --live-wins pulls it into the kit

`check` never writes anything. --kit-wins / --live-wins decide every differing file for that run (the old copy of the losing side is saved in BACKUPS)."""
import argparse, os, shutil, subprocess, sys, tempfile, time


def rd(p):
    try:
        with open(p, "rb") as fh:
            return fh.read()
    except OSError:
        return None


def wr(p, data, mode=None):
    os.makedirs(os.path.dirname(os.path.abspath(p)), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(p)), prefix=".ks-")
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    os.chmod(tmp, mode if mode is not None else (os.stat(p).st_mode & 0o7777 if os.path.exists(p) else 0o644))
    os.replace(tmp, p)


def save_copy(path, data, backups, key, tag):
    os.makedirs(backups, exist_ok=True)
    stamp = "" if tag == "conflict" else time.strftime("%Y%m%d-%H%M%S") + "-"      # one conflict file per key, rewritten on every run
    dst = os.path.join(backups, f"{stamp}{key.replace('/', '_')}.{tag}")
    with open(dst, "wb") as fh:
        fh.write(data)
    return dst


def merge3(live, base, kit):
    """(merged_bytes, conflicts). conflicts is None when the merge could not be tried (binary data, or git missing)."""
    if b"\0" in live + base + kit:
        return None, None
    with tempfile.TemporaryDirectory() as d:
        paths = []
        for name, data in (("live", live), ("base", base), ("kit", kit)):
            p = os.path.join(d, name)
            with open(p, "wb") as fh:
                fh.write(data)
            paths.append(p)
        try:
            r = subprocess.run(["git", "merge-file", "-p", "-L", "live", "-L", "installed-before", "-L", "kit", *paths], capture_output=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            return None, None
        if r.returncode < 0 or r.returncode > 127:
            return None, None
        return r.stdout, r.returncode


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["check", "install"])
    ap.add_argument("key")
    ap.add_argument("--kit", required=True)
    ap.add_argument("--live", required=True)
    ap.add_argument("--base", required=True)
    ap.add_argument("--backups", required=True)
    ap.add_argument("--label", default="")
    ap.add_argument("--mode", default="644")
    ap.add_argument("--kit-wins", action="store_true")
    ap.add_argument("--live-wins", action="store_true")
    ap.add_argument("--no-pull", action="store_true")
    a = ap.parse_args()
    mode = int(a.mode, 8)
    label = a.label or a.key
    chk = a.cmd == "check"
    K, L = rd(a.kit), rd(a.live)
    bpath = os.path.join(a.base, a.key)
    B = rd(bpath)

    def say(tag, msg):
        print(f"{tag:<9}{msg}")

    if K is None:                                            # nothing in the kit to install from
        say("MISSING", f"{label}: not in the kit ({a.kit})") if chk else None
        return 0
    if L is None:
        if chk:
            say("MISSING", f"{label}: {a.live} (install will write it)")
        else:
            wr(a.live, K, mode); wr(bpath, K, 0o644); say("wrote:", a.live)
        return 0
    if K == L:
        if not chk and B != K:
            wr(bpath, K, 0o644)
        say("ok", label) if chk else say("ok:", a.live)
        return 0

    # they differ
    def take_kit(why):
        bak = save_copy(a.live, L, a.backups, a.key, "bak")
        wr(a.live, K, mode); wr(bpath, K, 0o644); say("updated:", f"{a.live} ({why}; old copy: {bak})")

    def take_live(why):
        bak = save_copy(a.kit, K, a.backups, a.key, "kit-bak")
        wr(a.kit, L); wr(bpath, L, 0o644); say("pulled:", f"{a.live} into the kit file {a.kit} ({why}; kit's old copy: {bak})")

    if a.kit_wins:
        say("UPDATE", f"{label}: the kit's version replaces yours (--update)") if chk else take_kit("--update")
        return 0
    if a.live_wins:
        say("PULL", f"{label}: your live version goes into the kit (--pull)") if chk else take_live("--pull")
        return 0
    if B is None:
        say("UNKNOWN", f"{label}: differs and there is no history to tell which is newer; yours is kept. "
                       f"--update = the kit's wins, --pull = yours goes into the kit")
        return 0
    if L == B:
        say("UPDATE", f"{label}: the kit changed, yours did not (install updates it)") if chk else take_kit("the kit changed")
        return 0
    if K == B:
        if a.no_pull:
            say("LIVE NEWER" if chk else "kept:", f"{label}: yours changed, the kit did not; kept (--no-pull: not copied into the kit)")
        else:
            say("PULL", f"{label}: yours changed, the kit did not (install copies it into the kit)") if chk else take_live("yours is newer")
        return 0
    merged, conflicts = merge3(L, B, K)                       # both changed
    if merged is None:
        say("CONFLICT", f"{label}: both changed and it cannot be merged automatically; nothing changed. Use --update (kit wins) or --pull (yours wins)")
    elif conflicts:
        where = save_copy(a.live, merged, a.backups, a.key, "conflict") if not chk else "(not written in a check)"
        say("CONFLICT", f"{label}: both changed the same lines ({conflicts}); nothing changed. The merge with markers: {where}")
    elif chk:
        say("MERGE", f"{label}: both changed, different lines (install merges them into both)")
    else:
        bak = save_copy(a.live, L, a.backups, a.key, "bak")
        wr(a.live, merged, mode); wr(a.kit, merged); wr(bpath, merged, 0o644)
        say("merged:", f"{a.live} (both changed, different lines; merged into the live file and the kit; old copy: {bak})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
