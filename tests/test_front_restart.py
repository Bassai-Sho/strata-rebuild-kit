import os, re, shutil, signal, socket, subprocess, tempfile, time, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
AGENT_SRC = os.path.join(HERE, "..", "agent")
CLIENT = os.path.join(AGENT_SRC, "codex-client")


def functions(names):
    """The bodies of the named shell functions of the launcher (from `name() {` to the closing `}` at the start of a line)."""
    with open(CLIENT) as fh: text = fh.read()
    out = []
    for n in names:
        m = re.search(rf"(?m)^{n}\(\) \{{.*?^\}}$", text, re.S)
        assert m, n
        out.append(m.group(0))
    return "\n\n".join(out)


class FrontRestart(unittest.TestCase):
    """A running filter keeps the code it started with. After an update the launcher must replace it, but never while a session is using it."""
    def setUp(self):
        self.tmp = tempfile.mkdtemp(); self.agent = os.path.join(self.tmp, "agent"); os.makedirs(os.path.join(self.agent, "bin")); os.makedirs(os.path.join(self.agent, "run"))
        for f in ("strata-front.py", "apply_patch_rescue.py", "synth_compact.py"): shutil.copy(os.path.join(AGENT_SRC, f), os.path.join(self.agent, "bin", f))
        s = socket.socket(); s.bind(("127.0.0.1", 0)); self.port = s.getsockname()[1]; s.close()
        self.fakebin = os.path.join(self.tmp, "fakebin"); os.makedirs(self.fakebin)
        self.script = os.path.join(self.tmp, "t.sh")
        with open(self.script, "w") as fh:
            fh.write("set -u\nsay() { echo \"$@\"; }\nCONTAINER=strata-agent\n" + functions(["port_open", "listener_pids", "front_is_stale", "ensure_front"]) + "\nensure_front\necho \"FRONT=$FRONT\"\nlistener_pids \"$FRONT_PORT\"\n")
        self.env = dict(os.environ, AGENT=self.agent, FRONT_PORT=str(self.port), SOCK=os.path.join(self.agent, "run", "front.sock"), PORT="1", AGENT_DIR=self.agent,
                        PATH=self.fakebin + os.pathsep + os.environ["PATH"])
        self.pids = set()
    def tearDown(self):
        for p in self.pids:
            try: os.kill(p, signal.SIGTERM)
            except OSError: pass
        shutil.rmtree(self.tmp, ignore_errors=True)
    def ensure(self):
        r = subprocess.run(["bash", self.script], capture_output=True, text=True, env=self.env, timeout=60)
        lines = r.stdout.strip().splitlines(); pid = int(lines[-1].split()[0]) if lines and lines[-1].strip().split()[0].isdigit() else None
        if pid: self.pids.add(pid)
        return r, pid
    def fake_docker(self, running):
        with open(os.path.join(self.fakebin, "docker"), "w") as fh: fh.write("#!/bin/sh\n" + ("echo abc123\n" if running else "") + "exit 0\n")
        os.chmod(os.path.join(self.fakebin, "docker"), 0o755)
    def test_a_filter_older_than_its_files_is_replaced_and_a_current_one_is_left_alone(self):
        self.fake_docker(False)
        r, first = self.ensure(); self.assertIn("FRONT=1", r.stdout, r.stdout + r.stderr); self.assertIsNotNone(first)
        r, same = self.ensure(); self.assertEqual(same, first); self.assertNotIn("restarted", r.stdout)               # nothing changed: the same process
        later = time.time() + 5; os.utime(os.path.join(self.agent, "bin", "apply_patch_rescue.py"), (later, later))     # an update installed a newer file
        r, new = self.ensure(); self.assertIn("restarted it", r.stdout, r.stdout + r.stderr); self.assertIsNotNone(new); self.assertNotEqual(new, first)
        self.assertIn("FRONT=1", r.stdout)
        later2 = later + 5; os.utime(os.path.join(self.agent, "bin", "strata-front.py"), (later2, later2))
        r, third = self.ensure(); self.assertNotEqual(third, new)                                                        # each of the three files counts
    def test_a_running_session_keeps_its_filter(self):
        self.fake_docker(False)
        r, first = self.ensure(); self.assertIsNotNone(first)
        later = time.time() + 5; os.utime(os.path.join(self.agent, "bin", "synth_compact.py"), (later, later))
        self.fake_docker(True)                                                                                           # `docker ps` finds the session container
        r, same = self.ensure(); self.assertEqual(same, first); self.assertNotIn("restarted", r.stdout)
        self.fake_docker(False)
        r, new = self.ensure(); self.assertNotEqual(new, first); self.assertIn("restarted it", r.stdout)               # the session ended: now it is replaced


if __name__ == "__main__":
    unittest.main(verbosity=2)
