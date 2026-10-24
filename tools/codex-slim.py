#!/usr/bin/env python3
"""codex-slim.py: see what Codex's start-up prompt is made of, and slim it for a small local model.

Since kit 2026-10-19.1 the kit ships the slim catalog (agent/model-catalog.json, made by make-model-catalog.py), so `apply` is only needed on an install that
still has the stock catalog. `measure` is the useful part: it prints what the prompt is made of.

    python3 codex-slim.py measure [--codex /path/to/real/codex] [--image codex-sandbox] [--home ~/agent/codex-home] [--cwd PROJECT_DIR]
    python3 codex-slim.py apply   [--home ~/agent/codex-home]
    python3 codex-slim.py restore [--home ~/agent/codex-home]

measure  Runs the real Codex binary (taken from the Docker image unless --codex is given; `~/.local/bin/codex` is the kit's launcher, not the binary) against a stand-in server with a COPY of your Codex home (config, model catalog, AGENTS.md) and prints
         how many tokens each part of the first request takes (about: characters / 4). Nothing is sent to Strata; your files are not changed.
         --cwd lets you include a project's own AGENTS.md.
apply    1. replaces `base_instructions` in the model catalog named by config.toml (`model_catalog_json`) with a short prompt
         2. adds `[skills.bundled] enabled = false` to config.toml (the four built-in skills cost about 500 tokens and a local model cannot use them)
         Backups: <file>.bak-slim (made once). The short prompt is also written to <home>/slim-instructions.md: edit it, then run apply again.
restore  Puts the backups back.

Keeps everything else the kit relies on: the catalog's reasoning levels, tool_mode "direct", shell_type, the feature flags.
After `apply`, run the kit's tools/codex_compat.py: its tools probe compares the prompt size and may report the smaller prompt as a warning.
"""
import argparse, http.server, json, os, re, shutil, subprocess, sys, tempfile, threading, tomllib

SLIM = """You are a coding agent in the Codex CLI, working with a small local model. You work inside the project folder you were started in. You have two tools: exec_command (run a shell command) and write_stdin (send input to a running command).

How to work:
- Do exactly the task you were given, nothing more. AGENTS.md is already in your context: do not read it again.
- Start with ONE command: grep -n '^- \\[ \\]' PLAN.md | head -3; tail -5 progress.txt | cut -c1-400
  Do not cat PLAN.md or progress.txt; read PLAN.md in full only if that finds no open task. If progress.txt ends with a WIP: PLAN line that has no WIP: DONE after it, that change is still open: continue it, do not re-plan.
- Read files in small pieces: grep -n first, then sed -n 'A,Bp' for only the lines you need, at most 60 lines per piece. Put several reads in ONE command (separate them with echo ---). Never print a whole large file.
- Edit with small patches using apply_patch:
  apply_patch <<'PATCH'
  *** Begin Patch
  *** Update File: path/to/file
  @@
  -old line
  +new line
  *** End Patch
  PATCH
  (*** Add File: path creates a file; every content line then starts with +.) A file under 80 lines may be rewritten whole.
- Checkpoint to progress.txt (append only, never rewrite it), inside the same command as the patch or the next step, not as a separate command. Before you apply a patch you have designed, add "WIP: PLAN <file>: <exact change>". Once it is applied and checked, add "WIP: DONE <file>: <change>". One line each.
- After every change run the task's check command and read its output.
- Never say a task is done before its check passes.
- Keep answers to one or two short sentences. No headings, no list of what you did.
- If you cannot continue without an answer, ask in plain text. Otherwise do not ask for permission.
"""
tok = lambda s: round(len(s) / 4)


def home_of(a):
    return os.path.abspath(os.path.expanduser(a.home))


CONTAINER_HOME = "/home/agent/.codex"      # where the container sees ~/agent/codex-home; config.toml uses these paths


def catalog_path(home):
    """The model catalog as a path on THIS machine (config.toml names it as the container sees it)."""
    if not os.path.isfile(os.path.join(home, "config.toml")):
        sys.exit(f"no config.toml in {home}. Pass the Codex home with --home (normally ~/agent/codex-home)")
    with open(os.path.join(home, "config.toml"), "rb") as fh:
        cfg = tomllib.load(fh)
    p = cfg.get("model_catalog_json")
    if not p:
        sys.exit("config.toml has no model_catalog_json: nothing to slim (the model would use Codex's built-in prompt)")
    p = os.path.expanduser(p)
    if p == CONTAINER_HOME or p.startswith(CONTAINER_HOME + "/"):
        p = os.path.join(home, p[len(CONTAINER_HOME):].lstrip("/"))
    elif not os.path.isabs(p):
        p = os.path.join(home, p)
    if not os.path.isfile(p):
        sys.exit(f"the model catalog is not at {p} (config.toml says {cfg.get('model_catalog_json')}). Is --home the right folder?")
    return p


def backup(path):
    b = path + ".bak-slim"
    if not os.path.exists(b):
        shutil.copy2(path, b)
        print(f"  backup  {b}")


def apply(a):
    home = home_of(a)
    cat = catalog_path(home)
    cfgp = os.path.join(home, "config.toml")
    sf = os.path.join(home, "slim-instructions.md")
    if not os.path.exists(sf):
        with open(sf, "w") as fh:
            fh.write(SLIM)
    with open(sf) as fh:
        text = fh.read().strip("\n") + "\n"
    backup(cat); backup(cfgp)
    with open(cat) as fh:
        d = json.load(fh)
    n = 0
    for m in d.get("models", []):
        before = tok(m.get("base_instructions") or "")
        m["base_instructions"] = text
        print(f"  catalog {m.get('slug')}: base_instructions ~{before} -> ~{tok(text)} tokens")
        n += 1
    if not n:
        sys.exit("the catalog lists no models")
    with open(cat, "w") as fh:
        json.dump(d, fh, indent=1)
    with open(cfgp) as fh:
        cfg = fh.read()
    if re.search(r"^\[skills\.bundled\]", cfg, re.M):
        print("  config  [skills.bundled] already present: left as it is")
    else:
        with open(cfgp, "a") as fh:
            fh.write("\n[skills.bundled]\nenabled = false\n")
        print("  config  added [skills.bundled] enabled = false")
    print("done. Start a new session (the first request re-reads the shorter prompt once). To undo: restore")


def restore(a):
    home = home_of(a)
    for p in (catalog_path(home), os.path.join(home, "config.toml")):
        b = p + ".bak-slim"
        if os.path.exists(b):
            shutil.copy2(b, p); print(f"  restored {p}")
        else:
            print(f"  no backup for {p}")


def real_codex(a, tmp):
    """The real Codex binary: the --codex path (must be a program, not the kit's launcher script), or a copy taken out of the Docker image."""
    if a.codex:
        try:
            with open(a.codex, "rb") as fh:
                magic = fh.read(4)
        except OSError as e:
            sys.exit(f"cannot read {a.codex}: {e}")
        if magic != b"\x7fELF":
            sys.exit(f"{a.codex} is a script (the kit's `codex` launcher), not the Codex program. Leave out --codex to take the program from the image {a.image}.")
        return a.codex
    dest = os.path.join(tmp, "codex")
    try:
        cid = subprocess.run(["docker", "create", a.image], capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as e:
        sys.exit(f"could not read the image {a.image} ({getattr(e, 'stderr', e)}). Pass the Codex program with --codex PATH.")
    try:
        r = subprocess.run(["docker", "cp", f"{cid}:/usr/local/bin/codex", dest], capture_output=True, text=True)
        if r.returncode:
            sys.exit(f"could not copy /usr/local/bin/codex out of {a.image}: {r.stderr.strip()}")
    finally:
        subprocess.run(["docker", "rm", cid], capture_output=True)
    os.chmod(dest, 0o755)
    return dest


def measure(a):
    home = home_of(a)
    reqs = []

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *x): pass
        def do_GET(self): self.send_response(200); self.end_headers(); self.wfile.write(b"{}")
        def do_POST(self):
            reqs.append(json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))) or b"{}"))
            self.send_response(200); self.send_header("Content-Type", "text/event-stream"); self.end_headers()
            ev = [("response.created", {"type": "response.created", "response": {"id": "r1"}}),
                  ("response.output_item.done", {"type": "response.output_item.done", "item": {"type": "message", "role": "assistant", "id": "m1", "content": [{"type": "output_text", "text": "ok"}]}}),
                  ("response.completed", {"type": "response.completed", "response": {"id": "r1", "usage": {"input_tokens": 1, "input_tokens_details": None, "output_tokens": 1, "output_tokens_details": None, "total_tokens": 2}}})]
            for n, d in ev:
                self.wfile.write(f"event: {n}\ndata: {json.dumps(d)}\n\n".encode())

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    tmp = tempfile.mkdtemp(prefix="codex-slim-", dir=os.path.expanduser("~"))
    try:
        h = os.path.join(tmp, "home")
        shutil.copytree(home, h, ignore=shutil.ignore_patterns("sessions", "log", "logs", "*.sqlite*", "history.jsonl", "tmp", "*.bak*"), symlinks=True)
        cat = catalog_path(home)
        cfgp = os.path.join(h, "config.toml")          # point the copy's catalog at the copy (the real path only exists inside the container)
        with open(cfgp) as fh:
            c = fh.read()
        c = re.sub(r'^(model_catalog_json\s*=\s*)".*"$', lambda m: f'{m.group(1)}"{os.path.join(h, os.path.relpath(cat, home))}"', c, flags=re.M)
        with open(cfgp, "w") as fh:
            fh.write(c)
        with open(os.path.join(h, "config.toml"), "a") as fh:
            fh.write(f'\n[model_providers.standin]\nname = "Stand-in"\nbase_url = "http://127.0.0.1:{port}/v1"\nwire_api = "responses"\n')
        work = os.path.abspath(a.cwd) if a.cwd else tempfile.mkdtemp(dir=tmp)
        env = dict(os.environ, CODEX_HOME=h)
        codex = real_codex(a, tmp)
        r = subprocess.run([codex, "--no-daemon", "exec", "--skip-git-repo-check", "-s", "danger-full-access", "-c", 'approval_policy="never"',
                            "-c", 'model_provider="standin"', "hello"], cwd=work, env=env, capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    main = [b for b in reqs if b.get("tools") is not None]
    if not main:
        sys.exit(f"no request captured (exit {r.returncode}): {(r.stderr or r.stdout)[-400:]}")
    b = main[0]
    ins = b.get("instructions") or ""
    tools = b.get("tools") or []
    inp = b.get("input") or []
    print(f"Codex request, about tokens (chars/4):")
    rows = []
    if ins:
        rows.append(("instructions field", tok(ins)))
    for i in inp:
        text = " ".join(c.get("text", "") for c in i.get("content", []) if isinstance(c, dict)) if isinstance(i.get("content"), list) else str(i.get("content"))
        label = (i.get("role") or i.get("type") or "?")
        first = re.sub(r"\s+", " ", text)[:60]
        kind = ("skills list" if text.lstrip().startswith("<skills_instructions>") else "permissions" if text.lstrip().startswith("<permissions") else
                "AGENTS.md" if text.lstrip().startswith("# AGENTS.md instructions") else "environment" if text.lstrip().startswith("<environment_context>") else
                "your prompt" if text.strip() == "hello" else "base instructions" if label == "developer" else "other")
        rows.append((f"{label}: {kind}", tok(json.dumps(i))))
    tt = sum(tok(json.dumps(t)) for t in tools)
    for t in sorted(tools, key=lambda t: -len(json.dumps(t))):
        rows.append((f"tool {t.get('name') or t.get('type')}", tok(json.dumps(t))))
    total = sum(v for _, v in rows)
    for k, v in rows:
        print(f"  {k:36s} {v:6d}")
    hello = next((v for k, v in rows if k.endswith("your prompt")), 0)
    print(f"  {'TOTAL (excluding your prompt)':36s} {total - hello:6d}   cold read at 42 tok/s: {(total - hello) / 42:.0f} s")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["measure", "apply", "restore"])
    ap.add_argument("--home", default="~/agent/codex-home")
    ap.add_argument("--codex", help="path to the real Codex program (measure); default: copied out of the Docker image")
    ap.add_argument("--image", default="codex-sandbox", help="the Docker image that holds Codex (measure)")
    ap.add_argument("--cwd", help="a project folder whose AGENTS.md should be included (measure)")
    a = ap.parse_args()
    {"measure": measure, "apply": apply, "restore": restore}[a.cmd](a)


if __name__ == "__main__":
    main()
