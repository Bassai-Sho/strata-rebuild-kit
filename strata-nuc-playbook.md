# Strata on the NUC: rebuild and operating playbook

Updated 16 October 2026 for **Strata v0.1.44.1** (commit `296b59d2`). It replaces the 15 October version and keeps only what is needed to rebuild the system and run it. The settled settings, the patches and the tools are in the **rebuild kit** (`~/strata-rebuild-kit/`, listed in section 12). Keep a copy of the kit and this file off the machine: the machine is what you would be rebuilding.

**Unpack the newest download** (a second download is named `... (1).tar.gz`; unpacking the older one leaves you with an old kit): `tar xzf "$(ls -t ~/Downloads/strata-rebuild-kit*.tar.gz | head -1)" -C ~ && cat ~/strata-rebuild-kit/VERSION`, which prints the version.

**Just extracted a newer kit?** Nothing on the system has changed yet. Run `bash ~/strata-rebuild-kit/kit.sh status` to see what is out of date, then `bash ~/strata-rebuild-kit/kit.sh update` (it asks first and touches only `~/agent`). Strata itself, its config and the patches are changed by hand, following this file. `CHANGES.md` in the kit says what is new.

Tags: **(verified)** = run on this machine (the first install on 12 to 13 October is in the project's earlier chats; the rest is in this one). **(docs)** = read in Strata's or a vendor's documentation, not run here. **(untested)** = suggested only or not yet checked on the machine; section 11 lists the commands that check the few that remain (the RAPL service unit, the patch equality, the pins).

---

## 0. Rules

1. **One model at a time.** RAM is shared with the GPU. Stop the server before starting another model.
2. **Start with `~/start-swift.sh`.** It frees GPU-held memory first and refuses to start if memory looks wrong.
3. **Never remove `--prefill` from the config**, and keep it at `2048`. The server refuses to start without it (`needs --spec T and --prefill CHUNK`).
4. **`--short-read 64` is safe only while all 24,576 experts are in the GPU cache** (check the start log, section 3). Anything that shrinks the cache, including `--prefill 4096` or `8192`, is unsafe here: the batched prompt path hung when experts came from the host mirror.
5. **Back up a config before editing it** (`cp FILE FILE.bakN`). Update Strata with `strata-update.py` (section 9). Never run `./update.sh`, `./setup.sh --update` or `./setup.sh --setup` without `--model`: they can regenerate configs and drop the settings below, and setup now defaults to IQ3_S sizes.
6. **Never apply a patch, edit a Strata file or restart Strata while a tuning run is in progress.** The orchestrator restarts Strata before every batch, so a mistake lands in the middle of a run (it did once).
7. **Anything the Intel launcher overrides must be tested through the launcher.** `sycl/serve/server_intel.py` replaces `telemetry.gpu_reader`, so a change to `serve/telemetry.py` that passes its own unit tests can still stop Strata starting (it did once).
8. **The coding agent runs only in its container, started with the `codex` command (Step 9), on projects under `~/agent/work`.** Review what it wrote before copying anything out; keep secrets and real repositories out of that folder.
9. **Stop the server, not just the container.** The API on port 8085 is served by a host process that starts the engine container as its child. Killing only the container leaves the server answering and lets it restart the engine.

---

## 1. The machine

| Item | Value |
|---|---|
| Hardware | ASUS NUC14RVH-B, Core Ultra 5 125H (14 cores, 18 threads), Arc iGPU (Meteor Lake-P, `8086:7d55`, driver `xe`), BIOS `RVMTL357.0054` |
| Memory | 62 GiB DDR5-5600, shared with the GPU |
| Disk | `/home` on a 274 GB NVMe. Models are 68 to 76 GB each; keep at least 10 GB free for chats and builds (it was 14 to 15 GB free on 16 October: tight). |
| OS | Ubuntu 24.04, kernel 7.0.0-38-generic |
| Engine | Strata v0.1.44.1, JIT build `build-sycl/strata`, in Docker image `strata-sycl-dev` (built from `llama-sycl-b70:stable`; oneAPI DPC++/C++ 2026.1.0, NEO 26.35.39758.10) |
| Model (main) | Swift 1.5 IQ3_XXS: `Swift-Qwen3.8-Flash-Next-GSQ-RCO-IQ3_XXS-0000{1,2}-of-00002.gguf`, about 76 GB. A fine-tune of Qwen3.8-Flash-Next 125B MoE that thinks more briefly. |
| Model (fallback) | Qwen3.8-Flash-Next IQ2_XS, about 64 GB: `run-iq2_xs.sh`, `strata-iq2_xs.json` |
| Addresses | Chat page `http://127.0.0.1:8085/` and OpenAI API `http://127.0.0.1:8085/v1` (use the Strata page, not NextChat) |

---

## 2. Rebuild from scratch

### Step 1. Host

```bash
sudo apt install docker.io git python3 python3-venv jq curl     # Ubuntu's docker.io, not the snap (snap confinement breaks --device and mounts)
sudo usermod -aG docker,render $USER                            # then log out and in, or reboot; `render` is needed to reach the GPU from a container
mkdir -p ~/github/strata-work/sessions
sudo ln -s ~/github/strata-work /work                    # the engine in Docker sees the sessions folder at the same path
# cap the xe driver's freed-page pool (default 31 GiB, not counted in MemAvailable):
echo "options ttm page_pool_size=262144" | sudo tee /etc/modprobe.d/ttm-pool.conf     # kit: config/ttm-pool.conf
echo 262144 | sudo tee /sys/module/ttm/parameters/page_pool_size
sudo update-initramfs -u
```
After the next reboot `cat /sys/module/ttm/parameters/page_pool_size` must print `262144` (verified after the 16 October cold boot).

Nothing else on the host matters to Strata: the engine container brings its own compute stack (NEO 26.35.39758.10, IGC 2.41.5, Level Zero 1.32), so host GPU userspace packages and any pinned OpenVINO stack are irrelevant. Only the kernel's `xe` driver is used from the host.

### Step 2. Source

```bash
cd ~/github/strata-work
git clone https://github.com/Niko1221/Strata.git && cd Strata
git checkout -b local v0.1.44.1 && git rev-parse --short HEAD        # 296b59d2
```

### Step 3. The local Meteor Lake patch (still needed: upstream has no Meteor Lake support in 0.1.45 or in 0.2.3)

```bash
git apply --check ~/strata-rebuild-kit/patches/mtl-igpu-local.patch && git apply ~/strata-rebuild-kit/patches/mtl-igpu-local.patch
git diff --stat                                           # 2 files, 15 insertions, 3 deletions
```
**On Strata 0.2.2 and newer use the remade patch instead**: `patches/mtl-igpu-local.0.2.patch` (the same change; upstream rewrote `sycl/setup_intel.py` around it, and the original does not apply there; it applies to v0.2.2 and v0.2.3, not to v0.2.1.x). `strata-update.py` and `kit.sh status` pick the file that fits the tree by themselves. The remade one also leaves upstream's new "fit rule" (it shrinks context, reserve and prompt chunk for a card that is just short) off for this GPU, so the measured settings are not altered; that rule has not been tried here.
It does three things for PCI id `7d55` only: flags the GPU as integrated (`sycl/include/dpct/device.hpp`: without it free memory reads 0 and the cache has 0 slots), gives setup the right memory size, and writes the env settings in section 3 into generated configs. The kit's copy was taken from the upstream issue draft (written against 0.1.43.1); it re-applied cleanly on 0.1.44.1 here. If `--check` fails, try `git apply --3way`; if that fails, recreate the three changes from the patch text.

### Step 4. Build the engine, and check the kernels on this GPU

```bash
cd ~/github/strata-work/Strata
# Pin the build environment: pull the base image by digest, then tag it :stable so the Dockerfile's FROM line resolves to it.
B=ghcr.io/snailium/llama.cpp-sycl-intel-b70/llama-sycl-b70
D=sha256:b5799bf3f66bc1fb5ed6e52534e8f6e9e84bab57105f83ae210fd168208d8b41        # the index digest of the image pulled on 12 October
until docker pull $B@$D; do echo "retrying..."; sleep 5; done                  # about 3.2 GiB; 9 minutes at 6 MiB/s; resumes after the resets seen on this link
docker tag $B@$D $B:stable
docker run --rm --device /dev/dri --entrypoint /bin/bash $B:stable -lc 'sycl-ls'   # expect level_zero:gpu "Intel(R) Arc(TM) Graphics 12.71.4" and opencl:gpu (NEO 26.35.39758.10)
docker build -t strata-sycl-dev sycl/tools                 # adds oneAPI's migration tool and build tools; apt runs at build time, so a rebuild can differ slightly
touch sycl/CMakeLists.txt                                  # forces a version reconfigure
H=$(basename "$PWD")
docker run --rm -u $(id -u):$(id -g) -v "$PWD/..:/work" -e REPO=/work/$H -e BUILD_DIR=/work/$H/build-sycl strata-sycl-dev "cd /work/$H && bash sycl/tools/build.sh strata"
grep -a -o 'engine=[0-9.]*' build-sycl/strata | head -1     # engine=0.1.44.1 (its exit status can be 1 even on success: see below) ; the build ends with BUILD EXIT 0 and errors: 0 (about 3 to 20 minutes)
./setup.sh --low-ram auto --backend sycl --check           # read-only. Expect: Intel Arc Graphics (Meteor Lake-P, shared RAM), 45.8 GB VRAM
```
Note on the build's exit status: `sycl/tools/build.sh` runs under `set -o pipefail` and ends with `grep ... | head`, which finds nothing to list after a successful build and exits 1, while the script has just printed `BUILD EXIT 0` and `errors: 0`. Judge a build by those two lines and the engine's version, not by the exit status (`strata-update.py` does; reproduced here). **Do not patch `build.sh` in your checkout**: it is a tracked file, so `strata-update.py` would refuse to update ("changes the patches do not explain") and the next release would conflict. If you already did: `git -C ~/github/strata-work/Strata checkout sycl/tools/build.sh`.
A pull can look frozen: under Docker's containerd image store the first minutes show no byte counters, and a connection reset on one IPv6 address just restarts the layer (the retry loop handles it). Turning IPv6 off made the stall worse on 12 October; `sudo systemctl restart docker` and the retry loop did not.

**Check the kernels on this GPU (verified 12 October, before any model was loaded).** Three small tests, each must print `OK` and `exit=0`:

```bash
R=$(getent group render | cut -d: -f3)
docker run --rm --device /dev/dri --group-add $R -u $(id -u):$(id -g) -v "$PWD/..:/work" strata-sycl-dev \
  'cd /work/Strata && for t in quantize_act s2_gemv sampler; do echo "== $t"; timeout 300 build-sycl/${t}_parity --selftest 2>&1 | tail -3; echo "exit=${PIPESTATUS[0]}"; done'
```
The full suite (an extra build of 82 steps, estimated at 15 to 20 minutes, then 215 s of tests) gave **28 of 30 passing**: `cmake --build build-sycl && ctest --test-dir build-sycl --timeout 300 -E "ple_parity|iq_parity"` inside the same container. The two failures are expected: `s2_expert_grouped_parity` also fails on upstream's own Arc Pro B70 (a Q2_0 path the IQ models do not use), and `gr_parity` fails only in its `STRATA_QFUSE` variants (the fused kernel never writes its output). `STRATA_QFUSE` is off unless the environment sets it and is defaulted on only for AMD gfx12 and gfx1151, so **never set `STRATA_QFUSE` on this machine**.

### Step 5. Model, pack, draft head, expert profile

Strata's own setup does all of it (download, convert, pack, draft head, expert profile, `run-*.sh` and the config). The command that made the current install (verified 13 October; setup describes Swift 1.5 as a fine-tune by UkisAI that thinks much shorter, and `--family swift` is the documented way to pick it):

```bash
cd ~/github/strata-work/Strata
df -h /home                        # IQ3_XXS is about 76 GB plus about 5 GB for the draft layer: keep 90 GB free
./setup.sh --setup --backend sycl --family swift --model IQ3_XXS --context 16384 --vision no --low-ram auto --port 8085 --no-start \
           --models-dir ~/github/strata-work/models --data-dir ~/github/strata-work/data
```
The first time it was run without `--family` and without the two folder flags; the menu appeared and entry `2` (Swift 1.5) was chosen, and the files landed in `data/models/swift-IQ3_XXS`. Setup remembers the folders. It should not need sudo (stop and check if it asks). If the download stops, run the same command again: it resumes. Expect hours on this link (the base-image pull ran at about 6 MiB/s). `--no-browser` stops the chat page opening by itself.

- **Do not pass `--context 65536`.** For contexts above 32K setup writes `--prefill 4096` (and may add `--kv-resident`), which shrinks the expert cache and makes the batched path unsafe (section 6). Use 16384 here and raise it by editing the JSON in Step 6.
- **Do not accept setup's size recommendation (IQ3_S)** or run it without `--model`.
- **Files you already have:** put the two shards in `data/models/swift-IQ3_XXS` under their original names (`Swift-Qwen3.8-Flash-Next-GSQ-RCO-IQ3_XXS-00001-of-00002.gguf` and `...-00002-of-00002.gguf`), or pass `--gguf-dir FOLDER`. The source is the Hugging Face repo `ukisai/Swift-1.5-Qwen3.8-Flash-Next-GSQ-RCO-GGUF`; the license is the Swift Open License 1.0 (setup prints the link).
- **Expected at the end:** `run-swift-iq3_xxs.sh` and `strata-swift-iq3_xxs.json`, `--pool-workers 8` chosen for the hybrid CPU, an `env` block with the patched values (`STRATA_MIRROR_MIB` 1024). The engine path `/tmp/strata-sycl-.../strata` it prints is a throwaway stub; the real engine is `build-sycl/strata` (`STRATA_SYCL_BIN`).
- **Fallback model (verified 12 to 13 October):** `./setup.sh --backend sycl --no-start --family qwen --model IQ2_XS --context 8192 --port 8085 --no-browser --models-dir ~/github/strata-work/models --data-dir ~/github/strata-work/data` (about 68 GB plus 5 GB).

The result must look like this (host paths; inside the engine container the prefix is `/work/`):

```
~/github/strata-work/data/models/swift-IQ3_XXS/Swift-...-IQ3_XXS-0000{1,2}-of-00002.gguf     (about 76 GB)
~/github/strata-work/data/packs/swift-iq3_xxs/                                               (the pack and its tokenizer)
~/github/strata-work/data/mtp/rt                                                             (the draft head)
~/github/strata-work/Strata/data/expert-profile-sycl.bin                                     (the expert profile)
```

### Step 6. Config, run script, start script

```bash
cd ~/github/strata-work/Strata
cp strata-swift-iq3_xxs.json strata-swift-iq3_xxs.json.generated      # whatever setup wrote
cp ~/strata-rebuild-kit/config/strata-swift-iq3_xxs.json .              # the settled config (section 3 and Appendix A); fix /home/adam if your user differs
cp ~/strata-rebuild-kit/config/start-swift.sh ~/ && chmod +x ~/start-swift.sh
cp ~/strata-rebuild-kit/config/chat-save.sh ~/strata-rebuild-kit/config/chat-restore.sh ~/ && chmod +x ~/chat-*.sh
```
`run-swift-iq3_xxs.sh` is generated by setup; the kit's machine snapshot keeps the working copy.

### Step 7. First start and checks

```bash
~/start-swift.sh                    # wait for "ready: http://127.0.0.1:8085/v1" (about 1.5 to 2 minutes)
cd ~/github/strata-work/Strata
grep "expert cache auto" strata-swift-iq3_xxs.log | tail -1 | cut -c1-120      # about 58 GiB free ... -> 24576 slots
grep "session is up" strata-swift-iq3_xxs.log | tail -1                         # engine 0.1.44.1
grep -c "REFUSED\|no progress for" strata-swift-iq3_xxs.log                     # 0
python3 ~/strata-rebuild-kit/tools/strata-bench2.py check                       # flags show --max-context 65536 --short-read 64 --spec 4 ...
python3 ~/strata-rebuild-kit/tools/strata-bench2.py run first --runs 5 --prefill-runs 1     # about 10 minutes
```
Expect decode about 8.6 to 8.7 tok/s and prefill about 42 tok/s on real code (section 7). The first request after a start takes 17 to 18 s while the GPU code compiles.

### Step 8. Monitor patches (optional; the dashboard shows Intel GPU load, temperature and clock; an optional last patch hides the throttle note)

```bash
cd ~/github/strata-work/Strata
git apply --check ~/strata-rebuild-kit/patches/xe-monitor-v2.patch && git apply ~/strata-rebuild-kit/patches/xe-monitor-v2.patch
python3 -m unittest sycl/serve/test_xe_telemetry.py                    # Ran 10 tests ... OK
git apply --check ~/strata-rebuild-kit/patches/xe-power.patch && git apply ~/strata-rebuild-kit/patches/xe-power.patch
python3 -m unittest sycl/serve/test_xe_telemetry.py                    # Ran 11 tests ... OK
git apply --check ~/strata-rebuild-kit/patches/xe-monitor-quiet.patch && git apply ~/strata-rebuild-kit/patches/xe-monitor-quiet.patch     # optional: the GPU card's second line stops adding "· limited: pl4"
sudo install -m 755 sycl/tools/rapl_power.py /usr/local/sbin/strata-rapl-power
sudo install -m 644 sycl/tools/strata-rapl-power.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now strata-rapl-power
sleep 3; cat /run/strata/power.json                                    # {"ts": ..., "power_w": ..., "source": "rapl-package"}
```
Restart Strata and hard-refresh the browser (Ctrl+Shift+R). The patches change `sycl/serve/xe_telemetry.py` and `serve/web/app.js` only (power needs the root service; it publishes watts and keeps the raw RAPL counter root-only). The quiet patch changes one line of `serve/web/app.js` (the card's subtitle) and nothing else: the server still collects the throttle reason (`gpu_throttle`, also in `strata-recon.sh sample` and `strata-bench2.py`); only the dashboard no longer shows it. If you made that edit by hand, it is the same bytes as the patch, so `kit.sh status` shows it as applied and `strata-update.py` carries it across updates. Undo: `git apply -R` the patches in reverse order (quiet first, then power, then the dashboard patch), and disable the service. The unit file has not been checked with `systemd-analyze` yet (section 11, item 4).

### Step 9. The `codex` command: Codex on the local model, in a container (optional)

One-time setup (the image first, built with the Codex version in the kit's `agent/CODEX_VERSION`; then the client). Codex is not pinned for good: `codex-update.py` (Section 9) moves it to a newer release and back:

```bash
SKIP_BUILD=1 bash ~/strata-rebuild-kit/agent/strata-agent-setup.sh                # writes ~/agent/ (Dockerfile, codex-home/config.toml, bin/agent-run.sh, work/)
V=$(cat ~/strata-rebuild-kit/agent/CODEX_VERSION)                                    # 0.164.0 passed every probe (one warning, see section 9); 0.163.0 passed too; 0.162.1 is what ran on this machine first
docker build --build-arg CODEX_URL=https://github.com/openai/codex/releases/download/rust-v$V/codex-x86_64-unknown-linux-musl.tar.gz -t codex-sandbox ~/agent
docker run --rm codex-sandbox codex --version                                      # codex-cli $V
bash ~/strata-rebuild-kit/agent/install-codex-client.sh                            # the `codex` command; safe to run again (--update adopts newer default rules and template)
bash ~/strata-rebuild-kit/agent/add-browser-to-image.sh                            # a real headless Chromium in the image for real-browser checks (about 300 MB, a few minutes; optional, needed for web projects)
```
Later, after extracting a newer kit, the same installer is run for you by `bash ~/strata-rebuild-kit/kit.sh update` (`kit.sh status` first shows what would change; `install-codex-client.sh --check` is the read-only part of it).

The installer's Dockerfile defaults to `releases/latest`, hence the pinned build above (release assets follow `releases/download/rust-vVERSION/`, the convention distro packages use).

**Using it.** Nothing to set up per project:

| You type | What happens |
|---|---|
| `codex` | New project. Codex asks what to build, copies the project skeleton, writes `README.md` and `PLAN.md`, commits, and starts on task 1. It tells you the name to use next time. |
| `codex "build a snake game"` | The same, with your first message already typed |
| `codex NAME` | Work on project `NAME` (created empty if it does not exist). A fresh session each time: the project's `PLAN.md` and `AGENTS.md` carry the state, which suits a 64K context and a slow model |
| `codex --auto ["idea"]` / `codex NAME --auto` | **Autopilot**: Codex works through the whole plan without waiting for you after each task (see "Proceeding without waiting" below) |
| `codex NAME --spec FILE` | Copy your design into the project as `SPEC.md` and start: the session plans from it and asks nothing |
| `codex NAME resume` | Continue the previous conversation (re-reads the whole history: slower) |
| `codex loop NAME` | Run the plan unattended, one task per fresh session; the loop verifies each task itself (Step 9b) |
| `codex gate-test` | Prove the loop's container can reach only Strata (Step 9b) |
| `codex check NAME` | Run the project's own check in a **fresh container with no network** and show the result, the tasks ticked and the screenshots: a test the agent did not run itself |
| `codex ls` / `status` / `stop` | List projects with task counts / is Strata up and is a session running / stop Strata and free about 40 GB |

If Strata is not running, `codex` offers to start it (it asks for your sudo password once, frees memory like `start-swift.sh`, starts the server in the background and waits for it). Projects are `~/agent/work/NAME` on the host and `/work/NAME` in the container, which sees only that folder. The files the sessions read are `~/agent/codex-home/AGENTS.md` (the workspace rules: how to start a project, how to work in one) and `~/agent/codex-home/templates/project/` (the skeleton). Edit them freely; `install-codex-client.sh --update` restores the defaults and keeps your copies as `.bak` (for the template files, in `~/agent/backups/` as `*.template.bak`, because a backup inside `templates/project/` would be copied into every new project).

**What the installer sets in `~/agent/codex-home/config.toml`** (backup `config.toml.bak-client`), each checked against the real Codex binary (0.162.1 and 0.163.0):

| Setting | Why |
|---|---|
| `[features] daemon_auto_start = false` | **Interactive Codex fails without it** ("this CLI has no complete local package": it tries to start a background server the standalone binary cannot). This is why the old wrapper's `-i` mode never worked; only `codex exec` did. |
| `[features] goals, multi_agent, apps, plugins, view_image, worktrees = false` | A local model cannot use them and they cost tokens. The prompt Codex sends fell from about 12,300 to about 7,200 tokens in interactive mode (about 2 minutes less on every cold read at 42 tok/s). |
| `model_context_window = 65536`, `model_auto_compact_token_limit = 48000`, `tool_output_token_limit = 4000` | Matches Strata's 64K context; compaction is rare at this size and slow when it happens (about 12 minutes at 32K); large command output cannot flood the context |
| `model_catalog_json` (a file listing the `strata` model) | **Without it the reasoning shortcuts (Alt+. and Alt+,) say "unavailable for strata"**: Codex enables them only for a model whose levels it knows, and `strata` is not in its built-in list. The catalog gives it the levels none, low, medium and high (exactly what Strata's server accepts) and the same tools and base instructions as before |
| `[model_providers.strata_front]` (the interactive screen uses it; `agent-run.sh` keeps the direct `strata`) | **The screen asks the model for a session title on every prompt** ("Generate a concise, single-line task title"). Strata has one conversation slot and reads at 42 tok/s, so that extra ~6K-token request costs about a minute and pushes the real conversation out of the cache. No setting turns it off, so `strata-front.py`, a 130-line local filter on port 8086, answers that one request itself and streams everything else to Strata unchanged. `codex` starts it when needed and `codex stop` stops it. With it the model server received 4 requests for the new-project flow instead of 6. |
| launcher: approval policy `never` (`CODEX_APPROVAL=untrusted` to be asked) | In `danger-full-access` mode nothing is escalated, so `never` and `on-request` behave the same, and `never` saves about 900 tokens. The container is the sandbox. |

| `[skills.bundled] enabled = false` | Codex's four built-in skills (imagegen, openai-docs, skill-creator, skill-installer) add a list of about 600 tokens to every prompt, and a local model cannot use them (added 2026-10-19.1) |

**The slim prompt (2026-10-19.1), measured on this machine** with `python3 ~/strata-rebuild-kit/tools/codex-slim.py measure` (it copies Codex out of the image, uses a copy of your Codex home and a stand-in server, and prints what the first request is made of; nothing is sent to Strata and nothing is changed):

| Part of the first request | Codex's own | Slim |
|---|---|---|
| Base instructions | 4,245 tokens | about 300 (`agent/slim-instructions.md`) |
| Built-in skills list | 633 | 0 |
| Permissions text | 0 | 126 |
| Workspace `AGENTS.md` | 1,090 | 1,090 (at that time; it is larger now, see the `AGENTS.md` note below) |
| Tools (`exec_command`, `request_user_input`, `write_stdin`, `web_search`) | 983 | 983 |
| **Total** | **7,128 (cold read 170 s at 42 tok/s)** | **2,533 (60 s)** |

Codex's base instructions describe tools this setup does not have (planning, long patch syntax, formatting rules). The slim ones say what a session here needs: `exec_command`, small `apply_patch` patches (run on their own, ending with `*** End Patch`, because a model that followed a patch with `echo patched` hid a failure behind exit code 0), the check, short answers. `agent/make-model-catalog.py` writes the slim text into the catalog by default (`--stock` for Codex's own), the shipped `agent/model-catalog.json` carries it, and the installer keeps it there, so `codex-update.py` does not bring the long text back. Not measured: whether the model behaves as well with the short prompt on real work. Check with a real task, then compare the edit failure rate before and after: `python3 ~/strata-rebuild-kit/tools/edit-failures.py` counts failed edits per edit attempt in the session files (4.0% of 177 attempts, 7 failed, before the change); `--since "2026-10-19 22:30"` (a date and time) separates new sessions. `python3 tools/codex-slim.py restore` undoes an `apply` on an install that was slimmed by hand.

**The two `AGENTS.md` files.** `~/agent/codex-home/AGENTS.md` (workspace rules: starting a project, the plan format, the Checks, working rules) is read in every session. `~/agent/codex-home/templates/project/AGENTS.md` is copied into every new project as that project's `AGENTS.md`; it must stay short and hold only what differs per project, chiefly the `Check command ...: TO_BE_FILLED_IN` line, which `codex check NAME` reads (it refuses the placeholder). A copy of the workspace rules in the template loads them twice (4,250 instead of 3,070 tokens, 101 s instead of 73 s cold) and removes the check line. `tests/test_slim_prompt.py` fails if the template stops being the short project file. The plan-writing guidance (a two-pass D-A-C plan review, polish and retro wording, the simplicity rule) lives in the launcher and is added to the first message of a session that is about to write a plan (`codex plan`, a new idea, a project without a PLAN.md), not to `AGENTS.md`, so task sessions and the loop do not carry it. `CODEX_DAC=0` and `CODEX_UX=0` switch parts off; `CODEX_EFFORT=low codex ...` lowers the reasoning level (the launcher sets `high` for interactive and plan sessions; `codex loop` keeps its own).

**Reasoning levels (Alt+. up, Alt+, down).** Verified in the real interface with the catalog installed: Alt+. goes low, medium, high; Alt+, goes back down to none (thinking off; Codex's status line shows it as "default"), and the next request carries the level. What each level does is read from Strata's v0.1.44.1 source, not run against your live server:

| Level in Codex | What Strata does |
|---|---|
| none | Thinking off (an empty thinking block): fastest, for simple steps |
| low (the default) | The template's own "keep your thinking brief" sentence |
| medium | A "think as much as the task needs" sentence |
| high | Its heaviest level (xhigh), **but your `reasoning_budget_tokens` of 2048 still caps the thinking** (about 4 minutes at 8.6 tok/s); a bigger budget is a Strata config change |

**The catch: by default every change of level makes Strata read the whole conversation again**, because it writes the level as the first sentence of the prompt (about 3 minutes for a small context, about 12 at 30K tokens). Strata has an opt-in setting for this, `"effort_position": "end"`: it keeps the prompt start fixed and puts a low or medium level in a short turn just before the answer, so a change costs nothing. Its documentation says how well the model follows the level in that unusual place is not measured, so try it and compare:

```bash
python3 ~/strata-rebuild-kit/tools/strata-effort-position.py show     # what the config says now
python3 ~/strata-rebuild-kit/tools/strata-effort-position.py end      # backup first; then:  codex stop   and   codex
grep effort_position ~/github/strata-work/Strata/strata-swift-iq3_xxs.log | tail -2     # the server confirms it took effect
python3 ~/strata-rebuild-kit/tools/strata-effort-position.py start    # undo
```
The catalog carries the base instructions of the Codex it was made with. `codex-update.py` rebuilds it only when a new version's instructions differ (0.162.1 and 0.163.0 are byte-identical); by hand: `python3 ~/agent/bin/make-model-catalog.py --codex PATH-TO-CODEX --out ~/agent/codex-home/model-catalog.json`.

**Proceeding without waiting.** Why Codex stops and waits: its own instructions tell the model to keep a question pending and not continue until it is answered; a model that ends its turn with a plan or "shall I proceed?" simply waits for you; the project rules say one task per session by design (for the fresh-session loop); and Codex has a `request_user_input` tool that pops a question box (no setting removes it, so the workspace rules now say not to use it and never to ask whether to proceed). Three ways to keep it going:

| Way | How it works | Use it for |
|---|---|---|
| `codex --auto "idea"` or `codex NAME --auto` | Turns on Codex's own **goal** feature for this session and asks the model to create a goal "autopilot: finish every unticked task in PLAN.md". While a goal is active, Codex continues by itself after each turn until the model marks it complete or blocked. Tested in the real interface with a scripted model: after the goal was created, Codex sent two more continuation requests with nothing typed, then stopped on "Goal achieved". Esc interrupts; `/goal` shows the goal. You can also type `/goal` yourself (a slash command given as the start-up prompt reaches the model as plain text, so the launcher asks the model to create the goal instead) | Watching it work for a few tasks |
| `codex loop NAME` | Fresh session per task, the loop verifies each (Step 9b) | Long runs, leave it alone |
| Type "continue" | Plain step mode | Reviewing each task |

Limits of `--auto`: the goal tools add about 480 tokens to every request in that session only; **one session holds all the tasks**, so the context grows toward the 48K compaction (a 12-minute pause) after roughly 3 to 5 tasks, which is why `codex loop` is the right tool for long runs; and it needs the model to call `create_goal` and `update_goal` correctly. If it does not, the session simply behaves like step mode.

**What a new project contains.** Codex has no template of its own (its `/init` only writes an `AGENTS.md`); the skeleton is ours, in `~/agent/codex-home/templates/project/`, and the session copies it with one command (`cp -r --update=none`, so a `SPEC.md` you placed first is kept):

| File | Purpose |
|---|---|
| `SPEC.md` | Your design. The agent reads it in pieces (`sed -n 'A,Bp'`) and never rewrites it |
| `PLAN.md` | 5 to 8 tasks, one line each: `- [ ] T1: goal \| files: a, b \| spec: lines A-B \| check: command prints OK`. Each task names its SPEC lines, so a session reads only those (a long design never sits in the 64K context). The last task is always Acceptance |
| `AGENTS.md` | About 190 tokens, identical in every project so it stays in Strata's prompt cache: one task per session, the check command, "done only when the check prints OK", short outputs, commit, stop. A failing check three times means write down what was tried and stop |
| `README.md`, `progress.txt`, `src/`, `tests/`, `.gitignore` | Run instructions (written by the last task), one line per finished task, and `artifacts/` (screenshots) kept out of git |

The rules every session reads are `~/agent/codex-home/AGENTS.md` (about 440 tokens): a project is new if you were started in `/work` or its folder has no `PLAN.md`; if `SPEC.md` has content, ask no questions. Tested with the real interface and a scripted model (new project; a project you seeded with your own `SPEC.md`, which stayed byte-for-byte intact).

**Real-browser checks (why a passing headless check proved nothing).** The Pac-Man project's `check.js` runs the game against a *fake* browser and said OK while the real page was broken. On the same pages, three animated games that the fake-browser check passed (a canvas hidden by CSS, a key handler on an element that cannot take focus, a script asking for the wrong element id) all fail in a real browser. `browser-check PAGE [--keys ...] [--click S] [--expect-text T] [--probe JS --probe-change] [--expect-motion]` opens the page in headless Chromium inside the container and prints about ten short lines ending `OK` or `FAIL: ...`: it loads, no script errors, no missing files, the canvas is visible and drawn on, text and elements exist, and pressing the keys changes the screen. It saves `artifacts/screenshot.png` and `after-input.png` for a human to look at (the model is text-only and cannot). It lives in `~/agent/codex-home/tools/` and is editable without rebuilding the image. It cannot judge whether a game is fun or the movement feels right: that is still your playtest. CLI programs are checked by running them with real input.

Safety model, unchanged: `danger-full-access` because bubblewrap cannot run inside Docker; container limits 4 GiB, 2 CPUs, 512 processes, all capabilities dropped, `no-new-privileges`; **host network**, so commands the agent runs can reach any local service and the LAN (section 10). One session at a time, because Strata has one conversation slot. Interactive sessions keep the host network; **unattended runs use `codex loop` (Step 9b), whose container has no network at all.**

### Step 9b. `codex loop`: unattended runs (replaces `agent-ralph.sh`)

The idea is the one in the earlier harness research: many short sessions with fresh context, files and git as the memory. A fresh session starts in about 15 s because Strata caches the shared prompt; one long session would need a compaction (about 12 minutes). The prompt is identical every time, `AGENTS.md` never changes, and the state lives in `PLAN.md`, `progress.txt` and git. What is new is that **the loop never takes the agent's word for anything**.

**Why the file rule is strict by default (tested, not assumed).** A version of the loop that let a session change any file outside `tests/`, `test_*`, `PLAN.md`, `SPEC.md` and hidden files was tried on this machine and recommended for speed. Run against three scripted tampering sessions it marked a **wrong** implementation (`add()` returning 0) as *verified* every time: the agent rewrote the check script to `echo OK`; it gutted a test kept at `src/test_game.py` (not under `tests/`); and it appended "ignore all earlier rules" to the project's `AGENTS.md`, which every later session reads. The strict rule blocks all three (they are tests in `tests/`). The real problem behind that change was a planner that forgot to list `index.html` or a helper script, so the fix goes there: the page a `browser-check` opens is allowed automatically, `--allow-new-files` handles new scripts and assets, the workspace rules tell the planner to list the script AND the page, and a session that touched something unlisted is told so in `progress.txt` for the next attempt.

**Use it** (the first time, on a small project of 3 or 4 tasks):

```bash
codex gate-test                         # once, and after any Docker change: the agent's container reaches Strata and nothing else
codex NAME --spec ~/SPEC.md             # an interactive session writes PLAN.md from your design; read it, fix it
codex loop NAME --dry-run               # shows the plan, the lint result and the exact commands; runs nothing
codex loop NAME --sessions 2 --minutes 15      # a first, short trial
codex loop NAME                         # then the real run (12 sessions of up to 45 minutes by default)
codex check NAME                        # afterwards: run the project's check yourself, and look at artifacts/screenshot.png
python3 ~/agent/bin/codex-loop.py --lessons candidates                    # what the loop recorded, grouped and counted (--arg raw: every record)
python3 ~/agent/bin/codex-loop.py --lessons add --arg "- [web] text"      # promote one (refused if unsafe, duplicate or over the limits)
python3 ~/agent/bin/codex-loop.py --lessons list                          # the file, and the room per project type
python3 ~/agent/bin/codex-loop.py --lessons remove --arg "some of its text"
```
Stop it with a file named `STOP` in the project folder, or Ctrl+C. The report is `~/agent/logs/loop-NAME-DATE/loop-report.md`; the session logs and anything the loop reverted are in the same folder. Plans can come from the interactive session (the workspace rules tell it how) or from a cloud planner (section 9 of the earlier research: the plan format is the same).

**The plan format.** One line per task: ``- [ ] T1: goal | files: a, b | spec: lines A-B | check: `command` prints OK``. Every task needs `files:` and a runnable check. Keep a task to one or two files and about 4K tokens of output (about 7 minutes); the check should print a short, unambiguous result.

**What the loop guarantees, and how each was tested** (16 unit tests in `tests/`, run with `python3 -m unittest discover -s ~/strata-rebuild-kit/tests`; a scripted agent stands in for the model):

| Risk | What the loop does | Tested |
|---|---|---|
| The agent says "done" falsely, or ticks its own task | The loop runs the check itself, in a new container with no network, and ticks and commits only on a pass. The plan is restored after every session | yes |
| The agent weakens a test or edits the plan to make a check pass | A session may change only the files its task lists. Everything else is reverted (the agent's version is kept in the log folder), and `PLAN.md` is restored | yes |
| A check that cannot fail (`echo OK`, `true`, no test runner) | Refused before any session. A check that already passes before the work also stops the loop (`--accept-prepassed` overrides) | yes |
| A failed attempt leaves a mess | The attempt is saved on `refs/loop/Tn-k`, the tree is reset, a note goes into `progress.txt` for the next session; three failures on one task stop the loop | yes |
| A hung session | The container is killed at `--minutes`; two sessions in a row with no valid change stop the loop | yes |
| The agent commits on its own | Its commits are folded into the loop's one commit per task | yes |
| Your uncommitted work | The loop refuses to start on a dirty tree, because it resets the tree after a failure | yes |
| The agent reaches the LAN, Sunshine or the internet | `--network none`. The only way out is a Unix socket (mode 600) to the title filter, turned back into `127.0.0.1:8086` inside by `unix-relay.py`. `codex gate-test` probes Sunshine, SSH, Strata directly, the internet and DNS (all must be blocked) and Strata through the relay (must answer) | probe and relay chain yes; Docker flags not run here |
| The agent changes its own rules for the next session | Every session gets a fresh private copy of the Codex home | by design |
| The agent plants or changes a lesson, or a rule for the next session | Lessons are injected into a private copy that is discarded after the session; the lessons folder and master rules are never mounted; `LESSONS.md` or `AGENTS.md` written into the project is reverted | yes (attacks and a mutation check) |
| A bad session teaches the next ones | The loop records candidates only; nothing is injected until you add it, and candidate text is reduced to one line of printable ASCII | yes |

Also verified (and again on 0.163.0 by the probe suite): the real Codex 0.162.1 binary, given the loop's exact container command and prompt (minus Docker), worked through relay, socket and filter to a scripted model that read `PLAN.md`, wrote files and ran the check; the loop then verified and committed (4 model requests per session).

**Lessons: what the loop learns, and who decides.** `~/agent/lessons/LESSONS.md` is a global list of short rules from earlier runs, one line each:
`- [tag] when this happens, do that.` Tag `all` applies to every project; otherwise a tag must match the project, which the loop works out from the file types in
`PLAN.md` (`.html .htm .css .svg` or a `browser-check` give `web`; `.js .mjs .ts` give `js`; also `python`, `shell`, `c`, `go`, `rust`).

| What | How it works |
|---|---|
| Delivery | The loop appends the matching lines (at most 10 lines and 1,400 characters, in file order) to the `AGENTS.md` of the session's private Codex home. The master rules and the lessons folder are not mounted into any container and each session gets a fresh copy, so an agent cannot read, edit or carry over a lesson. The text is read once at the start of a run, so it is the same bytes in every session |
| Room | The file may hold 40 lessons but a project gets 10 lines. Lessons that do not fit are never cut silently: the run, `--dry-run`, `loop-report.md` and `--lessons list` say how many were left out and which, and `list` shows the room for each kind of project. Lessons higher in the file are kept first, so order is your priority |
| Cost | The lessons sit in the same message as the project's path and rules, after the shared prompt prefix, so they are re-read once per new project and stay cached within a project. Worst case about 400 tokens, roughly 10 seconds at 42 tok/s (an estimate, not measured) |
| Capture | The loop, not the agent, records a candidate in `lessons/candidates.jsonl` when a task is fixed on a retry, is blocked, or passes with reverted files. It holds the check output tail, file names, attempt count and the id of the lessons in force, cut to one line of printable ASCII. Nothing the agent wrote is copied, and nothing is ever injected without you |
| Counting | `--lessons candidates` groups the same problem (same kind, same project tags, same first failure with numbers ignored), most frequent first, marked "repeats: worth a lesson" from 2 up. At the end of a run the loop says when a problem it just recorded has now happened 2 or more times. `--arg raw` shows every record |
| Promotion | You read the candidates and add a line with `--lessons add`. It refuses lines that are not plain ASCII, are longer than 220 characters, duplicate one, would make a 41st, or contain a web address, a network or risky shell command, wording that tries to override other instructions, a credential or a path outside the project (a false alarm can be written into the file by hand). It warns when the new lesson will not fit |
| Removing | `--lessons remove --arg "some of its text"`: positions move when the file changes, text does not. A number, too little text, or text matching two lessons is refused |
| Backups | Every add or remove first copies the file to `~/agent/lessons/backups/` (the newest 20 are kept). To undo a change, copy a backup over `LESSONS.md` |

Habits that keep it useful: first ask whether the plan lint, the template or the planner's rules can enforce it, because a rule you can enforce beats a lesson. Promote only a failure
that happened twice (the counting does this for you), or once if it was expensive. Keep the list short and each line a trigger plus an action; a lesson that only restates something the
model can look up, or a one-off event, does not belong. Put `# why: ... remove when: ...` on a comment line above each lesson (comments are ignored by the loop and kept by `add` and
`remove`). Judge a lesson by running the same small project with and without it and comparing attempts, reverts and minutes in `loop-report.md`. Edit between runs, not during one.

**Not tested, so treat the first runs as trials:** the Docker flags on your machine (`codex gate-test` is the test); whether Codex starts and runs cleanly with no network (it may try to reach OpenAI for update checks and fail quietly); `browser-check` inside the no-network container; the real model's behaviour (does it write a good plan, does it stay inside its files); and timings. A session costs about 15 s to start, 5 to 15 s per turn of reading, and generates at about 9 tok/s, so a 4K-token task is roughly 10 to 25 minutes including the check; 8 tasks is an evening, plus any retries.
the real Codex and model with lessons present; whether lessons improve results; codex lessons is not wired into the codex command; interactive codex NAME sessions do not get lessons.

**What it cannot do:** judge whether a game is fun or the controls feel right (your playtest), or catch a plan whose checks are all weak but not obviously vacuous (read `PLAN.md` before the first run; `--dry-run` lists every check). A task that fails three times stops the loop rather than being skipped, because later tasks usually depend on it.

### Step 10. Security checklist (before first use)

- Sunshine (tablet streaming): `curl -sk -o /dev/null -w '%{http_code}\n' https://127.0.0.1:47990/api/config` must print `401` (verified 16 October). Allow ports 47984 to 47990 and 48010 only from the tablet's address; there is no host firewall (`ufw` is not installed).
- Retire the NextChat container (host network, root, 15-month-old image, API key variables in its environment); use Strata's own page.
- The engine's base image is pinned by digest in Step 4; Codex is updated deliberately with `codex-update.py` (Section 9): it tests the new binary before the image changes.
- Keep `STRATA_ENGINE_STALL_S=900` in the start script.

---

## 3. The settled configuration (`strata-swift-iq3_xxs.json`; full file in Appendix A)

| Setting | Value | Why |
|---|---|---|
| `--prefill` | `2048` | Required. `auto` hung. `4096` and `8192` shrink the expert cache (24,407 and 23,183 slots) and make the batched path unsafe. |
| `--short-read` | `64` | Batched prompt reading (35 to 54 tok/s). Safe only with all experts cached. Fallback if a start shows a smaller cache: `4096` (about 14 tok/s, cannot hang). |
| `--max-context` | `65536` | KV costs about 13 layers x 1,056 B per token at int8 (0.84 GiB at 64K). Raised from 32768 on 16 October. |
| `--spec 4`, `--spec-min-p 0.5` | stock | `--spec 6` was 9% slower; `--spec-min-p` 0.3 to 0.7 is within noise. |
| `--pool-workers 8`, `--kv int8`, `--stream-experts`, `--vram-reserve-mib 2048`, `--no-prefill-borrow`, `--expert-cache auto` | stock | 4 and 12 workers gave nothing. |
| `reasoning_budget_tokens` | `2048` | Caps thinking so it cannot fill the context. |
| `reasoning_effort` | `"low"` | Set for the agent; the log prints "thinking level: low". |
| `tool_call_recovery` | `true` | Repairs mis-formatted tool calls. |
| `fit_max_tokens` | `true` | Clamps the output limit to what fits. |
| `engine_silence_s` | `900` | Stops long prompt reads being ended as "frozen". |
| `slot_save_path` | `/work/sessions` | Saved chats (root-owned files; delete with `sudo rm`). |
| env `UR_L0_ENABLE_RELAXED_ALLOCATION_LIMITS=1` | | Lifts the GPU's 4 GiB single-allocation limit (the cache is one 33+ GiB allocation). |
| env `STRATA_WINDOW_SYNC=1` | | Prevents a decode freeze after 3 to 5 requests on xe. |
| env `STRATA_UMA_HEADROOM_GIB=12` | | RAM left for the system. |
| env `STRATA_MIRROR_MIB=12288` | | No effect while every expert is resident (1024 gave the same memory). Setup writes 1024; either is fine. |
| env `STRATA_SYCL_BIN=build-sycl/strata` | | The JIT engine. |
| env **`UR_L0_USE_IMMEDIATE_COMMANDLISTS=1`** | | **Chosen 16 October for temperature**: package temp p95 about 92 to 79 C, throttling (PL4) about 70% to 8% of loaded time, prefill +4 to 5%, decode unchanged. Canary 4/4, no errors in 9 batches. Intel's docs say Core Ultra 100-series iGPUs do not get immediate command lists by default. To undo: `strata-tune.py unset-env UR_L0_USE_IMMEDIATE_COMMANDLISTS` and restart. |

Shell exports (in `start-swift.sh`): `UR_L0_ENABLE_RELAXED_ALLOCATION_LIMITS=1`, `STRATA_WINDOW_SYNC=1`, `STRATA_ENGINE_STALL_S=900` (the quick watchdog; the default 90 s ends long prompt reads).

---

## 4. Daily routine

**Start:** `~/start-swift.sh`, wait for `ready:`.

**Stop:** press Ctrl+C in the server window (stops the server, which stops the engine container). Then check `docker ps` lists nothing from `strata-sycl-dev` and `grep MemAvailable /proc/meminfo` shows about 54 GiB or more. Save a chat you want to keep first: `~/chat-save.sh NAME`; after the next start `~/chat-restore.sh NAME`, continue in the same browser tab.

**Scripted stop and start** (used by the tuning tools): `python3 ~/strata-rebuild-kit/tools/strata-orchestrator.py enginetest` stops Strata (SIGINT to the server, escalating), frees memory and starts it again, reporting the slot count. About 5 minutes; verified.

**Fallback model:** `./run-iq2_xs.sh` with the three exports of the start script (verified earlier).

**Chat habits:** Thinking Off for coding and long chats, and keep it unchanged within a chat (a follow-up then re-reads only the new message). Ask for patches or one function. Do not edit earlier messages. Other front ends may send background requests that erase the single conversation slot. Context is 65,536 tokens, but a 48K prompt takes about 20 minutes to read.

---

## 5. Tools (in the kit's `tools/`)

| Tool | Use |
|---|---|
| `codex-update.py` | `check [--to VER]` (read-only), `apply`, `rollback`: move the Codex in the image to a newer stable release and back, after a 13-probe compatibility suite (`codex_compat.py`, also runnable alone on any binary) (section 9) |
| `strata-update.py` | `check [--to TAG]` (read-only), `apply --to TAG`, `rollback`: move Strata to a newer release and back, with the kit's patches, a backup and automatic undo (section 9); the undo puts the files the patches touch back byte for byte, so a hand edit inside one of them survives a stopped update |
| `strata-models.py` | `list`, `remove TAG [--yes]` (preview first), `show`, `set`, `arg`/`unarg`: see what each model owns, remove one safely, change settings with a backup (Section 9) |
| `strata-effort-position.py` | `show`, `end`, `start`: makes changing the reasoning level free of re-reading (or undoes it); backup first, restart Strata after |
| `strata-orchestrator.py` | `plan`, `run` (unattended tuning, about 5 to 6 h), `confirm --combo L1 --pairs 2` (ABBA confirmation, about 2 h), `enginetest`, `canarytest`, `report DIR`, `revert DIR`. It never adopts a result. Run it inside `tmux`; it asks for sudo once (drop_caches and three sysfs settings; model-written canary code runs only in `docker run --network none --read-only`). |
| `strata-bench2.py` | `check`, `run LABEL`, `compare "A*" "B*"`, `show`. Noise-aware: median of repeats, GPU clock, throttle, temperature, memory per batch. |
| `strata-recon.sh` | Read-only snapshot of system, GPU, Strata, sockets, Docker flags, agent setup and session statistics; `sample N` logs GPU clock and throttle reasons; `load` generates decode load. |
| `strata-tune.py` | `show`, `set-arg`, `set-env`, `set-key`, `restore` on the Strata config (first change saves `.tune-orig`). Copy it to `~/strata-tools/`. |
| `capture-machine-files.sh` | Snapshots what only this machine has (section 11). |

Rules for measuring on this machine: the package reaches 90 to 100 C under load and decode varies 4 to 8% between restarts, so only compare ABBA batches with a restart before each; never trust a single run. The code-edit ("copy-heavy") benchmark is chaotic (10 to 11.5 tok/s, bimodal by restart) because greedy output differs slightly between restarts. Keep Sunshine idle and the browser dashboard closed during a run.

---

## 6. What testing settled (do not repeat)

Full screening of 13 settings and two ABBA confirmations, 16 October. Effects smaller than the baseline's restart noise (4 to 8% on decode) are not real.

| Tried | Result |
|---|---|
| **`UR_L0_USE_IMMEDIATE_COMMANDLISTS=1`** | Kept (section 3): cooler, steadier, +4 to 5% prefill, decode flat |
| `intel_pstate/no_turbo=1` | Alone it cooled about 4 C in screening and added nothing once the line above was on; it costs about 3% decode (slower host thread). Not used. |
| EPP and platform profile `performance` | Nothing |
| Qwen sampling (t1.0, top_p 0.95, top_k 20) | 16% slower decode. Use it only if quality needs it; `STRATA_SPEC_COUPLED=1` is untested. |
| `--spec 6` | About 9% slower |
| `--lookup-chain 3` / `6`, `--mtp-q4 all` | Losses or noise |
| `--prefill 4096` / `8192` | Expert cache no longer all-resident: unsafe |
| `STRATA_MIRROR_MIB=1024` | No memory change |
| `--pool-workers 4` / `12` | Nothing |
| `--spec 3`, `--spec-min-p 0.6`, clock pinned at 2200, `taskset` | Nothing (earlier) |
| Clock capped at 1500 MHz | 28% slower: below about 2100 MHz decode follows the clock |

**The ceiling:** at 2100 to 2200 MHz decode is about 8.6 to 8.7 tok/s whatever the clock. There is no cheap speed gain left. The remaining lever is cooling: the BIOS fan profile is still unchecked.

---

## 7. Measured numbers (v0.1.44.1, L1 on)

| Measure | Value |
|---|---|
| Decode, 300 tokens | about 8.6 to 8.7 tok/s (about 35 s) |
| Copy-heavy output (code edit) | 10 to 11.5 tok/s (draft acceptance helps: +15 to 35% over plain decode) |
| Prefill, real code | about 42 tok/s (39 to 40 without L1); 8.5K-token agent prompt cold about 3.3 min |
| First request after a start | 17 to 18 s |
| GPU clock under load | 2,200 MHz with L1 (2,050 to 2,150 without); package temp p95 about 79 C (91 to 94 without) |
| Memory with the model loaded | MemAvailable about 6.2 to 7.7 GiB, swap about 2 to 2.5 GiB, all 24,576 experts (39.97 GiB) in the GPU cache |
| Codex prompt, interactive | about 2,500 tokens with the kit's slim base instructions and skills off (measured 7,128 before; about 12,300 with Codex's defaults) |
| Power | about 31 W package under load (limits 200/64/120 W are far above it) |
| Engine start | about 32 s to ready after the model files are cached; about 2 minutes from a cold system |

---

## 8. Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `REFUSED: N experts are neither in VRAM nor mirrored` | Memory was already taken. `docker ps` and stop leftovers; `sync; echo 2 \| sudo tee /proc/sys/vm/drop_caches`; want `MemAvailable` about 50 GiB; start with `~/start-swift.sh`. Cause: the xe TTM page pool (capped at 1 GiB by Step 1). Reported upstream as #2282 and #2412. |
| `needs --spec T and --prefill CHUNK` | `--prefill` missing from the config: restore a backup or add `--prefill 2048`. |
| `no progress for 60 s ... reading the prompt (batched)` | Check the last `expert cache auto` line: not 24,576 slots means the cache is not all-resident. Set `--short-read 4096` and restart. |
| "the engine said nothing for 90 s ... (frozen)" | False alarm on a slow read. Keep `STRATA_ENGINE_STALL_S=900` exported and `engine_silence_s` 900. |
| The reply reached max tokens while still thinking | Keep `reasoning_budget_tokens` 2048, or use Thinking Off or Low. |
| Follow-ups are slow | Thinking changed mid-chat or an earlier message was edited. |
| Save fails ("cannot read the free disk space") | The engine cannot see the path: check `ls /work/sessions` and `slot_save_path`; recreate the `/work` symlink. |
| `400 invalid request` from curl | JSON booleans are lowercase. |
| Engine died and restarted (exit 137 or 139) | Wait 1.5 to 2 minutes, then check the `expert cache auto` and `REFUSED` lines. 137 can be the kernel killing the container under heavy desktop memory use (`sudo dmesg \| grep -i 'killed process'`). |
| Version label shows an old number after an update | `touch sycl/CMakeLists.txt` and rebuild (Step 4). |
| **Strata dies at start with `install_xe_reader.<locals>.gpu_reader() takes from 0 to 2 positional arguments`** | A patch changed `gpu_reader`'s signature. The launcher's override takes `(index, amd)`. Revert it (`git apply -R`) and use the kit's `xe-monitor-v2.patch`, which does not touch `telemetry.py`. |
| The orchestrator says "Strata would not stop" with no container found | The server (not the container) is what answers on 8085. The current version stops the server first; check `docker ps` and `ss -ltnp \| grep 8085`. |
| Dashboard shows dashes for power | The kernel keeps the RAPL counter root-only. Install the root service in Step 8. |
| `docker pull` looks stuck at "Pulling fs layer" | Slow, not stuck: about 6 MiB/s here, no byte counters at first. Use the retry loop in Step 4; resets on GitHub's IPv6 addresses just restart a layer. |
| Alt+. / Alt+, say "Reasoning shortcuts are unavailable for strata" | The model catalog is not installed: `kit.sh update` (it installs `model-catalog.json` and the `model_catalog_json` setting) |
| Pressing Alt+. makes Codex pause for minutes | Strata is re-reading the conversation because the level sits at the start of its prompt: `strata-effort-position.py end` (see "Reasoning levels") |
| Codex waits for me after the plan and after each task | `codex --auto` (or `/goal`), or `codex loop NAME`; see "Proceeding without waiting" |
| The Codex screen shows `<tool_call><function=apply_patch>...` as text and the turn stops (you have to type `continue`) | The model wrote `apply_patch` as if it were a tool. It is a shell command run through `exec_command`; Strata does not read a call to an undeclared name as a call (its log says `did not read as a call of a declared tool ... apply_patch`). Since 2026-10-23.7 the title filter turns it into a real `exec_command` call (with the patch if it carried one, else one that prints how to call it), so the turn goes on. If it still stops: `codex status` should show the filter; restart `codex` so the launcher replaces an older filter; `~/agent/work/NO-APPLY-PATCH-RESCUE` turns the repair off. How often it happens: `grep -a "did not read as a call" <Strata log> | tail`; `python3 ~/strata-rebuild-kit/tools/edit-failures.py` counts the edits that failed. |
| Each prompt in the Codex screen is slow even for a short answer | The automatic session-title request is reaching Strata. Check `codex status` shows the title filter running; `~/agent/logs/strata-front.log`. |
| A game passed its check but is broken in the browser | The check faked the browser. Use `browser-check` (Step 9) and `codex check NAME`; look at `artifacts/screenshot.png`. |
| Interactive Codex shows `Error: this CLI has no complete local package` | The background server is on. `daemon_auto_start = false` under `[features]` in `~/agent/codex-home/config.toml` (the installer sets it), or pass `--no-daemon`. |
| `codex stop` says it would not stop | The server was started by hand from a bash background job (it ignores Ctrl+C). Close its window, or `kill -TERM` the process on port 8085; `docker ps` for the engine container. |
| A new project came out nested (`/work/NAME/project/...`) | The session reused a folder name that already existed. Tell it to choose another name; the workspace rules say so. |
| Codex misbehaves after a Codex update (shortcuts, paste, tools, slow prompts) | `codex-update.py rollback` (section 9), then `codex_compat.py --codex PATH` shows which probe fails |
| Decode or answers are worse after a Strata update | `strata-update.py rollback` (section 9); if only speed: try `env.STRATA_Q6_PLANAR=0`; if the writing changed: the `sampling` block (section 9, 0.1.45 table) |
| Disk is full / I want an old model gone | `strata-models.py list`, then `remove TAG` (preview, then `--yes`); Section 9 |
| Desktop sluggish while the model loads | About 40 GiB is loading into shared memory; it clears when `ready:` appears. Close heavy apps first. |
| A lesson does not reach the agent | `--lessons list` (a line that starts with `- ` but does not match `- [tag] text`, or is over 220 characters, is ignored and counted there); the run prints `Lessons in force: N line(s) for tags: ...`; a tag must be `all` or match the project's file types; the run and `list` say if it was left out because 10 lines or 1,400 characters were used |
| The run says lessons "did NOT fit" | Too many lessons match this kind of project. Lessons higher in the file are kept first: reorder, shorten or remove (`--lessons list` shows the room for each project type) |
| `--lessons add` says "not added" | Read the reason it prints: not plain ASCII or wrong shape (`- [tag] text`, tag in lower case letters, digits, comma, dash, text up to 220 characters), a duplicate, the 40-line limit, or text that looks unsafe (web address, network or risky shell command, override wording, credential, path outside the project). For a false alarm, write the line into the file by hand |
| `--lessons remove` says "not a number" or "contain that text" | Use some of the lesson's own text (4 characters or more); if two lessons match, use more of it |
| A lesson I changed or removed is wrong | Copy a file from `~/agent/lessons/backups/` over `~/agent/lessons/LESSONS.md` |
| Candidates are empty after a run | A clean first-time pass records nothing. Only a retry, a block or reverted files do |

---

## 9. Maintenance

**Updating Strata (not pinned to one version).** Do not use `./update.sh` or `git pull`: this checkout carries three local patches, upstream rewrote its history once (2026-10-06, #1276), and a failed update should leave you where you were. `strata-update.py` does it as one transaction (tested against the real Strata repository, v0.1.44.1 to v0.1.45 and back, and offline with a synthetic one for every failure path):

```bash
U=~/strata-rebuild-kit/tools/strata-update.py
python3 $U check                      # READ-ONLY: fetches the tags, and tests your kit patches against the newest release in a scratch copy
python3 $U check --to v0.1.45         # or a specific release; it lists the SYCL, Intel, server and config commits and the behaviour changes known for your configs
codex stop
python3 $U apply --to v0.1.45         # asks you to type the version; then: back up, switch, re-apply the patches, rebuild the engine, kernel tests
codex                                 # first start takes a few minutes; then the acceptance checks below
python3 $U rollback                   # if it is worse: back to the previous version, patches, engine and (changed) configs, byte for byte
```
`apply` refuses if Strata or an agent session is running, if your checkout has changes the kit's patches do not explain (`--force` sets them aside after saving the diff), or if the version you type is wrong; if a patch no longer applies, the build fails or the engine reports the wrong version, it **undoes everything** and leaves the backup (`~/strata-backups/update-FROM-to-TO-DATE/`: configs, run scripts, the full local diff, the old engine binary). Your configs are changed only for a known behaviour change, only with your say-so (below), and the originals are in the backup. `kit.sh status` shows which Strata version you are on and the newest release known locally.

**Acceptance after an update** (compare with section 7; decode varies 4 to 8% between restarts, so only trust an ABBA comparison):
- the log shows the new engine (`grep "session is up" strata-swift-iq3_xxs.log | tail -1`), `expert cache auto ... 24576 slots`, and `grep -c "REFUSED\|no progress for"` prints 0 (Step 7);
- `python3 ~/strata-rebuild-kit/tools/strata-bench2.py run after-update --runs 5 --prefill-runs 1`, then `compare` against your earlier label: decode about 8.6 tok/s, prefill about 42 tok/s, memory and throttling as before; `strata-orchestrator.py canarytest` 4 of 4;
- if anything is worse, `rollback`. If only decode is slower, see the Q6 note below before giving up.

**Strata 0.2.3 and Codex 0.164.0 (checked 23 October 2026; nothing here has been run on the machine, the kit's tools were run on a real checkout with a fake engine build).** Strata's 0.2 series: v0.2.0-beta (19 October), v0.2.1 to v0.2.1.4, v0.2.2, **v0.2.3 (23 October, 1,308 commits after 0.1.45, builds engine 0.1.48, `MIN_ENGINE` 0.1.46)**. What was checked: the three dashboard patches apply to every 0.2.x tag; the Meteor Lake patch does not (`sycl/setup_intel.py`), so it was remade as `mtl-igpu-local.0.2.patch` (applies to v0.2.2 and v0.2.3; the config setup generates for this GPU matches 0.1.45's for the test config used); Strata's own `test_xe_telemetry.py` passes on the patched 0.2.3 tree (11 tests, 1 skipped as root-only), including the test through the Intel launcher's reader that 0.2.x's new `reader.name()` call in `serve/telemetry.py` goes through; every config key and engine flag of the settled config still exists in 0.2.3. Read in the source: the new Intel prompt defaults (XMX on Arc A-series, int8 experts on Battlemage) do not switch on for this iGPU; `fit_max_tokens` now defaults to true (the settled config sets it); the default worker count of a hybrid CPU can change (the settled config pins 8). Release-note items are in `strata-update.py`'s COMPAT table and `check` prints them. `strata-update.py` was changed for this series: it reads the engine version the target builds from its `CMakeLists.txt` (the tag says 0.2.3, the build says 0.1.48, and the old check would have undone a good update), judges the build by `BUILD EXIT 0` and `errors: 0` and not its exit status, and picks the patch variant that fits. On a real v0.1.45 checkout (the four kit patches, the settled config, a fake engine): `check --to v0.2.3` says ready and changes nothing; `apply --to v0.2.3` succeeds, keeps `--prefill 2048`, `--short-read 64`, the sampling block and `fit_max_tokens`; `rollback` puts back the identical state. **Not done:** the real Docker build, the quick kernel tests, `setup.sh --check`, any speed, memory or answer-quality measurement on this machine, reading most of the 1,308 commits; so treat 0.2.3 as untested here. Do not use Strata's own `strata launch` / `strata update` on this machine (0.2.3 ships Intel Arc as a downloadable engine package): they would replace the engine you build and ignore the patches. After `apply`, the acceptance steps below apply unchanged; compare decode and prompt speed with `strata-bench2.py` (not with the monitor: 0.2.2 changed what the monitor's tokens-per-second counts).

**Strata 0.1.45 (released 2026-10-16), checked on 17 October, not yet run on this machine.** From the source: 264 commits; all three kit patches apply cleanly to the v0.1.45 tag; `device.hpp`, `setup_intel.py`, `xe_telemetry.py`, `server_intel.py` and `rapl_power.py` are unchanged upstream, so **the Meteor Lake fix is still needed** (the memory-detection bug is not fixed upstream); the minimum engine version becomes 0.1.45, so the engine must be rebuilt (`apply` does it); I found no pack, profile or draft-head format change, so nothing needs re-packing or re-downloading (setup's own checks still run). What matters for this machine:

| Change | Effect here | What the tool does |
|---|---|---|
| **A request with no sampling settings now gets the model card's sampling** (temperature 1.0, top_p 0.95 when thinking; 0.7 and presence_penalty 1.5 when not) instead of greedy decoding. Codex sends none, and your config has no `sampling` block | Different output, and sampling costs decode speed (about 16% was measured here before) | `apply` offers to add `"sampling": {"temperature": 0, "presence_penalty": 0}`, which keeps decoding as it was (the engine is greedy whenever temperature is 0, and the presence penalty is the one card value that would still leak in). Remove the block later to try the card's sampling deliberately |
| `STRATA_Q6_PLANAR` on by default: the Q6_K output head and the draft head are converted at load (bitwise equal output, faster Q6_K kernels; no VRAM change) | Possibly faster decode; **not measured on Meteor Lake** (an Arc A750 needed a launch fix for the planar kernel) | Nothing. If decode is slower: `python3 ~/strata-rebuild-kit/tools/strata-models.py set swift-iq3_xxs env.STRATA_Q6_PLANAR=0`, restart, compare |
| `max_tokens` now counts the thinking; without a thinking budget the thinking is cut early to leave a quarter of the cap for the answer (`answer_room`) | None expected: your configs set `reasoning_budget_tokens` | Nothing |

**Updating Codex (not pinned to one version).** Codex changes about weekly and the kit depends on details of how it behaves, so `codex-update.py` runs the real new binary through a probe suite *before* anything changes (it never installs it on the host). Newest stable release is chosen for you (alphas and betas never are):

```bash
C=~/strata-rebuild-kit/tools/codex-update.py
python3 $C check                      # READ-ONLY: release notes that may matter, a download, then the probes on the installed Codex and on the new one
python3 $C check --to 0.164.0         # or a specific stable version
python3 $C apply                      # same checks; if all pass: type the version; a new image layer (the old image is kept), catalog rebuilt only if needed
python3 $C rollback                   # instant: the previous image (and catalog) is tagged and kept
```
The probes use a copy of your real Codex home and a stand-in for Strata, and check: your config and model catalog are accepted; the seven feature flags the installer switches off still exist; the same function tools and a prompt of a similar size (about 2,500 tokens with the slim base instructions; it warns above 9,500); your workspace and project `AGENTS.md` arrive; a tool call runs and returns; the exact `codex loop` command line; the full-screen interface starts without the background server; the extra session-title request is still recognised by the title filter; Alt+. and Alt+, change the level and the request carries it; a small and a large paste arrive whole and wait for Enter; `--enable goals` continues by itself (`codex --auto`); and, with your Strata checkout, **Strata's own Responses parser accepts every request that Codex sends**. Any failure blocks `apply` (`--force` overrides). `apply` also refuses while a session or check container runs, restores the old image if the build fails or reports the wrong version or loses the browser layer, and tests all of that offline with a fake Docker.

**Codex 0.163.0 (released 2026-10-15), checked on 17 October, not yet applied on this machine.** The probe suite passes 13 of 13 on every combination of Codex 0.162.1 / 0.163.0 and Strata 0.1.44.1 / 0.1.45 (the Strata side being its own request parser, run offline). From the release notes and the binary: your seven feature flags are unchanged and the eight new features are all "under development", off by default; the base instructions are byte-identical (no catalog change); the prompt is the same size; `daemon_auto_start = false` still prevents the background-server failure. New in the interface and harmless here: a leader key in `/keymap` (Alt+. and Alt+, still work), steering by typing while Codex answers, and `/config`. Worth knowing: a new `async_compaction` feature (under development, off) is aimed at the compaction pause and may be worth testing once it is stable.

**Codex 0.164.0 (stable), checked on 23 October, not yet applied on this machine.** The probe suite (`codex_compat.py`, scratch Codex home made by the kit installer, so not your real one) passes on Codex 0.163.0 and 0.164.0 against the request parsers of Strata 0.1.45 and 0.2.3: 13 of 13 for 0.163.0, 12 ok and 1 warning for 0.164.0 in both. The warning: the `worktrees` feature is now `removed`, so the installer's `worktrees = false` and the launcher's `--disable worktrees` do nothing (0.164.0 still accepts both: `codex --disable worktrees --version` runs). The model catalog is identical (nothing to regenerate); the tools and the first request (about 3,515 tokens) are unchanged. `incremental_tools` became stable and on by default; the probes show the same tools, and nothing else about it was assessed. The `shortcuts` probe used to expect level `medium` after Alt+. Alt+. Alt+, whatever the level started at, which is wrong when your config starts at `none` (it ends at `low`); it now reads the starting level from a first message and judges the keys from there, says what it saw when it fails, and retries a lost key up to 3 times (a retried pass is a warning) (2026-10-23.6). Apply it with `python3 $C apply`; `rollback` is instant.

Two things from checking this that apply to the old version too: the installer now sets `check_for_update_on_startup = false` (a pinned binary in a container should not phone home or nag), and **Strata already has its own opt-in for the session-title request** (`"codex_thread_titles": true` in the model's config, since 0.1.43/0.1.44, #923). The kit's title filter does the same job in front of Strata, works with any Strata version, and is needed for the loop's socket anyway; it now recognises the request by Codex's own turn metadata (`thread_source: thread_title`, the signal Strata uses) with the old text match as a fallback. Enabling Strata's opt-in as well is harmless: `python3 ~/strata-rebuild-kit/tools/strata-models.py set swift-iq3_xxs codex_thread_titles=true`, then restart Strata.

**Updating the kit's own pieces (the `codex` command, its rules, template and tools):** extract the newer kit, then `bash ~/strata-rebuild-kit/kit.sh status` and `kit.sh update`. Your edited rules or template are replaced only after a `.bak` copy is saved.

**Removing a model, switching models, changing a model's settings.** (Read from Strata v0.1.44.1's source and docs; the helper below is tested on a synthetic layout, not yet run on your real files, so always preview first.)

- **Strata has no command to remove a model.** The files stay until you delete them. It does have: `./setup.sh --setup ... --model SIZE [--family swift]` to add or switch, one start script per model (`run-TAG.sh`), and `./setup.sh` alone, which asks which installed model to start when there are several.
- **Every model is named by a tag**, the family prefix plus the size: `IQ2_XS` for the original Qwen, `swift-IQ3_XXS` for Swift. From it: the settings `strata-TAG.json` (lower case), the start script `run-TAG.sh`, the log, the pack `data/packs/TAG`, and the GGUF folder `MODELS-DIR/TAG`. **Your two models are probably in two places:** the first install used `--models-dir ~/github/strata-work/models`, Swift went to `~/github/strata-work/data/models`. `strata-models.py list` reads the real paths from each config, so you do not have to guess.
- **Shared, so never removed with one model:** the draft head `data/mtp/rt`, the expert profile `Strata/data/expert-profile-sycl.bin`, and any file another config also names (the Coder reuses the original's shard 2). Saved chats (`sessions/`) are tied to a model and settings; the helper leaves them alone.
- **Changing a setting: edit the model's config, do not re-run setup.** Re-running `./setup.sh --setup` for an installed model keeps top-level keys it does not write (`reasoning_budget_tokens`, `effort_position`, `fit_max_tokens`...) and extra `env` entries, but **replaces the engine options in `"args"` with its own choices** (`--prefill 2048`, `--short-read 64`, `--spec`, `--pool-workers`... would be lost) and keeps only one `.bak`, overwritten by the next run. If you do run it to add a model, restore the settled config afterwards (`config/strata-swift-iq3_xxs.json` in the kit, or the `.bak`).
- Settings are read when Strata starts: after any change, `codex stop` then `codex`.

```bash
T=~/strata-rebuild-kit/tools/strata-models.py
python3 $T list                                   # each model, where its files really are, their sizes, what is shared, free space
python3 $T remove iq2_xs                          # PREVIEW: exactly what would be deleted and how much it frees (deletes nothing)
python3 $T remove iq2_xs --yes                    # then really do it: asks you to type the tag; its settings are archived first
python3 $T show swift-iq3_xxs                     # a model's settings: top-level keys, env, engine options
python3 $T set swift-iq3_xxs reasoning_budget_tokens=4096 env.STRATA_MIRROR_MIB=12288
python3 $T arg swift-iq3_xxs --prefill 2048       # set or replace an engine option;  unarg ... --flag  removes one
```
What `remove` does: deletes the model's own GGUF folder (or only its shard files if they sit in a folder not named after it), its pack, start script and log; **archives its settings and start script** under `~/strata-backups/removed-models/` so it can be restored; refuses a running model; never touches shared files or anything outside your Strata data folders. Every edit to settings makes a timestamped backup and checks that the file still parses. To bring a removed model back: `./setup.sh --setup ... --model SIZE` downloads it again, then copy the archived settings over the new config.

**After a kernel update:** `cat /sys/module/ttm/parameters/page_pool_size` must print `262144`; if not, re-run the Step 1 commands.

**Disk:** each model 68 to 76 GB, saved chats about 230 MB plus 15 KB per token (a 48K-token chat is about 1 GB). `sync; echo 2 | sudo tee /proc/sys/vm/drop_caches` frees the TTM pool without a restart.

---

## 10. Policy, security and what it is for

**What may go to a cloud planner:** public material and personal material you are happy to share. Never work-confidential material: that stays local or nowhere.

**What this machine is for:** slow, private, unattended mechanical work (a project's task list, such as a game or a small application), planned and reviewed by a stronger cloud model; not interactive coding. The capability gate (6 to 8 real tasks, each with a goal, decisions, a verification command and a blast radius) has not been run yet.

**Security register (open items marked):**

| Item | State |
|---|---|
| Young repository, one maintainer, a release every few days | Run a known version and update deliberately: `strata-update.py check` tests the patches against any newer release before you touch anything; `apply` is reversible (section 9) |
| Build environment `llama-sycl-b70:stable` is one person's mutable tag | Pinned in Step 4 by the digest of 12 October (`sha256:b5799bf3...`); the dev image still runs `apt` at build time |
| Agent container: `danger-full-access` | Interactive `codex` sessions keep the host network, so the agent can reach every local service, including Sunshine, and the LAN (you are watching). **Unattended `codex loop` runs use `--network none` with a socket to Strata only; run `codex gate-test` to verify it on this machine (not yet run there)** |
| Unattended loop | `agent-ralph.sh` (retired, not in the kit) trusted the agent's own ticks. Replaced by `codex loop` (Step 9b): the loop runs every check itself and limits each session to its task's files. First real runs are trials |
| Sunshine on 0.0.0.0, no host firewall | Credentials verified; **open:** restrict ports to the tablet |
| NextChat container | **Open:** retire |
| Docker group membership | Root-equivalent; no passwordless sudo (verified) |
| Services that run as root | Use a root-owned copy in `/usr/local/sbin`, never a script in a home folder |

---

## 11. Verify and record (run once on the machine)

What the earlier chats and the web settled: the install commands (Steps 1, 4, 5), the base-image digest, the kernel test results and the Codex asset pattern are now in this file. What only the machine can confirm is below. Everything here reads or copies; nothing changes the system.

```bash
tar xzf "$(ls -t ~/Downloads/strata-rebuild-kit*.tar.gz | head -1)" -C ~ && cat ~/strata-rebuild-kit/VERSION && cd ~/github/strata-work/Strata

# 1. the kit's Meteor Lake patch is exactly what is applied (the reverse check passes only then)
git apply --check -R ~/strata-rebuild-kit/patches/mtl-igpu-local.patch && echo "OK: identical" || echo "DIFFERS: use mtl-igpu-local-CURRENT.patch from the snapshot below"

# 2. the build environment: digests and the Dockerfile's base
docker images --digests --format '{{.Repository}}:{{.Tag}}  {{.Digest}}  {{.ID}}' | grep -E "llama-sycl|strata-sycl|codex-sandbox"
grep -n '^FROM' sycl/tools/Dockerfile                      # expect the llama-sycl-b70:stable reference

# 3. the agent: version in the image, and that the pinned asset exists
docker run --rm codex-sandbox codex --version              # the version in ~/agent/.codex-version (or the kit's agent/CODEX_VERSION on a fresh build)
cat ~/agent/.codex-version 2>/dev/null; cat ~/strata-rebuild-kit/agent/CODEX_VERSION

# 4. the RAPL service (only if you installed it): systemd's own verdict on the unit
systemd-analyze verify /etc/systemd/system/strata-rapl-power.service
systemd-analyze security strata-rapl-power | tail -4
systemctl is-active strata-rapl-power; cat /run/strata/power.json

# 5. the machine snapshot: run-*.sh, configs, the CURRENT patches, setup history, image history (mode 700; read it once, it is not redacted)
bash ~/strata-rebuild-kit/tools/capture-machine-files.sh

# 6. optional, 5 to 10 minutes of disk reading: model checksums, to compare with the Hugging Face page and to spot corruption after a re-download
cd ~/github/strata-work/data/models/swift-IQ3_XXS && sha256sum *.gguf | tee ~/strata-rebuild-kit/model-sha256.txt
```
Then re-pack the kit (`tar czf strata-rebuild-kit-mine.tar.gz -C ~ strata-rebuild-kit`) and keep a copy off the machine.

---

## 12. Open items

- Run `codex gate-test` on the machine, then a first `codex loop` trial on a small project (Step 9b); then the capability gate (section 10).
- Check the BIOS fan or performance profile and airflow (the package reaches 90 to 100 C under load).
- Restrict the Sunshine ports; retire NextChat.
- Run the checks in section 11 and re-pack the kit.
- Upstream: your report #2282 has the TTM findings (posted, crediting #2412). Still unreported: the batched-prompt hang on 0.1.43.1 (apparently fixed in 0.1.44.1), the 90 s watchdog false alarm, and the Intel iGPU monitor gap (the xe readings in the kit's patches would be a natural contribution).
- Untested: `STRATA_SPEC_COUPLED=1`/`GUMBEL=1` with sampling, `--kv-grow`, Tier 2 settings, a second machine (Ryzen 7735HS) as a fast tier.
- First lessons: run a small codex loop trial, then read --lessons candidates. For the capability gate, record per task: attempts, minutes, tool-call failures and tokens. Optional: wire codex lessons into the codex command.

## Appendix A. The settled config

`~/strata-rebuild-kit/config/strata-swift-iq3_xxs.json` (paths are this machine's; replace `/home/adam`):

```json
{
  "exe": "/home/adam/github/strata-work/Strata/sycl/serve/strata-sycl.sh",
  "cwd": "/home/adam/github/strata-work/Strata",
  "tokenizer": "/home/adam/github/strata-work/data/packs/swift-iq3_xxs/tokenizer",
  "model_name": "swift-1.5-iq3_xxs",
  "log": "/home/adam/github/strata-work/Strata/strata-swift-iq3_xxs.log",
  "port": 8085,
  "backend": "sycl",
  "sycl_root": "/home/adam/github/strata-work",
  "reasoning_budget_tokens": 2048,
  "fit_max_tokens": true,
  "engine_silence_s": 900,
  "slot_save_path": "/work/sessions",
  "tool_call_recovery": true,
  "reasoning_effort": "low",
  "args": [
    "--pack", "/work/data/packs/swift-iq3_xxs",
    "--native", "/work/data/models/swift-IQ3_XXS/Swift-Qwen3.8-Flash-Next-GSQ-RCO-IQ3_XXS-00001-of-00002.gguf",
    "--ple-gguf", "/work/data/models/swift-IQ3_XXS/Swift-Qwen3.8-Flash-Next-GSQ-RCO-IQ3_XXS-00001-of-00002.gguf",
    "--expert-profile", "/work/Strata/data/expert-profile-sycl.bin",
    "--expert-cache", "auto", "--prefill", "2048", "--spec", "4", "--spec-min-p", "0.5",
    "--mtp", "/work/data/mtp/rt", "--max-context", "65536", "--kv", "int8", "--pool-workers", "8",
    "--stream-experts", "--vram-reserve-mib", "2048", "--no-prefill-borrow", "--short-read", "64"
  ],
  "env": {
    "UR_L0_ENABLE_RELAXED_ALLOCATION_LIMITS": "1",
    "STRATA_WINDOW_SYNC": "1",
    "STRATA_UMA_HEADROOM_GIB": "12",
    "STRATA_MIRROR_MIB": "12288",
    "STRATA_SYCL_BIN": "build-sycl/strata",
    "UR_L0_USE_IMMEDIATE_COMMANDLISTS": "1"
  }
}
```

## Appendix B. The kit

```
strata-rebuild-kit/
  README.md
  strata-nuc-playbook.md            this file
  patches/  mtl-igpu-local.patch    the Meteor Lake patch (device.hpp, setup_intel.py) for Strata 0.1.x
            mtl-igpu-local.0.2.patch  the same, remade for 0.2.2 and newer (a NAME.SERIES.patch beside NAME.patch is a variant: the tools try each)
            xe-monitor-v2.patch     Intel GPU readings for the dashboard (apply first)
            xe-power.patch          package power via a root sampler (apply second)
            xe-monitor-quiet.patch  hides the "limited: pl4" note on the GPU card (optional, apply last)
  config/   strata-swift-iq3_xxs.json, start-swift.sh, chat-save.sh, chat-restore.sh, ttm-pool.conf
  tools/    strata-orchestrator.py, strata-bench2.py, strata-recon.sh, strata-tune.py, capture-machine-files.sh, strata-effort-position.py, strata-models.py, strata-update.py, codex-update.py, codex_compat.py
  kit.sh, VERSION, CHANGES.md, README.md   what to do after extracting (kit.sh status / update)
  agent/    strata-agent-setup.sh (builds the image), install-codex-client.sh + codex-client + strata-front.py (the `codex` command and its title filter),
            browser-check.py + add-browser-to-image.sh (real-browser checks)
  machine-snapshot-DATE/            created by capture-machine-files.sh
```
