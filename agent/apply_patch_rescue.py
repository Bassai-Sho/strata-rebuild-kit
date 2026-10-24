#!/usr/bin/env python3
"""apply_patch_rescue.py: turn an `apply_patch` call that the model wrote as TEXT into a real tool call, on its way back to Codex.

The problem (seen 2026-10-23, Strata 0.2.3 and Codex 0.164.0): the slim instructions say "Edit with small patches using apply_patch", and
now and then the model writes it as if it were a tool:

    <tool_call><function=apply_patch><parameter=cmd>apply_patch</parameter></function></tool_call>

The only tools are exec_command, write_stdin (and request_user_input, web_search), so Strata does not read that as a call: it returns it as
text (0.2.x says so in its log: "did not read as a call of a declared tool ... the names the model wrote: apply_patch"). Codex then sees an
assistant message with no tool call, ends the turn, and waits for you to type `continue`.

What this does, in the response only (the filter, strata-front.py, calls it; Strata and Codex are not touched):
- If the answer ENDS in such a block, and the same answer holds no real tool call, a real exec_command call is added before the response ends.
- If the block carries a patch (a parameter holding `*** Begin Patch ...`), the call runs it: `apply_patch <<'PATCH' ... PATCH` (an
  `*** End Patch` line is added when missing). If the model put the whole command in the parameter, that command is used as written.
- If the block carries no patch (the case above), the call prints the one thing the model needs to know and exits with 2, so the NEXT request
  shows the model how to call it, and the turn goes on instead of stopping.
Measured with the real Codex 0.164.0 and a scripted model: a function call for a name Codex does not know comes back as "unsupported call:
apply_patch" and the turn goes on; a bare `apply_patch` in exec_command comes back as "command not found" (exit 127); the heredoc form works.

Anything else passes through byte for byte. Off switch: STRATA_FRONT_APPLY_PATCH=0, or an empty file NO-APPLY-PATCH-RESCUE in ~/agent/work.
"""
import json, re, shlex, uuid

GUIDE = ("apply_patch is not a tool here. It is a shell command: call exec_command with cmd set to the whole command, for example:\n"
         "apply_patch <<'PATCH'\n"
         "*** Begin Patch\n"
         "*** Update File: path/to/file\n"
         "@@\n"
         "-old line\n"
         "+new line\n"
         "*** End Patch\n"
         "PATCH")

_OPEN = re.compile(r"(?:<tool_call>\s*)?<function=apply_patch>", re.I)
_PARAM = re.compile(r"<parameter=([A-Za-z_][A-Za-z0-9_]*)>\s*(.*?)\s*(?=</parameter>|</function>|</tool_call>|\Z)", re.S)
_AFTER = re.compile(r"\s*(?:</parameter>\s*)*(?:</function>\s*)?(?:</tool_call>\s*)?\Z")


def find_call(text):
    """The parameters {name: value} of an apply_patch call that ends `text`, or None (not there, or more text follows it)."""
    last = None
    for m in _OPEN.finditer(text):
        last = m
    if last is None:
        return None
    body = text[last.end():]
    if "<tool_call>" in body:                           # a later call: that one is the end of the answer, not this
        return None
    params = {k: v for k, v in _PARAM.findall(body)}
    tail = _PARAM.sub("", body)
    # whatever is left after the parameters must be only closers: prose after the block means it was a quotation
    if not _AFTER.match(re.sub(r"</?parameter[^>]*>", "", tail)):
        return None
    return params


def command_for(params):
    """The shell command a rescued call runs."""
    for value in params.values():
        if "*** Begin Patch" in value:
            if value.lstrip().startswith("apply_patch") and "PATCH" in value.split("*** Begin Patch", 1)[0]:
                return value.strip()                    # the whole command, written out by the model
            patch = value[value.index("*** Begin Patch"):].rstrip()
            if "*** End Patch" not in patch:
                patch += "\n*** End Patch"
            return "apply_patch <<'PATCH'\n" + patch + "\nPATCH"
    return "printf '%s\\n' " + shlex.quote(GUIDE) + "; exit 2"


def _item(cmd):
    tag = uuid.uuid4().hex[:8]
    return {"type": "function_call", "id": f"fc_rescue_{tag}", "call_id": f"call_rescue_{tag}", "name": "exec_command",
            "arguments": json.dumps({"cmd": cmd})}


class Stream:
    """Feed it the response's bytes as they arrive; it returns the bytes to pass on. Only `response.completed` is held back (to put the call before it)."""

    def __init__(self):
        self.buf = b""
        self.text = ""              # the last assistant message of this response
        self.called = False         # a real tool call is already in this response
        self.rescued = 0

    def _event(self, raw):
        name, data = "", None
        for line in raw.decode("utf8", "replace").split("\n"):
            if line.startswith("event:"):
                name = line[6:].strip()
            elif line.startswith("data:"):
                try:
                    data = json.loads(line[5:].strip())
                except ValueError:
                    data = None
        if isinstance(data, dict) and not name:
            name = data.get("type", "")
        return name, data

    def _handle(self, raw):
        name, data = self._event(raw)
        out = raw + b"\n\n"
        try:
            if name == "response.created":
                self.text, self.called = "", False
            elif name == "response.output_item.done" and isinstance(data, dict):
                item = data.get("item") or {}
                if item.get("type") in ("function_call", "custom_tool_call", "local_shell_call"):
                    self.called = True
                elif item.get("type") == "message" and item.get("role") == "assistant":
                    self.text = "".join(c.get("text", "") for c in item.get("content", []) if isinstance(c, dict))
            elif name == "response.completed" and not self.called and isinstance(data, dict):
                params = find_call(self.text)
                if params is not None:
                    item = _item(command_for(params))
                    ev = {"type": "response.output_item.done", "item": item}
                    resp = data.get("response")
                    if isinstance(resp, dict) and isinstance(resp.get("output"), list):
                        resp["output"].append(item)
                        out = f"event: response.completed\ndata: {json.dumps(data)}\n\n".encode()
                    self.rescued += 1
                    self.called = True
                    return f"event: response.output_item.done\ndata: {json.dumps(ev)}\n\n".encode() + out
        except Exception:  # noqa: BLE001 - never let the repair break a response: pass it on as it came
            return raw + b"\n\n"
        return out

    def feed(self, chunk):
        self.buf += chunk
        out = b""
        while b"\n\n" in self.buf:
            raw, self.buf = self.buf.split(b"\n\n", 1)
            out += self._handle(raw)
        return out

    def finish(self):
        rest, self.buf = self.buf, b""
        return rest
