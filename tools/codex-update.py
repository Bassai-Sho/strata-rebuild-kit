#!/usr/bin/env python3
"""codex-update.py: move the Codex in the `codex-sandbox` image to a newer release and back. Not pinned to one version.

    python3 codex-update.py check [--to VERSION]     READ-ONLY: the release notes, a download, and the full compatibility probe suite
    python3 codex-update.py apply [--to VERSION]     the same checks, then a new image layer carrying the verified binary (the old image is kept)
    python3 codex-update.py rollback                 back to the previous image (and the catalog it used)
        [--home DIR]      the Codex home (default ~/agent/codex-home)
        [--strata DIR]    the Strata checkout, so its own request parser is tested too (default ~/github/strata-work/Strata)
        [--yes]           do not ask for the typed confirmation
        [--force]         apply even if a probe fails (not recommended)
        [--no-baseline]   check: do not also run the probes on the Codex that is installed now

VERSION is a stable release such as 0.163.0 (the newest stable one if you leave it out). Pre-releases (alpha, beta, rc) are never chosen for you (only if you ask for one with --to).

Why a tool and not "latest": the kit depends on details of how Codex behaves, and Codex changes quickly (about weekly). The probe suite runs the
REAL new binary, never installing it, against a stand-in for Strata and a copy of your real Codex home, and checks every thing the kit relies on:
your config and model catalog are accepted, the tools and prompt size, your AGENTS.md rules, tool calls, the exact `codex loop` command line, the
full-screen interface (without the background server that fails with the static binary), the extra title request the title filter must still
recognise, the reasoning shortcuts, `codex --auto`, and (with the Strata checkout) Strata's own Responses parser on the requests it sends.

`apply` adds one thin layer to the image (the new binary, about 110 MB) and tags the old image codex-sandbox:codex-OLDVERSION, so `rollback` is
instant and the browser layer and tools in the image are untouched. If the catalog (which carries Codex's base instructions) differs for the new
version, it is regenerated and the old one is kept in the backup. Running sessions are not interrupted: refuse while one runs.
"""
import datetime, hashlib, html, json, os, re, shutil, subprocess, sys, tarfile, tempfile, urllib.error, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
BASE = os.environ.get("CODEX_UPDATE_BASE_URL", "https://github.com/openai/codex/releases")
ASSET = "codex-x86_64-unknown-linux-musl.tar.gz"
IMAGE = os.environ.get("CODEX_IMAGE", "codex-sandbox")
CACHE = os.path.expanduser(os.environ.get("CODEX_UPDATE_CACHE", "~/.cache/strata-kit/codex"))
BACKUPS = os.path.expanduser(os.environ.get("STRATA_BACKUPS", "~/strata-backups"))
NOTE_WORDS = re.compile(r"daemon|keymap|shortcut|leader|paste|sandbox|shell|config|migrat|deprecat|retire|remov|breaking|exec\b|title|goal|compact|reasoning|"
                        r"effort|catalog|provider|responses|proxy|network|AGENTS|skill|plugin|worktree|approval|permission|steer|session|resume|model", re.I)


class Stop(Exception):
    pass


def say(msg=""):
    print(msg, flush=True)


def read_text(path):
    with open(path) as fh:
        return fh.read().strip()


def read_json(path):
    with open(path) as fh:
        return json.load(fh)


def write_json(path, obj):
    with open(path, "w") as fh:
        json.dump(obj, fh, indent=1)


def write_text(path, text):
    with open(path, "w") as fh:
        fh.write(text)


def vkey(v):
    return tuple(int(x) for x in re.findall(r"\d+", v))


def run(cmd, check=True, **kw):
    r = subprocess.run(cmd, capture_output=True, text=True, **kw)
    if check and r.returncode:
        raise Stop(f"{' '.join(cmd[:4])}...: {(r.stderr or r.stdout).strip()[:300]}")
    return r


# ---- releases ----------------------------------------------------------------------------------------------------------------------------
class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def latest_stable():
    """The newest stable release: GitHub's /releases/latest redirect (pre-releases are not 'latest')."""
    override = os.environ.get("CODEX_UPDATE_LATEST")
    if override:
        return override
    opener = urllib.request.build_opener(NoRedirect)
    try:
        opener.open(urllib.request.Request(f"{BASE}/latest", method="HEAD"), timeout=30)
    except urllib.error.HTTPError as e:
        loc = e.headers.get("Location", "")
        m = re.search(r"rust-v(\d+(?:\.\d+)+)$", loc)
        if m:
            return m.group(1)
    except OSError as e:
        raise Stop(f"cannot reach GitHub to find the newest release: {e}")
    raise Stop("could not read the newest release from GitHub; give a version with --to")


def release_notes(version):
    try:
        with urllib.request.urlopen(f"{BASE}/tag/rust-v{version}", timeout=30) as r:
            page = r.read().decode("utf8", "replace")
    except OSError:
        return []
    m = re.search(r'data-test-selector="body-content"[^>]*>(.*?)</div>\s*</div>', page, re.S) or re.search(r'class="markdown-body[^"]*"[^>]*>(.*?)</div>', page, re.S)
    if not m:
        return []
    t = re.sub(r"<li[^>]*>", "\n- ", m.group(1)); t = re.sub(r"<h\d[^>]*>", "\n## ", t); t = re.sub(r"<[^>]+>", "", t)
    return [ln.strip() for ln in html.unescape(t).splitlines() if ln.strip()]


def download(version):
    """The release binary, cached under ~/.cache/strata-kit/codex/VERSION/ (never installed)."""
    d = os.path.join(CACHE, version)
    binary = os.path.join(d, "codex")
    if os.path.isfile(binary):
        return binary, read_text(os.path.join(d, "SHA256")) if os.path.exists(os.path.join(d, "SHA256")) else ""
    os.makedirs(d, exist_ok=True)
    tgz = os.path.join(d, ASSET)
    say(f"  downloading Codex {version} (about 110 MB) ...")
    try:
        urllib.request.urlretrieve(f"{BASE}/download/rust-v{version}/{ASSET}", tgz)
    except OSError as e:
        shutil.rmtree(d, ignore_errors=True)
        raise Stop(f"download failed: {e}")
    with open(tgz, "rb") as fh:
        sha = hashlib.sha256(fh.read()).hexdigest()
    with tarfile.open(tgz) as tf:
        member = next((m for m in tf.getmembers() if m.isfile() and os.path.basename(m.name).startswith("codex")), None)
        if member is None:
            raise Stop("the archive holds no codex binary")
        member.name = "codex"
        tf.extract(member, d)
    os.chmod(binary, 0o755)
    os.unlink(tgz)
    write_text(os.path.join(d, "SHA256"), sha)
    return binary, sha


# ---- the image ----------------------------------------------------------------------------------------------------------------------------
def docker_ok():
    return shutil.which("docker") is not None and run(["docker", "image", "inspect", IMAGE], check=False).returncode == 0


def image_version():
    r = run(["docker", "run", "--rm", IMAGE, "codex", "--version"], check=False)
    m = re.search(r"(\d+\.\d+\.\d+(?:[-.\w]*)?)", r.stdout)
    return m.group(1) if m else None


def image_binary(dest):
    r = subprocess.run(["docker", "run", "--rm", IMAGE, "cat", "/usr/local/bin/codex"], capture_output=True)
    if r.returncode or not r.stdout:
        raise Stop("could not copy the Codex binary out of the image")
    with open(dest, "wb") as fh:
        fh.write(r.stdout)
    os.chmod(dest, 0o755)
    return dest


def session_running():
    r = run(["docker", "ps", "--format", "{{.Names}}"], check=False)
    return [n for n in r.stdout.split() if n.startswith(("strata-agent", "strata-check", "strata-gate"))]


def has_browser():
    return run(["docker", "run", "--rm", IMAGE, "sh", "-c", "command -v browser-check"], check=False).returncode == 0


# ---- the probes ----------------------------------------------------------------------------------------------------------------------------
def run_probes(binary, home, catalog, strata, expect, label):
    if os.environ.get("CODEX_UPDATE_PROBE_RESULT"):               # test hook: a stated outcome instead of running a real Codex
        import codex_compat as cc
        res = cc.Result("probes")
        (res.fail("injected failure") if os.environ["CODEX_UPDATE_PROBE_RESULT"] == "fail" else res.ok("injected pass"))
        return expect, [res]
    import codex_compat as cc
    say(f"\nProbes on {label} ({binary}):")
    ctx, results = cc.run_all(binary, home, catalog, strata, expect, quick=False, say=lambda *_: None)
    for r in results:
        say(f"  {'ok  ' if r.status == 'pass' else r.status.upper().ljust(4)} {r.name:13s} {r.detail[:200]}")
    return ctx.version, results


def make_catalog(binary, out):
    r = run([sys.executable, os.path.join(HERE, "..", "agent", "make-model-catalog.py"), "--codex", binary, "--out", out], check=False)
    if r.returncode:
        raise Stop("could not generate the model catalog with the new binary: " + (r.stderr or r.stdout)[-300:])


def same_file(a, b):
    try:
        with open(a, "rb") as fa, open(b, "rb") as fb:
            return fa.read() == fb.read()
    except OSError:
        return False


# ---- check --------------------------------------------------------------------------------------------------------------------------------
def cmd_check(home, strata, to, baseline, quiet=False):
    cur = image_version() if docker_ok() else None
    newest = latest_stable()
    target = to or newest
    say(f"Codex in the image:   {cur or 'unknown (no docker or no image)'}")
    say(f"Newest stable:        {newest}")
    say(f"Target:               {target}")
    if cur and cur == target:
        say("\nThe image already has that version.")
        return 0, target, None
    notes = release_notes(target)
    if notes:
        say(f"\nRelease notes for {target} that may touch this kit:")
        picks = [n for n in notes if n.startswith("- ") and not re.match(r"- #\d+", n) and NOTE_WORDS.search(n)][:14]
        for n in picks:
            say("  " + re.sub(r"\s*\(#\d+(, #\d+)*\)\s*$", "", n)[:190])
        if not picks:
            say("  (nothing in the headline notes matched; read them: " + f"{BASE}/tag/rust-v{target})")
    binary, sha = download(target)
    say(f"  binary: {binary}  sha256 {sha[:16]}")
    tmp = tempfile.mkdtemp(prefix="codex-update-")
    try:
        results_base = []
        if baseline and cur and docker_ok() and not os.environ.get("CODEX_UPDATE_PROBE_RESULT"):
            old = image_binary(os.path.join(tmp, "codex-installed"))
            _, results_base = run_probes(old, home, None, strata, cur, f"the installed Codex {cur}")
        catalog = os.path.join(tmp, "model-catalog.json")
        installed_cat = os.path.join(home, "model-catalog.json")
        if os.environ.get("CODEX_UPDATE_FAKE_CATALOG"):                                   # test hooks
            shutil.copy(os.environ["CODEX_UPDATE_FAKE_CATALOG"], catalog)
        elif os.environ.get("CODEX_UPDATE_PROBE_RESULT"):
            if os.path.exists(installed_cat):
                shutil.copy(installed_cat, catalog)
        else:
            make_catalog(binary, catalog)
        cat_changed = os.path.exists(catalog) and not same_file(catalog, installed_cat)
        if cat_changed:
            say("\nThe model catalog (it carries Codex's base instructions) differs for the new version: `apply` regenerates it.")
        elif os.path.exists(catalog):
            say("\nThe model catalog is identical for the new version: nothing to regenerate.")
        _, results = run_probes(binary, home, catalog if cat_changed else None, strata, target, f"the new Codex {target}")
        fails = [r for r in results if r.status == "fail"]
        base_fails = {r.name for r in results_base if r.status == "fail"}
        say("\nVERDICT: " + (f"ready: every probe passes on Codex {target}." if not fails else
            f"NOT READY: {', '.join(r.name for r in fails)} failed on Codex {target}" + (f" ({', '.join(sorted(base_fails))} also fail on the installed version)" if base_fails else "") + "."))
        warns = [r for r in results if r.status == "warn"]
        if warns and not fails:
            say("  with warnings: " + "; ".join(f"{r.name}: {r.detail[:120]}" for r in warns))
        return (0 if not fails else 1), target, (binary, sha, catalog if cat_changed else None, tmp)
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        raise


# ---- apply / rollback ------------------------------------------------------------------------------------------------------------------
def cmd_apply(home, strata, to, yes, force):
    if not docker_ok():
        raise Stop("docker or the image codex-sandbox is missing")
    busy = session_running()
    if busy:
        raise Stop(f"a Codex session or check is running ({', '.join(busy)}). End it first.")
    rc, target, pack = cmd_check(home, strata, to, baseline=False)
    if pack is None:
        return 0
    binary, sha, catalog, tmp = pack
    try:
        if rc and not force:
            say("\nNot applying: a probe failed. Fix the cause, or use --force if you accept the risk.")
            return 1
        cur = image_version()
        say(f"\nUpdate Codex {cur} -> {target}: a new image layer; the old image stays as {IMAGE}:codex-{cur}; the browser layer and tools are untouched.")
        if not yes and input(f"Type the version ({target}) to go ahead: ").strip() != target:
            say("Not confirmed. Nothing was changed.")
            return 1
        bdir = os.path.join(BACKUPS, f"codex-update-{cur}-to-{target}-{datetime.datetime.now():%Y%m%d-%H%M%S}")
        os.makedirs(bdir, exist_ok=True)
        for f in ("config.toml", "model-catalog.json", "AGENTS.md"):
            if os.path.isfile(os.path.join(home, f)):
                shutil.copy2(os.path.join(home, f), bdir)
        state = {"from": cur, "to": target, "image_tag": f"{IMAGE}:codex-{cur}", "catalog_changed": bool(catalog), "browser": has_browser(), "status": "backed up"}
        write_json(os.path.join(bdir, "state.json"), state)
        run(["docker", "tag", IMAGE, f"{IMAGE}:codex-{cur}"])
        say(f"1. kept the current image as {IMAGE}:codex-{cur}; backup in {bdir}")
        ctxdir = os.path.join(tmp, "ctx")
        os.makedirs(ctxdir)
        shutil.copy(binary, os.path.join(ctxdir, "codex"))
        with open(os.path.join(ctxdir, "Dockerfile"), "w") as fh:
            fh.write(f"FROM {IMAGE}:codex-{cur}\nCOPY codex /usr/local/bin/codex\nRUN chmod 755 /usr/local/bin/codex && codex --version\n")
        try:
            run(["docker", "build", "-q", "-t", IMAGE, ctxdir])
            got = image_version()
            if got != target:
                raise Stop(f"the new image reports Codex {got!r}, expected {target}")
            if state["browser"] and not has_browser():
                raise Stop("the browser tool is missing from the new image")
            say(f"2. new layer built: the image now has Codex {got}")
        except Stop as e:
            say(f"\nSTOPPED: {e}\nPutting the old image back ...")
            run(["docker", "tag", f"{IMAGE}:codex-{cur}", IMAGE], check=False)
            return 1
        if catalog:
            shutil.copy(catalog, os.path.join(home, "model-catalog.json"))
            say("3. model catalog regenerated for the new version (the old one is in the backup)")
        state["status"] = "updated"
        write_json(os.path.join(bdir, "state.json"), state)
        write_text(os.path.join(BACKUPS, ".last-codex-update"), bdir)
        agent = os.path.dirname(home.rstrip("/"))
        try:
            write_text(os.path.join(agent, ".codex-version"), f"{target}\n")
        except OSError:
            pass
        say(f"\nDone: Codex {target}. Start a session as usual (codex). To go back:  python3 {os.path.basename(sys.argv[0])} rollback")
        say("(each update adds about 110 MB to the image; `docker image prune` removes layers no tag uses once you no longer need a rollback)")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def cmd_rollback(home, frm, yes):
    bdir = frm
    if not bdir:
        try:
            bdir = read_text(os.path.join(BACKUPS, ".last-codex-update"))
        except OSError:
            bdir = None
    if not bdir or not os.path.isfile(os.path.join(bdir, "state.json")):
        say("no Codex update backup found")
        return 2
    state = read_json(os.path.join(bdir, "state.json"))
    if state.get("status") == "rolled back":
        say("that update was already rolled back")
        return 0
    if session_running():
        raise Stop("a Codex session is running. End it first.")
    say(f"Rolling back to Codex {state['from']} (image tag {state['image_tag']})")
    if not yes and input(f"Type the version ({state['from']}) to go ahead: ").strip() != state["from"]:
        say("Not confirmed. Nothing was changed.")
        return 1
    run(["docker", "tag", state["image_tag"], IMAGE])
    if state.get("catalog_changed") and os.path.isfile(os.path.join(bdir, "model-catalog.json")):
        shutil.copy(os.path.join(bdir, "model-catalog.json"), os.path.join(home, "model-catalog.json"))
    state["status"] = "rolled back"
    write_json(os.path.join(bdir, "state.json"), state)
    try:
        write_text(os.path.join(os.path.dirname(home.rstrip("/")), ".codex-version"), f"{state['from']}\n")
    except OSError:
        pass
    say(f"Done: the image has Codex {image_version()}.")
    return 0


def main():
    argv = sys.argv[1:]
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    action, rest = argv[0], argv[1:]
    opts = {"--home": "~/agent/codex-home", "--strata": "~/github/strata-work/Strata", "--to": None, "--from": None}
    flags = {"--yes": False, "--force": False, "--no-baseline": False}
    i = 0
    while i < len(rest):
        if rest[i] in opts and i + 1 < len(rest):
            opts[rest[i]] = rest[i + 1]; i += 2
        elif rest[i] in flags:
            flags[rest[i]] = True; i += 1
        else:
            print(f"unknown argument {rest[i]!r} (--help)")
            return 2
    home = os.path.abspath(os.path.expanduser(opts["--home"]))
    strata = os.path.abspath(os.path.expanduser(opts["--strata"]))
    strata = strata if os.path.isdir(os.path.join(strata, "serve")) else None
    to = opts["--to"].lstrip("v") if opts["--to"] else None
    try:
        if action == "check":
            rc, _, pack = cmd_check(home, strata, to, baseline=not flags["--no-baseline"])
            if pack:
                shutil.rmtree(pack[3], ignore_errors=True)
            return rc
        if action == "apply":
            return cmd_apply(home, strata, to, flags["--yes"], flags["--force"])
        if action == "rollback":
            return cmd_rollback(home, opts["--from"], flags["--yes"])
    except Stop as e:
        print(f"stopped: {e}")
        return 1
    print(f"unknown action {action!r}; one of: check apply rollback")
    return 2


if __name__ == "__main__":
    sys.exit(main())
