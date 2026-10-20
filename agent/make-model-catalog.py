#!/usr/bin/env python3
"""make-model-catalog.py: write the model catalog that tells Codex what the `strata` model can do.

    python3 make-model-catalog.py --out ~/agent/codex-home/model-catalog.json [--stock [--codex-image codex-sandbox | --codex /path/to/codex]]

Why: Codex only enables its reasoning shortcuts (Alt+. and Alt+,) for a model whose levels it knows, and `strata` is not in its built-in
list ("Reasoning shortcuts are unavailable for strata"). `model_catalog_json` in config.toml points Codex at a file like this one.

What it contains: one model, `strata`, with
  * reasoning levels none, low, medium, high: exactly the levels Strata's server accepts (none = thinking off; high = its heaviest, which
    its thinking budget still caps), and `low` as the default;
  * the same tools Codex offers today (tool_mode direct: without it Codex hides every function tool behind a JavaScript sandbox);
  * the kit's SLIM base instructions (`slim-instructions.md`, next to this script, about 300 tokens). Codex's own are about 4,200 tokens that a local model reads
    at 42 tok/s on every cold start and that describe tools this setup does not have (planning, 17 pages of patch syntax, formatting rules). With `--stock` the
    catalog instead carries Codex's own base instructions, copied from the Codex you point it at, so it is tied to that version.
`tools/codex-update.py` runs this for you and only replaces the catalog when the result differs; the slim catalog does not depend on the Codex version.
"""
import argparse, json, os, subprocess, sys, tempfile

ENTRY = {
    "additional_speed_tiers": [],
    "apply_patch_tool_type": None,
    "auto_compact_token_limit": 48000,
    "auto_review_model_override": None,
    "availability_nux": None,
    "available_in_plans": [],
    "comp_hash": "3000",
    "context_window": 65536,
    "default_reasoning_level": "low",
    "default_reasoning_summary": "none",
    "default_service_tier": None,
    "default_verbosity": "low",
    "description": "The local Strata model on this machine",
    "display_name": "Strata (local)",
    "experimental_supported_tools": [],
    "guardian": None,
    "include_apps_usage_instructions": False,
    "include_plugin_usage_instructions": False,
    "include_skills_usage_instructions": False,
    "input_modalities": [
        "text"
    ],
    "max_context_window": 65536,
    "minimal_client_version": "0.153.0",
    "model_messages": None,
    "model_specialty": None,
    "multi_agent_reasoning_effort": None,
    "multi_agent_version": None,
    "node_repl_auto_review_required": True,
    "node_repl_disabled": False,
    "prefer_websockets": False,
    "priority": 2,
    "requires_sandboxed_review": False,
    "service_tiers": [],
    "shell_type": "shell_command",
    "slug": "strata",
    "support_verbosity": False,
    "supported_in_api": True,
    "supported_reasoning_levels": [
        {
            "description": "Thinking off: fastest, for simple steps",
            "effort": "none"
        },
        {
            "description": "Brief thinking (the default here)",
            "effort": "low"
        },
        {
            "description": "Moderate thinking",
            "effort": "medium"
        },
        {
            "description": "Deep thinking, capped by Strata's thinking budget (slow)",
            "effort": "high"
        }
    ],
    "supports_experimental_context": False,
    "supports_image_detail_original": False,
    "supports_parallel_tool_calls": False,
    "supports_reasoning_effort_updates": True,
    "supports_reasoning_summaries": False,
    "supports_reasoning_summary_parameter": False,
    "supports_search_tool": False,
    "tool_mode": "direct",
    "truncation_policy": {
        "limit": 10000,
        "mode": "tokens"
    },
    "upgrade": None,
    "use_responses_lite": False,
    "visibility": "list",
    "web_search_tool_type": "text"
}

LEVELS = [("none", "Thinking off: fastest, for simple steps"), ("low", "Brief thinking (the default here)"),
          ("medium", "Moderate thinking"), ("high", "Deep thinking, capped by Strata's thinking budget (slow)")]


def base_instructions(codex_cmd):
    """Codex's own base prompt for a model it does not know: Codex is run once against a tiny local server and the first message of its
    first request is read. (The offline prompt dump does not include it.)"""
    import http.server, threading
    seen = {}

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")

        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            seen.setdefault("body", json.loads(body))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for name, data in (("response.created", {"type": "response.created", "response": {"id": "r"}}),
                               ("response.output_item.done", {"type": "response.output_item.done", "item": {"type": "message", "role": "assistant", "id": "m",
                                "content": [{"type": "output_text", "text": "ok"}]}}),
                               ("response.completed", {"type": "response.completed", "response": {"id": "r", "usage": {
                                "input_tokens": 1, "input_tokens_details": None, "output_tokens": 1, "output_tokens_details": None, "total_tokens": 2}}})):
                self.wfile.write(f"event: {name}\ndata: {json.dumps(data)}\n\n".encode())

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    with tempfile.TemporaryDirectory() as home:
        with open(os.path.join(home, "config.toml"), "w") as fh:
            fh.write('model = "strata"\nmodel_provider = "strata"\n[model_providers.strata]\nname = "x"\n'
                     f'base_url = "http://127.0.0.1:{srv.server_address[1]}/v1"\nwire_api = "responses"\n[features]\ndaemon_auto_start = false\n')
        os.chmod(home, 0o777)
        tail = ["exec", "--skip-git-repo-check", "-s", "danger-full-access", "-c", 'approval_policy="never"', "hi"]
        if codex_cmd[0] == "docker":      # host network, so the container can reach the little server
            argv = codex_cmd[:2] + ["--network", "host", "--user", f"{os.getuid()}:{os.getgid()}", "-v", f"{home}:/h", "-e", "CODEX_HOME=/h", "-e", "HOME=/h"] + codex_cmd[2:] + tail
            env = None
        else:
            argv, env = codex_cmd + tail, dict(os.environ, CODEX_HOME=home)
        subprocess.run(argv, capture_output=True, text=True, env=env, stdin=subprocess.DEVNULL, timeout=120)
    srv.shutdown()
    try:
        text = seen["body"]["input"][0]["content"][0]["text"]
    except (KeyError, IndexError, TypeError):
        raise SystemExit("Codex did not send a request to the capture server; is the Codex command right?")
    if "coding agent" not in text[:200]:
        raise SystemExit("the first message is not Codex's base instructions; is this Codex 0.16x?")
    return text


def slim_instructions():
    """The kit's short base instructions: the file next to this script (also installed next to it in ~/agent/bin)."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "slim-instructions.md")
    try:
        with open(path) as fh:
            text = fh.read().strip("\n") + "\n"
    except OSError:
        raise SystemExit(f"{path} is missing. Install the kit again (bash kit.sh update), or use --stock for Codex's own instructions.")
    if "coding agent" not in text[:200] or len(text) < 200:
        raise SystemExit(f"{path} does not look like base instructions")
    return text


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True)
    ap.add_argument("--stock", action="store_true", help="use Codex's own base instructions (about 4,200 tokens) instead of the kit's slim ones")
    ap.add_argument("--codex", default="", help="path of a codex binary")
    ap.add_argument("--codex-image", default="", help="docker image that has codex (default: codex-sandbox)")
    a = ap.parse_args()
    entry = dict(ENTRY)
    if a.stock:
        cmd = [a.codex] if a.codex else ["docker", "run", "--rm", a.codex_image or "codex-sandbox", "codex"]
        entry["base_instructions"] = base_instructions(cmd)
    else:
        entry["base_instructions"] = slim_instructions()
    entry["supported_reasoning_levels"] = [{"effort": e, "description": d} for e, d in LEVELS]
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    with open(a.out, "w") as fh:
        json.dump({"models": [entry]}, fh, indent=1)
    print(f"wrote {a.out}: model strata, levels {[e for e, _ in LEVELS]}, {'stock' if a.stock else 'slim'} base instructions {len(entry['base_instructions'])} characters")


if __name__ == "__main__":
    sys.exit(main())
