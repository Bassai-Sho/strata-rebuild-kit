import hashlib, os, subprocess, sys, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
KS = next((os.path.join(HERE, c, "kit-sync.py") for c in ("../agent", "..", ".") if os.path.exists(os.path.join(HERE, c, "kit-sync.py"))), None)


def tree_hash(root):
    h = hashlib.sha256()
    for d, _, fs in sorted(os.walk(root)):
        for f in sorted(fs):
            p = os.path.join(d, f); h.update(p.encode()); h.update(open(p, "rb").read())
    return h.hexdigest()


class T(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.kit, self.live, self.base, self.bak = (os.path.join(self.d, n) for n in ("kit.txt", "live.txt", "base", "bak"))

    def w(self, p, text):
        with open(p, "w" if isinstance(text, str) else "wb") as fh: fh.write(text)
    def r(self, p):
        with open(p) as fh: return fh.read()
    def run_ks(self, cmd, *extra, mode="644"):
        before = tree_hash(self.d)
        r = subprocess.run([sys.executable, KS, cmd, "k/x", "--kit", self.kit, "--live", self.live, "--base", self.base, "--backups", self.bak, "--label", "the thing", "--mode", mode, *extra],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        if cmd == "check":
            self.assertEqual(tree_hash(self.d), before, "check must not change anything")
        return r.stdout
    def installed(self, text="a\nb\nc\n"):
        self.w(self.kit, text); self.run_ks("install")                       # first install: writes live and the baseline

    def test_first_install_and_ok(self):
        self.w(self.kit, "a\nb\n")
        self.assertIn("MISSING", self.run_ks("check")); self.assertIn("wrote:", self.run_ks("install"))
        self.assertEqual(self.r(self.live), "a\nb\n"); self.assertEqual(self.r(os.path.join(self.base, "k/x")), "a\nb\n")
        self.assertIn("ok", self.run_ks("check")); self.assertIn("ok:", self.run_ks("install"))
    def test_kit_changed_updates_live(self):
        self.installed(); self.w(self.kit, "a\nB\nc\n")
        self.assertIn("UPDATE", self.run_ks("check")); self.assertIn("updated:", self.run_ks("install"))
        self.assertEqual(self.r(self.live), "a\nB\nc\n"); self.assertTrue(os.listdir(self.bak))
    def test_live_changed_is_kept_and_pulled_into_the_kit(self):
        self.installed(); self.w(self.live, "a\nb\nc\nlesson\n")
        self.assertIn("PULL", self.run_ks("check"))
        out = self.run_ks("install")
        self.assertIn("pulled:", out); self.assertEqual(self.r(self.kit), "a\nb\nc\nlesson\n"); self.assertEqual(self.r(self.live), "a\nb\nc\nlesson\n")
        self.assertIn("ok", self.run_ks("check"))
    def test_no_pull_keeps_live_and_leaves_the_kit(self):
        self.installed(); self.w(self.live, "a\nb\nc\nlesson\n")
        self.assertIn("kept:", self.run_ks("install", "--no-pull")); self.assertEqual(self.r(self.kit), "a\nb\nc\n"); self.assertEqual(self.r(self.live), "a\nb\nc\nlesson\n")
    def test_both_changed_different_lines_merge(self):
        self.installed("1\n2\n3\n4\n5\n"); self.w(self.kit, "1 kit\n2\n3\n4\n5\n"); self.w(self.live, "1\n2\n3\n4\n5 live\n")
        self.assertIn("MERGE", self.run_ks("check")); self.assertIn("merged:", self.run_ks("install"))
        want = "1 kit\n2\n3\n4\n5 live\n"; self.assertEqual(self.r(self.live), want); self.assertEqual(self.r(self.kit), want)
    def test_both_changed_same_line_is_a_conflict_and_changes_nothing(self):
        self.installed("1\n2\n3\n"); self.w(self.kit, "1\nkit\n3\n"); self.w(self.live, "1\nlive\n3\n")
        self.assertIn("CONFLICT", self.run_ks("check")); out = self.run_ks("install"); self.assertIn("CONFLICT", out)
        self.assertEqual(self.r(self.live), "1\nlive\n3\n"); self.assertEqual(self.r(self.kit), "1\nkit\n3\n")
        saved = [f for f in os.listdir(self.bak) if f.endswith(".conflict")]; self.assertTrue(saved)
        self.assertIn("<<<<<<<", self.r(os.path.join(self.bak, saved[0])))
    def test_binary_both_changed_is_not_merged(self):
        self.w(self.kit, b"\0\1\2"); self.run_ks("install"); self.w(self.kit, b"\0\1\3"); self.w(self.live, b"\0\1\4")
        self.assertIn("CONFLICT", self.run_ks("install"))
    def test_no_history_keeps_live(self):
        self.w(self.kit, "kit\n"); self.w(self.live, "live\n")
        self.assertIn("UNKNOWN", self.run_ks("check")); self.assertIn("UNKNOWN", self.run_ks("install")); self.assertEqual(self.r(self.live), "live\n"); self.assertEqual(self.r(self.kit), "kit\n")
    def test_kit_wins_and_live_wins(self):
        self.w(self.kit, "kit\n"); self.w(self.live, "live\n"); self.run_ks("install", "--kit-wins")
        self.assertEqual(self.r(self.live), "kit\n"); self.assertTrue(os.listdir(self.bak))
        self.w(self.live, "live2\n"); self.run_ks("install", "--live-wins"); self.assertEqual(self.r(self.kit), "live2\n")
        self.assertIn("ok", self.run_ks("check"))
    def test_flags_override_even_when_both_changed(self):
        self.installed("1\n2\n3\n"); self.w(self.kit, "1\nkit\n3\n"); self.w(self.live, "1\nlive\n3\n"); self.run_ks("install", "--live-wins")
        self.assertEqual(self.r(self.kit), "1\nlive\n3\n")
    def test_mode_is_applied_on_write(self):
        self.w(self.kit, "#!/bin/sh\n"); self.run_ks("install", mode="755"); self.assertEqual(os.stat(self.live).st_mode & 0o777, 0o755)
    def test_a_file_missing_from_the_kit_is_skipped(self):
        self.w(self.live, "x\n"); self.assertIn("MISSING", self.run_ks("check")); self.assertEqual(self.run_ks("install"), ""); self.assertEqual(self.r(self.live), "x\n")
    def test_pull_keeps_the_kit_files_mode(self):
        self.w(self.kit, "#!/bin/sh\n"); os.chmod(self.kit, 0o755); self.run_ks("install", mode="755"); self.w(self.live, "#!/bin/sh\necho hi\n"); self.run_ks("install")
        self.assertEqual(os.stat(self.kit).st_mode & 0o777, 0o755)

if __name__ == "__main__":
    unittest.main(verbosity=1)
