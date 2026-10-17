import http.server, os, shutil, stat, subprocess, tempfile, textwrap, threading, unittest

KIT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
LAUNCHER = os.path.join(KIT, "agent", "codex-client")

class Up(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self): self.send_response(200); self.end_headers(); self.wfile.write(b"{}")

class Launcher(unittest.TestCase):
    """The launcher's argument handling, through its dry-run mode and a fake docker. No container, no Strata, no side effects."""
    @classmethod
    def setUpClass(cls):
        cls.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Up); threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
    @classmethod
    def tearDownClass(cls): cls.srv.shutdown(); cls.srv.server_close()
    def setUp(self):
        self.root = tempfile.mkdtemp(); self.home = os.path.join(self.root, "home"); self.agent = os.path.join(self.home, "agent")
        os.makedirs(os.path.join(self.agent, "codex-home")); os.makedirs(os.path.join(self.agent, "work")); os.makedirs(os.path.join(self.root, "bin"))
        with open(os.path.join(self.agent, "codex-home", "config.toml"), "w") as fh: fh.write('model = "strata"\n')
        fake = os.path.join(self.root, "bin", "docker")
        with open(fake, "w") as fh: fh.write('#!/bin/bash\ncase "$1" in image) exit 0;; ps) exit 0;; *) exit 0;; esac\n')
        os.chmod(fake, 0o755)
        self.log = os.path.join(self.root, "strata.log")
        with open(self.log, "w") as fh: fh.write("expert cache 24576 slots, 39.97 GiB\n")
        self.env = dict(os.environ, HOME=self.home, AGENT_DIR=self.agent, PATH=os.path.join(self.root, "bin") + ":" + os.environ["PATH"],
                        STRATA_URL=f"http://127.0.0.1:{self.srv.server_address[1]}", STRATA_LOG=self.log, STRATA_DIR=self.root, CODEX_DRY_RUN="1",
                        CODEX_FRONT_PORT="1")          # no filter binary in agent/bin: the launcher notes that and goes on without it
    def tearDown(self): shutil.rmtree(self.root, ignore_errors=True)
    def project(self, name, **files):
        d = os.path.join(self.agent, "work", name); os.makedirs(d, exist_ok=True)
        for f, text in files.items():
            with open(os.path.join(d, f.replace("_", ".", 1) if f.endswith("_md") else f), "w") as fh: fh.write(text)
    def run_l(self, *args):
        r = subprocess.run(["bash", LAUNCHER, *args], capture_output=True, text=True, env=self.env, stdin=subprocess.DEVNULL)
        cmd = next((l for l in r.stdout.splitlines() if l.startswith("docker")), "")
        return r.returncode, cmd.replace("\\ ", " ").replace('\\"', '"'), r.stdout + r.stderr
    def folders(self): return sorted(os.listdir(os.path.join(self.agent, "work")))

    def test_plain_and_sentence_start_a_new_project(self):
        for args in ((), ("build", "a", "snake", "game")):
            rc, cmd, out = self.run_l(*args); self.assertEqual(rc, 0, out); self.assertIn("-w /work ", cmd)
        self.assertIn("build a snake game", cmd); self.assertNotIn("--enable goals", cmd)
        self.assertEqual(self.folders(), [])                       # a sentence is not a project called "build"; a dry run creates nothing
    def test_name_and_prompt(self):
        self.project("mygame")
        for args in (("mygame", "add", "a", "score"), ("mygame", "add a score")):
            rc, cmd, out = self.run_l(*args); self.assertIn("-w /work/mygame ", cmd); self.assertTrue(cmd.rstrip().endswith("add a score"), cmd)
        rc, cmd, out = self.run_l("mygame", "resume"); self.assertIn("resume --last", cmd)
    def test_auto(self):
        rc, cmd, out = self.run_l("--auto", "build", "a", "snake", "game")
        self.assertIn("--enable goals", cmd); self.assertIn("-w /work ", cmd); self.assertIn("create_goal", cmd); self.assertIn("build a snake game", cmd)
        self.project("planned", PLAN_md="# Plan\n- [ ] T1: a | files: a | check: `x` prints OK\n"); self.project("seeded", SPEC_md="# Spec\ndesign\n"); self.project("empty")
        self.assertIn("Continue with PLAN.md", self.run_l("planned", "--auto")[1])
        self.assertIn("Start the project from SPEC.md", self.run_l("seeded", "--auto")[1])
        self.assertIn("First ask me what to build", self.run_l("empty", "--auto")[1])
        self.assertNotIn("--enable goals", self.run_l("planned", "resume", "--auto")[1])
    def test_unknown_options_are_errors_not_prompts(self):
        for args in (("--bogus",), ("mygame", "--bogus"), ("--auto", "--bogus")):
            rc, cmd, out = self.run_l(*args); self.assertNotEqual(rc, 0, args); self.assertIn("unknown option", out); self.assertEqual(cmd, "")
    def test_spec_needs_a_project_and_a_file(self):
        self.assertNotEqual(self.run_l("mygame", "--spec", "/nonexistent")[0], 0)
        self.assertNotEqual(self.run_l("--spec", "x.md")[0], 0)
    def test_dry_runs_leave_no_trace(self):
        self.run_l("brandnew"); self.run_l("brandnew", "--auto"); self.run_l("--auto", "x", "y")
        self.assertEqual(self.folders(), [])
        with open(os.path.join(self.agent, "codex-home", "config.toml")) as fh: self.assertNotIn("projects", fh.read())   # no trust entries written either
    def test_ls(self):
        self.project("snake", PLAN_md="# Plan\n- [x] T1: a\n- [ ] T2: b\n")
        rc, cmd, out = self.run_l("ls"); self.assertIn("snake", out); self.assertIn("1 of 2 tasks done", out)

if __name__ == "__main__":
    unittest.main(verbosity=2)
