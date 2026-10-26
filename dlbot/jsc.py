"""Persistent JS challenge solver for YouTube (one long-lived deno process).

Measured on Render free (0.1 CPU, 2026-10-26): every YouTube extraction started a new deno
process that parsed the ~150 KB solver library and evaluated the whole preprocessed player,
about 4.8 s of an 7-8 s extraction. This module keeps one small deno process alive that only
evaluates the PREPROCESSED player (once per player version) and then runs the tiny n/sig
functions. Preprocessing a raw player (a big AST job, the first time a player version is seen)
still runs in yt-dlp's normal one-shot deno, which exits and frees its memory: a warm process
that also did that held 312 MB and pushed the 512 MB container into OOM kills.

It plugs into yt-dlp's own deno provider (DenoJCP): `_construct_stdin` still builds the same
request, `_run_js_runtime` sends it to the warm process instead of a new one. Any failure
falls back to yt-dlp's normal one-shot run.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
import tempfile
import threading

log = logging.getLogger("jsc")

# Reads one JSON request per line on stdin, answers one JSON line per request on stdout.
# Works in deno and node. Needs no solver library: it only evaluates preprocessed players.
WARM_MAX_RSS_MB = int(os.environ.get("JSC_WARM_MAX_RSS_MB", "160"))   # recycle the process above this
SERVER_JS = r"""
const cache = new Map();          // player key -> {n, sig} solver functions
const MAX_PLAYERS = 4;
function solverFrom(pre) { const r = {n: null, sig: null}; Function("_result", pre)(r); return r; }
function remember(key, s) {
  cache.set(key, s);
  while (cache.size > MAX_PLAYERS) cache.delete(cache.keys().next().value);
}
function solveWith(s, requests) {
  return requests.map((req) => {
    const fn = s[req.type];
    if (!fn) return {type: "error", error: `Failed to extract ${req.type} function`};
    try { return {type: "result", data: Object.fromEntries(req.challenges.map((c) => [c, fn(c)]))}; }
    catch (e) { return {type: "error", error: e instanceof Error ? `${e.message}\n${e.stack}` : `${e}`}; }
  });
}
function handle(msg) {
  const d = msg.data;
  if (d.type === "cached") {
    const s = cache.get(msg.key);
    if (!s) return {type: "miss"};
    return {type: "result", responses: solveWith(s, d.requests)};
  }
  if (d.type === "preprocessed") {
    let s = cache.get(msg.key);
    if (!s) { s = solverFrom(d.preprocessed_player); remember(msg.key, s); }
    return {type: "result", responses: solveWith(s, d.requests)};
  }
  return {type: "error", error: "raw players are preprocessed by the one-shot run"};
}
function reply(id, obj) {
  const line = JSON.stringify({id, out: obj}) + "\n";
  if (typeof Deno !== "undefined") {
    const data = new TextEncoder().encode(line);   // writeSync may write only part of a big buffer
    let off = 0;
    while (off < data.length) off += Deno.stdout.writeSync(data.subarray(off));
  } else process.stdout.write(line);
}
function onLine(line) {
  if (!line.trim()) return;
  let msg;
  try { msg = JSON.parse(line); } catch (e) { return; }
  let out;
  try { out = handle(msg); } catch (e) { out = {type: "error", error: `${e}`}; }
  reply(msg.id, out);
}
async function main() {
  let buf = "";
  const dec = new TextDecoder();
  const feed = (chunk) => {
    buf += typeof chunk === "string" ? chunk : dec.decode(chunk, {stream: true});
    let i;
    while ((i = buf.indexOf("\n")) >= 0) { const line = buf.slice(0, i); buf = buf.slice(i + 1); onLine(line); }
  };
  if (typeof Deno !== "undefined") { for await (const chunk of Deno.stdin.readable) feed(chunk); }
  else { process.stdin.setEncoding("utf8"); process.stdin.on("data", feed); }
}
main();
"""


class WarmSolver:
    """One persistent JS process; thread-safe (calls are serialized)."""

    def __init__(self, cmd: list[str], timeout: float = 30.0, max_rss_mb: int = WARM_MAX_RSS_MB):
        self.cmd = cmd
        self.timeout = timeout
        self.max_rss_mb = max_rss_mb
        fd, self.script = tempfile.mkstemp(prefix="jsc-server-", suffix=".js")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(SERVER_JS)
        self.lock = threading.Lock()
        self.proc: subprocess.Popen | None = None
        self.known: set[str] = set()     # player keys the process has built solvers for
        self.n = 0

    def _ensure(self):
        if self.proc is None or self.proc.poll() is not None:
            self.known.clear()
            self.proc = subprocess.Popen([*self.cmd, self.script], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                         stderr=subprocess.DEVNULL, text=True, encoding="utf-8", bufsize=1)

    def _ask(self, msg: dict) -> dict:
        self.n += 1
        msg["id"] = self.n
        assert self.proc and self.proc.stdin and self.proc.stdout
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()
        result: dict = {}

        def read():
            line = self.proc.stdout.readline()
            if line:
                result["line"] = line

        t = threading.Thread(target=read, daemon=True)
        t.start()
        t.join(self.timeout)
        if "line" not in result:
            self.close()
            raise RuntimeError("warm solver did not answer")
        ans = json.loads(result["line"])
        if ans.get("id") != msg["id"]:
            self.close()
            raise RuntimeError("warm solver answered out of order")
        return ans["out"]

    def rss_mb(self) -> int | None:
        try:
            with open(f"/proc/{self.proc.pid}/status") as fh:
                return next(int(ln.split()[1]) // 1024 for ln in fh if ln.startswith("VmRSS:"))
        except Exception:
            return None

    def solve(self, data: dict) -> dict:
        """`data` (type "preprocessed") is the dict yt-dlp hands to jsc(); returns jsc()'s output dict."""
        if data.get("type") != "preprocessed":
            raise ValueError("warm solver only takes preprocessed players")
        key = hashlib.sha1((data.get("preprocessed_player") or "").encode()).hexdigest()
        with self.lock:
            self._ensure()
            try:
                if key in self.known:
                    out = self._ask({"key": key, "data": {"type": "cached", "requests": data["requests"]}})
                    if out.get("type") != "miss":
                        return out
                out = self._ask({"key": key, "data": data})
                if out.get("type") == "result":
                    self.known.add(key)
                return out
            finally:
                rss = self.rss_mb() if self.proc else None
                stats["rss_mb"] = rss
                if rss and rss > self.max_rss_mb:   # keep the 512 MB container safe
                    log.info("warm solver at %d MB, recycling it", rss)
                    self.close()

    def close(self):
        if self.proc is not None:
            try:
                self.proc.kill()
            except Exception:
                pass
        self.proc = None
        self.known.clear()


_solver: WarmSolver | None = None
_solver_lock = threading.Lock()
stats = {"warm": 0, "oneshot": 0, "fallback": 0, "error": None, "rss_mb": None}


def install() -> bool:
    """Patch yt-dlp's deno provider to use the warm process. Off with YT_WARM_JSC=0."""
    if os.environ.get("YT_WARM_JSC", "1") != "1":
        return False
    try:
        from yt_dlp.extractor.youtube.jsc._builtin.deno import DenoJCP
        from yt_dlp.extractor.youtube.jsc._builtin.ejs import ScriptVariant
    except Exception as e:  # layout changed in a newer yt-dlp
        log.warning("warm JS solver not installed: %s", e)
        return False
    if getattr(DenoJCP, "_dl_warm", False):
        return True
    orig_construct = DenoJCP._construct_stdin
    orig_run = DenoJCP._run_js_runtime

    def construct(self, player, preprocessed, requests, /):
        self._dl_data = {"type": "preprocessed", "preprocessed_player": player,
                         "requests": [{"type": r.type.value, "challenges": r.input.challenges} for r in requests]} \
            if preprocessed else {"type": "player", "player": player, "output_preprocessed": True,
                                  "requests": [{"type": r.type.value, "challenges": r.input.challenges} for r in requests]}
        return orig_construct(self, player, preprocessed, requests)

    def run(self, stdin, /):
        global _solver
        data = getattr(self, "_dl_data", None)
        self._dl_data = None
        if data is not None and data.get("type") != "preprocessed":
            stats["oneshot"] += 1     # first sight of a player version: heavy preprocessing, one-shot deno
            with _solver_lock:        # the old player's solver is useless now; free its RAM before the big run
                if _solver is not None:
                    _solver.close()
        elif data is not None and self._lib_script.variant != ScriptVariant.DENO_NPM:
            try:
                with _solver_lock:
                    if _solver is None:
                        _solver = WarmSolver([self.runtime_info.path, "run", "--no-prompt", "--no-remote", "--no-npm",
                                              "--no-config", "--no-lock", "--node-modules-dir=none", "--no-code-cache"])
                out = _solver.solve(data)
                stats["warm"] += 1
                return json.dumps(out)
            except Exception as e:
                stats["fallback"] += 1
                stats["error"] = str(e)[:200]
                log.warning("warm JS solver failed, one-shot deno instead: %s", e)
        return orig_run(self, stdin)

    DenoJCP._construct_stdin = construct
    DenoJCP._run_js_runtime = run
    DenoJCP._dl_warm = True
    return True
