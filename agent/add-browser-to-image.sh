#!/usr/bin/env bash
# add-browser-to-image.sh: give the codex-sandbox image a real (headless) Chromium, so `browser-check` can open a web project
# the way a person does. About 300 MB and a few minutes; needs internet. Safe to run again (it skips if the browser is there).
#   bash add-browser-to-image.sh [--force]
# The previous image is kept as codex-sandbox:before-browser (docker tag), so it can be put back.
set -euo pipefail
AGENT="${AGENT_DIR:-$HOME/agent}"
docker image inspect codex-sandbox >/dev/null 2>&1 || { echo "the image codex-sandbox is missing: run strata-agent-setup.sh first"; exit 1; }
if [ "${1:-}" != "--force" ] && docker run --rm codex-sandbox sh -c 'command -v browser-check' >/dev/null 2>&1; then
  echo "codex-sandbox already has browser-check. (--force rebuilds it)"; exit 0
fi
D="$(mktemp -d)"; trap 'rm -rf "$D"' EXIT
docker tag codex-sandbox codex-sandbox:before-browser
cat > "$D/Dockerfile" <<'DOCKERFILE'
FROM codex-sandbox:before-browser
ENV DEBIAN_FRONTEND=noninteractive PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers
RUN python3 -m venv /opt/pw \
 && /opt/pw/bin/pip install --no-cache-dir playwright pillow \
 && /opt/pw/bin/playwright install --with-deps chromium-headless-shell \
 && chmod -R a+rX /opt/pw /opt/pw-browsers \
 && rm -rf /var/lib/apt/lists/*
# the tool itself lives in ~/agent/codex-home/tools (mounted at /home/agent/.codex/tools), so it can be edited without rebuilding
RUN printf '#!/bin/sh\nexec /opt/pw/bin/python /home/agent/.codex/tools/browser-check.py "$@"\n' > /usr/local/bin/browser-check && chmod 755 /usr/local/bin/browser-check
DOCKERFILE
docker build -t codex-sandbox "$D"
# prove it works: a tiny page in a real browser, run the way a session runs it (host user, no capabilities)
T="$(mktemp -d)"; printf '<!doctype html><canvas id=c width=100 height=60></canvas><script>const x=document.getElementById("c").getContext("2d");x.fillStyle="#fc0";x.fillRect(5,5,40,30);x.fillStyle="#36f";x.fillRect(50,20,40,30);</script>' > "$T/t.html"
chmod -R a+rwX "$T"
docker run --rm --user "$(id -u):$(id -g)" -e HOME=/home/agent --cap-drop ALL --security-opt no-new-privileges \
  -v "$AGENT/codex-home:/home/agent/.codex" -v "$T:/t" -w /t codex-sandbox browser-check t.html --out /t/artifacts
rm -rf "$T"
echo "OK: codex-sandbox now has browser-check. Previous image: codex-sandbox:before-browser"
