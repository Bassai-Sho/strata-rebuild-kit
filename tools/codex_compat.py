#!/usr/bin/env python3
"""codex_compat.py: does this Codex version still do everything the kit depends on?

    python3 codex_compat.py --codex /path/to/codex [--home ~/agent/codex-home] [--catalog FILE] [--strata ~/github/strata-work/Strata] [--json]

Runs a real Codex binary (never installs it) against a tiny local server that stands in for Strata, with a COPY of your real Codex home
(config.toml, AGENTS.md, templates, model catalog), and checks, one by one, what the `codex` command, the title filter, the loop and the
reasoning shortcuts rely on:

  version        the binary runs and reports the version you expect
  config         your config.toml and model catalog are accepted
  features       the feature flags the installer switches off still exist
  tools          the same function tools as before (exec_command, write_stdin ...), a prompt of about the same size
  rules          your workspace AGENTS.md and a project's AGENTS.md reach the model
  tool-run       a tool call the model makes is executed and its output goes back
  loop-command   the exact command line `codex loop` uses is accepted
  interactive    the full-screen interface starts (without the background server that fails with this binary) and talks to the model
  title          the extra "write a session title" request is still recognised by the title filter
  shortcuts      Alt+. and Alt+, change the reasoning level and the request carries it
  paste          a pasted block of text (small and large) arrives whole and is not sent until you press Enter
  autopilot      `--enable goals`: the session continues by itself until the goal is complete (`codex --auto`)
  strata         (with --strata) Strata's own Responses-API parser accepts the requests this Codex sends

Exit code 0: nothing failed (warnings are listed). 1: something the kit depends on is broken.
"""
import argparse, glob, http.server, json, os, pty, re, select, shutil, signal, struct, subprocess, sys, tempfile, threading, time
import fcntl, termios

HERE = os.path.dirname(os.path.abspath(__file__))
REQUIRED_FEATURES = ["goals", "multi_agent", "apps", "plugins", "view_image", "worktrees", "daemon_auto_start"]
TITLE_MARK = "Generate a concise, single-line task title"
LOOP_PROMPT = "Read AGENTS.md, PLAN.md and progress.txt, then do the first unticked task in PLAN.md and nothing else."


# ---- a stand-in for Strata ---------------------------------------------------------------------------------------------------------------
def sse(handler, events):
    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream")
    handler.end_headers()
    for name, data in events:
        handler.wfile.write(f"event: {name}\ndata: {json.dumps(data)}\n\n".encode())
        handler.wfile.flush()


def text_events(text, n=0):
    return [("response.created", {"type": "response.created", "response": {"id": f"r{n}"}}),
            ("response.output_item.done", {"type": "response.output_item.done", "item": {"type": "message", "role": "assistant", "id": f"m{n}",
             "content": [{"type": "output_text", "text": text}]}}),
            ("response.completed", {"type": "response.completed", "response": {"id": f"r{n}", "usage": {"input_tokens": 1, "input_tokens_details": None,
             "output_tokens": 1, "output_tokens_details": None, "total_tokens": 2}}})]


def call_events(name, args, n=0):
    return [("response.created", {"type": "response.created", "response": {"id": f"r{n}"}}),
            ("response.output_item.done", {"type": "response.output_item.done", "item": {"type": "function_call", "id": f"fc{n}", "call_id": f"call{n}",
             "name": name, "arguments": json.dumps(args)}}),
            ("response.completed", {"type": "response.completed", "response": {"id": f"r{n}", "usage": {"input_tokens": 1, "input_tokens_details": None,
             "output_tokens": 1, "output_tokens_details": None, "total_tokens": 2}}})]


def is_title(body):
    meta = (body.get("client_metadata") or {}).get("x-codex-turn-metadata")
    try:
        if isinstance(meta, str) and json.loads(meta).get("thread_source") == "thread_title":
            return True
    except ValueError:
        pass
    try:
        return body["input"][-1]["content"][0]["text"].startswith(TITLE_MARK)
    except (KeyError, IndexError, TypeError, AttributeError):
        return False


class Mock:
    """script(n, body, requests) -> list of SSE events; title requests are answered by the mock and kept out of `main`."""
    def __init__(self, script=None):
        self.requests, self.script, self.lock = [], script or (lambda n, b, r: text_events("ok", n)), threading.Lock()
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a): pass
            def do_GET(self): self.send_response(200); self.end_headers(); self.wfile.write(b"{}")
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))) or b"{}")
                with outer.lock:
                    outer.requests.append(body)
                    n = len([r for r in outer.requests if not is_title(r)]) - 1
                if is_title(body):
                    return sse(self, text_events(json.dumps({"title": "Probe"}), 99))
                sse(self, outer.script(n, body, outer.requests))
        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    @property
    def main(self): return [r for r in self.requests if not is_title(r)]
    @property
    def titles(self): return [r for r in self.requests if is_title(r)]
    def close(self): self.srv.shutdown(); self.srv.server_close()


# ---- helpers --------------------------------------------------------------------------------------------------------------------------------
class Result:
    def __init__(self, name): self.name, self.status, self.detail, self.extra = name, "pass", "", {}
    def fail(self, d): self.status, self.detail = "fail", d; return self
    def warn(self, d): self.status = "warn" if self.status != "fail" else "fail"; self.detail = (self.detail + "; " if self.detail else "") + d; return self
    def ok(self, d): self.detail = d; return self


def clean(b):
    t = b.decode("utf8", "replace")
    t = re.sub(r"\x1b\[[0-9;?<>=]*[A-Za-z ]|\x1b\][^\x07\x1b]*(\x07|\x1b\\)|\x1b[=>]|[\u2800-\u28ff]", "", t)
    return re.sub(r"\s+", " ", t.replace("\r", " "))


def run_exec(codex, home, cwd, prompt, extra_args=(), timeout=90, provider=None):
    env = dict(os.environ, CODEX_HOME=home)
    argv = [codex, "--no-daemon", *extra_args, "exec", "--skip-git-repo-check", "-s", "danger-full-access", "-c", 'approval_policy="never"']
    if provider:
        argv += ["-c", f'model_provider="{provider}"']
    argv.append(prompt)
    try:
        r = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
        return r.returncode, r.stdout + r.stderr
    except subprocess.TimeoutExpired as e:
        return 124, (e.stdout or "") if isinstance(e.stdout, str) else ""


class Tui:
    """The real full-screen interface in a pseudo-terminal, driven like a person would."""
    def __init__(self, codex, home, cwd, args, rows=40, cols=130):
        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            os.chdir(cwd); os.environ.update(TERM="xterm-256color", CODEX_HOME=home)
            os.execv(codex, [codex, *args])
        fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        self.buf = b""

    def pump(self, sec):
        t0 = time.time(); out = b""
        while time.time() - t0 < sec:
            r, _, _ = select.select([self.fd], [], [], 0.25)
            if r:
                try: d = os.read(self.fd, 65536)
                except OSError: break
                if not d: break
                out += d
                if b"\x1b[6n" in d: os.write(self.fd, b"\x1b[10;10R")
        self.buf += out
        return clean(out)

    def wait_for(self, pred, sec):
        t0 = time.time()
        while time.time() - t0 < sec:
            if pred(): return True
            self.pump(0.5)
        return pred()

    def type_line(self, text):
        os.write(self.fd, text.encode()); time.sleep(0.9); os.write(self.fd, b"\r")     # a burst with Enter inside looks like a paste

    def alive(self):
        try: return os.waitpid(self.pid, os.WNOHANG) == (0, 0)
        except ChildProcessError: return False

    def close(self):
        try: os.kill(self.pid, signal.SIGKILL); os.waitpid(self.pid, 0)
        except (OSError, ChildProcessError): pass
        try: os.close(self.fd)
        except OSError: pass


def make_home(src, mock_port, catalog=None):
    """A private copy of the Codex home: provider URLs point at the stand-in server, the catalog path at the copy."""
    d = tempfile.mkdtemp(prefix="codex-compat-")
    if src and os.path.isdir(src):
        for item in ("config.toml", "AGENTS.md", "templates", "tools", "model-catalog.json"):
            s = os.path.join(src, item)
            if os.path.isdir(s): shutil.copytree(s, os.path.join(d, item))
            elif os.path.isfile(s): shutil.copy(s, os.path.join(d, item))
    if catalog:
        shutil.copy(catalog, os.path.join(d, "model-catalog.json"))
    cfg = os.path.join(d, "config.toml")
    text = open(cfg).read() if os.path.exists(cfg) else 'model = "strata"\nmodel_provider = "strata"\n'
    text = re.sub(r'base_url\s*=\s*"[^"]*"', f'base_url = "http://127.0.0.1:{mock_port}/v1"', text)
    text = re.sub(r'model_catalog_json\s*=\s*"[^"]*"', f'model_catalog_json = "{os.path.join(d, "model-catalog.json")}"', text)
    if "[model_providers." not in text:
        text += f'\n[model_providers.strata]\nname = "x"\nbase_url = "http://127.0.0.1:{mock_port}/v1"\nwire_api = "responses"\n'
    if "daemon_auto_start" not in text:
        text += "\n[features]\ndaemon_auto_start = false\n"
    with open(cfg, "w") as fh: fh.write(text)
    if not os.path.exists(os.path.join(d, "AGENTS.md")):
        with open(os.path.join(d, "AGENTS.md"), "w") as fh: fh.write("# Workspace rules\nWORKSPACE-RULES-MARKER\n")
    return d


def provider_of(home):
    with open(os.path.join(home, "config.toml")) as fh: text = fh.read()
    return "strata_front" if "[model_providers.strata_front]" in text else None


def tokens(body): return len(json.dumps(body)) // 4


# ---- the probes ---------------------------------------------------------------------------------------------------------------------------
def probe_version(ctx):
    r = Result("version")
    try:
        out = subprocess.run([ctx.codex, "--version"], capture_output=True, text=True, timeout=30).stdout.strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return r.fail(f"the binary does not run: {e}")
    ctx.version = out.split()[-1] if out else "?"
    if ctx.expect and ctx.expect not in out:
        return r.fail(f"reports {out!r}, expected {ctx.expect}")
    return r.ok(out)


def probe_config(ctx):
    r = Result("config")
    mock = Mock(); home = make_home(ctx.home, mock.port, ctx.catalog)
    try:
        rc, out = run_exec(ctx.codex, home, ctx.work, "hello", provider=provider_of(home))
        if mock.main:
            ctx.first_request = mock.main[0]
            errs = [ln for ln in out.splitlines() if ln.startswith("Error:")]
            warns = [ln.strip()[:110] for ln in out.splitlines() if re.search(r"warning|deprecat|migrat|retired|no longer", ln, re.I) and "metadata" not in ln.lower() and "PATH aliases" not in ln]
            r.ok("your config and model catalog are accepted")
            for w in sorted(set(warns))[:3]: r.warn(w)
            return r
        err = next((ln for ln in out.splitlines() if "Error" in ln), out.strip()[:200])
        r.fail(f"no request reached the model server: {err[:260]}")
        if "model_catalog_json" in err or "base_instructions" in err or "model `strata`" in err:
            r.extra["catalog_needs_rebuild"] = True
        return r
    finally:
        mock.close(); shutil.rmtree(home, ignore_errors=True)


def probe_features(ctx):
    r = Result("features")
    out = subprocess.run([ctx.codex, "features", "list"], capture_output=True, text=True, timeout=60).stdout
    have = {ln.split()[0]: ln for ln in out.splitlines() if ln.strip()}
    missing = [f for f in REQUIRED_FEATURES if f not in have]
    if missing:
        return r.fail("flags the installer sets no longer exist: " + ", ".join(missing))
    odd = [f for f in REQUIRED_FEATURES if "removed" in have[f] or "deprecated" in have[f]]
    if odd:
        r.warn("flagged removed or deprecated: " + ", ".join(odd))
    return r.ok(f"all {len(REQUIRED_FEATURES)} flags still exist")


def probe_tools(ctx):
    r = Result("tools")
    b = ctx.first_request
    if not b:
        return r.warn("no first request to inspect (config probe failed)")
    names = [t.get("name") or t.get("type") for t in b.get("tools", [])]
    ctx.tool_names, ctx.prompt_tokens = names, tokens(b)
    need = ["exec_command", "write_stdin"]
    if [n for n in need if n not in names]:
        return r.fail(f"function tools missing: {[n for n in need if n not in names]} (tools: {names})")
    extra = [n for n in names if n in ("get_goal", "create_goal", "update_goal", "multi_agent_v1", "spawn_agent") or str(n).startswith("mcp__")]
    r.ok(f"tools {names}; prompt about {ctx.prompt_tokens} tokens")
    if extra:
        r.warn(f"tools that should be switched off are present: {extra}")
    if ctx.prompt_tokens > 9500:
        r.warn(f"the prompt is {ctx.prompt_tokens} tokens (about 2,500 with the kit's slim base instructions, about 7,200 with Codex's own): a cold start reads it at about 42 tok/s")
    return r


def probe_rules(ctx):
    r = Result("rules")
    mock = Mock(); home = make_home(ctx.home, mock.port, ctx.catalog)
    proj = tempfile.mkdtemp(prefix="codex-proj-")
    try:
        with open(os.path.join(proj, "AGENTS.md"), "w") as fh: fh.write("# Project rules\nPROJECT-RULES-MARKER\n")
        gl = open(os.path.join(home, "AGENTS.md")).read().strip().splitlines()
        gmark = next((ln for ln in gl if ln.strip() and not ln.startswith("#")), gl[0])[:40]
        run_exec(ctx.codex, home, proj, "hello", provider=provider_of(home))
        if not mock.main:
            return r.fail("no request reached the model server")
        text = json.dumps(mock.main[0].get("input", []))
        miss = [n for n, m in (("workspace rules (AGENTS.md in the Codex home)", gmark), ("project rules (AGENTS.md in the project)", "PROJECT-RULES-MARKER")) if m not in text]
        return r.fail("not delivered: " + "; ".join(miss)) if miss else r.ok("workspace and project AGENTS.md both reach the model")
    finally:
        mock.close(); shutil.rmtree(home, ignore_errors=True); shutil.rmtree(proj, ignore_errors=True)


def probe_tool_run(ctx):
    r = Result("tool-run")
    proj = tempfile.mkdtemp(prefix="codex-proj-")
    target = os.path.join(proj, "proof.txt")
    def script(n, body, reqs):
        outs = [i for i in body.get("input", []) if i.get("type") == "function_call_output"]
        return text_events("done", n) if outs else call_events("exec_command", {"cmd": "echo TOOL-RAN > proof.txt"}, n)
    mock = Mock(script); home = make_home(ctx.home, mock.port, ctx.catalog)
    try:
        run_exec(ctx.codex, home, proj, "make the file", provider=provider_of(home))
        ctx.tool_bodies = mock.main
        if not os.path.exists(target):
            return r.fail("the model's tool call was not executed")
        if len(mock.main) < 2 or not any(i.get("type") == "function_call_output" for i in mock.main[-1].get("input", [])):
            return r.fail("the tool ran but its output was not sent back to the model")
        return r.ok("a tool call runs and its output returns")
    finally:
        mock.close(); shutil.rmtree(home, ignore_errors=True); shutil.rmtree(proj, ignore_errors=True)


def probe_loop_command(ctx):
    r = Result("loop-command")
    mock = Mock(); home = make_home(ctx.home, mock.port, ctx.catalog)
    try:
        rc, out = run_exec(ctx.codex, home, ctx.work, LOOP_PROMPT, provider="strata_front" if provider_of(home) else None)
        return r.ok("the exact `codex loop` command line is accepted") if mock.main else r.fail(f"rejected: {out.strip()[:240]}")
    finally:
        mock.close(); shutil.rmtree(home, ignore_errors=True)


def tui_args(home, extra=()):
    a = ["--no-daemon", "--disable", "worktrees", *extra]
    if provider_of(home):
        a += ["-c", 'model_provider="strata_front"']
    return a + ["-s", "danger-full-access", "-a", "never"]


def probe_interactive(ctx):
    r = Result("interactive")
    mock = Mock(); home = make_home(ctx.home, mock.port, ctx.catalog)
    t = Tui(ctx.codex, home, ctx.work, tui_args(home) + ["say hello"])
    try:
        ok = t.wait_for(lambda: bool(mock.main), 40)
        screen = clean(t.buf)
        if "no complete local package" in screen:
            return r.fail("the interface still tries to start the background server and fails (the daemon_auto_start setting no longer helps)")
        if not ok:
            return r.fail("the interface did not reach the model server: " + screen[-200:])
        ctx.tui_bodies = mock.main
        return r.ok("starts and talks to the model, without the background server")
    finally:
        t.close(); mock.close(); shutil.rmtree(home, ignore_errors=True)


def probe_title(ctx):
    r = Result("title")
    mock = Mock(); home = make_home(ctx.home, mock.port, ctx.catalog)
    t = Tui(ctx.codex, home, ctx.work, tui_args(home) + ["write a poem about cats"])
    try:
        t.wait_for(lambda: bool(mock.main) and bool(mock.titles), 40); t.pump(2)
        if not mock.titles:
            return r.warn("no extra title request was sent (this version may have dropped it: the filter is then simply idle)")
        body = mock.titles[0]
        meta = (body.get("client_metadata") or {}).get("x-codex-turn-metadata")
        by_meta = False
        try: by_meta = isinstance(meta, str) and json.loads(meta).get("thread_source") == "thread_title"
        except ValueError: pass
        by_text = body["input"][-1]["content"][0]["text"].startswith(TITLE_MARK)
        front = os.path.join(HERE, "..", "agent", "strata-front.py")
        recognised = None
        if os.path.isfile(front):
            import importlib.util
            spec = importlib.util.spec_from_file_location("sf", front); m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
            recognised = m.is_title_request(json.dumps(body).encode()) is not None
        if recognised is False:
            return r.fail("Codex still sends a title request, but the title filter no longer recognises it: every prompt would cost Strata a minute again")
        r.ok(f"one title request per prompt; recognised by the filter{'' if recognised else ' (filter not found, not tested)'}; by Codex's own metadata: {by_meta}; by text: {by_text}")
        if not by_meta:
            r.warn("Codex's turn metadata no longer says thread_source=thread_title (Strata's own opt-in `codex_thread_titles` would not recognise it either)")
        return r
    finally:
        t.close(); mock.close(); shutil.rmtree(home, ignore_errors=True)


LEVELS = ["none", "low", "medium", "high"]


def after_keys(start):
    """Where Alt+. Alt+. Alt+, ends from `start` (up, up, down; the ends are clamped)."""
    i = LEVELS.index(start); i = min(3, i + 1); i = min(3, i + 1); i = max(0, i - 1)
    return LEVELS[i]


def probe_shortcuts(ctx, attempts=3):
    """Alt+. raises and Alt+, lowers the reasoning level, and the next request carries it.
    The probe does not assume where the level starts (that comes from your config.toml and the catalog's default, and differs between homes: a start at `none` ends
    at `low` after up, up, down, which an older version of this probe called a failure). It sends a first message to see the starting level, presses the keys,
    sends a second one, and compares the second request with where up, up, down must end from THAT start (the ends are clamped).
    A slow start can still lose a key, so a wrong result is tried again in a fresh interface (up to `attempts`); a pass that needed a retry is a warning."""
    r = Result("shortcuts"); seen = []
    for attempt in range(1, attempts + 1):
        mock = Mock(); home = make_home(ctx.home, mock.port, ctx.catalog)
        t = Tui(ctx.codex, home, ctx.work, tui_args(home))
        try:
            t.pump(9)
            t.type_line("hello"); t.wait_for(lambda: len(mock.main) >= 1, 20); t.pump(4)
            os.write(t.fd, b"\x1b."); s1 = t.pump(1.5)
            if "unavailable" in s1.lower():
                return r.fail("Codex says the reasoning shortcuts are unavailable for strata: " + s1[-120:])
            os.write(t.fd, b"\x1b."); t.pump(1.0)
            os.write(t.fd, b"\x1b,"); t.pump(1.0)
            t.type_line("again"); t.wait_for(lambda: len(mock.main) >= 2, 20)
            eff = lambda k: ((mock.main[k].get("reasoning") or {}).get("effort") if len(mock.main) > k else None)
            start, end = eff(0), eff(1)
        finally:
            t.close(); mock.close(); shutil.rmtree(home, ignore_errors=True)
        seen.append((start, end))
        if start not in LEVELS:
            continue
        want = after_keys(start)
        if end == want:
            r.ok(f"Alt+. Alt+. Alt+, took the level from {start!r} to {end!r}, and the request carries it")
            return r.warn(f"passed on attempt {attempt} of {attempts}: an earlier start was too slow for the keys") if attempt > 1 else r
    start, end = seen[-1]
    if start not in LEVELS:
        return r.fail(f"the first request carried no reasoning level that this probe knows (effort {start!r}); starts seen: {seen}. Check model_reasoning_effort in config.toml and the catalog's default_reasoning_level")
    return r.fail(f"the level started at {start!r}; after Alt+. Alt+. Alt+, the request carried {end!r}, expected {after_keys(start)!r}, in each of {attempts} fresh starts (starts seen: {seen}; the keys may have moved: check /keymap)")


def probe_paste(ctx):
    r = Result("paste")
    small = "line one of my block\nline two: def f(x):\n    return x + 1\nline four"
    large = "\n".join(f"row {i}: " + "lorem ipsum dolor sit amet " * 3 for i in range(1, 121))
    for label, text, marks in (("small", small, ("line one of my block", "return x + 1", "line four")), ("large", large, ("row 1:", "row 60:", "row 120:"))):
        mock = Mock(); home = make_home(ctx.home, mock.port, ctx.catalog)
        t = Tui(ctx.codex, home, ctx.work, tui_args(home))
        try:
            t.pump(9)
            os.write(t.fd, b"\x1b[200~" + text.encode() + b"\x1b[201~")        # what a terminal sends for Ctrl+Shift+V
            t.pump(2.5)
            if mock.main:
                return r.fail(f"a {label} paste was sent to the model before Enter was pressed")
            os.write(t.fd, b"\r"); t.wait_for(lambda: bool(mock.main), 20)
            sent = json.dumps(mock.main[0].get("input", [])[-1:]) if mock.main else ""
            missing = [m for m in marks if m not in sent]
            if not mock.main or missing:
                return r.fail(f"a {label} paste did not arrive whole (missing {missing or 'everything'})")
        finally:
            t.close(); mock.close(); shutil.rmtree(home, ignore_errors=True)
    return r.ok("a small and a large paste arrive whole, and wait for Enter")


def probe_autopilot(ctx):
    r = Result("autopilot")
    def script(n, body, reqs):
        return call_events("create_goal", {"objective": "autopilot probe"}, n) if n == 0 else text_events("working", n) if n < 3 else call_events("update_goal", {"status": "complete"}, n) if n == 3 else text_events("done", n)
    mock = Mock(script); home = make_home(ctx.home, mock.port, ctx.catalog)
    t = Tui(ctx.codex, home, ctx.work, tui_args(home, ("--enable", "goals")) + ["Call create_goal with the objective: autopilot probe."])
    try:
        t.wait_for(lambda: len(mock.main) >= 4, 45); t.pump(1)
        tools = [x.get("name") for x in (mock.main[0].get("tools", []) if mock.main else [])]
        if "create_goal" not in tools:
            return r.fail(f"--enable goals no longer offers the goal tools (tools: {tools})")
        cont = [q for q in mock.main if "codex_internal_context" in json.dumps(q.get("input", [])[-1:]) and "goal" in json.dumps(q.get("input", [])[-1:])]
        if not cont:
            return r.fail("after the goal was created Codex did not continue by itself (`codex --auto` would behave like step mode)")
        return r.ok(f"the goal continues by itself ({len(cont)} automatic continuation request(s) with nothing typed)")
    finally:
        t.close(); mock.close(); shutil.rmtree(home, ignore_errors=True)


def probe_strata(ctx):
    r = Result("strata")
    if not ctx.strata:
        return None
    sys.path.insert(0, ctx.strata)
    try:
        from serve import responses as resp                     # Strata's own parser, run offline on the requests this Codex sends
    except Exception as e:  # noqa: BLE001
        return r.warn(f"could not import Strata's serve/responses.py from {ctx.strata} ({type(e).__name__}: {e}); skipped")
    finally:
        sys.path.pop(0)
    bodies = [b for b in [ctx.first_request, *getattr(ctx, "tool_bodies", []), *getattr(ctx, "tui_bodies", [])] if b]
    bad = []
    for i, b in enumerate(bodies):
        try:
            resp.check_request(b); resp.input_messages(b); resp.request_tools(b)
        except Exception as e:  # noqa: BLE001
            bad.append(f"request {i + 1}: {type(e).__name__}: {str(e)[:120]}")
    if bad:
        return r.fail("Strata's own parser rejects what this Codex sends: " + "; ".join(bad[:3]))
    return r.ok(f"Strata's own Responses parser accepts all {len(bodies)} captured requests (including tool outputs)")


PROBES = [probe_version, probe_config, probe_features, probe_tools, probe_rules, probe_tool_run, probe_loop_command,
          probe_interactive, probe_title, probe_shortcuts, probe_paste, probe_autopilot, probe_strata]


class Ctx:
    pass


def run_all(codex, home=None, catalog=None, strata=None, expect=None, quick=False, say=print):
    ctx = Ctx(); ctx.codex, ctx.home, ctx.catalog, ctx.strata, ctx.expect = codex, home, catalog, strata, expect
    ctx.work = tempfile.mkdtemp(prefix="codex-work-"); ctx.first_request = None; ctx.version = "?"
    results = []
    slow = (probe_interactive, probe_title, probe_shortcuts, probe_paste, probe_autopilot)
    try:
        for p in PROBES:
            if quick and p in slow:
                continue
            say(f"  running {p.__name__[6:].replace('_', '-')} ...")
            try:
                res = p(ctx)
            except Exception as e:  # noqa: BLE001
                res = Result(p.__name__[6:]).fail(f"the probe itself crashed: {type(e).__name__}: {e}")
            if res is not None:
                results.append(res)
    finally:
        shutil.rmtree(ctx.work, ignore_errors=True)
    return ctx, results


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--codex", required=True)
    ap.add_argument("--home", default=os.path.expanduser("~/agent/codex-home"))
    ap.add_argument("--catalog", default=None)
    ap.add_argument("--strata", default=None)
    ap.add_argument("--expect", default=None, help="the version the binary must report")
    ap.add_argument("--quick", action="store_true", help="skip the four full-screen probes")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    ctx, results = run_all(a.codex, a.home, a.catalog, a.strata, a.expect, a.quick, say=(lambda *_: None) if a.json else print)
    if a.json:
        print(json.dumps([{"probe": r.name, "status": r.status, "detail": r.detail} for r in results], indent=1))
    else:
        print(f"\nCodex {ctx.version}")
        for r in results:
            print(f"  {'ok  ' if r.status == 'pass' else r.status.upper().ljust(4)} {r.name:13s} {r.detail}")
    return 1 if any(r.status == "fail" for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
