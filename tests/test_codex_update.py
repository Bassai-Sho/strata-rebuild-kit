import io, json, os, shutil, stat, subprocess, sys, tarfile, tempfile, unittest

TOOL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tools", "codex-update.py")

FAKE_DOCKER = r'''#!/usr/bin/env python3
import json, os, shutil, subprocess, sys
S = os.environ["FAKE_DOCKER_STATE"]
st = json.load(open(S)); a = sys.argv[1:]
def save(): json.dump(st, open(S, "w"))
img = st["images"]
if a[:2] == ["image", "inspect"]: sys.exit(0 if a[2] in img else 1)
if a[:1] == ["ps"]: print("\n".join(st.get("running", []))); sys.exit(0)
if a[:1] == ["tag"]: img[a[2]] = dict(img[a[1]]); save(); sys.exit(0)
if a[:1] == ["run"]:
    name = next(x for x in a[2:] if x in img)
    rest = a[a.index(name) + 1:]
    if rest[:2] == ["codex", "--version"]: print("codex-cli " + img[name]["version"]); sys.exit(0)
    if rest[:1] == ["sh"]: sys.exit(0 if img[name].get("browser") else 1)
    if rest[:1] == ["cat"]: sys.stdout.buffer.write(("#!/bin/sh\necho codex-cli " + img[name]["version"] + "\n").encode()); sys.exit(0)
    sys.exit(0)
if a[:1] == ["build"]:
    ctx = a[-1]; tag = a[a.index("-t") + 1]
    if os.environ.get("FAKE_BUILD_FAIL"): print("build error", file=sys.stderr); sys.exit(1)
    base = [l.split()[1] for l in open(os.path.join(ctx, "Dockerfile")) if l.startswith("FROM")][0]
    new = dict(img[base]); out = subprocess.run([os.path.join(ctx, "codex"), "--version"], capture_output=True, text=True).stdout
    new["version"] = os.environ.get("FAKE_BUILD_VERSION") or out.split()[-1]
    if os.environ.get("FAKE_BUILD_DROP_BROWSER"): new["browser"] = False
    img[tag] = new; save(); sys.exit(0)
sys.exit(0)
'''

class Update(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(); self.bin = os.path.join(self.root, "bin"); os.makedirs(self.bin)
        with open(os.path.join(self.bin, "docker"), "w") as fh: fh.write(FAKE_DOCKER)
        os.chmod(os.path.join(self.bin, "docker"), 0o755)
        self.state = os.path.join(self.root, "docker.json"); self.write_state({"images": {"codex-sandbox": {"version": "0.162.1", "browser": True}}, "running": []})
        self.agent = os.path.join(self.root, "agent"); self.home = os.path.join(self.agent, "codex-home"); os.makedirs(self.home)
        for f, t in (("config.toml", "model = 'strata'\n"), ("AGENTS.md", "rules\n"), ("model-catalog.json", '{"models": ["old"]}')):
            with open(os.path.join(self.home, f), "w") as fh: fh.write(t)
        rel = os.path.join(self.root, "releases", "download", "rust-v0.163.0"); os.makedirs(rel)
        script = os.path.join(self.root, "codex-x86_64-unknown-linux-musl"); self.put(script, "#!/bin/sh\necho codex-cli 0.163.0\n"); os.chmod(script, 0o755)
        with tarfile.open(os.path.join(rel, "codex-x86_64-unknown-linux-musl.tar.gz"), "w:gz") as tf: tf.add(script, arcname="codex-x86_64-unknown-linux-musl")
        self.env = dict(os.environ, PATH=self.bin + ":" + os.environ["PATH"], FAKE_DOCKER_STATE=self.state, HOME=self.root, CODEX_UPDATE_BASE_URL="file://" + os.path.join(self.root, "releases"),
                        CODEX_UPDATE_CACHE=os.path.join(self.root, "cache"), STRATA_BACKUPS=os.path.join(self.root, "backups"), CODEX_UPDATE_LATEST="0.163.0", CODEX_UPDATE_PROBE_RESULT="pass")
    def tearDown(self): shutil.rmtree(self.root, ignore_errors=True)
    def put(self, path, text):
        with open(path, "w") as fh: fh.write(text)
    def write_state(self, st):
        with open(self.state, "w") as fh: json.dump(st, fh)
    def st(self):
        with open(self.state) as fh: return json.load(fh)
    def t(self, *args, env=None, stdin=""):
        r = subprocess.run([sys.executable, TOOL, *args, "--home", self.home, "--strata", "/nonexistent"], capture_output=True, text=True, env=env or self.env, input=stdin)
        return r.returncode, r.stdout + r.stderr
    def cat(self):
        with open(os.path.join(self.home, "model-catalog.json")) as fh: return fh.read()

    def test_check_reports_and_changes_nothing(self):
        before = self.st(); rc, out = self.t("check", "--no-baseline"); self.assertEqual(rc, 0, out)
        for n in ("Codex in the image:   0.162.1", "Newest stable:        0.163.0", "VERDICT: ready"): self.assertIn(n, out)
        self.assertEqual(self.st(), before)
        rc, out = self.t("check", "--no-baseline", env=dict(self.env, CODEX_UPDATE_PROBE_RESULT="fail")); self.assertEqual(rc, 1); self.assertIn("NOT READY", out)
    def test_already_at_the_target(self):
        self.write_state({"images": {"codex-sandbox": {"version": "0.163.0", "browser": True}}, "running": []})
        self.assertIn("already has that version", self.t("check", "--no-baseline")[1])
    def test_apply_swaps_the_layer_keeps_the_old_image_and_the_browser(self):
        rc, out = self.t("apply", "--yes"); self.assertEqual(rc, 0, out)
        st = self.st(); self.assertEqual(st["images"]["codex-sandbox"]["version"], "0.163.0"); self.assertTrue(st["images"]["codex-sandbox"]["browser"])
        self.assertEqual(st["images"]["codex-sandbox:codex-0.162.1"]["version"], "0.162.1")           # the rollback target
        with open(os.path.join(self.agent, ".codex-version")) as fh: self.assertEqual(fh.read().strip(), "0.163.0")
        b = os.path.join(self.root, "backups"); d = os.path.join(b, [x for x in os.listdir(b) if x.startswith("codex-update-")][0])
        self.assertIn("config.toml", os.listdir(d)); self.assertIn("state.json", os.listdir(d))
        self.assertEqual(self.cat(), '{"models": ["old"]}')                                           # identical catalog: left alone
    def test_a_different_catalog_is_replaced_and_the_old_one_kept(self):
        new = os.path.join(self.root, "newcat.json"); self.put(new, '{"models": ["new"]}')
        rc, out = self.t("apply", "--yes", env=dict(self.env, CODEX_UPDATE_FAKE_CATALOG=new)); self.assertEqual(rc, 0, out)
        self.assertIn("regenerates it", out); self.assertEqual(self.cat(), '{"models": ["new"]}')
        b = os.path.join(self.root, "backups"); d = os.path.join(b, [x for x in os.listdir(b) if x.startswith("codex-update-")][0])
        with open(os.path.join(d, "model-catalog.json")) as fh: self.assertEqual(fh.read(), '{"models": ["old"]}')
    def test_rollback_restores_image_and_catalog(self):
        new = os.path.join(self.root, "newcat.json"); self.put(new, '{"models": ["new"]}')
        self.t("apply", "--yes", env=dict(self.env, CODEX_UPDATE_FAKE_CATALOG=new))
        rc, out = self.t("rollback", "--yes"); self.assertEqual(rc, 0, out)
        self.assertEqual(self.st()["images"]["codex-sandbox"]["version"], "0.162.1"); self.assertEqual(self.cat(), '{"models": ["old"]}')
        self.assertIn("already rolled back", self.t("rollback", "--yes")[1])
    def test_a_failed_probe_blocks_apply_unless_forced(self):
        env = dict(self.env, CODEX_UPDATE_PROBE_RESULT="fail")
        rc, out = self.t("apply", "--yes", env=env); self.assertEqual(rc, 1); self.assertIn("Not applying", out); self.assertEqual(self.st()["images"]["codex-sandbox"]["version"], "0.162.1")
        rc, out = self.t("apply", "--yes", "--force", env=env); self.assertEqual(rc, 0, out); self.assertEqual(self.st()["images"]["codex-sandbox"]["version"], "0.163.0")
    def test_a_running_session_blocks_apply(self):
        self.write_state({"images": {"codex-sandbox": {"version": "0.162.1", "browser": True}}, "running": ["strata-agent"]})
        rc, out = self.t("apply", "--yes"); self.assertEqual(rc, 1); self.assertIn("running", out); self.assertEqual(self.st()["images"]["codex-sandbox"]["version"], "0.162.1")
    def test_a_failed_or_wrong_build_puts_the_old_image_back(self):
        for hook in ({"FAKE_BUILD_FAIL": "1"}, {"FAKE_BUILD_VERSION": "0.162.1"}, {"FAKE_BUILD_DROP_BROWSER": "1"}):
            self.write_state({"images": {"codex-sandbox": {"version": "0.162.1", "browser": True}}, "running": []})
            rc, out = self.t("apply", "--yes", env=dict(self.env, **hook)); self.assertEqual(rc, 1, (hook, out))
            self.assertEqual(self.st()["images"]["codex-sandbox"], {"version": "0.162.1", "browser": True}, hook)
    def test_wrong_confirmation_changes_nothing(self):
        rc, out = self.t("apply", stdin="0.162.1\n"); self.assertEqual(rc, 1); self.assertIn("Not confirmed", out); self.assertEqual(self.st()["images"]["codex-sandbox"]["version"], "0.162.1")

if __name__ == "__main__":
    unittest.main(verbosity=2)
