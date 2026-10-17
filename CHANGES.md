# What changed, newest first
## 2026-10-17.9
- **Lessons for `codex loop`** (`agent/codex-loop.py`): a global file, `~/agent/lessons/LESSONS.md`, holds short rules learned from earlier runs, one line each
  (`- [web] Create every file the page links in the same task.`). The LOOP copies the lines that fit the project (tag `all`, or a tag taken from the file types in
  PLAN.md: web, js, python, shell, c, go, rust) into each session's private copy of the Codex home, appended to that copy's `AGENTS.md`. The master rules and the
  lessons folder are never mounted into the container, and every session gets a fresh copy, so an agent cannot read, change or carry over a lesson. At most 10 lines
  and 1400 characters are injected, and the text is fixed at the start of a run so Strata's prompt cache holds.
- **Candidates, not auto-learning.** After a task that needed a retry, was blocked, or passed with files reverted, the loop appends a record to
  `~/agent/lessons/candidates.jsonl`: the check output tail, file names, the attempt count and the id of the lessons that were in force. Never the agent's own words,
  and always reduced to one line of printable ASCII. You promote one with `--lessons add`. (Rejected: a `LESSONS.md` copied into each project, which the agent could
  edit and which goes stale, and letting the agent or the loop write lessons from its own output, which is the same hole as the planted `AGENTS.md` your tests found.)
- **`--lessons list | candidates | add --arg "- [tag] text" | remove --arg N`** edits the file with checks: plain ASCII, one line of up to 220 characters, no duplicates,
  at most 40 lines. Comment lines you keep in the file are ignored by the loop and preserved.
- `loop-report.md` and the run output say how many lessons were in force (with an id) and how many candidates were recorded; `--dry-run` lists the lessons it would inject.
- **Fixed in the tests:** `test_blocked_after_attempts` expected a second `refs/loop/T2-*` ref, but in refinement mode a rewrite with identical content changes nothing, so
  none is written. The loop was right; the test now writes different content on each attempt.
- `make_home` is now also run in test mode and exports `LOOP_HOME` to a scripted agent, so tests can see (and tamper with) exactly what a session would get.
- 19 more tests in `tests/test_codex_loop.py` (35 there, was 16), including the attacks above. A manual mutation check (ten deliberate breakages: injecting into the
  master file, reusing one home, auto-promotion, an unsanitised candidate, a mounted lessons folder, no line cap, no tag filter, and more) caught all ten once two weak
  tests were strengthened; it is not part of the suite. Add the suite total from your own run: ______ tests.
- **Not tested:** Docker and the real Codex and model with lessons present (earlier, a real Codex binary did read a Codex-home `AGENTS.md` ahead of the project's);
  whether lessons improve results (compare attempts and reverts in `loop-report.md` with and without one); `codex lessons` is not wired into the `codex` command, so
  run the script directly. Interactive `codex NAME` sessions do not get lessons, only `codex loop`.
To adopt: `bash kit.sh update`, then `python3 ~/agent/bin/codex-loop.py --lessons list` (check the script's path with `ls ~/agent/bin/`). Start with no lessons.
## 2026-10-17.8
- **`tools/codex-update.py`**: Codex is no longer pinned to 0.162.1. `check` (read-only) downloads the newest stable release, shows the release notes that may matter, and
  runs a 13-probe compatibility suite (`tools/codex_compat.py`, also usable alone on any binary) on the installed Codex and the new one; `apply` adds one thin image layer
  carrying the verified binary (the old image is kept; the browser layer and tools are untouched) and rebuilds the model catalog only if the new base instructions differ;
  `rollback` is instant. It undoes itself if the build fails, reports the wrong version or loses the browser layer.
- **Codex 0.163.0 checked**: 13 of 13 probes pass on every combination of Codex 0.162.1 / 0.163.0 and Strata 0.1.44.1 / 0.1.45 (Strata's own Responses parser run offline on the
  requests Codex sends); the catalog's base instructions are byte-identical; the seven feature flags are unchanged.
- **The title filter** now recognises Codex's session-title request by Codex's own turn metadata (`thread_source: thread_title`), as Strata does, with the text match as a fallback.
  Strata has had its own opt-in for this since 0.1.43/0.1.44 (`"codex_thread_titles": true`): I missed that when I wrote the filter; the playbook explains how they relate.
- The installer sets `check_for_update_on_startup = false`. `agent/CODEX_VERSION` is the version a fresh rebuild installs (0.163.0).
- The paste behaviour is now one of the probes. 19 more tests (74 in all).
To adopt: `bash kit.sh update`, then `python3 ~/strata-rebuild-kit/tools/codex-update.py check`.

## 2026-10-17.7
- **`tools/strata-update.py`**: Strata is no longer pinned to 0.1.44.1. `check` (read-only) tests your three patches against any newer release in a scratch
  copy and lists the SYCL, server and config changes; `apply --to TAG` backs up, switches, re-applies the patches, rebuilds the engine and runs the kernel
  tests as one transaction that undoes itself on any failure; `rollback` restores the previous version, patches, engine and configs byte for byte.
  Tested against the real Strata repository (v0.1.44.1 to v0.1.45 and back) and offline for every failure path.
- **A behaviour change in Strata 0.1.45 that the tool handles**: requests with no sampling settings (Codex sends none) used to decode greedy and now get the
  model card's sampling (temperature 1.0). `apply` offers `"sampling": {"temperature": 0, "presence_penalty": 0}`, which keeps decoding as it was.
- `kit.sh status` shows the Strata version and the newest release known locally.
- The playbook has the 0.1.45 findings (patches apply, the Meteor Lake fix is still needed, engine rebuild required, no format changes found) and acceptance checks.
- 10 more tests (55 in all).
To adopt: nothing to install; run `python3 ~/strata-rebuild-kit/tools/strata-update.py check`.

## 2026-10-17.6
- **`tools/strata-models.py`**: Strata has no command to remove a model, and re-running setup to change a setting replaces your tuned engine options.
  `list` shows each model's real files (yours are in two folders), sizes and what is shared; `remove TAG` previews, then (with `--yes` and a typed
  confirmation) deletes only that model's own files and archives its settings; `show`, `set` and `arg`/`unarg` change a model's settings with a backup.
  Shared files (the draft head, the expert profile, a shard another model uses), running models and anything outside your Strata data folders are protected.
- 11 more tests (45 in all).
To adopt: nothing to install; the tool is run from the kit.

## 2026-10-17.5
- **Fixed in kit.sh:** `status` said the dashboard patch was "not applied" when the power patch sat on top of it (it cannot see an earlier patch that a later
  one has built on). It now counts each patch's own lines, knows the power patch needs the dashboard patch, and was tested against the real Strata v0.1.44.1
  source in four states. (An earlier fix of mine had been lost in packaging; the version read by `update` was also stale.)
- **Fixed in the `codex` command:** an unknown option (such as `--auto` in a launcher older than the kit) is now an error, not part of the prompt;
  `codex build a snake game` (plain words, no such project) is a sentence for a new project, not a project called "build"; dry runs no longer create folders
  or trust entries; `codex NAME --auto` on an empty project asks what to build.
- 34 tests (was 23): the launcher's argument handling and the patch detection are now covered, so these cannot silently regress.
- The tarball's name carries its version.
To adopt: `bash kit.sh update`

## 2026-10-17.4
- **Reasoning shortcuts work** (Alt+. and Alt+,): `agent/model-catalog.json` tells Codex the levels of the `strata` model (none, low, medium, high);
  `model_catalog_json` in config.toml points at it. Same tools and prompt as before. `agent/make-model-catalog.py` rebuilds it after a Codex update.
- **`tools/strata-effort-position.py`**: by default every change of level makes Strata re-read the whole conversation; `end` makes it free (opt-in, a Strata setting).
- **`codex --auto`**: autopilot. Uses Codex's goal feature so the session continues through the plan without waiting for you after each task.
- The workspace rules say: do not ask whether to proceed, do not use request_user_input, and do not ask what to build when the first message already says.
- tests/test_catalog.py: 7 more tests (23 in all).
To adopt: `bash kit.sh update`

## 2026-10-17.3
- **`codex loop NAME`** (agent/codex-loop.py): the unattended loop, replacing agent-ralph.sh. One task per fresh session, the same prompt every time.
  The LOOP runs each task's check itself in a fresh container with no network, then ticks the task and commits; the agent's own ticks count for nothing.
  A session may change only the files its task lists; anything else (a weakened test, an edited plan, a stray file) is reverted and kept in the log folder.
  A check that already passes before the work is refused (it cannot tell done from not done); unsafe plans are refused before any session.
  Failed attempts are kept on git refs, noted in progress.txt for the next session, and three failures stop the loop.
- **Network gate**: the loop's container has `--network none`; its only way out is a Unix socket to the title filter (agent/unix-relay.py inside, strata-front.py --unix outside).
  `codex gate-test` proves it on your machine.
- Plans now need `files:` and a runnable `check:` on every task (the rules and template say so; `update` adopts them).
- tests/test_codex_loop.py: 16 tests of the loop (cheating, hanging, committing, tampering).
To adopt: `bash kit.sh update`, then `codex gate-test`

## 2026-10-17.2
- **The `codex` command** (agent/): one command for new and existing projects, `codex check NAME` (an independent test), `codex NAME --spec FILE`.
- **Title filter** (agent/strata-front.py): Codex's screen asks the model for a session title on every prompt; the filter answers it locally.
- **Real-browser check** (agent/browser-check.py, add-browser-to-image.sh): replaces fake-browser checks for web projects.
- **New project template and workspace rules**: SPEC.md handling, tasks that name their spec lines, stricter "done" rules.
- **Codex settings** the installer applies: background server off (interactive mode failed without it), unused features off (prompt 12.3K to 7.2K tokens), 64K window.
- **kit.sh**: `status` and `update`, so extracting the kit is followed by one command.
To adopt: `bash kit.sh update`

## 2026-10-17.1
- First rebuild kit: playbook, patches (Meteor Lake, dashboard readings, package power), settled config, tuning tools, machine snapshot script.
