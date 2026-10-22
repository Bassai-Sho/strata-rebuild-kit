#!/usr/bin/env python3
"""codex-loop.py: run a project's PLAN.md unattended, one small task per fresh Codex session.

    codex loop NAME [--sessions 12] [--minutes 45] [--attempts 3] [--hours 8] [--dry-run] [--yes]
                    [--allow-new-files] [--clean-retry] [--accept-prepassed]
    codex-loop.py --lessons list | candidates [--arg raw] | add --arg "- [web] text" | remove --arg "some of its text"      (the global lessons, see below)

Why a loop of short sessions: a fresh session starts in about 15 s (Strata caches the shared prompt), a long one has to be compacted
(about 12 minutes), and the plan, the progress notes and git carry the memory between sessions. The prompt is identical every time,
so the cache covers it.

What makes it safe to leave alone (the agent's word is never taken for it):
  * the LOOP runs each task's check itself, in a new container with no network, and only then ticks the task and commits;
  * each session may change only the files its task lists (`files:`) and the page a `browser-check` names (that page is the product under test);
    anything else is reverted and kept in the log folder, and PLAN.md is restored: an agent cannot weaken a test, rewrite a check script, change the
    project's rules or tick its own work. `--allow-new-files` also lets a session ADD new files (a script, a stylesheet) that are not test or check
    material (tests/, test_*, check*, conftest.py, package.json, AGENTS.md and the like stay protected); existing files still need to be listed;
  * a check that already passes BEFORE the work cannot tell done from not done: the loop stops and asks;
  * the agent's container has NO network (--network none); its only way out is a Unix socket to Strata's filter;
  * REFINEMENT: a failed attempt's code stays on disk (committed as work in progress) and the next session is told why it failed (progress.txt) and
    fixes it instead of starting over; when the task finally passes, the work in progress is squashed into one commit. After the last failed attempt
    the tree goes back to where the task started (every attempt is kept on refs/loop/...) and the loop stops. `--clean-retry` restores the older
    behaviour: reset after every failed attempt.

LESSONS: ~/agent/lessons/LESSONS.md holds short rules learned from earlier runs, one line each:  - [tag] do this when that.  The LOOP copies the lines
that fit the project (tag `all`, or a tag from the file types in PLAN.md) into the session's private copy of the Codex home; the agent cannot read the
master file or change what the next session gets. The loop never writes a lesson itself: after a task that needed a retry, was blocked, or had files
reverted, it records a CANDIDATE (check output tails and file names only, never the agent's own words) in lessons/candidates.jsonl, and you promote
one with `--lessons add`. At most 10 lines and 1400 characters are injected (what does not fit is reported, never cut silently); the text is fixed for the whole
run so Strata's prompt cache holds. `--lessons candidates` groups the same problem together and says how often it happened; `add` refuses text that looks like injection,
a web address, a network or risky shell command or a credential; `remove` takes some of the lesson's own text (positions move); every change keeps a backup in lessons/backups.

Stop it with a file named STOP in the project folder, or Ctrl+C. Each run writes loop-report.md in ~/agent/logs/loop-NAME-DATE/.
"""
import argparse, datetime, fnmatch, hashlib, json, os, re, shutil, signal, subprocess, sys, tempfile, time, urllib.request

AGENT = os.environ.get("AGENT_DIR", os.path.expanduser("~/agent"))
IMAGE = os.environ.get("CODEX_IMAGE", "codex-sandbox")
STRATA_URL = os.environ.get("STRATA_URL", "http://127.0.0.1:8085")
STRATA_LOG = os.environ.get("STRATA_LOG", os.path.expanduser("~/github/strata-work/Strata/strata-swift-iq3_xxs.log"))
SLOTS = os.environ.get("STRATA_EXPECT_SLOTS", "24576")
SOCKET = os.environ.get("CODEX_FRONT_SOCKET", os.path.join(AGENT, "run", "strata-front.sock"))
FRONT_PORT = os.environ.get("CODEX_FRONT_PORT", "8086")
LOCAL = os.environ.get("CODEX_LOOP_LOCAL") == "1"            # test hooks: run checks on this machine, and the agent from CODEX_LOOP_AGENT_CMD
AGENT_CMD = os.environ.get("CODEX_LOOP_AGENT_CMD", "")
CHECK_SECONDS = int(os.environ.get("CODEX_LOOP_CHECK_SECONDS", "600"))

# identical in every session: Strata's prompt cache covers it
PROMPT = ("Read AGENTS.md, PLAN.md and progress.txt, then do the first unticked task in PLAN.md and nothing else. "
          "Read only the SPEC.md lines it names. Run its check command; if it fails, fix the code and run it again (at most 3 runs in this session). "
          "Do not edit PLAN.md and do not commit: the loop does that after it has run the check itself. "
          "When the check prints OK, stop. If every task is ticked, print ALL_TASKS_DONE.")

NOISE = ("__pycache__/", ".pyc", ".pytest_cache/", "node_modules/", "artifacts/", ".DS_Store", ".loop/")
RUNNERS = ("python", "python3", "pytest", "node", "npm", "bash", "sh", "make", "browser-check", "./", "cargo", "go ", "java", "ruby", "php", "cat ", "grep ", "test ", "diff ", "wc ")
NETWORK_WORDS = re.compile(r"\b(curl|wget|git\s+clone|pip3?\s+install|npm\s+install|apt(-get)?\s+install|docker)\b")
VACUOUS = re.compile(r"^\s*(true|:|exit\s+0|echo\b.*|printf\b.*|test\s+1)\s*$|(\|\||;)\s*(true|exit\s+0|:)\s*$")


def now():
    return datetime.datetime.now().strftime("%H:%M:%S")


def say(msg=""):
    print(msg, flush=True)


# ------------------------------------------------------------------------------------------------------------------ lessons
LESSON_RE = re.compile(r"^- \[([a-z0-9][a-z0-9,-]{0,40})\] (\S.{0,219})$")
LESSON_HEADER = "## Lessons from earlier runs\nThe loop added these to your rules. Follow them. You cannot change them."
LESSON_LINES_MAX, LESSON_CHARS_MAX, LESSONS_FILE_MAX = 10, 1400, 40
CAND_MAX, CAND_KEEP = 300, 200
TAG_BY_EXT = {".html": "web", ".htm": "web", ".css": "web", ".svg": "web", ".js": "js", ".mjs": "js", ".ts": "js", ".py": "python",
              ".sh": "shell", ".c": "c", ".h": "c", ".cpp": "c", ".go": "go", ".rs": "rust"}


def clean(text, n=200):
    """One line of printable ASCII, at most n characters: what may be stored or shown from anything a session could have influenced."""
    return re.sub(r"\s+", " ", re.sub(r"[^\x20-\x7e]", " ", str(text))).strip()[:n]


def lessons_dir():
    return os.path.join(AGENT, "lessons")


def lessons_path():
    return os.path.join(lessons_dir(), "LESSONS.md")


def candidates_path():
    return os.path.join(lessons_dir(), "candidates.jsonl")


def write_atomic(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        fh.write(text)
    os.replace(tmp, path)


def load_lessons():
    """[(tags, line)] for every well-formed lesson in the global file. Anything else in the file (comments, long or odd lines) is ignored."""
    try:
        with open(lessons_path(), errors="replace") as fh:
            raw = fh.read()
    except OSError:
        return []
    out = []
    for ln in raw.splitlines():
        m = LESSON_RE.match(clean(ln, 400))        # an over-long line fails the match; it is never cut down into a lesson
        if m:
            out.append((set(m.group(1).split(",")), f"- [{m.group(1)}] {m.group(2).strip()}"))
    return out


def project_tags(tasks):
    """Tags that describe the project, from the file types its plan lists (the plan is the loop's, the agent cannot edit it)."""
    tags = set()
    for t in tasks:
        for f in t.files:
            tag = TAG_BY_EXT.get(os.path.splitext(f.strip().lower())[1])
            if tag:
                tags.add(tag)
        if t.cmd and "browser-check" in t.cmd:
            tags.add("web")
    return tags


def lessons_plan(tags):
    """(text, lines, left_out): the lessons for a project with these tags, in file order, until the limits are reached. Same bytes every call
    while the file is unchanged: no dates, no counters. `left_out` are matching lessons that did not fit, so nobody is surprised by a silent cut."""
    want = {"all"} | set(tags)
    matching = [line for t, line in load_lessons() if t & want]
    picked, size = [], 0
    for line in matching:
        if len(picked) >= LESSON_LINES_MAX or size + len(line) + 1 > LESSON_CHARS_MAX:
            break
        picked.append(line)
        size += len(line) + 1
    return ((LESSON_HEADER + "\n" + "\n".join(picked) + "\n") if picked else ""), picked, matching[len(picked):]


def lessons_block(tags):
    """(text, lines): what lessons_plan injects."""
    text, picked, _ = lessons_plan(tags)
    return text, picked


def lessons_id(block):
    return hashlib.sha256(block.encode()).hexdigest()[:10] if block else ""


def lessons_usage():
    """[(label, n_matching, n_injected, chars, left_out)] for the lessons file: tag `all` alone, each tag with `all`, and every tag at once."""
    items = load_lessons()
    tags = sorted({t for ts, _ in items for t in ts} - {"all"})
    rows = []
    for label, ts in [("all only", set())] + [(f"all + {t}", {t}) for t in tags] + ([("every tag at once", set(tags))] if len(tags) > 1 else []):
        _, picked, left = lessons_plan(ts)
        rows.append((label, len(picked) + len(left), len(picked), sum(len(x) + 1 for x in picked), left))
    return rows


# What a pasted lesson may not contain. A person promotes lessons, but the text may come from a candidate or from anywhere else; these are
# the shapes of injection, exfiltration and risky commands. A false positive can be written into the file by hand.
LESSON_LINT = [
    (re.compile(r"https?://|\bwww\.", re.I), "a web address"),
    (re.compile(r"\b(curl|wget|scp|sftp|ssh|ftp|netcat|ncat|nc)\b", re.I), "a network command"),
    (re.compile(r"\b(sudo|chmod|chown|eval|base64)\b|\brm\s+-\w*r|\|\s*(ba|z)?sh\b", re.I), "a risky shell command"),
    (re.compile(r"\b(ignore|disregard|forget|override)\b.{0,30}\b(previous|prior|earlier|above|all|any|the)\b.{0,30}\b(rules?|instructions?|prompts?|lessons?)\b"
                r"|\byou are now\b|\bnew instructions\b|\b(system|developer) (prompt|message)\b", re.I), "wording that tries to override other instructions"),
    (re.compile(r"\b(passwords?|passwd|api[ _-]?keys?|private keys?|ssh keys?|credentials?|access tokens?|api tokens?)\b", re.I), "a secret or credential"),
    (re.compile(r"(^|[\s\"'(])(~/|/home/|/etc/|/root/|/mnt/)"), "a path outside the project"),
]


def lint_lesson(text):
    """The reason a lesson's text is refused, or None."""
    for rx, why in LESSON_LINT:
        if rx.search(text):
            return why
    return None


BACKUP_RE = re.compile(r"^LESSONS-\d{8}-\d{6}-\d{6}\.md$")
BACKUPS_KEEP = 20


def backup_lessons():
    """Copy the current lessons file to lessons/backups/ before a change (the newest BACKUPS_KEEP are kept). Returns the copy's path or None."""
    try:
        old = read_text(lessons_path())
    except OSError:
        return None
    if not old.strip():
        return None
    d = os.path.join(lessons_dir(), "backups")
    dst = os.path.join(d, f"LESSONS-{datetime.datetime.now():%Y%m%d-%H%M%S-%f}.md")
    write_atomic(dst, old)
    for name in sorted(f for f in os.listdir(d) if BACKUP_RE.match(f))[:-BACKUPS_KEEP]:
        os.remove(os.path.join(d, name))
    return dst


def record_candidate(project, task, tags, lid, kind, attempts, failed, files, reverted):
    """Best effort: a candidate lesson, built only from what the loop itself saw. Never injected anywhere; a person reads it. Returns the record or None."""
    try:
        rec = {"time": datetime.datetime.now().isoformat(timespec="seconds"), "project": clean(project, 60), "task": task.id,
               "goal": clean(task.goal, 120), "tags": sorted(tags), "kind": kind, "attempts": attempts,
               "failed": failed, "files": [clean(f, 80) for f in files[:8]], "reverted": [clean(f, 80) for f in reverted[:6]],
               "lessons": lid or "none"}
        os.makedirs(lessons_dir(), exist_ok=True)
        with open(candidates_path(), "a") as fh:
            fh.write(json.dumps(rec, sort_keys=True) + "\n")
        with open(candidates_path()) as fh:
            lines = fh.read().splitlines()
        if len(lines) > CAND_MAX:
            write_atomic(candidates_path(), "\n".join(lines[-CAND_KEEP:]) + "\n")
        return rec
    except OSError as e:
        say(f"           (could not record a lesson candidate: {e})")
        return None


def load_candidates():
    try:
        rows = read_text(candidates_path()).splitlines()
    except OSError:
        return []
    out = []
    for ln in rows:
        try:
            r = json.loads(ln)
        except ValueError:
            continue
        if isinstance(r, dict):
            out.append(r)
    return out


def candidate_signature(r):
    """What makes two candidates the same problem: the kind, the project tags, and the FIRST thing that went wrong (numbers ignored)."""
    norm = lambda x: re.sub(r"\d+", "#", clean(x, 300).lower())
    failed = r.get("failed") or []
    core = norm(failed[0].get("check", "")) if failed else "reverted: " + ",".join(sorted(norm(x) for x in r.get("reverted", [])))
    return (r.get("kind", ""), tuple(sorted(r.get("tags", []))), core)


def candidate_groups(rows):
    """[{sig, count, projects, last, files, check, kind, tags}] most frequent first."""
    groups = {}
    for r in rows:
        g = groups.setdefault(candidate_signature(r), {"sig": candidate_signature(r), "count": 0, "projects": [], "files": [], "last": ""})
        g["count"] += 1
        if r.get("project") not in g["projects"]:
            g["projects"].append(r.get("project"))
        for f in r.get("files", []):
            if f not in g["files"]:
                g["files"].append(f)
        g["last"] = max(g["last"], r.get("time", ""))
        g["kind"], g["tags"] = r.get("kind", ""), r.get("tags", [])
        fl = r.get("failed") or []
        g["check"] = fl[0].get("check", "") if fl else "(reverted: " + ", ".join(r.get("reverted", [])) + ")"
    return sorted(sorted(groups.values(), key=lambda g: g["last"], reverse=True), key=lambda g: -g["count"])


def lessons_cli(act, arg):
    path = lessons_path()
    arg = (arg or "").strip()
    if act == "list":
        items = load_lessons()
        say(f"{path}: {len(items)} lesson(s)")
        for n, (t, line) in enumerate(items, 1):
            say(f"  {n:2d}  {line}")
        try:
            skipped = sum(1 for ln in read_text(path).splitlines() if ln.startswith("- ") and not LESSON_RE.match(clean(ln, 400)))
        except OSError:
            skipped = 0
        if skipped:
            say(f"  ({skipped} line(s) that start with '- ' are ignored: they do not match  - [tag] text  or are too long)")
        if items:
            say(f"\nRoom per project type (limits: {LESSON_LINES_MAX} lines, {LESSON_CHARS_MAX} characters; lessons higher in the file are kept first):")
            for label, total, injected, chars, left in lessons_usage():
                say(f"  {label:20s} {injected} of {total} lines injected, {chars} characters" + (f", {len(left)} LEFT OUT: {left[0][:60]}..." if left else ""))
        return 0
    if act == "candidates":
        rows = load_candidates()
        if not rows:
            say("no candidates yet")
            return 0
        if arg == "raw":
            for r in rows[-20:]:
                say(f"{r.get('time', '')}  {r.get('project', '')}/{r.get('task', '')}  {r.get('kind', '')}  attempts={r.get('attempts', '')}  tags={','.join(r.get('tags', []))}  lessons={r.get('lessons', '')}")
                for f in r.get("failed", []):
                    say(f"    attempt {f.get('attempt')}: check said: {f.get('check', '')}" + (f"   reverted: {', '.join(f.get('reverted', []))}" if f.get("reverted") else ""))
                if r.get("files"):
                    say(f"    files changed by the attempt that ended it: {', '.join(r['files'])}")
                if r.get("reverted"):
                    say(f"    reverted in that attempt: {', '.join(r['reverted'])}")
            return 0
        groups = candidate_groups(rows)
        say(f"{len(rows)} candidate(s) in {len(groups)} kind(s) of problem, most frequent first (add --arg raw for every record):")
        for g in groups:
            say(f"\n{g['count']}x  {g['kind']}  tags={','.join(g['tags']) or '-'}  projects: {', '.join(g['projects'])}" + ("   <- repeats: worth a lesson" if g["count"] >= 2 else ""))
            say(f"    check said: {g['check']}")
            if g["files"]:
                say(f"    changed in the attempt that ended it: {', '.join(g['files'])}")
            say(f"    last seen: {g['last']}")
        return 0
    if act == "add":
        line = arg if arg.startswith("- ") else "- " + arg
        if clean(line, 400) != line or not LESSON_RE.match(line):
            say('not added: write one line of plain ASCII like  - [web] Create every file the page loads in the same task.  '
                '(tag: letters, digits, comma, dash; text up to 220 characters; tag `all` applies to every project)')
            return 2
        why = lint_lesson(line)
        if why:
            say(f"not added: the text contains {why}. A lesson is plain advice about the work; if this is a false alarm, write the line into {path} by hand.")
            return 2
        items = load_lessons()
        if len(items) >= LESSONS_FILE_MAX:
            say(f"not added: the file already has {len(items)} lessons (limit {LESSONS_FILE_MAX}). Remove one first: --lessons remove --arg 'some of its text'")
            return 2
        if line.lower() in [x.lower() for _, x in items]:
            say("not added: that lesson is already there")
            return 2
        bak = backup_lessons()
        try:
            old = read_text(path)
        except OSError:
            old = ""
        write_atomic(path, (old if not old or old.endswith("\n") else old + "\n") + line + "\n")
        say(f"added. {len(items) + 1} lesson(s) in {path}" + (f"   (previous file saved as {bak})" if bak else ""))
        tags = set(LESSON_RE.match(line).group(1).split(","))
        known = sorted({t for ts, _ in load_lessons() for t in ts} - {"all"})
        check_sets = [{t} for t in sorted(tags - {"all"})] + ([set()] + [{t} for t in known] if "all" in tags else [])
        for ts in check_sets:
            _, _, left = lessons_plan(ts)
            if line in left:
                say(f"  WARNING: for projects tagged {', '.join(sorted(ts)) or 'all only'} this lesson does NOT fit (limits {LESSON_LINES_MAX} lines, {LESSON_CHARS_MAX} characters); "
                    "lessons above it take the room. Shorten or remove one.")
                break
        return 0
    if act == "remove":
        q = clean(arg, 100)
        if len(q) < 4 or q.isdigit():
            say("remove needs some of the lesson's own text (at least 4 characters), not a number: numbers move when the file changes.\n"
                "  example:  --lessons remove --arg 'every file the page loads'")
            return 2
        try:
            lines = read_text(path).splitlines()
        except OSError:
            lines = []
        hits = [(i, ln) for i, ln in enumerate(lines) if LESSON_RE.match(clean(ln, 400)) and q.lower() in clean(ln, 400).lower()]
        if not hits:
            say("no lesson contains that text")
            return 2
        if len(hits) > 1:
            say(f"{len(hits)} lessons contain that text; use more of it:")
            for _, ln in hits:
                say(f"  {ln}")
            return 2
        bak = backup_lessons()
        i, gone = hits[0]
        kept = lines[:i] + lines[i + 1:]
        write_atomic(path, "\n".join(kept) + ("\n" if kept else ""))
        say(f"removed: {gone}" + (f"   (previous file saved as {bak})" if bak else ""))
        return 0
    return 2


# ------------------------------------------------------------------------------------------------------------------ the plan
class Task:
    def __init__(self, line_no, raw, done, tid, goal, files, spec, check_text, cmd):
        self.line_no, self.raw, self.done, self.id, self.goal = line_no, raw, done, tid, goal
        self.files, self.spec, self.check_text, self.cmd = files, spec, check_text, cmd

    def __repr__(self):
        return f"<{self.id} {'x' if self.done else ' '} files={self.files} cmd={self.cmd!r}>"


def check_command(text):
    """The runnable command in a task's `check:` text, or None if it is only a description."""
    text = text.strip()
    m = re.search(r"`([^`]+)`", text)
    if m:
        return m.group(1).strip()
    t = re.sub(r"\s+(prints|passes|exits|must print|should print)\b.*$", "", text, flags=re.I).strip()
    return t if t and t.lower().startswith(RUNNERS) else None


def parse_plan(text):
    tasks = []
    for n, raw in enumerate(text.splitlines()):
        m = re.match(r"^- \[([ xX])\]\s*(.*)$", raw)
        if not m:
            continue
        body = m.group(2)
        cm = re.search(r"\|\s*check:\s*(.*)$", body)
        head, check_text = (body[:cm.start()], cm.group(1)) if cm else (body, "")
        parts = [p.strip() for p in head.split("|")]
        im = re.match(r"^(T\d+|Task\s+\d+)\s*[:.\-]?\s*(.*)$", parts[0], re.I)
        tid = re.sub(r"\s+", "", im.group(1)).upper().replace("TASK", "T") if im else f"L{n + 1}"
        goal = im.group(2) if im else parts[0]
        files, spec = [], ""
        for p in parts[1:]:
            if p.lower().startswith("files:"):
                files = [f.strip() for f in re.split(r"[,\s]+", p[6:].strip()) if f.strip()]
            elif p.lower().startswith("spec:"):
                spec = p[5:].strip()
        tasks.append(Task(n, raw, m.group(1) != " ", tid, goal, files, spec, check_text, check_command(check_text)))
    return tasks


def lint(tasks):
    """(errors, warnings): problems that make a task unsafe or unfalsifiable."""
    errors, warnings, seen = [], [], set()
    for t in tasks:
        if t.id in seen:
            errors.append(f"{t.id}: the id appears twice")
        seen.add(t.id)
        if t.done:
            continue
        if not t.cmd:
            errors.append(f"{t.id}: no runnable check (write  check: `command` prints OK ; a description is not a test)")
            continue
        if VACUOUS.search(t.cmd):
            errors.append(f"{t.id}: the check `{t.cmd}` cannot fail")
        if NETWORK_WORDS.search(t.cmd):
            errors.append(f"{t.id}: the check uses the network (`{t.cmd}`); the container has none")
        if not t.files:
            errors.append(f"{t.id}: no files: list; the loop lets a session change only the files its task names")
        elif len(t.files) > 3:
            warnings.append(f"{t.id}: {len(t.files)} files; a small model does better with one or two")
        refs = any(f.rstrip("/") in t.cmd for f in t.files) or any(w in t.cmd for w in ("unittest", "pytest", "browser-check", "tests/", "node "))
        if not refs:
            warnings.append(f"{t.id}: the check does not mention this task's files or a test runner; make sure it can actually fail")
        if len(t.goal) > 200:
            warnings.append(f"{t.id}: the goal is {len(t.goal)} characters; shorter tasks succeed more often")
    if not [t for t in tasks if not t.done] and tasks:
        warnings.append("every task is already ticked")
    return errors, warnings


HARNESS_NAMES = {"conftest.py", "sitecustomize.py", "usercustomize.py", "pytest.ini", "tox.ini", "setup.cfg", "pyproject.toml", "package.json",
                 "package-lock.json", "makefile", "agents.md", "spec.md", "plan.md", "progress.txt", ".gitignore"}
HARNESS_DIRS = {"tests", "test", "__tests__", "spec", "specs", "e2e"}


def harness_like(path):
    """Test, check and configuration material: something that decides whether the work passes. An agent must never be able to change it."""
    p = path.lower(); parts = p.split("/"); base = parts[-1]
    if base in HARNESS_NAMES or p.startswith(".") or any(d in HARNESS_DIRS for d in parts[:-1]):
        return True
    return bool(re.search(r"(^|[._-])(test|tests|spec|check|verify)([._-]|$)", base)) or base.startswith(("test_", "check", "verify"))


def check_pages(cmd):
    """The pages a `browser-check` opens. The page is the product; the real browser is the check, and it lives in the image, out of the agent's reach."""
    return re.findall(r"browser-check\s+((?!-)[^\s;&|]+)", cmd or "")


def allowed(path, task, new_at_start=False, allow_new=False):
    """May a session of `task` change `path`? new_at_start: the file did not exist when the task began."""
    if path in ("progress.txt", "PLAN.md") or any(n in path for n in NOISE):
        return True
    for f in task.files:
        f = f.strip().lstrip("./")
        if path == f or (f.endswith("/") and path.startswith(f)) or fnmatch.fnmatch(path, f):
            return True
    if path in [x.lstrip("./") for x in check_pages(task.cmd)]:
        return True
    return bool(allow_new and new_at_start and not harness_like(path))


# ------------------------------------------------------------------------------------------------------------------ git
class Repo:
    def __init__(self, d):
        self.d = d
        self.env = dict(os.environ, GIT_AUTHOR_NAME="loop", GIT_AUTHOR_EMAIL="loop@localhost", GIT_COMMITTER_NAME="loop", GIT_COMMITTER_EMAIL="loop@localhost")

    def git(self, *args, check=True):
        r = subprocess.run(["git", "-C", self.d, *args], capture_output=True, text=True, env=self.env)
        if check and r.returncode:
            raise RuntimeError(f"git {' '.join(args)}: {r.stderr.strip()[:300]}")
        return r.stdout

    def head(self):
        return self.git("rev-parse", "HEAD").strip()

    def clean(self):
        return not self.git("status", "--porcelain", "--untracked-files=normal").strip()

    def changes(self, base):
        """(tracked, untracked): changes of the working tree against base."""
        tracked = []
        for ln in self.git("diff", "--name-status", "--no-renames", base).splitlines():
            st, _, path = ln.partition("\t")
            tracked.append((st[:1], path))
        untracked = [p for p in self.git("ls-files", "--others", "--exclude-standard").splitlines() if p]
        return tracked, untracked

    def show(self, base, path):
        r = subprocess.run(["git", "-C", self.d, "show", f"{base}:{path}"], capture_output=True, env=self.env)
        return r.stdout if r.returncode == 0 else None


# ------------------------------------------------------------------------------------------------------------------ containers
def uid_gid():
    return f"{os.getuid()}:{os.getgid()}"


def base_flags(name):
    return ["--rm", "--name", name, "--network", "none", "--user", uid_gid(), "-e", "HOME=/home/agent",
            "-e", "GIT_AUTHOR_NAME=agent", "-e", "GIT_AUTHOR_EMAIL=agent@localhost", "-e", "GIT_COMMITTER_NAME=agent", "-e", "GIT_COMMITTER_EMAIL=agent@localhost",
            "--memory", "4g", "--memory-swap", "4g", "--cpus", "2", "--pids-limit", "512", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges", "--tmpfs", "/tmp:rw,exec,size=512m"]


def agent_command(proj, name, home, minutes):
    inner = (f"python3 ~/.codex/tools/unix-relay.py --listen {FRONT_PORT} --unix /run/strata-front.sock & "
             f"for i in $(seq 1 50); do (exec 3<>/dev/tcp/127.0.0.1/{FRONT_PORT}) 2>/dev/null && break; sleep 0.1; done; "
             f"exec timeout -k 20 {int(minutes * 60)} codex --no-daemon exec --skip-git-repo-check -s danger-full-access "
             f"-c 'approval_policy=\"never\"' -c 'model_provider=\"strata_front\"' \"$0\" < /dev/null")
    return (["docker", "run"] + base_flags("strata-agent-loop") +
            ["-v", f"{proj}:/work/{name}", "-v", f"{home}:/home/agent/.codex", "-v", f"{SOCKET}:/run/strata-front.sock",
             "-w", f"/work/{name}", IMAGE, "bash", "-c", inner, PROMPT])


def check_docker_command(proj, name, cmd, seconds):
    return (["docker", "run"] + base_flags(f"strata-check-{os.getpid()}") +
            ["-v", f"{proj}:/work/{name}", "-v", f"{AGENT}/codex-home:/home/agent/.codex:ro", "-w", f"/work/{name}", IMAGE,
             "timeout", "-k", "10", str(seconds), "bash", "-lc", cmd])


def run_check(proj, name, cmd, seconds=None):
    """(exit code, output, timed_out): the task's check, run by the loop itself in a fresh container with no network."""
    seconds = seconds or CHECK_SECONDS
    argv = ["bash", "-lc", cmd] if LOCAL else check_docker_command(proj, name, cmd, seconds)
    try:
        r = subprocess.run(argv, cwd=proj if LOCAL else None, capture_output=True, text=True, timeout=seconds + 30)
        return r.returncode, (r.stdout + r.stderr), r.returncode in (124, 137)
    except subprocess.TimeoutExpired as e:
        if not LOCAL:
            subprocess.run(["docker", "rm", "-f", f"strata-check-{os.getpid()}"], capture_output=True)
        return 124, ((e.stdout or b"").decode("utf8", "replace") if isinstance(e.stdout, bytes) else (e.stdout or "")), True


def make_home(logs, i, lessons=""):
    """A fresh private copy of the Codex home for one session: the agent cannot change the rules or tools the next session reads.
    `lessons` (a block of text) is appended to this copy's AGENTS.md only; the master file and the lessons file are never mounted."""
    home = os.path.join(logs, f"home-{i}")
    src = os.path.join(AGENT, "codex-home")
    os.makedirs(home, exist_ok=True)
    for item in ("config.toml", "AGENTS.md", "templates", "tools", "model-catalog.json"):
        s = os.path.join(src, item)
        if os.path.isdir(s):
            shutil.copytree(s, os.path.join(home, item), dirs_exist_ok=True)
        elif os.path.isfile(s):
            shutil.copy(s, os.path.join(home, item))
    if lessons:
        dst = os.path.join(home, "AGENTS.md")
        try:
            os.chmod(dst, 0o644)
            old = read_text(dst)
        except OSError:
            old = ""
        write_atomic(dst, (old.rstrip("\n") + "\n\n" if old.strip() else "") + lessons)
    tools = os.path.join(home, "tools")
    os.makedirs(tools, exist_ok=True)
    relay = os.path.join(AGENT, "bin", "unix-relay.py")
    if os.path.isfile(relay):
        shutil.copy(relay, os.path.join(tools, "unix-relay.py"))
    os.chmod(home, 0o777)                                    # the container runs as your uid, but be safe on odd mounts
    return home


def run_agent(proj, name, logs, i, task, minutes, attempt, lessons=""):
    """(exit code, timed_out): one fresh session. Output goes to the log folder."""
    log = os.path.join(logs, f"session-{i}-{task.id}.log")
    home = make_home(logs, i, lessons)
    env = dict(os.environ, TASK_ID=task.id, ATTEMPT=str(attempt), PROJECT_DIR=proj, SESSION=str(i), LOOP_HOME=home)
    if AGENT_CMD:
        argv, kw = ["bash", "-c", AGENT_CMD], dict(cwd=proj, env=env)
    else:
        subprocess.run(["docker", "rm", "-f", "strata-agent-loop"], capture_output=True)
        argv, kw = agent_command(proj, name, home, minutes), {}
    with open(log, "wb") as fh:
        try:
            r = subprocess.run(argv, stdout=fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, timeout=minutes * 60 + (0 if AGENT_CMD else 60), **kw)
            return r.returncode, r.returncode in (124, 137)
        except subprocess.TimeoutExpired:
            if not AGENT_CMD:
                subprocess.run(["docker", "rm", "-f", "strata-agent-loop"], capture_output=True)
            return 124, True


# ------------------------------------------------------------------------------------------------------------------ Strata
def strata_ok():
    """(ok, why): Strata answers and every expert is in the GPU cache (batched prompt reading is only safe then)."""
    if os.environ.get("CODEX_LOOP_SKIP_STRATA") == "1":
        return True, ""
    try:
        with urllib.request.urlopen(f"{STRATA_URL}/v1/models", timeout=5) as r:
            if r.status != 200:
                return False, f"Strata answered HTTP {r.status}"
    except Exception as e:  # noqa: BLE001
        return False, f"Strata is not answering ({e})"
    try:
        with open(STRATA_LOG, errors="replace") as fh:
            last = [ln for ln in fh if re.search(r"expert cache \d+ slots", ln)][-1]
        if f"{SLOTS} slots" not in last:
            return False, f"the GPU expert cache is not all-resident: {last.strip()[:100]}"
    except (OSError, IndexError):
        return False, "no expert-cache line in the Strata log"
    avail = 0
    with open("/proc/meminfo") as fh:
        for ln in fh:
            if ln.startswith("MemAvailable"):
                avail = int(ln.split()[1]) // 1048576
    if avail < int(os.environ.get("CODEX_LOOP_MIN_GIB", "3")):
        return False, f"only {avail} GiB of memory available"
    return True, ""


# ------------------------------------------------------------------------------------------------------------------ the loop
def tail(text, n=4):
    lines = [ln for ln in text.strip().splitlines() if ln.strip()]
    return " / ".join(ln.strip()[:110] for ln in lines[-n:]) if lines else "(no output)"


def read_text(path, mode="r"):
    with open(path, mode) as fh:
        return fh.read()


def write_plan_tick(proj, task):
    p = os.path.join(proj, "PLAN.md")
    lines = read_text(p).split("\n")
    for k, ln in enumerate(lines):
        if ln == task.raw or (ln.startswith("- [ ]") and parse_plan(ln)[0].id == task.id):
            lines[k] = ln.replace("- [ ]", "- [x]", 1)
            break
    with open(p, "w") as fh:
        fh.write("\n".join(lines))


def strip_wip(proj):
    """Drop the agent's own `WIP:` checkpoint lines from progress.txt once a task is verified: they only matter while the task is open."""
    p = os.path.join(proj, "progress.txt")
    if not os.path.isfile(p):
        return
    lines = read_text(p).split("\n")
    kept = [ln for ln in lines if not ln.startswith("WIP:")]
    if len(kept) != len(lines):
        with open(p, "w") as fh:
            fh.write("\n".join(kept).rstrip("\n") + "\n")


def note(proj, text):
    with open(os.path.join(proj, "progress.txt"), "a") as fh:
        fh.write(text.rstrip() + "\n")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("name", nargs="?")
    ap.add_argument("--lessons", choices=("list", "candidates", "add", "remove"), help="show or edit the global lessons (a person's job; the loop only records candidates)")
    ap.add_argument("--arg", default="", help="--lessons add: the lesson line; remove: some of the lesson's own text; candidates: `raw` for every record")
    ap.add_argument("--sessions", type=int, default=12, help="most sessions in this run (default 12)")
    ap.add_argument("--minutes", type=float, default=45, help="time limit of one session (default 45)")
    ap.add_argument("--attempts", type=int, default=3, help="attempts per task before the loop stops (default 3)")
    ap.add_argument("--hours", type=float, default=8, help="time limit of the whole run (default 8)")
    ap.add_argument("--dry-run", action="store_true", help="show the plan, the lint result and the commands; run nothing")
    ap.add_argument("--accept-prepassed", action="store_true", help="tick a task whose check already passes before any work, instead of stopping")
    ap.add_argument("--allow-new-files", action="store_true", help="a session may also ADD new files that are not test or check material (existing files still need to be listed in files:)")
    ap.add_argument("--clean-retry", action="store_true", help="reset the project after every failed attempt instead of letting the next session refine the code")
    ap.add_argument("--strict-files", action="store_true", help="(the default; accepted so older command lines keep working)")
    ap.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    a = ap.parse_args(argv)
    if a.lessons:
        return lessons_cli(a.lessons, a.arg)
    if not a.name:
        ap.error("the project name is required")

    proj = os.path.join(AGENT, "work", a.name)
    if not os.path.isdir(os.path.join(proj, ".git")) or not os.path.isfile(os.path.join(proj, "PLAN.md")):
        say(f"{proj} is not a planned project (no git repository or PLAN.md). Start it with:  codex {a.name}")
        return 2
    repo = Repo(proj)
    tasks = parse_plan(read_text(os.path.join(proj, "PLAN.md")))
    errors, warnings = lint(tasks)
    todo = [t for t in tasks if not t.done]
    say(f"Project {a.name}: {len(tasks) - len(todo)} of {len(tasks)} tasks ticked, {len(todo)} to do.")
    for t in todo:
        say(f"  {t.id:4s} {t.goal[:70]}\n       files: {', '.join(t.files) or '-'}   check: {t.cmd or '(none)'}")
    for w in warnings:
        say(f"  warning: {w}")
    for e in errors:
        say(f"  ERROR:   {e}")
    if errors:
        say("\nFix PLAN.md first (an interactive session can do it:  codex " + a.name + "), then run this again.")
        return 2
    if not todo:
        say("Nothing to do.")
        return 0
    tags = project_tags(tasks)
    lessons_text, picked, left_out = lessons_plan(tags)
    lid = lessons_id(lessons_text)
    say(f"Lessons in force: {len(picked)} line(s) for tags: {', '.join(sorted({'all'} | tags))}" + (f" (id {lid})" if lid else ""))
    if left_out:
        say(f"  warning: {len(left_out)} matching lesson(s) did NOT fit (limits {LESSON_LINES_MAX} lines, {LESSON_CHARS_MAX} characters) and are left out: {left_out[0][:70]}...  "
            "(see the room per project type: --lessons list)")
    if a.dry_run:
        for ln in picked:
            say(f"  {ln}")
        for ln in left_out:
            say(f"  LEFT OUT: {ln}")
        say("\nA session would run:\n  " + " ".join(agent_command(proj, a.name, "<fresh copy of codex-home>", a.minutes)[:-1]) + " <the fixed prompt>")
        say("A check would run:\n  " + " ".join(check_docker_command(proj, a.name, todo[0].cmd, CHECK_SECONDS)))
        say("\n(dry run: nothing was run)")
        return 0
    if not repo.clean():
        say("The project has uncommitted changes. The loop resets the tree after a failed attempt, so commit or stash first:\n"
            f"  cd {proj} && git status")
        return 2
    if not LOCAL and not AGENT_CMD:
        if not os.path.exists(SOCKET):
            say(f"The Strata filter's socket {SOCKET} is missing. Start it with:  codex loop {a.name}  (the codex command does that), or  codex status")
            return 2
    if not a.yes:
        say(f"\nThis runs unattended: up to {a.sessions} sessions of at most {a.minutes:g} minutes, in a container with no network.")
        if input("Start? [Y/n] ").strip().lower().startswith("n"):
            return 0

    logs = os.path.join(AGENT, "logs", f"loop-{a.name}-{datetime.datetime.now():%Y-%m-%d-%H%M%S}")
    os.makedirs(logs, exist_ok=True)
    stop_flag = {"stop": False}
    signal.signal(signal.SIGINT, lambda *_: stop_flag.update(stop=True))
    signal.signal(signal.SIGTERM, lambda *_: stop_flag.update(stop=True))
    t_start = time.time()
    events, sessions, attempts, idle, idle_reverted = [], 0, {}, 0, False
    fails, cand_n, run_sigs = {}, 0, set()    # per task: what each failed attempt looked like (for a lesson candidate); the problems this run recorded
    task_bases = {}                       # the commit each task started from (attempts that fail are kept on top of it)
    reason = ""

    def log_event(**kw):
        kw["time"] = now()
        events.append(kw)
        with open(os.path.join(logs, "loop.jsonl"), "a") as fh:
            fh.write(json.dumps(kw) + "\n")

    while True:
        tasks = parse_plan(read_text(os.path.join(proj, "PLAN.md")))
        todo = [t for t in tasks if not t.done]
        if not todo:
            reason = "all tasks ticked"
            break
        if stop_flag["stop"] or os.path.exists(os.path.join(proj, "STOP")):
            reason = "stopped by you (STOP file or Ctrl+C)"
            break
        if sessions >= a.sessions:
            reason = f"reached the session limit ({a.sessions})"
            break
        if time.time() - t_start > a.hours * 3600:
            reason = f"reached the time limit ({a.hours:g} hours)"
            break
        task = todo[0]
        n_try = attempts.get(task.id, 0) + 1
        if n_try > a.attempts:
            reason = f"{task.id} failed {a.attempts} times: blocked (the attempts are on git refs refs/loop/{task.id}-*)"
            break
        ok, why = strata_ok()
        waited = 0
        while not ok and waited < 300 and not stop_flag["stop"]:
            say(f"[{now()}] waiting: {why}")
            time.sleep(30)
            waited += 30
            ok, why = strata_ok()
        if not ok:
            reason = f"Strata unavailable: {why}"
            break

        base = repo.head()                                   # this attempt starts here (after a failure: on top of the kept work in progress)
        task_base = task_bases.setdefault(task.id, base)     # the task started here
        say(f"\n[{now()}] session {sessions + 1}: {task.id} (attempt {n_try} of {a.attempts}): {task.goal[:70]}")

        if n_try == 1:   # the check must fail before the work, or it cannot tell done from not done
            rc0, out0, _ = run_check(proj, a.name, task.cmd)
            if rc0 == 0:
                if a.accept_prepassed:
                    write_plan_tick(proj, task)
                    note(proj, f"{task.id} already satisfied: its check passed before any work (accepted by you)")
                    repo.git("add", "-A")
                    repo.git("commit", "-qm", f"{task.id}: already satisfied (check passed before the work)")
                    log_event(task=task.id, result="prepassed-accepted")
                    say(f"[{now()}] {task.id}: the check passes already; ticked (--accept-prepassed)")
                    task_bases.pop(task.id, None)
                    continue
                reason = (f"{task.id}: its check `{task.cmd}` ALREADY PASSES before any work. Either the task is done, or the check cannot tell. "
                          f"Look at it; then edit PLAN.md, or rerun with --accept-prepassed")
                break

        t0 = time.time()
        rc, timed_out = run_agent(proj, a.name, logs, sessions + 1, task, a.minutes, n_try, lessons_text)
        sessions += 1
        attempts[task.id] = n_try
        dur = time.time() - t0

        # 1. scope: the session may change only its task's files (and add new non-harness files with --allow-new-files).
        #    Everything else is reverted and kept in the log folder.
        tracked, untracked = repo.changes(base)
        violations, qdir = [], os.path.join(logs, f"quarantine-{sessions}")
        for st, path in tracked:
            if not allowed(path, task, repo.show(task_base, path) is None, a.allow_new_files):
                violations.append(path)
                if os.path.isfile(os.path.join(proj, path)):
                    os.makedirs(os.path.dirname(os.path.join(qdir, path)), exist_ok=True)
                    shutil.copy(os.path.join(proj, path), os.path.join(qdir, path))
                if st == "A":
                    repo.git("rm", "-f", "-q", "--", path, check=False)
                else:
                    repo.git("checkout", base, "--", path, check=False)
        for path in untracked:
            if not allowed(path, task, True, a.allow_new_files):
                violations.append(path)
                os.makedirs(os.path.dirname(os.path.join(qdir, path)), exist_ok=True)
                shutil.move(os.path.join(proj, path), os.path.join(qdir, path))
        # 2. the plan is the loop's: restore it whatever the session did to it
        plan_before = repo.show(base, "PLAN.md")
        plan_changed = plan_before is not None and read_text(os.path.join(proj, "PLAN.md"), "rb") != plan_before
        if plan_changed:
            repo.git("checkout", base, "--", "PLAN.md", check=False)
            violations.append("PLAN.md")
        repo.git("reset", "-q", "--soft", base, check=False)       # an agent commit becomes part of the loop's commit
        tracked, untracked = repo.changes(base)
        meta = ("progress.txt", "PLAN.md")
        changed = [p for _, p in tracked if p not in meta] + [p for p in untracked if p not in meta]       # what THIS attempt did
        t_tracked, t_untracked = repo.changes(task_base)
        task_changes = [p for _, p in t_tracked if p not in meta] + [p for p in t_untracked if p not in meta]   # what the task has so far, kept work included
        # 3. the check, run by the loop
        rc1, out1, to1 = run_check(proj, a.name, task.cmd)
        passed = rc1 == 0 and bool(task_changes)
        if rc1 == 0 and not task_changes:
            out1 += "\n(the check passes but the task has changed nothing)"
        say(f"[{now()}] session {sessions} ended after {dur / 60:.1f} min{' (TIME LIMIT)' if timed_out else ''}: "
            f"{len(changed)} file(s) changed{', ' + str(len(violations)) + ' reverted (outside the task)' if violations else ''}; "
            f"check {'PASSED' if passed else 'FAILED'}: {tail(out1, 2)}")
        if violations:
            say(f"           reverted: {', '.join(violations[:6])}   (kept in {qdir})")

        if passed:
            write_plan_tick(proj, task)
            strip_wip(proj)
            note(proj, f"{task.id} done (attempt {n_try}, {dur / 60:.0f} min, verified by the loop): {task.goal[:80]}")
            repo.git("reset", "-q", "--soft", task_base)               # squash the work-in-progress commits of earlier attempts into one
            repo.git("add", "-A")
            repo.git("commit", "-qm", f"{task.id}: {task.goal[:70]} (verified by loop)")
            log_event(task=task.id, attempt=n_try, result="passed", minutes=round(dur / 60, 1), reverted=violations)
            hist = fails.pop(task.id, [])
            if hist or violations:
                rec = record_candidate(a.name, task, tags, lid, "fixed-on-retry" if hist else "passed-with-reverts", n_try, hist, changed, violations)
                if rec:
                    cand_n += 1
                    run_sigs.add(candidate_signature(rec))
            task_bases.pop(task.id, None)
            idle = 0
        else:
            ref = f"refs/loop/{task.id}-{n_try}"
            if changed:
                repo.git("add", "-A")
                tree = repo.git("write-tree").strip()
                commit = repo.git("commit-tree", tree, "-p", base, "-m", f"failed attempt {n_try} of {task.id}").strip()
                repo.git("update-ref", ref, commit)
            vnote = (f"{task.id} attempt {n_try}: these changes were reverted because they are outside this task's files: {', '.join(violations[:6])}"
                     if violations else "")
            final = n_try >= a.attempts
            fails.setdefault(task.id, []).append({"attempt": n_try, "check": clean(tail(out1, 3), 300), "timed_out": bool(timed_out),
                                                  "reverted": [clean(v, 80) for v in violations[:6]]})
            if final:
                rec = record_candidate(a.name, task, tags, lid, "blocked", n_try, fails.pop(task.id), changed, violations)
                if rec:
                    cand_n += 1
                    run_sigs.add(candidate_signature(rec))
            if final or a.clean_retry:
                repo.git("reset", "-q", "--hard", task_base)            # back to where the task started; every attempt is on refs/loop/
                repo.git("clean", "-fdq")
                if vnote:                                               # written AFTER the reset, which would discard it
                    note(proj, vnote)
                note(proj, f"{task.id} attempt {n_try} FAILED: {tail(out1, 3)}" + (" (session timed out)" if timed_out else "") +
                     (f" (edits kept on {ref})" if changed else " (no changes made)"))
                repo.git("add", "-A")
                repo.git("commit", "-qm", f"loop: note on {task.id} attempt {n_try}")
                task_bases.pop(task.id, None)
                if not final:
                    say("           reset for a clean retry (--clean-retry)")
            else:
                say(f"           refinement: the code stays on disk for attempt {n_try + 1}")
                if vnote:
                    note(proj, vnote)
                note(proj, f"{task.id} attempt {n_try} FAILED: {tail(out1, 3)}" + (" (session timed out)" if timed_out else "") +
                     " (your code from this attempt is still on disk: fix it, do not start over)")
                repo.git("add", "-A")
                repo.git("commit", "-qm", f"loop: {task.id} attempt {n_try} failed (kept for refinement)")
            idle = idle + 1 if not changed else 0
            idle_reverted = bool(violations)
            log_event(task=task.id, attempt=n_try, result="failed", minutes=round(dur / 60, 1), reverted=violations, check=tail(out1, 3), timed_out=timed_out)
            if idle >= 2:
                reason = ("two sessions in a row made no change that counts: everything they touched was outside the task and was reverted"
                          if idle_reverted else "two sessions in a row changed nothing")
                break

    # ------------------------------------------------------------------ report
    tasks = parse_plan(read_text(os.path.join(proj, "PLAN.md")))
    done = [t for t in tasks if t.done]
    lines = [f"# Loop report: {a.name}", "", f"Finished {datetime.datetime.now():%Y-%m-%d %H:%M}. Reason: **{reason}**.",
             f"{len(done)} of {len(tasks)} tasks ticked; {sessions} session(s); {(time.time() - t_start) / 60:.0f} minutes.",
             f"Lessons in force: {len(picked)} line(s)" + (f", id {lid}" if lid else "") + f"; left out (did not fit): {len(left_out)}. Lesson candidates recorded: {cand_n}.", "",
             "| Time | Task | Attempt | Result | Minutes | Reverted (outside the task) |", "|---|---|---|---|---|---|"]
    for e in events:
        lines.append(f"| {e['time']} | {e['task']} | {e.get('attempt', '')} | {e['result']} | {e.get('minutes', '')} | {', '.join(e.get('reverted') or []) or '-'} |")
    lines += ["", "Review with:", f"  cd {proj} && git log --oneline && cat progress.txt",
              f"  codex check {a.name}        (runs the project's check again in a fresh container, no network)",
              f"Session logs and anything reverted: {logs}"]
    with open(os.path.join(logs, "loop-report.md"), "w") as fh:
        fh.write("\n".join(lines) + "\n")
    say(f"\nLoop finished: {reason}.")
    say(f"{len(done)} of {len(tasks)} tasks ticked in {sessions} session(s). Report: {os.path.join(logs, 'loop-report.md')}")
    if cand_n:
        repeats = [g for g in candidate_groups(load_candidates()) if g["count"] >= 2 and g["sig"] in run_sigs]
        say(f"{cand_n} lesson candidate(s) recorded" + (f"; {len(repeats)} of them are problems seen 2 or more times (worth a lesson)" if repeats else "") +
            f". Read them with:  python3 {os.path.abspath(__file__)} --lessons candidates")
    return 0 if len(done) == len(tasks) else 1


if __name__ == "__main__":
    sys.exit(main())
