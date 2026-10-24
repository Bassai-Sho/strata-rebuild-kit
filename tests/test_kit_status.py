import os, shutil, subprocess, tempfile, unittest

KIT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")

BASE = "def read_value(path):\n    return open(path).read()\n"
SUB = "def card_subtitle(card):\n    return [gpu_name(card), clock_text(card), throttle_text(card)]\n"             # the card's second line, with the throttle note
SUB_QUIET = "def card_subtitle(card):\n    return [gpu_name(card), clock_text(card)]\n"                             # ... and without it (what the quiet patch leaves)
V2 = "def gt_active_time(card):\n    return read_idle_residency(card) / 1000.0 if card else None\n" + SUB      # added by the dashboard patch
POWER = "def package_power_watts(card):\n    return read_rapl_energy_delta(card) / elapsed_seconds()\n"        # added by the power patch
POWER_CHANGES = "def gt_active_time(card):\n    return read_idle_residency(card) / 1000.0 if card else 0.0\n" + SUB   # ...which also rewrites a line the dashboard patch added
STACK = BASE + POWER_CHANGES + POWER                                                                              # all of the above, on top of each other
QUIET_STACK = STACK.replace(SUB, SUB_QUIET)                                                                       # the quiet patch, on top of those

def diff(old, new, path="serve/xe.py"):
    import difflib
    return "".join(difflib.unified_diff(old.splitlines(True), new.splitlines(True), f"a/{path}", f"b/{path}"))

class PatchState(unittest.TestCase):
    """kit.sh recognises each patch even when a later patch sits on top of it (git apply -R cannot)."""
    def setUp(self):
        self.root = tempfile.mkdtemp(); self.kit = os.path.join(self.root, "kit"); self.repo = os.path.join(self.root, "repo")
        os.makedirs(os.path.join(self.kit, "patches")); os.makedirs(os.path.join(self.repo, "serve"))
        shutil.copy(os.path.join(KIT, "kit.sh"), self.kit)
        self.write_repo(BASE)
        for name, new in (("mtl-igpu-local.patch", BASE + "# mtl: integrated flag for PCI 7d55 is set here\n"), ("xe-monitor-v2.patch", BASE + V2),
                          ("xe-power.patch", STACK), ("xe-monitor-quiet.patch", QUIET_STACK)):
            with open(os.path.join(self.kit, "patches", name), "w") as fh: fh.write(self.patch_for(name, new))
        subprocess.run(["git", "init", "-q"], cwd=self.repo); subprocess.run(["git", "add", "-A"], cwd=self.repo)
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base"], cwd=self.repo)
    def tearDown(self): shutil.rmtree(self.root, ignore_errors=True)
    def patch_for(self, name, new):
        base = {"xe-power.patch": BASE + V2, "xe-monitor-quiet.patch": STACK}.get(name, BASE)      # the power patch is made against the dashboard patch's result, the quiet patch against both
        return diff(base, new)
    def write_repo(self, text):
        with open(os.path.join(self.repo, "serve", "xe.py"), "w") as fh: fh.write(text)
    def states(self):
        env = dict(os.environ, HOME=self.root, STRATA_DIR=self.repo, AGENT_DIR=os.path.join(self.root, "agent"), STRATA_URL="http://127.0.0.1:1")
        out = subprocess.run(["bash", os.path.join(self.kit, "kit.sh"), "status"], capture_output=True, text=True, env=env).stdout
        pick = lambda label: next(l.split(":", 1)[1].strip() for l in out.splitlines() if l.startswith(label))
        self.quiet_state = pick("Throttle note hidden")
        return pick("Meteor Lake patch"), pick("Dashboard readings"), pick("Package power")
    def test_clean(self):
        mtl, dash, power = self.states()
        self.assertTrue(mtl.startswith("not applied; it applies cleanly"), mtl); self.assertTrue(dash.startswith("not applied; it applies cleanly"), dash)
        self.assertTrue(power.startswith("not applied"), power)
    def test_dashboard_only(self):
        self.write_repo(BASE + V2); mtl, dash, power = self.states()
        self.assertEqual(dash, "applied"); self.assertTrue(power.startswith("not applied; it applies cleanly"), power)
    def test_both_stacked_is_recognised_as_both(self):
        self.write_repo(BASE + POWER_CHANGES + POWER); mtl, dash, power = self.states()
        self.assertTrue(dash.startswith("applied"), dash); self.assertEqual(power, "applied")     # the old reverse-check said the dashboard patch was NOT applied here
    def test_quiet_patch_is_told_apart_from_the_stack_below_it(self):
        self.write_repo(STACK); self.states(); self.assertTrue(self.quiet_state.startswith("not applied; it applies cleanly"), self.quiet_state)   # dashboard + power, no quiet patch
        self.write_repo(QUIET_STACK); mtl, dash, power = self.states()
        self.assertEqual(self.quiet_state, "applied"); self.assertTrue(dash.startswith("applied"), dash); self.assertEqual(power, "applied")
    def test_quiet_patch_with_nothing_else_is_reported_with_its_foundation(self):
        self.write_repo(BASE + V2.replace(SUB, SUB_QUIET)); mtl, dash, power = self.states()                   # the dashboard patch's lines, minus the throttle note
        self.assertTrue(dash.startswith("applied"), dash)
    def test_a_patch_remade_for_a_newer_line_is_recognised_whichever_file_fits(self):
        line = "# mtl 0.2: the integrated flag for PCI 7d55, remade for the 0.2 code\n"
        base_b = BASE.replace("def read_value(path):", "def read_value(path, mode=None):")                         # the 0.2 code: a line next to the patch was rewritten
        with open(os.path.join(self.kit, "patches", "mtl-igpu-local.0.2.patch"), "w") as fh: fh.write(diff(base_b, base_b + line))
        self.write_repo(base_b + line); self.assertEqual(self.states()[0], "applied")                          # the remade patch is on: only the variant says so
        self.write_repo(base_b); mtl = self.states()[0]
        self.assertTrue(mtl.startswith("not applied; it applies cleanly"), mtl)                                 # the first file does not fit this tree, the variant does
        self.write_repo(BASE + "# mtl: integrated flag for PCI 7d55 is set here\n"); self.assertEqual(self.states()[0], "applied")   # the original is still recognised with the variant beside it
    def test_meteor_lake(self):
        self.write_repo(BASE + "# mtl: integrated flag for PCI 7d55 is set here\n"); self.assertEqual(self.states()[0], "applied")

if __name__ == "__main__":
    unittest.main(verbosity=2)
