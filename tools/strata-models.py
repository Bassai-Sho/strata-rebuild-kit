#!/usr/bin/env python3
"""strata-models.py: see what each Strata model owns, remove one safely, and change a model's settings.

    python3 strata-models.py list                          every installed model, where its files are, their size, what is shared
    python3 strata-models.py remove TAG                    PREVIEW only: what would be deleted and how much space it frees
    python3 strata-models.py remove TAG --yes              delete it (asks you to type the tag first)
    python3 strata-models.py show TAG                      the model's settings (strata-TAG.json)
    python3 strata-models.py set TAG key=value [...]       a top-level setting:  reasoning_budget_tokens=4096   effort_position=end
                                                           an env entry:         env.STRATA_MIRROR_MIB=12288
    python3 strata-models.py arg TAG --flag [value]        set or replace an engine option:  arg swift-iq3_xxs --prefill 2048
    python3 strata-models.py unarg TAG --flag             remove an engine option
        [--strata DIR]   the Strata folder (default ~/github/strata-work/Strata)

TAG is the part of the config's name after `strata-`: for strata-swift-iq3_xxs.json it is swift-iq3_xxs (`list` shows them).

Why this exists. Strata has no command to remove a model: the files stay until you delete them. And re-running `./setup.sh --setup` to
change a setting does NOT keep engine options you added to a config's "args" (setup chooses those itself; it keeps one .bak of the old file),
so tuned values such as --prefill or --short-read would be replaced. Editing the config is the safe way to change a setting.

What `remove` deletes: the model's own folder (its GGUF shards), its pack (tokenizer) folder, its start script and log. Its config is not
thrown away: it is archived under ~/strata-backups/removed-models/ so it can be restored. What it never deletes: anything another model's
config also uses (a shared shard, the draft head `mtp`, the expert profile), the saved chats folder, or anything outside your Strata
data folders. A model that is running cannot be removed. Settings are read when Strata starts: restart it after a change (codex stop, codex).
"""
import datetime, glob, json, os, re, shutil, sys

DEFAULT = os.path.expanduser("~/github/strata-work/Strata")
PATH_FLAGS = ("--pack", "--native", "--ple-gguf", "--mtp", "--expert-profile", "--model", "--gguf")


def human(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1000 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1000.0


def size_of(path):
    if os.path.isfile(path):
        return os.path.getsize(path)
    total = 0
    for root, _, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


class Model:
    def __init__(self, strata, cfg_path):
        self.strata, self.cfg_path = strata, cfg_path
        self.tag = os.path.basename(cfg_path)[len("strata-"):-len(".json")]
        with open(cfg_path) as fh:
            self.cfg = json.load(fh)
        # the engine runs in a container where the data folder is /work; on this machine /work is the same folder (sycl_root)
        self.root = self.cfg.get("sycl_root") or os.path.dirname(strata)

    def host(self, p):
        if p.startswith("/work/") or p == "/work":
            cand = os.path.join(self.root, p[len("/work/"):])
            return cand
        return p

    def args(self):
        return [str(a) for a in self.cfg.get("args", [])]

    def flag(self, name):
        a = self.args()
        for i, x in enumerate(a[:-1]):
            if x == name:
                return self.host(a[i + 1])
        return None

    def files(self):
        """What this model uses, as host paths: {role: path}."""
        out = {}
        native = self.flag("--native") or self.flag("--model") or self.flag("--gguf")
        if native:
            folder = os.path.dirname(native)
            if os.path.basename(folder).lower() == self.tag.lower():          # setup's own layout: one folder per model
                out["model folder (GGUF shards)"] = folder
            else:                                                             # a folder of your own (--gguf-dir): only this model's shard files
                m = re.match(r"^(.*)-\d{5}-of-\d{5}\.gguf$", os.path.basename(native))
                shards = sorted(glob.glob(os.path.join(folder, glob.escape(m.group(1)) + "-?????-of-?????.gguf"))) if m else [native]
                for i, sh in enumerate(shards or [native]):
                    out[f"GGUF shard {i + 1}"] = sh
        pack = self.flag("--pack")
        if pack:
            out["pack (tokenizer)"] = pack
        if self.cfg.get("tokenizer") and not pack:
            out["pack (tokenizer)"] = os.path.dirname(self.host(self.cfg["tokenizer"]))
        return out

    def shared(self):
        """Paths several models use: the draft head and the expert profile."""
        out = {}
        if self.flag("--mtp"):
            out["draft head (mtp)"] = self.flag("--mtp")
        if self.flag("--expert-profile"):
            out["expert profile"] = self.flag("--expert-profile")
        return out

    def extras(self):
        out = {}
        run = os.path.join(self.strata, f"run-{self.tag}.sh")
        if os.path.isfile(run):
            out["start script"] = run
        log = self.cfg.get("log") and self.host(self.cfg["log"])
        if log and os.path.isfile(log):
            out["log"] = log
        return out


def load_models(strata):
    models = []
    for p in sorted(glob.glob(os.path.join(strata, "strata-*.json"))):
        if ".bak" in p or ".removed" in p or p.endswith(".tmp"):
            continue
        try:
            models.append(Model(strata, p))
        except (OSError, ValueError):
            print(f"(skipping {os.path.basename(p)}: not a readable JSON config)")
    return models


def running_tags(models):
    """Models whose config a running Strata server was started with (read from the process list); STRATA_MODELS_RUNNING overrides for tests."""
    forced = os.environ.get("STRATA_MODELS_RUNNING")
    if forced is not None:
        return {t for t in forced.split(",") if t}
    tags = set()
    for cmdline in glob.glob("/proc/[0-9]*/cmdline"):
        try:
            with open(cmdline, "rb") as fh:
                text = fh.read().replace(b"\0", b" ").decode("utf8", "replace")
        except OSError:
            continue
        if "server.py" in text and "--config" in text:
            for m in models:
                if os.path.basename(m.cfg_path) in text:
                    tags.add(m.tag)
    return tags


def used_elsewhere(path, others):
    """True if another model's config refers to this path or to anything inside it (a shared shard)."""
    prefix = os.path.realpath(path)
    for m in others:
        for p in [*m.files().values(), *m.shared().values(), *[m.host(a) for a in m.args() if a.startswith("/")]]:
            rp = os.path.realpath(p)
            if rp == prefix or rp.startswith(prefix + os.sep) or prefix.startswith(rp + os.sep):
                return m.tag
    return None


def within_data(path, models):
    roots = {os.path.realpath(m.root) for m in models}
    rp = os.path.realpath(path)
    return any(rp.startswith(r + os.sep) for r in roots)


def cmd_list(strata, models):
    if not models:
        print(f"no strata-*.json configs in {strata}")
        return 1
    running = running_tags(models)
    for m in models:
        print(f"\n{m.tag}{'   (RUNNING)' if m.tag in running else ''}")
        print(f"  settings: {m.cfg_path}")
        for role, p in {**m.files(), **m.extras()}.items():
            print(f"  {role:28s} {human(size_of(p)) if os.path.exists(p) else 'missing':>9s}  {p}")
        for role, p in m.shared().items():
            users = [o.tag for o in models if o.shared().get(role) == p]
            print(f"  {role:28s} {human(size_of(p)) if os.path.exists(p) else 'missing':>9s}  {p}   (shared by {len(users)} model{'s' if len(users) != 1 else ''}: never removed with one)")
    try:
        free = shutil.disk_usage(models[0].root).free
        print(f"\nfree space where the data lives: {human(free)}")
    except OSError:
        pass
    return 0


def cmd_remove(strata, models, tag, yes):
    target = next((m for m in models if m.tag == tag), None)
    if target is None:
        print(f"no model {tag!r}. Installed: {', '.join(m.tag for m in models) or 'none'}")
        return 2
    others = [m for m in models if m is not target]
    if tag in running_tags(models):
        print(f"{tag} is running. Stop Strata first (codex stop, or Ctrl+C in its window), then run this again.")
        return 2
    plan, keep, freed = [], [], 0
    for role, p in {**target.files(), **target.extras()}.items():
        if not os.path.exists(p):
            continue
        if not within_data(p, models):
            keep.append((role, p, "outside your Strata data folders: left alone"))
        elif (other := used_elsewhere(p, others)):
            keep.append((role, p, f"also used by {other}: kept"))
        else:
            plan.append((role, p)); freed += size_of(p)
    for role, p in target.shared().items():
        keep.append((role, p, "shared: kept" if others else "shared (this is the last model): kept; delete by hand if you are done with Strata"))
    print(f"\nRemoving {tag}" + ("" if yes else "   (PREVIEW: nothing is deleted without --yes)"))
    for role, p in plan:
        print(f"  delete   {role:28s} {human(size_of(p)):>9s}  {p}")
    for role, p, why in keep:
        print(f"  keep     {role:28s} {'':>9s}  {p}   ({why})")
    archive = os.path.expanduser(f"~/strata-backups/removed-models/{tag}-{datetime.datetime.now():%Y%m%d-%H%M%S}")
    print(f"  archive  its settings ({os.path.basename(target.cfg_path)}) and start script to {archive} (so it can be restored), then remove them from {strata}")
    print(f"\nThis frees about {human(freed)}.")
    if not plan:
        print("Nothing of this model's own to delete.")
    if not yes:
        print(f"To do it:  python3 {os.path.basename(sys.argv[0])} remove {tag} --yes")
        return 0
    if input(f"Type the model's tag ({tag}) to delete these files: ").strip() != tag:
        print("Not confirmed. Nothing was deleted.")
        return 1
    os.makedirs(archive, exist_ok=True)
    for p in (target.cfg_path, os.path.join(strata, f"run-{tag}.sh")):
        if os.path.isfile(p):
            shutil.copy2(p, archive)
    for role, p in plan:
        if os.path.isdir(p):
            shutil.rmtree(p)
        else:
            os.unlink(p)
        print(f"  deleted {p}")
    os.unlink(target.cfg_path)
    run = os.path.join(strata, f"run-{tag}.sh")
    if os.path.isfile(run):
        os.unlink(run)
    print(f"Done. {human(freed)} freed. To bring the model back: run ./setup.sh --setup to download it again, then copy the archived settings back over the new {os.path.basename(target.cfg_path)}.")
    return 0


def write_config(m, cfg, note):
    backup = f"{m.cfg_path}.bak-settings-{datetime.datetime.now():%Y%m%d-%H%M%S}"
    shutil.copy2(m.cfg_path, backup)
    tmp = m.cfg_path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(cfg, fh, indent=2)
        fh.write("\n")
    with open(tmp) as fh:
        json.load(fh)                                         # never leave a file that does not parse
    os.replace(tmp, m.cfg_path)
    print(f"{note}\nbackup: {backup}\nRestart Strata for it to take effect:  codex stop   then   codex")


def parse_value(text):
    try:
        return json.loads(text)
    except ValueError:
        return text


def cmd_show(m):
    cfg = dict(m.cfg)
    args, env = cfg.pop("args", []), cfg.pop("env", {})
    print(f"{m.cfg_path}\n")
    for k, v in cfg.items():
        print(f"  {k}: {json.dumps(v)}")
    print("  env:")
    for k, v in env.items():
        print(f"    {k} = {v}")
    print("  args (engine options):")
    a, i = [str(x) for x in args], 0
    while i < len(a):
        if a[i].startswith("--") and i + 1 < len(a) and not a[i + 1].startswith("--"):
            print(f"    {a[i]} {a[i + 1]}"); i += 2
        else:
            print(f"    {a[i]}"); i += 1
    return 0


def cmd_set(m, pairs):
    cfg = json.loads(json.dumps(m.cfg))
    notes = []
    for pair in pairs:
        if "=" not in pair:
            print(f"expected key=value, got {pair!r}")
            return 2
        key, _, value = pair.partition("=")
        if key.startswith("env."):
            cfg.setdefault("env", {})[key[4:]] = value
            notes.append(f"env {key[4:]} = {value}")
        elif key == "args":
            print("change engine options with:  arg TAG --flag value")
            return 2
        else:
            cfg[key] = parse_value(value)
            notes.append(f"{key} = {json.dumps(cfg[key])}")
    write_config(m, cfg, "set " + "; ".join(notes))
    return 0


def split_args(args):
    """[--a, 1, --b, --c, 2] -> [[--a, 1], [--b], [--c, 2]] (an option and its value, if it has one)."""
    out = []
    for x in args:
        if x.startswith("--") or not out:
            out.append([x])
        else:
            out[-1].append(x)
    return out


def cmd_arg(m, flag, values, remove):
    if not flag.startswith("--"):
        print("an engine option starts with --, for example --prefill")
        return 2
    cfg = json.loads(json.dumps(m.cfg))
    groups = split_args([str(a) for a in cfg.get("args", [])])
    idx = next((i for i, g in enumerate(groups) if g[0] == flag), None)
    if remove:
        if idx is None:
            print(f"{flag} is not set: nothing to remove")
            return 0
        old = groups.pop(idx)
        note = f"removed {' '.join(old)}"
    else:
        new = [flag, *values]
        note = f"{flag} {' '.join(values)}".strip() + (f" (was: {' '.join(groups[idx])})" if idx is not None else " (new)")
        if idx is None:
            groups.append(new)
        else:
            groups[idx] = new
    cfg["args"] = [x for g in groups for x in g]
    write_config(m, cfg, note)
    return 0


def main():
    argv = sys.argv[1:]
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    action, rest = argv[0], argv[1:]
    if action not in ("list", "remove", "show", "set", "arg", "unarg"):
        print(f"unknown action {action!r}; one of: list remove show set arg unarg  (--help)")
        return 2
    strata, yes = DEFAULT, False
    cleaned = []
    i = 0
    while i < len(rest):                                       # --strata DIR and --yes may stand anywhere; everything else is the command's own
        if rest[i] == "--strata" and i + 1 < len(rest):
            strata = rest[i + 1]; i += 2; continue
        if rest[i] == "--yes":
            yes = True; i += 1; continue
        cleaned.append(rest[i]); i += 1
    strata = os.path.abspath(os.path.expanduser(strata))
    models = load_models(strata)
    if action == "list":
        return cmd_list(strata, models)
    tag, rest = (cleaned[0], cleaned[1:]) if cleaned and not cleaned[0].startswith("--") else (None, cleaned)
    if not tag:
        print("which model? (python3 strata-models.py list shows the tags)")
        return 2
    m = next((x for x in models if x.tag == tag), None)
    if m is None:
        print(f"no model {tag!r}. Installed: {', '.join(x.tag for x in models) or 'none'}")
        return 2
    if action == "remove":
        return cmd_remove(strata, models, tag, yes)
    if action == "show":
        return cmd_show(m)
    if action == "set":
        return cmd_set(m, rest)
    if not rest:
        print("which option? for example:  arg TAG --prefill 2048")
        return 2
    return cmd_arg(m, rest[0], rest[1:], action == "unarg")


if __name__ == "__main__":
    sys.exit(main())
