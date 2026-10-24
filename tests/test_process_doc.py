import glob, os, re, unittest

KIT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")

def read(path):
    with open(path) as fh: return fh.read()

class ProcessDoc(unittest.TestCase):
    """MAINTAINER-PROMPT.md must describe the kit as it is: a future session is told to rely on it."""
    def setUp(self): self.doc = read(os.path.join(KIT, "MAINTAINER-PROMPT.md"))

    def test_every_script_and_tool_is_named(self):
        missing = []
        for sub in ("tools", "agent"):
            for p in sorted(glob.glob(os.path.join(KIT, sub, "*"))):
                name = os.path.basename(p)
                if os.path.isfile(p) and not name.startswith(".") and name not in self.doc: missing.append(f"{sub}/{name}")
        self.assertEqual(missing, [], "add these to the kit table in MAINTAINER-PROMPT.md section 3")
    def test_every_environment_hook_it_names_exists_in_the_code(self):
        code = ""
        for sub in ("tools", "agent", "tests"):
            for p in glob.glob(os.path.join(KIT, sub, "*")):
                if os.path.isfile(p): code += read(p)
        section = self.doc.split("**Test hooks**", 1)[1].split("- **Before/after fingerprints**", 1)[0]      # only the hooks list (CODEX_VERSION elsewhere is a file name)
        hooks = set(re.findall(r"`((?:CODEX|STRATA)_[A-Z_]+)`", section))
        self.assertTrue(hooks, "the prompt lists no hooks")
        self.assertEqual([h for h in sorted(hooks) if h not in code], [])
    def test_the_standard_commands_and_sections_are_there(self):
        for needle in ('tar xzf "$(ls -t ~/Downloads/strata-rebuild-kit*.tar.gz | head -1)" -C ~', "python3 -W error::ResourceWarning -m unittest discover -s tests",
                       "kit.sh", "SHA256SUMS", "VERSION", "CHANGES.md", "codex_compat.py", "strata-update.py", "codex-update.py"): self.assertTrue(needle in self.doc, f"the prompt does not mention: {needle}")
        for n in range(1, 12): self.assertTrue(re.search(rf"(?m)^## {n}\. ", self.doc), f"section {n} is missing")
    def test_the_loop_guarantees_it_states_are_the_ones_the_loop_implements(self):
        loop = read(os.path.join(KIT, "agent", "codex-loop.py"))
        for flag in ("--allow-new-files", "--clean-retry"):
            self.assertTrue(flag in self.doc, f"the prompt does not mention {flag}"); self.assertTrue(flag in loop, f"the loop has no {flag}")
        for name in ("conftest.py", "package.json", "AGENTS.md"): self.assertTrue(name in loop, f"the loop does not protect {name}")
    def test_the_kit_documents_itself_consistently(self):
        self.assertTrue("MAINTAINER-PROMPT.md" in read(os.path.join(KIT, "README.md")), "README.md does not mention MAINTAINER-PROMPT.md")

if __name__ == "__main__":
    unittest.main(verbosity=2)
