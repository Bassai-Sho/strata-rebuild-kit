import importlib.util, json, os, shutil, subprocess, sys, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
KIT = os.path.join(HERE, "..")
STRATA_ACCEPTS = {"none", "low", "medium", "high"}     # serve/frontend.py EFFORT: none/off/minimal -> thinking off; low; medium; high/xhigh/max -> xhigh

def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path); m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m

class Catalog(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(KIT, "agent", "model-catalog.json")) as fh: self.model = json.load(fh)["models"][0]
    def test_levels_are_ones_strata_accepts(self):
        levels = [l["effort"] for l in self.model["supported_reasoning_levels"]]
        self.assertEqual(levels, ["none", "low", "medium", "high"]); self.assertTrue(set(levels) <= STRATA_ACCEPTS)   # an unknown level would be a 400 from Strata
        self.assertEqual(self.model["default_reasoning_level"], "low")
    def test_tools_stay_as_they_are_today(self):
        self.assertEqual(self.model["slug"], "strata")
        self.assertEqual(self.model["tool_mode"], "direct")                     # code_mode_only hides every function tool behind a JavaScript sandbox
        self.assertFalse(self.model["use_responses_lite"]); self.assertFalse(self.model["prefer_websockets"])
        self.assertIsNone(self.model["apply_patch_tool_type"]); self.assertEqual(self.model["input_modalities"], ["text"])
    def test_base_instructions_and_window(self):
        self.assertTrue(self.model["base_instructions"].startswith("You are a coding agent running in the Codex CLI"))
        self.assertEqual((self.model["context_window"], self.model["auto_compact_token_limit"]), (65536, 48000))
    def test_generator_template_matches_the_shipped_catalog(self):
        gen = load(os.path.join(KIT, "agent", "make-model-catalog.py"), "gen")
        shipped = dict(self.model); shipped.pop("base_instructions"); shipped.pop("supported_reasoning_levels")
        template = dict(gen.ENTRY); template.pop("supported_reasoning_levels", None)
        self.assertEqual(shipped, template)
        self.assertEqual([e for e, _ in gen.LEVELS], ["none", "low", "medium", "high"])

class LoopHome(unittest.TestCase):
    def test_a_session_home_carries_the_catalog(self):
        with tempfile.TemporaryDirectory() as agent:
            home = os.path.join(agent, "codex-home"); os.makedirs(os.path.join(home, "tools"))
            for f in ("config.toml", "AGENTS.md", "model-catalog.json"):
                with open(os.path.join(home, f), "w") as fh: fh.write("x")
            os.environ["AGENT_DIR"] = agent
            loop = load(os.path.join(KIT, "agent", "codex-loop.py"), "loopmod")
            made = loop.make_home(os.path.join(agent, "logs"), 1)
            self.assertTrue(os.path.isfile(os.path.join(made, "model-catalog.json")))   # without it the loop's Codex could not read the config key

class EffortPosition(unittest.TestCase):
    def run_tool(self, cfg, *args):
        return subprocess.run([sys.executable, os.path.join(KIT, "tools", "strata-effort-position.py"), *args, "--config", cfg], capture_output=True, text=True)
    def test_end_start_roundtrip_with_backup(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = os.path.join(d, "strata.json")
            with open(cfg, "w") as fh: json.dump({"reasoning_budget_tokens": 2048, "args": ["--prefill", "2048"]}, fh)
            r = self.run_tool(cfg, "end"); self.assertEqual(r.returncode, 0, r.stdout)
            with open(cfg) as fh: c = json.load(fh)
            self.assertEqual(c["effort_position"], "end"); self.assertEqual(c["reasoning_budget_tokens"], 2048); self.assertEqual(c["args"], ["--prefill", "2048"])   # nothing else touched
            self.assertEqual(len([f for f in os.listdir(d) if ".bak-effort-" in f]), 1)
            self.assertIn("already set", self.run_tool(cfg, "end").stdout)
            r = self.run_tool(cfg, "start")
            with open(cfg) as fh: self.assertNotIn("effort_position", json.load(fh))
            self.assertIn("'start'", self.run_tool(cfg, "show").stdout)
    def test_unreadable_config_is_reported(self):
        r = self.run_tool("/nonexistent/x.json", "show"); self.assertEqual(r.returncode, 2); self.assertIn("cannot read", r.stdout)

if __name__ == "__main__":
    unittest.main(verbosity=2)
