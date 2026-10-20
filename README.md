# Strata rebuild kit

Everything needed to rebuild the Strata system on the NUC (Meteor Lake iGPU, Swift 1.5 IQ3_XXS, Strata v0.1.44.1), to run the
sandboxed Codex agent on it, and to measure it. The version is in `VERSION`; what changed is in `CHANGES.md`.

## Just extracted it? Start here

Extracting changes **nothing** on your system. It only puts files in `~/strata-rebuild-kit`. The next step is one command:

```bash
bash ~/strata-rebuild-kit/kit.sh status      # read-only: what is installed, what is out of date
bash ~/strata-rebuild-kit/kit.sh update      # asks first; touches only ~/agent (the codex command and what its sessions read)
codex                                        # then use it (codex help)
```

| Your situation | What to do |
|---|---|
| A working system, and you just extracted a newer kit | `kit.sh status`, then `kit.sh update`. It never touches the Strata source, its config or the patches (they change Strata itself). If `status` says a patch is "not applied" and you want it, follow the playbook's Step 8 |
| Rebuilding from scratch | Follow `strata-nuc-playbook.md` section 2 in order (Step 9 installs the `codex` command), then `kit.sh status` |
| Checking the live machine against this kit | Playbook section 11 |
| Wondering what is new | `CHANGES.md` |

**Unpack the newest download** (a browser names a second download `... (1).tar.gz`, and extracting the old one by mistake leaves you with an old kit):

```bash
tar xzf "$(ls -t ~/Downloads/strata-rebuild-kit*.tar.gz | head -1)" -C ~ && cat ~/strata-rebuild-kit/VERSION        # prints the version you just unpacked
sha256sum -c ~/strata-rebuild-kit/SHA256SUMS | grep -vc ': OK'   # 0 = every file matches what was packed
```
The version is also in the file name (`strata-rebuild-kit-VERSION.tar.gz`); `kit.sh status` prints it first.
Once, on the working machine: `bash ~/strata-rebuild-kit/tools/capture-machine-files.sh` records what only that machine has.

## What is in it

| Folder or file | Contents |
|---|---|
| `kit.sh` | `status` and `update`: the step after extracting |
| `strata-nuc-playbook.md` | The rebuild and operating guide |
| `patches/` | `mtl-igpu-local.patch` (Meteor Lake fix), `xe-monitor-v2.patch` then `xe-power.patch` (dashboard readings) |
| `config/` | The settled Strata config, `start-swift.sh`, chat save and restore, the TTM page-pool cap |
| `tools/` | Tuning orchestrator, benchmark, recon snapshot, config editor, machine-file capture, `strata-models.py` (remove a model, change settings), `strata-update.py` (move Strata to a newer release and back), `codex-update.py` and `codex_compat.py` (the same for Codex, behind a 13-probe compatibility suite), `strata-effort-position.py`, `codex-slim.py` (what Codex's first request is made of; slims it) and `edit-failures.py` (how often edits fail in real sessions). Run them from here |
| `agent/` | The sandboxed Codex agent: `strata-agent-setup.sh` (image), `install-codex-client.sh`, `codex-client` and `strata-front.py` (the `codex` command), `browser-check.py` and `add-browser-to-image.sh` (real-browser checks for web projects), `codex-loop.py` and `unix-relay.py` (`codex loop`: the unattended loop and its network gate), `model-catalog.json`, `make-model-catalog.py` and `slim-instructions.md` (the reasoning shortcuts and the short base instructions) |
| `tests/` | tests (the loop, the model catalog, the effort tool, the launcher, the title filter, patch detection, and the model, Strata-update and Codex-update tools): `python3 -m unittest discover -s ~/strata-rebuild-kit/tests` |

## What is NOT in it, and why

The model files (about 76 GB; playbook Step 5), the generated `run-*.sh` scripts (written by Strata's setup; the capture script saves the working copies), and the old loop script `agent-ralph.sh` (retired: it trusted the agent's own task ticks; `codex loop` replaces it).

Keep a copy of this kit off the machine. It contains paths and settings, no passwords, keys or chat files.
