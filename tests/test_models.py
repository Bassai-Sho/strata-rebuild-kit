import json, os, shutil, subprocess, sys, tempfile, unittest

TOOL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tools", "strata-models.py")

class Models(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(); self.home = os.path.join(self.root, "home"); os.makedirs(self.home)
        self.sw = os.path.join(self.root, "strata-work"); self.strata = os.path.join(self.sw, "Strata"); os.makedirs(self.strata)
        self.mk("models/IQ2_XS/Q-IQ2_XS-00001-of-00002.gguf", 4000); self.mk("models/IQ2_XS/Q-IQ2_XS-00002-of-00002.gguf", 3000)
        self.mk("data/models/swift-IQ3_XXS/S-IQ3_XXS-00001-of-00002.gguf", 5000); self.mk("data/models/swift-IQ3_XXS/S-IQ3_XXS-00002-of-00002.gguf", 4000)
        self.mk("data/packs/iq2_xs/tokenizer/tok.json", 100); self.mk("data/packs/swift-iq3_xxs/tokenizer/tok.json", 100)
        self.mk("data/mtp/rt/experts.bin", 800); self.mk("Strata/data/expert-profile-sycl.bin", 50)
        self.config("iq2_xs", "/work/models/IQ2_XS/Q-IQ2_XS-00001-of-00002.gguf", "/work/data/packs/iq2_xs")
        self.config("swift-iq3_xxs", "/work/data/models/swift-IQ3_XXS/S-IQ3_XXS-00001-of-00002.gguf", "/work/data/packs/swift-iq3_xxs")
    def tearDown(self): shutil.rmtree(self.root, ignore_errors=True)
    def mk(self, rel, size):
        p = os.path.join(self.sw, rel); os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as fh: fh.write(b"x" * size)
    def config(self, tag, native, pack, ple=None):
        cfg = {"sycl_root": self.sw, "log": f"{self.strata}/strata-{tag}.log", "tokenizer": f"{self.sw}/data/packs/{tag}/tokenizer", "reasoning_budget_tokens": 2048,
               "args": ["--pack", pack, "--native", native, "--ple-gguf", ple or native, "--expert-profile", "/work/Strata/data/expert-profile-sycl.bin", "--prefill", "2048",
                        "--mtp", "/work/data/mtp/rt", "--stream-experts", "--short-read", "64"], "env": {"STRATA_WINDOW_SYNC": "1"}}
        with open(os.path.join(self.strata, f"strata-{tag}.json"), "w") as fh: json.dump(cfg, fh)
        for ext in (f"run-{tag}.sh", f"strata-{tag}.log"):
            with open(os.path.join(self.strata, ext), "w") as fh: fh.write("x")
    def run_t(self, *args, stdin="", running=""):
        env = dict(os.environ, HOME=self.home, STRATA_MODELS_RUNNING=running)
        r = subprocess.run([sys.executable, TOOL, *args, "--strata", self.strata], input=stdin, capture_output=True, text=True, env=env)
        return r.returncode, r.stdout + r.stderr
    def exists(self, rel): return os.path.exists(os.path.join(self.sw, rel))
    def cfg(self, tag):
        with open(os.path.join(self.strata, f"strata-{tag}.json")) as fh: return json.load(fh)

    def test_list_shows_both_locations_and_what_is_shared(self):
        rc, out = self.run_t("list"); self.assertEqual(rc, 0, out)
        for needle in ("iq2_xs", "swift-iq3_xxs", "models/IQ2_XS", "data/models/swift-IQ3_XXS", "shared by 2 models"): self.assertIn(needle, out)
    def test_preview_deletes_nothing(self):
        rc, out = self.run_t("remove", "iq2_xs"); self.assertEqual(rc, 0, out)
        self.assertIn("PREVIEW", out); self.assertIn("delete", out); self.assertIn("This frees about", out)
        self.assertTrue(self.exists("models/IQ2_XS/Q-IQ2_XS-00001-of-00002.gguf")); self.assertTrue(os.path.exists(os.path.join(self.strata, "strata-iq2_xs.json")))
    def test_remove_one_keeps_the_other_and_the_shared_files(self):
        rc, out = self.run_t("remove", "iq2_xs", "--yes", stdin="iq2_xs\n"); self.assertEqual(rc, 0, out)
        self.assertFalse(self.exists("models/IQ2_XS")); self.assertFalse(self.exists("data/packs/iq2_xs"))
        self.assertFalse(os.path.exists(os.path.join(self.strata, "strata-iq2_xs.json"))); self.assertFalse(os.path.exists(os.path.join(self.strata, "run-iq2_xs.sh")))
        for keep in ("data/models/swift-IQ3_XXS/S-IQ3_XXS-00001-of-00002.gguf", "data/packs/swift-iq3_xxs", "data/mtp/rt/experts.bin", "Strata/data/expert-profile-sycl.bin"): self.assertTrue(self.exists(keep), keep)
        self.assertTrue(os.path.exists(os.path.join(self.strata, "strata-swift-iq3_xxs.json")))
        arch = os.path.join(self.home, "strata-backups", "removed-models"); self.assertEqual(len(os.listdir(arch)), 1)
        self.assertIn("strata-iq2_xs.json", os.listdir(os.path.join(arch, os.listdir(arch)[0])))                  # the settings can be restored
    def test_wrong_confirmation_deletes_nothing(self):
        rc, out = self.run_t("remove", "iq2_xs", "--yes", stdin="yes\n"); self.assertEqual(rc, 1); self.assertTrue(self.exists("models/IQ2_XS"))
    def test_a_running_model_cannot_be_removed(self):
        rc, out = self.run_t("remove", "swift-iq3_xxs", "--yes", stdin="swift-iq3_xxs\n", running="swift-iq3_xxs"); self.assertEqual(rc, 2); self.assertIn("is running", out)
        self.assertTrue(self.exists("data/models/swift-IQ3_XXS"))
    def test_a_file_another_model_uses_is_kept(self):
        self.config("swift-iq3_xxs", "/work/data/models/swift-IQ3_XXS/S-IQ3_XXS-00001-of-00002.gguf", "/work/data/packs/swift-iq3_xxs", ple="/work/models/IQ2_XS/Q-IQ2_XS-00002-of-00002.gguf")
        rc, out = self.run_t("remove", "iq2_xs", "--yes", stdin="iq2_xs\n"); self.assertEqual(rc, 0, out)
        self.assertTrue(self.exists("models/IQ2_XS/Q-IQ2_XS-00002-of-00002.gguf")); self.assertIn("also used by swift-iq3_xxs", out)   # a shared shard, like the Coder's
    def test_nothing_outside_the_data_folders_is_touched(self):
        outside = os.path.join(self.root, "elsewhere"); os.makedirs(outside); shutil.copy(__file__, os.path.join(outside, "m-00001-of-00001.gguf"))
        self.config("odd", os.path.join(outside, "m-00001-of-00001.gguf"), "/work/data/packs/odd")
        rc, out = self.run_t("remove", "odd", "--yes", stdin="odd\n"); self.assertIn("outside your Strata data folders", out)
        self.assertTrue(os.path.exists(os.path.join(outside, "m-00001-of-00001.gguf")))
    def test_a_shared_folder_loses_only_this_models_shards(self):
        self.mk("data/gguf-dir/Mine-IQ3_XXS-00001-of-00002.gguf", 500); self.mk("data/gguf-dir/Mine-IQ3_XXS-00002-of-00002.gguf", 500)
        self.mk("data/gguf-dir/Other-model-00001-of-00001.gguf", 700); self.mk("data/gguf-dir/notes.txt", 10)
        self.config("mine", "/work/data/gguf-dir/Mine-IQ3_XXS-00001-of-00002.gguf", "/work/data/packs/mine")
        rc, out = self.run_t("remove", "mine", "--yes", stdin="mine\n"); self.assertEqual(rc, 0, out)
        self.assertFalse(self.exists("data/gguf-dir/Mine-IQ3_XXS-00001-of-00002.gguf")); self.assertFalse(self.exists("data/gguf-dir/Mine-IQ3_XXS-00002-of-00002.gguf"))
        self.assertTrue(self.exists("data/gguf-dir/Other-model-00001-of-00001.gguf")); self.assertTrue(self.exists("data/gguf-dir/notes.txt"))   # the folder and its other files stay
    def test_set_changes_only_what_was_asked_and_backs_up(self):
        rc, out = self.run_t("set", "swift-iq3_xxs", "reasoning_budget_tokens=4096", "effort_position=end", "env.STRATA_MIRROR_MIB=12288"); self.assertEqual(rc, 0, out)
        c = self.cfg("swift-iq3_xxs"); self.assertEqual((c["reasoning_budget_tokens"], c["effort_position"], c["env"]["STRATA_MIRROR_MIB"]), (4096, "end", "12288"))
        self.assertEqual(c["env"]["STRATA_WINDOW_SYNC"], "1"); self.assertIn("--short-read", c["args"])
        self.assertEqual(len([f for f in os.listdir(self.strata) if ".bak-settings-" in f]), 1); self.assertIn("Restart Strata", out)
    def test_arg_replaces_adds_and_removes_engine_options(self):
        self.assertEqual(self.run_t("arg", "swift-iq3_xxs", "--prefill", "4096")[0], 0)
        a = self.cfg("swift-iq3_xxs")["args"]; self.assertEqual(a[a.index("--prefill") + 1], "4096"); self.assertEqual(a.count("--prefill"), 1)
        self.run_t("arg", "swift-iq3_xxs", "--spec", "4"); a = self.cfg("swift-iq3_xxs")["args"]; self.assertEqual(a[-2:], ["--spec", "4"])
        self.run_t("unarg", "swift-iq3_xxs", "--stream-experts"); a = self.cfg("swift-iq3_xxs")["args"]; self.assertNotIn("--stream-experts", a); self.assertIn("--short-read", a)
        rc, out = self.run_t("arg", "swift-iq3_xxs", "prefill", "1"); self.assertEqual(rc, 2)                       # not an option
    def test_show_and_errors(self):
        rc, out = self.run_t("show", "swift-iq3_xxs"); self.assertIn("--short-read 64", out); self.assertIn("STRATA_WINDOW_SYNC", out)
        self.assertEqual(self.run_t("remove", "nosuch")[0], 2); self.assertEqual(self.run_t("show")[0], 2)

if __name__ == "__main__":
    unittest.main(verbosity=2)
