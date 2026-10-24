"""codex plan: plan only, then check the plan the way `codex loop` will.

Runs the real launcher (agent/codex-client) with a fake docker and a fake curl on PATH, so no Docker, Strata or model is needed. A "session" is a shell
snippet the fake docker runs where the model would have worked (FAKE_SESSION). The plan check uses the real agent/codex-loop.py in dry-run mode.
"""
import os, shlex, shutil, stat, subprocess, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
CLIENT = os.environ.get("CLIENT_SRC", os.path.join(HERE, "..", "agent", "codex-client"))
LOOP = os.environ.get("LOOP_SRC", os.path.join(HERE, "..", "agent", "codex-loop.py"))
REAL_AWK = shutil.which("awk") or "/usr/bin/awk"

PLAN_WRITE = "Plan only: write PLAN.md and README.md and the Check command line in AGENTS.md, then do the plan review into PLAN-REVIEW.md, commit all of them, then STOP and wait for me."      # 2026-10-19.1 added the plan review
REVIEW_MARK = "Plan review, D-A-C in two passes"
SIMPLE = " Keep it simple:"                                  # the simplicity rule is always added to the first message of a session that writes a plan
NO_TASKS = "Do NOT start T1 or any other task: I review the plan first."
GOOD_PLAN = "# Plan\n\n- [ ] T1: game module | files: src/game.py | spec: lines 1-5 | check: `test -f src/game.py && echo OK` prints OK\n"
BAD_PLAN = "# Plan\n\n- [ ] T1: do it all | check: the whole thing works\n"
TICKED_PLAN = "# Plan\n\n- [x] T1: game module | files: src/game.py | check: `test -f src/game.py && echo OK` prints OK\n- [ ] T2: more | files: src/more.py | check: `test -f src/more.py && echo OK` prints OK\n"

FAKE_DOCKER = """#!/usr/bin/env bash
case "$1" in
  image|ps|rm) exit 0 ;;
  run) printf '%s\\n' "$@" > "$FAKE_DOCKER_LOG"; [ -n "${FAKE_SESSION:-}" ] && bash -c "$FAKE_SESSION"; exit "${FAKE_RC:-0}" ;;
esac
"""
FAKE_CURL = "#!/bin/sh\nprintf 200\n"
FAKE_AWK = '#!/bin/sh\ncase "$*" in *MemAvailable*) echo 64 ;; *) exec "$REAL_AWK" "$@" ;; esac\n'


GIT = "git init -q && git add -A && git -c user.name=t -c user.email=t@t commit -qm plan"


def writes_plan(name, plan):
    """What a real planning session leaves behind: the folder, PLAN.md, a git repository with one commit."""
    return f'mkdir -p "$W/{name}" && printf %s {shlex.quote(plan)} > "$W/{name}/PLAN.md" && cd "$W/{name}" && {GIT}'


def bash_split(line):
    """The launcher prints its command with printf %q: a multi-line first message comes out as $'...\\n...', which shlex cannot read. bash can."""
    out = subprocess.run(["bash", "-c", 'eval "set -- $1"; printf "%s\\0" "$@"', "x", line], capture_output=True, text=True).stdout
    return out.split("\0")[:-1]


def tail_after_image(argv):
    """The codex command line after the image name, from a docker run line."""
    return argv[argv.index("codex-sandbox") + 1:]


class Base(unittest.TestCase):
    GUIDANCE = {"CODEX_DAC": "0", "CODEX_UX": "0"}               # the two switches that turn the guidance off (2026-10-19.1); the effort flag below is not one of them
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.bin = os.path.join(self.root, "bin"); os.makedirs(self.bin)
        for name, body in (("docker", FAKE_DOCKER), ("curl", FAKE_CURL), ("awk", FAKE_AWK)):
            p = os.path.join(self.bin, name)
            with open(p, "w") as fh: fh.write(body)
            os.chmod(p, os.stat(p).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        self.agent = os.path.join(self.root, "agent")
        self.work = os.path.join(self.agent, "work")
        os.makedirs(os.path.join(self.agent, "codex-home")); os.makedirs(self.work); os.makedirs(os.path.join(self.agent, "bin"))
        if os.path.exists(LOOP): shutil.copy(LOOP, os.path.join(self.agent, "bin", "codex-loop.py"))
        self.slog = os.path.join(self.root, "strata.log")
        with open(self.slog, "w") as fh: fh.write("expert cache 24576 slots\n")
        self.dlog = os.path.join(self.root, "docker-run.txt")
    def tearDown(self): shutil.rmtree(self.root, ignore_errors=True)

    def assert_plan(self, got, opening):
        """A planning session's first message: the opening, the instruction to write the plan and the review, then the guidance, ending with the line that stops it."""
        got = got if isinstance(got, str) else got[0] if len(got) == 1 else self.fail(f"expected one message, got {len(got)}")
        self.assertTrue(got.startswith(f"{opening} {PLAN_WRITE}"), got[:260]); self.assertTrue(got.endswith(NO_TASKS), got[-160:])
        self.assertIn(SIMPLE, got)
    def assert_msg(self, got, old):
        """The launcher's own words first; the simplicity rule (always on) may follow them."""
        self.assertTrue(got == old or got.startswith(old + SIMPLE), got[:220])

    def proj(self, name, spec=None, plan=None):
        d = os.path.join(self.work, name); os.makedirs(d, exist_ok=True)
        if spec is not None:
            with open(os.path.join(d, "SPEC.md"), "w") as fh: fh.write(spec)
        if plan is not None:
            with open(os.path.join(d, "PLAN.md"), "w") as fh: fh.write(plan)
        return d

    def git_init(self, name):
        d = os.path.join(self.work, name)
        subprocess.run(f"cd {shlex.quote(d)} && {GIT}", shell=True, check=True, capture_output=True)
    def run_codex(self, *args, dry=False, session="", rc=0):
        env = dict(os.environ, PATH=self.bin + os.pathsep + os.environ["PATH"], AGENT_DIR=self.agent, STRATA_LOG=self.slog, HOME=self.root,
                   FAKE_DOCKER_LOG=self.dlog, FAKE_SESSION=session, FAKE_RC=str(rc), REAL_AWK=REAL_AWK, W=self.work)
        env.update(self.GUIDANCE)                                      # these tests pin the launcher's own wording; the D-A-C / polish guidance has its own class below
        env.pop("CODEX_DRY_RUN", None)
        if dry: env["CODEX_DRY_RUN"] = "1"
        r = subprocess.run(["bash", CLIENT, *args], env=env, capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL, cwd=self.root)
        return r
    def dry_argv(self, *args):
        r = self.run_codex(*args, dry=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        line = [ln for ln in r.stdout.splitlines() if ln.startswith("docker run")][-1]
        return bash_split(line)
    def dry_codex(self, *args): return tail_after_image(self.dry_argv(*args))
    def prompt_of(self, *args):
        c = self.dry_codex(*args); return c[c.index("never") + 1:]
    def session_argv(self):
        with open(self.dlog) as fh: return fh.read().splitlines()


class PlanPrompt(Base):
    def test_plan_is_a_command_like_loop_and_check(self):
        self.proj("snake", spec="# Spec\nlines\n")
        self.assert_plan(self.prompt_of("plan", "snake"), "Start the project from SPEC.md.")
        self.assert_plan(self.dry_argv("plan", "snake")[-1], "Start the project from SPEC.md.")
    def test_the_old_flag_form_says_what_to_type_instead(self):
        self.proj("snake", spec="# Spec\n")
        r = self.run_codex("--plan", "snake", dry=True); self.assertEqual(r.returncode, 1); self.assertIn("plan is a command, not an option:  codex plan NAME", r.stderr)
        r = self.run_codex("snake", "--plan", dry=True); self.assertEqual(r.returncode, 1); self.assertIn("codex plan snake", r.stderr)
        self.assertEqual([ln for ln in r.stdout.splitlines() if ln.startswith("docker")], [])               # nothing was started
    def test_plan_is_a_reserved_word_just_like_loop(self):
        self.proj("plan", spec="# Spec\n")                                                                  # a project that happens to be called plan
        argv = self.dry_argv("plan"); self.assertEqual(argv[argv.index("-w") + 1], "/work")               # `codex plan` is the command: a new project, not that folder
        self.assert_plan(argv[-1], "Start a new project.")
        argv = self.dry_argv("plan", "plan"); self.assertEqual(argv[argv.index("-w") + 1], "/work/plan")  # and the folder is still reachable by name
    def test_a_design_and_no_plan_writes_the_plan_and_stops(self):
        self.proj("snake", spec="# Spec\nlines\n")
        c = self.dry_codex("plan", "snake")
        self.assert_plan(c[c.index("never") + 1:], "Start the project from SPEC.md.")
        self.assertNotIn("--enable", c)                                                           # not autopilot
    def test_nothing_to_go_on_asks_what_to_build_then_plans(self):
        self.proj("fresh")
        self.assert_plan(self.prompt_of("plan", "fresh"), "Start this project. First ask me what to build.")
    def test_an_existing_plan_is_reviewed_never_rewritten(self):
        self.proj("snake", spec="# Spec\n", plan=GOOD_PLAN)
        (p,) = self.prompt_of("plan", "snake")
        self.assertIn("PLAN.md already exists", p); self.assertIn("Change nothing until I say so.", p); self.assertTrue(p.endswith(NO_TASKS))
        self.assertNotIn("write PLAN.md", p); self.assertNotIn("Plan only", p)                    # nothing tells the session to write the file again
    def test_your_own_words_come_first(self):
        self.proj("snake", spec="# Spec\n")
        self.assert_plan(self.prompt_of("plan", "snake", "two players on one keyboard"), "two players on one keyboard")
        self.proj("old", spec="# Spec\n", plan=GOOD_PLAN)
        (q,) = self.prompt_of("plan", "old", "make T2 smaller")                                              # a plan exists: still no rewrite
        self.assertTrue(q.startswith("make T2 smaller ")); self.assertTrue(q.endswith(NO_TASKS)); self.assertNotIn("Plan only", q); self.assertNotIn("write PLAN.md", q)
    def test_a_new_project_and_a_sentence(self):
        c = self.dry_codex("plan")
        self.assert_plan(c[c.index("never") + 1:], "Start a new project.")
        self.assertEqual(self.dry_argv("plan")[self.dry_argv("plan").index("-w") + 1], "/work")
        self.assert_plan(self.prompt_of("plan", "build a snake game"), "build a snake game")
        self.assert_plan(self.prompt_of("plan", "build", "a", "snake", "game"), "build a snake game")
    def test_a_design_file_is_copied_in_and_planned_from(self):
        self.proj("snake"); spec = os.path.join(self.root, "my-design.md")
        with open(spec, "w") as fh: fh.write("# My design\n")
        self.assert_plan(self.prompt_of("plan", "snake", "--spec", spec), "Start the project from SPEC.md.")
        with open(os.path.join(self.work, "snake", "SPEC.md")) as fh: self.assertEqual(fh.read(), "# My design\n")
    def test_the_container_is_set_up_exactly_as_for_any_session(self):
        self.proj("snake", spec="# Spec\n")
        a, b = self.dry_argv("plan", "snake"), self.dry_argv("snake")
        self.assertEqual(a[:a.index("codex-sandbox")], b[:b.index("codex-sandbox")])                # same mounts, limits, network: only the prompt differs
        self.assertEqual(a[a.index("codex-sandbox"):a.index("never") + 1], b[b.index("codex-sandbox"):b.index("never") + 1])
    def test_it_cannot_be_combined_with_autopilot_or_resume(self):
        self.proj("snake", spec="# Spec\n")
        r = self.run_codex("plan", "--auto", "snake", dry=True); self.assertEqual(r.returncode, 1); self.assertIn("do not go together", r.stderr)
        r = self.run_codex("plan", "snake", "--auto", dry=True); self.assertEqual(r.returncode, 1)
        r = self.run_codex("plan", "snake", "resume", dry=True); self.assertEqual(r.returncode, 1); self.assertIn("starts a planning session", r.stderr)
    def test_other_unknown_options_are_still_errors(self):
        self.proj("snake")
        for args in (("plan", "--bogus"), ("plan", "snake", "--bogus"), ("--bogus", "snake")):
            r = self.run_codex(*args, dry=True); self.assertEqual(r.returncode, 1, args); self.assertIn("unknown option", r.stderr)
    def test_help_lists_the_option_and_nothing_stray(self):
        r = self.run_codex("help"); self.assertEqual(r.returncode, 0)
        self.assertIn("codex plan [NAME]", r.stdout); self.assertIn("plan only", r.stdout); self.assertIn("codex gate-test", r.stdout)
        self.assertIn("Projects live in", r.stdout)
        self.assertNotIn("set -u", r.stdout); self.assertNotIn("AGENT=", r.stdout)               # the old fixed line range printed these two shell lines

    def test_help_follows_the_header_however_long_it_grows(self):
        with open(CLIENT) as fh: lines = fh.read().split("\n")
        longer = os.path.join(self.root, "codex-longer"); head = lines[:3] + ["#   codex a-new-command      a line added later to the comment block"] * 3 + lines[3:]
        with open(longer, "w") as fh: fh.write("\n".join(head))
        env = dict(os.environ, PATH=self.bin + os.pathsep + os.environ["PATH"], AGENT_DIR=self.agent, REAL_AWK=REAL_AWK, HOME=self.root)
        out = subprocess.run(["bash", longer, "help"], env=env, capture_output=True, text=True, timeout=60, cwd=self.root).stdout
        self.assertEqual(out.count("a-new-command"), 3); self.assertTrue(out.rstrip().endswith("The container sees only that folder."), out[-200:])
        self.assertNotIn("set -u", out); self.assertNotIn("AGENT=", out)


class WithoutPlanNothingChanges(Base):
    """What the launcher did before `codex plan` existed must not move."""
    BASE = ["codex", "--no-daemon", "--disable", "worktrees", "-c", 'model_reasoning_effort="high"', "-s", "danger-full-access", "-a", "never"]       # high is the default since 2026-10-19.1
    def test_a_design_and_no_plan_still_starts_planning(self):
        self.proj("snake", spec="# Spec\n"); c = self.dry_codex("snake")
        self.assertEqual(c[:-1], self.BASE); self.assert_msg(c[-1], "Start the project from SPEC.md.")
    def test_a_plain_project_has_no_first_message(self):
        self.proj("plain"); self.assertEqual(self.dry_codex("plain"), self.BASE)
        self.proj("planned", plan=GOOD_PLAN); self.assertEqual(self.dry_codex("planned"), self.BASE)
    def test_resume_and_sentences_and_your_own_words(self):
        self.proj("snake", spec="# Spec\n")
        self.assertEqual(self.dry_codex("snake", "resume"), self.BASE + ["resume", "--last"])
        for args, want in ((("build", "a", "snake", "game"), "build a snake game"), (("snake", "two players"), "two players")):
            c = self.dry_codex(*args); self.assertEqual(c[:-1], self.BASE); self.assert_msg(c[-1], want)
    def test_autopilot_is_untouched(self):
        self.proj("snake", spec="# Spec\n")
        c = self.dry_codex("snake", "--auto")
        self.assertEqual(c[:12], ["codex", "--no-daemon", "--disable", "worktrees", "--enable", "goals", "-c", 'model_reasoning_effort="high"', "-s", "danger-full-access", "-a", "never"])
        self.assertTrue(c[12].startswith("Start the project from SPEC.md.")); self.assertIn("Then call create_goal", c[12]); self.assertNotIn("Plan only", c[12])
    def test_no_plan_wording_leaks_into_other_prompts(self):
        self.proj("snake", spec="# Spec\n")
        for args in (("snake",), ("snake", "resume"), ("snake", "--auto"), ("build", "a", "game"), ()):
            self.assertNotIn("Plan only", " ".join(self.dry_codex(*args))); self.assertNotIn("Do NOT start", " ".join(self.dry_codex(*args)))


class GuidanceOn(Base):
    """The defaults as shipped (2026-10-19.1): a D-A-C plan review and the polish and simplicity guidance on the first message of a session that writes a plan,
    reasoning level high. Never in AGENTS.md; CODEX_DAC=0 / CODEX_UX=0 / CODEX_EFFORT=low turn parts off."""
    GUIDANCE = {}
    def test_a_plan_session_carries_the_review_and_never_edits_an_existing_plan(self):
        self.proj("snake", spec="# Spec\n"); (p,) = self.prompt_of("plan", "snake")
        self.assert_plan(p, "Start the project from SPEC.md."); self.assertIn(REVIEW_MARK, p); self.assertIn("PLAN-REVIEW.md", p)
        self.proj("old", spec="# Spec\n", plan=GOOD_PLAN); (q,) = self.prompt_of("plan", "old")
        self.assertTrue(q.startswith("PLAN.md already exists. Read it with SPEC.md and review it. Change nothing except PLAN-REVIEW.md, and commit that file."), q[:200])
        self.assertIn("Do NOT edit PLAN.md.", q); self.assertIn(REVIEW_MARK, q); self.assertTrue(q.endswith(NO_TASKS)); self.assertNotIn("write PLAN.md", q)
    def test_a_project_that_already_has_a_plan_gets_no_first_message(self):
        self.proj("planned", spec="# Spec\n", plan=GOOD_PLAN); c = self.dry_codex("planned")
        self.assertEqual(c, Base_ARGV)                                                  # not even the guidance: nothing is about to be planned
    def test_the_switches_turn_the_parts_off(self):
        self.proj("snake", spec="# Spec\n")
        for env, gone in (({"CODEX_DAC": "0"}, REVIEW_MARK),):
            self.GUIDANCE = env; (p,) = self.prompt_of("plan", "snake"); self.assertNotIn(gone, p); self.assertTrue(p.endswith(NO_TASKS))
        self.GUIDANCE = {}; self.assertIn('model_reasoning_effort="high"', self.dry_codex("snake"))
        self.GUIDANCE = {"CODEX_EFFORT": "low"}; c = self.dry_codex("snake"); self.assertIn('model_reasoning_effort="low"', c); self.assertNotIn('model_reasoning_effort="high"', c)


Base_ARGV = ["codex", "--no-daemon", "--disable", "worktrees", "-c", 'model_reasoning_effort="high"', "-s", "danger-full-access", "-a", "never"]


class AfterThePlanSession(Base):
    """A fake session writes (or does not write) the plan; the launcher then shows it the way the loop sees it."""
    @unittest.skipUnless(os.path.exists(LOOP), "agent/codex-loop.py is not next to the tests")
    def test_a_good_plan_is_shown_and_accepted(self):
        self.proj("snake", spec="# Spec\nl1\nl2\n")
        r = self.run_codex("plan", "snake", session=writes_plan("snake", GOOD_PLAN))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        for text in ("Plan only: Codex writes PLAN.md and stops", "The plan, as codex loop sees it:", "T1", "src/game.py", "The loop accepts this plan",
                     "codex loop snake --sessions 2 --minutes 15", "codex loop snake  "):
            self.assertIn(text, r.stdout)
        self.assertNotIn("REFUSE", r.stdout); self.assertNotIn("Note: the session ticked", r.stdout); self.assertNotIn("A session would run", r.stdout); self.assertIn("0 of 1 tasks ticked", r.stdout)     # the commands are not shown
        self.assertNotIn("Continue with", r.stdout)
        self.assertEqual(sorted(os.listdir(os.path.join(self.work, "snake"))), [".git", "PLAN.md", "SPEC.md"])                                # looking at the plan changed nothing
        self.assertEqual(os.listdir(os.path.join(self.agent, "logs")) if os.path.isdir(os.path.join(self.agent, "logs")) else [], [])
    @unittest.skipUnless(os.path.exists(LOOP), "agent/codex-loop.py is not next to the tests")
    def test_a_plan_the_loop_would_refuse_is_flagged_with_the_reason(self):
        self.proj("snake", spec="# Spec\n")
        r = self.run_codex("plan", "snake", session=writes_plan("snake", BAD_PLAN))
        self.assertEqual(r.returncode, 0)
        self.assertIn("ERROR", r.stdout); self.assertIn("no runnable check", r.stdout); self.assertIn("The loop would REFUSE this plan (the reason is above)", r.stdout)
        self.assertIn("codex plan snake", r.stdout); self.assertNotIn("The loop accepts", r.stdout); self.assertNotIn("codex loop snake --sessions", r.stdout)
    @unittest.skipUnless(os.path.exists(LOOP), "agent/codex-loop.py is not next to the tests")
    def test_a_plan_without_a_git_repository_gets_the_loops_own_explanation(self):
        self.proj("snake", spec="# Spec\n")
        r = self.run_codex("plan", "snake", session=f"printf %s {shlex.quote(GOOD_PLAN)} > \"$W/snake/PLAN.md\"")
        self.assertIn("no git repository or PLAN.md", r.stdout); self.assertIn("The loop would REFUSE this plan (the reason is above)", r.stdout)
    def test_no_plan_written_is_said_plainly(self):
        self.proj("snake", spec="# Spec\n")
        r = self.run_codex("plan", "snake", session="true"); self.assertEqual(r.returncode, 0)
        self.assertIn("No PLAN.md was written", r.stdout); self.assertIn("codex plan snake", r.stdout); self.assertNotIn("The plan, as codex loop sees it", r.stdout)
    @unittest.skipUnless(os.path.exists(LOOP), "agent/codex-loop.py is not next to the tests")
    def test_tasks_ticked_during_a_plan_only_session_are_reported(self):
        self.proj("snake", spec="# Spec\n", plan=GOOD_PLAN); self.git_init("snake")
        r = self.run_codex("plan", "snake", session="sed -i 's/- \\[ \\] T1/- [x] T1/' \"$W/snake/PLAN.md\"")
        self.assertIn("Note: the session ticked 1 task(s) although you asked for plan only", r.stdout); self.assertIn("git -C", r.stdout)
    @unittest.skipUnless(os.path.exists(LOOP), "agent/codex-loop.py is not next to the tests")
    def test_tasks_that_were_already_ticked_before_are_not_blamed_on_the_session(self):
        self.proj("snake", spec="# Spec\n", plan=TICKED_PLAN); self.git_init("snake")
        r = self.run_codex("plan", "snake", session="true"); self.assertEqual(r.returncode, 0)
        self.assertNotIn("Note: the session ticked", r.stdout); self.assertIn("The plan, as codex loop sees it", r.stdout)
    @unittest.skipUnless(os.path.exists(LOOP), "agent/codex-loop.py is not next to the tests")
    def test_a_new_project_is_checked_under_the_name_it_was_given(self):
        r = self.run_codex("plan", "make a snake game", session=writes_plan("snakey", GOOD_PLAN))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("Created: snakey", r.stdout); self.assertNotIn("Continue with", r.stdout)
        self.assertIn("The plan, as codex loop sees it:", r.stdout); self.assertIn("codex loop snakey", r.stdout)
        self.assertEqual(self.session_argv()[-1].split(" ")[0], "make"); self.assertIn("Plan only", self.session_argv()[-1])
    def test_without_the_option_the_old_message_remains_and_nothing_is_checked(self):
        r = self.run_codex("make a snake game", session="mkdir -p \"$W/snakey\"")
        self.assertIn("Created: snakey", r.stdout); self.assertIn("Continue with:  codex snakey", r.stdout)
        self.assertNotIn("The plan, as codex loop", r.stdout); self.assertNotIn("Plan only", " ".join(self.session_argv()))
    def test_a_missing_loop_script_is_a_note_not_a_failure(self):
        os.remove(os.path.join(self.agent, "bin", "codex-loop.py")); self.proj("snake", spec="# Spec\n")
        r = self.run_codex("plan", "snake", session=writes_plan("snake", GOOD_PLAN))
        self.assertEqual(r.returncode, 0); self.assertIn("codex-loop.py is missing", r.stdout); self.assertNotIn("The loop accepts", r.stdout)
    @unittest.skipUnless(os.path.exists(LOOP), "agent/codex-loop.py is not next to the tests")
    def test_the_sessions_own_exit_code_is_kept(self):
        self.proj("snake", spec="# Spec\n")
        r = self.run_codex("plan", "snake", session=writes_plan("snake", GOOD_PLAN), rc=3)
        self.assertEqual(r.returncode, 3); self.assertIn("The plan, as codex loop sees it", r.stdout)
    def test_the_session_really_gets_the_plan_prompt(self):
        self.proj("snake", spec="# Spec\n"); self.run_codex("plan", "snake", session="true")
        self.assert_plan(self.session_argv()[-1], "Start the project from SPEC.md.")


if __name__ == "__main__":
    unittest.main(verbosity=2)
