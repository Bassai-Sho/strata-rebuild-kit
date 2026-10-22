# Project rules
- Check command (run it after every change): TO_BE_FILLED_IN
  Web page or game: browser-check index.html --keys ArrowLeft,Space --expect-text "TEXT" --expect-motion  (a REAL browser; --help lists more). Command-line program: run it with real input and compare the output.
- A task is done only when its check prints OK. Never tick it, or say it is done, before that.
- Keep command output short (head, tail -20, grep -n).
- Polish happens only in tasks named "Polish: ...". Any other task: no extra polish.

## Lessons from past projects (append new ones here, one sentence per line)
- WebGL: getContext('webgl') returning an object is NOT a usable context - probe its methods and constants and compile+link a real vertex+fragment shader on a throwaway canvas, then fall back WebGL -> canvas 2D -> spare canvas -> SVG, name the step that failed on screen, and only print NO RENDERER when every probe fails.
- Install the keydown listener before any renderer init and guard every GL call, so a renderer crash can never leave a dead start screen; window.onerror prints a visible ERROR line.
- Keys pressed before the game is ready are queued and flushed at boot (Enter on the start screen).
- document.hidden lies: Firefox reports a background window or a resistFingerprinting profile as hidden forever, so never pause-and-reset on hidden - pause only until the first keypress and keep the real score/wave.
- Pick a probe value that only the feature moves (window.game.ship.quat[1] moves on yaw alone). "probe changed 0 -> 0" means the code is wrong, not the check: fix the code, never weaken the probe.
- When a task remaps keys, AGENTS.md's check command belongs in that task's files: - a stale --keys list silently stops testing thrust.
- Prove every render fallback in a real browser: scratch copies that override getContext (no-webgl, no-2d, no-canvas, broken-draw), browser-check each one, delete scratch/ before the commit.
- Headless stubs grow with the page: press() records defaultPrevented (game keys must not scroll) and fireDoc('visibilitychange') mimics a tab switch.
- No CDN importmap for a 3D engine: write the renderer yourself, a CDN import in T1 forces a rewrite (pages work offline).
- A user-reported bug is a bugfix, not the current task: add a headless case proven to fail on the pre-fix page, re-run every suite 3x plus browser-check in each render mode, and leave the task unticked.
- After a failed attempt your code is still on disk: fix it in place, do not start over.
- "Movement feels wrong" in a 3D game is usually the camera, not the physics: print dot products against the vehicle's own axes in a scratch probe before touching the integrator.
- Chase cam = the vehicle's own axes: eye = pos - nose*dist + ownUp*height, view up = the vehicle's up, smoothed, snapped on reset; a fixed world offset with world-up gimbal-locks and flips 180 degrees when the nose passes vertical.
- Build the view basis as right = cross(up, forward); the shortcut right = (-fz, 0, fx) is its negation - a mirrored basis renders the world flipped and reads as "wrong x/y/z".
- Expose the camera as window.game.cam {pos,right,up} so the view is testable headlessly: a check on state the page keeps private can only pass by luck.
- 3D flight = quaternion increments on the ship's LOCAL axes, thrust along the rotated nose, velocity in world space, one key per axis (a key that is both thrust and pitch reads as broken flight).
- A headless suite must print its own FAIL lines: an uncaught throw inside the loaded page prints a Node stack and the check reads as a session timeout.
