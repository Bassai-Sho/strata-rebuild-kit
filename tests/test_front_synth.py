import http.client, http.server, importlib.util, json, os, tempfile, threading, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "..", "agent", "strata-front.py")
spec = importlib.util.spec_from_file_location("front", SRC); front = importlib.util.module_from_spec(spec); spec.loader.exec_module(front)


class Upstream(http.server.BaseHTTPRequestHandler):
    seen = []
    def log_message(self, *a): pass
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0)); Upstream.seen.append(body)
        out = b'event: response.completed\ndata: {"from":"strata"}\n\n'
        self.send_response(200); self.send_header("Content-Type", "text/event-stream"); self.send_header("Content-Length", str(len(out))); self.end_headers(); self.wfile.write(out)


def msg(role, text, kind="input_text"):
    return {"type": "message", "role": role, "content": [{"type": kind, "text": text}]}


class Synth(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.up = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Upstream); threading.Thread(target=cls.up.serve_forever, daemon=True).start()
        front.Front.upstream = ("127.0.0.1", cls.up.server_address[1])
        cls.srv = front.Server(("127.0.0.1", 0), front.Front); threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.port = cls.srv.server_address[1]
    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown(); cls.up.shutdown(); cls.srv.server_close(); cls.up.server_close()
    def setUp(self):
        Upstream.seen.clear(); self.tmp = tempfile.mkdtemp(); front.WORK_ROOT = os.path.join(self.tmp, "work"); proj = os.path.join(front.WORK_ROOT, "demo"); os.makedirs(proj)
        with open(os.path.join(proj, "PLAN.md"), "w") as fh: fh.write("- [ ] T1: build it | files: a.js | check: `node a.js`\n")
        with open(os.path.join(proj, "progress.txt"), "w") as fh: fh.write("WIP: NEXT wire it\n")
        self.items = [msg("developer", "perm"), msg("user", "<environment_context>\n <cwd>/work/demo</cwd>\n</environment_context>"), msg("user", "go"),
                      msg("assistant", "Working on it.", "output_text")]
        os.environ.pop("STRATA_FRONT_SYNTH", None)
    def post(self, body):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10); c.request("POST", "/v1/responses", body=json.dumps(body), headers={"Content-Type": "application/json"})
        r = c.getresponse(); data = r.read().decode(); c.close(); return r.status, data
    def compaction(self):
        return {"input": self.items + [msg("user", front.synth_compact.MARK + "\nWrite a handoff.")], "stream": True}

    def test_compaction_is_answered_by_the_filter(self):
        status, data = self.post(self.compaction())
        self.assertEqual(status, 200); self.assertIn("Automatic handoff", data); self.assertIn("wire it", data); self.assertIn("response.completed", data)
        self.assertEqual(Upstream.seen, [])                                           # Strata was never asked
    def test_ordinary_requests_still_go_to_strata(self):
        status, data = self.post({"input": self.items + [msg("user", "next")]})
        self.assertIn('"from":"strata"', data); self.assertEqual(len(Upstream.seen), 1)
    def test_titles_still_answered(self):
        body = {"input": [msg("user", front.TITLE_MARK + ".\n\nUser prompt:\nbuild a game")], "client_metadata": {"x-codex-turn-metadata": json.dumps({"thread_source": "thread_title"})}}
        status, data = self.post(body); self.assertIn("Build a game", data); self.assertEqual(Upstream.seen, [])
    def test_nothing_useful_is_forwarded(self):
        self.items = [msg("user", "hi")]
        status, data = self.post(self.compaction()); self.assertIn('"from":"strata"', data); self.assertEqual(len(Upstream.seen), 1)
    def test_off_switch_file(self):
        open(os.path.join(front.WORK_ROOT, "NO-SYNTH-COMPACT"), "w").close()
        status, data = self.post(self.compaction()); self.assertIn('"from":"strata"', data)
    def test_off_switch_env(self):
        os.environ["STRATA_FRONT_SYNTH"] = "0"
        try:
            status, data = self.post(self.compaction()); self.assertIn('"from":"strata"', data)
        finally:
            os.environ.pop("STRATA_FRONT_SYNTH", None)
    def test_works_without_the_module(self):
        saved = front.synth_compact; front.synth_compact = None
        try:
            status, data = self.post({"input": self.items + [msg("user", "STRATA-HANDOFF\nx")]}); self.assertIn('"from":"strata"', data)
        finally:
            front.synth_compact = saved
    def test_a_crash_in_the_builder_forwards(self):
        orig = front.synth_compact.build_handoff
        front.synth_compact.build_handoff = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
        try:
            status, data = self.post(self.compaction()); self.assertIn('"from":"strata"', data)
        finally:
            front.synth_compact.build_handoff = orig

if __name__ == "__main__":
    unittest.main(verbosity=2)
