#!/usr/bin/env python3
"""synth_compact.py: answer Codex's "compaction" request at once, from what is already on disk and in the request itself.

Why: when Codex's context passes model_auto_compact_token_limit it sends the WHOLE history back to the model with a request to summarise it.
Strata reads that from token 0 (it never shares the cache with a normal request): 47-51K tokens, about 18 minutes, every time (measured: 4 of 4).
The facts the next window needs are already here: the open task in PLAN.md, the model's own checkpoint lines (WIP:) in progress.txt, the files it
patched, its last message and the end of its last chain of thought. This module writes them into a short handoff; strata-front.py sends it back as
the model's answer, so the "compaction" costs nothing. Codex keeps the user's own messages by itself.

Used by strata-front.py:  is_compaction_request(raw)  and  build_handoff(raw, work_root)  ->  text or None  (None = forward to Strata as usual).
Markers: config.toml's compact_prompt must start with MARK.  Off switch: an empty file named NO-SYNTH-COMPACT in the work root, or env STRATA_FRONT_SYNTH=0.

Safety (this filter is the only way out of the loop's no-network container, so it must not become a way to read the host):
  * the project folder comes only from the <cwd> of the first message(s) Codex itself wrote, before any model output, and must be a direct child of work_root;
  * only PLAN.md and progress.txt are read, never through a symlink, from inside that folder, at most 64 KB;
  * no git, no shell, nothing is executed; everything in the handoff was written by the agent or by Codex; the result is capped at MAX_TEXT characters.
"""
import base64, json, os, re

MARK = "STRATA-HANDOFF"
MAX_TEXT = 6000
CWD_RE = re.compile(r"<cwd>\s*(/work/[A-Za-z0-9._-]+)\s*</cwd>")
PATCH_RE = re.compile(r"\*\*\* (?:Update|Add|Delete) File: ([^\s\\\"]+)")


def _text(item):
    out = []
    for c in (item.get("content") or []):
        if isinstance(c, dict) and isinstance(c.get("text"), str):
            out.append(c["text"])
    return "\n".join(out)


def is_compaction_request(raw):
    try:
        j = json.loads(raw)
        last = j["input"][-1]
        return last.get("role") == "user" and MARK in _text(last)[:300]
    except Exception:  # noqa: BLE001 - anything unexpected: an ordinary request
        return False


def project_dir(items, work_root):
    """The host folder of the project, from Codex's own opening messages only (never from tool output the model could have forged)."""
    root = os.path.realpath(work_root)
    for it in items:
        if it.get("type") in ("reasoning", "function_call", "function_call_output") or it.get("role") == "assistant":
            break                                           # first model-made item: everything after it is not trusted for this
        m = CWD_RE.search(_text(it)) if it.get("role") == "user" else None
        if m:
            name = os.path.basename(m.group(1))
            if name in ("", ".", "..") or name.startswith("."):
                return None
            proj = os.path.realpath(os.path.join(root, name))
            return proj if os.path.dirname(proj) == root and os.path.isdir(proj) else None
    return None


def read_file(proj, name, limit=65536, tail=False):
    p = os.path.join(proj, name)
    try:
        if os.path.islink(p) or not os.path.isfile(p) or os.path.dirname(os.path.realpath(p)) != proj:
            return ""
        size = os.path.getsize(p)
        with open(p, "rb") as fh:
            if tail and size > limit:
                fh.seek(size - limit)
            return fh.read(limit).decode("utf-8", "replace")
    except OSError:
        return ""


def _reasoning_text(it):
    t = "".join(c.get("text", "") for c in (it.get("content") or []) if isinstance(c, dict))
    if t.strip():
        return t
    enc = it.get("encrypted_content") or ""
    if enc.startswith("strata.r1:"):                        # Strata hands the thinking back to Codex as base64 JSON
        try:
            return json.loads(base64.b64decode(enc[10:]).decode("utf-8", "replace")).get("text", "")
        except Exception:  # noqa: BLE001
            return ""
    return ""


def _cut(s, n):
    s = " ".join(str(s).split()) if "\n" not in str(s) else str(s).strip()
    return s if len(s) <= n else s[:n].rstrip() + " ..."


def patch_changes(cmd):
    """['index.html (+34 -2) @@ function setupGL', ...] from one apply_patch command: which file, how many lines, near what."""
    out, cur = [], None
    for ln in cmd.splitlines():
        m = re.match(r"\*\*\* (Update|Add|Delete) File: (\S+)", ln)
        if m:
            cur = {"f": m.group(2), "kind": m.group(1), "add": 0, "del": 0, "ctx": "", "first": ""}
            out.append(cur)
        elif cur is not None:
            if ln.startswith("@@") and not cur["ctx"]:
                cur["ctx"] = ln[2:].strip()
            elif ln.startswith("+") and not ln.startswith("+++"):
                cur["add"] += 1
                if not cur["first"]:
                    cur["first"] = ln[1:].strip()
            elif ln.startswith("-") and not ln.startswith("---"):
                cur["del"] += 1
    return [f"{c['f']} ({c['kind'].lower()} +{c['add']} -{c['del']})" + (f" near: {_cut(c['ctx'], 70)}" if c["ctx"] else f" starting: {_cut(c['first'], 70)}" if c["first"] else "") for c in out]


def history_facts(items):
    last_msg, last_think, files, cmds, changes = "", "", [], [], []
    for it in items:
        t, role = it.get("type"), it.get("role")
        if t == "message" and role == "assistant":
            last_msg = _text(it)
        elif t == "reasoning":
            last_think = _reasoning_text(it) or last_think
        elif t == "function_call":
            args = it.get("arguments") or ""
            for f in PATCH_RE.findall(args if isinstance(args, str) else json.dumps(args)):
                if f in files:
                    files.remove(f)
                files.append(f)
            try:
                c = json.loads(args).get("cmd", "")
            except Exception:  # noqa: BLE001
                c = ""
            if c and "apply_patch" in c:
                changes += patch_changes(c)
            elif c:
                cmds.append(" ".join(c.split()))
    return last_msg, last_think, files[-12:], cmds[-3:], changes[-15:]


def build_handoff(raw, work_root):
    if not is_compaction_request(raw):
        return None
    try:
        items = json.loads(raw)["input"][:-1]
    except Exception:  # noqa: BLE001
        return None
    proj = project_dir(items, work_root)
    plan = read_file(proj, "PLAN.md") if proj else ""
    prog = read_file(proj, "progress.txt", 16384, tail=True) if proj else ""
    last_msg, think, files, cmds, changes = history_facts(items)
    tasks = [_cut(ln, 700) for ln in plan.splitlines() if ln.startswith("- [ ]")][:1]
    plines = [ln.rstrip() for ln in prog.splitlines() if ln.strip()]
    wip = [_cut(ln, 400) for ln in plines if ln.startswith("WIP:")][-6:]
    other = [_cut(ln, 200) for ln in plines if not ln.startswith("WIP:")][-1:]
    planned = ""
    for ln in [x for x in plines if x.startswith("WIP:")]:                  # the last PLAN with no DONE after it
        if ln.startswith("WIP: PLAN"):
            planned = ln
        elif ln.startswith("WIP: DONE"):
            planned = ""
    if not (tasks or wip or changes or files or last_msg or think):
        return None                                         # nothing useful to say: let the model summarise as before
    parts = ["Automatic handoff. Your earlier context was dropped to save time. Your files and notes on disk are intact. Do not re-plan from scratch."]
    if tasks:
        parts.append("OPEN TASK in PLAN.md:\n" + "\n".join(tasks))
    if wip or other:
        parts.append("NOTES in progress.txt (newest last):\n" + "\n".join(other + wip))
    if planned:
        parts.append("LAST PLANNED CHANGE (not marked done; grep for it before you redo it):\n" + _cut(planned, 400))
    if changes:
        parts.append("PATCHES YOU APPLIED (oldest first):\n" + "\n".join("- " + c for c in changes))
    elif files:
        parts.append("FILES YOU PATCHED THIS SESSION: " + ", ".join(files))
    if cmds:
        parts.append("YOUR LAST COMMANDS:\n" + "\n".join("- " + _cut(c, 160) for c in cmds))
    if last_msg.strip():
        parts.append("YOUR LAST MESSAGE: " + _cut(last_msg, 500))
    tail = ("NEXT: everything you need from PLAN.md, progress.txt and AGENTS.md is above, so do not read them again and do not run git status or git log. "
            "Go straight to the code for the last planned change (or the open task): grep -n the function names, sed -n about 50 lines around them, then patch. "
            "Read only what you need.")
    body = "\n\n".join(parts)
    room = MAX_TEXT - len(body) - len(tail) - 60
    if think.strip() and room > 200:
        t = think.strip()
        if len(t) > min(room, 1000):
            t = t[-min(room, 1000):]
            t = "... " + t[t.find("\n") + 1:] if "\n" in t[:200] else "... " + t
        body += "\n\nTHE END OF YOUR LAST THINKING (your most recent plan):\n" + t
    return (body + "\n\n" + tail)[:MAX_TEXT]


def compaction_events(text):
    """The same three events the title answer uses; Codex takes the message as the summary."""
    return [("response.created", {"type": "response.created", "response": {"id": "resp_handoff"}}),
            ("response.output_item.done", {"type": "response.output_item.done", "item": {"type": "message", "role": "assistant", "id": "msg_handoff",
                                           "content": [{"type": "output_text", "text": text}]}}),
            ("response.completed", {"type": "response.completed", "response": {"id": "resp_handoff", "usage": {
                "input_tokens": 0, "input_tokens_details": None, "output_tokens": 0, "output_tokens_details": None, "total_tokens": 0}}})]
