import importlib.util, json, os, re, shutil, subprocess, sys, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
KIT = os.path.join(HERE, "..")
INSTALLER = os.path.join(KIT, "agent", "install-codex-client.sh")


def read(*p):
    with open(os.path.join(KIT, *p)) as fh:
        return fh.read()


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class SlimCatalog(unittest.TestCase):
    def test_shipped_catalog_carries_the_slim_instructions(self):
        model = json.loads(read("agent", "model-catalog.json"))["models"][0]
        slim = read("agent", "slim-instructions.md")
        self.assertEqual(model["base_instructions"].strip(), slim.strip())
        self.assertTrue(model["base_instructions"].startswith("You are a coding agent running in the Codex CLI"))
        self.assertLess(len(model["base_instructions"]), 2000)             # about 500 tokens: Codex's own are about 4,200

    def test_slim_text_keeps_what_the_session_needs(self):
        slim = read("agent", "slim-instructions.md")
        for must in ("exec_command", "apply_patch", "*** End Patch", "check", "AGENTS.md", "PLAN.md", "on its own"):
            self.assertIn(must, slim)
        self.assertNotIn("request_user_input", slim)                        # the workspace rules already forbid it; one source

    def test_generator_makes_the_slim_catalog_without_codex(self):
        gen = os.path.join(KIT, "agent", "make-model-catalog.py")
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "c.json")
            r = subprocess.run([sys.executable, "-B", gen, "--out", out], capture_output=True, text=True, env=dict(os.environ, PATH="/nonexistent"))
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)             # no docker, no codex binary needed
            with open(out) as fh:
                made = json.load(fh)["models"][0]
        shipped = json.loads(read("agent", "model-catalog.json"))["models"][0]
        self.assertEqual(made, shipped)                                        # the shipped file is what the generator makes

    def test_generator_without_the_slim_file_says_what_to_do(self):
        with tempfile.TemporaryDirectory() as d:
            shutil.copy(os.path.join(KIT, "agent", "make-model-catalog.py"), d)
            r = subprocess.run([sys.executable, "-B", os.path.join(d, "make-model-catalog.py"), "--out", os.path.join(d, "c.json")], capture_output=True, text=True)
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("--stock", r.stdout + r.stderr)

    def test_the_tool_carries_the_same_text(self):
        tool = read("tools", "codex-slim.py")
        m = re.search(r'SLIM = """(.*?)"""\n', tool, re.S)
        self.assertEqual(m.group(1).strip(), read("agent", "slim-instructions.md").strip())


class InstallerDefaults(unittest.TestCase):
    """Run the real installer against a scratch ~/agent and look at what it writes."""
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d, True)
        self.agent = os.path.join(self.d, "agent"); self.home = os.path.join(self.d, "home")
        os.makedirs(os.path.join(self.agent, "codex-home")); os.makedirs(self.home)
        fake = os.path.join(self.d, "bin"); os.makedirs(fake)
        with open(os.path.join(fake, "docker"), "w") as fh:                  # no real docker call from a test
            fh.write("#!/bin/sh\nexit 1\n")
        os.chmod(os.path.join(fake, "docker"), 0o755)
        self.env = dict(os.environ, AGENT_DIR=self.agent, HOME=self.home, PATH=fake + os.pathsep + os.environ["PATH"])

    def run_installer(self, *args):
        return subprocess.run(["bash", INSTALLER, *args], capture_output=True, text=True, env=self.env)

    def cat(self, *p):
        with open(os.path.join(self.agent, *p)) as fh:
            return fh.read()

    def test_fresh_install_writes_the_new_defaults(self):
        r = self.run_installer()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        ws = self.cat("codex-home", "AGENTS.md"); tpl = self.cat("codex-home", "templates", "project", "AGENTS.md")
        self.assertTrue(ws.startswith("# Workspace rules")); self.assertIn("## Checks", ws); self.assertIn("## Plan review", ws)
        self.assertIn("Then stop.", ws)
        self.assertTrue(tpl.startswith("# Project rules"))                     # the template must NOT be the workspace rules (it is copied into every new project)
        self.assertNotIn("## Plan review", tpl); self.assertNotIn("## New project", tpl)
        self.assertLess(len(tpl), 1200)                                        # short: it is loaded next to the workspace rules in every session

    def test_template_check_line_is_what_codex_check_reads(self):
        self.run_installer()
        tpl = self.cat("codex-home", "templates", "project", "AGENTS.md")
        launcher = read("agent", "codex-client")
        sed = re.search(r"sed -n '(s/\^\.\*Check command\[\^:\]\*: \*//p)'", launcher)
        self.assertTrue(sed, "the launcher's way of reading the check line changed: update the template and this test")
        out = subprocess.run(["sed", "-n", sed.group(1)], input=tpl, capture_output=True, text=True).stdout.splitlines()
        self.assertEqual(out[0].strip(), "TO_BE_FILLED_IN")                     # the launcher refuses this placeholder
        self.assertEqual(len(out), 1)                                          # and nothing else in the file looks like a check line

    def test_the_slim_catalog_and_skills_off_are_installed(self):
        self.run_installer()
        model = json.loads(self.cat("codex-home", "model-catalog.json"))["models"][0]
        self.assertLess(len(model["base_instructions"]), 2000)
        self.assertTrue(os.path.isfile(os.path.join(self.agent, "bin", "slim-instructions.md")))
        import tomllib
        with open(os.path.join(self.agent, "codex-home", "config.toml"), "rb") as fh:
            cfg = tomllib.load(fh)
        self.assertIs(cfg["skills"]["bundled"]["enabled"], False)
        self.assertIs(cfg["features"]["goals"], False)                          # the older settings are still there

    def test_second_run_changes_nothing_and_check_is_clean(self):
        self.run_installer()
        again = self.run_installer()
        self.assertIn("config.toml already set", again.stdout)
        chk = self.run_installer("--check")
        self.assertNotIn("UPDATE", chk.stdout.replace("--update", ""), chk.stdout)   # the read-only check finds nothing to update

    def test_an_edited_workspace_file_is_kept_unless_update(self):
        self.run_installer()
        path = os.path.join(self.agent, "codex-home", "AGENTS.md")
        with open(path, "a") as fh:
            fh.write("my own rule\n")
        r = self.run_installer()
        self.assertIn("kept your edited", r.stdout)
        self.assertIn("my own rule", self.cat("codex-home", "AGENTS.md"))
        r = self.run_installer("--update")
        self.assertNotIn("my own rule", self.cat("codex-home", "AGENTS.md"))
        self.assertTrue(os.path.isfile(path + ".bak"))                          # the edited copy is kept

    def test_template_backups_never_land_inside_the_template_folder(self):
        self.run_installer()
        tpl = os.path.join(self.agent, "codex-home", "templates", "project", "AGENTS.md")
        with open(tpl, "a") as fh:
            fh.write("an edit\n")
        r = self.run_installer()                                               # kept: the newer default goes beside the backups, not into the template
        self.assertIn("kept your edited", r.stdout)
        r = self.run_installer("--update")
        self.assertIn("updated:", r.stdout)
        inside = [f for f in os.listdir(os.path.dirname(tpl)) if f.endswith((".bak", ".new"))]
        self.assertEqual(inside, [], inside)                                   # cp -r of this folder would copy them into every new project
        saved = os.listdir(os.path.join(self.agent, "backups"))
        self.assertTrue([f for f in saved if f.endswith(".template.bak")], saved)
        self.assertTrue([f for f in saved if f.endswith(".template.new")], saved)
        with open(os.path.join(self.agent, "backups", [f for f in saved if f.endswith(".template.bak")][0])) as fh:
            self.assertIn("an edit", fh.read())                                # the edited copy is what was saved

    def test_config_keeps_an_existing_skills_table_and_other_settings(self):
        with open(os.path.join(self.agent, "codex-home", "config.toml"), "w") as fh:
            fh.write('model = "strata"\n[skills.bundled]\nenabled = false\n[mine]\nx = 1\n')
        self.run_installer()
        import tomllib
        with open(os.path.join(self.agent, "codex-home", "config.toml"), "rb") as fh:
            cfg = tomllib.load(fh)
        self.assertEqual(cfg["mine"]["x"], 1); self.assertEqual(cfg["model"], "strata")
        text = self.cat("codex-home", "config.toml")
        self.assertEqual(text.count("[skills.bundled]"), 1)                       # not added twice


class SlimTool(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d, True)
        entry = json.loads(read("agent", "model-catalog.json"))["models"][0]
        entry["base_instructions"] = "You are a coding agent. " + "x" * 16000          # a stock-sized prompt
        with open(os.path.join(self.d, "model-catalog.json"), "w") as fh:
            json.dump({"models": [entry]}, fh)
        with open(os.path.join(self.d, "config.toml"), "w") as fh:
            fh.write('model_catalog_json = "/home/agent/.codex/model-catalog.json"\n[features]\ndaemon_auto_start = false\n')   # the container's path, as the installer writes it

    def run_tool(self, *args):
        return subprocess.run([sys.executable, "-B", os.path.join(KIT, "tools", "codex-slim.py"), *args, "--home", self.d], capture_output=True, text=True)

    def catalog(self):
        with open(os.path.join(self.d, "model-catalog.json")) as fh:
            return json.load(fh)["models"][0]

    def test_apply_maps_the_container_path_and_restore_undoes_it(self):
        r = self.run_tool("apply")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertLess(len(self.catalog()["base_instructions"]), 2000)
        with open(os.path.join(self.d, "config.toml")) as fh:
            cfg = fh.read()
        self.assertIn('model_catalog_json = "/home/agent/.codex/model-catalog.json"', cfg)     # the container path is left as it was
        self.assertEqual(cfg.count("[skills.bundled]"), 1)
        again = self.run_tool("apply")
        self.assertEqual(again.returncode, 0); self.assertIn("already present", again.stdout)
        with open(os.path.join(self.d, "config.toml")) as fh:
            self.assertEqual(fh.read().count("[skills.bundled]"), 1)
        self.assertEqual(self.run_tool("restore").returncode, 0)
        self.assertGreater(len(self.catalog()["base_instructions"]), 10000)
        with open(os.path.join(self.d, "config.toml")) as fh:
            self.assertNotIn("skills", fh.read())

    def test_wrong_home_is_a_message_not_a_traceback(self):
        r = subprocess.run([sys.executable, "-B", os.path.join(KIT, "tools", "codex-slim.py"), "apply", "--home", os.path.join(self.d, "nowhere")], capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0); self.assertIn("--home", r.stderr); self.assertNotIn("Traceback", r.stderr)

    def test_measure_refuses_the_launcher_script(self):
        script = os.path.join(self.d, "codex")
        with open(script, "w") as fh:
            fh.write("#!/bin/sh\necho hi\n")
        r = self.run_tool("measure", "--codex", script)
        self.assertNotEqual(r.returncode, 0); self.assertIn("launcher", r.stderr)


class EditFailures(unittest.TestCase):
    def test_counts_per_call_not_per_log_line_and_splits_loop_from_interactive(self):
        d = tempfile.mkdtemp(); self.addCleanup(shutil.rmtree, d, True)
        os.makedirs(os.path.join(d, "sessions")); os.makedirs(os.path.join(d, "logs", "loop-x", "home-1", "sessions"))
        ev = lambda p: json.dumps({"type": "response_item", "payload": p})
        call = lambda cid, cmd: ev({"type": "function_call", "name": "exec_command", "arguments": json.dumps({"cmd": cmd}), "call_id": cid})
        out = lambda cid, o: ev({"type": "function_call_output", "call_id": cid, "output": o})
        end = lambda cid, o: json.dumps({"type": "event_msg", "payload": {"type": "exec_command_end", "call_id": cid, "aggregated_output": o}})
        lines = [call("c1", "apply_patch <<'P'\n*** Begin Patch\nP"), out("c1", "Done!"),
                 call("c2", "apply_patch <<'P'\nx"), out("c2", "apply_patch verification failed: Failed to find expected lines in a.js: foo"), end("c2", "Failed to find expected lines in a.js: foo"),
                 call("c3", "apply_patch x\necho patched"), out("c3", "Invalid patch: The last line of the patch must be '*** End Patch' patched"), end("c3", "Invalid patch: The last line of the patch must be '*** End Patch'"),
                 call("c4", "ls"), out("c4", "a.js")]
        with open(os.path.join(d, "sessions", "rollout-a.jsonl"), "w") as fh:
            fh.write("\n".join(lines) + "\n")
        with open(os.path.join(d, "logs", "loop-x", "home-1", "sessions", "rollout-b.jsonl"), "w") as fh:
            fh.write("\n".join([call("l1", "apply_patch y"), out("l1", "Done!")]) + "\n")
        tool = os.path.join(KIT, "tools", "edit-failures.py")
        r = subprocess.run([sys.executable, "-B", tool, d], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertRegex(r.stdout, r"interactive\s+1 sessions\s+4 tool calls\s+3 edit attempts\s+2 failed")   # c2 and c3, each once
        self.assertRegex(r.stdout, r"loop\s+1 sessions\s+1 tool calls\s+1 edit attempts\s+0 failed")
        self.assertIn("lines not found", r.stdout); self.assertIn("bad patch format", r.stdout)
        r = subprocess.run([sys.executable, "-B", tool, d, "--since", "2999-01-01"], capture_output=True, text=True)
        self.assertIn("read 0 session files", r.stdout)
        r = subprocess.run([sys.executable, "-B", tool, d, "--since", "nonsense"], capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0); self.assertNotIn("Traceback", r.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
