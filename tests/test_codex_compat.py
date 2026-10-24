import importlib.util, json, os, shutil, stat, subprocess, sys, tempfile, unittest, urllib.request

TOOLS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tools")
spec = importlib.util.spec_from_file_location("cc", os.path.join(TOOLS, "codex_compat.py")); cc = importlib.util.module_from_spec(spec); spec.loader.exec_module(cc)

def post(port, body):
    req = urllib.request.Request(f"http://127.0.0.1:{port}/v1/responses", data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r: return r.read().decode()

class Harness(unittest.TestCase):
    """The probe suite's own machinery (the probes themselves need a real Codex binary: run codex-update.py check)."""
    def test_the_stand_in_server_records_requests_and_keeps_title_requests_apart(self):
        m = cc.Mock()
        try:
            self.assertIn("response.completed", post(m.port, {"input": [{"content": [{"text": "hello"}]}]}))
            title = {"client_metadata": {"x-codex-turn-metadata": json.dumps({"thread_source": "thread_title"})}, "input": [{"content": [{"text": "name this"}]}]}
            self.assertIn('Probe', post(m.port, title))      # the stand-in answers a title request with a title
            post(m.port, {"input": [{"content": [{"text": cc.TITLE_MARK + " ..."}]}]})                              # the older shape is recognised too
            self.assertEqual((len(m.main), len(m.titles)), (1, 2))
        finally: m.close()
    def test_scripted_replies_follow_the_request_count(self):
        m = cc.Mock(lambda n, b, r: cc.call_events("exec_command", {"cmd": "true"}, n) if n == 0 else cc.text_events("done", n))
        try:
            self.assertIn("function_call", post(m.port, {"input": []})); self.assertIn("output_text", post(m.port, {"input": []}))
        finally: m.close()
    def test_make_home_copies_the_real_files_and_points_them_at_the_stand_in(self):
        src = tempfile.mkdtemp()
        try:
            with open(os.path.join(src, "config.toml"), "w") as fh:
                fh.write('model_catalog_json = "/home/agent/.codex/model-catalog.json"\n[model_providers.strata]\nbase_url = "http://127.0.0.1:8085/v1"\n[model_providers.strata_front]\nbase_url = "http://127.0.0.1:8086/v1"\n')
            with open(os.path.join(src, "model-catalog.json"), "w") as fh: fh.write("{}")
            with open(os.path.join(src, "AGENTS.md"), "w") as fh: fh.write("my rules\n")
            home = cc.make_home(src, 4321)
            text = open(os.path.join(home, "config.toml")).read()
            self.assertEqual(text.count("127.0.0.1:4321"), 2); self.assertNotIn("8085", text); self.assertIn(os.path.join(home, "model-catalog.json"), text)
            self.assertIn("daemon_auto_start", text); self.assertEqual(cc.provider_of(home), "strata_front")
            self.assertTrue(os.path.exists(os.path.join(home, "AGENTS.md"))); shutil.rmtree(home)
        finally: shutil.rmtree(src, ignore_errors=True)
    def test_probes_against_a_fake_codex(self):
        d = tempfile.mkdtemp()
        try:
            fake = os.path.join(d, "codex")
            with open(fake, "w") as fh:
                fh.write('#!/bin/sh\ncase "$1" in\n --version) echo "codex-cli 9.9.9";;\n features) echo "goals stable true"; echo "apps stable true";;\n *) exit 3;;\nesac\n')
            os.chmod(fake, 0o755)
            ctx = cc.Ctx(); ctx.codex, ctx.expect, ctx.home, ctx.catalog, ctx.strata = fake, "9.9.9", None, None, None
            ctx.work = d; ctx.first_request = None; ctx.version = "?"
            self.assertEqual(cc.probe_version(ctx).status, "pass")
            ctx.expect = "1.2.3"; self.assertEqual(cc.probe_version(ctx).status, "fail")
            r = cc.probe_features(ctx); self.assertEqual(r.status, "fail"); self.assertIn("multi_agent", r.detail)         # a missing flag is named
            r = cc.probe_config(ctx); self.assertEqual(r.status, "fail"); self.assertIn("no request reached", r.detail)    # a codex that never calls the model
        finally: shutil.rmtree(d, ignore_errors=True)
    def _shortcuts_with(self, pairs):
        """probe_shortcuts with a fake interface: each fresh start sends two requests, whose efforts are the (start, end) pair given for it (no Codex, no waiting)."""
        import unittest.mock as um
        mocks, n = [], [0]
        class FakeMock:
            def __init__(self, *a): self.port, self.main = 1, []; mocks.append(self)
            def close(self): pass
        class FakeTui:
            def __init__(self, *a, **k): self._r, self.fd = os.pipe(); n[0] += 1; self.i = n[0] - 1; self.sent = 0
            def pump(self, sec): return ""
            def type_line(self, text): mocks[-1].main.append({"reasoning": {"effort": pairs[self.i][self.sent]}}); self.sent += 1
            def wait_for(self, fn, timeout): return fn()
            def close(self): os.close(self._r); os.close(self.fd)
        ctx = cc.Ctx(); ctx.codex, ctx.home, ctx.catalog, ctx.work = "x", None, None, tempfile.gettempdir()
        with um.patch.object(cc, "Mock", FakeMock), um.patch.object(cc, "Tui", FakeTui), um.patch.object(cc, "make_home", lambda *a, **k: tempfile.mkdtemp()), \
             um.patch.object(cc, "tui_args", lambda *a, **k: []):
            return cc.probe_shortcuts(ctx), n[0]
    def test_shortcuts_are_judged_from_where_the_level_started(self):
        for start, end in (("low", "medium"), ("none", "low"), ("medium", "medium"), ("high", "medium")):       # up, up, down from each start; the ends are clamped
            r, starts = self._shortcuts_with([(start, end)]); self.assertEqual((r.status, starts), ("pass", 1), (start, end))
            self.assertIn(f"from {start!r} to {end!r}", r.detail)
        self.assertEqual({k: cc.after_keys(k) for k in cc.LEVELS}, {"none": "low", "low": "medium", "medium": "medium", "high": "medium"})
    def test_a_start_at_none_used_to_be_called_a_failure(self):
        r, _ = self._shortcuts_with([("none", "low")]); self.assertEqual(r.status, "pass")                         # the old probe expected 'medium' whatever the start was
    def test_the_shortcuts_probe_retries_a_lost_key_but_not_a_moved_keymap(self):
        r, starts = self._shortcuts_with([("low", "low"), ("low", "medium")]); self.assertEqual((r.status, starts), ("warn", 2)); self.assertIn("attempt 2 of 3", r.detail)
        r, starts = self._shortcuts_with([("low", "low")] * 3); self.assertEqual((r.status, starts), ("fail", 3))
        self.assertIn("started at 'low'", r.detail); self.assertIn("expected 'medium'", r.detail); self.assertIn("starts seen", r.detail)    # says what it saw
        r, starts = self._shortcuts_with([("none", "none")] * 3); self.assertEqual(r.status, "fail"); self.assertIn("expected 'low'", r.detail)
    def test_a_first_request_with_no_level_is_reported_as_that(self):
        r, starts = self._shortcuts_with([(None, None)] * 3); self.assertEqual((r.status, starts), ("fail", 3)); self.assertIn("no reasoning level", r.detail)
    def test_the_probe_list_is_what_the_docs_say(self):
        names = [p.__name__[6:].replace("_", "-") for p in cc.PROBES]
        self.assertEqual(names, ["version", "config", "features", "tools", "rules", "tool-run", "loop-command", "interactive", "title", "shortcuts", "paste", "autopilot", "strata"])

if __name__ == "__main__":
    unittest.main(verbosity=2)
