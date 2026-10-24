import http.client, http.server, importlib.util, json, os, shlex, subprocess, tempfile, threading, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
AGENT = os.path.join(HERE, "..", "agent")


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, os.path.join(AGENT, file)); m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


import sys
sys.path.insert(0, AGENT)
R = load("apply_patch_rescue", "apply_patch_rescue.py")
front = load("front", "strata-front.py")

SCREENSHOT = ("<tool_call>\n<function=apply_patch>\n<parameter=cmd>\napply_patch\n</parameter>\n</function>\n</tool_call>")      # as shown in the Codex session, 2026-10-23
PATCH = "*** Begin Patch\n*** Update File: src/game.js\n@@\n-old\n+new\n*** End Patch"


def ev(name, data): return f"event: {name}\ndata: {json.dumps(data)}\n\n".encode()


def reply(text=None, call=None, output=True):
    items = []
    if text is not None: items.append({"type": "message", "role": "assistant", "id": "m1", "content": [{"type": "output_text", "text": text}]})
    if call is not None: items.append({"type": "function_call", "id": "fc1", "call_id": "c1", "name": call[0], "arguments": json.dumps(call[1])})
    out = ev("response.created", {"type": "response.created", "response": {"id": "r1"}})
    for it in items: out += ev("response.output_item.done", {"type": "response.output_item.done", "item": it})
    done = {"type": "response.completed", "response": {"id": "r1", "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}}}
    if output: done["response"]["output"] = list(items)
    return out + ev("response.completed", done)


def run(stream_bytes, step=None):
    s = R.Stream(); out = b""
    if step is None: out += s.feed(stream_bytes)
    else:
        for i in range(0, len(stream_bytes), step): out += s.feed(stream_bytes[i:i + step])
    return out + s.finish(), s


def events(b):
    res = []
    for raw in b.decode().split("\n\n"):
        if not raw.strip(): continue
        name = next((l[6:].strip() for l in raw.split("\n") if l.startswith("event:")), ""); data = next((json.loads(l[5:]) for l in raw.split("\n") if l.startswith("data:")), None)
        res.append((name, data))
    return res


def calls(b): return [d["item"] for n, d in events(b) if n == "response.output_item.done" and d["item"]["type"] == "function_call"]


class Transform(unittest.TestCase):
    def test_the_call_from_the_session_becomes_a_real_exec_command_that_tells_the_model_what_to_do(self):
        out, s = run(reply(SCREENSHOT)); c = calls(out)
        self.assertEqual(len(c), 1); self.assertEqual(c[0]["name"], "exec_command"); self.assertEqual(s.rescued, 1)
        cmd = json.loads(c[0]["arguments"])["cmd"]
        self.assertIn("exit 2", cmd)
        shown = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True)                      # what the model will read back
        self.assertEqual(shown.returncode, 2); self.assertEqual(shown.stdout.strip(), R.GUIDE)
        self.assertIn("apply_patch <<'PATCH'", shown.stdout); self.assertIn("*** End Patch", shown.stdout); self.assertIn("exec_command", shown.stdout)
        names = [n for n, _ in events(out)]
        self.assertEqual(names, ["response.created", "response.output_item.done", "response.output_item.done", "response.completed"])      # the call comes before the end
        done = events(out)[-1][1]["response"]; self.assertEqual(done["output"][-1]["name"], "exec_command")                              # the end lists it too
    def test_a_block_that_carries_a_patch_runs_the_patch(self):
        for param in ("input", "patch", "cmd"):
            out, _ = run(reply(f"<tool_call>\n<function=apply_patch>\n<parameter={param}>\n{PATCH}\n</parameter>\n</function>\n</tool_call>"))
            self.assertEqual(json.loads(calls(out)[0]["arguments"])["cmd"], "apply_patch <<'PATCH'\n" + PATCH + "\nPATCH", param)
        out, _ = run(reply("<tool_call>\n<function=apply_patch>\n<parameter=input>\n" + PATCH.replace("\n*** End Patch", "") + "\n</parameter>\n</function>\n</tool_call>"))
        self.assertTrue(json.loads(calls(out)[0]["arguments"])["cmd"].endswith("*** End Patch\nPATCH"))        # a missing end line is added
    def test_a_whole_command_written_in_the_block_is_used_as_written(self):
        whole = "apply_patch <<'PATCH'\n" + PATCH + "\nPATCH"
        out, _ = run(reply(f"<tool_call>\n<function=apply_patch>\n<parameter=cmd>\n{whole}\n</parameter>\n</function>\n</tool_call>"))
        self.assertEqual(json.loads(calls(out)[0]["arguments"])["cmd"], whole)
    def test_the_command_really_applies_the_patch(self):
        d = tempfile.mkdtemp(); fake = os.path.join(d, "apply_patch")                                    # a stand-in that records what it was given
        with open(fake, "w") as fh: fh.write("#!/bin/sh\ncat > got.txt\n")
        os.chmod(fake, 0o755)
        out, _ = run(reply("<tool_call>\n<function=apply_patch>\n<parameter=input>\n" + PATCH + "\n</parameter>\n</function>\n</tool_call>"))
        subprocess.run(["bash", "-c", json.loads(calls(out)[0]["arguments"])["cmd"]], cwd=d, env={"PATH": d + ":/usr/bin:/bin"}, check=True)
        with open(os.path.join(d, "got.txt")) as fh: self.assertEqual(fh.read().strip(), PATCH)
    def test_text_before_the_block_does_not_stop_it(self):
        out, _ = run(reply("I will patch the file now.\n\n" + SCREENSHOT)); self.assertEqual(len(calls(out)), 1)
    def test_prose_after_the_block_or_a_real_call_beside_it_or_other_names_are_left_alone(self):
        for text, call in ((SCREENSHOT + "\nThat is how the call would look.", None), ("The call is <function=apply_patch> in the template.", None),
                           ("<tool_call>\n<function=read_file>\n<parameter=path>\na.js\n</parameter>\n</function>\n</tool_call>", None),
                           ("Applying it.\n" + SCREENSHOT, ("exec_command", {"cmd": "ls"})), ("Nothing to do here.", None)):
            raw = reply(text, call); out, s = run(raw); self.assertEqual(out, raw, text); self.assertEqual(s.rescued, 0)         # byte for byte
    def test_only_the_last_message_of_a_response_counts_and_each_response_starts_clean(self):
        raw = reply(SCREENSHOT) + reply("Done.")
        out, s = run(raw); self.assertEqual(len(calls(out)), 1); self.assertEqual(s.rescued, 1)
    def test_chunk_boundaries_do_not_matter(self):
        import re
        norm = lambda b: re.sub(rb"_rescue_[0-9a-f]{8}", b"_rescue_X", b)                                   # the ids are random
        raw = reply(SCREENSHOT); whole, _ = run(raw)
        for step in (1, 3, 7, 50, 1000): self.assertEqual(norm(run(raw, step)[0]), norm(whole), step)
    def test_without_an_output_list_in_the_end_event_the_call_is_still_added(self):
        out, _ = run(reply(SCREENSHOT, output=False)); self.assertEqual(len(calls(out)), 1)
    def test_garbage_and_non_event_data_pass_through(self):
        for raw in (b"", b"not an event", b"data: {broken\n\n", b": keepalive\n\n", b"event: response.completed\ndata: {}\n\n"):
            out, _ = run(raw); self.assertEqual(out, raw)


class Upstream(http.server.BaseHTTPRequestHandler):
    body = b""; ctype = "text/event-stream"
    def log_message(self, *a): pass
    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.send_response(200); self.send_header("Content-Type", Upstream.ctype); self.send_header("Content-Length", str(len(Upstream.body))); self.end_headers(); self.wfile.write(Upstream.body)


class ThroughTheFront(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.up = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Upstream); threading.Thread(target=cls.up.serve_forever, daemon=True).start()
        front.Front.upstream = ("127.0.0.1", cls.up.server_address[1])
        cls.srv = front.Server(("127.0.0.1", 0), front.Front); threading.Thread(target=cls.srv.serve_forever, daemon=True).start(); cls.port = cls.srv.server_address[1]
    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown(); cls.up.shutdown(); cls.srv.server_close(); cls.up.server_close()          # close the listening sockets, not only stop the loops
    def setUp(self):
        self.tmp = tempfile.mkdtemp(); front.WORK_ROOT = os.path.join(self.tmp, "work"); os.makedirs(front.WORK_ROOT); os.environ.pop("STRATA_FRONT_APPLY_PATCH", None)
        Upstream.ctype = "text/event-stream"
    def post(self, path="/v1/responses"):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10); c.request("POST", path, body=json.dumps({"input": [], "stream": True}), headers={"Content-Type": "application/json"})
        r = c.getresponse(); data = r.read(); c.close(); return r.status, data
    def test_the_repair_happens_in_the_filter(self):
        Upstream.body = reply(SCREENSHOT); status, data = self.post(); self.assertEqual(status, 200)
        self.assertEqual(len(calls(data)), 1); self.assertEqual(calls(data)[0]["name"], "exec_command")
    def test_an_ordinary_reply_is_forwarded_unchanged(self):
        Upstream.body = reply("All done."); self.assertEqual(self.post()[1], Upstream.body)
    def test_the_off_switches(self):
        Upstream.body = reply(SCREENSHOT)
        os.environ["STRATA_FRONT_APPLY_PATCH"] = "0"
        try: self.assertEqual(self.post()[1], Upstream.body)
        finally: os.environ.pop("STRATA_FRONT_APPLY_PATCH", None)
        with open(os.path.join(front.WORK_ROOT, "NO-APPLY-PATCH-RESCUE"), "w"): pass
        self.assertEqual(self.post()[1], Upstream.body)
    def test_only_a_streamed_responses_reply_is_touched(self):
        Upstream.body = reply(SCREENSHOT)
        self.assertEqual(self.post("/v1/other")[1], Upstream.body)                                      # another path
        Upstream.ctype = "application/json"; self.assertEqual(self.post()[1], Upstream.body)            # not an event stream
    def test_the_filter_still_works_when_the_module_is_missing(self):
        saved = front.apply_patch_rescue; front.apply_patch_rescue = None
        try:
            Upstream.body = reply(SCREENSHOT); self.assertEqual(self.post()[1], Upstream.body)
        finally: front.apply_patch_rescue = saved


if __name__ == "__main__":
    unittest.main(verbosity=2)
