# What changed, newest first
## 2026-10-23.8
- **Test hygiene only; nothing in the running system changes.** Running the suite printed `ResourceWarning: unclosed <socket.socket ...>` at exit (not shown by the usual `grep` of the result lines, which is why it went unseen): `tests/test_front_synth.py` and the new `tests/test_apply_patch_rescue.py` stopped their test servers without closing their listening sockets (`server_close()` added), `tests/test_front_restart.py` read `codex-client` without closing it, and `tools/codex_compat.py` (`provider_of`) read `config.toml` without closing it. All four fixed; the full suite prints no socket warning now.
- **Not fixed:** the full suite still prints 39 `unclosed file` warnings: 37 `BufferedReader` ones from `tests/test_kit_sync.py` (they were there before this release; its helpers close their files, so the leak is in how the tests run the sync helper) and 2 `TextIOWrapper` ones from a test I did not trace. Neither was traced further. They are noise, not failures; `python3 -W error::ResourceWarning -m unittest discover -s tests` passes.
## 2026-10-23.7
- **The `apply_patch` call written as text no longer ends the turn.** In a tetris session (Strata 0.2.3, Codex 0.164.0) the model wrote `<tool_call><function=apply_patch><parameter=cmd>apply_patch</parameter></function></tool_call>` as if apply_patch were a tool. It is not: the tools are `exec_command`, `write_stdin` (and `request_user_input`, `web_search`), and apply_patch is a shell command run through exec_command, as the slim instructions show. Strata does not read a call to an undeclared name as a call and returns the text (0.2.x says so in its log: `[strata] 1 <tool_call> block(s) in the answer did not read as a call of a declared tool ... the names the model wrote: apply_patch`); Codex then sees a message with no tool call, ends the turn, and waits for you to type `continue`. That text also had no patch in it (cmd was just the word `apply_patch`), so there was nothing to run.
  **New `agent/apply_patch_rescue.py`, used by the front filter (`strata-front.py`), on the way back to Codex:** when the answer ENDS in such a block and holds no real tool call, a real `exec_command` call is added before the response ends. If the block carries a patch (`*** Begin Patch ...` in any parameter) it runs it as `apply_patch <<'PATCH' ... PATCH` (an `*** End Patch` line is added if missing; a whole command written by the model is used as written). If not (the case above) it runs a command that prints how to call apply_patch and exits 2, so the next request shows the model the right form and the turn goes on. Only a streamed Responses reply is looked at; everything else passes byte for byte; the filter works without the module. Off switch: `STRATA_FRONT_APPLY_PATCH=0` or an empty `~/agent/work/NO-APPLY-PATCH-RESCUE`. The installer puts the module in `~/agent/bin` and `kit.sh status` lists it.
  **Measured with the real Codex 0.164.0 and a scripted model** (the model's answer is exactly the text from your session): without the repair, one request and the turn ends on the text (your session); with it, two requests, Codex runs the call, and the model gets back "apply_patch is not a tool here. It is a shell command: call exec_command with cmd set to the whole command, for example: ..." (exit code 2). Also measured: a function call for a name Codex does not know comes back as `unsupported call: apply_patch`; a bare `apply_patch` through exec_command as `command not found` (exit 127); the heredoc form works. 15 new tests (the transform on your exact text, a block carrying a patch in three parameter names, a whole command, text before the block, prose after it, a real call beside it, another name, chunk boundaries, garbage input, the filter with the switches, another path or content type, the module missing); five deliberate breaks each fail them.
  **Not done, on purpose:** the likely CAUSE is the slim instructions' wording ("Edit with small patches using apply_patch" beside "You have two tools"). Saying outright that apply_patch is a shell command would probably make this rarer, but it changes the measured prompt, so it needs a before and after with `tools/edit-failures.py` on your sessions rather than my guess. Declaring apply_patch as a real tool (`apply_patch_tool_type` in the catalog) would add Codex's long patch grammar to every request; not done. **Not run on your machine:** the repair has run against real Codex with a scripted model, not against your Strata and the real model. To see how often this happens: `grep -a "did not read as a call" <Strata log> | tail`.
  **A running filter keeps the code it started with, and nothing replaced it after an update** (this was true of the compaction shortcut too): without a fix the repair would not have been active until the filter was killed or the machine restarted. `codex` (`ensure_front` in the launcher) now replaces a running filter that started before any of its three files (`strata-front.py`, `apply_patch_rescue.py`, `synth_compact.py`) was installed, prints "(the title filter was older than its installed files: restarted it)", and never does it while the session container is running. Tested with the launcher's own functions, a real filter process and a fake `docker`: unchanged files keep the same process; each of the three files makes it replace it; a running session keeps it, and the filter is replaced once the session has ended; three deliberate breaks fail the tests.
To adopt: extract, then `bash ~/strata-rebuild-kit/kit.sh update`, then end any open `codex` session and start `codex` again: it restarts the old filter and says so. `codex status` shows the filter. If you want the repair off: `touch ~/agent/work/NO-APPLY-PATCH-RESCUE`.
## 2026-10-23.6
- **`tools/codex_compat.py`: the `shortcuts` probe judged Alt+. / Alt+, from an assumed starting level, and 2026-10-23.5's explanation of the failure was wrong.** `codex-update.py check` failed `shortcuts` on your machine three starts in a row on BOTH Codex 0.163.0 and 0.164.0 ("the request carries effort 'low', expected 'medium'"). That is not a slow start (2026-10-23.5 said it was and added a retry, which a deterministic failure ignores); it is the probe's own assumption. It expected `medium` after up, up, down, which is true only when the level starts at `low` or higher; from `none` the same keys end at `low`, which is exactly what you got. The starting level comes from `model_reasoning_effort` in your config.toml (or the catalog's default), and your home evidently differs from the scratch home I tested with. **That your config starts at `none` is the best fit, not something I have seen:** run `grep -n -i "effort\|reasoning" ~/agent/codex-home/config.toml` to see.
  The probe now sends a first message to see the starting level, presses the keys, sends a second message, and compares with where up, up, down must end from THAT start (clamped at both ends: none to low, low to medium, medium to medium, high to medium). When it fails it says what it saw (the start, the end, the end expected, the starts seen in each attempt). The retry stays (a slow start can lose a key; a retried pass is a warning). Run against the real Codex binaries in my sandbox: 0.163.0 and 0.164.0 each pass from every start (config level unset, none, low, medium, high); the unit tests pin each start and the failure messages, and breaking the probe on purpose (the old fixed expectation, no retry, wrap instead of clamp) fails them. Not run on your machine.
- Nothing else changed from 2026-10-23.5.
## 2026-10-23.5
- **`tools/codex_compat.py`: the `shortcuts` probe no longer fails on one slow start.** On your machine `codex-update.py check --to 0.164.0` stopped with "shortcuts: after Alt+. Alt+. Alt+, the request carries effort 'low', expected 'medium'" and said the same for the installed 0.163.0, while the other 12 probes passed. It is not a change in Codex: the probe sends its keys a fixed 9 seconds after the interface starts, a slow start loses the first key, and the request then carries one level too low. Reproduced here with the same home and the same binary: Codex 0.163.0 failed once and passed twice, 0.164.0 failed once and passed in every later run (it passed in the first full run too). The probe now tries again in a fresh interface (up to 3 starts); a pass that needed a retry is reported as a warning ("passed on attempt 2 of 3"), and a keymap that really moved still fails every time. Tested with a fake interface (one start; a lost key then a pass; three wrong starts) and by breaking the probe on purpose (no retry, any level accepted): the test fails each time. The real interface was run only in my sandbox (six runs of the unchanged probe, as above), not on your machine.
- Nothing else changed from 2026-10-23.4. Re-run `python3 ~/strata-rebuild-kit/tools/codex-update.py check`: it should say ready (with the `worktrees` warning, and possibly a shortcuts warning if your machine is busy).
## 2026-10-23.4
- **Strata 0.2.3 and Codex 0.164.0 are supported by the tools; neither has run on the machine.** What was done and how it is known:
  - **`patches/mtl-igpu-local.0.2.patch`**: the Meteor Lake patch remade for Strata 0.2.2 and newer (upstream rewrote `sycl/setup_intel.py`; the original applies to 0.1.x only; this one applies to v0.2.2 and v0.2.3 and not to v0.2.1.x). Same change; for the test config used, setup generates the same config as 0.1.45 did for this GPU (compared here by running both versions of `to_sycl`; only the checkout path differs). It leaves upstream's new fit rule off for this GPU so the measured settings are not altered (that rule is untried here). The three dashboard patches apply to every 0.2.x tag; Strata's own `test_xe_telemetry.py` passes on the patched 0.2.3 tree (11 tests, 1 skipped as root-only).
  - **`tools/strata-update.py`**: a patch can have variant files (`NAME.SERIES.patch`): the tool tries each on the tree, for `check`, `apply`, the undo and `rollback` (which goes back to the old tag with the old file). The expected engine version is read from the target's `CMakeLists.txt` (v0.2.3 builds engine 0.1.48; the old check compared with the tag's own number and would have undone a good update). A build is judged by `BUILD EXIT 0` and `errors: 0`, not by its exit status. `check` says `no action needed` instead of `ACTION` for a settings change none of your configs needs. New COMPAT entries for 0.2.x: `fit_max_tokens` (now defaults to true; offered only to a config without the key; the settled config has it), and information: the new Intel prompt defaults do not switch on for this iGPU (read in the source, not run), the worker count of a hybrid CPU can change, the monitor's tokens-per-second changed definition in 0.2.2, do not use Strata's own `strata launch` / `strata update` here, the engine version of a 0.2.x tag.
  - **Real-checkout test (fake engine build, no Docker):** a v0.1.45 clone with the four kit patches and the settled config: `check --to v0.2.3` says ready and changes nothing (same fingerprint); `apply --to v0.2.3` succeeds, keeps `--prefill 2048`, `--short-read 64`, the sampling block and `fit_max_tokens`, and reports engine 0.1.48; `rollback` restores the identical state. **Not run:** the Docker build, the quick kernel tests, `setup.sh --check`, speed, memory, answers. Not read: most of the 1,308 commits since 0.1.45 (read: the release notes for 0.2.0 to 0.2.3 by keyword, the Intel and SYCL commits, the telemetry, launcher, Responses and sampling changes, the config keys and engine flags the settled config uses).
  - **`kit.sh status`** knows variants: it reports the Meteor Lake patch as applied when the original or any remade file is on the tree.
  - **Codex 0.164.0** (`agent/CODEX_VERSION` is now 0.164.0): the probe suite passes on Codex 0.163.0 and 0.164.0 against the request parsers of Strata 0.1.45 and 0.2.3 (13 of 13 for 0.163.0; 12 ok and 1 warning for 0.164.0 in both: the `worktrees` feature is now `removed`, the flag is still accepted). The catalog is identical, the tools and the first request (about 3,515 tokens) are unchanged. Run on a scratch Codex home made by the installer, not your real one. `incremental_tools` is newly stable and on; nothing else about it was assessed. `codex-update.py apply` has not been run on the machine.
- **`tests/test_launcher_plan.py` is back** (33 tests; it was the 30 `codex plan` launcher tests of the old kit copy). Its harness now reads the launcher's `printf %q` output with bash (a multi-line first message comes out as `$'...'`). Two classes pin the launcher's own wording with `CODEX_DAC=0 CODEX_UX=0`, one pins the shipped guidance defaults (the D-A-C plan review, never editing an existing plan, no message for a project that already has a plan, and the `CODEX_DAC` and `CODEX_EFFORT` switches). Breaking the launcher on purpose (default effort, the never-edit-PLAN.md sentence, the stop line) failed 6, 1 and 11 tests.
- **Playbook and `MAINTAINER-PROMPT.md`:** Step 3 and Appendix B name the variant file; section 9 has the 0.2.3 and 0.164.0 paragraphs; the prompt's open items say what is untested.
To adopt: extract, then `python3 ~/strata-rebuild-kit/tools/strata-update.py check --to v0.2.3` (read-only; it should say ready), then see playbook section 9 before `apply`. For Codex: `python3 ~/strata-rebuild-kit/tools/codex-update.py check`, then `apply`. Both have `rollback`.
## 2026-10-23.3
- **The suite is green again** (the 7 tests that failed at 2026-10-22.1 and 2026-10-23.2 are fixed). Per test, which side was stale: the shipped catalog and `agent/slim-instructions.md` already carried the newer slim text, so the TESTS (and `tools/codex-slim.py`, see below) were stale; the installer's behaviour had changed on purpose (three-way sync), so two installer tests were rewritten for it; the project template had grown to 3,605 characters (promoted lessons), so its 1,200-character guard was out of date and is now 4,000.
- **`tools/codex-slim.py` carried the OLD slim instructions** (the 1,159-character text from 2026-10-19.1). `codex-slim.py apply` would have installed them over the current 1,799-character text. It now carries the current text, and a test imports the tool and compares the string it would write with `agent/slim-instructions.md`.
- **Tests no longer write into the kit.** The installer copies a change found only in `~/agent` INTO the kit folder it runs from (`kit-sync.py`; this is by design). The installer tests ran it from the real kit, so every run appended `an edit` and `my own rule` to `agent/defaults/*-AGENTS.md`. `InstallerDefaults` now runs a scratch copy of the kit; with the isolation removed on purpose, two tests fail.
- **`tests/test_process_doc.py` is back** (it existed in the old kit copy and was missing from the git copy): it checks that `MAINTAINER-PROMPT.md` names every script and tool, every test hook it lists, the standard commands and sections, and the loop guarantees. It found 7 files the prompt did not name; they are in the kit table now (`kit-sync.py`, `synth_compact.py`, `compact-prompt.txt`, `codex-stats.py`, `slim-instructions.md`, `codex-slim.py`, `edit-failures.py`).
- **Playbook:** two paragraphs that were only in the project's older copy are back in the kit's: the note that `sycl/tools/build.sh` exits 1 after a successful build (judge it by `BUILD EXIT 0`, `errors: 0` and the engine version; never patch `build.sh` in the checkout), and "Why the file rule is strict by default (tested, not assumed)". A new paragraph in section 9 records the newer releases below.
- **Found, not fixed (nothing here has been run on the machine):** Strata **v0.2.3** exists (0.2 series since 2026-10-19). The three dashboard patches apply to v0.2.1.3 to v0.2.3; `mtl-igpu-local.patch` does not (`sycl/setup_intel.py`), and upstream still has no Meteor Lake support, so it has to be remade and reviewed before moving to 0.2.x. Codex **0.164.0** is stable; the kit was tested with 0.163.0. The old `tests/test_launcher_plan.py` (30 `codex plan` launcher tests) is not ported: it fails against the current launcher for intended reasons (multi-line first message, `high` reasoning level) and needs its exact-prompt assertions rewritten.
- **Tests:** the full suite prints its own count; see the hand-over message. Nothing in the running system changes with this release except that `codex-slim.py` now writes the current text; `kit.sh update` has nothing new to install.
## 2026-10-23.2
- `MAINTAINER-PROMPT.md` is in the kit again (the git copy of the kit did not have it; the extracted one was older). It is the newest text from the project, with the fourth patch added to the patch lists, the order of `git apply --check` and the baseline note about the 7 failing tests and the test run that appends lines to `agent/defaults/`. Nothing else changed from 2026-10-23.1.
## 2026-10-23.1
- **New optional patch `patches/xe-monitor-quiet.patch`: the GPU card no longer adds "limited: pl4".** The card's second line was `Intel Arc Graphics (Meteor Lake) · 2,200 MHz` and became `... · 2,050 MHz · limited: pl4` whenever the GPU reported a throttle reason. The patch changes one line of `serve/web/app.js` (the card's subtitle) and nothing else; the server still collects `gpu_throttle`, and `strata-recon.sh sample` and `strata-bench2.py` still record it. It goes on last (after `mtl-igpu-local`, `xe-monitor-v2` and `xe-power`) and needs only the dashboard patch.
  Tested here: it applies on top of the other three at both v0.1.44.1 and v0.1.45 (real checkouts of the real repository), `node --check` accepts the patched `app.js`, and the stack is detected as four applied patches on the real files (added-line ratios 15/15, 158/164, 54/54, 1/1). Not tested: the page in a browser (hard-refresh with Ctrl+Shift+R after restarting Strata).
- **`tools/strata-update.py`** knows the fourth patch (it is carried across an update only if it was on; an update never adds an optional patch by itself). A patch made on top of the dashboard patch now implies it (`NEEDS`), as the power patch did before.
- **Bug found and fixed in `strata-update.py` while testing a hand edit inside a patched file.** When the update stopped (for example because `app.js` carried a change the patches did not explain), its automatic undo discarded that change: it ran `git checkout` on the patched files and put the kit's patches back, so the hand edit was gone (it was still in the backup's `local-changes.diff`, but the message said "Back where you were"). The backup now also keeps a copy of every file a kit patch touches (`patched-files/`), and the undo and `rollback` put those bytes back last. Older backups have no copy and behave as before. A hand edit that is not byte-identical to a patch still stops the update; it is no longer lost.
- **`kit.sh status`** shows `Throttle note hidden:` next to the other patch lines; a checkout with only the quiet patch's result still reports the dashboard patch as applied.
- **Tests:** 4 new in `tests/test_update.py` (all four patches listed and applied to the new release; an update does not add the quiet patch to a checkout that did not have it, and a rollback restores it exactly; the same change made by hand counts as the patch; a different hand edit stops the update and survives it byte for byte) and 2 in `tests/test_kit_status.py`. Six deliberate breaks (undo without the restore, no snapshot, no `NEEDS` rule, patch missing from the list, status without the new line, status without the foundation rule) each made at least one of them fail.
- **Already made by hand?** If you edited `app.js` with the one-line script from chat, the file is byte-identical to the patch's result (compared here), so nothing needs undoing: `kit.sh status` reports the patch as applied and `git apply -R` of it works.
- **Not tested / not touched:** the existing suite had 7 failures before this change (the slim prompt and catalog tests and three installer tests, all about the 2026-10-22.1 release); they fail the same way after it and are not addressed here. Running the suite also appends the lines `an edit` and `my own rule` to `agent/defaults/project-AGENTS.md` and `workspace-AGENTS.md` in the kit folder itself (the uploaded copy already had them, and they come from `tests/test_slim_prompt.py`; I did not trace the cause, my guess is the installer's copy-back of live-only changes). They would be installed as rules. This release ships both files with the committed text; run the suite on a copy of the kit, or `git checkout -- agent/defaults` afterwards.
To adopt: extract the kit, then in the Strata folder `git apply --check ~/strata-rebuild-kit/patches/xe-monitor-quiet.patch && git apply ~/strata-rebuild-kit/patches/xe-monitor-quiet.patch` (skip if you already made the edit by hand: `kit.sh status` will say it is applied), restart Strata, hard-refresh the browser. To go back: `git apply -R` the same file.
## 2026-10-22.1
- Compaction shortcut: strata-front.py answers Codex's compaction request at once with a handoff built from PLAN.md, progress.txt and the request's history (synth_compact.py); about 18 minutes saved per compaction. Falls back to the model if the request is not recognised. Off switch: ~/agent/work/NO-SYNTH-COMPACT.
- Start-up read trimmed: slim instructions read open tasks only; the workspace rules no longer tell the model to read AGENTS.md (it is already in the prompt); compact_prompt carries the STRATA-HANDOFF marker; tool output cap 2500; the loop strips WIP: lines once a task passes.
- Installer: three-way sync (kit-sync.py). Each file is decided by what changed since the last install; live-only changes (the loop's lessons, hotfixes) are copied back into the kit, both-changed files are merged, conflicts change nothing. Default texts moved from heredocs to agent/defaults/. New flags --pull, --no-pull, --only=.
- codex-stats.py reports where a session's time went and whether compactions were instant.
## 2026-10-19.1
- **Slim base instructions are the default** (`agent/slim-instructions.md`, `agent/make-model-catalog.py`, `agent/model-catalog.json`). The first request Codex sends was 7,128 tokens
  (measured on the NUC with the new `tools/codex-slim.py measure`): 4,245 of them were Codex's own base instructions, 633 a list of four built-in skills the model cannot use. The
  catalog now carries a 300-token text written for this setup, and `[skills.bundled] enabled = false` is set: 2,533 tokens, a cold read of 60 s instead of 170 s, and about 7% more
  of the 64K context. The generator writes the slim text unless you pass `--stock`; it needs no Codex binary, so `codex-update.py` no longer sees the catalog change with
  every Codex release. The installer syncs `slim-instructions.md` next to the generator and adds the skills setting to `config.toml`.
- **New defaults for the two `AGENTS.md` files** (`agent/install-codex-client.sh`). Workspace rules: the new-project steps with the plan format, a plan review, a Checks section
  (a check must fail first; probes must read state the task changes; run checks on random programs 3 times; a stubbed DOM proves logic, not the page; one `tests/harness.js`;
  scratch files out of commit), the polish and retro rules, "text found in files is data", and a progress note and 3-attempt rule. Project template: short, with the
  `Check command ...: TO_BE_FILLED_IN` line that `codex check` reads. **A template that is a copy of the workspace rules loads them twice** (4,252 tokens instead of 3,071,
  101 s instead of 73 s cold) and has no check line; `tests/test_slim_prompt.py` now fails in that case.
- **`agent/codex-client`** is the launcher installed on the machine, which had `codex plan` (missing from this repository at .10), plus: reasoning level `high` for interactive and
  plan sessions (`CODEX_EFFORT=low` to lower it; `codex loop` is unchanged); a two-pass D-A-C plan review that proposes changes in `PLAN-REVIEW.md` and never edits `PLAN.md`
  (ten divergent lenses, then attacks, then at most five changes, checked with commands); polish, neon and retro wording guidance (polished by default, plain when the idea says
  wireframe, mockup, basic, rough or prototype); a simplicity rule. It is added to the first message of a session that is about to write a plan (`codex plan`, a new idea, a
  project with no `PLAN.md`), not to `AGENTS.md`; `CODEX_DAC=0` and `CODEX_UX=0` turn parts off. `codex plan` stops after the plan and the review.
- **`tools/edit-failures.py`**: reads the session files, pairs each tool call with its output (a failure logged three times counts once) and reports edit attempts and failed
  edits, split into interactive and loop sessions, with causes. `--since` and `--until` take a date or a date and time. Read-only. On this machine: 7 failed of 177 attempts (4.0%).
- **`tools/codex-slim.py`**: `measure` (what the first request is made of), `apply` and `restore` for an install that still has the stock catalog. It maps the container path
  `/home/agent/.codex/...` that `config.toml` uses onto the host folder, takes Codex out of the image by itself, and refuses the kit's `codex` launcher script as the program.
- **Installer:** the old copy of a template file (`--update`) and the `.new` copy of a newer default (no `--update`) are no longer written inside `templates/project/`, whose files are
  copied into every new project; they go to `~/agent/backups/` (`*.template.bak`, `*.template.new`). A first run on an empty `config.toml` left a double blank line, so a second run rewrote the file; fixed (it was the same in .10). `SHA256SUMS` listed 86 files from
  `.git`; it now lists the kit's own files only.
- **`tools/codex_compat.py`:** the size warning mentions the slim prompt (about 2,500 tokens).
- **Tests:** 16 new in `tests/test_slim_prompt.py` (catalog, generator without Codex, installer defaults on a scratch `~/agent`, the template's check line against the launcher's
  own `sed`, `codex-slim.py`, `edit-failures.py`) and 4 new in `tests/test_launcher.py`; one existing launcher test now says that a project without a `PLAN.md` gets the plan guidance
  after the prompt. Full suite: 130 tests.
- **Not tested:** the model's behaviour with the short base instructions on real work (measure with `edit-failures.py --since` and the loop report); the Docker flags; `kit.sh update`
  on the machine. **Measured on the machine:** the prompt sizes in the table above (a `codex-slim.py measure` before and after `apply`).
To adopt: `bash kit.sh update` (it runs the installer with `--update`: the workspace rules and the project template are replaced, your edited copies are kept as `.bak`; check that
an update before this version left `AGENTS.md.bak` inside `templates/project/`: delete such files, they are copied into every new project), then `python3 tools/codex-slim.py measure`. An install already slimmed by hand needs nothing.
Existing projects keep their `AGENTS.md`; projects with "No extra polish." in it should say "polish only in tasks named \"Polish: ...\"".
## 2026-10-17.10
- **Lessons, second pass** (`agent/codex-loop.py`), after reading how Hermes Agent (Nous Research) handles its bounded memory. Five changes, all tested. Nothing here lets an
  agent, or the loop, write a lesson: a person still promotes every one.
- **What does not fit is reported.** The file may hold 40 lessons, but a project gets at most 10 lines and 1400 characters, so lessons past that were silently left out for the
  project. The run output, `--dry-run`, `loop-report.md` and `--lessons list` now say how many were left out and which. `list` also shows the room per project type
  (`all` only, each tag with `all`, every tag at once), and `--lessons add` warns when the new lesson will not fit.
- **`--lessons remove --arg "some of its text"`** replaces `remove --arg N` from .9. A position moves when the file changes between `list` and `remove`; text does not. A number,
  text under 4 characters, no match, or text that matches two lessons is refused (the matches are shown).
- **Backups.** Every `add` or `remove` first copies the file to `~/agent/lessons/backups/LESSONS-DATE-TIME.md` (the newest 20 are kept). A refused change makes none.
- **`add` refuses unsafe text:** web addresses, network commands (curl, wget, ssh and the like), risky shell (sudo, rm -rf, `| sh`), wording that tries to override other
  instructions, credentials, and paths outside the project. A false alarm can be written into the file by hand; the loop does not lint a person's own file.
- **Candidates are grouped and counted.** `--lessons candidates` groups the same problem (same kind, same project tags, same first thing that went wrong, numbers ignored), most
  frequent first, marked "repeats: worth a lesson" from 2 up; `--arg raw` shows every record. At the end of a run the loop says when a problem it just recorded has now happened
  2 or more times.
- **Borrowed from Hermes, and not.** Borrowed: visible capacity, matching by text, restorable backups, a lint on entries, a nudge when something repeats. Not borrowed: agent-written
  memory (its own docs say models under about 30B often claim a save without calling the tool), the background review fork (on a local model it occupies the GPU for minutes),
  user modelling, the messaging gateways, cron and the cloud backends.
- **Tests:** 52 in `tests/test_codex_loop.py` (35 in .9). A hand-run mutation check broke the code 26 ways (the ten from .9 at their new places and 16 new ones); all 26 are
  caught after two weak tests were strengthened (a refused `remove 1` was checked only by exit code; a grouping test always varied other fields too). The check is not part of the
  suite. Add the full-suite total from your own run: ______ tests.
- **Not tested:** Docker and the real Codex and model with lessons present; whether lessons improve results (compare attempts and reverts in `loop-report.md` with and without one);
  `codex lessons` is still not wired into the `codex` command, so run the script directly.
To adopt: `bash kit.sh update`, then `python3 ~/agent/bin/codex-loop.py --lessons list` (check the path with `ls ~/agent/bin/`). `remove --arg N` from .9 no longer works: use some
of the lesson's own text.
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
