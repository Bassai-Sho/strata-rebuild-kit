#!/usr/bin/env python3
"""strata-front.py: a tiny local filter between Codex's interactive screen and Strata.

Why: in interactive mode Codex sends a SECOND request to the model for every prompt you type, only to invent a short title for the
session ("Generate a concise, single-line task title ..."). Strata has one conversation slot and reads at about 42 tokens a second:
that extra request costs a minute or more, and it pushes your real conversation out of Strata's cache. This filter answers that one
request itself, instantly (a title made from your first words), and passes every other request through to Strata unchanged, streaming.

    python3 strata-front.py [--listen 8086] [--upstream 127.0.0.1:8085] [--unix PATH]

--unix PATH also serves the same filter on a Unix socket. `codex loop` mounts that one socket into a container that has NO network at all
(`--network none`): inside, a tiny relay (unix-relay.py) turns it back into 127.0.0.1:8086, so the agent can reach Strata and nothing else:
not the internet, not your LAN, not Sunshine or Dropbox on this machine.

Nothing is stored or logged; it only looks at the request's shape. The `codex` command starts it when needed.
"""
import argparse, http.client, http.server, json, re, socketserver, sys

TITLE_MARK = "Generate a concise, single-line task title"


def is_title_request(body):
    """The text of the user's prompt if this is Codex's session-title request, else None.

    Codex says what a request is in client_metadata["x-codex-turn-metadata"] (a JSON string): thread_source "thread_title" is the title turn. That is the
    signal Strata's own opt-in uses and it does not depend on the wording of the prompt. The older shape (a title schema and the instruction text) is
    kept as a second way to recognise it, for Codex versions that do not send the metadata."""
    try:
        j = json.loads(body)
        meta = (j.get("client_metadata") or {}).get("x-codex-turn-metadata")
        by_meta = False
        if isinstance(meta, str):
            try:
                by_meta = json.loads(meta).get("thread_source") == "thread_title"
            except ValueError:
                pass
        last = j["input"][-1]["content"][0]["text"]
        if not by_meta:
            schema = (((j.get("text") or {}).get("format") or {}).get("schema") or {})
            if "title" not in (schema.get("properties") or {}) or not last.startswith(TITLE_MARK):
                return None
        m = re.search(r"User prompt:\s*\n(.*)\Z", last, re.S)
        return (m.group(1) if m else (last if by_meta else "")).strip()
    except Exception:  # noqa: BLE001 - anything unexpected: treat as an ordinary request and forward it
        return None


def make_title(prompt):
    words = re.sub(r"[`\"'*#_>\[\]]", "", (prompt.splitlines() or [""])[0]).split()
    title = " ".join(words[:5])[:36].rstrip(" ,.;:-")
    return (title[:1].upper() + title[1:]) if title else "New task"


class Front(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    upstream = ("127.0.0.1", 8085)

    def log_message(self, *a):
        pass

    def _sse(self, events):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        for name, data in events:
            self.wfile.write(f"event: {name}\ndata: {json.dumps(data)}\n\n".encode())
        self.wfile.flush()
        self.close_connection = True

    def _title(self, prompt):
        text = json.dumps({"title": make_title(prompt)})
        self._sse([("response.created", {"type": "response.created", "response": {"id": "resp_title"}}),
                   ("response.output_item.done", {"type": "response.output_item.done", "item": {"type": "message", "role": "assistant", "id": "msg_title",
                                                  "content": [{"type": "output_text", "text": text}]}}),
                   ("response.completed", {"type": "response.completed", "response": {"id": "resp_title", "usage": {
                       "input_tokens": 0, "input_tokens_details": None, "output_tokens": 0, "output_tokens_details": None, "total_tokens": 0}}})])

    def _forward(self, body):
        try:
            conn = http.client.HTTPConnection(*self.upstream, timeout=None)       # no timeout: Strata may read a long prompt before the first byte
            hdrs = {k: v for k, v in self.headers.items() if k.lower() not in ("host", "content-length", "connection", "transfer-encoding")}
            if body:
                hdrs["Content-Length"] = str(len(body))
            conn.request(self.command, self.path, body=body or None, headers=hdrs)
            resp = conn.getresponse()
        except Exception as e:  # noqa: BLE001
            msg = json.dumps({"error": {"message": f"strata-front: cannot reach Strata at {self.upstream[0]}:{self.upstream[1]}: {e}"}}).encode()
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(msg)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(msg)
            self.close_connection = True
            return
        self.send_response(resp.status)
        for k, v in resp.getheaders():
            if k.lower() not in ("transfer-encoding", "connection", "content-length"):
                self.send_header(k, v)
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            while True:
                chunk = resp.read1(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass                                                                    # Codex went away (Esc, or closed): drop the upstream request too
        finally:
            conn.close()
            self.close_connection = True

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        prompt = is_title_request(body)
        if prompt is not None:
            return self._title(prompt)
        self._forward(body)

    def do_GET(self):
        self._forward(b"")

    do_PUT = do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = do_GET


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class UnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True

    def get_request(self):                          # an AF_UNIX peer has no (host, port); the handler only needs something indexable
        request, _ = super().get_request()
        return request, ("unix", 0)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--listen", type=int, default=8086)
    ap.add_argument("--upstream", default="127.0.0.1:8085")
    ap.add_argument("--unix", default="", help="also listen on this Unix socket (mode 600)")
    a = ap.parse_args()
    host, _, port = a.upstream.partition(":")
    Front.upstream = (host, int(port or 8085))
    if a.unix:
        import os, threading
        os.makedirs(os.path.dirname(os.path.abspath(a.unix)), exist_ok=True)
        if os.path.exists(a.unix):
            os.unlink(a.unix)
        us = UnixServer(a.unix, Front)
        os.chmod(a.unix, 0o600)
        threading.Thread(target=us.serve_forever, daemon=True).start()
    Server(("127.0.0.1", a.listen), Front).serve_forever()


if __name__ == "__main__":
    sys.exit(main())
