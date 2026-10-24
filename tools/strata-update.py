#!/usr/bin/env python3
"""strata-update.py: move Strata to a newer release safely, and back again if it is worse. Not pinned to one version.

    python3 strata-update.py check [--to TAG]     READ-ONLY: what a newer release changes, and whether your kit patches still apply to it
    python3 strata-update.py apply --to TAG       back up, switch to TAG, re-apply the patches, rebuild the engine, test; undoes itself on any failure
    python3 strata-update.py rollback             go back to the version before the last update (engine, code and patches)
        [--strata DIR]      the Strata folder (default ~/github/strata-work/Strata)
        [--patches DIR]     the kit's patches (default: ../patches next to this file)
        [--no-fetch]        do not ask GitHub for new tags (check offline)
        [--yes]             do not ask for the typed confirmation
        [--force]           apply: set aside local changes the patches do not explain (they are saved in the backup first)
        [--skip-build] [--skip-tests]
        [--no-compat]       apply: do not offer the settings changes that keep your model decoding as before (see COMPAT below)

Why not `./update.sh` or `git pull`: this checkout carries three local patches (the Meteor Lake fix and the dashboard readings), upstream rewrote its
history once (2026-10-06), and a failed update should leave you where you were. This tool tests the patches against the new release in a scratch
copy first (nothing in your folder changes during `check`), and `apply` does the rest as one transaction.

What `apply` does, in order, stopping and undoing everything at the first problem:
  1. refuses if Strata is running or an agent session is; checks free disk; checks that your changes are exactly the kit's patches
  2. backs up to ~/strata-backups/update-FROM-to-TO-DATE/: every strata-*.json and run-*.sh, ~/start-swift.sh, a full diff of local changes,
     the engine binary, the commit you are on
  3. takes the patches off (newest first), switches to the new tag, puts them back (each tested first)
  4. rebuilds the engine in the build container and checks it reports the new version; runs the three quick kernel tests
  5. offers the settings changes a release needs to keep your decoding as it was (the COMPAT table: for 0.1.45, requests with no sampling settings,
     which is what Codex sends, stop being greedy and get the model card's temperature 1.0 unless the config says otherwise)
  6. reads `./setup.sh --check` (read-only)
Your configs are changed only by step 5, only with your say-so, and the originals are in the backup. After it succeeds you start Strata and run the acceptance checks in the
playbook; `rollback` undoes the update.
"""
import datetime, glob, json, os, re, shutil, subprocess, sys, tempfile, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_STRATA = os.path.expanduser("~/github/strata-work/Strata")
PATCHES = ["mtl-igpu-local.patch", "xe-monitor-v2.patch", "xe-power.patch", "xe-monitor-quiet.patch"]           # in the order they go on
NEEDS = {"xe-power.patch": "xe-monitor-v2.patch", "xe-monitor-quiet.patch": "xe-monitor-v2.patch"}         # a patch made on top of another one implies it
URL = os.environ.get("STRATA_URL", "http://127.0.0.1:8085")
BACKUPS = os.path.expanduser(os.environ.get("STRATA_BACKUPS", "~/strata-backups"))
WATCH = re.compile(r"sycl|intel|\bxe\b|arc |igpu|integrated|\buma\b|meteor|ttm|responses|codex|effort|thinking|reasoning|max_tokens|"
                   r"setup.*(config|re-?run|keep)|config|default|MIN_ENGINE|opt-in|opt out|crash|hang|fix", re.I)


class Stop(Exception):
    pass


# Changes in a release that alter behaviour for a config that says nothing about it. Each: the release it starts in, a test on the config,
# the setting that keeps the OLD behaviour, and why. `info` entries only inform. (Written from reading the releases' source and docs.)
COMPAT = [
    {"id": "model-card-sampling", "since": (0, 1, 45),
     "title": "a request with no sampling settings (Codex sends none) used to decode greedy; it now gets the model card's sampling "
              "(temperature 1.0, top_p 0.95 when thinking; 0.7 and presence_penalty 1.5 when not)",
     "applies": lambda cfg: "sampling" not in cfg,
     "fix": {"sampling": {"temperature": 0, "presence_penalty": 0}},
     "why": "keeps decoding exactly as before: greedy, no penalty. Sampling changes what the model writes and costs decode speed (about 16% was measured on this "
            "machine before); try it deliberately later, with an A/B, by removing this block."},
    {"id": "q6-planar", "since": (0, 1, 45), "info": True,
     "title": "STRATA_Q6_PLANAR is on by default: the Q6_K output head and the draft head are converted at load (bitwise equal output, faster Q6_K kernels). "
              "Not measured on Meteor Lake: if decode is slower than before, put STRATA_Q6_PLANAR=0 in the config's env and compare."},
    {"id": "answer-room", "since": (0, 1, 45), "info": True,
     "title": "max_tokens now counts the thinking; without a thinking budget the thinking is cut early to leave a quarter of the cap for the answer. "
              "Your configs set reasoning_budget_tokens, so this should not change anything (\"answer_room\": false turns it off)."},
    # ---- the 0.2 series (read from the release notes and the source at v0.2.3 on 2026-10-23; none of it has run on this machine) ----
    {"id": "fit-max-tokens", "since": (0, 2, 0),
     "title": "a request whose max_tokens is more than the room the prompt leaves in the context used to get HTTP 400; fit_max_tokens now defaults to true and shortens it",
     "applies": lambda cfg: "fit_max_tokens" not in cfg,
     "fix": {"fit_max_tokens": False},
     "why": "keeps the old answer (a 400) for a config that does not say. The settled config sets \"fit_max_tokens\": true already, so it is not touched; Codex normally benefits from the "
            "new default, so say no here unless you want errors instead of shortened answers."},
    {"id": "intel-defaults-not-for-this-gpu", "since": (0, 2, 0), "info": True,
     "title": "the new Intel prompt defaults (XMX grouped experts on Arc A-series, int8 experts on Battlemage) do not switch on for the Meteor Lake iGPU: the engine classifies a GPU by "
              "architecture, PCI id or name and this one is neither (read in sycl/include/strata/sycl_queue.hpp, prefill.cpp, xmx_moe.dp.cpp). Not run: after the update the start log "
              "should show no 'int8 prompt experts are on' line, and prompt speed should match the earlier ABBA numbers."},
    {"id": "workers-low-power-cores", "since": (0, 2, 1), "info": True,
     "title": "the engine and setup count the CPU's low-power E cores (cpu_lowpower) as efficiency cores, so the default worker count of a hybrid CPU such as the Core Ultra 5 125H may change "
              "(the release notes: a 255H went from 15 to 10). The settled config pins --pool-workers 8, which wins; a config without it will get a different count."},
    {"id": "speed-number", "since": (0, 2, 2), "info": True,
     "title": "'one speed number everywhere': tokens per second is now every token the model produced (thinking, answer, tool calls; with MTP the kept tokens) divided by decode time, in the chat "
              "footer, the monitor, the model API and the server console. A figure shown after the update is not comparable with one from 0.1.45; compare with strata-bench2.py on both."},
    {"id": "engine-package", "since": (0, 2, 3), "info": True,
     "title": "Intel Arc is now a released engine package that `strata launch` and `strata update` fetch. Do not use those stock commands on this machine: they would replace the engine you build "
              "from source and ignore the kit's patches. Keep using this tool (it builds in the strata-sycl-dev image)."},
    {"id": "engine-version", "since": (0, 2, 0), "info": True,
     "title": "the engine a 0.2.x tag builds reports its own number (v0.2.3 builds engine 0.1.48; MIN_ENGINE is 0.1.46). This tool reads it from the target's CMakeLists.txt and checks the build against it."},
]


def compat_for(repo, cur, target):
    """[(entry, [config names it applies to])] for a move from cur to target."""
    out = []
    for e in COMPAT:
        if vkey(cur) >= e["since"] or vkey(target) < e["since"]:
            continue
        if e.get("info"):
            out.append((e, []))
            continue
        names = []
        for f in sorted(glob.glob(os.path.join(repo, "strata-*.json"))):
            if any(x in f for x in (".bak", ".removed", ".tmp")):
                continue
            try:
                with open(f) as fh:
                    if e["applies"](json.load(fh)):
                        names.append(os.path.basename(f))
            except (OSError, ValueError):
                pass
        out.append((e, names))
    return out


def apply_compat(repo, bdir, state, yes, skip):
    todo = [(e, n) for e, n in compat_for(repo, state["from"], state["to"]) if n and not e.get("info")]
    if not todo or skip:
        return []
    changed = []
    for e, names in todo:
        say(f"\nSettings change: {e['title']}")
        say(f"  to keep the old behaviour: add {json.dumps(e['fix'])} to {', '.join(names)}")
        say(f"  {e['why']}")
        if not yes and input("  Add it? [Y/n] ").strip().lower().startswith("n"):
            say("  left as it is: you will get the new behaviour"); continue
        for n in names:
            path = os.path.join(repo, n)
            with open(path) as fh:
                cfg = json.load(fh)
            cfg.update(e["fix"])
            tmp = path + ".tmp"
            with open(tmp, "w") as fh:
                json.dump(cfg, fh, indent=2); fh.write("\n")
            with open(tmp) as fh:
                json.load(fh)
            os.replace(tmp, path)
            changed.append(n)
            say(f"  added to {n} (the original is in the backup)")
    return changed


def say(msg=""):
    print(msg, flush=True)


def run(cmd, cwd=None, check=True, env=None, input_text=None):
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, env=env, input=input_text)
    if check and r.returncode:
        raise Stop(f"{' '.join(cmd[:4])}...: {(r.stderr or r.stdout).strip()[:400]}")
    return r


def git(repo, *args, check=True):
    return run(["git", "-C", repo, *args], check=check).stdout


def vkey(tag):
    return tuple(int(x) for x in re.findall(r"\d+", tag))


def tags_of(repo):
    return sorted([t for t in git(repo, "tag").split() if re.fullmatch(r"v\d+(\.\d+)+", t)], key=vkey)


def current_version(repo):
    r = run(["git", "-C", repo, "describe", "--tags", "--exact-match", "HEAD"], check=False)
    if r.returncode == 0:
        return r.stdout.strip()
    near = run(["git", "-C", repo, "describe", "--tags", "--abbrev=0", "HEAD"], check=False).stdout.strip()
    return f"{near}+" if near else git(repo, "rev-parse", "--short", "HEAD").strip()


def engine_version(repo):
    p = os.path.join(repo, "build-sycl", "strata")
    try:
        with open(p, "rb") as fh:
            m = re.search(rb"engine=([0-9][0-9.]*)", fh.read())
        return m.group(1).decode().rstrip(".") if m else None
    except OSError:
        return None


def show(repo, ref, path):
    r = subprocess.run(["git", "-C", repo, "show", f"{ref}:{path}"], capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else ""


def engine_version_of(repo, ref):
    """The engine version a build of REF reports: `project(strata VERSION x)` in its CMakeLists.txt (v0.2.3 is engine 0.1.48), else the tag's own numbers."""
    m = re.search(r"project\(\s*strata\s+VERSION\s+([0-9][0-9.]*)", show(repo, ref, "CMakeLists.txt"), re.I)
    return m.group(1).rstrip(".") if m else ".".join(str(x) for x in vkey(ref))


def min_engine(repo, ref):
    m = re.search(r"^MIN_ENGINE = \(([0-9, ]+)\)", show(repo, ref, "setup.py"), re.M)
    return ".".join(x.strip() for x in m.group(1).split(",")) if m else None


# ---- the patches ---------------------------------------------------------------------------------------------------------------
def variants(pdir, name):
    """The kit's files for ONE patch: NAME.patch, then NAME.SERIES.patch for a newer Strata line (mtl-igpu-local.0.2.patch).
    The same change is remade when upstream rewrites the code around it; which file fits is decided by trying it on the tree."""
    stem = name[:-len(".patch")]
    first = os.path.join(pdir, name)
    return ([first] if os.path.isfile(first) else []) + sorted(glob.glob(os.path.join(pdir, stem + ".*.patch")))


def have_patch(pdir, name):
    return bool(variants(pdir, name))


def pick(repo, pdir, name, reverse=False):
    """(the variant of NAME that git can apply (or take off) on this tree now, the first error)."""
    err = ""
    for v in variants(pdir, name):
        r = run(["git", "-C", repo, "apply", *(["-R"] if reverse else []), "--check", v], check=False)
        if r.returncode == 0:
            return v, ""
        err = err or r.stderr.strip()
    return None, err


def all_files(pdir, name):
    """Files any variant of the patch touches."""
    return sorted({f for v in variants(pdir, name) for f in patch_files(v)[0]})


def patch_files(patch):
    """(files the patch touches, files it creates)."""
    touched, created, last = [], [], None
    with open(patch, errors="replace") as fh:
        for ln in fh:
            if ln.startswith("--- "):
                last = ln[4:].strip()
            elif ln.startswith("+++ b/"):
                f = ln[6:].strip()
                touched.append(f)
                if last == "/dev/null":
                    created.append(f)
    return touched, created


def applied_ratio(repo, patch):
    added, cur = {}, None
    with open(patch, errors="replace") as fh:
        for ln in fh:
            if ln.startswith("+++ b/"):
                cur = ln[6:].strip(); added.setdefault(cur, [])
            elif ln.startswith("+") and not ln.startswith("+++") and cur and len(ln[1:].strip()) >= 30:
                added[cur].append(ln[1:].strip())
    hit = tot = 0
    for f, lines in added.items():
        try:
            with open(os.path.join(repo, f), errors="replace") as fh:
                have = {x.strip() for x in fh}
        except OSError:
            have = set()
        tot += len(lines); hit += sum(1 for t in lines if t in have)
    return hit, tot


def applied_patches(repo, pdir):
    """Which kit patches are on this checkout (the power and quiet patches are made on top of the dashboard patch, so each implies it)."""
    names = [n for n in PATCHES if have_patch(pdir, n)]
    on = []
    for n in names:
        if any((lambda ht: ht[1] and ht[0] * 100 // ht[1] >= 80)(applied_ratio(repo, v)) for v in variants(pdir, n)):
            on.append(n)
    for n, need in NEEDS.items():                                    # the later patch rewrites lines the earlier one added, so the earlier one may look partly applied
        if n in on and need not in on and need in names:
            on.append(need)
    return [n for n in names if n in on]


def apply_patches(repo, pdir, names):
    done = []
    for n in names:
        p, err = pick(repo, pdir, n)
        if not p:
            raise Stop(f"{n} does not apply to this version: {err[:300]}")
        git(repo, "apply", p)
        done.append(n)
    return done


def reverse_patches(repo, pdir, names, force):
    """Take the patches off, newest first. If the files carry changes beyond the patches, stop (or, with force, set them aside: the diff is saved)."""
    for n in reversed(names):
        p, err = pick(repo, pdir, n, reverse=True)
        if p:
            git(repo, "apply", "-R", p)
            continue
        if not force:
            raise Stop(f"your files have changes the kit's {n} does not explain ({err[:200]}). "
                       f"The full diff is in the backup; use --force to set them aside, or look at them first:  git -C {repo} diff")
        touched, created = set(), set()
        for v in variants(pdir, n):
            t, c = patch_files(v); touched.update(t); created.update(c)
        for f in sorted(touched):
            fp = os.path.join(repo, f)
            if f in created:
                if os.path.exists(fp):
                    os.unlink(fp)
            else:
                git(repo, "checkout", "--", f)


# ---- state of the machine ------------------------------------------------------------------------------------------------------------
def strata_running():
    try:
        with urllib.request.urlopen(f"{URL}/v1/models", timeout=3) as r:
            return r.status == 200
    except Exception:  # noqa: BLE001
        pass
    for cmdline in glob.glob("/proc/[0-9]*/cmdline"):
        try:
            with open(cmdline, "rb") as fh:
                text = fh.read().replace(b"\0", b" ").decode("utf8", "replace")
        except OSError:
            continue
        if "server.py" in text and "--config" in text and "strata" in text:
            return True
    return False


def agent_running():
    if os.environ.get("STRATA_UPDATE_SKIP_DOCKER") == "1":
        return False
    r = run(["docker", "ps", "--format", "{{.Names}}"], check=False)
    return r.returncode == 0 and any(n.startswith(("strata-agent", "strata-check")) for n in r.stdout.split())


# ---- check -------------------------------------------------------------------------------------------------------------------------------
def scratch_worktree(repo, ref):
    d = tempfile.mkdtemp(prefix="strata-check-")
    run(["git", "-C", repo, "worktree", "add", "-q", "--detach", d, ref])
    return d


def drop_worktree(repo, d):
    run(["git", "-C", repo, "worktree", "remove", "--force", d], check=False)
    shutil.rmtree(d, ignore_errors=True)


def cmd_check(repo, pdir, to, fetch):
    if fetch:
        r = run(["git", "-C", repo, "fetch", "-q", "--tags", "origin"], check=False)
        if r.returncode:
            say(f"(could not reach GitHub: {r.stderr.strip()[:120]}; using the tags already here)")
    cur, eng = current_version(repo), engine_version(repo)
    tags = tags_of(repo)
    target = to or (tags[-1] if tags else None)
    say(f"Strata folder:      {repo}")
    say(f"Installed:          {cur}   (engine binary reports: {eng or 'no build-sycl/strata found'})")
    say(f"Newest release:     {tags[-1] if tags else 'none known'}")
    if not target or target not in tags:
        say(f"Target {target!r} is not a known release tag. Known recent: {', '.join(tags[-5:])}")
        return 2
    base = cur.rstrip("+")
    if base == target and not cur.endswith("+"):
        say(f"\nAlready at {target}.")
        return 0
    n = len(git(repo, "log", "--oneline", f"{base}..{target}").splitlines()) if base in tags else "?"
    say(f"\nTarget:             {target}   ({n} commits after {base})")
    on = applied_patches(repo, pdir)
    say(f"Kit patches on your checkout now: {', '.join(on) or 'none'}")
    # 1. do the patches apply to the target? (in a scratch copy; your folder is not touched)
    wt = scratch_worktree(repo, target)
    ok_all = True
    try:
        say("\nDo the kit's patches apply to the new release?")
        for name in [x for x in PATCHES if have_patch(pdir, x)]:
            p, err = pick(wt, pdir, name)
            if p:
                git(wt, "apply", p); say(f"  {name:24s} applies cleanly" + (f" (as {os.path.basename(p)})" if os.path.basename(p) != name else ""))
            else:
                ok_all = False; say(f"  {name:24s} DOES NOT APPLY: {err.splitlines()[0][:110] if err else '?'}")
                say("      (the Strata files it patches changed upstream; the patch needs a new version first. Do not update until it is fixed.)")
    finally:
        drop_worktree(repo, wt)
    # 2. what changed in the files the patches carry
    changed = set(git(repo, "diff", "--name-only", base, target).split()) if base in tags else set()
    touched = {f for name in PATCHES for f in all_files(pdir, name)}
    both = sorted(changed & touched)
    say("\nPatched files that upstream also changed: " + (", ".join(both) if both else "none"))
    me_now, me_new = min_engine(repo, base) if base in tags else None, min_engine(repo, target)
    say(f"Minimum engine version: {me_now} -> {me_new}" + ("   (the engine must be rebuilt, which `apply` does)" if me_now != me_new else ""))
    # 3. what is in it, filtered for things that can matter here
    subjects = git(repo, "log", "--format=%h %s", f"{base}..{target}").splitlines() if base in tags else []
    sy = [s for s in subjects if re.search(r"sycl|intel|\bxe\b|arc |igpu|\buma\b", s, re.I)]
    sv = [s for s in subjects if re.search(r"serve|config|responses|codex|effort|thinking|reasoning|max_tokens|setup", s, re.I) and s not in sy]
    say(f"\nCommits that mention SYCL / Intel ({len(sy)} of {len(subjects)}):")
    for s in sy[:14]:
        say("  " + s[:150])
    say(f"\nServer, setup and config commits ({len(sv)}):")
    for s in sv[:14]:
        say("  " + s[:150])
    comp = compat_for(repo, base, target) if base in tags else []
    if comp:
        say("\nBehaviour changes that matter for your configs (known for this release):")
        for e, names in comp:
            say(f"  {'info' if e.get('info') else 'ACTION' if names else 'no action needed'}: {e['title']}")
            if names:
                say(f"      applies to: {', '.join(names)}.  `apply` offers to add {json.dumps(e['fix'])}")
    say("\nBefore you decide: read those lists for anything that changes a default on Intel (a kernel, a memory setting, how thinking tokens are counted).")
    say("`apply` backs up first and undoes itself on any failure; `rollback` undoes it afterwards. After it, start Strata and run the playbook's acceptance checks.")
    say("\nVERDICT: " + (f"ready: all patches apply to {target}." if ok_all else "NOT READY: a patch does not apply."))
    return 0 if ok_all else 1


# ---- apply / rollback -----------------------------------------------------------------------------------------------------------------------
def backup_dir_for(frm, to):
    return os.path.join(BACKUPS, f"update-{frm.rstrip('+')}-to-{to}-{datetime.datetime.now():%Y%m%d-%H%M%S}")


def kit_patch_files(pdir):
    """Every file any kit patch touches (the patches may be on, off, or replaced by a hand edit)."""
    return sorted({f for n in PATCHES for f in all_files(pdir, n)})


def snapshot_patched_files(repo, bdir, pdir):
    """Copy the files the patches touch exactly as they are now (hand edits included), so an undo puts back those bytes and not just 'the patches'."""
    snap, gone = os.path.join(bdir, "patched-files"), []
    for f in kit_patch_files(pdir):
        src = os.path.join(repo, f)
        if os.path.isfile(src):
            os.makedirs(os.path.dirname(os.path.join(snap, f)), exist_ok=True)
            shutil.copy2(src, os.path.join(snap, f))
        else:
            gone.append(f)                                       # a file a patch creates and that is not there yet
    with open(os.path.join(bdir, "patched-files.json"), "w") as fh:
        json.dump({"absent": gone}, fh)


def restore_patched_files(repo, bdir):
    """The undo's last step: the patched files go back byte for byte. Older backups have no snapshot; nothing is done for them."""
    try:
        with open(os.path.join(bdir, "patched-files.json")) as fh:
            absent = json.load(fh)["absent"]
    except (OSError, ValueError, KeyError):
        return
    snap = os.path.join(bdir, "patched-files")
    for root, _, files in os.walk(snap):
        for name in files:
            src = os.path.join(root, name)
            dst = os.path.join(repo, os.path.relpath(src, snap))
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)
    for f in absent:
        if os.path.isfile(os.path.join(repo, f)):
            os.unlink(os.path.join(repo, f))


def save_backup(repo, bdir, frm, on, pdir=None):
    os.makedirs(bdir, exist_ok=True)
    if pdir:
        snapshot_patched_files(repo, bdir, pdir)
    for pat in ("strata-*.json", "run-*.sh"):
        for f in glob.glob(os.path.join(repo, pat)):
            shutil.copy2(f, bdir)
    sw = os.path.expanduser("~/start-swift.sh")
    if os.path.isfile(sw):
        shutil.copy2(sw, bdir)
    with open(os.path.join(bdir, "local-changes.diff"), "w") as fh:
        fh.write(git(repo, "diff"))
    eng = os.path.join(repo, "build-sycl", "strata")
    if os.path.isfile(eng):
        shutil.copy2(eng, os.path.join(bdir, f"strata-engine-{engine_version(repo) or 'unknown'}"))
    branch = run(["git", "-C", repo, "symbolic-ref", "-q", "--short", "HEAD"], check=False).stdout.strip()
    state = {"from": frm, "sha": git(repo, "rev-parse", "HEAD").strip(), "branch": branch, "patches": on, "engine": engine_version(repo), "status": "backed up"}
    save_state(bdir, state)
    return state


def save_state(bdir, state):
    with open(os.path.join(bdir, "state.json"), "w") as fh:
        json.dump(state, fh, indent=1)


def build_engine(repo):
    cmd = os.environ.get("STRATA_UPDATE_BUILD_CMD")
    if cmd:
        r = subprocess.run(["bash", "-c", cmd], cwd=repo, capture_output=True, text=True)
    else:
        name = os.path.basename(repo)
        os.utime(os.path.join(repo, "sycl", "CMakeLists.txt"), None)               # forces the version to be reconfigured
        r = subprocess.run(["docker", "run", "--rm", "-u", f"{os.getuid()}:{os.getgid()}", "-v", f"{os.path.dirname(repo)}:/work", "-e", f"REPO=/work/{name}",
                            "-e", f"BUILD_DIR=/work/{name}/build-sycl", "strata-sycl-dev", f"cd /work/{name} && bash sycl/tools/build.sh strata"], capture_output=True, text=True)
    out = r.stdout + r.stderr
    tail = "\n".join(out.strip().splitlines()[-6:])
    # sycl/tools/build.sh (0.1.x) ends in `grep ... | head` under `set -o pipefail`: it exits 1 after a build that printed BUILD EXIT 0 and errors: 0
    printed_ok = bool(re.search(r"^BUILD EXIT 0\s*$", out, re.M)) and bool(re.search(r"^errors: 0\s*$", out, re.M))
    if r.returncode and not printed_ok:
        raise Stop(f"the engine build failed:\n{tail}")
    return tail


def kernel_tests(repo):
    if os.environ.get("STRATA_UPDATE_BUILD_CMD"):
        return "skipped (test build hook)"
    name, rgid = os.path.basename(repo), run(["getent", "group", "render"], check=False).stdout.split(":")[2:3]
    script = "cd /work/%s && for t in quantize_act s2_gemv sampler; do echo \"== $t\"; timeout 300 build-sycl/${t}_parity --selftest 2>&1 | tail -2; echo exit=${PIPESTATUS[0]}; done" % name
    argv = ["docker", "run", "--rm", "--device", "/dev/dri", *(["--group-add", rgid[0]] if rgid else []), "-u", f"{os.getuid()}:{os.getgid()}",
            "-v", f"{os.path.dirname(repo)}:/work", "strata-sycl-dev", script]
    r = subprocess.run(argv, capture_output=True, text=True)
    out = r.stdout + r.stderr
    if out.count("exit=0") != 3:
        raise Stop("the quick kernel tests did not all pass:\n" + out[-600:])
    return "3 of 3 quick kernel tests pass"


def checkout_back(repo, state, pdir):
    """Return to the recorded commit with the recorded patches on."""
    target = state["branch"] or state["sha"]
    git(repo, "checkout", "-q", target)
    if git(repo, "rev-parse", "HEAD").strip() != state["sha"]:
        git(repo, "checkout", "-q", "--detach", state["sha"])
    apply_patches(repo, pdir, state["patches"])


def cmd_apply(repo, pdir, to, yes, force, skip_build, skip_tests, no_compat=False):
    if not to:
        say("which release? e.g.  apply --to v0.1.45   (check shows the newest)"); return 2
    if not os.path.isdir(os.path.join(repo, ".git")):
        say(f"{repo} is not a git checkout"); return 2
    tags = tags_of(repo)
    if to not in tags:
        say(f"{to} is not a release tag here. Run `check` first (it fetches), or: git -C {repo} fetch --tags origin"); return 2
    if strata_running():
        say("Strata is running. Stop it first (codex stop), then run this again."); return 2
    if agent_running():
        say("A Codex session or loop is running. End it first."); return 2
    free = shutil.disk_usage(repo).free
    if free < 5 * 1000 ** 3:
        say(f"only {free / 1e9:.1f} GB free; need about 5 GB for the build and the backup"); return 2
    frm, on = current_version(repo), None
    on = applied_patches(repo, pdir)
    mod = [ln[3:] for ln in git(repo, "status", "--porcelain", "--untracked-files=no").splitlines()]
    allowed = {f for n in on for f in all_files(pdir, n)}
    stray = [f for f in mod if f not in allowed]
    if stray and not force:
        say("Changes in your checkout that the kit's patches do not explain:\n  " + "\n  ".join(stray[:10]) +
            "\nLook at them (git diff), or use --force to set them aside (the full diff is saved in the backup first)."); return 2
    say(f"\nUpdate {frm} -> {to}")
    say(f"  patches now on: {', '.join(on) or 'none'}")
    say(f"  steps: back up, take patches off, switch to {to}, put them back, " + ("" if skip_build else "rebuild the engine, ") + ("" if skip_tests else "run the quick kernel tests, ") + "read setup --check")
    if not yes and input(f"Type the version ({to}) to go ahead: ").strip() != to:
        say("Not confirmed. Nothing was changed."); return 1
    bdir = backup_dir_for(frm, to)
    state = save_backup(repo, bdir, frm, on, pdir)
    state["to"] = to
    save_state(bdir, state)
    say(f"\n1. backed up to {bdir}")
    try:
        for f in (stray if force else []):                       # set aside: the full diff is in the backup already
            git(repo, "checkout", "--", f)
        reverse_patches(repo, pdir, on, force)
        git(repo, "checkout", "-q", "--detach", to)
        say(f"2. switched to {to}")
        apply_patches(repo, pdir, on)
        say(f"3. patches back on: {', '.join(on) or 'none'}")
        state["status"] = "switched"; save_state(bdir, state)
        if not skip_build:
            tail = build_engine(repo)
            ev = engine_version(repo)
            want = engine_version_of(repo, to)
            if ev != want:
                raise Stop(f"the engine built reports {ev!r}, expected {want!r}")
            say(f"4. engine rebuilt: reports {ev}")
        if not skip_tests:
            say("5. " + kernel_tests(repo))
    except Stop as e:
        say(f"\nSTOPPED: {e}\nUndoing the update ...")
        try:
            undo(repo, pdir, state, bdir, force=True)
            say("Back where you were. Nothing of the update remains (the backup folder is kept).")
        except Stop as e2:
            say(f"Could not undo cleanly: {e2}\nEverything needed is in {bdir} (state.json, local-changes.diff, the engine binary).")
        return 1
    state["configs_changed"] = apply_compat(repo, bdir, state, yes, no_compat)
    save_state(bdir, state)
    r = run(["bash", "./setup.sh", "--low-ram", "auto", "--backend", "sycl", "--check"], cwd=repo, check=False) if os.path.isfile(os.path.join(repo, "setup.sh")) and not os.environ.get("STRATA_UPDATE_BUILD_CMD") else None
    if r is not None:
        say("6. setup --check: " + ("passes" if r.returncode == 0 else f"reported a problem:\n{(r.stdout + r.stderr)[-500:]}"))
    state["status"] = "updated"; save_state(bdir, state)
    with open(os.path.join(BACKUPS, ".last-update"), "w") as fh:
        fh.write(bdir)
    for e, _ in compat_for(repo, frm, to):
        if e.get("info"):
            say(f"\nNote: {e['title']}")
    say(f"\nDone: Strata {to}. Now:")
    say("  codex           (starts Strata; first start takes a few minutes), then the acceptance checks in the playbook (decode, prompt speed, cache all-resident, memory)")
    say(f"  to go back:     python3 {os.path.basename(sys.argv[0])} rollback")
    return 0


def undo(repo, pdir, state, bdir, force):
    cur_on = applied_patches(repo, pdir)
    reverse_patches(repo, pdir, cur_on, force)
    checkout_back(repo, state, pdir)
    restore_patched_files(repo, bdir)
    eng = os.path.join(bdir, f"strata-engine-{state.get('engine') or 'unknown'}")
    if os.path.isfile(eng):
        os.makedirs(os.path.join(repo, "build-sycl"), exist_ok=True)
        shutil.copy2(eng, os.path.join(repo, "build-sycl", "strata"))
    for n in state.get("configs_changed", []):                 # settings the update added go back to what they were
        src = os.path.join(bdir, n)
        if os.path.isfile(src):
            shutil.copy2(src, os.path.join(repo, n))
    state["status"] = "rolled back"; save_state(bdir, state)


def cmd_rollback(repo, pdir, frm, yes, force):
    bdir = frm
    if not bdir:
        try:
            with open(os.path.join(BACKUPS, ".last-update")) as fh:
                bdir = fh.read().strip()
        except OSError:
            cands = sorted(glob.glob(os.path.join(BACKUPS, "update-*", "state.json")))
            bdir = os.path.dirname(cands[-1]) if cands else None
    if not bdir or not os.path.isfile(os.path.join(bdir, "state.json")):
        say("no update backup found (looked in " + BACKUPS + ")"); return 2
    with open(os.path.join(bdir, "state.json")) as fh:
        state = json.load(fh)
    if state.get("status") == "rolled back":
        say(f"{bdir} was already rolled back"); return 0
    if strata_running():
        say("Strata is running. Stop it first (codex stop)."); return 2
    say(f"Rolling back to {state['from']} ({state['sha'][:8]}) with patches {', '.join(state['patches']) or 'none'}; engine {state.get('engine')}")
    if not yes and input(f"Type the version ({state['from']}) to go ahead: ").strip() != state["from"].rstrip("+"):
        say("Not confirmed. Nothing was changed."); return 1
    try:
        undo(repo, pdir, state, bdir, force)
    except Stop as e:
        say(f"STOPPED: {e}"); return 1
    say(f"Done: {current_version(repo)}, engine {engine_version(repo)}. Configs were not changed; the old copies are in {bdir} if you need them.")
    return 0


def main():
    argv = sys.argv[1:]
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__); return 0
    action, rest = argv[0], argv[1:]
    opts = {"--strata": DEFAULT_STRATA, "--patches": os.path.join(HERE, "..", "patches"), "--to": None, "--from": None}
    flags = {"--no-fetch": False, "--yes": False, "--force": False, "--skip-build": False, "--skip-tests": False, "--no-compat": False}
    i = 0
    while i < len(rest):
        if rest[i] in opts and i + 1 < len(rest):
            opts[rest[i]] = rest[i + 1]; i += 2
        elif rest[i] in flags:
            flags[rest[i]] = True; i += 1
        else:
            print(f"unknown argument {rest[i]!r} (--help)"); return 2
    repo = os.path.abspath(os.path.expanduser(opts["--strata"]))
    pdir = os.path.abspath(os.path.expanduser(opts["--patches"]))
    if not os.path.isdir(os.path.join(repo, ".git")):
        print(f"{repo} is not a git checkout of Strata (use --strata DIR)"); return 2
    try:
        if action == "check":
            return cmd_check(repo, pdir, opts["--to"], not flags["--no-fetch"])
        if action == "apply":
            return cmd_apply(repo, pdir, opts["--to"], flags["--yes"], flags["--force"], flags["--skip-build"], flags["--skip-tests"], flags["--no-compat"])
        if action == "rollback":
            return cmd_rollback(repo, pdir, opts["--from"], flags["--yes"], flags["--force"])
    except Stop as e:
        print(f"stopped: {e}"); return 1
    print(f"unknown action {action!r}; one of: check apply rollback"); return 2


if __name__ == "__main__":
    sys.exit(main())
