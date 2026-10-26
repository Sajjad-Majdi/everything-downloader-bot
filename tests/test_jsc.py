"""The warm JS challenge solver (one long-lived process instead of a new deno per extraction)."""
import shutil

import pytest

from dlbot import jsc

pytestmark = pytest.mark.skipif(not shutil.which("node"), reason="node not installed (deno runs it on Render)")

PRE = '_result.n = (x) => x.split("").reverse().join(""); _result.sig = (x) => x + "!";'


def test_warm_solver_builds_once_then_reuses(tmp_path):
    s = jsc.WarmSolver(["node"], timeout=10)
    try:
        with pytest.raises(ValueError):                 # raw players go to the one-shot run
            s.solve({"type": "player", "player": "RAW", "requests": []})
        req = {"type": "preprocessed", "preprocessed_player": PRE,
               "requests": [{"type": "n", "challenges": ["abc", "xy"]}, {"type": "sig", "challenges": ["s"]}]}
        out = s.solve(dict(req))
        assert out["responses"][0]["data"] == {"abc": "cba", "xy": "yx"}
        assert out["responses"][1]["data"] == {"s": "s!"}
        pid = s.proc.pid
        out = s.solve(dict(req))                       # cached: the player text is not sent again
        assert out["responses"][0]["data"]["abc"] == "cba" and s.proc.pid == pid
        s.proc.kill()                                  # process died: started again, rebuilt
        s.proc.wait()
        out = s.solve(dict(req))
        assert out["responses"][1]["data"] == {"s": "s!"} and s.proc.pid != pid
    finally:
        s.close()


def test_install_patches_deno_provider_once():
    from yt_dlp.extractor.youtube.jsc._builtin.deno import DenoJCP
    assert jsc.install() is True and DenoJCP._dl_warm is True
    run = DenoJCP._run_js_runtime
    assert jsc.install() is True and DenoJCP._run_js_runtime is run
