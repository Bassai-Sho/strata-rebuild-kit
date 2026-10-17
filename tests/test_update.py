import http.server, json, os, shutil, subprocess, sys, tempfile, threading, unittest

TOOL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tools", "strata-update.py")
ENV = dict(GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
BUILD = ("v=$(grep -m1 -o 'project(strata VERSION [0-9.]*' CMakeLists.txt | grep -o '[0-9][0-9.]*$'); mkdir -p build-sycl; "
         "printf \"ELF engine=$v\\0\" > build-sycl/strata; echo BUILD EXIT 0")

DEVICE = "// device.hpp\nint probe() { return 1; }\n// integrated flag helpers\nbool is_integrated() { return false; }\n"
XE = "# xe_telemetry\ndef read_card():\n    return {}\n"

def tool(*args, env=None, stdin=""):
    r = subprocess.run([sys.executable, TOOL, *args], capture_output=True, text=True, env=env, input=stdin)
    return r.returncode, r.stdout + r.stderr

class Update(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(); self.up = os.path.join(self.root, "upstream"); self.ws = os.path.join(self.root, "Strata"); self.pd = os.path.join(self.root, "patches")
        self.home = os.path.join(self.root, "home"); os.makedirs(self.home); os.makedirs(self.pd)
        os.makedirs(self.up); self.git(self.up, "init", "-q")
        self.write(self.up, "setup.py", "MIN_ENGINE = (0, 1, 44, 1)   # base\n"); self.write(self.up, "CMakeLists.txt", "project(strata VERSION 0.1.44.1 LANGUAGES CXX)\n")
        self.write(self.up, "sycl/CMakeLists.txt", "# sycl\n"); self.write(self.up, "sycl/include/dpct/device.hpp", DEVICE); self.write(self.up, "sycl/serve/xe_telemetry.py", XE)
        self.write(self.up, "serve/server.py", "print('server')\n")
        self.commit_tag("v0.1.44.1")
        self.write(self.up, "setup.py", "MIN_ENGINE = (0, 1, 45)   # new\n"); self.write(self.up, "CMakeLists.txt", "project(strata VERSION 0.1.45 LANGUAGES CXX)\n")
        self.write(self.up, "serve/server.py", "print('server 0.1.45')\n"); self.commit_tag("v0.1.45")
        self.write(self.up, "sycl/include/dpct/device.hpp", "// rewritten upstream\nint probe() { return 2; }\n"); self.write(self.up, "CMakeLists.txt", "project(strata VERSION 0.1.46 LANGUAGES CXX)\n")
        self.commit_tag("v0.1.46")                                                       # device.hpp changed: the Meteor Lake patch will not apply
        # the three kit patches (made against 0.1.44.1)
        self.mkpatch("mtl-igpu-local.patch", "sycl/include/dpct/device.hpp", DEVICE, DEVICE + "// MTL: the integrated flag for PCI device 7d55 is set here for the iGPU\n")
        v2 = XE + "def gt_active_time(card):\n    return read_idle_residency(card) / 1000.0 if card else None\n"
        self.mkpatch("xe-monitor-v2.patch", "sycl/serve/xe_telemetry.py", XE, v2)
        self.mkpatch("xe-power.patch", "sycl/serve/xe_telemetry.py", v2, v2.replace("else None", "else 0.0") + "def package_power_watts(card):\n    return read_rapl_energy_delta(card) / elapsed_seconds()\n")
        # the working copy: 0.1.44.1 with the patches on, an engine, configs and a run script
        subprocess.run(["git", "clone", "-q", self.up, self.ws], check=True); self.git(self.ws, "checkout", "-q", "--detach", "v0.1.44.1")
        for n in ("mtl-igpu-local.patch", "xe-monitor-v2.patch", "xe-power.patch"): self.git(self.ws, "apply", os.path.join(self.pd, n))
        os.makedirs(os.path.join(self.ws, "build-sycl"))
        with open(os.path.join(self.ws, "build-sycl", "strata"), "wb") as fh: fh.write(b"ELF engine=0.1.44.1\0")
        self.write(self.ws, "strata-swift-iq3_xxs.json", json.dumps({"args": ["--prefill", "2048"], "reasoning_budget_tokens": 2048})); self.write(self.ws, "run-swift-iq3_xxs.sh", "#!/bin/sh\n")
        self.env = dict(os.environ, HOME=self.home, STRATA_BACKUPS=os.path.join(self.root, "backups"), STRATA_URL="http://127.0.0.1:1", STRATA_UPDATE_SKIP_DOCKER="1", STRATA_UPDATE_BUILD_CMD=BUILD)
    def tearDown(self): shutil.rmtree(self.root, ignore_errors=True)
    def git(self, d, *a): subprocess.run(["git", "-C", d, *a], check=True, capture_output=True, env=dict(os.environ, **ENV))
    def write(self, d, rel, text):
        p = os.path.join(d, rel); os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as fh: fh.write(text)
    def commit_tag(self, tag): self.git(self.up, "add", "-A"); self.git(self.up, "commit", "-qm", tag); self.git(self.up, "tag", tag)
    def mkpatch(self, name, path, old, new):
        import difflib
        with open(os.path.join(self.pd, name), "w") as fh: fh.write("".join(difflib.unified_diff(old.splitlines(True), new.splitlines(True), f"a/{path}", f"b/{path}")))
    def t(self, *args, stdin="", env=None): return tool(*args, "--strata", self.ws, "--patches", self.pd, env=env or self.env, stdin=stdin)
    def snap(self):
        r = lambda *a: subprocess.run(["git", "-C", self.ws, *a], capture_output=True, text=True).stdout.strip()
        with open(os.path.join(self.ws, "build-sycl", "strata"), "rb") as fh: eng = fh.read()
        with open(os.path.join(self.ws, "strata-swift-iq3_xxs.json")) as fh: cfg = fh.read()
        return (r("rev-parse", "HEAD"), r("diff"), eng, cfg)

    def test_check_is_read_only_and_says_ready_or_not(self):
        before = self.snap()
        rc, out = self.t("check", "--to", "v0.1.45", "--no-fetch"); self.assertEqual(rc, 0, out)
        for needle in ("applies cleanly", "VERDICT: ready", "Minimum engine version: 0.1.44.1 -> 0.1.45", "ACTION: a request with no sampling settings"): self.assertIn(needle, out)
        rc, out = self.t("check", "--to", "v0.1.46", "--no-fetch"); self.assertEqual(rc, 1); self.assertIn("DOES NOT APPLY", out); self.assertIn("NOT READY", out)
        self.assertEqual(self.snap(), before)                                            # nothing in the folder changed
        self.assertEqual(subprocess.run(["git", "-C", self.ws, "worktree", "list"], capture_output=True, text=True).stdout.count("\n"), 1)   # no scratch copy left
    def test_apply_switches_rebuilds_keeps_files_and_adds_the_sampling_block(self):
        rc, out = self.t("apply", "--to", "v0.1.45", "--yes"); self.assertEqual(rc, 0, out)
        self.assertIn("engine rebuilt: reports 0.1.45", out)
        head = subprocess.run(["git", "-C", self.ws, "describe", "--tags", "--exact-match"], capture_output=True, text=True).stdout.strip(); self.assertEqual(head, "v0.1.45")
        with open(os.path.join(self.ws, "sycl/serve/xe_telemetry.py")) as fh: self.assertIn("package_power_watts", fh.read())          # the patches are back on
        with open(os.path.join(self.ws, "sycl/include/dpct/device.hpp")) as fh: self.assertIn("MTL: the integrated flag", fh.read())
        self.assertTrue(os.path.exists(os.path.join(self.ws, "run-swift-iq3_xxs.sh")))
        with open(os.path.join(self.ws, "strata-swift-iq3_xxs.json")) as fh: c = json.load(fh)
        self.assertEqual(c["sampling"], {"temperature": 0, "presence_penalty": 0}); self.assertEqual(c["args"], ["--prefill", "2048"])
        b = os.path.join(self.root, "backups"); d = os.path.join(b, [x for x in os.listdir(b) if x.startswith("update-")][0])
        for f in ("state.json", "local-changes.diff", "strata-swift-iq3_xxs.json", "run-swift-iq3_xxs.sh", "strata-engine-0.1.44.1"): self.assertIn(f, os.listdir(d))
        with open(os.path.join(d, "strata-swift-iq3_xxs.json")) as fh: self.assertNotIn("sampling", json.load(fh))                       # the backup is the original
    def test_declining_the_settings_change_leaves_the_config_alone(self):
        rc, out = self.t("apply", "--to", "v0.1.45", "--no-compat", "--yes"); self.assertEqual(rc, 0, out)
        with open(os.path.join(self.ws, "strata-swift-iq3_xxs.json")) as fh: self.assertNotIn("sampling", json.load(fh))
    def test_rollback_restores_everything(self):
        before = self.snap(); self.assertEqual(self.t("apply", "--to", "v0.1.45", "--yes")[0], 0)
        rc, out = self.t("rollback", "--yes"); self.assertEqual(rc, 0, out); self.assertEqual(self.snap(), before)       # commit, patches, engine and config, byte for byte
        self.assertIn("already rolled back", self.t("rollback", "--yes")[1])
    def test_a_patch_that_stops_applying_undoes_the_whole_update(self):
        before = self.snap(); rc, out = self.t("apply", "--to", "v0.1.46", "--yes"); self.assertEqual(rc, 1, out)
        self.assertIn("does not apply to this version", out); self.assertIn("Back where you were", out); self.assertEqual(self.snap(), before)
    def test_a_failed_build_undoes_the_whole_update(self):
        before = self.snap(); env = dict(self.env, STRATA_UPDATE_BUILD_CMD="echo compile error >&2; exit 1")
        rc, out = self.t("apply", "--to", "v0.1.45", "--yes", env=env); self.assertEqual(rc, 1, out); self.assertIn("the engine build failed", out); self.assertEqual(self.snap(), before)
    def test_an_engine_that_reports_the_wrong_version_undoes_it(self):
        before = self.snap(); env = dict(self.env, STRATA_UPDATE_BUILD_CMD="mkdir -p build-sycl; printf 'engine=0.1.44.1\\0' > build-sycl/strata")
        rc, out = self.t("apply", "--to", "v0.1.45", "--yes", env=env); self.assertEqual(rc, 1, out); self.assertIn("expected '0.1.45'", out); self.assertEqual(self.snap(), before)
    def test_refusals(self):
        before = self.snap()
        self.assertEqual(self.t("apply", "--to", "v9.9.9", "--yes")[0], 2)
        rc, out = self.t("apply", "--to", "v0.1.45", stdin="no\n"); self.assertEqual(rc, 1); self.assertIn("Not confirmed", out)
        self.write(self.ws, "serve/server.py", "print('my experiment')\n")
        rc, out = self.t("apply", "--to", "v0.1.45", "--yes"); self.assertEqual(rc, 2); self.assertIn("do not explain", out); self.assertIn("serve/server.py", out)
        self.git(self.ws, "checkout", "--", "serve/server.py"); self.assertEqual(self.snap(), before)
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), type("H", (http.server.BaseHTTPRequestHandler,), {"log_message": lambda *a: None, "do_GET": lambda s: (s.send_response(200), s.end_headers(), s.wfile.write(b"{}"))}))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            rc, out = self.t("apply", "--to", "v0.1.45", "--yes", env=dict(self.env, STRATA_URL=f"http://127.0.0.1:{srv.server_address[1]}")); self.assertEqual(rc, 2); self.assertIn("Strata is running", out)
        finally: srv.shutdown(); srv.server_close()
    def test_force_sets_stray_changes_aside_after_saving_them(self):
        self.write(self.ws, "serve/server.py", "print('my experiment')\n")
        rc, out = self.t("apply", "--to", "v0.1.45", "--yes", "--force"); self.assertEqual(rc, 0, out)
        b = os.path.join(self.root, "backups"); d = os.path.join(b, [x for x in os.listdir(b) if x.startswith("update-")][0])
        with open(os.path.join(d, "local-changes.diff")) as fh: self.assertIn("my experiment", fh.read())          # saved before it was set aside
        with open(os.path.join(self.ws, "serve/server.py")) as fh: self.assertIn("0.1.45", fh.read())
    def test_already_at_the_target(self):
        rc, out = self.t("check", "--to", "v0.1.44.1", "--no-fetch"); self.assertIn("Already at v0.1.44.1", out)

if __name__ == "__main__":
    unittest.main(verbosity=2)
