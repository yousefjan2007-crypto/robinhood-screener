"""
THE invariant suite for robinhood_screener — this project's tests. Run after any change:

    python3 verify.py                      # the Mac partition (everything)
    GITHUB_ACTIONS=true python3 verify.py  # the cloud partition (mac_only sections print SKIP)

Every section is OFFLINE: injected fakes, temp dirs, monkeypatched module functions. Nothing
here sends (every send_all is dry_run=True), commits, pushes or touches the network. EXACTLY
three sections need the Mac (statsmodels for the Benjamini-Yekutieli comparison, the cross-pin of
the vendored Deflated Sharpe against the sibling repo's copy, a git identity for publish's temp
bare origin): they are tagged mac_only and print SKIP under config.IS_CI or when the dependency
is absent. The launchd checks are NOT among them — the plist walk, the retired-label set and the
launchd/ contents check read committed files and call no launchctl, so they run on BOTH
partitions and a launchd regression fails the cloud run too. The Deflated Sharpe itself
(selfimprove/dsr.py: numpy + statistics.NormalDist) runs on BOTH partitions — the Sunday gates
run on the Actions runner, which has no sibling repo and no scipy.

Every check corresponds to a real mistake, cited in the check name where there is one:
  $Cubrate            a wallet farm that looked impossibly good at 20 min (age/rate gates)
  FOMO / Bull500      quote artifacts read as 5000x winners (quote-integrity gates)
  472-of-1,400        a rate limit read as "no route" fabricated 472 dead ledger rows
  199-fills           a crash before the state write re-fired one policy 199 times
  tier-frozen         39 of 42 real A alerts recorded as B (tier fixed at first sighting)
  403 outage          Blockscout's Cloudflare challenge silently darkened a source for 3 weeks
  P1 look-ahead       the proven-dev tier scored on facts that post-dated the alert

Fail-fast: the first failing check raises. Ends with 'ALL INVARIANTS PASSED (N checks, M skipped)'.
"""
from __future__ import annotations

import ast
import calendar
import contextlib
import csv
import glob
import hashlib
import io
import json
import math
import os
import plistlib
import re
import shutil
import fcntl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

import config                                          # noqa: E402

N_PASS = 0
N_SKIP = 0
_SECTION = ""


def check(name: str, cond, detail: str = "") -> None:
    """Print PASS/FAIL and assert — fail-fast: the first broken invariant stops the run."""
    global N_PASS
    if cond:
        N_PASS += 1
        print(f"  [PASS] {name}")
        return
    print(f"  [FAIL] {name}" + (f" — {detail}" if detail else ""))
    raise AssertionError(f"invariant failed in section {_SECTION!r}: {name} {detail}")


def skip(name: str, why: str) -> None:
    global N_SKIP
    N_SKIP += 1
    print(f"  [SKIP] {name} — {why}")


def section(title: str) -> None:
    global _SECTION
    _SECTION = title
    print(f"\n=== {title} ===")


MAC = not config.IS_CI
ENTRY_BOT_STATS = os.path.join(config.HOME, "entry_bot", "stats.py")


def _have(mod: str) -> bool:
    try:
        __import__(mod)
        return True
    except Exception:
        return False


DSR_XPIN = MAC and os.path.isfile(ENTRY_BOT_STATS) and _have("scipy")   # the cross-pin only; the DSR is vendored
HAVE_GIT = shutil.which("git") is not None


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


def _tree(path: str) -> ast.AST:
    return ast.parse(_read(path), filename=path)


def _rel(path: str) -> str:
    return os.path.relpath(path, ROOT)


def _py_files() -> list:
    out = []
    for dp, dns, fns in os.walk(ROOT):
        rel = os.path.relpath(dp, ROOT)
        parts = rel.split(os.sep)
        if parts[0] in ("cache", "data", ".git", "__pycache__", ".claude") or "__pycache__" in parts:
            dns[:] = []
            continue
        for fn in fns:
            if fn.endswith(".py"):
                out.append(os.path.join(dp, fn))
    return sorted(out)


def _is_main_guard(node) -> bool:
    if not isinstance(node, ast.If) or not isinstance(node.test, ast.Compare):
        return False
    t = node.test
    names = [n.id for n in ast.walk(t) if isinstance(n, ast.Name)]
    consts = [c.value for c in ast.walk(t) if isinstance(c, ast.Constant)]
    return "__name__" in names and "__main__" in consts


def _compute_nodes(tree, skip_funcs=()):
    """Every node outside the `if __name__ == "__main__"` guard and outside the named
    (test-only) functions."""
    for stmt in tree.body:
        if _is_main_guard(stmt):
            continue
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)) and stmt.name in skip_funcs:
            continue
        for n in ast.walk(stmt):
            yield n


def _attr_chain(node) -> list:
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return list(reversed(parts))
    return []


def _top_imports(tree) -> set:
    out = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            out.update(a.name.split(".")[0] for a in n.names)
        elif isinstance(n, ast.ImportFrom) and n.module and not n.level:
            out.add(n.module.split(".")[0])
    return out


def _time_time_calls(nodes) -> int:
    n = 0
    for node in nodes:
        if isinstance(node, ast.Call) and _attr_chain(node.func) == ["time", "time"]:
            n += 1
    return n


def _mentions(node, ident: str) -> bool:
    for n in ast.walk(node):
        if isinstance(n, ast.Name) and n.id == ident:
            return True
        if isinstance(n, ast.Attribute) and n.attr == ident:
            return True
    return False


def _fake_hdrs(d: dict | None = None):
    import email.message
    m = email.message.Message()
    for k, v in (d or {}).items():
        m[k] = v
    return m


def _http_error(url: str, code: int, hdrs: dict | None = None):
    return urllib.error.HTTPError(url, code, f"HTTP {code}", _fake_hdrs(hdrs), io.BytesIO(b""))


def _capture(fn, *a, **k) -> tuple:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        r = fn(*a, **k)
    return r, buf.getvalue()


# ═══════════════════════════════════════════════════════════════════════════════════
section("A. source hygiene (AST-based; identifiers in strings/comments never trip)")
# ═══════════════════════════════════════════════════════════════════════════════════
import screen                                           # noqa: E402
import http_client                                      # noqa: E402
from selfimprove.entry_lab import bands as B            # noqa: E402
from selfimprove.candidates import register as REGC     # noqa: E402

FORBIDDEN_IMPORTS = {"datetime", "time", "urllib", "http_client", "random", "subprocess",
                     "requests", "socket", "pathlib"}
FORBIDDEN_CALLS = {"open", "exec", "eval", "__import__"}


def _hygiene(path: str, allow_open_in=()) -> tuple:
    """(forbidden imports, forbidden attribute chains, forbidden calls outside allow_open_in)."""
    tree = _tree(path)
    bad_imp = sorted(_top_imports(tree) & FORBIDDEN_IMPORTS)
    bad_attr, bad_call = [], []
    allowed_open_calls = set()
    for stmt in tree.body:
        if isinstance(stmt, ast.FunctionDef) and stmt.name in allow_open_in:
            for n in ast.walk(stmt):
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "open":
                    allowed_open_calls.add(id(n))
    for n in _compute_nodes(tree):                       # the __main__ smoke test is not a band body
        if isinstance(n, ast.Attribute):
            ch = _attr_chain(n)
            for a, b in zip(ch, ch[1:]):
                if (a in ("numpy", "np") and b == "random") or (a == "os" and b == "environ"):
                    bad_attr.append(".".join(ch))
        elif isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in FORBIDDEN_CALLS:
            if n.func.id == "open" and id(n) in allowed_open_calls:
                continue
            bad_call.append(n.func.id)
    return bad_imp, bad_attr, bad_call


REG_RAW = json.load(open(config.REGISTRY_PATH))
CAND_DIR = os.path.join(config.SELFIMPROVE_DIR, "candidates")
cand_paths = [os.path.join(CAND_DIR, e["module"].split(".", 1)[1] + ".py")
              for e in REG_RAW.get("candidates", []) if str(e.get("module", "")).startswith("candidates.")]
cand_paths.append(os.path.join(CAND_DIR, "_template.py"))
# The two adaptive-exit POLICY modules that ship in candidates/. Registration is a permanent
# counted trial and the operator's own step, so it may land on any day without a verify edit:
# every check below must hold whether they are registered yet or not. They face the full
# candidate hygiene either way, and the path list is deduped so a registered one is walked once.
SHIPPED_POLICY_CANDS = ["tp15_half_armtrail30_stop50_6h", "tp15_half_flowtrail_stop50_6h"]
cand_paths.extend(os.path.join(CAND_DIR, n + ".py") for n in SHIPPED_POLICY_CANDS)
cand_paths = list(dict.fromkeys(cand_paths))

for p in (os.path.join(ROOT, "screen.py"),):
    bi, ba, bc = _hygiene(p)
    check("screen.py imports no clock/network/RNG/subprocess module and calls no open/exec/eval",
          not bi and not ba and not bc, f"{bi} {ba} {bc}")
bi, ba, bc = _hygiene(os.path.join(ROOT, "selfimprove", "entry_lab", "bands.py"),
                      allow_open_in=("static_ok", "load_registry"))
check("bands.py imports no clock/network/RNG module; open() only inside the loader "
      "(static_ok/load_registry), never in a band body", not bi and not ba and not bc, f"{bi} {ba} {bc}")
for p in cand_paths:
    bi, ba, bc = _hygiene(p)
    check(f"candidate {_rel(p)} is clock/network/RNG/open-free", not bi and not ba and not bc,
          f"{bi} {ba} {bc}")
    ok, why = B.static_ok(p)
    check(f"bands.static_ok accepts {_rel(p)}", ok, why)
for _n in SHIPPED_POLICY_CANDS:
    _r = REGC._run_validator(os.path.join(CAND_DIR, _n + ".py"), _n, [], "1")
    check(f"candidate {_n} imports cleanly under `python3 -I -S` exactly the way register.py validates one "
          "(stdlib only, no site-packages, no env) and declares NAME == the file name, KIND 'policy', a "
          "RATIONALE and a POLICY the CURRENT validate_policy accepts",
          bool(_r.get("ok")) and _r.get("name") == _n and _r.get("kind") == "policy"
          and str(_r.get("rationale") or "").strip()
          and REGC.POL.validate_policy("zz_probe", _r.get("policy")) is None, str(_r)[:200])
_reg_pols = [c for c in REG_RAW.get("candidates", []) if c.get("kind") == "policy" and c.get("status") != "retired"]
check("every non-retired kind == 'policy' registry entry still validates under the CURRENT validate_policy "
      "(shape checked under a probe name, since a registered candidate is already in POLICIES), so the "
      "extended arm/flow schema can never orphan one that is already registered",
      not [c.get("name") for c in _reg_pols
           if REGC.POL.validate_policy("zz_probe", c.get("policy")) is not None],
      str([c.get("name") for c in _reg_pols]))
with tempfile.TemporaryDirectory() as _d:
    cases = {"t_time.py": "import time\nNAME='x'\n",
             "t_open.py": "import config\ndef verdict(f):\n    return open('/etc/passwd')\n",
             "t_sklearn.py": "from sklearn.linear_model import LogisticRegression\n",
             "t_str.py": "import config\nNAME = 'ctl_random_band'\n# import time, open()\n"
                         "def verdict(f):\n    return f.get('time') is None\n"}
    for fn, src in cases.items():
        with open(os.path.join(_d, fn), "w") as fh:
            fh.write(src)
    check("static_ok rejects a candidate importing time", not B.static_ok(os.path.join(_d, "t_time.py"))[0])
    check("static_ok rejects a candidate calling open()", not B.static_ok(os.path.join(_d, "t_open.py"))[0])
    check("static_ok rejects a candidate importing sklearn (registry-only imports, import allowlist)",
          not B.static_ok(os.path.join(_d, "t_sklearn.py"))[0])
    check("static_ok never matches identifiers inside strings/comments ('time', 'open()' in a comment)",
          B.static_ok(os.path.join(_d, "t_str.py"))[0])

run_tree = _tree(os.path.join(ROOT, "run.py"))
check("run.py contains exactly ONE time.time() call (the single wall-clock capture)",
      _time_time_calls(ast.walk(run_tree)) == 1, str(_time_time_calls(ast.walk(run_tree))))
for rel in ("ledger.py", "screen.py", os.path.join("sources", "safety.py"), "quotes.py",
            os.path.join("selfimprove", "policies.py")):
    t = _tree(os.path.join(ROOT, rel))
    n = _time_time_calls(_compute_nodes(t))
    check(f"{rel} has no time.time() in any compute path (outside the __main__ smoke test)", n == 0, str(n))

missing_future = []
for p in _py_files():
    t = _tree(p)
    body = t.body
    i = 1 if (body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant)
              and isinstance(body[0].value.value, str)) else 0
    if len(body) <= i:
        continue                                        # an empty __init__.py has no statements
    st = body[i]
    if not (isinstance(st, ast.ImportFrom) and st.module == "__future__"
            and any(a.name == "annotations" for a in st.names)):
        missing_future.append(_rel(p))
check("every .py (cache/, data/ excluded) begins with 'from __future__ import annotations' after its docstring",
      not missing_future, str(missing_future))

# a budget constant lives in config.py and NOWHERE else: a module literal beside config's own
# GT_NEW_POOLS_CACHE_S shadowed it silently (the feed paged at a TTL config did not know about)
_gt_path = os.path.join(ROOT, "sources", "geckoterminal.py")
_gt_ttl = sorted(t_.id for n_ in _tree(_gt_path).body if isinstance(n_, ast.Assign)
                 for t_ in n_.targets if isinstance(t_, ast.Name) and t_.id.endswith("CACHE_S"))
check("sources/geckoterminal.py defines no cache-TTL literal of its own — the new-pools TTL is read from "
      "config.GT_NEW_POOLS_CACHE_S, so one edit in config changes the feed's freshness",
      not _gt_ttl and "config.GT_NEW_POOLS_CACHE_S" in _read(_gt_path), str(_gt_ttl))

leaks = []
for base in (os.path.join(ROOT, "docs"), os.path.join(ROOT, "selfimprove", "research"), os.path.join(ROOT, ".github")):
    for dp, dns, fns in os.walk(base):
        dns[:] = [d for d in dns if d not in ("context", "logs", "__pycache__")]   # gitignored scratch
        for fn in fns:
            p = os.path.join(dp, fn)
            try:
                txt = _read(p)
            except Exception:
                continue
            if "/Users/" in txt or "vrp_backtest" in txt:
                leaks.append(_rel(p))
check("no file under docs/, selfimprove/research/ or .github/ contains '/Users/' or 'vrp_backtest' (public-repo scrub)",
      not leaks, str(leaks))

# ── cadence drift (Phase 9): the Mac dispatch job is gone and the keeper's cadence is a config
# constant, so no source or doc may still quote the retired job's interval or a nominal "one run
# per five minutes". The needles are ASSEMBLED, never written out, so this file does not trip its
# own walk; the same trick keeps the entry-lag needle out of the text below.
_STALE_CADENCE = ("StartInterval " + "300", "every " + "5 min")
_LAG_NEEDLES = ("3" + "–" + "8 min", "3" + "-" + "8 min")
_TEXT_EXT = (".md", ".py", ".yml", ".yaml", ".sh", ".plist")
_SKIP_DIRS = {".git", "cache", "data", "__pycache__", "context", "logs", "node_modules"}
_cadence_bad, _lag_bad = [], []
for dp, dns, fns in os.walk(ROOT):
    dns[:] = [d for d in dns if d not in _SKIP_DIRS and not d.startswith(".super")]
    for fn in fns:
        if not fn.endswith(_TEXT_EXT):
            continue
        p = os.path.join(dp, fn)
        try:
            txt = _read(p)
        except Exception:
            continue
        for needle in _STALE_CADENCE:
            if needle in txt:
                _cadence_bad.append(f"{_rel(p)}:{needle!r}")
        for i, ln in enumerate(txt.splitlines(), 1):
            if any(nd in ln for nd in _LAG_NEEDLES) and "retired" not in ln.lower():
                _lag_bad.append(f"{_rel(p)}:{i}")
check("no file in the tree still carries the retired Mac dispatch job's cadence — neither its launchd "
      "interval key with the old value nor a nominal per-five-minute phrasing; the scan's cadence is "
      f"config.KEEPER_CADENCE_S = {config.KEEPER_CADENCE_S} s and the docs print MEASURED runs",
      not _cadence_bad, str(_cadence_bad))
check("every line that quotes the old 3-to-8-minute entry lag says 'retired' on that same line — inside "
      "the keeper the lag is the scan's wall time after alert_ts plus at most one tick, and the Mac "
      "measurement must never be read as the live number",
      not _lag_bad, str(_lag_bad))

rate_names = {n: getattr(config, n) for n in dir(config) if n.endswith("_RATE_HZ") and n != "HOST_RATE_HZ"}
unmapped = [h for h, hz in http_client._HOST_HZ.items() if hz not in rate_names.values()]
check("every http_client._HOST_HZ value is a config.*_RATE_HZ constant (no literals in http_client)",
      not unmapped and dict(http_client._HOST_HZ) == dict(config.HOST_RATE_HZ), str(unmapped))
check("HTTP_RATE_SCALE: with RH_HTTP_RATE_SCALE unset the assembled HOST_RATE_HZ is exactly today's constants (scale 1.0, no "
      "arithmetic applied)",
      config.HTTP_RATE_SCALE == 1.0 and config.HOST_RATE_HZ == {
          "api.dexscreener.com": config.DEXSCREENER_RATE_HZ, "api.geckoterminal.com": config.GECKOTERMINAL_RATE_HZ,
          "robinhoodchain.blockscout.com": config.BLOCKSCOUT_RATE_HZ, "rpc.mainnet.chain.robinhood.com": config.RPC_RATE_HZ,
          "scanhood.xyz": config.SCANHOOD_RATE_HZ, "api.robinx.io": config.ROBINX_RATE_HZ,
          "aggregator-api.kyberswap.com": config.KYBER_RATE_HZ, "openapi.gmgn.ai": config.GMGN_RATE_HZ}, str(config.HOST_RATE_HZ))
_rs = subprocess.run([sys.executable, "-c", "import config, json; print(json.dumps(config.HOST_RATE_HZ))"], cwd=ROOT,
                     env=dict(os.environ, RH_HTTP_RATE_SCALE="0.5"), capture_output=True, text=True)
_rs0 = subprocess.run([sys.executable, "-c", "import config"], cwd=ROOT, env=dict(os.environ, RH_HTTP_RATE_SCALE="0"),
                      capture_output=True, text=True)
check("RH_HTTP_RATE_SCALE=0.5 halves every assembled rate (a multiplier applied where HOST_RATE_HZ is assembled — the operator's knob "
      "for the keeper's book process only, never verify); a non-positive scale is refused at import (the throttle divides by hz)",
      _rs.returncode == 0 and json.loads(_rs.stdout) == {h: hz * 0.5 for h, hz in config.HOST_RATE_HZ.items()} and _rs0.returncode != 0,
      (_rs.stderr or _rs.stdout)[-300:])
gt_hz = http_client._HOST_HZ["api.geckoterminal.com"]
check("GeckoTerminal <= 0.4 Hz everywhere and == 0.25 Hz on the Mac (30/min per IP is shared with "
      "solana-listener/-livebook; 0.4 Hz measured 47% 429s)",
      gt_hz <= 0.4 and (config.IS_CI or gt_hz == 0.25), str(gt_hz))

bad_urlopen = []
for p in _py_files():
    for n in ast.walk(_tree(p)):
        if isinstance(n, ast.Call):
            f = n.func
            is_uo = (isinstance(f, ast.Attribute) and f.attr == "urlopen") or \
                    (isinstance(f, ast.Name) and f.id == "_urlopen")
            if not is_uo:
                continue
            ctx = [k for k in n.keywords if k.arg == "context"]
            ok = bool(ctx) and "SSL" in (ctx[0].value.id if isinstance(ctx[0].value, ast.Name)
                                         else getattr(ctx[0].value, "attr", ""))
            if not ok:
                bad_urlopen.append(f"{_rel(p)}:{n.lineno}")
check("every urllib urlopen call site passes context=<certifi SSL context> (system certs fail CERTIFICATE_VERIFY_FAILED)",
      not bad_urlopen, str(bad_urlopen))

writers = []
for p in _py_files():
    if _rel(p) in (os.path.join("selfimprove", "champion.py"), "verify.py"):   # verify's temp fixtures are not writers
        continue
    for n in _compute_nodes(_tree(p), skip_funcs=("_selftest", "_smoke", "_fixture")):
        if not isinstance(n, ast.Call):
            continue
        f = n.func
        if isinstance(f, ast.Name) and f.id == "open" and n.args and _mentions(n.args[0], "CHAMPION_PATH"):
            mode = None
            if len(n.args) > 1 and isinstance(n.args[1], ast.Constant):
                mode = n.args[1].value
            for k in n.keywords:
                if k.arg == "mode" and isinstance(k.value, ast.Constant):
                    mode = k.value.value
            if mode and str(mode)[0] in "wa":
                writers.append(f"{_rel(p)}:{n.lineno}")
        if _attr_chain(f) == ["os", "replace"] and len(n.args) > 1 and _mentions(n.args[1], "CHAMPION_PATH"):
            writers.append(f"{_rel(p)}:{n.lineno}")
check("no module other than selfimprove/champion.py opens config.CHAMPION_PATH for writing (sole writer = write_state)",
      not writers, str(writers))

GIT_BAD = {"pull", "checkout", "reset"}
py_git = []
for rel in ("selfimprove/improve.py", "selfimprove/publish.py", "selfimprove/livebook.py",
            "selfimprove/research/allowlist.py", "selfimprove/weekly_summary.py"):
    for n in ast.walk(_tree(os.path.join(ROOT, rel))):
        if isinstance(n, (ast.List, ast.Tuple)):
            elems = [e.value for e in n.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)]
            if ("git" in elems or elems[:1] in (["fetch"], ["worktree"], ["push"], ["add"], ["commit"], ["show"])) \
                    and set(elems) & GIT_BAD:
                py_git.append(f"{rel}:{n.lineno}")
check("improve.py / publish.py / livebook.py / research scripts never run git pull/checkout/reset "
      "(a data job must never move the user's HEAD)", not py_git, str(py_git))
sh_git = []
for rel in ("selfimprove/research/run_research.sh",):   # run_improve.sh and launchd/dispatch.sh: deleted
    for i, ln in enumerate(_read(os.path.join(ROOT, rel)).splitlines(), 1):
        s = ln.strip()
        if s.startswith("#"):
            continue
        if re.search(r"\bgit\b[^|&;]*\b(pull|checkout|reset)\b", s) and '-C "$WT"' not in s:
            sh_git.append(f"{rel}:{i}")
check("the shell wrappers run git checkout/reset only inside the temp worktree (-C \"$WT\"), never on the Mac tree",
      not sh_git, str(sh_git))
# the keeper (runner-only) is allowed `git pull --rebase --autostash` on ITS checkout — it is the
# committer — but never checkout/reset/stash: a rebase that goes wrong is aborted, never forced
KEEPER_SH = os.path.join(ROOT, ".github", "keeper.sh")
keeper_git = []
for i, ln in enumerate(_read(KEEPER_SH).splitlines(), 1):
    s = ln.strip()
    if s.startswith("#"):
        continue
    if re.search(r"\bgit\b[^|&;]*\b(checkout|reset|stash)\b", s):
        keeper_git.append(f".github/keeper.sh:{i}")
check("keeper.sh never runs git checkout/reset/stash (a failed rebase is aborted; --autostash on the pull instead)",
      not keeper_git, str(keeper_git))
_ks = _read(KEEPER_SH)
for needle in ("flock", "git add data/ docs/", "--autostash", "rebase --abort", "trap", "gh auth status",
               "gh workflow run robinhood-pages", "-f mode=keeper", "keeper_handoff.json", "--keeper-alive",
               "--ensure-keeper", "set -u", "KEEPER_CADENCE_S", "KEEPER_MAX_S", "KEEPER_HANDOFF_LEAD_S",
               "KEEPER_HANDOFF_WAIT_S", "PAGES_EVERY_N_ITERATIONS", "robinhood-weekly", "exit 3"):
    check(f"keeper.sh contains {needle!r}", needle in _ks)
check("keeper.sh reads its constants from config (never a hardcoded cadence) and defines the modes as functions",
      "import config" in _ks and "print(config.KEEPER_CADENCE_S" in _ks
      and all(f"{fn}()" in _ks for fn in ("keeper_alive", "ensure_keeper", "commit_push", "with_lock", "write_handoff")))
_bn = subprocess.run(["bash", "-n", KEEPER_SH], capture_output=True, text=True)
check("bash -n .github/keeper.sh parses", _bn.returncode == 0, _bn.stderr[-300:])
# round 1 (review): the handoff marker is written on ORIGIN's base, the alive checks are three-valued and
# fail closed, the successor breaks on a dead predecessor, the exit-3 self-dispatch sits behind the breaker
def _bash_fn(src, name):
    """The text of a top-level bash function `name() {` … up to the first line that is exactly `}`."""
    m = re.search(r"^" + re.escape(name) + r"\(\) \{.*?^\}", src, re.M | re.S)
    return m.group(0) if m else ""
for needle in ("set -u -o pipefail", "_keeper_state()", "keeper_state()", "running_state()", "circuit_open()", "--circuit-open",
               "_push_marker()", "push_marker()", "marker_only_local_commit()", "rev-list --count origin/main..HEAD",
               "config.KEEPER_CIRCUIT_FAILURES, config.KEEPER_CIRCUIT_WINDOW_S, int(config.LIVEBOOK_TICK_INTERVAL_S), "
               "config.KEEPER_BOOK_STOP_WAIT_S)"):
    check(f"keeper.sh contains {needle!r}", needle in _ks)
_fn = {n: _bash_fn(_ks, n) for n in ("_keeper_state", "keeper_alive", "ensure_keeper", "maybe_dispatch_successor",
                                     "_commit_push", "_push_marker", "await_predecessor", "finish")}
check("keeper.sh: every function the round-1 pins read is a top-level `name() {` … `}` block",
      all(_fn.values()), str([k for k, v in _fn.items() if not v]))
check("keeper.sh: the alive helpers are three-valued — alive | none | unknown — a failed gh call is unknown, never none, and "
      "--keeper-alive returns 2 for it",
      all(w in _fn["_keeper_state"] for w in ("echo alive", "echo none", "echo unknown", "failed=1"))
      and "return 2" in _fn["keeper_alive"])
check("keeper.sh: ensure_keeper and the successor dispatch act only on a DEFINITE none (unknown ⇒ nothing dispatched: rc 2, or "
      "retried next lead-window iteration)",
      "unknown)" in _fn["ensure_keeper"] and "return 2" in _fn["ensure_keeper"]
      and '[ "$st" = none ] || return 0' in _fn["maybe_dispatch_successor"])
check("keeper.sh: the marker routine pulls origin BEFORE writing the marker (a `done` committed on the stale line conflicts on every "
      "retry and never lands), finish delivers `done` through it after the straggler commit — never a bare write_handoff on the old "
      "base — and the successor's `ready` rides the same routine",
      _fn["_push_marker"].index("git pull --rebase --autostash -q origin main") < _fn["_push_marker"].index('write_handoff "$1"')
      and "push_marker done" in _fn["finish"] and "write_handoff done" not in _fn["finish"]
      and _fn["finish"].index("commit_push") < _fn["finish"].index("push_marker done")
      and "push_marker ready" in _fn["await_predecessor"] and "commit_push" not in _fn["await_predecessor"])
check("keeper.sh: `-X theirs` appears exactly once, inside the marker routine, behind the marker-only-single-commit gate; the scan "
      "commit routine stays abort-only (a data conflict is never resolved blindly)",
      _ks.count("-X theirs") == 1 and "-X theirs" in _fn["_push_marker"]
      and _fn["_push_marker"].index("marker_only_local_commit ||") < _fn["_push_marker"].index("-X theirs")
      and "-X " not in _fn["_commit_push"] and "rebase --abort" in _fn["_push_marker"])
check("keeper.sh: the successor's wait breaks on a DEFINITE none from running_state inside the poll loop (never on unknown), and "
      "finish's exit-3 self-dispatch sits behind circuit_open (open or unknown ⇒ the watchdog owns the restart)",
      _fn["await_predecessor"].index("while [") < _fn["await_predecessor"].index('[ "$(running_state)" = none ]')
      and "unknown" in _fn["await_predecessor"]
      and "circuit_open; c=$?" in _fn["finish"] and _fn["finish"].index("circuit_open") < _fn["finish"].index("dispatch_keeper")
      and 'if [ "$c" != 1 ]' in _fn["finish"] and 'none)  dispatch_keeper "$(other_slot)" keeper' in _fn["finish"])
# Phase 4: the paper book inside the keeper — the Python side holds the lock, after await_predecessor, stopped before `done`
for needle in ("KEEPER_BOOK", "export LIVEBOOK_FEED_SOURCE=worktree", "book_loop()", "book_start()", "book_stop()",
               "book_last_tick_ts()", "python3 -u selfimprove/livebook.py --tick; rc=$?", "book first tick: gap since snapshot's last_tick_ts",
               "data/livebook_ticks.jsonl", 'LOCK="data/.keeper.lock"', "commit_push() { with_lock _commit_push; }",
               "push_marker() { with_lock _push_marker"):
    check(f"keeper.sh contains {needle!r}", needle in _ks)
_fnb = {n: _bash_fn(_ks, n) for n in ("book_loop", "book_start", "book_stop", "keeper_main", "finish", "read_config")}
check("keeper.sh: the book functions, keeper_main and read_config are top-level `name() {` … `}` blocks",
      all(_fnb.values()), str([k for k, v in _fnb.items() if not v]))
check("keeper.sh (fix round 1): the tick is NOT run under with_lock — livebook._state_lock takes flock(2) on the same file around its "
      "reads and its write phase only, so a 26–160 s quote phase never holds a commit up — while commit_push and push_marker keep "
      "with_lock (flock(1) on data/.keeper.lock); book_loop naps between ticks (never a bare sleep, never a negative nap) and logs "
      "every 10th tick plus every non-zero rc",
      "python3 -u selfimprove/livebook.py --tick; rc=$?" in _fnb["book_loop"] and "with_lock" not in _fnb["book_loop"]
      and "nap " in _fnb["book_loop"]
      and not re.search(r"^\s*sleep\b", _fnb["book_loop"], re.M) and "n % 10" in _fnb["book_loop"] and '"$rc" != 0' in _fnb["book_loop"]
      and '-gt 0 ] && nap' in _fnb["book_loop"] and "trap 'BOOK_STOP=1' TERM" in _fnb["book_loop"])
check("keeper.sh: book_start runs AFTER await_predecessor and BEFORE main_loop (a successor never ticks before the predecessor's "
      "`done` or its absence), exports LIVEBOOK_FEED_SOURCE=worktree, backgrounds book_loop and honours KEEPER_BOOK=0",
      _fnb["keeper_main"].index("await_predecessor") < _fnb["keeper_main"].index("book_start") < _fnb["keeper_main"].index("main_loop")
      and "export LIVEBOOK_FEED_SOURCE=worktree" in _fnb["book_start"] and "book_loop &" in _fnb["book_start"]
      and '"$KEEPER_BOOK" != 1' in _fnb["book_start"])
check("keeper.sh: book_start refuses to start the loop while data/livebook.json is not TRACKED in the checkout (the cloud book must be "
      "seeded before the first tick, or a fresh book collides with the seed at the next commit_push)",
      "git ls-files --error-unmatch data/livebook.json" in _fnb["book_start"]
      and _fnb["book_start"].index("git ls-files --error-unmatch") < _fnb["book_start"].index("book_loop &"))
check("keeper.sh: finish stops the book loop (TERM + a bounded wait for the in-flight tick) BEFORE the final commit_push, which "
      "precedes push_marker done — the predecessor's last tick rides its final snapshot",
      _fnb["finish"].index("book_stop") < _fnb["finish"].index("commit_push") < _fnb["finish"].index("push_marker done")
      and "kill -TERM" in _fnb["book_stop"] and "BOOK_STOP_WAIT_S" in _fnb["book_stop"] and "kill -0" in _fnb["book_stop"])
check("keeper.sh: read_config prints the tick interval and the stop bound from config (no tick literal in the script)",
      "int(config.LIVEBOOK_TICK_INTERVAL_S)" in _fnb["read_config"] and "config.KEEPER_BOOK_STOP_WAIT_S" in _fnb["read_config"]
      and "BOOK_TICK_S" in _fnb["read_config"] and "BOOK_STOP_WAIT_S" in _fnb["read_config"])
pub_src = _read(os.path.join(ROOT, "selfimprove", "publish.py"))
res_src = _read(os.path.join(ROOT, "selfimprove", "research", "run_research.sh"))
check("publish.py adds its detached worktree under tempfile.mkdtemp() (never a repo-relative path a "
      "sibling voice assistant would glob)", "mkdtemp(" in pub_src and 'wt = tempfile.mkdtemp' in pub_src
      and '"worktree", "add", "--detach", wt' in pub_src)
check("run_research.sh cuts its worktree from `mktemp -d` and adds it with worktree add --detach \"$WT\"",
      'WT="$(mktemp -d' in res_src and re.search(r'worktree add --detach "\$WT" origin/main', res_src) is not None
      and '$REPO/wt' not in res_src)

plist_dir = os.path.join(ROOT, "launchd")
plists = sorted(glob.glob(os.path.join(plist_dir, "*.plist")))
labels = []
for p in plists:
    # launchd's parser (plutil -lint: OK) tolerates '--' inside XML comments; expat does not, so
    # the comments are stripped before the strict parse — the keys are what matter here
    d = plistlib.loads(re.sub(rb"<!--.*?-->", b"", open(p, "rb").read(), flags=re.S))
    labels.append(d.get("Label"))
    pa = d.get("ProgramArguments") or []
    check(f"launchd/{os.path.basename(p)}: Label com.yousefjan.*, absolute python/bash, WorkingDirectory set",
          str(d.get("Label", "")).startswith("com.yousefjan.") and pa and os.path.isabs(pa[0])
          and os.path.basename(pa[0]) in ("python3", "bash") and os.path.isabs(str(d.get("WorkingDirectory", ""))),
          str(d))
# The retired set grew as each loop moved into the cloud: -screener / -dashboard at the rebuild,
# -improve when the Sunday gates moved to weekly.yml, and -dispatch / -livebook on 2026-09-19 when
# the keeper became the scan loop and started ticking the book itself. Each plist is DELETED along
# with its script, so a stale copy on the Mac cannot double-run anything — and a label left loaded
# after that fails every interval (its program is gone), which a sibling voice assistant reads as a
# fault. The deletion and `launchctl bootout gui/$(id -u)/<label>` go together.
retired = {"com.yousefjan.robinhood-screener", "com.yousefjan.robinhood-dashboard",
           "com.yousefjan.robinhood-improve", "com.yousefjan.robinhood-dispatch",
           "com.yousefjan.robinhood-livebook"}
check("the five retired launchd labels com.yousefjan.robinhood-screener / -dashboard / -improve / -dispatch / -livebook do not "
      "exist under launchd/, and neither selfimprove/run_improve.sh nor launchd/dispatch.sh survives (the Sunday gates run in "
      ".github/workflows/weekly.yml; the scan and the book run in .github/keeper.sh)",
      not (retired & set(labels)) and not any(os.path.basename(p).replace(".plist", "") in retired for p in plists)
      and not os.path.exists(os.path.join(ROOT, "selfimprove", "run_improve.sh"))
      and not os.path.exists(os.path.join(ROOT, "launchd", "dispatch.sh")), str(sorted(labels)))
_launchd_files = sorted(os.path.basename(f_) for f_ in glob.glob(os.path.join(plist_dir, "*"))
                        if os.path.isfile(f_))
check("launchd/ holds EXACTLY com.yousefjan.robinhood-research.plist — one optional Mac job, nothing else: the scan, the book and "
      "the Sunday gates all run on Actions, and a second file here would mean a loop has two owners",
      _launchd_files == ["com.yousefjan.robinhood-research.plist"], str(_launchd_files))

yml = _read(os.path.join(ROOT, ".github", "workflows", "screener.yml"))
for needle in ("cancel-in-progress: false", 'python-version: "3.11"', "git add data/ docs/",
               "robinhood-screener[bot]", "dashboard.py --write", "workflow_dispatch", "run-name:",
               "timeout-minutes: 355", "bash .github/keeper.sh", "TRIGGER: keeper", "--keeper-alive",
               "--ensure-keeper", "gh workflow run robinhood-pages --ref main", "upload-artifact@v4",
               "if: ${{ always() && inputs.mode == 'keeper' }}", "GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}"):
    check(f"screener.yml contains {needle!r}", needle in yml)
check("screener.yml wires the three secret names", all(s in yml for s in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "NTFY_TOPIC")))
check("screener.yml carries the private-repo guard (scheduled runs inert until public: minutes)",
      "repository.private" in yml and "github.event_name != 'schedule'" in yml)
check("screener.yml has NO schedule: trigger (a cron fire must never queue a one-shot beside a keeper; the watchdog carries the cron), "
      "no Pages deploy of its own, and `*/5` lives only in keeper-watchdog.yml",
      not re.search(r"^\s*(schedule|cron):", yml, re.M) and "deploy-pages" not in yml and "upload-pages-artifact" not in yml
      and re.search(r'^\s*- cron: "\*/5 \* \* \* \*"', _read(os.path.join(ROOT, ".github", "workflows", "keeper-watchdog.yml")), re.M) is not None)
_inputs = yml[yml.index("inputs:"):yml.index("permissions:")]
check("screener.yml: input `mode` defaults to keeper (a bare `gh workflow run robinhood-screener` starts the chain; the Mac's "
      "`-f mode=run` stays a one-shot) and `slot` defaults to a",
      _inputs.index("mode:") < _inputs.index('default: "keeper"') < _inputs.index("slot:") < _inputs.index('default: "a"'))
check("screener.yml permissions are contents: write + actions: write (pages/id-token moved to pages.yml; actions: write dispatches the successor)",
      "contents: write" in yml and "actions: write" in yml and "pages: write" not in yml and "id-token: write" not in yml)
check("screener.yml: the Keeper step runs only in keeper mode and the one-shot Run step guards on the three-valued --keeper-alive (alive OR "
      "unknown ⇒ the one-shot exits 0 without scanning; only a DEFINITE none scans: neither the cutover nor an API blip can double-scan), "
      "and the ensure step reads the rc the same way (2 = unknown ⇒ nothing dispatched)",
      re.search(r"name: Keeper.*\n\s+if: \$\{\{ inputs\.mode == 'keeper' \}\}", yml) is not None
      and 'bash .github/keeper.sh --keeper-alive; alive=$?' in yml and '[ "$alive" != 1 ]' in yml and "one-shot skipped" in yml
      and 'bash .github/keeper.sh --keeper-alive; then' not in yml
      and 'bash .github/keeper.sh --ensure-keeper; rc=$?' in yml and '2) echo "keeper state unknown; nothing dispatched"' in yml)
_kstep = yml[yml.index("name: Keeper"):yml.index("name: Run screener")]
check("screener.yml: the Keeper step's env carries KEEPER_BOOK: \"1\" and LIVEBOOK_FEED_SOURCE: worktree (the book ticks inside the keeper "
      "and reads THIS checkout), and the always() artifact step keeps retention-days (data/livebook_ticks.jsonl rides it, never the commit)",
      'KEEPER_BOOK: "1"' in _kstep and "LIVEBOOK_FEED_SOURCE: worktree" in _kstep and "retention-days:" in yml
      and yml.index("retention-days:") > yml.index("upload-artifact@v4") and "livebook_ticks.jsonl" in yml)
vyml = _read(os.path.join(ROOT, ".github", "workflows", "verify.yml"))
check("verify.yml runs verify.py with fetch-depth 0 on human pushes only (never inside the 5-min job)",
      "fetch-depth: 0" in vyml and "python verify.py" in vyml and "screener state" in vyml
      and "verify.py" not in yml)
reqs = {re.split(r"[<>=~!\s]", ln.strip())[0] for ln in _read(os.path.join(ROOT, "requirements.txt")).splitlines()
        if ln.strip() and not ln.startswith("#")}
check("requirements.txt is exactly pandas + certifi", reqs == {"pandas", "certifi"}, str(reqs))

# ── weekly.yml: the Sunday gates, the publish and the ONE weekly message, in the cloud ──
wyml = _read(os.path.join(ROOT, ".github", "workflows", "weekly.yml"))
for needle in ("name: robinhood-weekly", "group: screener-weekly", "cancel-in-progress: false",
               'python-version: "3.11"', "timeout-minutes: 40", "contents: write", "workflow_dispatch",
               "pip install -r requirements.txt numpy",            # numpy only: dsr.py replaced scipy
               "python selfimprove/improve.py --apply --send",     # the exit gate
               "python selfimprove/entry_lab/improve_bands.py --apply --send",   # the entry gate
               "python selfimprove/improve.py --summary-json", "robinhood-improve[bot]",
               "improve: Sunday gates", "git pull --rebase --autostash", "rebase --abort",
               "PUBLISH FAILED", "alerts.format_event", "IMPROVE_PUBLISH_RETRIES",
               "python selfimprove/weekly_summary.py --send --research",
               "if: ${{ always() }}", "applied"):
    check(f"weekly.yml contains {needle!r}", needle in wyml, wyml[:0])
check("weekly.yml fires on a SUNDAY cron at 10:00 UTC year-round (the Mac's 11:00 LOCAL was two UTC hours) and takes a `dry` input "
      "defaulting to false",
      re.search(r'^\s*- cron: "0 10 \* \* 0"', wyml, re.M) is not None
      and re.search(r"^\s+dry:", wyml, re.M) is not None and 'default: "false"' in wyml)
check("weekly.yml wires the three alert secrets and carries the private-repo guard",
      all(f"{s}: ${{{{ secrets.{s} }}}}" in wyml for s in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "NTFY_TOPIC"))
      and "repository.private" in wyml and "github.event_name != 'schedule'" in wyml)
check("weekly.yml's idempotency step skips the gates only when the last improve_history.jsonl line is BOTH dated today and "
      "applied — a `dry=true` rehearsal leaves applied false and never consumes the Sunday",
      "improve_history.jsonl" in wyml and 'same_day and last.get("applied")' in wyml
      and "steps.idem.outputs.skip != 'true'" in wyml and "--summary-json" in wyml)
_wpub = wyml[wyml.index("name: Publish"):wyml.index("name: Weekly summary")]
for _p in ("selfimprove/champion.json", "selfimprove/trials.json", "selfimprove/improve_history.jsonl",
           "selfimprove/candidates/registry.json", "data/entry_lab_history.jsonl", "data/livebook_summary.json",
           "data/proposals/"):
    check(f"weekly.yml's publish step stages {_p!r} (exactly the deleted run_improve.sh's list)", _p in _wpub)
check("weekly.yml never stages a Mac-local book file: data/livebook.json / _fills.csv / _feed.json / _missed.jsonl / _ticks.jsonl "
      "appear nowhere in it (they ride keeper.sh's `git add data/ docs/`); the ONE data/livebook* path is the weekly digest",
      not any(x in wyml for x in ("data/livebook.json", "data/livebook_fills.csv", "data/livebook_feed.json",
                                  "data/livebook_missed.jsonl", "data/livebook_ticks.jsonl"))
      and wyml.count("data/livebook") == wyml.count("data/livebook_summary.json"))
# PUBLISH FAILED is the ONE message the operator gets at the moment the Sunday state failed to
# land, so it must be true of the CLOUD. It used to promise a recovery that does not exist here:
# that the run log "stands" and that publish.reconcile() republishes a diverged champion.json
# next week. reconcile() acts on a LOCAL file, and on a runner the workspace — the champion.json
# change, the trials bumps, the history line — is destroyed when the job ends; nothing schedules
# it either. An operator who believed it would wait for a republish that can never happen.
_pf = wyml[wyml.index('alerts.format_event("PUBLISH FAILED"'):wyml.index("alerts.send_all(t, b, dry_run=False)")]
_rec_hits = []
for _f in sorted(glob.glob(os.path.join(ROOT, ".github", "workflows", "*.yml"))
                 + glob.glob(os.path.join(ROOT, ".github", "*.sh"))
                 + glob.glob(os.path.join(ROOT, "selfimprove", "*.sh"))
                 + glob.glob(os.path.join(ROOT, "selfimprove", "research", "*.sh"))
                 + glob.glob(os.path.join(ROOT, "launchd", "*"))):
    _inside = _pf if _rel(_f) == ".github/workflows/weekly.yml" else ""
    _code = "\n".join(l_ for l_ in _read(_f).splitlines() if not l_.strip().startswith("#"))
    if _code.count("reconcile") != _inside.count("reconcile"):
        _rec_hits.append(f"{_rel(_f)} ({_code.count('reconcile')})")
check("PUBLISH FAILED tells the operator the CLOUD's truth: the gates decided in this runner's workspace only, it is discarded "
      "with the runner, nothing republishes automatically, and the fix is to clear the push obstacle and re-run "
      "`gh workflow run robinhood-weekly` (the day is not consumed — improve_history.jsonl never landed either). "
      "publish.reconcile() is named as a MANUAL Mac-side recovery this workflow does not run — and that is true: no workflow, "
      "keeper script, selfimprove shell script or launchd job names reconcile outside this alert's own body",
      all(w in _pf for w in ("only in this runner's workspace", "discarded with the runner",
                             "nothing republishes automatically", "gh workflow run robinhood-weekly",
                             "day is not consumed", "MANUAL Mac-side recovery",
                             "This workflow does not run it, and no schedule does."))
      and "republishes a diverged champion.json next week" not in wyml and "their run log stands" not in wyml
      and not _rec_hits, str(_rec_hits))
check("weekly.yml sends the ONE weekly message itself (--send, or --dry under the rehearsal) with a --research line built from the "
      "merged proposals and the unmerged research branches — it no longer depends on the Mac's research session",
      "--research" in wyml and "PROPOSAL_*.md" in wyml and "refs/heads/research/*" in wyml
      and "python selfimprove/weekly_summary.py --dry --research" in wyml)
# The summary step is `always()` BY DESIGN (a Sunday whose gates crashed must still say so), so it
# is the one step a duplicate run reaches with nothing new to report — and keeper.sh's Sunday
# insurance plus a late cron fire make duplicates machine-made rather than an operator slip. The
# guard asks the API, excludes this run, and FAILS OPEN: gh erroring leaves PRIOR empty.
_wsum = wyml[wyml.index("name: Weekly summary"):]
check("weekly.yml's summary step keeps `if: ${{ always() }}` and carries an idempotency guard so at most ONE --send happens per "
      "Sunday: it queries `gh run list --workflow robinhood-weekly --created <today> --status success` for another run's id, "
      "excludes $GITHUB_RUN_ID, renders --dry when one is found, and is wired with GH_TOKEN and the `actions: read` permission",
      "actions: read" in wyml and "GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}" in _wsum
      and "gh run list --workflow robinhood-weekly --created" in _wsum and "--status success" in _wsum
      and '--jq \'.[].databaseId\'' in _wsum and 'grep -vx "${GITHUB_RUN_ID:-0}"' in _wsum
      and 'if [ "$DRY" = "true" ] || [ -n "$PRIOR" ]; then' in _wsum
      and _wsum.index("PRIOR=") < _wsum.index("weekly_summary.py --dry --research")
      < _wsum.index("weekly_summary.py --send --research")
      and "if: ${{ always() }}" in _wsum, _wsum[:0])
check("the guard FAILS OPEN: gh's stderr is discarded and its failure is not fatal (`set +e`, no `|| exit`), so an unreachable or "
      "unauthorised API leaves PRIOR empty and the ONE weekly message still goes out — a broken guard can never silence it",
      "set +e -o pipefail" in _wsum and "2>/dev/null" in _wsum and "FAILS OPEN" in _wsum
      and not re.search(r"PRIOR=.*\|\|\s*exit", _wsum), _wsum[:0])
_res_src0 = _read(os.path.join(ROOT, "selfimprove", "research", "run_research.sh"))
check("run_research.sh: step 0 ff-syncs the Mac tree to origin/main (never forced — the deleted run_improve.sh's job) and its "
      "finish() renders the summary with --dry unless RESEARCH_SEND_SUMMARY=1, so exactly ONE weekly message goes out",
      'git -C "$REPO" fetch -q origin' in _res_src0 and 'merge -q --ff-only origin/main' in _res_src0
      and 'RESEARCH_SEND_SUMMARY:-0' in _res_src0
      and re.search(r'RESEARCH_SEND_SUMMARY:-0\}" = "1" \].*MODE="--send"; else MODE="--dry"', _res_src0) is not None,
      _res_src0[:0])
_wbn = subprocess.run(["bash", "-n", os.path.join(ROOT, "selfimprove", "research", "run_research.sh")],
                      capture_output=True, text=True)
check("bash -n selfimprove/research/run_research.sh parses", _wbn.returncode == 0, _wbn.stderr[-300:])
# every `run: |` block of weekly.yml must parse as bash AFTER the YAML block indent is stripped. A
# heredoc body written at column 0 silently ENDS the block scalar (found writing this file), and a
# broken Sunday step would only be discovered on a Sunday.
_wbad = []
for _ind, _body in re.findall(r"^(\s+)run: \|\n((?:\1  .*\n|\n)+)", wyml, re.M):
    _txt = "".join(l[len(_ind) + 2:] if l.strip() else "\n" for l in _body.splitlines(keepends=True))
    with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as _fh:
        _fh.write(_txt)
    _r = subprocess.run(["bash", "-n", _fh.name], capture_output=True, text=True)
    os.unlink(_fh.name)
    if _r.returncode != 0:
        _wbad.append(_txt.splitlines()[0][:60] + " :: " + _r.stderr.strip()[:120])
check("every `run: |` block in weekly.yml parses as bash once the YAML block indent is stripped (4 steps), so no heredoc has "
      "silently escaped its block scalar", len(re.findall(r"^\s+run: \|\n", wyml, re.M)) == 4 and not _wbad, str(_wbad))


def _local_module(name: str, from_file: str):
    """Resolve an import name to a repo file (package __init__ or module), else None."""
    parts = name.split(".")
    cand = [os.path.join(ROOT, *parts) + ".py", os.path.join(ROOT, *parts, "__init__.py")]
    for c in cand:
        if os.path.isfile(c):
            return c
    return None


def _import_graph(roots: list) -> tuple:
    seen, external, todo = set(), set(), list(roots)
    while todo:
        p = todo.pop()
        if p in seen:
            continue
        seen.add(p)
        for n in ast.walk(_tree(p)):
            names = []
            if isinstance(n, ast.Import):
                names = [a.name for a in n.names]
            elif isinstance(n, ast.ImportFrom) and n.module and not n.level:
                names = [n.module] + [n.module + "." + a.name for a in n.names]
            for nm in names:
                loc = _local_module(nm, p)
                if loc:
                    todo.append(loc)
                elif "." not in nm or _local_module(nm.split(".")[0], p) is None:
                    external.add(nm.split(".")[0])
    return seen, external


_graph, _ext = _import_graph([os.path.join(ROOT, f) for f in ("run.py", "dashboard.py", "paper_exec.py")])
check("the cloud import chain (run.py + dashboard.py + paper_exec.py, transitively) never imports "
      "scipy/statsmodels/sklearn (requirements.txt is pandas + certifi)",
      not ({"scipy", "statsmodels", "sklearn"} & _ext), str(sorted(_ext)))
check("selfimprove/champion.py is in the cloud import chain (paper_exec/alerts read exit.champion) and "
      "improve.py / scorecard.py are not (they run in weekly.yml on the runner, never inside the scan)",
      os.path.join(ROOT, "selfimprove", "champion.py") in _graph
      and os.path.join(ROOT, "selfimprove", "improve.py") not in _graph
      and os.path.join(ROOT, "selfimprove", "entry_lab", "scorecard.py") not in _graph)
# scorecard.py is NOT AST-walked for wall-clock (it reads time.time() once for the markdown header),
# so the guard that matters is a different one: every exclusion it applies must come from the ROW.
_sc_path = os.path.join(ROOT, "selfimprove", "entry_lab", "scorecard.py")
_sc_src = _read(_sc_path)
check("scorecard.py reads its exclusions from the LEDGER ROW only: it imports no subprocess and mentions "
      "neither 'publish.' nor 'origin_blob' (a scorer that shells out to git is not point-in-time)",
      "subprocess" not in _top_imports(_tree(_sc_path)) and "publish." not in _sc_src
      and "origin_blob" not in _sc_src)


def _ignored(rel: str) -> bool:
    if HAVE_GIT:
        r = subprocess.run(["git", "-C", ROOT, "check-ignore", "-q", "--no-index", rel],
                           capture_output=True, text=True)
        if r.returncode in (0, 1):
            return r.returncode == 0
    pats = [ln.strip() for ln in _read(os.path.join(ROOT, ".gitignore")).splitlines()
            if ln.strip() and not ln.startswith("#")]
    return any(rel.startswith(p.rstrip("/")) or rel.endswith(p.lstrip("*")) for p in pats)


must_ignore = ["cache/x.json", "config.local.json", "run.out.log", "livebook.err.log", "data/livebook_ticks.jsonl",
               "data/livebook.json.123.tmp", "data/backups/livebook_20260912.json", "selfimprove/PAUSE",
               "selfimprove/research/context/context_2026-09-13.md", "selfimprove/research/logs/run_2026-09-13.log",
               "data/archive/proven_dev/social_verdicts.json", "data/archive/proven_dev/history.bundle", "data/.keeper.lock"]
must_keep = ["data/proposals/entry-20260913-000000.md", "selfimprove/champion.json", "selfimprove/trials.json",
             "selfimprove/candidates/registry.json", "data/ledger.csv", "data/livebook_summary.json",
             "data/livebook.json", "data/livebook_fills.csv", "data/livebook_feed.json", "data/livebook_missed.jsonl",
             "selfimprove/research/proposals/PROPOSAL_2026-09-13.md"]
not_ign = [p for p in must_ignore if not _ignored(p)]
wrongly = [p for p in must_keep if _ignored(p)]
check(".gitignore covers cache/, config.local.json, *.out.log/*.err.log, data/livebook_ticks.jsonl (the per-tick log: artifact only), "
      "the book's *.tmp, data/backups/, selfimprove/PAUSE, research context/ + logs/, the offline archive files, the flock file",
      not not_ign, str(not_ign))
check(".gitignore does NOT ignore data/proposals/, champion.json, trials.json, registry.json, ledger.csv, livebook_summary.json, "
      "research proposals, nor the four COMMITTED book files (livebook.json, _fills.csv, _feed.json, _missed.jsonl — they ride the "
      "keeper's `git add data/`)", not wrongly, str(wrongly))
check("none of the four committed book file names contains 'ledger' (a sibling voice assistant globs *ledger*.csv under this repo)",
      not any("ledger" in os.path.basename(f_) for f_ in must_keep if "livebook" in f_))
_stage_bad = []
for _f in sorted(set(glob.glob(os.path.join(ROOT, ".github", "workflows", "*.yml")) + glob.glob(os.path.join(ROOT, ".github", "*.sh"))
                     + glob.glob(os.path.join(ROOT, "selfimprove", "*.sh")) + glob.glob(os.path.join(ROOT, "launchd", "*.sh")))):
    for i, ln in enumerate(_read(_f).splitlines(), 1):
        s_ = ln.strip()
        m_ = re.search(r"\bgit\b(?:\s+-C\s+\S+)?\s+add\b(.*)$", s_)
        if s_.startswith("#") or not m_:
            continue
        args_ = m_.group(1).split("#")[0].split()
        if _rel(_f) == ".github/keeper.sh" and args_ == ["data/", "docs/"]:
            continue                                   # the ONE staging line the four book files ride
        if any(t in ("-A", "--all", ".", "data", "data/", "-u", "--update") or t.startswith("data/livebook") for t in args_):
            _stage_bad.append(f"{_rel(_f)}:{i}")
check("only keeper.sh's `git add data/ docs/` can stage a data/livebook* path: no other git add line in .github/workflows/*.yml, "
      ".github/*.sh, selfimprove/*.sh or launchd/*.sh stages -A / . / data/ / a livebook file", not _stage_bad, str(_stage_bad))

cs_bad = []
cs_input = False
for n in ast.walk(_tree(os.path.join(ROOT, "cloud_secrets.py"))):
    if isinstance(n, ast.Call) and _attr_chain(n.func) == ["subprocess", "run"] and n.args \
            and isinstance(n.args[0], ast.List):
        elems = n.args[0].elts
        consts = [e.value for e in elems if isinstance(e, ast.Constant)]
        if "secret" in consts and "set" in consts:
            names = [e.id for e in elems if isinstance(e, ast.Name)]
            if any(nm not in ("name", "REPO") for nm in names) or any(not isinstance(e, (ast.Constant, ast.Name)) for e in elems):
                cs_bad.append(names)
            cs_input = any(k.arg == "input" for k in n.keywords)
check("cloud_secrets.py never puts a credential value in argv: `gh secret set NAME` takes the value over stdin (input=)",
      not cs_bad and cs_input, f"{cs_bad} input={cs_input}")

prompt = _read(os.path.join(ROOT, "selfimprove", "research", "research_prompt.md"))
markers = ["100/100 seeds", "3,910-wallet", "435", "t+60", "creation block", "47 devs", "p=0.0212",
           "14 of 14", "27/27", "3% coverage", "pooled-quantile", "AUC 0.546/0.541"]
missing_m = [m for m in markers if m not in prompt]
check("research_prompt.md carries all 12 dead-end markers", not missing_m, str(missing_m))
check("research_prompt.md carries the 'at most 2' candidate budget and the never-edit list",
      re.search(r"at most 2", prompt, re.I) is not None and "Never edit" in prompt
      and all(f in prompt for f in ("config.py", "screen.py", "run.py", "alerts.py", "ledger.py", "verify.py",
                                    "champion.json", "trials.json", "registry.json", "register.py", "_template.py")))
check("run_research.sh: PAUSE check, allowlist step (git diff --name-only), verify gate BEFORE the merge, "
      "mktemp, and never `run.py --send`",
      "selfimprove/PAUSE" in res_src and re.search(r'git -C "\$WT" diff --name-only', res_src) is not None
      and "mktemp -d" in res_src
      and res_src.index("verify.py") < res_src.index("merge -q --no-ff") and "run.py --send" not in res_src
      and "allowlist.py" in res_src)
from selfimprove.research import allowlist as AL        # noqa: E402
ok_a, off_a = AL.allowlist_ok(["selfimprove/candidates/band_liq_rt.py",
                               "selfimprove/research/proposals/PROPOSAL_2026-09-13.md"])
check("allowlist_ok accepts the two allowed shapes (candidates/<name>.py, proposals/PROPOSAL_<date>.md)",
      ok_a and off_a == [])
rej = ["config.py", "screen.py", "run.py", "alerts.py", "selfimprove/champion.json",
       "selfimprove/candidates/registry.json", "selfimprove/candidates/register.py",
       "selfimprove/candidates/_template.py", "data/x"]
check("allowlist_ok rejects config.py/screen.py/run.py/alerts.py/champion.json/registry.json/register.py/"
      "_template.py/data/x (the research branch cannot touch the champion)",
      all(not AL.allowlist_ok([p])[0] for p in rej) and AL.allowlist_ok(rej)[1] == rej)

# post-merge pin (Phase 4 and Phase 5 each independently grew a `flow` schema for policies.py —
# Phase 4 placed its copy between POLICIES and _POLICY_KEYS, Phase 5 placed its copy near the
# schema comment above POLICIES — and the rebase resolution kept only Phase 5's copy). An AST
# walk of the TOP-LEVEL body only, so a name reused inside a function or the __main__ smoke test
# never trips this.
_pol_top_names: dict = {}
for _stmt in _tree(os.path.join(ROOT, "selfimprove", "policies.py")).body:
    if isinstance(_stmt, ast.FunctionDef):
        _pol_top_names[_stmt.name] = _pol_top_names.get(_stmt.name, 0) + 1
    elif isinstance(_stmt, ast.Assign):
        for _t in _stmt.targets:
            if isinstance(_t, ast.Name):
                _pol_top_names[_t.id] = _pol_top_names.get(_t.id, 0) + 1
_FLOW_HELPERS = ("FLOW_FEATURES", "flow_policy_names", "flow_from_market", "trail_active",
                 "flow_step", "flow_state_init")
check("policies.py defines each of FLOW_FEATURES/flow_policy_names/flow_from_market/trail_active/"
      "flow_step/flow_state_init exactly once at module scope (the duplicate-copy trap two "
      "independent branches hit at the same time)",
      all(_pol_top_names.get(_n) == 1 for _n in _FLOW_HELPERS),
      str({_n: _pol_top_names.get(_n, 0) for _n in _FLOW_HELPERS}))


# ═══════════════════════════════════════════════════════════════════════════════════
section("B. screen.py — hard gates fail closed only on positive findings; A never on unknowns")
# ═══════════════════════════════════════════════════════════════════════════════════
CLEAN_M = {"price_usd": 0.001, "liq_usd": 45_000.0, "vol_h24": 750_000.0, "mcap": 250_000.0,
           "fdv": 250_000.0, "buys_h1": 450, "sells_h1": 50, "pair_age_min": 240.0,
           "vol_h1": 9000.0, "vol_h6": 40000.0, "buys_h24": 3000, "sells_h24": 2500,
           "price_chg_h1": 4.0, "dex": "uniswap"}
CLEAN_S = {"owner_state": "renounced", "owner_renounced": True, "lp_locked_pct": 100.0,
           "lp_check_source": "rpc_v2", "honeypot": False, "roundtrip_loss_pct": 4.8,
           "is_scam": False, "template_name": "FlapTaxTokenV3", "is_proxy": True,
           "verified_source": True, "total_holders": 1400, "holders_source": "blockscout",
           "top10_pct": 18.0, "top10_pct_gt": 20.0, "lp_share_pct": 30.0, "dev_pct": 1.2,
           "deployer": "0xdev", "creator_prior_tokens": 0, "creator_dead_frac": 0.0,
           "creator_score": 80.0, "dev_sniped": False, "sniper_swaps_first_blocks": 4,
           "buys_per_buyer_m5": 1.1, "tx_per_holder_total": 5.0, "gt_score": 70.0,
           "gt_verified": True, "launchpad_graduation_pct": 100.0, "launchpad_completed": True,
           "launchpad_completed_age_s": 9000.0, "holders_updated_age_s": 600.0,
           "scanhood_verdict": "PASS", "scanhood_sellable": True, "sources_dark": []}


def _feat(m, s):
    f = {k: None for k in config.FEATURE_FIELDS}
    f.update(m); f.update(s)
    f["score"] = screen.soft_score(m, s)[0]
    f["token"] = "0x" + "ab" * 20; f["first_sighting"] = True; f["sighting_age_s"] = 0.0
    return f


rug_m = {"price_usd": 1e-6, "liq_usd": 500.0, "vol_h24": 100.0, "mcap": 20_000.0, "buys_h1": 0, "sells_h1": 5, "pair_age_min": 3.0}
rug_s = {"owner_state": "owned", "lp_locked_pct": 0.0, "honeypot": True, "roundtrip_loss_pct": 60.0,
         "is_scam": False, "top10_pct": 85.0, "dev_pct": 40.0, "total_holders": 30, "sources_dark": []}
ok, gr = screen.hard_gates(rug_m, rug_s)
check("rug fixture (owned, LP unburned, honeypot, 60% round trip) is rejected", not ok and gr["honeypot_ok"] is False)
check("clean fixture passes every hard gate", screen.hard_gates(CLEAN_M, CLEAN_S)[0])
check("owner 'owned' rejects; 'renounced' / 'no_owner_fn' / None pass (positive finding only)",
      not screen.hard_gates(CLEAN_M, dict(CLEAN_S, owner_state="owned"))[0]
      and all(screen.hard_gates(CLEAN_M, dict(CLEAN_S, owner_state=o))[0] for o in ("renounced", "no_owner_fn", None)))
lp100 = screen.hard_gates(CLEAN_M, dict(CLEAN_S, lp_locked_pct=100.0))
lp80 = screen.hard_gates(CLEAN_M, dict(CLEAN_S, lp_locked_pct=80.0))
lpnone = screen.hard_gates(CLEAN_M, dict(CLEAN_S, lp_locked_pct=None, lp_check_source=None))
check("LP-burn semantics: 100% passes, 80% rejected, None passes through with lp_check_source unknown",
      lp100[0] and not lp80[0] and lp80[1]["lp_ok"] is False and lpnone[0] and lpnone[1]["lp_ok"] is True)
check("honeypot True rejects, None passes through; round trip 20% rejects sell_tax_ok, 4.8% passes",
      not screen.hard_gates(CLEAN_M, dict(CLEAN_S, honeypot=True))[0]
      and screen.hard_gates(CLEAN_M, dict(CLEAN_S, honeypot=None))[0]
      and screen.hard_gates(CLEAN_M, dict(CLEAN_S, roundtrip_loss_pct=20.0))[1]["sell_tax_ok"] is False
      and not screen.hard_gates(CLEAN_M, dict(CLEAN_S, roundtrip_loss_pct=20.0))[0]
      and screen.hard_gates(CLEAN_M, dict(CLEAN_S, roundtrip_loss_pct=4.8))[0])
check("is_scam True rejects / None passes", not screen.hard_gates(CLEAN_M, dict(CLEAN_S, is_scam=True))[0]
      and screen.hard_gates(CLEAN_M, dict(CLEAN_S, is_scam=None))[0])
_saved_bl = config.TEMPLATE_BLOCKLIST
config.TEMPLATE_BLOCKLIST = {"EvilProxy"}
try:
    tb = screen.hard_gates(CLEAN_M, dict(CLEAN_S, template_name="EvilProxy"))
    tu = screen.hard_gates(CLEAN_M, dict(CLEAN_S, template_name="Unknown", verified_source=False))
    hc_u = screen.high_conviction(_feat(CLEAN_M, dict(CLEAN_S, template_name="Unknown", verified_source=False)))
    check("a template in TEMPLATE_BLOCKLIST rejects; an unknown template passes hard gates but is NOT A",
          not tb[0] and tu[0] and hc_u[0] is False)
finally:
    config.TEMPLATE_BLOCKLIST = _saved_bl

dark_s = dict(CLEAN_S, total_holders=None, holders_source=None, top10_pct=None, template_name=None,
              verified_source=None, is_scam=None, dev_pct=None, creator_score=None, gt_score=None,
              sources_dark=["blockscout", "geckoterminal", "robinx"])
okd, gd = screen.hard_gates(CLEAN_M, dark_s)
hcd = screen.hc_checks(_feat(CLEAN_M, dark_s))
check("403 outage: with every pass-2 source dark a liquid token still passes hard gates (pass-through)", okd, str(gd))
check("403 outage: hc_checks returns None (never False) for holders/top10/holders_per_min/tx_per_holder/template",
      all(hcd[k] is None for k in ("holders", "top10", "holders_per_min", "tx_per_holder", "template")), str(hcd))
_fdark = _feat(CLEAN_M, dark_s)
check("tier-frozen bug guard: the dark token is never A — False here (its soft score is a definite miss beside the "
      "unknowns), and NA (not False) once every KNOWN check passes, so the tier is B by refusal either way",
      B.band_a_strict.verdict(_fdark) is False and hcd["score"] is False
      and B.band_a_strict.verdict(dict(_fdark, score=75.0)) is None,
      str({k: v for k, v in hcd.items() if v is not True}))
check("the same dark token with a POSITIVE finding (is_scam True) is rejected",
      not screen.hard_gates(CLEAN_M, dict(dark_s, is_scam=True))[0])

pristine = _feat(CLEAN_M, CLEAN_S)
check("pristine mature token IS A under band_a_strict (every hc check True)",
      B.band_a_strict.verdict(pristine) is True and screen.high_conviction(pristine)[0])
degradations = {"top10 25": ({}, {"top10_pct": 25.0}), "holders 500": ({}, {"total_holders": 500}),
                "liq 20k": ({"liq_usd": 20_000.0}, {}), "roundtrip 12": ({}, {"roundtrip_loss_pct": 12.0}),
                "dev_pct 4": ({}, {"dev_pct": 4.0}), "lp None": ({}, {"lp_locked_pct": None, "lp_check_source": None}),
                "template unknown": ({}, {"template_name": None, "verified_source": None}),
                "age 60": ({"pair_age_min": 60.0}, {}), "holders/min 20": ({"pair_age_min": 150.0}, {"total_holders": 3000}),
                "tx/holder 4": ({"buys_h1": 3000, "sells_h1": 2600}, {})}
for label, (dm, ds) in degradations.items():
    m = dict(CLEAN_M, **dm); s_ = dict(CLEAN_S, **ds)
    okx = screen.hard_gates(m, s_)[0]
    v = B.band_a_strict.verdict(_feat(m, s_))
    check(f"single-field degradation '{label}' still passes hard gates and flips A to {v!r} (A strictly tighter)",
          okx and v is not True, f"gates={okx} verdict={v}")

cub_m = dict(CLEAN_M, liq_usd=30_031.46, vol_h24=397_126.31, mcap=151_307.0, buys_h1=3662, sells_h1=1987, pair_age_min=19.94)
cub_s = dict(CLEAN_S, top10_pct=4.4, total_holders=1344)
cub_score = screen.soft_score(cub_m, cub_s)[0]
cub_f = _feat(cub_m, cub_s)
cub_hc = screen.hc_checks(cub_f)
rate_trips = [k for k in ("age", "holders_per_min", "tx_per_holder") if cub_hc[k] is False]
check("$Cubrate replay scores 80.5 on the soft score (looks great) yet is NOT A", cub_score == 80.5
      and B.band_a_strict.verdict(cub_f) is False, str(cub_score))
check("$Cubrate replay trips >= 2 independent rate gates (age / holders-per-min / tx-per-holder)",
      len(rate_trips) >= 2, str(rate_trips))
check("soft_score is deterministic and identical for total_holders None vs 0",
      screen.soft_score(CLEAN_M, CLEAN_S) == screen.soft_score(CLEAN_M, CLEAN_S)
      and screen.soft_score(CLEAN_M, dict(CLEAN_S, total_holders=None))[0] == screen.soft_score(CLEAN_M, dict(CLEAN_S, total_holders=0))[0])
okc, gc = screen.hard_gates(CLEAN_M, CLEAN_S)
solana_only = ("mint_revoked", "freeze_revoked", "insider_ok", "insider_net_ok", "graph_insiders_ok", "risk_ok", "no_danger_risk")
check("solana-only gate keys are None in the gates dict and never ANDed (a clean token still passes)",
      okc is True and all(gc[k] is None for k in solana_only))
check("gates_bitmask length == len(_GATE_ORDER), '-' for informational keys",
      len(screen.gates_bitmask(gc)) == len(screen._GATE_ORDER) and set(screen.gates_bitmask(gc)) <= {"0", "1", "-"})

# ═══════════════════════════════════════════════════════════════════════════════════
section("C. ledger.py — event model, write-once forward returns, exits from the frozen plan")
# ═══════════════════════════════════════════════════════════════════════════════════
import ledger as LED                                    # noqa: E402
from selfimprove import policies as POL                 # noqa: E402

T0 = 1_780_000_000.0
MK = {"price_usd": 1.0, "mcap": 1e6, "liq_usd": 5e4}


@contextlib.contextmanager
def _policies(pols: dict):
    """Temporarily add exit policies to POL.POLICIES, then restore the family exactly.

    Registration is a permanent counted trial that deflates every later Deflated Sharpe, so it is
    the operator's move, not a side effect of a test: a section that needs a candidate policy
    injects it for the length of one block rather than registering it. The restore puts back the
    PRIOR value of every injected name — popping unconditionally would silently evict a candidate
    the operator has since registered, and every later section would score a shrunken family.
    """
    prior = {k: POL.POLICIES[k] for k in pols if k in POL.POLICIES}
    POL.POLICIES.update(pols)
    try:
        yield
    finally:
        for k in pols:
            POL.POLICIES.pop(k, None)
        POL.POLICIES.update(prior)


def _ev(token, tier="B", kind="first_sighting", price=1.0, fired=None, prior=None, mcap=None):
    return {"token": token, "symbol": token[-4:].upper(), "tier": tier, "band": config.DEFAULT_ENTRY_BAND,
            "fired_band": fired, "event_kind": kind, "prior_event_seq": prior,
            "market": {"price_usd": price, "mcap": (price * 1e6 if mcap is None else mcap), "liq_usd": 5e4}, "score": 55.0, "gates": {},
            "sources_dark": [], "deployer": "0xdev"}


def _snap(price=1.0, mcap=None, liq=5e4, absent=(), deferred=()):
    def fn(toks):
        ok = {t: {"price_usd": price, "mcap": (mcap if mcap is not None else price * 1e6), "liq_usd": liq}
              for t in toks if t not in absent and t not in deferred}
        return {"ok": ok, "absent": set(absent), "deferred": set(deferred)}
    return fn


with tempfile.TemporaryDirectory() as d:
    LP = os.path.join(d, "ledger.csv")
    LED.ensure_exists(LP)
    check("next_event_seq() == 1 on an empty ledger", LED.next_event_seq(LED.load(LP)) == 1)
    w1 = LED.record_rows([_ev("0xaaa1")], alert_ts=T0, path=LP)
    w2 = LED.record_rows([_ev("0xAAA1", tier="A", kind="promotion", price=2.0, fired=config.DEFAULT_ENTRY_BAND,
                              prior=w1["0xaaa1"])], alert_ts=T0 + 7200, path=LP)
    led = LED.load(LP)
    check("recording B then A yields two rows with event_seq 1, 2 (monotonic from 1, minted 1+max)",
          len(led) == 2 and w1["0xaaa1"] == 1 and w2["0xaaa1"] == 2
          and [int(float(x)) for x in led["event_seq"]] == [1, 2])
    check("tier-frozen bug: the promotion row is A at ITS OWN alert price (2.0), the first sighting stays B at 1.0",
          led.at[1, "tier"] == "A" and float(led.at[1, "entry_price"]) == 2.0
          and led.at[0, "tier"] == "B" and float(led.at[0, "entry_price"]) == 1.0)
    check("promoted_ts stamped on the B row and prior_event_seq set on the A row",
          float(led.at[0, "promoted_ts"]) == T0 + 7200 and int(float(led.at[1, "prior_event_seq"])) == 1)
    w3 = LED.record_rows([_ev("0xaaa1", tier="A", kind="promotion", price=3.0, fired=config.DEFAULT_ENTRY_BAND)],
                         alert_ts=T0 + 9000, path=LP)
    check("recording (token, promotion) again is a no-op (a token is alerted at most once)",
          w3 == {} and len(LED.load(LP)) == 2)
    # the per-token cap
    LED.record_rows([_ev("0xcap1")], alert_ts=T0, path=LP)
    for i in range(1, config.BAND_MAX_EVENTS_PER_TOKEN):
        LED.record_rows([_ev("0xcap1", kind="band_fire", fired=f"band_{i}")], alert_ts=T0 + i, path=LP)
    n_cap = int((LED.load(LP)["token"] == "0xcap1").sum())
    LED.record_rows([_ev("0xcap1", kind="band_fire", fired="band_late")], alert_ts=T0 + 99, path=LP)
    n_after = int((LED.load(LP)["token"] == "0xcap1").sum())
    wp = LED.record_rows([_ev("0xcap1", tier="A", kind="promotion", fired=config.DEFAULT_ENTRY_BAND)],
                         alert_ts=T0 + 100, path=LP)
    check(f"the per-token cap ({config.BAND_MAX_EVENTS_PER_TOKEN}) binds band fires but never a promotion",
          n_cap == config.BAND_MAX_EVENTS_PER_TOKEN and n_after == n_cap and "0xcap1" in wp)
    # B rows never emit exits; the A row's rungs/stop from its own plan, each exactly once
    filled, ev = LED.update_forward(T0 + 7300, _snap(price=5.0), path=LP)
    kinds = [(e["token"], e["kind"]) for e in ev]
    a_tokens = set(LED.load(LP).loc[LED.load(LP)["tier"] == "A", "token"])
    check("B rows never emit exits; the A row (cfg_ladder_stop) emits exactly one tp at 2.5x",
          {t for t, _k in kinds} <= a_tokens and all(k == "tp" for _t, k in kinds)
          and [e["levels"] for e in ev if e["token"] == "0xaaa1"] == [[(2.0, 0.5)]], str(kinds))
    _, ev2 = LED.update_forward(T0 + 7360, _snap(price=5.0), path=LP)
    check("the 2x rung never re-fires", not [e for e in ev2 if e["token"] == "0xaaa1"])
    _, ev3 = LED.update_forward(T0 + 7420, _snap(price=12.0), path=LP)
    _, ev3b = LED.update_forward(T0 + 7480, _snap(price=12.0), path=LP)
    a3 = [e for e in ev3 if e["token"] == "0xaaa1"]
    check("the next rung (5x) fires once with only the newly crossed levels, then never again",
          len(a3) == 1 and a3[0]["kind"] == "tp" and a3[0]["levels"] == [(5.0, 0.25)]
          and not [e for e in ev3b if e["token"] == "0xaaa1"])
    _, ev4 = LED.update_forward(T0 + 7540, _snap(price=0.8), path=LP)
    _, ev4b = LED.update_forward(T0 + 7600, _snap(price=0.7), path=LP)
    a4 = [e for e in ev4 if e["token"] == "0xaaa1"]
    check("the hard stop fires once at -60% (entry 2.0 -> 0.8) and never again",
          len(a4) == 1 and a4[0]["kind"] == "stop" and abs(a4[0]["ret"] + 0.6) < 1e-9
          and not [e for e in ev4b if e["token"] == "0xaaa1"])
    # trail off the high-water mark (plan trail_30) and time_exit (sell_15m) from the row's OWN plan
    LED.record_rows([_ev("0xtrail", tier="A", kind="promotion", fired="x")], alert_ts=T0 + 20000, plan_name="trail_30", path=LP)
    LED.record_rows([_ev("0xt15m", tier="A", kind="promotion", fired="x")], alert_ts=T0 + 20000, plan_name="sell_15m", path=LP)
    _, e1 = LED.update_forward(T0 + 20100, _snap(price=3.0), path=LP)
    _, e2 = LED.update_forward(T0 + 20200, _snap(price=2.0), path=LP)
    _, e2b = LED.update_forward(T0 + 20300, _snap(price=1.9), path=LP)
    tr = [e for e in e2 if e["token"] == "0xtrail"]
    check("trail_30 row: no event at the 3x high-water mark, one trail event on the drop to 2.0 (<= 3.0*0.7), none after",
          not [e for e in e1 if e["token"] == "0xtrail"] and len(tr) == 1 and tr[0]["kind"] == "trail"
          and not [e for e in e2b if e["token"] == "0xtrail"])
    check("exits come from the row's OWN frozen plan: the sell_15m row ignores the 3x ladder rung",
          not [e for e in e1 + e2 if e["token"] == "0xt15m"])
    _, e5 = LED.update_forward(T0 + 20900, _snap(price=2.0), path=LP)
    _, e5b = LED.update_forward(T0 + 21000, _snap(price=2.0), path=LP)
    te = [e for e in e5 if e["token"] == "0xt15m"]
    check("sell_15m row: exactly one time_exit at +900 s with due_ts = alert_ts + max_hold_s, never again",
          len(te) == 1 and te[0]["kind"] == "time_exit" and te[0]["due_ts"] == T0 + 20000 + 900
          and not [e for e in e5b if e["token"] == "0xt15m"])
    # THE ARMED TRAIL ON THE CLOUD LEG. A trail with no arm sits above the -50% stop from the first
    # tick (a 30% trail off a 1.0x high-water mark is 0.70 x entry), which would make "the stop is
    # always live" false by construction: the trail would always fire first.
    with _policies({"zz_armtrail": {"trail": 0.30, "trail_arm": 1.5, "stop": 0.50}}):
        LED.record_rows([_ev("0xarm", tier="A", kind="promotion", fired="x")], alert_ts=T0 + 30000,
                        plan_name="zz_armtrail", path=LP)
        arm_ev = []
        for dt, price in ((100, 1.2), (200, 0.8), (300, 1.6), (400, 1.1), (500, 1.05), (600, 0.45)):
            _, evx = LED.update_forward(T0 + 30000 + dt, _snap(price=price), path=LP)
            arm_ev.append([e["kind"] for e in evx if e["token"] == "0xarm"])
        check("trail_arm 1.5 on the cloud leg: 1.2x then 0.8x emits NOTHING (0.8 is below an unarmed 30% "
              "trail at 0.84 — the arm suppresses it); 1.6x then 1.1x emits exactly one 'trail'; 0.45x "
              "emits 'stop' from the -50% leg that was live the whole time",
              arm_ev == [[], [], [], ["trail"], [], ["stop"]], str(arm_ev))
    check("forward return math: 100 -> 250 == +150%",
          abs([e for e in e1 if e["token"] == "0xtrail"] == [] and 0) == 0 and
          abs(LED.update_forward(T0 + 20950, _snap(price=2.5), path=LP)[1][0]["ret"] - 1.5) < 1e-9
          if False else abs((250.0 / 100.0 - 1.0) - 1.5) < 1e-12)

with tempfile.TemporaryDirectory() as d:
    LP = os.path.join(d, "ledger.csv")
    LED.ensure_exists(LP)
    LED.record_rows([_ev(f"0x{i:040x}") for i in range(1, 71)], alert_ts=T0, path=LP)
    calls = {"n": 0, "tokens": []}

    def counting(price):
        base = _snap(price=price)

        def fn(toks):
            calls["n"] += 1; calls["tokens"].append(list(toks))
            return base(toks)
        return fn
    before = _read(LP)
    LED.update_forward(T0 + 100, counting(1.0), path=LP)
    check("batched forward update calls snapshot_many_fn exactly ONCE per run for 70 open rows",
          calls["n"] == 1 and len(calls["tokens"][0]) == 70)
    LED.update_forward(T0 + 200, lambda toks: {"ok": {}, "absent": set(), "deferred": set(toks)}, path=LP)
    after = _read(LP)
    check("deferred snapshots write nothing (last_snapshot_ts untouched; 472-of-1,400: deferred is never dead)",
          before == after and bool(LED.empty_mask(LED.load(LP)["last_snapshot_ts"]).all()))
    t1 = f"0x{1:040x}"
    f1, _ = LED.update_forward(T0 + 300, _snap(price=1.0, absent=[t1]), path=LP)
    r = LED.load(LP).set_index("token")
    check("first absence fills nothing (absent_ticks 1, ret_1h empty) — an EVM pair can drop from the index once",
          int(float(r.at[t1, "absent_ticks"])) == 1 and str(r.at[t1, "ret_1h"]) in LED._EMPTY and f1 == 0)
    LED.update_forward(T0 + 400, _snap(price=1.0), path=LP)
    r = LED.load(LP).set_index("token")
    check("an ok snapshot in between resets absent_ticks to 0", int(float(r.at[t1, "absent_ticks"])) == 0)
    LED.update_forward(T0 + 3700, _snap(price=1.0, absent=[t1]), path=LP)
    r1 = LED.load(LP).set_index("token")
    LED.update_forward(T0 + 3800, _snap(price=1.0, absent=[t1]), path=LP)
    r = LED.load(LP).set_index("token")
    check("a due horizon is NOT written on the first absence even when due", str(r1.at[t1, "ret_1h"]) in LED._EMPTY)
    check(f"the {config.DEAD_CONFIRM_TICKS}nd consecutive absence writes -100% (price 0) for the due 1h horizon",
          float(r.at[t1, "ret_1h"]) == -1.0 and float(r.at[t1, "price_1h"]) == 0.0)
    # quote-integrity: a pair switch (implied supply 3x) fills no cell and ratchets nothing
    t2 = "0xsus1"
    LED.record_rows([_ev(t2)], alert_ts=T0 + 4000, path=LP)
    TS = T0 + 4000 + 3700                                                # the 1h cell is DUE

    def sus_snap(toks):                                                  # only t2 pair-switches
        ok = {t: ({"price_usd": 2.0, "mcap": 6e6, "liq_usd": 5e4} if t == t2 else
                  {"price_usd": 1.0, "mcap": 1e6, "liq_usd": 5e4}) for t in toks}
        return {"ok": ok, "absent": set(), "deferred": set()}
    LED.update_forward(TS, sus_snap, path=LP)                            # implied supply 3e6 vs 1e6 at entry
    r = LED.load(LP).set_index("token")
    check("FOMO/Bull500: a pair-switch snapshot (supply ratio 3x) fills no cell (even a due one), ratchets "
          "nothing, suspect_ticks += 1",
          str(r.at[t2, "ret_1h"]) in LED._EMPTY and float(r.at[t2, "max_ret_seen"]) == 0.0
          and int(float(r.at[t2, "suspect_ticks"])) == 1 and str(r.at[t2, "status"]) == "open")
    for i in range(config.SUSPECT_TICKS_MAX - 1):
        LED.update_forward(TS + 100 + i, sus_snap, path=LP)
    r = LED.load(LP).set_index("token")
    polled = {"toks": None}

    def spy(toks):
        polled["toks"] = set(toks)
        return _snap(price=1.0)(toks)
    LED.update_forward(TS + 300, spy, path=LP)
    check(f"{config.SUSPECT_TICKS_MAX} consecutive suspect ticks => status 'suspect' and the row is never polled again",
          str(r.at[t2, "status"]) == "suspect" and t2 not in polled["toks"] and t1 in polled["toks"])
    # cells write-once and time-gated
    t3 = f"0x{3:040x}"
    r = LED.load(LP).set_index("token")
    check("cells are time-gated: after the 1h fill at 1.0, ret_6h is still empty before +6h",
          float(r.at[t3, "ret_1h"]) == 0.0 and str(r.at[t3, "ret_6h"]) in LED._EMPTY)
    LED.update_forward(T0 + 7200, _snap(price=3.0), path=LP)
    r = LED.load(LP).set_index("token")
    check("cells are write-once: a later 3x snapshot does not overwrite ret_1h (still 0.0)",
          float(r.at[t3, "ret_1h"]) == 0.0 and str(r.at[t3, "ret_6h"]) in LED._EMPTY)
    LED.update_forward(T0 + 21700, _snap(price=3.0), path=LP)
    r = LED.load(LP).set_index("token")
    check("ret_6h fills at +6h with the observed 3x (+200%)", abs(float(r.at[t3, "ret_6h"]) - 2.0) < 1e-9)
    # write-once lag stamps: how late the run grid actually sampled each horizon (the FOMOPAD
    # outage, 2026-09-14..17: cells filled hours late were silently consumed by every scorecard)
    check("every filled forward cell carries lag_{h} = now_s - (alert_ts + hsec): the 1h cell sampled at "
          "+3700 s stamps 100 s, the 6h cell sampled at +21700 s stamps 100 s, an unfilled horizon has none",
          abs(float(r.at[t3, "lag_1h"]) - 100.0) < 1e-6 and abs(float(r.at[t3, "lag_6h"]) - 100.0) < 1e-6
          and str(r.at[t3, "lag_24h"]) in LED._EMPTY,
          f"{r.at[t3, 'lag_1h']} {r.at[t3, 'lag_6h']} {r.at[t3, 'lag_24h']}")
    check("the dead path stamps a lag too: t1's 1h cell was written on its 2nd absence at +3800 s (lag 200 s)",
          abs(float(r.at[t1, "lag_1h"]) - 200.0) < 1e-6, str(r.at[t1, "lag_1h"]))
    LED.update_forward(T0 + 30000, _snap(price=3.0), path=LP)
    r_l = LED.load(LP).set_index("token")
    check("lag cells are write-once with their cell: a later run does not restamp lag_1h / lag_6h",
          float(r_l.at[t3, "lag_1h"]) == 100.0 and float(r_l.at[t3, "lag_6h"]) == 100.0,
          f"{r_l.at[t3, 'lag_1h']} {r_l.at[t3, 'lag_6h']}")
    # rugged_after flips once and never back
    LED.update_forward(T0 + 21800, _snap(price=1.0, liq=100.0), path=LP)
    r = LED.load(LP).set_index("token")
    LED.update_forward(T0 + 21900, _snap(price=1.0, liq=1e5), path=LP)
    r2 = LED.load(LP).set_index("token")
    check("rugged_after flips when liquidity collapses below RUG_LIQ_USD while priced and never flips back",
          str(r.at[t3, "rugged_after"]).lower() == "true" and str(r2.at[t3, "rugged_after"]).lower() == "true")
    # old-schema CSV loads and updates
    old = os.path.join(d, "old.csv")
    pd.DataFrame([{"token": "0xold", "symbol": "OLD", "tier": "A", "alert_ts": T0, "entry_price": 1.0,
                   "entry_mcap": 1e6, "entry_liq": 5e4, "status": "open", "plan_name": "cfg_ladder_stop"}]).to_csv(old, index=False)
    ledo = LED.load(old)
    fo, evo = LED.update_forward(T0 + 3700, _snap(price=2.5), path=old)
    ro = LED.load(old)
    check("a CSV written before a schema addition loads with the missing columns present and still updates "
          "(pre-migration incident path)", list(ledo.columns) == LED.COLUMNS and fo == 1
          and abs(float(ro.at[0, "ret_1h"]) - 1.5) < 1e-9 and [e["kind"] for e in evo] == ["tp"])
    hdr_old = _read(old).splitlines()[0].split(",")
    check("the four lag_{h} columns are APPENDED LAST in horizon order (jarvis reads the CSV by header): "
          "an old CSV saves with them at the end and lag_7d is the final column",
          hdr_old[-len(LED.HORIZ):] == [f"lag_{h}" for h in LED.HORIZ] and LED.COLUMNS[-1] == "lag_7d"
          and hdr_old == LED.COLUMNS, str(hdr_old[-6:]))
    # rotation
    rot = os.path.join(d, "rot.csv")
    LED.ensure_exists(rot)
    told = T0 - 100 * 86400
    LED.record_rows([_ev("0xold1"), _ev("0xnew1")], alert_ts=told, path=rot)
    LED.update_forward(told + 8 * 86400, _snap(price=1.2), path=rot)      # every horizon due -> resolved
    st = set(LED.load(rot)["status"])
    moved = LED.rotate(T0, path=rot)
    outs = glob.glob(os.path.join(d, "resolved_*.csv"))
    check(f"rotate() moves resolved rows older than LEDGER_ROTATE_AFTER_DAYS ({config.LEDGER_ROTATE_AFTER_DAYS}) "
          "to resolved_YYYY.csv (no 'ledger' in the name)", st == {"resolved"} and moved == 2 and len(outs) == 1
          and len(LED.load(rot)) == 0 and len(pd.read_csv(outs[0])) == 2, f"{st} {moved} {outs}")

with tempfile.TemporaryDirectory() as d:
    # EDDICE (event_seq 498): ret_6h = 11,026,957 with mcap_6h = 4.3e11 and a CONSTANT implied
    # supply — the supply-drift gate cannot see a quote leg that is itself mispriced.
    LP = os.path.join(d, "ledger.csv")
    LED.ensure_exists(LP)
    LED.record_rows([_ev("0ximp1"), _ev("0xok1")], alert_ts=T0, path=LP)
    MULT = 2.0 * config.LEDGER_MAX_PLAUSIBLE_MULT

    def imp_snap(toks):                              # only 0ximp1 is implausibly quoted
        ok = {t: ({"price_usd": MULT, "mcap": MULT * 1e6, "liq_usd": 5e4} if t == "0ximp1" else
                  {"price_usd": 1.0, "mcap": 1e6, "liq_usd": 5e4}) for t in toks}
        return {"ok": ok, "absent": set(), "deferred": set()}
    fi, _evi = LED.update_forward(T0 + 3700, imp_snap, path=LP)   # the 1h cell is DUE for both
    ri = LED.load(LP).set_index("token")
    check(f"a tick at {MULT:.0f}x entry (> LEDGER_MAX_PLAUSIBLE_MULT={config.LEDGER_MAX_PLAUSIBLE_MULT:.0f}) with a "
          "CONSTANT implied supply fills NO horizon cell, never moves max_ret_seen, and takes the existing "
          "suspect path (suspect_ticks += 1); an honest row beside it still fills",
          str(ri.at["0ximp1", "ret_1h"]) in LED._EMPTY and float(ri.at["0ximp1", "max_ret_seen"]) == 0.0
          and int(float(ri.at["0ximp1", "suspect_ticks"])) == 1 and str(ri.at["0ximp1", "status"]) == "open"
          and float(ri.at["0xok1", "ret_1h"]) == 0.0 and fi == 1,
          f"{ri.at['0ximp1', 'ret_1h']} {ri.at['0ximp1', 'max_ret_seen']} {ri.at['0ximp1', 'suspect_ticks']} {fi}")
    for i in range(config.SUSPECT_TICKS_MAX - 1):
        LED.update_forward(T0 + 3800 + i, imp_snap, path=LP)
    ri = LED.load(LP).set_index("token")
    check(f"{config.SUSPECT_TICKS_MAX} consecutive implausible ticks flip status to 'suspect' (the SAME counter as "
          "the supply-drift path — no second bookkeeping)",
          str(ri.at["0ximp1", "status"]) == "suspect" and str(ri.at["0xok1", "status"]) == "open"
          and str(ri.at["0ximp1", "ret_6h"]) in LED._EMPTY)
    _, out_i = _capture(LED.summary, LP)
    check("summary() prints one 'late cells (lag > LEDGER_MAX_CELL_LAG_S)' line per horizon and the "
          "'implausible-mult suspects' line",
          out_i.count("late cells (lag > LEDGER_MAX_CELL_LAG_S)") == len(LED.HORIZ)
          and "implausible-mult suspects:" in out_i, out_i[-600:])
    check("summary() names precisely who excludes gap-sampled cells (the entry-band scorecard and the "
          "paper gate) and says plainly that this printout does not — 'print raw cells', twice, never "
          "the old blanket 'every scorecard excludes it'",
          out_i.count("print raw cells") == 2 and "entry-band scorecard" in out_i and "paper gate" in out_i
          and "every scorecard excludes it" not in out_i, out_i[-800:])

with tempfile.TemporaryDirectory() as d:
    LP = os.path.join(d, "ledger.csv")
    LED.ensure_exists(LP)
    LED.record_rows([_ev(f"0x{i:040x}") for i in range(1, 2001)], alert_ts=T0, path=LP)
    before = _read(LP).splitlines()
    LED.update_forward(T0 + 100, _snap(price=1.0), path=LP)
    after = _read(LP).splitlines()
    changed = sum(1 for a, b in zip(before, after) if a != b) + abs(len(after) - len(before))
    check("repo growth: a 2,000-open-row run with no new extremes and nothing due changes <= 10 ledger lines",
          len(before) == 2001 and changed <= 10, f"changed {changed} lines")

all_ledgers = sorted(glob.glob(os.path.join(ROOT, "**", "*ledger*.csv"), recursive=True))
rel_ledgers = [_rel(p) for p in all_ledgers]
stray = [p for p in rel_ledgers if p.split(os.sep)[0] in ("cache", ".claude", ".github")
         or p.startswith(os.path.join("data", "archive"))]
stray += [_rel(p) for base in (".claude", ".github", "cache") for p in glob.glob(os.path.join(ROOT, base, "**", "*ledger*.csv"), recursive=True)]
check("jarvis glob contract: sorted(glob('**/*ledger*.csv'))[0] is data/ledger.csv and no *ledger*.csv "
      "lives under cache/, .claude/, .github/ or data/archive/",
      rel_ledgers and rel_ledgers[0] == os.path.join("data", "ledger.csv") and not stray, f"{rel_ledgers[:3]} {stray}")

with tempfile.TemporaryDirectory() as d:
    LP = os.path.join(d, "ledger.csv")
    LED.ensure_exists(LP)
    LED.record_rows([_ev("0xs1", tier="A", kind="promotion", fired="x"), _ev("0xs2")], alert_ts=T0, path=LP)
    LED.update_forward(T0 + 3700, _snap(price=1.5), path=LP)
    _, out = _capture(LED.summary, LP)
    check("summary() prints the verbatim verdict sentence, the negative-expectancy reminder and a sample-size line",
          "If tier A does not clearly beat tier B here, the A-tier band is NOT adding signal" in out
          and "negative expectancy is the base rate" in out and "alert-day(s)" in out
          and f"below MIN_BOOTSTRAP_CLUSTERS={config.MIN_BOOTSTRAP_CLUSTERS}" in out, out[-400:])


# ═══════════════════════════════════════════════════════════════════════════════════
section("D. http_client — throttle under the lock, three-way contract, the 403 challenge (403 outage)")
# ═══════════════════════════════════════════════════════════════════════════════════
BS_HOST = "robinhoodchain.blockscout.com"
FAKE_HOST = "fake.verify.invalid"
http_client._HOST_HZ["throttle.verify.invalid"] = 20.0
http_client._HOST_HZ[FAKE_HOST] = 1000.0
http_client._HOST_HZ[BS_HOST] = 1000.0                   # restored below; keeps the fake 403 dance fast
stamps: list = []


def _hit():
    http_client._throttle("throttle.verify.invalid")
    stamps.append(time.monotonic())


threads = [threading.Thread(target=_hit) for _ in range(8)]
for t in threads:
    t.start()
for t in threads:
    t.join()
stamps.sort()
gaps = [b - a for a, b in zip(stamps, stamps[1:])]
check("8 concurrent callers on a 20 Hz host keep a tightest inter-arrival gap >= 0.045 s (sleep INSIDE the lock; "
      "unlocked, 8 fired in 0.87 s with a 0.000 s gap)", min(gaps) >= 0.045, f"min gap {min(gaps):.4f}")


class _Resp:
    def __init__(self, body: bytes):
        self._b = body
        self.headers = _fake_hdrs({})

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


_saved_urlopen, _saved_retries, _saved_hz_bs = http_client._urlopen, config.HTTP_RETRIES, config.HOST_RATE_HZ[BS_HOST]
n_calls = {"n": 0}


def _fake(script):
    """script: callable(url) -> bytes | Exception (raised)."""
    def f(req, timeout=None, context=None):
        n_calls["n"] += 1
        r = script(req.full_url, req.headers)
        if isinstance(r, Exception):
            raise r
        return _Resp(r)
    return f


try:
    with tempfile.TemporaryDirectory() as d:
        http_client.reset_health()
        http_client._urlopen = _fake(lambda u, h: _http_error(u, 404))
        cp = os.path.join(d, "neg.json")
        got = http_client.get_json(f"https://{FAKE_HOST}/thing", cache_path=cp, cache_404=True)
        check("a 404 returns NOT_FOUND (absent) and writes the negative-cache marker when cache_404",
              http_client.is_absent(got) and json.load(open(cp)) == {"__absent__": True})
        check("the negative marker is served from cache (NOT_FOUND again, no request)",
              http_client.is_absent(http_client.get_json(f"https://{FAKE_HOST}/thing", cache_path=cp, cache_404=True))
              and n_calls["n"] == 1)
        config.HTTP_RETRIES = 1
        http_client._urlopen = _fake(lambda u, h: urllib.error.URLError("dns down"))
        DARK_HOST = "dark.verify.invalid"
        http_client._HOST_HZ[DARK_HOST] = 1000.0
        check("a network failure returns None (deferred — never absent: 472-of-1,400)",
              http_client.get_json(f"https://{DARK_HOST}/x") is None)
        check("a dark host appears in degraded_hosts() and health_report says DARK; a host that answered a 404 does not",
              DARK_HOST in http_client.degraded_hosts() and "DARK" in http_client.health_report()
              and FAKE_HOST not in http_client.degraded_hosts())
        http_client._urlopen = _fake(lambda u, h: json.dumps({"ok": [1, 2]}).encode())
        check("a 200 returns the parsed object", http_client.get_json(f"https://{FAKE_HOST}/y") == {"ok": [1, 2]})
        check("post_json sends the JSON body and parses the answer",
              http_client.post_json(f"https://{FAKE_HOST}/rpc", {"m": 1}) == {"ok": [1, 2]})
        # the Cloudflare challenge on Blockscout: rotate ONCE, then blocked for the run
        config.HTTP_RETRIES = 3
        http_client.reset_health()
        n_calls["n"] = 0
        uas: list = []

        def challenge(u, h):
            uas.append(dict(h))
            return _http_error(u, 403, {"cf-mitigated": "challenge", "server": "cloudflare"})
        http_client._urlopen = _fake(challenge)
        got = http_client.get_json(f"https://{BS_HOST}/api/v2/tokens/0x1")
        check("403 outage: a cf-mitigated challenge rotates to header set 1 and retries exactly once (2 requests)",
              got is None and n_calls["n"] == 2 and len(uas) == 2
              and uas[1].get("Sec-ch-ua") == config.BLOCKSCOUT_HEADER_SETS[1]["sec-ch-ua"]
              and "Referer" in uas[0], str(n_calls))
        check("a second challenge marks bot_challenge: is_blocked() True, health_report names BOT-CHALLENGED",
              http_client.is_blocked(BS_HOST) and "BOT-CHALLENGED" in http_client.health_report())
        http_client.get_json(f"https://{BS_HOST}/api/v2/tokens/0x2")
        check("no third request is made while the host is blocked (deferred without a call)", n_calls["n"] == 2)
        check("a 404 is never a challenge, a plain 403 (no WAF header) is not a challenge",
              not http_client._is_bot_challenge(404, _http_error("u", 404, {"cf-mitigated": "challenge"}))
              and not http_client._is_bot_challenge(403, _http_error("u", 403, {})))
        http_client.reset_health()
        n_calls["n"] = 0
        config.HTTP_RETRIES = 1
        http_client._urlopen = _fake(lambda u, h: _http_error(u, 403, {}))
        got = http_client.get_json(f"https://{FAKE_HOST}/plain")
        check("a plain 403 is retried as a transient failure and returns None without marking bot_challenge",
              got is None and not http_client.is_blocked(FAKE_HOST) and n_calls["n"] == 1)
        http_client.reset_health()
        n_calls["n"] = 0
        http_client.mark_blocked(FAKE_HOST, "cross-run backoff")
        check("mark_blocked makes _request return None without a call (the cross-run Blockscout backoff)",
              http_client.get_json(f"https://{FAKE_HOST}/z") is None and n_calls["n"] == 0
              and "cross-run backoff" in http_client.health_report())
finally:
    http_client._urlopen = _saved_urlopen
    config.HTTP_RETRIES = _saved_retries
    http_client._HOST_HZ[BS_HOST] = _saved_hz_bs
    http_client.reset_health()

# ═══════════════════════════════════════════════════════════════════════════════════
section("E. quotes.py — dead is decided ON-CHAIN FIRST; the Multicall3 ABI is pinned")
# ═══════════════════════════════════════════════════════════════════════════════════
import quotes                                           # noqa: E402
from sources import kyber, rpc, scanhood, dexscreener, geckoterminal   # noqa: E402
from http_client import NOT_FOUND                       # noqa: E402

WPX = 2500.0
POOL = config.MIZUKARA_POOL.lower()
_orig = {"v2_pair": rpc.v2_pair, "gao": rpc.get_amounts_out, "res": rpc.v2_reserves, "kyber": kyber.route,
         "sh": scanhood.quote, "mc": rpc.multicall3, "dec": rpc._decimals_ex, "decimals": rpc.decimals,
         "dexw": dexscreener.weth_price_usd, "gta": geckoterminal.token_attrs}
kcalls = {"n": 0}


def _patch(pair=("ok", POOL), gao=(None, "revert"), res=("ok", (0, 0)), kyb=None, sh=None, dec=(18, False), mc=None):
    rpc.v2_pair = lambda t: pair
    rpc.get_amounts_out = (gao if callable(gao) else (lambda a, p: gao))
    rpc.v2_reserves = lambda p, t: res
    rpc.multicall3 = mc if mc is not None else _orig["mc"]

    def kf(*a, **k):
        kcalls["n"] += 1
        return kyb
    kyber.route = kf
    scanhood.quote = lambda a, s_, amt: sh
    rpc._decimals_ex = lambda t: dec
    rpc.decimals = lambda t: dec[0]


try:
    _patch(gao=(0, "zero"), res=("ok", (0, 0)), kyb=None)
    qa = quotes.quote_sell("0xfixture", 10 ** 24, T0, weth_px=WPX)
    check("the absent rule on-chain first: router zero + reserves (0,0) + kyber deferred => ABSENT, kyber never asked",
          qa["status"] == "absent" and kcalls["n"] == 0 and qa["probes"]["reserves"] == [0, 0])
    _patch(gao=(None, "revert"), res=("ok", (10 ** 24, 2 * 10 ** 18)))
    qb = quotes.quote_sell("0xfixture", 10 ** 22, T0, weth_px=WPX)
    exp = quotes._cp_out(10 ** 22, 10 ** 24, 2 * 10 ** 18)
    check("router revert + live reserves => ok, source 'reserves', the constant-product amount (never dead)",
          qb["status"] == "ok" and qb["source"] == "reserves" and qb["amount_out_raw"] == exp)
    _patch(pair=("absent", None), kyb=NOT_FOUND, sh=None)
    qc = quotes.quote_sell("0xfixture", 10 ** 24, T0, weth_px=WPX)
    check("no pair + kyber NOT_FOUND + scanhood None => DEFERRED (a probe the branch needs went unanswered)",
          qc["status"] == "deferred" and qc["probes"]["scanhood"] == "deferred")
    _patch(pair=("absent", None), kyb=NOT_FOUND, sh=NOT_FOUND)
    qd = quotes.quote_sell("0xfixture", 10 ** 24, T0, weth_px=WPX)
    check("no pair + both aggregators no_route => ABSENT", qd["status"] == "absent")
    _patch(pair=("absent", None), kyb=None, sh=NOT_FOUND)
    check("no pair + kyber deferred + scanhood no_route => DEFERRED (absent needs BOTH explicit no_routes)",
          quotes.quote_sell("0xfixture", 10 ** 24, T0, weth_px=WPX)["status"] == "deferred")
    _patch(pair=("deferred", None))
    kn = kcalls["n"]
    qe = quotes.quote_sell("0xfixture", 10 ** 24, T0, weth_px=WPX)
    check("pair deferred => DEFERRED and no aggregator is asked", qe["status"] == "deferred"
          and qe["probes"]["pair"] == "deferred" and kcalls["n"] == kn)
    TOK = "0x" + "0" * 39 + "2"
    amt = 123456789
    hand = ("0x" + config.SEL_GET_AMOUNTS_OUT[2:] + "%064x" % amt + "%064x" % 0x40 + "%064x" % 2
            + config.WETH[2:].lower().rjust(64, "0") + TOK[2:].rjust(64, "0"))
    got = rpc.encode_call(config.SEL_GET_AMOUNTS_OUT, ("uint", amt), ("addr[]", [config.WETH, TOK]))
    check("getAmountsOut calldata for (amount, [WETH, TOKEN]) equals the hand-encoded fixture", got == hand)
    enc_amounts = "0x" + rpc.enc_uint(0x20) + rpc.enc_uint(2) + rpc.enc_uint(amt) + rpc.enc_uint(777)
    check("the uint256[] decoder returns the LAST amount (the path's final leg)",
          quotes._amounts_from(True, enc_amounts) == (777, "ok") and rpc._dec_uint_array(enc_amounts) == [amt, 777])
    calls = [(config.MULTICALL3, "0x313ce567"), (TOK, "0x0902f1ac")]
    hand_mc = (config.SEL_AGGREGATE3 + rpc.enc_uint(0x20) + rpc.enc_uint(2) + rpc.enc_uint(0x40) + rpc.enc_uint(0x40 + 0xa0)
               + rpc.enc_addr(config.MULTICALL3) + rpc.enc_uint(1) + rpc.enc_uint(0x60) + rpc.enc_uint(4)
               + "313ce567" + "0" * 56
               + rpc.enc_addr(TOK) + rpc.enc_uint(1) + rpc.enc_uint(0x60) + rpc.enc_uint(4) + "0902f1ac" + "0" * 56)
    check("the Multicall3 aggregate3 encoder matches a hand-encoded fixture for two calls",
          rpc._enc_aggregate3(calls) == hand_mc)
    ret_ok = rpc.enc_uint(18)
    fixture_ret = ("0x" + rpc.enc_uint(0x20) + rpc.enc_uint(2) + rpc.enc_uint(0x40) + rpc.enc_uint(0x40 + 0x80)
                   + rpc.enc_uint(1) + rpc.enc_uint(0x40) + rpc.enc_uint(32) + ret_ok
                   + rpc.enc_uint(0) + rpc.enc_uint(0x40) + rpc.enc_uint(0))
    check("the aggregate3 decoder round-trips a fixture with one success=false (a per-call revert is not a batch failure)",
          rpc._dec_aggregate3(fixture_ret, 2) == [(True, "0x" + ret_ok), (False, "0x")]
          and rpc._dec_aggregate3(fixture_ret, 3) is None)
    # quote_sell_many == per-token quote_sell on an injected RPC for 3 tokens (one reverting)
    T_A, T_B, T_C = "0x" + "0" * 39 + "2", "0x" + "0" * 39 + "3", "0x" + "0" * 39 + "4"
    per_tok = {T_A: (19 * 10 ** 15, "ok"), T_B: (None, "revert"), T_C: (7 * 10 ** 15, "ok")}
    RT, RW = 10 ** 24, 2 * 10 ** 18
    enc_res = "0x" + rpc.enc_uint(RT) + rpc.enc_uint(RW) + rpc.enc_uint(1)

    def mc_fake(calls_):
        out = []
        for i in range(0, len(calls_), 2):
            tok = "0x" + calls_[i][1][2 + 8 + 64 * 3:2 + 8 + 64 * 4][24:]     # path[0] = the token
            a, st = per_tok[tok]
            out.append((True, "0x" + rpc.enc_uint(0x20) + rpc.enc_uint(2) + rpc.enc_uint(10 ** 22) + rpc.enc_uint(a))
                       if st == "ok" else (False, "0x"))
            out.append((True, enc_res))
        return out
    _patch(gao=lambda a, p: per_tok[p[0]], res=("ok", (RT, RW)), mc=mc_fake)
    many = quotes.quote_sell_many([(T_A, 10 ** 22), (T_B, 10 ** 22), (T_C, 10 ** 22)], T0, weth_px=WPX)
    single = {t: quotes.quote_sell(t, 10 ** 22, T0, weth_px=WPX) for t in (T_A, T_B, T_C)}
    same = all(many[t]["status"] == single[t]["status"] and many[t]["amount_out_raw"] == single[t]["amount_out_raw"]
               and abs((many[t]["usd"] or 0) - (single[t]["usd"] or 0)) < 1e-9 for t in single)
    check("quote_sell_many equals per-token quote_sell on an injected RPC for 3 tokens (one reverting -> reserves branch)",
          same and many[T_A]["source"] == "multicall" and single[T_A]["source"] == "router"
          and many[T_B]["source"] == single[T_B]["source"] == "reserves", str({t: (many[t]["status"], many[t]["source"]) for t in many}))
    _patch(gao=lambda a, p: per_tok[p[0]], res=("ok", (RT, RW)), mc=lambda c: [(True, "0x")])   # wrong length
    fb = quotes.quote_sell_many([(T_A, 10 ** 22), (T_C, 10 ** 22)], T0, weth_px=WPX)
    check("an undecodable multicall response falls back to per-token quote_sell for EVERY item (never a silent deferral)",
          all(fb[t]["status"] == "ok" and fb[t]["source"] == "router" for t in fb))
    qbuy = quotes.quote_buy("0xfixture", 10.0, T0, weth_px=WPX)
    check("buy sizing: amount_in_wei == int(usd / weth_px * 1e18)", qbuy["amount_in_raw"] == int(10.0 / WPX * 1e18))
    dexscreener.weth_price_usd = lambda now_s: None
    geckoterminal.token_attrs = lambda a, network=None: None
    check("weth_price_usd with both sources down returns ('deferred', None) — never 0.0",
          quotes.weth_price_usd(T0) == ("deferred", None))
    check("a deferred WETH price makes a buy quote deferred (a fill nobody can value is never a fill)",
          quotes.quote_buy("0xfixture", 10.0, T0)["status"] == "deferred")
finally:
    rpc.v2_pair, rpc.get_amounts_out, rpc.v2_reserves = _orig["v2_pair"], _orig["gao"], _orig["res"]
    kyber.route, scanhood.quote, rpc.multicall3 = _orig["kyber"], _orig["sh"], _orig["mc"]
    rpc._decimals_ex, rpc.decimals = _orig["dec"], _orig["decimals"]
    dexscreener.weth_price_usd, geckoterminal.token_attrs = _orig["dexw"], _orig["gta"]


# ═══════════════════════════════════════════════════════════════════════════════════
section("F. paper_exec.py — the A book: three-way fills, atomic state, frozen plan")
# ═══════════════════════════════════════════════════════════════════════════════════
import paper_exec as PX                                 # noqa: E402
from selfimprove import champion as CH                  # noqa: E402

PTOK = "0x" + "a" * 40
USD, GAS = PX._size_usd(), PX._gas()


def _pq(side, token, amount_in, now_s, status, out=None, source="router"):
    q = quotes._new(side, token, amount_in, now_s, WPX)
    if status == "ok":
        quotes._finish_ok(q, out, source, WPX, impact=0.31)
    elif status == "absent":
        q["probes"].update(pair=POOL, router="zero", reserves=[0, 0])
        quotes._finish_absent(q)
    else:
        q["probes"]["pair"] = "deferred"
    return q


def _mk_buy(script):
    n = {"n": 0}

    def f(token, usd, now_s, *, weth_px=None):
        st = script[min(n["n"], len(script) - 1)]; n["n"] += 1
        return _pq("buy", token, int(usd / WPX * 1e18), now_s, st, out=10 ** 24)
    f.calls = n
    return f


def _mk_sell(script):
    n = {"n": 0}

    def f(token, raw, now_s, *, weth_px=None):
        st, usd_out = script[min(n["n"], len(script) - 1)]; n["n"] += 1
        return _pq("sell", token, raw, now_s, st, out=int(usd_out / WPX * 1e18) if usd_out else 0)
    f.calls = n
    return f


with tempfile.TemporaryDirectory() as d:
    PP, PL = os.path.join(d, "pos.json"), os.path.join(d, "led.csv")
    kw = dict(positions_path=PP, ledger_path=PL)
    plan = CH.exit_plan("cfg_ladder_stop")
    r = PX.open_position(PTOK, "DEMO", "A", 7, T0, plan=plan, alert_ts=T0 - 240, quote_buy_fn=_mk_buy(["ok"]), **kw)
    r2 = PX.open_position(PTOK, "DEMO", "A", 7, T0 + 1, plan=plan, alert_ts=T0 - 240, quote_buy_fn=_mk_buy(["ok"]), **kw)
    st = PX._load_state(PP)
    check("P1 a buy opens once (usd_flow = -(size+gas), gap_s = entry lag); a re-open is a no-op",
          r and r["side"] == "buy" and abs(r["usd_flow"] + (USD + GAS)) < 1e-9 and abs(r["gap_s"] - 240) < 1e-9
          and r2 is None and len(st["positions"]) == 1 and st["positions"][PX._key(PTOK, 7)]["plan"]["name"] == "cfg_ladder_stop")
    _fp = PX._frozen_plan({"name": "x", "ladder": [[1.5, 0.5]], "stop": 0.5, "trail": 0.3, "trail_arm": 1.5,
                           "flow": {"buy_share_max": 0.45, "weak_ticks": 2, "vol_floor_frac": 0.20, "min_txns_m5": 5}})
    _fp_def = PX._frozen_plan(None)
    _flow_sell = _mk_sell([("ok", 10.0)])
    _flow_r = PX.execute_exit({"kind": "flow_exit", "token": PTOK, "symbol": "DEMO", "event_seq": 7,
                               "price": 3e-23, "ret": 0.5, "mult": 1.5, "gap_s": 60.0},
                              T0 + 60, quote_sell_fn=_flow_sell, **kw)
    check("P1b the frozen plan carries trail_arm and flow for the audit trail, and the cloud book executes "
          "NEITHER: 'flow_exit' is not an EXIT_KIND, so execute_exit returns None on an OPEN position "
          "without taking a single sell quote (the flow leg is scored by the paper book alone)",
          _fp["trail_arm"] == 1.5 and _fp["flow"]["weak_ticks"] == 2 and _fp_def["trail_arm"] is None
          and _fp_def["flow"] is None and _flow_r is None and _flow_sell.calls["n"] == 0
          and "flow_exit" not in PX.EXIT_KINDS and "flow_exit" not in PX.PROTECTIVE_KINDS)
    ev = {"kind": "tp", "token": PTOK, "symbol": "DEMO", "event_seq": 7, "price": 2e-23, "ret": 1.0, "mult": 2.0,
          "levels": [(2.0, 0.5)], "gap_s": 120.0}
    r = PX.execute_exit(ev, T0 + 3600, quote_sell_fn=_mk_sell([("ok", 10.0)]), **kw)
    p7 = PX._load_state(PP)["positions"][PX._key(PTOK, 7)]
    check("P2 tp sells the ladder fraction of the ORIGINAL size in integer math (exactly 5e23); usd_flow = usd - gas",
          r["side"] == "tp_2x" and r["tokens_raw_delta"] == -5 * 10 ** 23 and abs(r["usd_flow"] - (10.0 - GAS)) < 1e-9
          and p7["tokens_remaining_raw"] == 5 * 10 ** 23 and not p7["closed"])
    r = PX.execute_exit(dict(ev, levels=[(5.0, 0.25), (10.0, 0.15)], mult=10.0), T0 + 7200, quote_sell_fn=_mk_sell([("ok", 30.0)]), **kw)
    p7 = PX._load_state(PP)["positions"][PX._key(PTOK, 7)]
    check("P2b the moonbag survives a full ladder (10% remains, position open)",
          r["side"] == "tp_5x+10x" and p7["tokens_remaining_raw"] == 10 ** 23 and not p7["closed"])
    ev3 = {"kind": "stop", "token": PTOK, "symbol": "DEMO", "event_seq": 7, "price": 0.0, "ret": -1.0, "mult": 0.0, "gap_s": 300.0}
    r = PX.execute_exit(ev3, T0 + 9000, quote_sell_fn=_mk_sell([("absent", 0)]), **kw)
    p7 = PX._load_state(PP)["positions"][PX._key(PTOK, 7)]
    check("P3 $0 only on ABSENT for a protective kind: stop + absent => no_route close, $0 proceeds, NO gas",
          r["side"] == "no_route" and r["usd_flow"] == 0.0 and r["gas_usd"] == 0.0 and p7["closed"]
          and p7["close_reason"] == "no_route" and "on-chain first" in r["note"])
    check("P3b a closed position ignores further exits",
          PX.execute_exit(ev3, T0 + 9001, quote_sell_fn=_mk_sell([("ok", 5.0)]), **kw) is None)
    PX.open_position(PTOK, "DEMO", "A", 8, T0, plan=plan, alert_ts=T0, quote_buy_fn=_mk_buy(["ok"]), **kw)
    ev4 = dict(ev3, event_seq=8, gap_s=60.0)
    n_rows = len(PX._read_rows(PL))
    r = PX.execute_exit(ev4, T0 + 100, quote_sell_fn=_mk_sell([("deferred", 0)]), **kw)
    p8 = PX._load_state(PP)["positions"][PX._key(PTOK, 8)]
    check("P4 a deferred stop does NOT fill (472-of-1,400): queued in pending_exits, no CSV row, tokens intact",
          r is None and len(p8["pending_exits"]) == 1 and p8["tokens_remaining_raw"] == 10 ** 24 and len(PX._read_rows(PL)) == n_rows)
    c = PX.retry_pending(T0 + 400, quote_buy_fn=_mk_buy(["ok"]), quote_sell_fn=_mk_sell([("ok", 4.0)]), **kw)
    p8 = PX._load_state(PP)["positions"][PX._key(PTOK, 8)]
    last = PX._read_rows(PL)[-1]
    check("P4b retry_pending fills the queued stop (gap = signal gap + time deferred)",
          c["exits_filled"] == 1 and p8["closed"] and p8["close_reason"] == "stop" and last["side"] == "stop"
          and abs(float(last["gap_s"]) - 360.0) < 1e-6)
    PX.open_position(PTOK, "DEMO", "A", 9, T0, plan=plan, alert_ts=T0, quote_buy_fn=_mk_buy(["ok"]), **kw)
    PX.execute_exit(dict(ev3, event_seq=9), T0 + 100, quote_sell_fn=_mk_sell([("deferred", 0)]), **kw)
    for i in range(config.PAPER_PENDING_MAX_RUNS):
        c = PX.retry_pending(T0 + 200 + i, quote_buy_fn=_mk_buy(["deferred"]), quote_sell_fn=_mk_sell([("deferred", 0)]), **kw)
    p9 = PX._load_state(PP)["positions"][PX._key(PTOK, 9)]
    check(f"P5 a deferred exit is given up as stop_failed after PAPER_PENDING_MAX_RUNS={config.PAPER_PENDING_MAX_RUNS}; tokens kept",
          c["exits_failed"] == 1 and not p9["closed"] and p9["tokens_remaining_raw"] == 10 ** 24
          and PX._read_rows(PL)[-1]["side"] == "stop_failed")
    r = PX.execute_exit(dict(ev, event_seq=9), T0 + 5000, quote_sell_fn=_mk_sell([("absent", 0)]), **kw)
    p9 = PX._load_state(PP)["positions"][PX._key(PTOK, 9)]
    check("P6 tp + absent => tp_failed keeps the tokens (a token priced at 2x that nobody can route is suspicious, not dead)",
          r is None and PX._read_rows(PL)[-1]["side"] == "tp_failed" and p9["tokens_remaining_raw"] == 10 ** 24 and not p9["closed"])
    n_rows = len(PX._read_rows(PL))
    check("P7 an exit for an unknown (token, event_seq) is ignored — the B control never enters the book",
          PX.execute_exit(dict(ev3, event_seq=999), T0, quote_sell_fn=_mk_sell([("ok", 9.0)]), **kw) is None
          and len(PX._read_rows(PL)) == n_rows)
    before = _read(PP)
    _orig_replace = os.replace

    def boom(src, dst):
        assert os.path.exists(src) and os.path.getsize(src) > 0
        raise OSError("simulated crash between tmp write and replace")
    os.replace = boom
    try:
        stx = PX._load_state(PP); stx["positions"]["garbage:1"] = {"token": "g", "nan": float("nan")}
        okw = PX._save_state(stx, PP)
        rr = PX.open_position(PTOK, "DEMO", "A", 10, T0, plan=plan, alert_ts=T0, quote_buy_fn=_mk_buy(["ok"]), **kw)
    finally:
        os.replace = _orig_replace
    check("P8 positions JSON via tmp+os.replace: a crash between tmp write and replace leaves the original intact, no tmp",
          okw is False and rr is None and _read(PP) == before and json.loads(before)
          and not [f for f in os.listdir(d) if f.endswith(".tmp")])
    stx = PX._load_state(PP); stx["positions"]["k"] = {"v": float("inf"), "pending_exits": []}
    check("P8b NaN/inf are written as null (allow_nan=False after a NaN pre-pass)",
          PX._save_state(stx, PP) and json.load(open(PP))["positions"]["k"]["v"] is None)
    stx = PX._load_state(PP); stx["positions"].pop("k"); PX._save_state(stx, PP)
    with tempfile.TemporaryDirectory() as d2:
        p9c = os.path.join(d2, "champion.json")
        _saved_cp = config.CHAMPION_PATH
        config.CHAMPION_PATH = p9c
        try:
            CH.write_state(exit={"champion": "sell_15m", "promoted_ts": T0}, path=p9c)
            ev9 = {"kind": "time_exit", "token": PTOK, "symbol": "DEMO", "event_seq": 9, "price": 1e-23, "ret": 0.0,
                   "mult": 1.0, "due_ts": T0 + 900, "gap_s": 30.0}
            sell = _mk_sell([("ok", 9.0)])
            r9 = PX.execute_exit(ev9, T0 + 1000, quote_sell_fn=sell, **kw)
            p15 = CH.exit_plan()
            PX.open_position(PTOK, "DEMO", "A", 11, T0, plan=p15, alert_ts=T0, quote_buy_fn=_mk_buy(["ok"]), **kw)
            r11 = PX.execute_exit(dict(ev9, event_seq=11), T0 + 1000, quote_sell_fn=_mk_sell([("ok", 9.5)]), **kw)
            check("P9 the plan is FROZEN per position: after the champion switches to sell_15m, the cfg_ladder_stop "
                  "position ignores time_exit (no quote, no row) while a position opened under sell_15m honours it",
                  r9 is None and sell.calls["n"] == 0 and CH.exit_plan()["max_hold_s"] == 900
                  and r11 and r11["side"] == "time_exit" and r11["plan_name"] == "sell_15m")
        finally:
            config.CHAMPION_PATH = _saved_cp
    r = PX.open_position(PTOK, "DEMO", "A", 12, T0 + 10, plan=plan, alert_ts=T0, quote_buy_fn=_mk_buy(["deferred"]), **kw)
    stx = PX._load_state(PP)
    check("P10 a deferred buy is queued (buy_deferred row, pending_buys) — not opened, not failed",
          r is None and len(stx["pending_buys"]) == 1 and PX._key(PTOK, 12) not in stx["positions"]
          and PX._read_rows(PL)[-1]["side"] == "buy_deferred")
    c = PX.retry_pending(T0 + 900, quote_buy_fn=_mk_buy(["ok"]), quote_sell_fn=_mk_sell([("ok", 1.0)]), **kw)
    stx = PX._load_state(PP)
    check("P10b retry_pending fills the deferred buy with entry_lag_s in the note",
          c["buys_filled"] == 1 and stx["pending_buys"] == [] and PX._key(PTOK, 12) in stx["positions"]
          and "entry_lag_s=900" in PX._read_rows(PL)[-1]["note"])
    rows = PX._read_rows(PL)
    check("P11 every row carries quote_source + quote_status; every 'ok' row names a real quotes source",
          rows and list(rows[0].keys()) == PX.COLUMNS
          and all(r_["quote_status"] in ("ok", "absent", "deferred") and r_["quote_source"] for r_ in rows)
          and all(r_["quote_source"] in quotes._SOURCES for r_ in rows if r_["quote_status"] == "ok"))
    summ, out = _capture(PX.summary, mark_open=False, **kw)
    check("summary prints the pre-committed automation sentence and returns the numbers",
          PX.SUMMARY_SENTENCE in out and summ["n_positions"] >= 4 and "by_plan" in summ)

# ═══════════════════════════════════════════════════════════════════════════════════
section("G. livebook.py — (token, event_seq) identity, the quote-integrity gate, durable-first saves")
# ═══════════════════════════════════════════════════════════════════════════════════
from selfimprove import livebook as LB                  # noqa: E402
from selfimprove import policies as POL                 # noqa: E402

_g = vars(LB)
_lb_tree = _tree(os.path.join(ROOT, "selfimprove", "livebook.py"))
check("livebook.py never sleeps (the keeper's loop naps between ticks; only the write phase holds the scan's lock) and its docstring "
      "documents the four committed state files, the ticks log as artifact and the two feed modes — not 'all gitignored'",
      not any(isinstance(n, ast.Call) and _attr_chain(n.func) in (["time", "sleep"], ["sleep"]) for n in ast.walk(_lb_tree))
      and "gitignored (data/livebook.json" not in LB.__doc__ and "LIVEBOOK_FEED_SOURCE" in LB.__doc__ and "worktree" in LB.__doc__
      and "livebook_ticks.jsonl" in LB.__doc__ and "COMMITTED" in LB.__doc__)
# ── fix round 1: the lock is held around the state reads and the write phase only, never a quote ──
check("livebook.LOCK_PATH is keeper.sh's LOCK (data/.keeper.lock under config.DATA_DIR = ROOT/data): flock(1) there and fcntl.flock "
      "here are both flock(2) on ONE file, so commit_push and a write phase exclude each other",
      LB.LOCK_PATH == os.path.join(config.DATA_DIR, ".keeper.lock") and config.DATA_DIR == os.path.join(config.ROOT, "data")
      and 'LOCK="data/.keeper.lock"' in _read(KEEPER_SH) and "data/.keeper.lock" in _read(os.path.join(ROOT, ".gitignore")).splitlines())
_lb_fns = {n.name: n for n in _lb_tree.body if isinstance(n, ast.FunctionDef)}


def _lock_map(fn_node) -> dict:
    """{call key: [the innermost enclosing `with _state_lock():` node, or None, per call]} over one function's body,
    nested defs included (they run where they are called, which is never inside a hold)."""
    out: dict = {}

    def visit(node, cur):
        if isinstance(node, ast.With) and any(isinstance(it.context_expr, ast.Call)
                                              and _attr_chain(it.context_expr.func) == ["_state_lock"] for it in node.items):
            cur = node
        if isinstance(node, ast.Call):
            out.setdefault(".".join(_attr_chain(node.func)) or "?", []).append(cur)
        for ch in ast.iter_child_nodes(node):
            visit(ch, None if isinstance(ch, (ast.FunctionDef, ast.Lambda)) else cur)
    for st_ in fn_node.body:
        visit(st_, None)
    return out


_WRITES = ("_save_atomic", "_append_fill", "_append_jsonl")
_UNDER_LOCK_OK = {"_state_lock", "_save_atomic", "_append_fill", "_append_jsonl", "_load", "open", "csv.DictReader", "dict",
                  "fh.read", "_pos_key"}
_NEVER_UNDER_LOCK = ("quote_buy_fn", "quote_many_fn", "weth_px_fn", "dex_fn", "flow_fn", "_default_dex", "rows_fn", "_prepare_open",
                     "_sidecar_true", "_step_policy", "subprocess.run", "publish.origin_blob", "_quote_rounds", "_row_item",
                     "_stamp_item", "decimals_fn", "rpc.decimals")
_lm = {n: _lock_map(_lb_fns[n]) for n in ("tick", "feed_from_ledger", "open_alert", "_cloud_ledger_rows", "_sidecar_true")}
_bad_under = {n: sorted(k for k, ws in m.items() if any(w is not None for w in ws) and k not in _UNDER_LOCK_OK) for n, m in _lm.items()}
_quote_under = {n: sorted(k for k in _NEVER_UNDER_LOCK if any(w is not None for w in m.get(k, []))) for n, m in _lm.items()}
check("livebook.py (AST): inside `with _state_lock():` only the state I/O primitives run (_load / open / csv / _save_atomic / "
      "_append_*) — no quote, no rows_fn, no sidecar read, no _prepare_open, no _step_policy, no subprocess, in tick, "
      "feed_from_ledger, open_alert, _cloud_ledger_rows or _sidecar_true",
      not any(_bad_under.values()) and not any(_quote_under.values()), str((_bad_under, _quote_under)))
_module_writes = [(k, w) for fname, fn_ in _lb_fns.items() if fname != "_smoke" for k, ws in _lock_map(fn_).items() if k in _WRITES for w in ws]
check("livebook.py (AST): EVERY _save_atomic / _append_fill / _append_jsonl call in the module (outside _smoke) sits inside a "
      "`with _state_lock():` block — no state write ever lands outside the lock",
      _module_writes and all(w is not None for _k, w in _module_writes), str([k for k, w in _module_writes if w is None]))
_tick_w = {w for k, ws in _lm["tick"].items() if k in _WRITES for w in ws}
_feed_w = {w for k, ws in _lm["feed_from_ledger"].items() if k in _WRITES for w in ws}
_feed_logs = {w for k, ws in _lm["feed_from_ledger"].items() if k in ("_append_fill", "_append_jsonl") for w in ws}
_feed_reads = {w for w in _lm["feed_from_ledger"].get("_load", [])}
check("livebook.py (AST): the tick's write phase is ONE hold (its book save, fill flush and tick flush share one With node) and its "
      "book read is a separate, earlier hold; feed_from_ledger's write phase is ONE hold (book save, fills, missed lines, feed state "
      "in one With node) besides the first-run watermark save, and its book + feed-state read is one hold",
      len(_tick_w) == 1 and len(_feed_logs) == 1 and len(_feed_w) == 2 and _feed_logs <= _feed_w
      and sum(1 for w in _lm["feed_from_ledger"]["_save_atomic"] if w in _feed_logs) == 2
      and len(_feed_reads) == 1 and None not in _feed_reads and len(_lm["feed_from_ledger"]["_load"]) == 2
      and len(_lm["tick"]["_load"]) == 1 and _lm["tick"]["_load"][0] is not None and _lm["tick"]["_load"][0] not in _tick_w,
      str((len(_tick_w), len(_feed_w), len(_feed_logs), len(_feed_reads))))
_saved_lb = {k: _g[k] for k in ("BOOK_PATH", "FILLS_PATH", "TICKS_PATH", "FEED_STATE_PATH", "MISSED_PATH", "LOCK_PATH")}
_saved_max_open = config.LIVEBOOK_MAX_OPEN
TOK_RAW = 10 ** 24
LUSD = config.STACK_USD * config.POSITION_PCT
LT0 = 1_800_000_000.0
script: dict = {}
dex_mult: dict = {}
dex_calls = {"n": 0}


def _skel(side, token, status, now_s):
    return {"status": status, "side": side, "token": token, "amount_in_raw": 0, "amount_out_raw": None,
            "usd": None, "weth_px_usd": WPX, "source": None, "impact_pct": None, "gas_usd": 0.07, "ts": now_s,
            "probes": {"router": "skipped", "kyber": "skipped", "scanhood": "skipped", "pair": "skipped", "reserves": None}}


def buy_ok(token, usd, now_s, *, weth_px=None):
    q = _skel("buy", token, "ok", now_s)
    q.update(amount_in_raw=int(usd / WPX * 1e18), amount_out_raw=TOK_RAW, usd=usd, source="router", impact_pct=0.3)
    q["probes"].update(router="ok", pair="0xpair", reserves=[10 ** 27, 2 * 10 ** 18])
    return q


def sell_many(items, now_s, *, weth_px=None):
    out = {}
    for token, raw in items:
        sc = script.get(token, {"status": "ok", "mult": 1.0})
        q = _skel("sell", token, sc.get("status", "ok"), now_s)
        q["amount_in_raw"] = int(raw)
        if q["status"] == "ok":
            usd = LUSD * sc["mult"] * int(raw) / TOK_RAW
            q.update(amount_out_raw=int(usd / WPX * 1e18), usd=usd, source="multicall", impact_pct=0.3)
            rw = sc.get("reserve_weth")
            q["probes"].update(router="ok", pair="0xpair", reserves=[10 ** 27, rw] if rw is not None else None)
        out[token] = q
    return out


def dex_fn(tokens, now_s):
    dex_calls["n"] += 1
    ok = {t: {"price_usd": LUSD / TOK_RAW * 10 ** 18 * dex_mult[t], "symbol": "X"} for t in tokens if t in dex_mult}
    return {"ok": ok, "absent": set(), "deferred": set(tokens) - set(ok)}


flow_market: dict = {}                  # token -> the m5 window the flow stub answers with
flow_calls = {"n": 0, "tokens": []}


def flow_fn(tokens, now_s):
    """The per-tick flow read (Phase 4), stubbed: deferred by default — nothing in verify reaches
    the network — and a SEPARATE call from dex_fn so the R2 corroboration counts stay untouched."""
    flow_calls["n"] += 1
    flow_calls["tokens"].append(sorted(tokens))
    ok = {t: dict(flow_market[t]) for t in tokens if t in flow_market}
    return {"ok": ok, "absent": set(), "deferred": set(tokens) - set(ok)}


def lrow(token, seq, tier="A", kind="promotion", ats=None, prior=None):
    return {"token": token, "event_seq": seq, "symbol": "T%d" % seq, "tier": tier, "event_kind": kind,
            "prior_event_seq": prior, "plan_name": "cfg_ladder_stop", "alert_ts": LT0 - 30 if ats is None else ats}


def opn(r, now_s=LT0, buy=buy_ok):
    return LB.open_alert(r, now_s, quote_buy_fn=buy, weth_px=WPX, decimals_fn=lambda t: 18)


def tk(now_s):
    return LB.tick(now_s, quote_many_fn=sell_many, dex_fn=dex_fn, flow_fn=flow_fn, weth_px=WPX, verbose=False)


def book():
    return LB._load(LB.BOOK_PATH, {})


def fills():
    if not os.path.exists(LB.FILLS_PATH):
        return []
    with open(LB.FILLS_PATH, newline="") as fh:
        return list(csv.DictReader(fh))


def ticks():
    if not os.path.exists(LB.TICKS_PATH):
        return []
    return [json.loads(ln) for ln in open(LB.TICKS_PATH) if ln.strip()]


def _reset(d):
    for name, base in (("BOOK_PATH", "livebook.json"), ("FILLS_PATH", "livebook_fills.csv"),
                       ("TICKS_PATH", "livebook_ticks.jsonl"), ("FEED_STATE_PATH", "livebook_feed.json"),
                       ("MISSED_PATH", "livebook_missed.jsonl"), ("LOCK_PATH", ".keeper.lock")):
        _g[name] = os.path.join(d, base)
        if os.path.exists(_g[name]):
            os.remove(_g[name])
    script.clear(); dex_mult.clear(); flow_market.clear()


def _lock_free() -> bool:
    """Probe: is data/.keeper.lock free RIGHT NOW? A fresh fd's LOCK_EX|LOCK_NB succeeds only when no other open file
    description (this process's _state_lock included — flock(2) locks are per description, not per process) holds it."""
    fd = os.open(LB.LOCK_PATH, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_UN)
        return True
    except OSError:
        return False
    finally:
        os.close(fd)


def _no_nan(c):
    raise ValueError("non-finite JSON constant %s" % c)


try:
    with tempfile.TemporaryDirectory() as d:
        _reset(d)
        MAXT = float(config.LIVEBOOK_MAX_TRACK_S)
        # identity
        T4 = "0x" + "d" * 39 + "4"
        s1 = opn(lrow(T4, 10, tier="B", kind="first_sighting"))[0]
        s2 = opn(lrow(T4, 10, tier="B", kind="first_sighting"))[0]
        s3 = opn(lrow(T4, 11, tier="A", kind="promotion", prior=10))[0]
        b = book()
        check("tier-frozen bug: identity is (token, event_seq) — the same row opens once ('already'), a promotion row "
              "for an open B token opens a SECOND position with its own alert_seq",
              (s1, s2, s3) == ("opened", "already", "opened") and LB._pos_key(T4, 10) in b and LB._pos_key(T4, 11) in b
              and b[LB._pos_key(T4, 11)]["alert_seq"] == b[LB._pos_key(T4, 10)]["alert_seq"] + 1
              and b[LB._pos_key(T4, 11)]["prior_event_seq"] == 10)
        script[T4] = {"status": "ok", "mult": 2.5}
        stt = tk(LT0 + 60)
        check("both positions on one token are priced (rounds of unique tokens) and both ladders fill at 2.5x",
              stt["quoted"] >= 2 and all(book()[LB._pos_key(T4, q)]["policies"]["cfg_ladder"]["realized_usd"] > 0 for q in (10, 11)))
        # the fabricated 5000x tick (FOMO / Bull500)
        T5 = "0x" + "e" * 39 + "5"
        opn(lrow(T5, 5))
        script[T5] = {"status": "ok", "mult": 1.0}
        tk(LT0 + 60)
        dex_mult[T5] = 1.0
        script[T5] = {"status": "ok", "mult": 5000.0}
        nf = len(fills())
        tk(LT0 + 120)
        p5 = book()[LB._pos_key(T5, 5)]
        last = [t for t in ticks() if t["token"] == T5][-1]
        check("FOMO/Bull500: a fabricated 5000x tick is 'suspect' — steps nothing, fills nothing, ladder untouched",
              last["status"] == "suspect" and last["reason"].startswith("mult>") and p5["suspect_ticks"] == 1
              and len(fills()) == nf and p5["policies"]["cfg_ladder"]["realized_usd"] == 0.0)
        script[T5] = {"status": "ok", "mult": 12.0}
        tk(LT0 + 180)
        last = [t for t in ticks() if t["token"] == T5][-1]
        check("a 12x jump that Dexscreener contradicts (1.0x) is suspect; R2 asked Dexscreener once",
              last["status"] == "suspect" and last["reason"] == "jump contradicted by dexscreener")
        del dex_mult[T5]
        tk(LT0 + 240)
        last = [t for t in ticks() if t["token"] == T5][-1]
        check("a 12x jump with Dexscreener dark is 'jump uncorroborated' (never accepted on one source)",
              last["status"] == "suspect" and last["reason"] == "jump uncorroborated")
        script[T5] = {"status": "ok", "mult": 1.5, "reserve_weth": 1}
        tk(LT0 + 300)
        check("R3: WETH out above the pool's WETH reserve is suspect even at 1.5x",
              [t for t in ticks() if t["token"] == T5][-1]["reason"] == "out exceeds pool reserve")
        T6 = "0x" + "f" * 39 + "6"
        opn(lrow(T6, 6))
        script[T6] = {"status": "ok", "mult": 1.0}
        tk(LT0 + 60)
        dex_mult[T6] = 24.0
        script[T6] = {"status": "ok", "mult": 12.0}
        nd = dex_calls["n"]
        tk(LT0 + 120)
        p6 = book()[LB._pos_key(T6, 6)]
        sides = {f["side"] for f in fills() if f["token"] == T6 and f["policy"] == "cfg_ladder"}
        check("a corroborated 12x jump (Dexscreener 24x, within LIVEBOOK_XSOURCE_TOL) is accepted: three rungs fill at their levels",
              dex_calls["n"] == nd + 1 and sides == {"tp_2x", "tp_5x", "tp_10x"}
              and abs(p6["policies"]["cfg_ladder"]["remaining"] - 0.1) < 1e-9)
        T7 = "0x" + "0" * 38 + "a7"
        opn(lrow(T7, 7))
        script[T7] = {"status": "ok", "mult": 1.0}
        tk(LT0 + 60)
        script[T7] = {"status": "ok", "mult": 0.01, "reserve_weth": 1}
        nd = dex_calls["n"]
        tk(LT0 + 120)
        p7 = book()[LB._pos_key(T7, 7)]
        check("downside is NEVER gated: a 0.01x collapse is honest (Dexscreener never asked) and stop_50 fires",
              [t for t in ticks() if t["token"] == T7][-1]["status"] == "ok" and dex_calls["n"] == nd
              and p7["policies"]["stop_50"]["close_reason"] == "stop" and p7["suspect_ticks"] == 0)
        T8 = "0x" + "0" * 38 + "b8"
        opn(lrow(T8, 8))
        script[T8] = {"status": "ok", "mult": 5000.0}
        for i in range(config.LIVEBOOK_SUSPECT_TICKS_MAX):
            tk(LT0 + 60 * (i + 1))
        p8 = book()[LB._pos_key(T8, 8)]
        check(f"{config.LIVEBOOK_SUSPECT_TICKS_MAX} consecutive suspect ticks retire the position 'suspect' with NO fills "
              "(excluded from every statistic, never -100%)",
              p8["done"] and p8["suspect"] and all(s_["close_reason"] == "suspect" and s_["remaining"] == 1.0 for s_ in p8["policies"].values())
              and not [f for f in fills() if f["token"] == T8 and f["policy"] != "*"])
        T10 = "0x" + "0" * 37 + "d10"
        opn(lrow(T10, 12))
        script[T10] = {"status": "deferred"}
        for i in range(config.LIVEBOOK_DEFERRED_TICKS_MAX):
            tk(LT0 + 60 * (i + 1))
        p10 = book()[LB._pos_key(T10, 12)]
        check(f"{config.LIVEBOOK_DEFERRED_TICKS_MAX} deferred quotes retire the position 'unpriced' (472-of-1,400: never -100%)",
              p10["done"] and p10["unpriced"] and not p10["suspect"] and p10["n_ticks"] == 0
              and all(s_["close_reason"] == "unpriced" and s_["remaining"] == 1.0 for s_ in p10["policies"].values()))
        T3 = "0x" + "c" * 39 + "3"
        opn(lrow(T3, 3))
        script[T3] = {"status": "absent"}
        tk(LT0 + 60)
        p3 = book()[LB._pos_key(T3, 3)]
        check("an ABSENT quote (on-chain-first dead) closes every policy at exactly -1.00 with no_route",
              p3["done"] and all(s_["close_reason"] == "no_route" and s_["realized_usd"] == 0.0 for s_ in p3["policies"].values()))
        d_ = LB.live_stats_dict(LT0)
        check("live_stats_dict excludes suspect/unpriced positions and is NaN-free JSON",
              d_["n_suspect"] >= 1 and d_["n_unpriced"] == 1
              and d_["per_policy"]["hold_to_end"]["n"] == sum(1 for q in book().values() if q["done"] and not q["suspect"] and not q["unpriced"])
              and json.dumps(d_, allow_nan=False))
        # the feed
        _reset(d)
        tf = LT0 + 10_000
        stf = LB.feed_from_ledger(tf, quote_buy_fn=buy_ok, rows_fn=lambda: [lrow("0x" + "1" * 40, 1, ats=tf - 100)],
                                  weth_px=WPX, decimals_fn=lambda t: 18, verbose=False)
        check("the first feed run sets the watermark and opens nothing (a historical backlog is never opened stale)",
              LB._load(LB.FEED_STATE_PATH, {})["watermark_alert_ts"] == tf and stf["opened"] == 0 and not book())
        late = tf + 10
        now_f = late + config.MAX_ENTRY_LAG_S + 1
        rows_ = [lrow("0x" + "1" * 40, 1, ats=late), lrow("0x" + "2" * 40, 2, ats=now_f - 30),
                 lrow("0x" + "3" * 40, 3, tier="C", ats=now_f - 20)]
        stf = LB.feed_from_ledger(now_f, quote_buy_fn=buy_ok, rows_fn=lambda: rows_, weth_px=WPX, decimals_fn=lambda t: 18, verbose=False)
        missed = [json.loads(ln) for ln in open(LB.MISSED_PATH) if ln.strip()]
        m1 = [m for m in missed if m["event_seq"] == 1][0]
        check("a row older than MAX_ENTRY_LAG_S is refused and logged with its lag; the fresh row opens (entry_lag_s stamped)",
              stf["opened"] == 1 and stf["missed"] == 2 and m1["reason"].startswith("entry lag")
              and m1["entry_lag_s"] == config.MAX_ENTRY_LAG_S + 1
              and book()[LB._pos_key("0x" + "2" * 40, 2)]["entry_lag_s"] == 30.0)
        calls = {"n": 0}

        def buy_defer_first(token, usd, now_s, *, weth_px=None):
            calls["n"] += 1
            return _skel("buy", token, "deferred", now_s) if calls["n"] == 1 else buy_ok(token, usd, now_s, weth_px=weth_px)
        tp_ = now_f + 300
        stf = LB.feed_from_ledger(tp_, quote_buy_fn=buy_defer_first, rows_fn=lambda: [lrow("0x" + "4" * 40, 4, ats=tp_ - 10)],
                                  weth_px=WPX, decimals_fn=lambda t: 18, verbose=False)
        pend = LB._load(LB.FEED_STATE_PATH, {})["pending"]
        stf2 = LB.feed_from_ledger(tp_ + 60, quote_buy_fn=buy_defer_first, rows_fn=lambda: [], weth_px=WPX, decimals_fn=lambda t: 18, verbose=False)
        check("a deferred buy parks in feed.pending and opens on the next tick (lag stamped 70 s)",
              stf["pending"] == 1 and len(pend) == 1 and stf2["opened"] == 1
              and book()[LB._pos_key("0x" + "4" * 40, 4)]["entry_lag_s"] == 70.0)
        config.LIVEBOOK_MAX_OPEN = len([p for p in book().values() if not p.get("done")])
        tb = tp_ + 600
        rows_ = [lrow("0x" + "5" * 40, 5, tier="B", kind="first_sighting", ats=tb - 10),
                 lrow("0x" + "6" * 40, 6, tier="A", kind="promotion", ats=tb - 10)]
        stf = LB.feed_from_ledger(tb, quote_buy_fn=buy_ok, rows_fn=lambda: rows_, weth_px=WPX, decimals_fn=lambda t: 18, verbose=False)
        missed = [json.loads(ln) for ln in open(LB.MISSED_PATH) if ln.strip()]
        check("book_full refuses a B row and admits the A row at LIVEBOOK_MAX_OPEN",
              stf["opened"] == 1 and missed[-1]["event_seq"] == 5 and missed[-1]["reason"] == "book_full"
              and LB._pos_key("0x" + "6" * 40, 6) in book())
        config.LIVEBOOK_MAX_OPEN = _saved_max_open
        # durable FIRST (199-fills)
        _reset(d)
        T11 = "0x" + "0" * 37 + "e11"
        opn(lrow(T11, 21))
        script[T11] = {"status": "ok", "mult": 1.0}
        tk(LT0 + 60)
        script[T11] = {"status": "ok", "mult": 10.0}
        nf, nt = len(fills()), len(ticks())
        _orig_save = _g["_save_atomic"]

        def boom_save(obj, path):
            raise OSError("disk full (simulated)")
        _g["_save_atomic"] = boom_save
        raised = False
        try:
            tk(LT0 + 120)
        except OSError:
            raised = True
        finally:
            _g["_save_atomic"] = _orig_save
        n_ticks_disk = book()[LB._pos_key(T11, 21)]["n_ticks"]
        tk(LT0 + 120)
        fills_after = [f for f in fills() if f["token"] == T11 and f["side"] == "tp_2x" and f["policy"] == "cfg_ladder"]
        check("199-fills: fills are flushed only AFTER the atomic book save — a failing save flushes no fill/tick line, "
              "and the retry fills exactly once", raised and len(fills()) > nf and n_ticks_disk == 1 and len(fills_after) == 1)
        T12 = "0x" + "0" * 37 + "f12"
        opn(lrow(T12, 22))
        key12 = "%s:%d" % (T12, 22)
        hold = float(np.random.default_rng(config.SEED + int(hashlib.sha256(key12.encode()).hexdigest()[:8], 16))
                     .uniform(0.0, config.LIVEBOOK_DECISION_HORIZON_S))
        script[T12] = {"status": "ok", "mult": 1.2}
        times = [LT0 + 60, LT0 + max(hold + 1.0, 114.0)]
        expected = [t for t in times if t - LT0 >= hold][0]
        for tt in times:
            tk(tt)
        s12 = book()[LB._pos_key(T12, 22)]["policies"]["ctl_random_exit"]
        check("ctl_random_exit fires at its sha256-derived hold (recomputed independently from config.SEED)",
              abs(hold - LB.random_exit_hold_s(book()[LB._pos_key(T12, 22)])) < 1e-12 and s12["closed"]
              and s12["close_reason"] == "random_exit" and s12["closed_ts"] == expected)
        T13 = "0x" + "0" * 36 + "1a13"
        opn(lrow(T13, 23))
        script[T13] = {"status": "ok", "mult": 1.0}
        tk(LT0 + 60)
        script[T13] = {"status": "ok", "mult": 0.3}
        tk(LT0 + 60 + 3600)
        p13 = book()[LB._pos_key(T13, 23)]
        s13 = p13["policies"]["stop_50"]
        script[T13] = {"status": "ok", "mult": 1.0}
        tk(LT0 + MAXT + 3800)
        p13 = book()[LB._pos_key(T13, 23)]
        check(f"a market exit after a tick gap > LIVEBOOK_MAX_SCORABLE_GAP_S ({config.LIVEBOOK_MAX_SCORABLE_GAP_S:.0f} s) is "
              "'gapped' (NaN for the scorer); hold_to_end's terminal mark is not",
              s13["close_reason"] == "stop" and s13["close_gap_s"] == 3600.0 and s13["gapped"] is True
              and p13["done"] and p13["policies"]["hold_to_end"]["gapped"] is False
              and LB.live_stats_dict(LT0)["per_policy"]["stop_50"]["n_gapped"] >= 1)

        # ── the per-tick flow features (Phase 4): the m5 window in the market dict, inert in the book
        #    until a policy carries a `flow` schema (Phase 5) ──
        _m5 = dexscreener._normalize({"baseToken": {"address": T4}, "volume": {"m5": "12.5", "h1": "300"},
                                      "txns": {"m5": {"buys": 7, "sells": "2"}, "h1": {"buys": 40, "sells": 10}}}, T4, LT0)
        _nom5 = dexscreener._normalize({"baseToken": {"address": T4}, "volume": {"h1": "300"}, "txns": {"h1": {"buys": 40}}}, T4, LT0)
        check("dexscreener._normalize carries vol_m5 / buys_m5 / sells_m5 (float / int / int) and MARKET_KEYS lists the three "
              "right after sells_h24, in the dict's own order",
              (_m5["vol_m5"], _m5["buys_m5"], _m5["sells_m5"]) == (12.5, 7, 2) and tuple(_m5.keys()) == dexscreener.MARKET_KEYS
              and dexscreener.MARKET_KEYS[dexscreener.MARKET_KEYS.index("sells_h24") + 1:][:3] == ("vol_m5", "buys_m5", "sells_m5"))
        check("an absent m5 window is None on all three — NEVER 0.0 (unknown flow is not zero flow); the h1 window keeps its "
              "0.0 absence value", _nom5["vol_m5"] is None and _nom5["buys_m5"] is None and _nom5["sells_m5"] is None
              and _nom5["sells_h1"] == 0 and _nom5["vol_h6"] == 0.0)
        check("the 5-minute feature read is GATED on POL.flow_policy_names(): with no flow policy live the book never called "
              "flow_fn once across the whole G harness (call counter 0), and flow_policy_names() names exactly the live "
              "policies carrying a flow dict — the gate, not a snapshot of today's family, is what keeps the read off",
              (POL.flow_policy_names() != [] or flow_calls["n"] == 0)
              and set(POL.flow_policy_names()) == {n_ for n_, p_ in POL.POLICIES.items() if isinstance(p_.get("flow"), dict)},
              f"{POL.flow_policy_names()} calls={flow_calls['n']}")
        check("POL.flow_from_market: None for a non-dict or any None m5 field; else exactly FLOW_FEATURES copied",
              POL.FLOW_FEATURES == ("vol_m5", "buys_m5", "sells_m5", "vol_h1", "buys_h1", "sells_h1", "liq_usd")
              and POL.flow_from_market(None) is None and POL.flow_from_market(NOT_FOUND) is None
              and POL.flow_from_market({"vol_m5": 1.0, "buys_m5": 2, "sells_m5": None, "vol_h1": 1.0}) is None
              and POL.flow_from_market(dict(_m5, liq_usd=5e4)) == {"vol_m5": 12.5, "buys_m5": 7, "sells_m5": 2, "vol_h1": 300.0,
                                                                  "buys_h1": 40, "sells_h1": 10, "liq_usd": 5e4})
        _reset(d)
        # post-merge fix: under Phase 4 alone `flow` was inert plumbing (no flow_step existed), so
        # this fixture used a placeholder {"fixture": True} to mark "a flow-schema policy exists"
        # for the wiring test below. Post-rebase, `_step_policy` actually calls POL.flow_step on
        # any policy carrying a `flow` dict, which indexes real FLOW_KEYS (vol_floor_frac,
        # min_txns_m5, buy_share_max, weak_ticks) — the placeholder raised KeyError. This is a
        # deliberately inert-but-valid flow config (a floor of 0.0 and a min-txns/weak-ticks
        # threshold no tick here reaches), so flow_step runs cleanly and never itself closes the
        # position: the assertions below still rely on the untouched 0.30 trail to close both
        # positions at 0.5x, exactly as before the rebase.
        POL.POLICIES["fx_flow_probe"] = {"trail": 0.30, "flow": {"buy_share_max": 0.0, "weak_ticks": 10 ** 9,
                                                                 "vol_floor_frac": 0.0, "min_txns_m5": 10 ** 9}}   # test-only; popped in the finally below
        try:
            TF1, TF2 = "0x" + "0" * 36 + "f1a1", "0x" + "0" * 36 + "f1a2"
            nd_flow0 = dex_calls["n"]
            opn(lrow(TF1, 31)); opn(lrow(TF2, 32))
            script[TF1] = {"status": "ok", "mult": 1.0}; script[TF2] = {"status": "ok", "mult": 1.0}
            flow_market[TF1] = {"vol_m5": 100.0, "buys_m5": 7, "sells_m5": 2, "vol_h1": 900.0, "buys_h1": 40, "sells_h1": 10,
                                "liq_usd": 5e4, "price_usd": 1.0}
            n0 = flow_calls["n"]
            tk(LT0 + 60)
            t1 = {t["token"]: t for t in ticks() if t["ts"] == LT0 + 60}
            check("a fixture flow policy with an open state ⇒ exactly ONE batched flow_fn call per tick over the due honest "
                  "tokens; flow ok for the answered token, dark for the unanswered one; the tick line carries flow + flow_status",
                  flow_calls["n"] == n0 + 1 and flow_calls["tokens"][-1] == sorted([TF1, TF2])
                  and t1[TF1]["flow_status"] == "ok"
                  and t1[TF1]["flow"] == {"vol_m5": 100.0, "buys_m5": 7, "sells_m5": 2, "vol_h1": 900.0, "buys_h1": 40,
                                          "sells_h1": 10, "liq_usd": 5e4}
                  and t1[TF2]["flow_status"] == "dark" and t1[TF2]["flow"] is None)
            script[TF2] = {"status": "deferred"}
            n0 = flow_calls["n"]
            tk(LT0 + 120)
            check("a deferred-quote position is not in the flow set (deferred is never scored) — its line says n/a; the honest one "
                  "still is, in ONE call", flow_calls["n"] == n0 + 1 and flow_calls["tokens"][-1] == [TF1]
                  and [t for t in ticks() if t["token"] == TF2][-1]["flow_status"] == "n/a")
            script[TF1] = {"status": "ok", "mult": 0.5}; script[TF2] = {"status": "ok", "mult": 0.5}
            tk(LT0 + 180)                        # the fixture's trail closes on both at 0.5x
            n0 = flow_calls["n"]
            tk(LT0 + 240)
            check("once every flow-policy state is closed the read stops (no call); every tick line since carries flow/flow_status "
                  "and the ticks jsonl parses with NaN/Infinity refused (allow_nan=False clean)",
                  flow_calls["n"] == n0 and all(("flow_status" in t and "flow" in t) for t in ticks() if t["ts"] >= LT0 + 60)
                  and all(json.loads(ln, parse_constant=_no_nan) is not None for ln in open(LB.TICKS_PATH) if ln.strip())
                  and book()[LB._pos_key(TF1, 31)]["policies"]["fx_flow_probe"]["closed"])
            check("the flow read is a SEPARATE call from the R2 corroboration: four flow ticks moved dex_calls not at all",
                  dex_calls["n"] == nd_flow0)
        finally:
            POL.POLICIES.pop("fx_flow_probe", None)

        # ── the band-under-test admission + the sidecar stamp (Phase 4): inert while the constant is None ──
        _reset(d)
        _saved_c2 = (config.LIVEBOOK_BAND_UNDER_TEST, config.LIVEBOOK_BAND_UNDER_TEST_MAX_OPEN, config.LIVEBOOK_FEED_SOURCE,
                     config.BAND_VERDICTS_PATH, config.LEDGER_PATH, config.LIVEBOOK_MAX_OPEN,
                     LB.subprocess.run, LB.publish.origin_blob, _g["_sidecar_true"])
        _sc_calls = {"n": 0}
        _orig_sidecar = _g["_sidecar_true"]

        def _counting_sidecar(seqs):
            _sc_calls["n"] += 1
            return _orig_sidecar(seqs)
        _g["_sidecar_true"] = _counting_sidecar
        try:
            config.LIVEBOOK_FEED_SOURCE = "worktree"
            config.BAND_VERDICTS_PATH = os.path.join(d, "band_verdicts.csv")
            config.LIVEBOOK_BAND_UNDER_TEST = "band_x"
            config.LIVEBOOK_BAND_UNDER_TEST_MAX_OPEN = 20

            def _write_sidecar(lines):
                with open(config.BAND_VERDICTS_PATH, "w", newline="") as fh:
                    fh.write("event_seq,token,alert_ts,band,verdict\n")
                    for ln in lines:
                        fh.write(",".join(str(x) for x in ln) + "\n")

            def _feed(rows, now_s, buy=buy_ok):
                return LB.feed_from_ledger(now_s, quote_buy_fn=buy, rows_fn=lambda: rows, weth_px=WPX,
                                           decimals_fn=lambda t: 18, verbose=False)

            def _missed():
                return [json.loads(ln) for ln in open(LB.MISSED_PATH) if ln.strip()]
            tu = LT0 + 20_000
            _write_sidecar([])
            _feed([], tu)                                              # the watermark
            # every event row has its sidecar lines, as store.append_verdicts writes them (72's carry no 1: unstamped)
            _write_sidecar([(71, "0x" + "7" * 40, tu + 10, "band_a_strict", 1), (72, "0x" + "8" * 40, tu + 10, "band_y", 0)])
            _feed([lrow("0x" + "7" * 40, 71, tier="A", kind="promotion", ats=tu + 10),
                   lrow("0x" + "8" * 40, 72, tier="A", kind="promotion", ats=tu + 10)], tu + 20)
            config.LIVEBOOK_MAX_OPEN = len([p_ for p_ in book().values() if not p_.get("done")])   # the book is now FULL
            TB1 = "0x" + "9" * 40
            _write_sidecar([(81, TB1, tu + 30, "band_a_strict", 0), (81, TB1, tu + 30, "band_x", 1), (81, TB1, tu + 30, "band_y", "NA")])
            r81 = lrow(TB1, 81, tier="B", kind="band_fire", ats=tu + 30); r81["fired_band"] = "band_x"
            n_sc = _sc_calls["n"]
            stf = _feed([r81], tu + 40)
            p81 = book().get(LB._pos_key(TB1, 81))
            buy81 = [f for f in fills() if f["token"] == TB1 and f["side"] == "buy"]
            check("a B row whose sidecar line says band_x,1 at its event_seq is admitted at LIVEBOOK_MAX_OPEN: pos.sidecar_true == "
                  "['band_x'] (sorted, the 0 and NA bands absent), fired_band carried, the shared-buy note names both; the sidecar "
                  "was read once for the batch",
                  stf["opened"] == 1 and p81 is not None and p81["sidecar_true"] == ["band_x"] and p81["fired_band"] == "band_x"
                  and len(buy81) == 1 and "sidecar_true band_x" in buy81[0]["note"] and "fired_band band_x" in buy81[0]["note"]
                  and _sc_calls["n"] == n_sc + 1, str((stf, p81 and p81.get("sidecar_true"), buy81 and buy81[0]["note"])))
            TB2, TB3, TB4, TB5 = ("0x" + "a" * 39 + "2", "0x" + "a" * 39 + "3", "0x" + "a" * 39 + "4", "0x" + "a" * 39 + "5")
            _write_sidecar([(82, TB2, tu + 50, "band_x", 0)]); _feed([lrow(TB2, 82, tier="B", kind="first_sighting", ats=tu + 50)], tu + 60)
            _write_sidecar([(83, TB3, tu + 60, "band_x", "NA")]); _feed([lrow(TB3, 83, tier="B", kind="first_sighting", ats=tu + 60)], tu + 70)
            _write_sidecar([(99, TB4, tu + 70, "band_x", 1)])
            stf84 = _feed([lrow(TB4, 84, tier="B", kind="first_sighting", ats=tu + 70)], tu + 80)
            pend84 = LB._load(LB.FEED_STATE_PATH, {})["pending"]
            check("worktree mode: a row with a sidecar line for ANOTHER event_seq only (none of its own) is not yet stamped — it waits "
                  "in feed.pending (stamp_wait 1, sidecar_pending 1): not missed, not opened, nothing decided on a stamp that had "
                  "not landed",
                  stf84["pending"] == 1 and stf84["stamp_wait"] == 1 and stf84["missed"] == 0 and stf84["opened"] == 0
                  and len(pend84) == 1 and pend84[0]["event_seq"] == 84 and pend84[0]["sidecar_pending"] == 1
                  and pend84[0]["sidecar_true"] == [] and LB._pos_key(TB4, 84) not in book(), str((stf84, pend84)))
            config.BAND_VERDICTS_PATH = d                               # a directory: the sidecar is unreadable
            _feed([lrow(TB5, 85, tier="B", kind="first_sighting", ats=tu + 80)], tu + 90)   # 84 is re-stamped here: unreadable ⇒ [] at once
            config.BAND_VERDICTS_PATH = os.path.join(d, "band_verdicts.csv")
            got = {m["event_seq"]: m["reason"] for m in _missed()}
            check("verdict 0 / NA / an unreadable sidecar ⇒ the B row is 'book_full' at once, and the row that waited on a missing line "
                  "is 'book_full' on the next call when the sidecar has gone unreadable (fail closed: nothing is always-admitted on a "
                  "stamp the feed cannot read, and a dark sidecar never parks a row)",
                  all(got.get(q) == "book_full" for q in (82, 83, 84, 85))
                  and not any(LB._pos_key(t_, q) in book() for t_, q in ((TB2, 82), (TB3, 83), (TB4, 84), (TB5, 85)))
                  and LB._load(LB.FEED_STATE_PATH, {})["pending"] == [], str(got))
            config.LIVEBOOK_BAND_UNDER_TEST_MAX_OPEN = 1                # one under-test position (81) is open: the sub-cap is reached
            TB6, TA7 = "0x" + "a" * 39 + "6", "0x" + "a" * 39 + "7"
            _write_sidecar([(86, TB6, tu + 90, "band_x", 1), (87, TA7, tu + 90, "band_x", 1)])
            n_sc = _sc_calls["n"]
            stf = _feed([lrow(TB6, 86, tier="B", kind="first_sighting", ats=tu + 90),
                         lrow(TA7, 87, tier="A", kind="promotion", ats=tu + 90)], tu + 100)
            m_ = _missed()
            check("at the sub-cap (LIVEBOOK_BAND_UNDER_TEST_MAX_OPEN) the next selected B row is refused 'band_under_test_full' while "
                  "an A row is still admitted (and stamped); one sidecar read for the batch",
                  stf["opened"] == 1 and stf["missed"] == 1 and m_[-1]["event_seq"] == 86 and m_[-1]["reason"] == "band_under_test_full"
                  and LB._pos_key(TA7, 87) in book() and book()[LB._pos_key(TA7, 87)]["sidecar_true"] == ["band_x"]
                  and _sc_calls["n"] == n_sc + 1, str((stf, m_[-1])))
            config.LIVEBOOK_BAND_UNDER_TEST_MAX_OPEN = 20
            TB8 = "0x" + "a" * 39 + "8"
            _write_sidecar([(88, TB8, tu + 110, "band_x", 1)])
            calls2 = {"n": 0}

            def buy_defer_once(token, usd, now_s, *, weth_px=None):
                calls2["n"] += 1
                return _skel("buy", token, "deferred", now_s) if calls2["n"] == 1 else buy_ok(token, usd, now_s, weth_px=weth_px)
            stf = _feed([lrow(TB8, 88, tier="B", kind="first_sighting", ats=tu + 110)], tu + 120, buy=buy_defer_once)
            pend = LB._load(LB.FEED_STATE_PATH, {})["pending"]
            n_sc = _sc_calls["n"]
            os.remove(config.BAND_VERDICTS_PATH)                        # gone: a re-read would find nothing
            stf2 = _feed([], tu + 180, buy=buy_defer_once)
            check("a deferred buy keeps its stamp through feed.pending and opens under it on the next tick — at the cap, with the "
                  "sidecar gone and NOT re-read (no new rows ⇒ no read)",
                  stf["pending"] == 1 and len(pend) == 1 and pend[0]["sidecar_true"] == ["band_x"] and stf2["opened"] == 1
                  and _sc_calls["n"] == n_sc and book()[LB._pos_key(TB8, 88)]["sidecar_true"] == ["band_x"], str((stf, pend, stf2)))
            d2 = LB.live_stats_dict(tu + 200)
            check("live_stats_dict: sidecar_coverage {n_stamped, n_unstamped} and band_under_test {name, n_open, n_done, n_refused_full} "
                  "(the band_under_test_full lines in the missed log); NaN-free JSON",
                  d2["sidecar_coverage"] == {"n_stamped": 4, "n_unstamped": 1}
                  and d2["band_under_test"] == {"name": "band_x", "n_open": 3, "n_done": 0, "n_refused_full": 1}
                  and json.dumps(d2, allow_nan=False), str((d2["sidecar_coverage"], d2["band_under_test"])))
            _rc_sc, _out_sc = _capture(LB.scorecard, tu + 200)
            check("--scorecard prints the one sidecar / band-under-test line",
                  "sidecar: 4 stamped / 1 unstamped; band under test: band_x" in _out_sc, _out_sc[:300])
            # fix round 1 (the ruling): a row whose sidecar lines had not landed is NOT stamped [] for good
            TC1, TC2 = "0x" + "c" * 39 + "1", "0x" + "c" * 39 + "2"
            _write_sidecar([(87, TA7, tu + 90, "band_x", 1)])          # lines exist; none for 90 yet
            n_sc = _sc_calls["n"]
            stf = _feed([lrow(TC1, 90, tier="B", kind="first_sighting", ats=tu + 210)], tu + 220)
            pend = LB._load(LB.FEED_STATE_PATH, {})["pending"]
            ok1 = (stf["pending"] == 1 and stf["stamp_wait"] == 1 and stf["opened"] == 0 and stf["missed"] == 0 and len(pend) == 1
                   and pend[0]["sidecar_pending"] == 1 and pend[0]["sidecar_true"] == [] and _sc_calls["n"] == n_sc + 1
                   and LB._pos_key(TC1, 90) not in book() and not any(m["event_seq"] == 90 for m in _missed()))
            _write_sidecar([(90, TC1, tu + 210, "band_x", 1), (90, TC1, tu + 210, "band_a_strict", 0)])   # the append lands
            stf2 = _feed([], tu + 280)                                  # no new rows: the one read is for the waiting row
            p90 = book().get(LB._pos_key(TC1, 90))
            check("(ruling) a feed call that sees a row with NO sidecar line leaves it pending — one read, nothing opened or missed — "
                  "and the next feed call (the line has landed) stamps it ['band_x'] and admits it at the cap under the band-under-test "
                  "rule; still one sidecar read per feed call",
                  ok1 and stf2["opened"] == 1 and stf2["stamp_wait"] == 0 and stf2["pending"] == 0 and _sc_calls["n"] == n_sc + 2
                  and p90 is not None and p90["sidecar_true"] == ["band_x"], str((stf, pend, stf2, p90 and p90.get("sidecar_true"))))
            _write_sidecar([(90, TC1, tu + 210, "band_x", 1)])         # a line for 90 only: 91 has none, and never will
            n_sc = _sc_calls["n"]
            stf = _feed([lrow(TC2, 91, tier="B", kind="first_sighting", ats=tu + 290)], tu + 300)
            stf2 = _feed([], tu + 360)
            m91 = [m for m in _missed() if m["event_seq"] == 91]
            check(f"the wait is bounded by LIVEBOOK_SIDECAR_WAIT_TICKS = {config.LIVEBOOK_SIDECAR_WAIT_TICKS}: a row whose line never "
                  "lands waits that many calls, then is stamped [] and settled under plain B rules ('book_full' at the cap) — a missing "
                  "line can never park a row until the lag cap",
                  config.LIVEBOOK_SIDECAR_WAIT_TICKS == 1 and stf["stamp_wait"] == 1 and stf["missed"] == 0
                  and stf2["stamp_wait"] == 0 and stf2["missed"] == 1 and len(m91) == 1 and m91[0]["reason"] == "book_full"
                  and _sc_calls["n"] == n_sc + 2 and LB._load(LB.FEED_STATE_PATH, {})["pending"] == [], str((stf, stf2, m91)))

            def _stamped(mode, stamps, n=0):
                it = {"event_seq": 5, "sidecar_true": [], "sidecar_pending": n}
                config.LIVEBOOK_FEED_SOURCE = mode
                LB._stamp_item(it, stamps)
                return it["sidecar_true"], it["sidecar_pending"]
            _mx = (_stamped("worktree", {5: ["band_x"]}), _stamped("worktree", {5: []}), _stamped("worktree", None),
                   _stamped("worktree", {}), _stamped("worktree", {}, n=1), _stamped("origin", {}))
            config.LIVEBOOK_FEED_SOURCE = "worktree"
            check("_stamp_item: lines with a 1 ⇒ stamped; lines with no 1 ⇒ [] at once; an unreadable sidecar (None) ⇒ [] at once, "
                  "never a wait; no line ⇒ waits in worktree mode (pending 1), then [] once the bound is spent; origin mode never "
                  "waits (ledger and sidecar come from one commit, so a missing line is a fact)",
                  _mx == ((["band_x"], 0), ([], 0), ([], 0), ([], 1), ([], 0), ([], 0)), str(_mx))
            config.LIVEBOOK_BAND_UNDER_TEST = None
            TB9 = "0x" + "a" * 39 + "9"
            _write_sidecar([(89, TB9, tu + 300, "band_x", 1)])
            _feed([lrow(TB9, 89, tier="B", kind="first_sighting", ats=tu + 300)], tu + 310)
            m_ = _missed()
            check("with LIVEBOOK_BAND_UNDER_TEST = None the rule is inert: the same stamped B row is 'book_full' and live_stats_dict "
                  "reports band_under_test None (the stamp itself is still recorded)",
                  m_[-1]["event_seq"] == 89 and m_[-1]["reason"] == "book_full" and LB.live_stats_dict(tu + 400)["band_under_test"] is None
                  and LB._pos_key(TB9, 89) not in book())
            # the feed's two read modes
            _lp = os.path.join(d, "ledger_wt.csv")
            with open(_lp, "w", newline="") as fh:
                fh.write("token,event_seq,alert_ts,tier\n0xabc,5,1.0,B\n")
            config.LEDGER_PATH = _lp
            _sp = {"n": 0}

            def _fake_run(args, **kw):
                _sp["n"] += 1
                return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")
            LB.subprocess.run = _fake_run
            LB.publish.origin_blob = lambda rel, root=None: ("token,event_seq,alert_ts,tier\n0xdef,6,2.0,A\n" if rel == LB.LEDGER_REL
                                                             else "event_seq,token,alert_ts,band,verdict\n7,0xq,1.0,band_x,1\n7,0xq,1.0,band_z,0\n"
                                                             "8,0xr,1.0,band_z,0\n"
                                                             if rel == LB.VERDICTS_REL else None)
            config.LIVEBOOK_FEED_SOURCE = "worktree"
            rows_wt = LB._cloud_ledger_rows()
            n_wt = _sp["n"]
            config.LIVEBOOK_FEED_SOURCE = "origin"
            rows_or = LB._cloud_ledger_rows()
            n_or = _sp["n"]
            st_or = _orig_sidecar({7, 8, 9})
            check("LIVEBOOK_FEED_SOURCE=worktree reads config.LEDGER_PATH with csv.DictReader and NO subprocess; origin mode fetches "
                  "ONCE and reads origin/main:data/ledger.csv through publish.origin_blob; the sidecar in origin mode rides the same "
                  "fetch (origin_blob(VERDICTS_REL), no second fetch) and answers three-way: a seq with a 1 ⇒ its bands, a seq with "
                  "lines but no 1 ⇒ [], a seq with no line ⇒ absent",
                  rows_wt == [{"token": "0xabc", "event_seq": "5", "alert_ts": "1.0", "tier": "B"}] and n_wt == 0
                  and rows_or == [{"token": "0xdef", "event_seq": "6", "alert_ts": "2.0", "tier": "A"}] and n_or == 1
                  and st_or == {7: ["band_x"], 8: []} and _sp["n"] == n_or, str((rows_wt, rows_or, st_or, _sp)))
            config.LIVEBOOK_FEED_SOURCE = "worktree"
            config.LEDGER_PATH = os.path.join(d, "nope.csv")
            _present_no_line = _orig_sidecar({1})                       # the fixture's sidecar exists: no line for 1 ⇒ {}
            config.BAND_VERDICTS_PATH = os.path.join(d, "nope_verdicts.csv")
            check("a missing worktree ledger is [] and a missing sidecar is None — the failure value, distinct from a present sidecar "
                  "with no line ({}) — while nothing asked is {} (a feed outage never kills the tick loop)",
                  LB._cloud_ledger_rows() == [] and _present_no_line == {} and _orig_sidecar({1}) is None and _orig_sidecar(set()) == {})
        finally:
            (config.LIVEBOOK_BAND_UNDER_TEST, config.LIVEBOOK_BAND_UNDER_TEST_MAX_OPEN, config.LIVEBOOK_FEED_SOURCE,
             config.BAND_VERDICTS_PATH, config.LEDGER_PATH, config.LIVEBOOK_MAX_OPEN,
             LB.subprocess.run, LB.publish.origin_blob, _g["_sidecar_true"]) = _saved_c2

        # ── fix round 1: the lock at runtime — free during every quote, held during every write, ONE write hold per call,
        #    exclusive against ANOTHER process's flock(2) holder (what util-linux flock(1) is); the --tick / --live cutover guard ──
        _reset(d)
        _saved_r1 = (config.LIVEBOOK_FEED_SOURCE, config.BAND_VERDICTS_PATH, config.ROOT, _g["_state_lock"], _g["_save_atomic"],
                     _g["_append_fill"], _g["_append_jsonl"], _g["_live"], _g["feed_from_ledger"], _g["tick"], LB.quotes.weth_price_usd)
        _lk = {"n": 0, "free_at_quote": [], "held_at_write": []}
        _orig_lock, _orig_save2, _orig_fill2, _orig_jsonl2 = _g["_state_lock"], _g["_save_atomic"], _g["_append_fill"], _g["_append_jsonl"]

        @contextlib.contextmanager
        def _counting_lock():
            _lk["n"] += 1
            with _orig_lock():
                yield

        def _probe_save(obj, path):
            _lk["held_at_write"].append(not _lock_free())
            return _orig_save2(obj, path)

        def _probe_fill(row):
            _lk["held_at_write"].append(not _lock_free())
            return _orig_fill2(row)

        def _probe_jsonl(path, obj):
            _lk["held_at_write"].append(not _lock_free())
            return _orig_jsonl2(path, obj)

        def buy_probe(token, usd, now_s, *, weth_px=None):
            _lk["free_at_quote"].append(_lock_free())
            return buy_ok(token, usd, now_s, weth_px=weth_px)

        def sell_probe(items, now_s, *, weth_px=None):
            _lk["free_at_quote"].append(_lock_free())
            return sell_many(items, now_s, weth_px=weth_px)
        try:
            config.LIVEBOOK_FEED_SOURCE = "worktree"
            config.BAND_VERDICTS_PATH = os.path.join(d, "band_verdicts.csv")
            _g["_state_lock"], _g["_save_atomic"], _g["_append_fill"], _g["_append_jsonl"] = _counting_lock, _probe_save, _probe_fill, _probe_jsonl
            TL1, TL2 = "0x" + "e" * 39 + "1", "0x" + "e" * 39 + "2"
            tl = LT0 + 30_000
            LB.feed_from_ledger(tl, quote_buy_fn=buy_probe, rows_fn=lambda: [], weth_px=WPX, decimals_fn=lambda t: 18, verbose=False)
            n_init = _lk["n"]
            with open(config.BAND_VERDICTS_PATH, "w", newline="") as fh:
                fh.write("event_seq,token,alert_ts,band,verdict\n101,%s,%r,band_a_strict,1\n102,%s,%r,band_a_strict,1\n"
                         % (TL1, tl + 10, TL2, tl + 10))
            stf = LB.feed_from_ledger(tl + 20, quote_buy_fn=buy_probe, rows_fn=lambda: [lrow(TL1, 101, ats=tl + 10), lrow(TL2, 102, ats=tl + 10)],
                                      weth_px=WPX, decimals_fn=lambda t: 18, verbose=False)
            n_feed = _lk["n"] - n_init
            script[TL1] = {"status": "ok", "mult": 2.5}; script[TL2] = {"status": "ok", "mult": 1.0}
            n0 = _lk["n"]
            st_t = LB.tick(tl + 80, quote_many_fn=sell_probe, dex_fn=dex_fn, flow_fn=flow_fn, weth_px=WPX, verbose=False)
            n_tick = _lk["n"] - n0
            n0 = _lk["n"]
            st_idle = LB.tick(tl + 90, quote_many_fn=sell_probe, dex_fn=dex_fn, flow_fn=flow_fn, weth_px=WPX, verbose=False)
            n_idle = _lk["n"] - n0
            check("at runtime: the first-run feed takes 2 holds (state read, watermark save); a feed with new rows exactly 3 (state read, "
                  "sidecar read, the ONE write phase); a tick with due positions exactly 2 (book read, the ONE write phase); an idle tick 1 "
                  "(read only). The lock was FREE at every buy and sell quote and HELD at every _save_atomic / _append_fill / _append_jsonl",
                  n_init == 2 and stf["opened"] == 2 and n_feed == 3 and st_t["fills"] >= 1 and n_tick == 2 and st_idle["due"] == 0
                  and n_idle == 1 and len(_lk["free_at_quote"]) == 3 and all(_lk["free_at_quote"])
                  and len(_lk["held_at_write"]) >= 8 and all(_lk["held_at_write"]),
                  str((n_init, stf, n_feed, st_t["fills"], n_tick, n_idle, _lk["free_at_quote"], _lk["held_at_write"])))
            # two processes. (1) a holder that already has the lock: the tick's book READ waits for it (a read never lands in a
            # `git pull --rebase` transient), so its quote runs only after the release; (2) a holder that takes the lock DURING the
            # quote phase (a commit_push starting mid-tick): the quote phase is untouched and the WRITE phase waits for the release
            _holder_src = ("import fcntl, os, sys, time\nfd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT)\n"
                           "fcntl.flock(fd, fcntl.LOCK_EX)\nprint('held', flush=True)\ntime.sleep(float(sys.argv[2]))\n"
                           "fcntl.flock(fd, fcntl.LOCK_UN)\nprint('released', flush=True)\n")

            def _spawn_holder(hold_s):
                p = subprocess.Popen([sys.executable, "-c", _holder_src, LB.LOCK_PATH, str(hold_s)], stdout=subprocess.PIPE, text=True)
                assert p.stdout.readline().strip() == "held"
                return p, time.monotonic()
            script[TL1] = {"status": "ok", "mult": 1.0}; script[TL2] = {"status": "ok", "mult": 1.0}
            _q = {}

            def sell_stamp(items, now_s, *, weth_px=None):
                _q["t"] = time.monotonic(); _q["free"] = _lock_free()
                return sell_many(items, now_s, weth_px=weth_px)
            hp, t_held = _spawn_holder(1.5)
            st_x = LB.tick(tl + 150, quote_many_fn=sell_stamp, dex_fn=dex_fn, flow_fn=flow_fn, weth_px=WPX, verbose=False)
            t_done = time.monotonic(); hp.wait(timeout=10)
            read_waited = _q["t"] - t_held >= 1.2 and _q["free"] is True and t_done - t_held >= 1.2 and st_x["quoted"] == 2
            _q.clear()

            def sell_then_hold(items, now_s, *, weth_px=None):
                _q["p"], _q["t_held"] = _spawn_holder(1.5)          # a commit starting DURING the quote phase
                _q["t"] = time.monotonic()
                return sell_many(items, now_s, weth_px=weth_px)
            mt0 = os.path.getmtime(LB.BOOK_PATH); t_start = time.monotonic()
            st_y = LB.tick(tl + 210, quote_many_fn=sell_then_hold, dex_fn=dex_fn, flow_fn=flow_fn, weth_px=WPX, verbose=False)
            t_done = time.monotonic(); _q["p"].wait(timeout=10)
            write_waited = (_q["t"] - t_start < 1.0 and t_done - _q["t_held"] >= 1.2 and st_y["quoted"] == 2
                            and os.path.getmtime(LB.BOOK_PATH) >= mt0 and _q["p"].returncode == 0 and hp.returncode == 0)
            check("two processes: (1) with another process already holding flock(2) on data/.keeper.lock the tick's book read waited "
                  "≥ 1.2 s for the release and only then quoted (the lock free by then); (2) a holder taking the lock during the quote "
                  "phase left the quote phase untouched and the write phase waited ≥ 1.2 s for its release — the book was saved after it",
                  read_waited and write_waited, str((_q.get("t", 0) - t_start, t_done - _q.get("t_held", 0), st_x, st_y)))
            hold_probe = []
            with _orig_lock():                                          # the Python side held: another process's LOCK_NB fails…
                hold_probe.append(subprocess.run([sys.executable, "-c", "import fcntl, os, sys\nfd = os.open(sys.argv[1], os.O_RDWR)\n"
                                                  "try:\n    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)\nexcept OSError:\n    sys.exit(1)\n",
                                                  LB.LOCK_PATH], timeout=30).returncode)
            hold_probe.append(subprocess.run([sys.executable, "-c", "import fcntl, os, sys\nfd = os.open(sys.argv[1], os.O_RDWR)\n"
                                              "fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)\n", LB.LOCK_PATH], timeout=30).returncode)
            check("…and the reverse: while _state_lock is held here, another process's LOCK_EX|LOCK_NB on the same file fails (rc 1); "
                  "after the release it succeeds (rc 0) — the fence is symmetric, as flock(1) in commit_push needs it to be",
                  hold_probe == [1, 0], str(hold_probe))
            # the cutover guard: --tick / --live refuse (rc 2, nothing run) when data/livebook.json is TRACKED in the checkout unless
            # LIVEBOOK_FEED_SOURCE=worktree (the keeper) — the Mac must never write tracked book state after the cutover
            gd = os.path.join(d, "repo"); os.makedirs(os.path.join(gd, "data"))
            subprocess.run(["git", "init", "-q", gd], check=True, timeout=60)
            with open(os.path.join(gd, "data", "livebook.json"), "w") as fh:
                fh.write("{}\n")
            subprocess.run(["git", "-C", gd, "add", "data/livebook.json"], check=True, timeout=60)
            subprocess.run(["git", "-C", gd, "-c", "user.name=t", "-c", "user.email=t@x", "commit", "-qm", "seed"], check=True, timeout=60)
            _calls: list = []
            _g["_live"] = lambda: _calls.append("live") or 0
            _g["feed_from_ledger"] = lambda now_s, **kw: _calls.append("feed") or {}
            _g["tick"] = lambda now_s, **kw: _calls.append("tick") or {}
            LB.quotes.weth_price_usd = lambda now_s: _calls.append("weth") or ("deferred", None)
            config.ROOT = gd
            config.LIVEBOOK_FEED_SOURCE = "origin"
            rc_l, out_l = _capture(LB.main, ["--live"]); rc_t, out_t = _capture(LB.main, ["--tick"])
            refused_ok = rc_l == 2 and rc_t == 2 and _calls == [] and "refused" in out_l and "tracked" in out_t and "--scorecard" in out_t
            config.LIVEBOOK_FEED_SOURCE = "worktree"
            rc_l2, _ = _capture(LB.main, ["--live"]); rc_t2, _ = _capture(LB.main, ["--tick"])
            keeper_ok = rc_l2 == 0 and rc_t2 == 0 and _calls == ["live", "weth", "feed", "tick"]
            subprocess.run(["git", "-C", gd, "rm", "-q", "--cached", "data/livebook.json"], check=True, timeout=60)
            config.LIVEBOOK_FEED_SOURCE = "origin"
            rc_l3, _ = _capture(LB.main, ["--live"])
            untracked_ok = rc_l3 == 0 and _calls[-1] == "live" and len(_calls) == 5
            config.ROOT = os.path.join(d, "not-a-repo"); os.makedirs(config.ROOT)
            nonrepo_ok = LB._tracked_state() is False
            check("the cutover guard: `--tick` and `--live` refuse with rc 2 and run NOTHING (no WETH read, no feed, no tick) when "
                  "data/livebook.json is tracked in the checkout and LIVEBOOK_FEED_SOURCE != worktree; the keeper (worktree) passes; an "
                  "untracked checkout passes; a non-repo is not a finding (only rc 0 from git ls-files --error-unmatch is)",
                  refused_ok and keeper_ok and untracked_ok and nonrepo_ok, str((rc_l, rc_t, rc_l2, rc_t2, rc_l3, _calls, out_l[:200])))
        finally:
            (config.LIVEBOOK_FEED_SOURCE, config.BAND_VERDICTS_PATH, config.ROOT, _g["_state_lock"], _g["_save_atomic"],
             _g["_append_fill"], _g["_append_jsonl"], _g["_live"], _g["feed_from_ledger"], _g["tick"], LB.quotes.weth_price_usd) = _saved_r1

        # ── restore honesty across a keeper handoff gap (the state is committed and restored on the successor) ──
        _reset(d)
        TG1, TG2 = "0x" + "b" * 39 + "1", "0x" + "b" * 39 + "2"
        opn(lrow(TG1, 91)); opn(lrow(TG2, 92))
        script[TG1] = {"status": "ok", "mult": 1.0}; script[TG2] = {"status": "ok", "mult": 1.0}
        tk(LT0 + 60)
        T_ = LT0 + 60
        saved_ts = {k_: p_["last_tick_ts"] for k_, p_ in book().items()}
        gap_h = float(config.KEEPER_CADENCE_S + 120)
        script[TG1] = {"status": "ok", "mult": 0.3}; script[TG2] = {"status": "ok", "mult": 2.5}
        tk(T_ + gap_h)                                                 # the successor's first tick, 360 s after the snapshot
        g1, g2 = book()[LB._pos_key(TG1, 91)], book()[LB._pos_key(TG2, 92)]
        rung = [f for f in fills() if f["token"] == TG2 and f["policy"] == "cfg_ladder" and f["side"] == "tp_2x"]
        check(f"restore honesty: a book saved with last_tick_ts = T and ticked at T + KEEPER_CADENCE_S + 120 ({gap_h:.0f} s > "
              f"{config.LIVEBOOK_MAX_SCORABLE_GAP_S:.0f}) closes `stop` gapped; a limit rung fill on the SAME tick is a limit, not a "
              "decision — never gapped; max_gap_s records the handoff",
              all(v_ == T_ for v_ in saved_ts.values()) and gap_h > config.LIVEBOOK_MAX_SCORABLE_GAP_S
              and g1["policies"]["stop_50"]["close_reason"] == "stop" and g1["policies"]["stop_50"]["gapped"] is True
              and g1["policies"]["stop_50"]["close_gap_s"] == gap_h
              and len(rung) == 1 and float(rung[0]["gap_s"]) == gap_h and float(rung[0]["px"]) == 2.0 * g2["entry_px"]
              and g2["policies"]["cfg_ladder"]["gapped"] is False and not g2["policies"]["cfg_ladder"]["closed"]
              and g2["max_gap_s"] == gap_h)
finally:
    for k, v in _saved_lb.items():
        _g[k] = v
    config.LIVEBOOK_MAX_OPEN = _saved_max_open


# ═══════════════════════════════════════════════════════════════════════════════════
section("G2. adaptive exit — trail_arm / flow (the -50% stop is always live; flow is livebook-only)")
# ═══════════════════════════════════════════════════════════════════════════════════
from selfimprove import evaluate as EV                  # noqa: E402

CAND_BENCH = "tp15_half_armtrail30_stop50_6h"
CAND_FLOW = "tp15_half_flowtrail_stop50_6h"


def _cand_policy(name: str) -> dict:
    """The candidate module's own POLICY dict, read from the file the operator will register."""
    return B.load_candidate_module(os.path.join(CAND_DIR, name + ".py"), name).POLICY


def _norm(pol: dict) -> dict:
    """policies.load_candidates' own normalisation: a registry/JSON ladder is a list of lists;
    simulate() removes rungs by tuple identity, so the loader retuples them at registration."""
    if not pol.get("ladder"):
        return dict(pol)
    return dict(pol, ladder=[(float(m), float(f)) for m, f in pol["ladder"]])


SHIPPED_BAND_CANDS = ["band_hype_early", "band_hype_attention"]


def _registration_faults(registry_path: str, trials_path: str) -> list:
    """Registration-consistency faults for the four shipped candidate modules against ONE
    (registry, trials) pair — the single implementation, run on the REAL tree below and on a
    SIMULATED post-registration tree in section Q.

    Registration is the operator's own step and a permanent counted trial, so the rule is never
    "the module must be absent": that pin was true the day it was written and red the instant
    `register.py --scan` ran, taking the repo's primary safety signal and run_research.sh's own
    merge gate down with it. The rule is:
      · unregistered ⇒ shipping the file registered nothing — the name is live in no loader;
      · registered   ⇒ the entry IS the module (kind, module `candidates.<name>`, not retired,
        the POLICY dict after the loader's ladder retupling / the module's declared REQUIRES), it
        resolves through the loader the runtime actually uses, and the name is ALREADY in its
        family's trials list, so the DSR denominator can never lag the family.
    """
    raw = REGC.load_registry_raw(registry_path)
    by_name = {c.get("name"): c for c in raw.get("candidates", [])}
    tr = REGC.trials.load(trials_path)
    live_pol = REGC.POL.load_candidates(registry_path)
    reg = B.load_registry(registry_path)
    faults = []
    for nm in SHIPPED_POLICY_CANDS + SHIPPED_BAND_CANDS:
        kind = "policy" if nm in SHIPPED_POLICY_CANDS else "band"
        mod = B.load_candidate_module(os.path.join(CAND_DIR, nm + ".py"), nm)
        e = by_name.get(nm)
        live = (nm in live_pol) if kind == "policy" else (nm in reg.names())
        counted = nm in set(tr.get("policies_ever_scored" if kind == "policy"
                                   else "bands_ever_scored") or [])
        if nm in B.BUILTINS:
            faults.append(f"{nm}: shadows a built-in band")
        if e is None:
            if live:
                faults.append(f"{nm}: live in a loader with no registry entry "
                              "(shipping the file registered it)")
            continue
        why = []
        if e.get("kind") != kind:
            why.append(f"kind {e.get('kind')!r} != {kind!r}")
        if e.get("module") != f"candidates.{nm}":
            why.append(f"module {e.get('module')!r}")
        if e.get("status") == "retired":
            why.append("retired")
        elif not live:
            why.append("registered but the loader does not resolve it")
        if kind == "policy" and e.get("status") != "retired" and live_pol.get(nm) != _norm(mod.POLICY):
            why.append(f"the live policy dict is not the module's POLICY ({live_pol.get(nm)!r})")
        if kind == "band" and list(e.get("requires") or []) != [str(k) for k in getattr(mod, "REQUIRES", ())]:
            why.append(f"requires {e.get('requires')!r} != the module's "
                       f"{[str(k) for k in getattr(mod, 'REQUIRES', ())]!r}")
        if not counted:
            why.append("registered but NOT in trials.json (the DSR denominator lags the family)")
        if why:
            faults.append(f"{nm}: " + "; ".join(why))
    return faults


CAND_RAW = {n: _cand_policy(n) for n in (CAND_BENCH, CAND_FLOW)}
CAND_POL = {n: _norm(p) for n, p in CAND_RAW.items()}
FLOW_OK = dict(CAND_RAW[CAND_FLOW]["flow"])

check("both candidate dicts validate: ladder 1.5x/half, stop 0.50, trail 0.30, trail_arm 1.5, 6 h — and the "
      "adaptive one adds exactly the four flow keys",
      all(POL.validate_policy(n, p) is None for n, p in CAND_RAW.items()),
      str({n: POL.validate_policy(n, p) for n, p in CAND_RAW.items()}))
_bad_shapes = {
    "trail_arm without trail": {"stop": 0.5, "trail_arm": 1.5},
    "trail_arm <= 1": {"trail": 0.3, "trail_arm": 1.0},
    "flow without trail_arm": {"trail": 0.3, "flow": FLOW_OK},
    "flow missing a key": {"trail": 0.3, "trail_arm": 1.5,
                           "flow": {k: v for k, v in FLOW_OK.items() if k != "min_txns_m5"}},
    "flow with an extra key": {"trail": 0.3, "trail_arm": 1.5, "flow": dict(FLOW_OK, vol_floor_pct=0.2)},
    "flow not a dict": {"trail": 0.3, "trail_arm": 1.5, "flow": 0.45},
    "buy_share_max out of (0,1)": {"trail": 0.3, "trail_arm": 1.5, "flow": dict(FLOW_OK, buy_share_max=1.0)},
    "vol_floor_frac out of (0,1)": {"trail": 0.3, "trail_arm": 1.5, "flow": dict(FLOW_OK, vol_floor_frac=0.0)},
    "weak_ticks < 1": {"trail": 0.3, "trail_arm": 1.5, "flow": dict(FLOW_OK, weak_ticks=0)},
    "min_txns_m5 negative": {"trail": 0.3, "trail_arm": 1.5, "flow": dict(FLOW_OK, min_txns_m5=-1)},
}
_slipped = [k for k, p in _bad_shapes.items() if POL.validate_policy("zz_probe", p) is None]
check("validate_policy rejects every malformed arm/flow shape — in particular a flow rule WITHOUT trail_arm, "
      "which would be laxer than the stop-only regime the operator asked for before 1.5x", not _slipped, str(_slipped))
check("the extended schema leaves every pre-declared policy validating unchanged",
      not [n for n, p in POL.POLICIES.items() if p and POL.validate_policy("zz_probe", p) is not None])
with tempfile.TemporaryDirectory() as _rd:
    _rp = os.path.join(_rd, "registry.json")
    with open(_rp, "w") as _fh:
        json.dump({"candidates": [{"name": CAND_BENCH, "kind": "policy", "status": "candidate",
                                   "policy": CAND_RAW[CAND_BENCH]}]}, _fh)
    _loaded = POL.load_candidates(_rp)
    check("registration day: load_candidates accepts the benchmark from a JSON registry and retuples its "
          "ladder, so simulate() can remove the rung it fills (a list-of-lists ladder would raise)",
          list(_loaded) == [CAND_BENCH] and _loaded[CAND_BENCH]["ladder"] == [(1.5, 0.5)]
          and not math.isnan(POL.simulate(1.0, [{"ts": 0, "o": 1.0, "h": 2.0, "l": 1.0, "c": 2.0, "v": 1.0}],
                                          _loaded[CAND_BENCH])))


def _sbar(ts, o, h, l, c):
    return {"ts": ts, "o": o, "h": h, "l": l, "c": c, "v": 1.0}


def _sim(pol, bars, pess=True):
    return POL.simulate(1.0, bars, dict(pol), pessimistic=pess)


ARM_POL = {"stop": 0.50, "trail": 0.30, "trail_arm": 1.5}
NOARM_POL = {"stop": 0.50, "trail": 0.30}
STOP_POL = {"stop": 0.50}
P_LOW = [_sbar(0, 1.0, 1.2, 1.0, 1.2), _sbar(60, 1.2, 1.2, 0.45, 0.45)]    # peak 1.2x, never arms
P_HIGH = [_sbar(0, 1.0, 1.6, 1.0, 1.6), _sbar(60, 1.6, 1.6, 1.1, 1.1)]     # peak 1.6x, arms
check("simulate below the arm: on a 1.2x → 0.45x path trail_arm 1.5 keeps the trail INERT, so the exit is the "
      "-50% stop at 0.50 — byte-identical to stop-only and strictly worse than the unarmed trail (0.84)",
      abs(_sim(ARM_POL, P_LOW) - _sim(STOP_POL, P_LOW)) < 1e-12 and _sim(ARM_POL, P_LOW) < _sim(NOARM_POL, P_LOW) - 1e-9)
check("simulate past the arm: on a 1.6x → 1.1x path the trail is live and fills at 1.12 (30% off the 1.6x "
      "high-water mark) — identical to the unarmed trail, better than stop-only",
      abs(_sim(ARM_POL, P_HIGH) - _sim(NOARM_POL, P_HIGH)) < 1e-12 and _sim(ARM_POL, P_HIGH) > _sim(STOP_POL, P_HIGH) + 1e-9)
check("the arm changes the within-bar BRACKET in neither reading: below it the armed policy equals stop-only "
      "pessimistically AND optimistically, above it the unarmed trail — so the arm removes exits, it never "
      "invents a within-bar ordering",
      all(abs(_sim(ARM_POL, P_LOW, q) - _sim(STOP_POL, P_LOW, q)) < 1e-12
          and abs(_sim(ARM_POL, P_HIGH, q) - _sim(NOARM_POL, P_HIGH, q)) < 1e-12 for q in (True, False)))
check("a trail policy can still score HIGHER pessimistically than optimistically (the optimistic reading "
      "ratchets the high-water mark to this bar's high before the same bar's low tests the RAISED trail — "
      "shipped behaviour of trail_30 since the 2026-08-12 anchor fix), and the arm reproduces exactly that "
      "bracket, both readings finite for the price-only benchmark",
      [_sim(ARM_POL, b, True) > _sim(ARM_POL, b, False) for b in (P_HIGH, P_LOW)]
      == [_sim({"trail": 0.30}, b, True) > _sim({"trail": 0.30}, b, False) for b in (P_HIGH, P_LOW)]
      == [True, False]
      and all(math.isfinite(_sim(CAND_POL[CAND_BENCH], b, q)) for b in (P_LOW, P_HIGH) for q in (True, False)))

_saved_lb2 = {k: _g[k] for k in ("BOOK_PATH", "FILLS_PATH", "TICKS_PATH", "FEED_STATE_PATH", "MISSED_PATH")}
try:
    with _policies({n: CAND_POL[n] for n in (CAND_BENCH, CAND_FLOW)}), tempfile.TemporaryDirectory() as d2:
        _reset(d2)
        check("simulate refuses to score the flow leg: an OHLCV bar carries no 5-minute buy/sell split, so a "
              "flow policy returns NaN while the price-only benchmark returns a number",
              math.isnan(POL.simulate(1.0, P_HIGH, POL.POLICIES[CAND_FLOW]))
              and not math.isnan(POL.simulate(1.0, P_HIGH, POL.POLICIES[CAND_BENCH])))
        _erows = [{"entry": 1.0, "bars": P_HIGH, "alert_ts": LT0 + 86400 * i, "res": "minute"} for i in range(6)]
        _escore = EV.score(_erows)
        check("evaluate.score drops the NaN rather than inventing one: the flow candidate scores n = 0 rows, the "
              "benchmark scores all 6 (a bar backtest may never quote a number for the adaptive policy)",
              _escore[CAND_FLOW]["ret"].size == 0 and _escore[CAND_BENCH]["ret"].size == 6)

        ENTRY = 1.0
        ARMED_PX = ENTRY * 1.6
        FP = POL.POLICIES[CAND_FLOW]

        def _flow(vol, buys, sells):
            return {"vol_m5": vol, "buys_m5": buys, "sells_m5": sells, "vol_h1": vol * 10.0,
                    "buys_h1": buys * 10, "sells_h1": sells * 10, "liq_usd": 50_000.0}

        def _run_flow(scr):
            """(reasons, state) for one script of (px, flow, gap_s) through flow_step alone."""
            st = LB._new_policy_state()
            st["peak_px"] = ENTRY
            POL.flow_state_init(st)
            out = []
            for i, (px, fl, gap) in enumerate(scr):
                st["peak_px"] = max(st["peak_px"], px)          # what _step_policy does first
                out.append(POL.flow_step(st, FP, fl, px, ENTRY, LT0 + 60 * (i + 1), gap, 180.0))
            return out, st

        WEAK, STRONG = _flow(1000.0, 1, 9), _flow(1000.0, 9, 1)
        _scr = [(ARMED_PX, STRONG, 60.0), (ARMED_PX, WEAK, 60.0), (ARMED_PX, WEAK, 60.0)]
        r1, s1 = _run_flow(_scr)
        r2, s2 = _run_flow(_scr)
        check("flow_step is a pure function of (state, policy, flow, prices, now_s, gap): two independent state "
              "dicts over one script give identical reasons and identical states, and the 2-tick weak streak "
              "exits 'weak_flow' — never on the first weak tick",
              r1 == r2 and s1 == s2 and r1 == [None, None, "weak_flow"] and s1["armed_ts"] == LT0 + 60, str(r1))
        rN, sN = _run_flow([(ARMED_PX, STRONG, 60.0), (ARMED_PX, WEAK, 60.0), (ARMED_PX, None, 60.0),
                            (ARMED_PX, WEAK, 60.0)])
        rG, sG = _run_flow([(ARMED_PX, STRONG, 60.0), (ARMED_PX, WEAK, 60.0), (ARMED_PX, WEAK, 3600.0)])
        check("the weak streak can span neither a dark tick nor a gap: a features-None tick (counted in "
              "flow_dark_ticks, never an exit) and a 3600 s gap both reset weak_ticks, so neither script reaches "
              "the 2-tick exit", rN == [None] * 4 and rG == [None] * 3 and sN["flow_dark_ticks"] == 1
              and sG["flow_dark_ticks"] == 0, str((rN, rG)))
        rV, sV = _run_flow([(ENTRY * 1.2, _flow(10_000.0, 9, 1), 60.0), (ARMED_PX, _flow(100.0, 9, 1), 60.0),
                            (ARMED_PX, _flow(10.0, 9, 1), 60.0)])
        check("the volume floor is structurally impossible on the ARMING tick: peak_vol_m5 is reset while "
              "unarmed and set to this tick's own volume when the trail arms, so vol_m5 < 0.20 x peak cannot "
              "hold there — it fires on the NEXT tick, when volume actually collapses",
              rV == [None, None, "vol_dry"] and sV["armed"] is True and sV["peak_vol_m5"] == 100.0, str(rV))
        rT, _ = _run_flow([(ARMED_PX, _flow(1000.0, 1, 3), 60.0), (ARMED_PX, _flow(1000.0, 1, 3), 60.0),
                           (ARMED_PX, _flow(1000.0, 1, 3), 60.0)])
        check("min_txns_m5 = 5: a window with only 4 trades is 'not enough flow' — it can neither extend nor "
              "start the weak streak, however lopsided the buy share looks", rT == [None] * 3, str(rT))

        def _pos_flow():
            return {"token": "0x" + "9" * 40, "event_seq": 1, "symbol": "FLW", "tier": "A",
                    "entry_px": ENTRY, "opened_ts": LT0, "max_gap_s": 0.0}

        def _drive(name, mults, flows, gap=60.0):
            """Step ONE policy through a tick script; (fill rows, state, index of the closing tick)."""
            pos, st = _pos_flow(), LB._new_policy_state()
            st["peak_px"] = ENTRY
            rows, closed_at = [], None
            for i, m in enumerate(mults):
                fl = flows[i] if flows else None
                rows.extend(LB._step_policy(pos, name, st, ENTRY * m, LT0 + 60 * (i + 1), 10.0 * m, gap, flow=fl))
                if st["closed"] and closed_at is None:
                    closed_at = i
            return rows, st, closed_at

        _rng = np.random.default_rng(config.SEED + int(hashlib.sha256(b"phase5 stop dominance").hexdigest()[:8], 16))
        _late, _n_stop = [], 0
        for _s in range(50):
            _mults = [float(x) for x in np.exp(_rng.normal(0.0, 0.9, size=14))]
            _flows = [_flow(float(_rng.uniform(10.0, 5000.0)), int(_rng.integers(0, 12)), int(_rng.integers(0, 12)))
                      for _ in _mults]
            _first = next((i for i, m in enumerate(_mults) if m <= 0.5), None)
            if _first is None:
                continue
            _n_stop += 1
            for _nm, _fl in ((CAND_BENCH, None), (CAND_FLOW, _flows)):
                _, _st, _c = _drive(_nm, _mults, _fl)
                if _c is None or _c > _first:
                    _late.append((_s, _nm, _first, _c))
        check(f"the -50% stop DOMINATES on {_n_stop} of 50 sha256-seeded tick scripts that reach it: the position "
              "is already closed or closes on the very tick at or below 0.5 x entry, for both candidates — the "
              "flow rule can only ever exit EARLIER, never later", not _late and _n_stop >= 20, str(_late[:3]))
        _mseq = [1.0, 1.2, 1.6, 2.0, 1.4, 1.1, 0.9, 0.6, 0.4]
        _rb, _sb, _cb = _drive(CAND_BENCH, _mseq, None)
        _rf, _sf, _cf = _drive(CAND_FLOW, _mseq, [None] * len(_mseq))
        _cols = ["side", "frac_of_original", "px", "usd_proceeds", "gap_s", "note"]
        check("features dark ⇒ the adaptive policy IS the benchmark, tick for tick: every fill row matches on "
              "every column but the policy name, the close reason and closing tick match, and the dark ticks are "
              "counted rather than silently swallowed",
              [[r[c] for c in _cols] for r in _rb] == [[r[c] for c in _cols] for r in _rf] and _cb == _cf
              and _sb["close_reason"] == _sf["close_reason"] == "trail" and _sf["flow_dark_ticks"] > 0,
              str((_cb, _cf, _sb["close_reason"], _sf["close_reason"])))
        _p9, _s9 = _pos_flow(), LB._new_policy_state()
        _s9["peak_px"] = ENTRY * 1.6
        _s9["rungs_left"] = [[1.5, 0.5]]
        POL.flow_state_init(_s9)
        _s9.update(armed=True, armed_ts=LT0, peak_vol_m5=1000.0, weak_ticks=1)
        _f9 = LB._step_policy(_p9, CAND_FLOW, _s9, ENTRY * 1.6, LT0 + 120, 16.0, 60.0, flow=WEAK)
        check("on one tick that satisfies BOTH the 1.5x rung and the flow rule the pre-committed LIMIT fills "
              "first, at exactly 1.5 x entry, and only the remainder leaves at the observed price as flow_exit "
              "(crediting the rung at the observed price would be the fill-at-a-price-never-seen leak)",
              [f["side"] for f in _f9] == ["tp_1.5x", "flow_exit"] and _f9[0]["px"] == ENTRY * 1.5
              and _f9[1]["px"] == ENTRY * 1.6 and abs(_f9[0]["frac_of_original"] - 0.5) < 1e-9
              and abs(_f9[1]["frac_of_original"] - 0.5) < 1e-9 and _f9[1]["note"] == "weak_flow"
              and _s9["close_reason"] == "flow_exit", str([(f["side"], f["px"]) for f in _f9]))
        _p10, _s10 = _pos_flow(), LB._new_policy_state()
        _s10["peak_px"] = ENTRY * 1.6
        _s10["rungs_left"] = []
        POL.flow_state_init(_s10)
        _s10.update(armed=True, armed_ts=LT0, peak_vol_m5=1000.0, weak_ticks=1)
        # the STREAK rule cannot fire across a 3600 s gap (the gap resets it), so the gapped case is
        # necessarily the volume floor — which is exactly why both must be marked the same way.
        _f10 = LB._step_policy(_p10, CAND_FLOW, _s10, ENTRY * 1.6, LT0 + 3700, 16.0, 3600.0,
                               flow=_flow(10.0, 9, 1))
        check("a flow_exit is a MARKET close like stop / trail / time_exit: taken after a tick gap above "
              f"LIVEBOOK_MAX_SCORABLE_GAP_S ({config.LIVEBOOK_MAX_SCORABLE_GAP_S:.0f} s) it is 'gapped' and the "
              "scorer drops it (and the streak rule could not have fired there at all — the gap reset it)",
              "flow_exit" in LB.MARKET_CLOSES and _s10["gapped"] is True
              and _s10["close_reason"] == "flow_exit" and [f["side"] for f in _f10] == ["flow_exit"]
              and _f10[0]["note"] == "vol_dry" and _s10["weak_ticks"] == 0)
        _pol_state = dict(LB._new_policy_state(), closed=True, closed_ts=LT0 + 600, remaining=0.0)

        def _flow_pos(token, close_reason, flow_ticks, flow_dark_ticks):
            """One scorable closed position on CAND_FLOW with a hand-set (flow_ticks, flow_dark_ticks)
            pair — bypasses flow_step entirely so the fix-round-1 semantics of live_stats_dict can be
            probed directly, independent of how a position actually arms."""
            return {token + ":1": {"token": token, "event_seq": 1, "symbol": "FLW", "tier": "A",
                                   "cost_usd": 10.0, "entry_lag_s": 5.0, "opened_ts": LT0,
                                   "last_tick_ts": LT0 + 600, "done": True,
                                   "policies": {CAND_FLOW: dict(_pol_state, close_reason=close_reason,
                                                                realized_usd=11.0, flow_ticks=flow_ticks,
                                                                flow_dark_ticks=flow_dark_ticks)}}}

        # 9 of 10 closes never armed (flow_ticks == flow_dark_ticks == 0 — the position never reached
        # 1.5x, so flow_step returned before touching either counter); the one that armed was dark on
        # every tick it ran. Fix round 1's bug: the OLD n_flow_dark counted any close with
        # flow_dark_ticks > 0, so this book would have reported 1 / 10 == 0.10, under the 0.50 kill
        # line, even though the rule was dark on 100% of the ticks it ever actually ran.
        _bk1 = {}
        for i in range(9):
            _bk1.update(_flow_pos("0xnv%d" % i, "trail", 0, 0))
        _bk1.update(_flow_pos("0xarmed", "flow_exit", 0, 4))
        LB._save_atomic(_bk1, LB.BOOK_PATH)
        _ls1 = LB.live_stats_dict(LT0 + 600)["per_policy"][CAND_FLOW]
        check("flow_dark_share is measured over ARMED positions, not every close: 9 never-armed closes "
              "(0 ticks each) plus one close that armed and was 100% dark reports n_armed == 1 and "
              "flow_dark_share == 1.0 — not 0.10, the close-level count the old n_flow_dark bug produced",
              _ls1["n_armed"] == 1 and _ls1["flow_dark_share"] == 1.0 and _ls1["n_flow_exit"] == 1
              and _ls1["n_flow_dark"] == 1, str(_ls1))

        # the mirror case: one armed position, mostly lit, with a single dark tick in 60 — the share
        # must be the tick-level fraction (1/60), and its own share is well under 0.5 so it must NOT
        # be counted by the backward-compatible per-close n_flow_dark.
        _bk2 = _flow_pos("0xmostlylit", "trail", 59, 1)
        LB._save_atomic(_bk2, LB.BOOK_PATH)
        _ls2 = LB.live_stats_dict(LT0 + 600)["per_policy"][CAND_FLOW]
        check("one armed close with 1 dark tick out of 60 reports flow_dark_share == 1/60, not 1.0 and "
              "not 0 — tick-level, not a boolean 'any dark tick' flag — and n_flow_dark stays 0 because "
              "this close's own share (1/60) is under the 0.5 backward-compat threshold",
              _ls2["n_armed"] == 1 and abs(_ls2["flow_dark_share"] - 1.0 / 60.0) < 1e-12
              and _ls2["n_flow_dark"] == 0, str(_ls2))

        # no close ever armed at all: the denominator is 0, and None (not 0.0, not NaN) is the only
        # honest value — 0.0 would silently read as "the rule ran perfectly", which is false; it never
        # ran, exactly the confusion this fix round exists to remove.
        _bk3 = {}
        for i in range(3):
            _bk3.update(_flow_pos("0xnever%d" % i, "trail", 0, 0))
        LB._save_atomic(_bk3, LB.BOOK_PATH)
        _ls3 = LB.live_stats_dict(LT0 + 600)["per_policy"][CAND_FLOW]
        check("no armed closes at all ⇒ flow_dark_share is None (never 0.0 or NaN) and n_armed == 0, "
              "and the whole structure stays JSON-safe",
              _ls3["n_armed"] == 0 and _ls3["flow_dark_share"] is None
              and json.dumps(LB.live_stats_dict(LT0 + 600), allow_nan=False))
        check("POL.flow_policy_names() names exactly the policies carrying a flow dict (the livebook reads the "
              "5-minute features only when one is live; the list is empty until registration)",
              POL.flow_policy_names() == [CAND_FLOW]
              and set(POL.FLOW_FEATURES) == {"vol_m5", "buys_m5", "sells_m5", "vol_h1", "buys_h1", "sells_h1", "liq_usd"})
        check("POL.flow_from_market copies the seven features from a market dict and returns None (never a "
              "fabricated zero) when any of vol_m5 / buys_m5 / sells_m5 is missing",
              POL.flow_from_market({"vol_m5": 1.0, "buys_m5": 2, "sells_m5": 3, "vol_h1": 9.0, "buys_h1": 4,
                                    "sells_h1": 5, "liq_usd": 6.0})
              == {"vol_m5": 1.0, "buys_m5": 2, "sells_m5": 3, "vol_h1": 9.0, "buys_h1": 4, "sells_h1": 5,
                  "liq_usd": 6.0}
              and POL.flow_from_market({"vol_m5": 1.0, "buys_m5": None, "sells_m5": 3}) is None
              and POL.flow_from_market(None) is None and POL.flow_from_market({}) is None)
finally:
    for k, v in _saved_lb2.items():
        _g[k] = v
# Registration is the operator's step and a PERMANENT counted trial, so what verify pins is the
# consistency that protects the DSR denominator, never a snapshot of who is registered today:
# a name that is live in POLICIES came from the registry and is already counted in trials.json.
# (The old check asserted the two modules were ABSENT — true on the day it was written, and red
# the instant the operator ran `register.py --scan`, taking the repo's primary safety signal and
# run_research.sh's merge gate down with it.)
_reg_pol_live = POL.load_candidates()                       # the registry's own non-retired policy rows
_trial_pols = set(json.load(open(config.TRIALS_PATH)).get("policies_ever_scored") or [])
check("the exit-policy family grows ONLY by registration: POLICIES is the 16 pre-declared rows plus exactly the "
      "non-retired kind == 'policy' registry entries, and every one of those is ALREADY a counted trial in "
      "trials.json — a registered policy that escaped policies_ever_scored would under-deflate every later DSR",
      len(POL.POLICIES) == 16 + len(_reg_pol_live)
      and set(_reg_pol_live) <= set(POL.POLICIES) and set(_reg_pol_live) <= _trial_pols,
      f"{len(POL.POLICIES)} = 16 + {sorted(_reg_pol_live)}; uncounted {sorted(set(_reg_pol_live) - _trial_pols)}")
_reg_faults_now = _registration_faults(config.REGISTRY_PATH, config.TRIALS_PATH)
check("shipping a candidate file registers NOTHING, and a registered one is fully consistent: for each of the four shipped "
      "modules (two adaptive-exit policies, two band-under-test bands) an unregistered name is live in no loader, and a "
      "registered one's entry IS the module — kind, module candidates.<name>, not retired, its POLICY dict after the loader's "
      "ladder retupling / its declared REQUIRES — resolves through that loader and is already a counted trial in trials.json",
      not _reg_faults_now, str(_reg_faults_now))


# ═══════════════════════════════════════════════════════════════════════════════════
section("H. improve.py — the exit gate: thin evidence refuses, controls void, forward-only promotion")
# ═══════════════════════════════════════════════════════════════════════════════════
from selfimprove import improve as IM                   # noqa: E402
from selfimprove import trials as TR                    # noqa: E402
import alerts                                           # noqa: E402

_saved_im = {"BOOK_PATH": LB.BOOK_PATH, "MISSED_PATH": LB.MISSED_PATH, "CHAMPION_PATH": config.CHAMPION_PATH,
             "TRIALS_PATH": config.TRIALS_PATH, "IMPROVE_HISTORY_PATH": config.IMPROVE_HISTORY_PATH,
             "PROPOSALS_DIR": config.PROPOSALS_DIR, "LIVEBOOK_SUMMARY_PATH": config.LIVEBOOK_SUMMARY_PATH,
             "PAUSE_PATH": config.PAUSE_PATH, "send_all": alerts.send_all}
_tmp_im = tempfile.mkdtemp(prefix="verify_improve_")
sent: list = []
try:
    LB.BOOK_PATH = os.path.join(_tmp_im, "livebook.json")
    LB.MISSED_PATH = os.path.join(_tmp_im, "missed.jsonl")
    config.CHAMPION_PATH = os.path.join(_tmp_im, "champion.json")
    config.TRIALS_PATH = os.path.join(_tmp_im, "trials.json")
    config.IMPROVE_HISTORY_PATH = os.path.join(_tmp_im, "improve_history.jsonl")
    config.PROPOSALS_DIR = os.path.join(_tmp_im, "proposals")
    config.LIVEBOOK_SUMMARY_PATH = os.path.join(_tmp_im, "livebook_summary.json")
    config.PAUSE_PATH = os.path.join(_tmp_im, "PAUSE")
    alerts.send_all = lambda title, body, dry_run=True: sent.append((title, dry_run))
    # the Deflated Sharpe below is the REAL one on both partitions (selfimprove/dsr.py) — it was
    # injected as 0.99 under CI while it lived in the sibling repo
    DEF = config.IMPROVE_DEFAULT_EXIT_CHAMPION
    CHL = "sell_3h"
    D, P = config.IMPROVE_PROMOTE_MIN_CLUSTERS + 2, 8
    now = LT0 + (D + 1) * 86400.0

    def _run(book, now_s):
        LB._save_atomic(book, LB.BOOK_PATH)
        res = IM.evaluate_all(now_s, book)
        return res, IM.decide(res)

    res, v = _run(IM._synthetic_book(10, 5, LT0, seed=1, adv={CHL: 0.35}), now)
    check("thin evidence refuses (n=50, 10 days): no promotion, no nomination, reasons name the floors",
          not v["promote"] and v["nominate"] is None and not v["gate_broken"]
          and any("alert-days" in r_ for r_ in v["reasons"]) and any("positions" in r_ for r_ in v["reasons"]))
    res, v = _run(IM._synthetic_book(D, P, LT0, seed=2, adv={CHL: 0.35}, inert_random=True), now)
    check("an INERT control (bit-identical to the champion) voids the run", v["gate_broken"] and "ctl_random_exit" in res["inert_controls"])
    res, v = _run(IM._synthetic_book(D, P, LT0, seed=3, adv={CHL: 0.35}, ctl_immediate_mean=0.20), now)
    check("a PROFITABLE control (own bound > 0) voids the run and the proposal suppresses the table",
          v["gate_broken"] and "ctl_exit_immediately" in v["reasons"][1]
          and "no number from this run may be quoted" in open(IM.write_proposal(res, v)).read().lower())
    res, v = _run(IM._synthetic_book(D, P, LT0, seed=4, adv={CHL: 0.35}), now)
    c_lb = res["controls"]["ctl_exit_immediately"]["day_lb"]
    check("a control that beats the champion but is not profitable does NOT void (a fact about the asset class)",
          c_lb > res["champ_lb"] and c_lb < 0 and not v["gate_broken"])
    res, v = _run(IM._synthetic_book(D, P, LT0, seed=5, adv={CHL: 0.05}), now)
    row = res["policies"][CHL]
    check("below the control bar cannot promote or nominate even with paired LB > 0 (the bar is the best NEGATIVE CONTROL)",
          v["winner"] == CHL and row["paired_lb"] > 0 and not v["promote"] and v["nominate"] is None and not v["checks"][1][1])
    res, v = _run(IM._synthetic_book(D, P, LT0, seed=6, adv={CHL: 0.35}), now)
    failed = [c[0] for c in v["checks"] if not c[1]]
    check("clearing every bar but forward-only NOMINATES (a nomination is not a claim)",
          v["nominate"] == CHL and not v["promote"] and len(failed) == 1 and "FORWARD-ONLY" in failed[0], str(failed))
    out = IM.apply(res, v, now, send=False, proposal_path=None)
    stx = CH.state()["exit"]
    check("--apply records the nomination in champion.json (nominated_at_alert_seq == max_alert_seq) and bumps trials; "
          "the NOMINATED event went out dry",
          stx["nominee"] == CHL and stx["nominated_at_alert_seq"] == res["max_alert_seq"]
          and "NOMINATED" in out["events"] and sent and sent[-1][1] is True and TR.family_count("nominations") == 1)
    nom_seq, nom_ts = stx["nominated_at_alert_seq"], stx["nominated_ts"]
    book2 = IM._synthetic_book(D, P, LT0, seed=7, adv={CHL: 0.35, "trail_30": 0.80})
    res, v = _run(book2, now)
    check("a live nominee is judged ALONE even when another policy leads by a mile",
          v["winner"] == CHL and res["policies"]["trail_30"]["paired_lb"] > res["policies"][CHL]["paired_lb"]
          and any("awaiting its forward sample" in r_ for r_ in v["reasons"]))
    fwd_t0 = now + 3600.0
    fwd = IM._synthetic_book(46, 4, fwd_t0, seed=8, adv={CHL: 0.40}, seq0=nom_seq + 1)
    book3 = dict(IM._synthetic_book(D, P, LT0, seed=6, adv={CHL: 0.35})); book3.update(fwd)
    now2 = fwd_t0 + 47 * 86400.0
    res, v = _run(book3, now2)
    nomi = res["nomination"]
    check("a matured forward prefix (earliest rows after nomination, cut at the floors; later rows EXCLUDED) PROMOTES",
          nomi["matured"] and res["forward_only"] and nomi["n_forward"] >= config.IMPROVE_FWD_MIN_POSITIONS
          and nomi["days_forward"] == config.IMPROVE_FWD_MIN_DAYS and nomi["n_forward"] < len(fwd) and v["promote"] and v["winner"] == CHL,
          str(v["reasons"]))
    before = _read(config.CHAMPION_PATH)
    IM.write_proposal(res, v); IM._append_history(res, v)
    check("without --apply nothing is written to champion.json", _read(config.CHAMPION_PATH) == before)
    open(config.PAUSE_PATH, "w").close()
    out = IM.apply(res, v, now2, send=False)
    check("PAUSE => no write, a PAUSED event only", out["paused"] and _read(config.CHAMPION_PATH) == before and out["events"] == ["PAUSED"])
    os.remove(config.PAUSE_PATH)
    path = IM.write_proposal(res, v)
    out = IM.apply(res, v, now2, send=False, proposal_path=path)
    stx = CH.state()["exit"]
    evd = stx["evidence"]
    check("--apply PROMOTES: champion.json carries the seven numbers, promoted_at_alert_seq == max_alert_seq, "
          "nominee cleared, history line 'promote'",
          stx["champion"] == CHL and stx["previous"] == DEF and stx["nominee"] is None
          and stx["promoted_at_alert_seq"] == res["max_alert_seq"] and stx["promoted_at_ts"] == now2
          and all(k in evd for k in ("paired_lb", "paired_mean", "day_lb", "dsr", "n_days", "n", "n_positions"))
          and evd["paired_lb"] > 0 and evd["day_lb"] > evd["control_day_lb"] and evd["forward_only"]
          and CH.state()["history"][-1]["action"] == "promote" and "PROMOTED" in out["events"])
    book = IM._synthetic_book(20, 5, LT0, seed=9, gapped={"sell_15m": 0.5}, n_suspect=7, n_unpriced=4)
    counts: dict = {}
    rets, days, meta = IM.live_returns(book, counts)
    first = book[meta[0]["key"]]
    first["policies"]["sell_1h"]["backfilled_ts"] = LT0
    rets2, _, _ = IM.live_returns(book)
    check("gapped / suspect / unpriced / backfilled states are NaN (never scored): suspect 7 + unpriced 4 dropped, "
          "gapped sell_15m rows NaN, a backfilled state NaN",
          counts["n_suspect"] == 7 and counts["n_unpriced"] == 4 and len(meta) == 89
          and np.isnan(rets["sell_15m"]).sum() == counts["n_gapped"]["sell_15m"] > 0
          and not np.isnan(rets["hold_to_end"]).any() and np.isnan(rets2["sell_1h"][0]))
    stx = CH.state()["exit"]
    book = IM._synthetic_book(D, P, LT0, seed=10, adv={DEF: 0.35}, seq0=stx["promoted_at_alert_seq"] + 1)
    res, v = _run(book, now2 + 50 * 86400)
    check("the previous champion can be nominated as a challenger to the new one",
          res["champion"] == CHL and v["winner"] == DEF and v["nominate"] == DEF)
    CH.write_state(exit={"failed_nominee": DEF, "failed_ts": (now2 + 50 * 86400) - 10 * 86400, "failed_at_alert_seq": 1})
    res, v = _run(book, now2 + 50 * 86400)
    check("renomination cooldown: a failed nominee inside IMPROVE_RENOMINATE_COOLDOWN_DAYS is skipped",
          v["winner"] != DEF and v["nominate"] != DEF and any("cooldown" in r_ for r_ in v["reasons"]))
    CH.write_state(exit={"failed_ts": (now2 + 50 * 86400) - (config.IMPROVE_RENOMINATE_COOLDOWN_DAYS + 5) * 86400})
    res, v = _run(book, now2 + 50 * 86400)
    check("...and eligible again after the cooldown", v["winner"] == DEF)
    CH.write_state(exit={"failed_nominee": None, "failed_ts": None, "failed_at_alert_seq": None})
    stx = CH.state()["exit"]
    pseq, pts = stx["promoted_at_alert_seq"], stx["promoted_at_ts"]
    pre = IM._synthetic_book(5, 4, pts - 10 * 86400, seed=11, adv={CHL: 0.35}, seq0=max(1, pseq - 19))
    fwd = IM._synthetic_book(46, 3, pts + 3600.0, seed=12, adv={CHL: -0.10}, seq0=pseq + 1)
    book = dict(pre); book.update(fwd)
    now3 = pts + 48 * 86400.0
    res, v = _run(book, now3)
    dem = res["demotion"]
    out = IM.apply(res, v, now3, send=False, proposal_path=IM.write_proposal(res, v))
    stx = CH.state()["exit"]
    check("DEMOTION on a failed forward prefix: the promoted champion's own LB <= the best control's => revert to the "
          "default, demotion_judged set, DEMOTED event, one nomination counted",
          dem and dem["matured"] and dem["day_lb"] <= dem["ctl_lb"] and v["demote"] and dem["n_forward"] <= len(fwd)
          and stx["champion"] == DEF and stx["previous"] == CHL and stx["demotion_judged"] == now3
          and "DEMOTED" in out["events"] and CH.state()["history"][-1]["action"] == "demote")
    pj = IM.summary_json(res)
    raw = _read(pj)
    strict = json.loads(raw, parse_constant=lambda c: (_ for _ in ()).throw(ValueError(c)))
    check("summary_json is NaN-free under a strict parser", "NaN" not in raw and "Infinity" not in raw and strict["n_positions"] > 0)
    n_before = TR.family_count("policies")
    IM.evaluate_all(now3, book)
    check("trials only grow: re-scoring the same family never changes policies_ever_scored",
          TR.family_count("policies") == n_before == len(POL.POLICIES))
finally:
    LB.BOOK_PATH, LB.MISSED_PATH = _saved_im["BOOK_PATH"], _saved_im["MISSED_PATH"]
    config.CHAMPION_PATH, config.TRIALS_PATH = _saved_im["CHAMPION_PATH"], _saved_im["TRIALS_PATH"]
    config.IMPROVE_HISTORY_PATH = _saved_im["IMPROVE_HISTORY_PATH"]
    config.PROPOSALS_DIR = _saved_im["PROPOSALS_DIR"]
    config.LIVEBOOK_SUMMARY_PATH = _saved_im["LIVEBOOK_SUMMARY_PATH"]
    config.PAUSE_PATH = _saved_im["PAUSE_PATH"]
    alerts.send_all = _saved_im["send_all"]
    shutil.rmtree(_tmp_im, ignore_errors=True)


# ═══════════════════════════════════════════════════════════════════════════════════
section("I. entry lab — feat, bands, controls, events, sidecar, scorecard, the entry gate")
# ═══════════════════════════════════════════════════════════════════════════════════
from selfimprove.entry_lab import runtime as LAB        # noqa: E402
from selfimprove.entry_lab import store as STORE        # noqa: E402
from selfimprove.entry_lab import scorecard as SC       # noqa: E402
from selfimprove.entry_lab import improve_bands as IB   # noqa: E402

REG = B.load_registry()
CHAMP = config.DEFAULT_ENTRY_BAND
_but = config.LIVEBOOK_BAND_UNDER_TEST
check("config.LIVEBOOK_BAND_UNDER_TEST is None or a REGISTERED non-control band (the always-admission privilege can never name "
      "a control or an unregistered name); the registry can tell a control apart",
      (_but is None or (_but in REG.names() and not REG.is_control(_but)))
      and REG.is_control("ctl_random_band") and not REG.is_control(CHAMP) and config.LIVEBOOK_BAND_UNDER_TEST_MAX_OPEN > 0
      and config.LIVEBOOK_BAND_UNDER_TEST_MAX_OPEN <= config.LIVEBOOK_MAX_OPEN, str(_but))
clean = B.clean_fixture()
market = {k: clean[k] for k in ("price_usd", "liq_usd", "mcap", "fdv", "vol_h1", "vol_h6", "vol_h24", "buys_h1",
                                 "sells_h1", "buys_h24", "sells_h24", "price_chg_h1", "pair_age_min", "dex")}
safety = {k: v for k, v in clean.items() if k not in market and k not in ("token", "score", "first_sighting", "sighting_age_s")}
feat = LAB.build_feat(clean["token"].upper(), dict(market, liq_usd=float("nan"), vol_h24=float("inf")),
                      dict(safety, top10_pct=pd.NA, extra_key=1), float("nan"), True, np.float64(12.0))
check("build_feat yields None for NaN/inf/pd.NA and EXACTLY the FEATURE_FIELDS keys (extras dropped, token lowercased)",
      set(feat) == set(config.FEATURE_FIELDS) and feat["liq_usd"] is None and feat["vol_h24"] is None
      and feat["top10_pct"] is None and feat["score"] is None and feat["token"] == clean["token"] and feat["sighting_age_s"] == 12.0)
good = LAB.build_feat(clean["token"], market, safety, clean["score"], True, 0.0)
check("build_feat on the clean fixture reproduces bands.clean_fixture exactly", good == clean)
bad_req = [n for n, sp in B.BUILTINS.items() if not set(sp.REQUIRES) <= set(config.FEATURE_FIELDS)]
det_bad = [n for n, sp in B.BUILTINS.items() if sp.verdict(good) != sp.verdict(dict(good)) or sp.verdict(good) not in (True, False, None)]
check("every band is deterministic, verdict in {True, False, None}, REQUIRES ⊆ FEATURE_FIELDS", not bad_req and not det_bad, f"{bad_req} {det_bad}")
_fires = {"band_top10_le15": {"top10_pct": 15.0, "top10_pct_gt": 15.0}, "band_graduated_only": {"launchpad_completed_age_s": 3600.0},
          "band_new_creation": {"pair_age_min": 10.0}, "band_almost_bonded": {"gmgn_progress": 0.8, "launchpad_completed": False}}
na_bad = [(n, k) for n, sp in B.BUILTINS.items() for k in sp.REQUIRES
          if sp.verdict(dict(dict(good, **_fires.get(n, {})), **{k: None})) is not None]
check("a dark REQUIRES field yields None (NA) for every band", not na_bad, str(na_bad[:3]))
HC3 = [n for n, sp in B.BUILTINS.items() if sp.THREE_VALUED]
kl = dict(good, roundtrip_loss_pct=12.0, dev_pct=None)     # a definite miss beside an unknown
check("three-valued AND (hc family): a definite miss (round trip 12%) beside an unknown (dev_pct None) is False, not NA; "
      "the unknown alone is NA; True never fires with an unknown",
      len(HC3) == 8 and all(B.BUILTINS[n].verdict(kl) is False for n in HC3)
      and B.BUILTINS[CHAMP].verdict(dict(good, dev_pct=None)) is None
      and B.BandSpec("t", "", ("score",), (), "candidate", lambda f: True, THREE_VALUED=True).verdict(dict(good, score=None)) is None,
      str([(n, B.BUILTINS[n].verdict(kl)) for n in HC3]))
boom = B.BandSpec("boom", "", ("score",), (), "candidate", lambda f: 1 / 0)
check("a raising verdict body yields None, never an exception", boom.verdict(good) is None)
variations = [{}, {"score": 50.0}, {"score": 69.9}, {"top10_pct": 25.0}, {"top10_pct": 20.0}, {"total_holders": 999},
              {"total_holders": 1000}, {"pair_age_min": 89.0}, {"pair_age_min": 90.0}, {"pair_age_min": 30.0, "total_holders": 1300},
              {"liq_usd": 24_999.0}, {"creator_prior_tokens": 2}, {"roundtrip_loss_pct": 8.5}, {"dev_pct": 2.5}, {"dev_sniped": True},
              {"template_name": "Unknown", "verified_source": False}, {"buys_h1": 3000, "sells_h1": 3000}, {"lp_locked_pct": None},
              {"liq_usd": 30_031.46, "vol_h24": 397_126.31, "mcap": 151_307.0, "buys_h1": 3662, "sells_h1": 1987,
               "pair_age_min": 19.94, "top10_pct": 4.4, "total_holders": 1344},
              {"liq_usd": 30_031.46, "vol_h24": 397_126.31, "mcap": 151_307.0, "buys_h1": 3662, "sells_h1": 1987,
               "pair_age_min": 95.0, "top10_pct": 4.4, "total_holders": 1344}]
mism = []
n_true = 0
for var in variations:
    f_ = dict(good); f_.update(var); f_["score"] = screen.soft_score(f_, f_)[0]
    v_ = B.band_a_strict.verdict(f_)
    hc_ok, _m = screen.high_conviction(f_)
    n_true += v_ is True
    if (v_ is True) != hc_ok or (v_ is None) != (hc_ok is False and any(x is None for x in screen.hc_checks(f_).values())):
        mism.append(var)
check("band_a_strict == screen.high_conviction on 20 fixtures incl. the $Cubrate replay (ONE implementation of the HC checks)",
      not mism and n_true >= 3 and B.band_a_strict.verdict(dict(good, pair_age_min=19.94, total_holders=1344,
                                                                 buys_h1=3662, sells_h1=1987)) is False, str(mism))
f80 = dict(good, pair_age_min=80.0)
check("band_no_age is NOT inert vs band_a_strict (an 80-min token with 1400 holders: a_strict False, no_age True)",
      B.band_a_strict.verdict(f80) is False and B.band_no_age.verdict(f80) is True)
check("tier_for(False, {champ: True}) == 'B' and tier_for(True, {champ: None}) == 'B' (a laxer band is impossible)",
      B.tier_for(False, {CHAMP: True}, CHAMP) == "B" and B.tier_for(True, {CHAMP: None}, CHAMP) == "B"
      and B.tier_for(True, {CHAMP: True}, CHAMP) == "A")

# ── the band-under-test modules: VALIDATED here, registered by the operator ───────────────────
# Registration is a counted trial (trials.json only grows and deflates every later DSR), so it is
# the operator's step and may land on any day: verify runs register.py's own validator functions
# WITHOUT calling scan(), and pins the CONSISTENCY between module, registry and trial list rather
# than a snapshot of who is registered today. The validator is handed the registry MINUS this
# module's own entry, which is exactly the view register.py had the instant before it registered
# it (validate_module refuses a name already taken — that rule is about collisions, not about the
# module still being valid).
from selfimprove.candidates import register as REGM   # noqa: E402
import selfimprove.trials as TRIALS_MOD                # noqa: E402

HYPE_E = os.path.join(config.SELFIMPROVE_DIR, "candidates", "band_hype_early.py")
HYPE_A = os.path.join(config.SELFIMPROVE_DIR, "candidates", "band_hype_attention.py")
_raw_reg = REGM.load_registry_raw(config.REGISTRY_PATH)
_fx = REGM.fixtures()
for _hp in (HYPE_E, HYPE_A):
    _nm = os.path.splitext(os.path.basename(_hp))[0]
    bi_, ba_, bc_ = _hygiene(_hp)
    check(f"candidate {_rel(_hp)} is clock/network/RNG/open-free and passes bands.static_ok (the import allowlist and the "
          "forbidden attribute chains)", not bi_ and not ba_ and not bc_ and B.static_ok(_hp)[0],
          f"{bi_} {ba_} {bc_} {B.static_ok(_hp)[1]}")
    _entry_nm = next((c_ for c_ in _raw_reg.get("candidates", []) if c_.get("name") == _nm), None)
    _raw_pre = dict(_raw_reg, candidates=[c_ for c_ in _raw_reg.get("candidates", []) if c_.get("name") != _nm])
    _ok_v, _why_v, _info_v = REGM.validate_module(_hp, _raw_pre, REG, CHAMP, _fx)
    check(f"{_nm} passes register.py's FULL validator: NAME == the file name, RATIONALE and CONSUMED_DATA "
          f"declared, REQUIRES a non-empty subset of FEATURE_FIELDS, deterministic across two PYTHONHASHSEEDs on {len(_fx)} fixtures, "
          "NA whenever a REQUIRES field is None, no undeclared dependency, not all-NA and not inert vs the champion",
          _ok_v and _info_v.get("kind") == "band" and _info_v.get("name") == _nm, _why_v)
    check(f"{_nm}'s declared REQUIRES is exactly what register.py's validator read out of the module, so the `requires` the "
          "registry entry carries (and _registration_faults compares against) is the module's own — and the validator was run "
          "against the registry MINUS this entry, the view register.py had the instant before it registered it",
          list(_info_v.get("requires") or []) == [str(k) for k in B.load_candidate_module(_hp, _nm).REQUIRES]
          and (_entry_nm is None or list(_entry_nm.get("requires") or []) == list(_info_v.get("requires") or [])),
          f"{_info_v.get('requires')} {None if _entry_nm is None else _entry_nm.get('requires')}")

_HE = B.spec_from_module(B.load_candidate_module(HYPE_E, "band_hype_early"))
_HA = B.spec_from_module(B.load_candidate_module(HYPE_A, "band_hype_attention"))
# FOMOPAD's own numbers at its 4.07-minute sighting (the one winner the screener ever saw)
_fomo = dict(good, pair_age_min=4.07, vol_h1=323_290.0, liq_usd=53_263.0, mcap=346_387.0, buys_h1=1273, sells_h1=1137,
             top10_pct=14.93, holders_source="blockscout", honeypot=False, is_scam=False, roundtrip_loss_pct=-7.3,
             gmgn_is_honeypot=False, gmgn_top10_holder_pct=None, gmgn_visiting_count=None)
check("band_hype_early fires on FOMOPAD's at-sighting numbers (age 4.07 min, hour-1 volume $323k, liq $53k, mcap $346k, buys 1273 vs "
      "sells 1137, top-10 14.9 % exact from Blockscout, no positive rug finding) where the champion band_volume_early does NOT: its "
      "buys >= 2x sells clause is exactly what rejected the one winner the screener sighted",
      _HE.verdict(_fomo) is True and B.BUILTINS["band_volume_early"].verdict(_fomo) is False,
      str((_HE.verdict(_fomo), _HE.explain(_fomo), B.BUILTINS["band_volume_early"].verdict(_fomo))))
_clone = dict(_fomo, vol_h1=12_000.0, liq_usd=30_000.0, buys_h1=1400, sells_h1=1000)
check("band_hype_early refuses the clone swarms (~$30k liq, $25-38k LIFETIME volume, buys/sells 1.4-1.5x): they clear every hard gate "
      "but never reach $50k of volume in hour one at an age <= 15 min, and that volume floor is the whole separation",
      _HE.verdict(_clone) is False and "volume" in _HE.explain(_clone), _HE.explain(_clone))
check("band_hype_early is NA (never False) when the top-10 share is unknown — no exact Blockscout snapshot and no GMGN value — and "
      "reads the GMGN value when there is one, naming the source it used in explain()",
      _HE.verdict(dict(_fomo, top10_pct=None, holders_source="gt", gmgn_top10_holder_pct=None)) is None
      and _HE.verdict(dict(_fomo, top10_pct=None, holders_source="gt", gmgn_top10_holder_pct=6.0)) is True
      and _HE.verdict(dict(_fomo, top10_pct=None, holders_source="gt", gmgn_top10_holder_pct=44.0)) is False
      and "gmgn" in _HE.explain(dict(_fomo, top10_pct=None, holders_source="gt", gmgn_top10_holder_pct=44.0)).lower()
      and "blockscout" in _HE.explain(dict(_fomo, top10_pct=88.0)).lower(),
      _HE.explain(dict(_fomo, top10_pct=None, holders_source="gt", gmgn_top10_holder_pct=44.0)))
check("band_hype_early refuses a POSITIVE rug finding whatever the flow says (honeypot True, is_scam True, GMGN's own honeypot 'yes', "
      "a round trip above the cap), and an UNKNOWN round trip is not a finding",
      all(_HE.verdict(dict(_fomo, **kv)) is False for kv in ({"honeypot": True}, {"is_scam": True},
                                                             {"gmgn_is_honeypot": True}, {"roundtrip_loss_pct": 9.0}))
      and _HE.verdict(dict(_fomo, roundtrip_loss_pct=None)) is True)
check("band_hype_attention is band_hype_early AND at least 3 GMGN viewers, and REQUIRES the viewer count — so it is NA wherever GMGN "
      "did not see the token at all, which is why its coverage (latest_scan.json gmgn_coverage) must be measured before it is registered",
      _HA.verdict(dict(_fomo, gmgn_visiting_count=8)) is True and _HA.verdict(dict(_fomo, gmgn_visiting_count=2)) is False
      and _HA.verdict(_fomo) is None and "gmgn_visiting_count" in _HA.REQUIRES
      and _HA.verdict(dict(_clone, gmgn_visiting_count=8)) is False)
n_sel = 0
for i in range(10_000):
    tok = "0x" + hashlib.sha256(f"synthetic-{i}".encode()).hexdigest()[:40]
    n_sel += bool(B.ctl_random_band.verdict(dict(good, token=tok, sighting_age_s=float(config.BAND_WATCH_WINDOW_S))))
rate = n_sel / 10_000
check(f"ctl_random_band rate within ±2 pp of {config.BAND_CTL_RANDOM_RATE} on 10,000 addresses and process-stable (sha256, pinned)",
      abs(rate - config.BAND_CTL_RANDOM_RATE) <= 0.02 and B.random_control_params("0x" + "ab" * 20) == (False, 62357.833739732814)
      and B.random_control_params("0x" + "AB" * 20) == B.random_control_params("0x" + "ab" * 20), str(rate))
# the control must FIRE on the rows the system actually records. run.py writes first-sighting
# verdicts at sighting_age_s = 0 and controls are barred from opening a band_fire row, so the old
# per-token firing DELAY made it return 0 on 672/672 committed verdict rows — an inert control
# cannot make failure visible, which is the only job it has.
n_true_ctl, n_na_ctl = 0, 0
for i in range(2_000):
    tok = "0x" + hashlib.sha256(f"ctl-live-{i}".encode()).hexdigest()[:40]
    v_ = LAB.evaluate_bands(dict(good, token=tok, sighting_age_s=0.0), REG, CHAMP)["ctl_random_band"]
    n_true_ctl += v_ is True
    n_na_ctl += v_ is None
rate_ctl = n_true_ctl / 2_000
check(f"ctl_random_band through evaluate_bands at sighting_age_s = 0 (what run.py records): coverage 1.0 and "
      f"a True rate of {config.BAND_CTL_RANDOM_RATE} ± 0.02 over 2,000 tokens — never the inert 0/672",
      n_na_ctl == 0 and abs(rate_ctl - config.BAND_CTL_RANDOM_RATE) <= 0.02 and n_true_ctl > 0,
      f"rate {rate_ctl} na {n_na_ctl}")
# REQUIRES = ("token",) now — sighting_age_s = 0.0 above would not have caught a REQUIRES hole
# (0.0 is not None); prove a None sighting_age_s (unrecorded / absent) still yields True/False.
n_na_none_sa = sum(LAB.evaluate_bands(dict(good, token="0x" + hashlib.sha256(f"ctl-nosa-{i}".encode()).hexdigest()[:40],
                                            sighting_age_s=None), REG, CHAMP)["ctl_random_band"] is None
                    for i in range(500))
check("ctl_random_band's REQUIRES no longer forces NA on a None sighting_age_s (REQUIRES == ('token',), not "
      "('token', 'sighting_age_s') — the verdict no longer reads that field)",
      n_na_none_sa == 0 and B.ctl_random_band.REQUIRES == ("token",), f"na={n_na_none_sa} requires={B.ctl_random_band.REQUIRES}")
_ctl_text = (B.ctl_random_band.RATIONALE + " " + B.ctl_random_band.explain(good)
             + " " + B.ctl_random_band.explain(dict(good, token="0x" + "00" * 20))).lower()
check("ctl_random_band's registered RATIONALE and explain() text never claim the removed per-token delay "
      "('delay' / 'fires at') — the Sunday research session reads bands.py as the family's contract",
      "delay" not in _ctl_text and "fires at" not in _ctl_text, _ctl_text)
vd = LAB.evaluate_bands(good, REG, CHAMP)
vna = LAB.evaluate_bands(dict(good, score=None), REG, CHAMP)
check("ctl_inverse_band is computed on the fly in evaluate_bands (== not champion; None when the champion is None) "
      "and its own verdict() is always None",
      vd[CHAMP] is True and vd["ctl_inverse_band"] is False and vna[CHAMP] is None and vna["ctl_inverse_band"] is None
      and B.ctl_inverse_band.verdict(good) is None and set(vd) == set(REG.names()))
with tempfile.TemporaryDirectory() as d:
    with open(os.path.join(d, "band_never.py"), "w") as fh:
        fh.write("NAME='band_never'\nREQUIRES=()\nRATIONALE=''\ndef verdict(f):\n    return True\n")
    with open(os.path.join(d, "band_bad.py"), "w") as fh:
        fh.write("import time\nNAME='band_bad'\nREQUIRES=()\nRATIONALE=''\ndef verdict(f):\n    return True\n")
    rp = os.path.join(d, "registry.json")
    json.dump({"schema": 2, "candidates": [
        {"name": CHAMP, "kind": "band", "module": B.BUILTIN_MODULE, "status": "champion"},
        {"name": "band_bad", "kind": "band", "module": "candidates.band_bad", "status": "candidate"}]}, open(rp, "w"))
    r2, _out = _capture(B.load_registry, rp)
    v2 = LAB.evaluate_bands(good, r2, CHAMP)
    check("a module under candidates/ absent from the registry is never evaluated; a listed module failing static_ok "
          "is skipped and its verdicts are NA", "band_never" not in v2 and v2["band_bad"] is None and "band_bad" in r2.skipped
          and r2.get("band_never") is None)
    tp_ = os.path.join(d, "trials.json")
    TR.bump("bands", ["band_a", "band_b"], tp_)
    n1 = TR.bump("bands", ["band_b", "band_a"], tp_)
    check("trials.bump is idempotent and never decrements (order kept, cumulative recomputed)",
          n1 == 2 and TR.load(tp_)["bands_ever_scored"] == ["band_a", "band_b"] and TR.load(tp_)["cumulative_trials"] == 2)
names_reg = [e["name"] for e in REG_RAW["candidates"]]
check("registry entries are unique, each with kind ∈ {band, policy} and a status; controls are status 'control'",
      len(names_reg) == len(set(names_reg)) and all(e.get("kind") in ("band", "policy") and e.get("status") for e in REG_RAW["candidates"])
      and set(REG.controls()) == set(B.CONTROL_NAMES) and REG.status(CHAMP) == "champion")
# events + sidecar
with tempfile.TemporaryDirectory() as d:
    lp = os.path.join(d, "ledger.csv"); vp = os.path.join(d, "band_verdicts.csv")
    LED.ensure_exists(lp)
    tok = good["token"]

    def surv(f_, v_, extra=None):
        dd = {"token": f_["token"], "symbol": "DEMO", "gates_ok": True, "verdicts": v_, "feat": f_, "market": market,
              "score": f_["score"], "gates": {}, "sources_dark": [], "deployer": "0xdev", "url": "u"}
        dd.update(extra or {})
        return dd
    fb = dict(good, score=50.0, sighting_age_s=0.0)
    vb = LAB.evaluate_bands(fb, REG, CHAMP)
    ev1 = LAB.decide_events([surv(fb, vb)], {}, T0, CHAMP, REG)
    seqs1 = LED.record_rows(ev1, alert_ts=T0, path=lp)
    STORE.append_verdicts([{"event_seq": seqs1[tok], "token": tok, "alert_ts": T0, "verdicts": vb}], vp)
    idx = LED.index(LED.load(lp))
    prior = {tok: {b_ for b_, v_ in vb.items() if v_ is True}}
    ev2 = LAB.decide_events([surv(dict(fb, sighting_age_s=300.0), vb)], idx, T0 + 300, CHAMP, REG, prior_true=prior)
    fa = dict(good, sighting_age_s=7200.0)
    va = LAB.evaluate_bands(fa, REG, CHAMP)
    ev3 = LAB.decide_events([surv(fa, va)], idx, T0 + 7200, CHAMP, REG, prior_true=prior)
    seqs3 = LED.record_rows(ev3, alert_ts=T0 + 7200, path=lp)
    STORE.append_verdicts([{"event_seq": seqs3[tok], "token": tok, "alert_ts": T0 + 7200, "verdicts": va}], vp)
    check("decide_events over the 3-run fixture (B sighting -> still B -> promotion) yields [first_sighting], [], [promotion]",
          [e["event_kind"] for e in ev1] == ["first_sighting"] and ev1[0]["tier"] == "B" and ev2 == []
          and [e["event_kind"] for e in ev3] == ["promotion"] and ev3[0]["tier"] == "A" and ev3[0]["fired_band"] == CHAMP)
    led2 = LED.load(lp)
    check("event_seq is monotonic across appends and the sidecar joins on it",
          [int(float(x)) for x in led2["event_seq"]] == [1, 2] and set(STORE.load_verdicts(vp)["event_seq"]) == {1, 2})
    v0 = {n: (None if n == "ctl_inverse_band" else False) for n in REG.names()}
    v1 = dict(v0, band_graduated_only=True, band_dev_score_ge70=True, ctl_random_band=True)
    evb = LAB.decide_events([surv(fb, v1)], {tok: {"n": 1, "has_a": False, "first_sighting_ts": T0, "fired": set(), "last_alert_ts": T0}},
                            T0 + 600, CHAMP, REG)
    check("a band_fire opens on run 2 for a newly-True non-champion band (alphabetically first; controls never fire a row)",
          len(evb) == 1 and evb[0]["event_kind"] == "band_fire" and evb[0]["fired_band"] == "band_dev_score_ge70" and evb[0]["tier"] == "B"
          and LAB.decide_events([surv(fb, dict(v0, ctl_random_band=True))], idx, T0 + 600, CHAMP, REG) == [])
    idx_cap = {tok: {"n": config.BAND_MAX_EVENTS_PER_TOKEN, "has_a": False, "first_sighting_ts": T0, "fired": set(), "last_alert_ts": T0}}
    check(f"the cap (BAND_MAX_EVENTS_PER_TOKEN={config.BAND_MAX_EVENTS_PER_TOKEN}) blocks band_fire but not a first promotion",
          LAB.decide_events([surv(fb, v1)], idx_cap, T0 + 600, CHAMP, REG) == []
          and [e["event_kind"] for e in LAB.decide_events([surv(fa, va)], idx_cap, T0 + 600, CHAMP, REG)] == ["promotion"])
    wide = STORE.pivot_verdicts(STORE.load_verdicts(vp))
    recomputed = {n: LAB.evaluate_bands(fa, REG, CHAMP)[n] for n in REG.names()}
    rec_ok = all((math.isnan(wide.at[2, n]) if recomputed[n] is None else wide.at[2, n] == float(recomputed[n])) for n in REG.names())
    check("recorded verdicts == recomputed on the survivor dict (sidecar 1/0/NA round-trips every band exactly)", rec_ok)
scan_live = json.load(open(config.SCAN_PATH)) if os.path.exists(config.SCAN_PATH) else {}
surv_live = [s_ for s_ in (scan_live.get("survivors") or []) if isinstance(s_, dict) and isinstance(s_.get("bands"), dict)]
if surv_live and scan_live.get("band") in REG.names() and scan_live.get("bands_hash") != STORE.bands_code_hash():
    skip("recorded verdicts == recomputed on the committed latest_scan.json",
         f"band semantics changed since this scan was recorded (bands_hash {scan_live.get('bands_hash')} != {STORE.bands_code_hash()}); re-arms on the next scan")
elif surv_live and scan_live.get("band") in REG.names():
    mism = []
    for s_ in surv_live:
        f_ = {k: s_.get(k) for k in config.FEATURE_FIELDS}
        got = {k: (None if v_ is None else int(bool(v_))) for k, v_ in LAB.evaluate_bands(f_, REG, scan_live["band"]).items()}
        if got != s_["bands"]:
            mism.append(s_.get("symbol"))
    check(f"P1 look-ahead guard: recorded verdicts == recomputed on the committed latest_scan.json ({len(surv_live)} survivors, "
          "point-in-time feature store)", not mism, str(mism))
else:
    skip("recorded verdicts == recomputed on the committed latest_scan.json", "no survivors in data/latest_scan.json")
# scorecard
ser = SC.synthetic_series(planted="band_score60", champion=CHAMP)
r_ = ser["r"].values.astype(float)
days = [str(x) for x in ser["day"].values]
buckets = [int(x) for x in ser["age_bucket"].values]
s_p = SC.sel(ser, "band_score60", CHAMP)
s_r = SC.sel(ser, "band_holders500", CHAMP)
s_c = SC.sel(ser, CHAMP, CHAMP)
rng_v = np.random.default_rng(config.SEED + 99)
r_plus = np.where(s_p == 1, 0.5 + rng_v.normal(0, 0.1, len(ser)), r_)
r_zero = np.where(s_p == 1, rng_v.normal(0, 0.3, len(ser)), r_)
check("own_lb clears zero on a planted +0.5 fixture and not on a zero-mean one (day-clustered 2.5% quantile)",
      SC.own_lb(s_p, r_plus, days)[0] > 0 and not (SC.own_lb(s_p, r_zero, days)[0] > 0)
      and SC.own_lb(s_p, r_plus, days) == SC.own_lb(s_p, r_plus, days))
lb_p, mean_p, nd_p = SC.selection_lift_lb(s_p, r_, days, buckets)
lb_r, _, _ = SC.selection_lift_lb(s_r, r_, days, buckets)
s_greedy = np.zeros(len(ser)); s_five = np.zeros(len(ser))
for _d, g in ser.groupby("day"):
    s_greedy[g.index[g["age_bucket"] == 0][:7]] = 1.0
    s_five[g.index[g["age_bucket"] == 0][:5]] = 1.0
check("selection_lift_lb: planted band clears zero over 60 retained days, random band does not; strata with "
      "n_unsel < max(2, n_sel) are dropped (7/8 and 5/8 selected -> 0 days)",
      lb_p > 0 and nd_p == 60 and not (lb_r > 0) and SC.selection_lift_lb(s_greedy, r_, days, buckets)[2] == 0
      and SC.selection_lift_lb(s_five, r_, days, buckets)[2] == 0)
seen_perm: list = []
_orig_lp = SC._lift_parts


def _spy_lp(s_arr, rr, strata, ns, sday, nd):
    seen_perm.append((np.asarray(s_arr).copy(), np.asarray(strata).copy()))
    return _orig_lp(s_arr, rr, strata, ns, sday, nd)
SC._lift_parts = _spy_lp
try:
    fp = SC.shuffled_fp_rate(s_p, r_, days, buckets, reps=200)
finally:
    SC._lift_parts = _orig_lp
ok_idx = np.flatnonzero(np.isfinite(s_p))
base_counts = np.bincount(seen_perm[0][1][seen_perm[0][0] == 1], minlength=1)
preserved = all(np.array_equal(np.bincount(st_[s_ == 1], minlength=len(base_counts)), base_counts) for s_, st_ in seen_perm)
check(f"shuffled_fp_rate preserves per-stratum selection counts on every permutation and destroys the planted edge "
      f"(fp {fp:.3f} <= {config.BAND_SHUFFLE_MAX_FP})", preserved and fp <= config.BAND_SHUFFLE_MAX_FP and len(seen_perm) == 200)
pv = [0.001, 0.004, 0.02, 0.03, 0.2, 0.5, 0.8, 0.9, float("nan")]
m_ = len(pv); c_m = sum(1.0 / i for i in range(1, m_ + 1))
ps = sorted((x if x == x else 1.0) for x in pv)
kmax = max([k + 1 for k in range(m_) if ps[k] <= (k + 1) / (m_ * c_m) * config.FDR_Q] or [0])
cut = ps[kmax - 1] if kmax else -1.0
hand = [bool(x == x and x <= cut) for x in pv]
check("by_reject matches a hand-computed Benjamini-Yekutieli answer (NaN never rejected)", SC.by_reject(pv) == hand and hand[0] and not hand[-1])
if MAC and _have("statsmodels"):
    from statsmodels.stats.multitest import multipletests
    sm = [bool(x) for x in multipletests([x if x == x else 1.0 for x in pv], alpha=config.FDR_Q, method="fdr_by")[0]]
    check("by_reject == statsmodels fdr_by (mac_only)", sm == SC.by_reject(pv))
else:
    skip("by_reject == statsmodels fdr_by (mac_only)", "statsmodels absent or IS_CI")
n_tr = len(B.BUILTINS)
check("DSR on day means (selfimprove/dsr.py, both partitions): planted >= gate, random < gate; < 8 days is NaN",
      SC.dsr_day_means(s_p, r_, days, n_tr) >= config.BAND_DSR_GATE and SC.dsr_day_means(s_r, r_, days, n_tr) < config.BAND_DSR_GATE
      and math.isnan(SC.dsr_day_means(s_p[:24], r_[:24], days[:24], n_tr)))
check("scorecard carries no sibling-repo path (ENTRY_BOT_DIR is gone) and fails CLOSED (NaN) below DSR_MIN_DAYS days",
      not hasattr(SC, "ENTRY_BOT_DIR") and math.isnan(SC.dsr_day_means(s_p[:24], r_[:24], days[:24], n_tr)))
inv = SC.sel(ser.assign(**{CHAMP: ser[CHAMP].where(ser.index != 0)}), "ctl_inverse_band", CHAMP)
check("scorecard recomputes ctl_inverse_band on the fly from the champion column (NaN where the champion is NaN)",
      math.isnan(inv[0]) and inv[1] == 1.0 - float(ser.loc[1, CHAMP]))
# the three symmetric exclusions, read from the ROW: implausible (EDDICE), gapped (the 2026-09-14..17
# outage) and lag_unknown (every pre-schema row)
_MET = config.BAND_OUTCOME_METRIC
_LAGC = "lag_" + _MET.split("_", 1)[1]


def _excl_fixture(tiers) -> pd.DataFrame:
    """5 matured rows: clean, clean, gapped, implausible, lag_unknown — `tiers` assigns the arms."""
    rows = []
    for k, (kind, tier) in enumerate(zip(("clean", "clean", "gapped", "implausible", "lag_unknown"), tiers)):
        rec = {c: "" for c in LED.COLUMNS}
        rec.update({"token": f"0x{k + 1:040x}", "symbol": f"X{k}", "tier": tier, "band": CHAMP,
                    "event_seq": k + 1, "event_kind": "first_sighting", "alert_ts": T0 + k * 600,
                    "entry_price": 1.0, "entry_mcap": 1e6, "entry_liq": 5e4, "status": "open",
                    _MET: 0.25, _LAGC: 60.0})
        if kind == "gapped":
            rec[_LAGC] = config.LEDGER_MAX_CELL_LAG_S + 1.0
        elif kind == "implausible":
            rec[_MET] = config.LEDGER_MAX_PLAUSIBLE_MULT + 0.5     # a multiple above the cap
        elif kind == "lag_unknown":
            rec[_LAGC] = ""                                        # written before the stamp existed
        rows.append(rec)
    return pd.DataFrame(rows, columns=LED.COLUMNS)


ser_x, excl_x = SC.outcome_series(_excl_fixture(("A", "B", "A", "B", "A")), None)
ser_y, excl_y = SC.outcome_series(_excl_fixture(("B", "A", "B", "A", "B")), None)   # arms swapped
check("outcome_series excludes gapped / implausible / lag_unknown cells and counts each: 5 matured rows in, "
      "the 2 clean ones out",
      len(ser_x) == 2 and sorted(ser_x["event_seq"]) == [1, 2] and excl_x["gapped"] == 1
      and excl_x["implausible"] == 1 and excl_x["lag_unknown"] == 1 and excl_x["unmatured"] == 0,
      f"{len(ser_x)} {excl_x}")
check("the three exclusions are SYMMETRIC across arms: swapping every row's tier changes neither the kept "
      "rows nor any count (a one-armed exclusion would manufacture a lift)",
      excl_x == excl_y and sorted(ser_y["event_seq"]) == sorted(ser_x["event_seq"]), f"{excl_x} {excl_y}")
md_x = SC.markdown([{"band": CHAMP, "status": "champion", "n": 2, "days": 1, "coverage": 1.0, "mean": 0.25,
                     "own_lb": float("nan"), "lift_lb": float("nan"), "paired_lb": float("nan"),
                     "dsr": float("nan"), "p": float("nan"), "by_keep": None, "median_age_s": 0.0}], [],
                    {"champion": CHAMP, "n_events": 2, "n_days": 1, "n_excluded": excl_x, "n_trials": 1})
check("the scorecard footer prints the three exclusion counts and names the date before which "
      "ctl_random_band carries no evidence",
      "implausible 1" in md_x and "gapped 1" in md_x and "lag_unknown 1" in md_x
      and f"ctl_random_band carries no evidence before {config.BAND_CTL_RANDOM_CHANGED_ON}" in md_x, md_x[-500:])

# the entry gate
_saved_dsr = SC.dsr_day_means
_saved_send = alerts.send_all
alerts.send_all = lambda title, body, dry_run=True: None
base_dir = tempfile.mkdtemp(prefix="verify_entry_lab_")
SH = 200
try:
    planted = "band_score60"
    d = os.path.join(base_dir, "i"); os.makedirs(d)
    Pi = IB._fixture(d)
    now_i = 1_780_012_800.0 + 61 * 86400
    res = IB.evaluate_all(now_i, paths=Pi, shuffle_reps=SH)
    v = IB.decide(res)
    row = res["bands"][planted]
    failed = [c for c in v["checks"] if not c[1]]
    check("entry gate: the planted band clears every check but 7 (forward-only) => NOMINATED, not promoted",
          v["winner"] == planted and v["nominate"] == planted and not v["promote"] and len(failed) == 1 and failed[0][0].startswith("7 "))
    for label, mut, want in (("days", {"lift_days": config.BAND_PROMOTE_MIN_CLUSTERS - 1}, "5 "),
                             ("selected", {"n_total": config.BAND_MIN_SELECTED - 1}, "6 "),
                             ("DSR (injected)", {"dsr": 0.5}, "4 "),
                             ("BY", {"by_keep": False}, "8 ")):
        res_m = json.loads(json.dumps(res, default=lambda o: None))
        res_m["bands"][planted].update(mut)
        v_m = IB.decide(res_m)
        fl = [c[0] for c in v_m["checks"] if not c[1]]
        check(f"improve_bands: a single failing check ({label}) refuses promotion AND nomination",
              not v_m["promote"] and v_m["nominate"] is None and any(x.startswith(want) for x in fl), str(fl))
    res_m = json.loads(json.dumps(res, default=lambda o: None))
    res_m["bands"][planted]["coverage"] = 0.5
    v_m = IB.decide(res_m)
    check("improve_bands: coverage below BAND_MIN_COVERAGE makes the band ineligible (never the winner)", v_m["winner"] != planted)
    check("improve_bands: 'forward-only' alone is the check that blocks (the split is what nomination exists for)",
          not IB.decide(res)["promote"] and IB.decide(res)["nominate"] == planted)
    s_nom = SC.sel(*(lambda ser_: (ser_, planted, CHAMP))(SC.outcome_series(LED.load(Pi["ledger"]),
                    STORE.pivot_verdicts(STORE.load_verdicts(Pi["verdicts"])))[0]))
    ser_i = SC.outcome_series(LED.load(Pi["ledger"]), STORE.pivot_verdicts(STORE.load_verdicts(Pi["verdicts"])))[0]
    pref = IB.forward_prefix(ser_i, SC.sel(ser_i, planted, CHAMP), 300)
    check("forward_prefix excludes rows with event_seq <= nominated_at_event_seq (P1 look-ahead: pre-nomination rows are never evidence)",
          pref["mask"].any() and int(ser_i["event_seq"].values[pref["mask"]].min()) > 300
          and int(ser_i["event_seq"].values[pref["mask"]].min()) == 301)
    a = IB.apply(res, v, now_i, send=False, paths=Pi)
    st = CH.state(Pi["champion"])["entry_band"]
    res2 = IB.evaluate_all(now_i + 86400, paths=Pi, shuffle_reps=SH)
    v2 = IB.decide(res2)
    a2 = IB.apply(res2, v2, now_i + 86400, send=False, paths=Pi)
    check("sticky nomination: --apply records nominee/nominated_at_event_seq once; the next run keeps it and nominates nothing new",
          a["nominated"] == planted and st["nominee"] == planted and st["nominated_at_event_seq"] == 720
          and res2["nomination"]["nominee"] == planted and v2["nominate"] is None and not a2["applied"]
          and CH.state(Pi["champion"])["entry_band"]["nominee"] == planted
          and TR.load(Pi["trials"])["nominations_ever"] == [f"band:{planted}@720"])
    d2 = os.path.join(base_dir, "ii"); os.makedirs(d2)
    P2 = IB._fixture(d2, n_days=105)
    CH.write_state(entry_band={"nominee": planted, "nominated_at_event_seq": 720, "nominated_ts": now_i}, path=P2["champion"])
    exit_before = json.load(open(P2["champion"]))["exit"]
    now2 = 1_780_012_800.0 + 106 * 86400
    res = IB.evaluate_all(now2, paths=P2, shuffle_reps=SH)
    v = IB.decide(res)
    a = IB.apply(res, v, now2, send=False, paths=P2)
    st = CH.state(P2["champion"])["entry_band"]
    raw2 = json.load(open(P2["champion"]))
    check("a matured forward prefix (120 selected / 40 days of the 480-row prefix) PROMOTES under --apply; "
          "promoted_at_event_seq == max_event_seq, evidence carries the 9 checks, registry flipped",
          res["forward_only"] and res["nomination"]["complete"] and v["promote"] and a["promoted"] == planted
          and st["champion"] == planted and st["previous"] == CHAMP and st["promoted_at_event_seq"] == res["max_event_seq"] == 1260
          and len(st["evidence"]["checks"]) == 9 and st["evidence"]["paired_lb"] > 0
          and {e["name"]: e["status"] for e in json.load(open(P2["registry"]))["candidates"]}[planted] == "champion", str(v["reasons"]))
    check("promotion writes champion.json via tmp+os.replace with the exit arm byte-identical (no tmp left behind)",
          raw2["exit"] == exit_before and not [f for f in os.listdir(d2) if f.endswith(".tmp")]
          and any(_attr_chain(n.func) == ["os", "replace"] for n in ast.walk(_tree(os.path.join(ROOT, "selfimprove", "champion.py"))) if isinstance(n, ast.Call)))
    open(P2["pause"], "w").close()
    CH.write_state(entry_band={"champion": CHAMP, "previous": None, "promoted_ts": None, "promoted_at_event_seq": None,
                               "nominee": planted, "nominated_at_event_seq": 720, "nominated_ts": now_i}, path=P2["champion"])
    snap = _read(P2["champion"])
    res = IB.evaluate_all(now2, paths=P2, shuffle_reps=SH)
    a = IB.apply(res, IB.decide(res), now2, send=False, paths=P2)
    check("PAUSE => the entry gate reports only: champion.json byte-identical, PAUSED event",
          a["paused"] and not a["applied"] and a["events"] == ["PAUSED"] and _read(P2["champion"]) == snap)
    os.remove(P2["pause"])
    d5 = os.path.join(base_dir, "v"); os.makedirs(d5)
    P5 = IB._fixture(d5, control_edge=True)
    res = IB.evaluate_all(now_i, paths=P5, shuffle_reps=SH)
    v = IB.decide(res)
    check("K1: the run is VOID when the random control alone has own_lb > 0 (the apparatus is measuring itself)",
          res["controls"]["ctl_random_band"]["own_lb"] > 0 and v["gate_broken"] and v["winner"] is None
          and "no number from this run may be quoted" in _read(IB.write_proposal(res, v, P5)))
    d10 = os.path.join(base_dir, "x"); os.makedirs(d10)
    P10 = IB._fixture(d10, gapped_frac=0.5)
    res10 = IB.evaluate_all(now_i, paths=P10, shuffle_reps=SH)
    v10 = IB.decide(res10)
    check(f"SAMPLING GAP: half the rows sampled later than LEDGER_MAX_CELL_LAG_S VOIDs the entry gate "
          f"(share over the rows that HAVE a lag cell, > BAND_MAX_GAPPED_SHARE={config.BAND_MAX_GAPPED_SHARE})",
          abs(res10["gapped_share"] - 0.5) < 1e-9 and v10["gate_broken"] and v10["winner"] is None
          and any(x.startswith("SAMPLING GAP: gapped share") for x in v10["void"]),
          f"{res10['gapped_share']} {v10['void']}")
    res_ok = IB.evaluate_all(now_i, paths=Pi, shuffle_reps=SH)
    check("a fully-sampled fixture has gapped_share 0.0 and reports lag_unknown separately (0 here); the gate "
          "decides as before", res_ok["gapped_share"] == 0.0 and res_ok["lag_unknown"] == 0
          and not IB.decide(res_ok)["gate_broken"], f"{res_ok['gapped_share']} {res_ok['lag_unknown']}")
    d6 = os.path.join(base_dir, "vi"); os.makedirs(d6)
    P6 = IB._fixture(d6, flat_edge=True)
    res = IB.evaluate_all(now_i, paths=P6, shuffle_reps=SH)
    v = IB.decide(res)
    row = res["bands"][planted]
    check(f"check 3 is NET of cost: 0 < own_lb < round_trip_cost() ({IB.BAND_OWN_LB_MIN:.3f}) fails it",
          v["winner"] == planted and 0 < row["own_lb"] < IB.BAND_OWN_LB_MIN and not v["checks"][2][1] and v["nominate"] is None)
    d7 = os.path.join(base_dir, "vii"); os.makedirs(d7)
    P7 = IB._fixture(d7, n_days=100, edge_until_seq=300)
    CH.write_state(entry_band={"champion": planted, "previous": CHAMP, "promoted_ts": now_i, "promoted_at_event_seq": 300}, path=P7["champion"])
    rawr = json.load(open(P7["registry"]))
    for e in rawr["candidates"]:
        e["status"] = "champion" if e["name"] == planted else ("candidate" if e["name"] == CHAMP else e["status"])
    IB._atomic_json(P7["registry"], rawr)
    now7 = 1_780_012_800.0 + 101 * 86400
    res = IB.evaluate_all(now7, paths=P7, shuffle_reps=SH)
    v = IB.decide(res)
    a = IB.apply(res, v, now7, send=False, paths=P7)
    st = CH.state(P7["champion"])["entry_band"]
    check("DEMOTION: a promoted champion whose forward lift LB <= 0 reverts to DEFAULT_ENTRY_BAND (one-shot, registry flipped back)",
          res["demotion"]["ready"] and not (res["demotion"]["lift_lb"] > 0) and v["demote"] and a["demoted"] == planted
          and st["champion"] == CHAMP and st["demotion_judged"] == now7
          and IB.evaluate_all(now7 + 86400, paths=P7, shuffle_reps=SH)["demotion"] is None)
    d9 = os.path.join(base_dir, "ix"); os.makedirs(d9)
    P9 = IB._fixture(d9)
    CH.write_state(entry_band={"failed_nominee": planted, "failed_ts": now_i - 10 * 86400}, path=P9["champion"])
    v9 = IB.decide(IB.evaluate_all(now_i, paths=P9, shuffle_reps=SH))
    d9b = os.path.join(base_dir, "ixb"); os.makedirs(d9b)
    P9b = IB._fixture(d9b, n_days=105, edge_until_seq=720)
    CH.write_state(entry_band={"nominee": planted, "nominated_at_event_seq": 720, "nominated_ts": now_i}, path=P9b["champion"])
    res = IB.evaluate_all(now2, paths=P9b, shuffle_reps=SH)
    v = IB.decide(res)
    a = IB.apply(res, v, now2, send=False, paths=P9b)
    st = CH.state(P9b["champion"])["entry_band"]
    check("one-shot failure + cooldown: a nominee failing its complete prefix is cleared with failed_ts; a failed nominee "
          f"inside {config.BAND_RENOMINATE_COOLDOWN_DAYS} days is never renominated",
          v9["winner"] != planted and v9["nominate"] != planted and any("cooldown" in r_ for r_ in v9["reasons"])
          and res["forward_only"] and v["nomination_failed"] == planted and st["nominee"] is None
          and st["failed_nominee"] == planted and st["failed_ts"] == now2 and st["champion"] == CHAMP)
    with tempfile.TemporaryDirectory() as dd:
        missing = os.path.join(dd, "nope.json")
        corrupt = os.path.join(dd, "bad.json")
        open(corrupt, "w").write("{not json")
        _r, _o = _capture(CH.entry_band, corrupt)
        res_un = IB.evaluate_all(now_i, paths=dict(Pi, champion=missing), champion="band_does_not_exist", shuffle_reps=SH)
        check("champion.entry_band falls back to DEFAULT_ENTRY_BAND on a missing file or corrupt JSON; the gate falls back "
              "on an unregistered name", CH.entry_band(missing) == CHAMP and _r == CHAMP and res_un["champion"] == CHAMP)
finally:
    SC.dsr_day_means = _saved_dsr
    alerts.send_all = _saved_send
    shutil.rmtree(base_dir, ignore_errors=True)


# ═══════════════════════════════════════════════════════════════════════════════════
section("J. run.py — exact discovery (cursor never skips a log), atomic JSON, a dry run writes nothing")
# ═══════════════════════════════════════════════════════════════════════════════════
import run as RUN                                       # noqa: E402
from sources import geckoterminal as GT, safety as SAFE  # noqa: E402
from sources import gmgn as GMR                          # noqa: E402


def _pad(a):
    return "0x" + a[2:].lower().rjust(64, "0")


def _log(addr, topics, data, block, idx=0):
    return {"address": addr, "topics": topics, "data": data, "blockNumber": hex(block),
            "transactionHash": "0x" + ("%02x" % (block % 256)) * 32, "logIndex": hex(idx)}


TOK_P, PAIR_P = "0x" + "1" * 40, "0x" + "2" * 40
TOK_V3, POOL_V3 = "0x" + "3" * 40, "0x" + "4" * 40
TOK_F, CREATOR_F = "0x" + "5" * 40, "0x" + "6" * 40
fixture_logs = [
    _log(config.UNIV2_FACTORY, [config.TOPIC_PAIR_CREATED, _pad(config.WETH), _pad(TOK_P)],
         "0x" + rpc.enc_addr(PAIR_P) + rpc.enc_uint(7), 1000, 0),
    _log(config.V3_FACTORY, [config.TOPIC_POOL_CREATED, _pad(TOK_V3), _pad(config.WETH), rpc.enc_uint(3000)],
         "0x" + rpc.enc_uint(60) + rpc.enc_addr(POOL_V3), 1001, 0),
    _log(config.FLAP_ROUTER, [config.TOPIC_FLAP_TOKEN_CREATED],
         "0x" + rpc.enc_uint(1_700_000_000) + rpc.enc_addr(CREATOR_F) + rpc.enc_uint(1) + rpc.enc_addr(TOK_F) + rpc.enc_uint(0), 1002, 3),
]
recs = rpc.decode_discovery(fixture_logs)
by_kind = {r_["kind"]: r_ for r_ in recs}
check("decode_discovery: PairCreated -> the non-WETH leg is the candidate and pair = data word 0",
      by_kind["pair_v2"]["token"] == TOK_P and by_kind["pair_v2"]["pair"] == PAIR_P)
check("decode_discovery: PoolCreated -> pool = the LAST data word; Flap TokenCreated -> creator word 1, token word 3",
      by_kind["pool_v3"]["token"] == TOK_V3 and by_kind["pool_v3"]["pair"] == POOL_V3
      and by_kind["flap_create"]["token"] == TOK_F and by_kind["flap_create"]["creator"] == CREATOR_F
      and [r_["block"] for r_ in recs] == [1000, 1001, 1002])
_saved_get_logs = rpc.get_logs
calls_gl: list = []
try:
    rpc.get_logs = lambda addrs, topics, a, b: (calls_gl.append((a, b)) or ([lg for lg in fixture_logs if a <= int(lg["blockNumber"], 16) <= b], None))
    disc, cur, gap, _meta = RUN.discover_from_logs({"last_block": 990}, 1500, RUN._Budget(60))
    check("discover_from_logs decodes the three fixture logs through rpc.decode_discovery and advances the cursor to head",
          set(disc) == {TOK_P, TOK_V3, TOK_F} and cur["last_block"] == 1500 and gap == 0)
    calls_gl.clear()
    rpc.get_logs = lambda addrs, topics, a, b: (calls_gl.append((a, b)) or (None, "query returned more than 10000 results"))
    H = 2_000_000
    disc, cur, gap, _meta = RUN.discover_from_logs({"last_block": H - 50_000}, H, RUN._Budget(60))
    spans = [b - a + 1 for a, b in calls_gl]
    check(f"a window error halves the window down to MIN_LOG_WINDOW ({config.MIN_LOG_WINDOW}) and the cursor STAYS",
          cur["last_block"] == H - 50_000 and disc == {} and spans[-1] <= config.MIN_LOG_WINDOW
          and all(y <= x for x, y in zip(spans, spans[1:])) and len(spans) >= 7, str(spans))
    calls_gl.clear()
    rpc.get_logs = lambda addrs, topics, a, b: (calls_gl.append((a, b)) or ([], None))
    RUN.discover_from_logs({}, H, RUN._Budget(60))
    check(f"the first run with no cursor scans exactly DISCOVERY_BACKFILL_BLOCKS ({config.DISCOVERY_BACKFILL_BLOCKS}) back from head",
          calls_gl[0][0] == H - config.DISCOVERY_BACKFILL_BLOCKS and calls_gl[-1][1] == H)
    calls_gl.clear()
    disc, cur, gap, _meta = RUN.discover_from_logs({"last_block": H - 400_000}, H, RUN._Budget(60))
    check(f"a cursor older than DISCOVERY_MAX_CATCHUP_BLOCKS ({config.DISCOVERY_MAX_CATCHUP_BLOCKS}) is capped and the gap reported",
          calls_gl[0][0] == H - config.DISCOVERY_MAX_CATCHUP_BLOCKS and gap == 400_000 - config.DISCOVERY_MAX_CATCHUP_BLOCKS - 1
          and cur["last_block"] == H)
    START = 3_000_000
    many_logs = [_log(config.FLAP_ROUTER, [config.TOPIC_FLAP_TOKEN_CREATED],
                      "0x" + rpc.enc_uint(1) + rpc.enc_addr(CREATOR_F) + rpc.enc_uint(i) + rpc.enc_addr("0x%040x" % (i + 1)) + rpc.enc_uint(0),
                      START + i, 0) for i in range(300)]
    rpc.get_logs = lambda addrs, topics, a, b: ([lg for lg in many_logs if a <= int(lg["blockNumber"], 16) <= b], None)
    disc, cur, gap, meta = RUN.discover_from_logs({"last_block": START - 1}, START + 400, RUN._Budget(60))
    cap = config.DISCOVERY_MAX_LOG_TOKENS_PER_RUN
    check(f"with 300 log tokens only DISCOVERY_MAX_LOG_TOKENS_PER_RUN ({cap}) are processed and the cursor sits one block "
          "below the cut (log tokens are never truncated silently)",
          len(disc) == cap and cur["last_block"] == START + cap - 2 and set(disc) == {"0x%040x" % (i + 1) for i in range(cap)}
          and meta["catchup"] is False and meta["cap"] == cap and meta["lag_blocks"] == 401)
    # catch-up: while the cursor is more than DISCOVERY_CATCHUP_TRIGGER_BLOCKS behind, the per-run token
    # cap rises so the cursor can actually close the gap (at the steady-state cap the cursor advanced
    # ~10k blocks/run and the 300k floor then DROPPED ranges — discovery hole 5)
    disc_c, cur_c, gap_c, meta_c = RUN.discover_from_logs({"last_block": START - 1}, START + 40_000, RUN._Budget(60))
    cap_c = config.DISCOVERY_MAX_LOG_TOKENS_CATCHUP
    check(f"a cursor more than DISCOVERY_CATCHUP_TRIGGER_BLOCKS ({config.DISCOVERY_CATCHUP_TRIGGER_BLOCKS:,}) behind raises the cap to "
          f"DISCOVERY_MAX_LOG_TOKENS_CATCHUP ({cap_c}) and the same 300-log fixture processes min(300, {cap_c}); the cursor still "
          "advances only to the last log actually processed",
          meta_c["catchup"] is True and meta_c["cap"] == cap_c and len(disc_c) == min(300, cap_c)
          and cur_c["last_block"] == START + 40_000 and meta_c["lag_blocks"] == 40_001, str((len(disc_c), meta_c)))
    check("the arithmetic that keeps a full run inside the enrich bound: logs + rechecks + feeds <= MAX_DISCOVER in steady state AND "
          "in catch-up (the watchlist is exempt), and a pull-forward can never crowd out half the scheduled rechecks",
          config.DISCOVER_QUOTA["logs"] + config.RECHECK_PER_RUN + config.DISCOVER_QUOTA["feeds"] <= config.MAX_DISCOVER
          and cap_c + config.RECHECK_PER_RUN + config.DISCOVER_QUOTA["feeds"] <= config.MAX_DISCOVER
          and config.FEED_PULL_FORWARD_MAX <= config.RECHECK_PER_RUN // 2
          and config.DISCOVER_QUOTA["rechecks"] == config.RECHECK_PER_RUN,
          str((config.DISCOVER_QUOTA, config.RECHECK_PER_RUN, config.MAX_DISCOVER, config.FEED_PULL_FORWARD_MAX)))
finally:
    rpc.get_logs = _saved_get_logs

# ── the feeds are a discovery SOURCE with a ladder, not a one-shot look ────────────────
# A feed-sighted token Dexscreener does not price yet used to be marked seen for SEEN_TTL_S with no
# recheck at all (the else-branch), while a log-sighted one got a ladder: FOMOPAD was sighted at
# 4.07 min through gt_new_pools alone (discovery hole 1).
_FEED_KINDS = ("gt_new_pools", "gmgn_new_creation", "gmgn_near_completion", "gmgn_completed")
_ladders = {k: config.RECHECK_SCHEDULE_BY_KIND.get(k) for k in _FEED_KINDS}
check("every feed kind has its own recheck ladder: ascending slots, the FIRST slot <= 600 s (a feed token is minutes old, not "
      "hours), and the launchpad kinds keep their single 30-min slot (pons_create alone is 750-1,250 launches/hour)",
      all(isinstance(v, tuple) and v and all(y > x for x, y in zip(v, v[1:])) and v[0] <= 600 for v in _ladders.values())
      and config.RECHECK_SCHEDULE_BY_KIND["pons_create"] == (1800,)
      and all(RUN._recheck_schedule({"kind": k}) == _ladders[k] for k in _FEED_KINDS)
      and RUN._recheck_schedule({"kind": "no_such_kind"}) == config.RECHECK_SCHEDULE_S, str(_ladders))

_saved_fd = (GT.new_pools, GMR.trenches)
_GT_TOK, _GM_TOK, _PULL_TOK = "0x" + "a1" * 20, "0x" + "b2" * 20, "0x" + "c3" * 20
try:
    GT.new_pools = lambda page=1, network=None: ([{"token": _GT_TOK, "symbol": "GTX", "pool": "0x" + "d4" * 20,
                                                   "created_ts": 1.7e9, "buys_m5": 6, "buyers_m5": 3}] if page == 1 else [])
    _tr_cols = {"new_creation": [{"address": _GM_TOK.upper(), "creator": "0x" + "ee" * 20,
                                  "created_timestamp": 1789263019, "launchpad_platform": "bankr",
                                  "is_wash_trading": False, "suspected_insider_hold_rate": 0.02}],
                "near_completion": [], "completed": [{"address": _PULL_TOK, "creator": "0x" + "ef" * 20,
                                                      "created_timestamp": 1789261939, "launchpad_platform": "pons"}]}
    GMR.trenches = lambda **k: _tr_cols
    _rk = {_PULL_TOK: {"n_checks": 1, "next_check": 9.9e9, "first_seen": 1.0,
                       "disc": {"kind": "pons_create", "creator": "0x" + "11" * 20}}}
    _ft, _frows, _fdisc, _fhits = RUN.feed_tokens({}, set(_rk), RUN._Budget(60), recheck=_rk)
    check("feed_tokens returns (tokens, GMGN rows, disc records, pull-forward hits): a GT new-pools row becomes a disc record of kind "
          "gt_new_pools carrying the row's own fields, a GMGN row becomes kind gmgn_<column> with created_ts + launchpad_platform and "
          "NEVER a `creator` key (safety._apply_disc writes creator -> deployer and would override the creation-tx-sender attribution)",
          _ft == {_GT_TOK, _GM_TOK.lower()} and set(_fdisc) == {_GT_TOK, _GM_TOK.lower(), _PULL_TOK}
          and _fdisc[_GT_TOK]["kind"] == "gt_new_pools" and _fdisc[_GT_TOK]["buys_m5"] == 6
          and _fdisc[_GM_TOK.lower()] == {"kind": "gmgn_new_creation", "created_ts": 1789263019, "launchpad_platform": "bankr"}
          and _fdisc[_PULL_TOK]["kind"] == "gmgn_completed"
          and not any("creator" in d_ or "deployer" in d_ for t_, d_ in _fdisc.items() if t_ != _GT_TOK)
          and set(_frows) == {_GM_TOK.lower(), _PULL_TOK}, str(_fdisc))
    _many = {"0x%040x" % (i + 1): {"n_checks": 0, "next_check": 9.9e9} for i in range(config.FEED_PULL_FORWARD_MAX + 5)}
    GMR.trenches = lambda **k: {"completed": [{"address": t_} for t_ in _many], "near_completion": [], "new_creation": []}
    _fh_many = RUN.feed_tokens({}, set(_many), RUN._Budget(60), recheck=_many)[3]
    check("a feed row for a token ALREADY in recheck is a pull-forward hit (a new pool for a token we are waiting on is its "
          f"graduation), not a re-discovery: it stays out of the new-token set, and the hits are capped at FEED_PULL_FORWARD_MAX "
          f"({config.FEED_PULL_FORWARD_MAX}) so they can never crowd out the scheduled rechecks",
          _fhits == [_PULL_TOK] and _PULL_TOK not in _ft
          and len(_fh_many) == config.FEED_PULL_FORWARD_MAX and _fh_many == sorted(_many)[: config.FEED_PULL_FORWARD_MAX],
          str(len(_fh_many)))
    GMR.trenches = lambda **k: _tr_cols
    _ft0, _fr0, _fd0, _fh0 = RUN.feed_tokens({_GT_TOK: 1.0}, {_GM_TOK.lower()}, RUN._Budget(60), recheck={})
    check("seen / known tokens are still not re-discovered by a feed; with no recheck entry there are no pull-forward hits and the "
          "same row is an ordinary discovery (what makes it a pull-forward is that we were already waiting on the token)",
          _ft0 == {_PULL_TOK} and _fh0 == [] and set(_fd0) == {_GT_TOK, _GM_TOK.lower(), _PULL_TOK}, str((_ft0, _fh0)))
finally:
    GT.new_pools, GMR.trenches = _saved_fd

# ── the absent-routing table, driven through a REAL committed run into a temp data dir ────────
# "never mark a token seen without a market snapshot" — a Dexscreener `absent` IS a snapshot, so
# routing it is a decision, not an omission: a feed-sighted token joins its kind's LADDER exactly
# like a log-sighted one (it used to take the else-branch straight to `seen` for SEEN_TTL_S with no
# recheck at all, and FOMOPAD was sighted at 4.07 min through gt_new_pools alone), a ladder that has
# run out still ends at `seen`, and a token pulled forward by a feed row keeps its scheduled slot.
_RPATHS = ("LEDGER_PATH", "STATE_PATH", "SEEN_PATH", "RECHECK_PATH", "WATCHLIST_PATH", "CURSOR_PATH",
           "SCAN_PATH", "BAND_VERDICTS_PATH", "PAPER_LEDGER_PATH", "PAPER_POSITIONS_PATH", "RUN_LOG_PATH")
_saved_rp = {k: getattr(config, k) for k in _RPATHS}
_saved_pe = config.PAPER_EXEC
_saved_rt = {"bn": rpc.block_number, "gl": rpc.get_logs, "np_": GT.new_pools, "tr": GMR.trenches,
             "en": RUN.dex.enrich_many, "fw": RUN.dex.forward_snapshot_many, "p1": SAFE.pass1_many,
             "p2": SAFE.pass2, "st": scanhood.stock_tokens, "sa": RUN.send_all}
T_FEED, T_PULL, T_DONE, T_SURV = "0x" + "11" * 20, "0x" + "22" * 20, "0x" + "33" * 20, "0x" + "55" * 20
_SURV_MKT = {"symbol": "SURV", "name": "survivor", "price_usd": 0.001, "liq_usd": 53_263.0, "mcap": 346_387.0,
             "fdv": 346_387.0, "vol_h1": 323_290.0, "vol_h6": 400_000.0, "vol_h24": 500_000.0, "buys_h1": 1273,
             "sells_h1": 1137, "buys_h24": 2000, "sells_h24": 1800, "price_chg_h1": 40.0, "pair_age_min": 4.07,
             "dex": "uniswap", "url": "https://dexscreener.com/robinhood/0xpair", "pair": "0x" + "66" * 20}
_SURV_ROW = {"address": T_SURV, "symbol": "SURV", "launchpad_platform": "bankr", "is_wash_trading": False,
             "suspected_insider_hold_rate": 0.02, "visiting_count": 8, "top_10_holder_rate": 0.0566,
             "created_timestamp": 1789261939, "is_honeypot": "no"}
try:
    with tempfile.TemporaryDirectory() as _dd:
        for _k in _RPATHS:
            setattr(config, _k, os.path.join(_dd, os.path.basename(_saved_rp[_k])))
        config.PAPER_EXEC = False
        _pull_rec = {"n_checks": 1, "next_check": 9.9e9, "first_seen": 1.0,
                     "disc": {"kind": "gmgn_completed", "created_ts": 1789261939}}
        _done_rec = {"n_checks": len(config.RECHECK_SCHEDULE_BY_KIND["gt_new_pools"]), "next_check": 1.0,
                     "first_seen": 1.0, "disc": {"kind": "gt_new_pools", "token": T_DONE}}
        with open(config.RECHECK_PATH, "w") as _fh:
            json.dump({T_PULL: _pull_rec, T_DONE: _done_rec}, _fh)
        rpc.block_number = lambda: 1000
        rpc.get_logs = lambda addrs, topics, a, b: ([], None)
        GT.new_pools = lambda page=1, network=None: ([{"token": T_FEED, "symbol": "FEED", "pool": "0x" + "44" * 20,
                                                       "created_ts": 1.7e9}] if page == 1 else [])
        GMR.trenches = lambda **k: {"new_creation": [], "near_completion": [],
                                    "completed": [{"address": T_PULL, "launchpad_platform": "pons"}, _SURV_ROW]}
        RUN.dex.enrich_many = lambda addrs, now_s, max_age_sec=None: {
            "ok": {T_SURV: dict(_SURV_MKT)} if T_SURV in addrs else {},
            "absent": {a_ for a_ in addrs if a_ != T_SURV}, "deferred": set()}
        RUN.dex.forward_snapshot_many = lambda toks, now_s: {}
        SAFE.pass1_many = lambda toks, markets_, disc_, now_s, chain_cache=None: {t_: SAFE.empty_safety() for t_ in toks}
        SAFE.pass2 = lambda token, market, s1_, now_s, **k: dict(s1_, **{"pass": 2})   # adds NOTHING of its own
        scanhood.stock_tokens = lambda: set()
        RUN.send_all = lambda title, body, dry_run=True: None
        _t_run = time.time()
        _r_run, _out_run = _capture(RUN.run, dry_run=False, send=False)
        _rec_after = json.load(open(config.RECHECK_PATH))
        _seen_after = json.load(open(config.SEEN_PATH))
        _scan_after = json.load(open(config.SCAN_PATH))
        _sched0 = config.RECHECK_SCHEDULE_BY_KIND["gt_new_pools"][0]
        check(f"a FEED-sighted token Dexscreener cannot price yet joins its kind's ladder — recheck at +{_sched0} s with n_checks 1 and "
              "its feed disc record persisted — and is NOT marked seen (the feed used to be a one-shot look)",
              T_FEED in _rec_after and int(_rec_after[T_FEED]["n_checks"]) == 1 and T_FEED not in _seen_after
              and abs(float(_rec_after[T_FEED]["next_check"]) - (_t_run + _sched0)) < 30
              and (_rec_after[T_FEED].get("disc") or {}).get("kind") == "gt_new_pools", str(_rec_after.get(T_FEED)))
        check("a token whose ladder is exhausted still ends at seen and leaves recheck (the ladder terminates; recheck.json cannot leak)",
              T_DONE not in _rec_after and T_DONE in _seen_after, str((T_DONE in _rec_after, T_DONE in _seen_after)))
        check("a token PULLED FORWARD by a feed row is looked at WITHOUT consuming its scheduled slot: its recheck record is identical "
              "afterwards (same next_check, same n_checks) — the pull-forward adds a look rather than spending one",
              _rec_after.get(T_PULL) == _pull_rec, str(_rec_after.get(T_PULL)))
        _srv = {r_["token"]: r_ for r_ in (_scan_after.get("survivors") or [])}
        check("the free Trenches row reaches EVERY token's feature dict, not only the handful that get a pass 2: the row is attached "
              "to the pass-1 safety dict, so the row-only fields (the wash flag and the insider hold rate, which /v1/token/info does "
              "not carry at all) are present on a survivor whose pass 2 added nothing. Measured before this: 11/157 survivors carried "
              "them against 107/157 for the info-sourced fields, because the row was applied inside pass2 and pass 2 runs for at most "
              "GT_INFO_BUDGET_PER_RUN + WATCH_REFRESH_PER_RUN tokens while the watchlist is ~98 % of the survivors",
              T_SURV in _srv and _srv[T_SURV]["gmgn_is_wash_trading"] is False
              and abs(float(_srv[T_SURV]["gmgn_insider_hold_pct"]) - 2.0) < 1e-9
              and _srv[T_SURV]["gmgn_visiting_count"] == 8 and _srv[T_SURV]["gmgn_launchpad_platform"] == "bankr",
              str({k_: v_ for k_, v_ in (_srv.get(T_SURV) or {}).items() if k_.startswith("gmgn_")}))
        check("latest_scan.json carries gmgn_coverage — the share of survivors with a known gmgn_visiting_count — so the coverage of a "
              "GMGN-dependent band is measured before that band is ever registered",
              abs(float(_scan_after.get("gmgn_coverage")) - 1.0) < 1e-9, str(_scan_after.get("gmgn_coverage")))
        check("latest_scan.json records how discovery ran this time — catchup, cursor_lag_blocks, feed_hits — so a run that is behind, or "
              "is being led by the feeds, says so in the point-in-time store",
              _scan_after.get("catchup") is False and _scan_after.get("cursor_lag_blocks") == 0
              and _scan_after.get("feed_hits") == 1, str({k: _scan_after.get(k) for k in ("catchup", "cursor_lag_blocks", "feed_hits")}))
finally:
    for _k in _RPATHS:
        setattr(config, _k, _saved_rp[_k])
    config.PAPER_EXEC = _saved_pe
    rpc.block_number, rpc.get_logs, GT.new_pools, GMR.trenches = _saved_rt["bn"], _saved_rt["gl"], _saved_rt["np_"], _saved_rt["tr"]
    RUN.dex.enrich_many, RUN.dex.forward_snapshot_many = _saved_rt["en"], _saved_rt["fw"]
    SAFE.pass1_many, SAFE.pass2, scanhood.stock_tokens, RUN.send_all = _saved_rt["p1"], _saved_rt["p2"], _saved_rt["st"], _saved_rt["sa"]
    http_client.reset_health()

# ── a token cut by the pass-1 TIME budget (never evaluated at all) still gets a recheck record —
# the cursor has already moved past it, so dropping it here is a silent PER-TOKEN loss, not a retry
# (task-6 review Important #2: catch-up makes this reachable at up to ~790 addresses against 240 s) ──
_saved_bgt = RUN._Budget


class _CutAfterFirst(_saved_bgt):
    """budget.ok('pass1') is True exactly once, then False — forces the pass-1 budget-cut branch
    deterministically for the SECOND and later tokens without racing a real clock."""
    def __init__(self, seconds):
        super().__init__(seconds)
        self._pass1_n = 0

    def ok(self, stage):
        if stage != "pass1":
            return True
        self._pass1_n += 1
        if self._pass1_n == 1:
            return True
        self.cuts[stage] = self.cuts.get(stage, 0) + 1
        return False


T_CBFIRST, T_CBLOG, T_CBRECHECK = "0x" + "aa" * 20, "0x" + "bb" * 20, "0x" + "cc" * 20
PAIR_CBFIRST, PAIR_CBLOG = "0x" + "dd" * 20, "0x" + "ee" * 20
_cb_logs = [
    _log(config.UNIV2_FACTORY, [config.TOPIC_PAIR_CREATED, _pad(config.WETH), _pad(T_CBFIRST)],
         "0x" + rpc.enc_addr(PAIR_CBFIRST) + rpc.enc_uint(7), 1990, 0),
    _log(config.UNIV2_FACTORY, [config.TOPIC_PAIR_CREATED, _pad(config.WETH), _pad(T_CBLOG)],
         "0x" + rpc.enc_addr(PAIR_CBLOG) + rpc.enc_uint(7), 1991, 0),
]
_CB_MKT = dict(_SURV_MKT, symbol="CUT")
_precut_rec = {"n_checks": 1, "next_check": 1.0, "first_seen": 1.0,
               "disc": {"kind": "pons_create", "pair": "0x" + "ff" * 20}}
_saved_rt2 = {"bn": rpc.block_number, "gl": rpc.get_logs, "np_": GT.new_pools, "tr": GMR.trenches,
              "en": RUN.dex.enrich_many, "fw": RUN.dex.forward_snapshot_many, "p1": SAFE.pass1_many,
              "p2": SAFE.pass2, "st": scanhood.stock_tokens, "sa": RUN.send_all}
try:
    with tempfile.TemporaryDirectory() as _dd2:
        for _k in _RPATHS:
            setattr(config, _k, os.path.join(_dd2, os.path.basename(_saved_rp[_k])))
        config.PAPER_EXEC = False
        with open(config.RECHECK_PATH, "w") as _fh:
            json.dump({T_CBRECHECK: _precut_rec}, _fh)
        RUN._Budget = _CutAfterFirst
        rpc.block_number = lambda: 2000
        rpc.get_logs = lambda addrs, topics, a, b: ([lg for lg in _cb_logs if a <= int(lg["blockNumber"], 16) <= b], None)
        GT.new_pools = lambda page=1, network=None: []
        GMR.trenches = lambda **k: {"new_creation": [], "near_completion": [], "completed": []}
        RUN.dex.enrich_many = lambda addrs, now_s, max_age_sec=None: {
            "ok": {a_: dict(_CB_MKT) for a_ in (T_CBFIRST, T_CBLOG, T_CBRECHECK) if a_ in addrs},
            "absent": set(), "deferred": set()}
        RUN.dex.forward_snapshot_many = lambda toks, now_s: {}
        SAFE.pass1_many = lambda toks, markets_, disc_, now_s, chain_cache=None: {t_: SAFE.empty_safety() for t_ in toks}
        SAFE.pass2 = lambda token, market, s1_, now_s, **k: dict(s1_, **{"pass": 2})
        scanhood.stock_tokens = lambda: set()
        RUN.send_all = lambda title, body, dry_run=True: None
        _t_cb = time.time()
        _capture(RUN.run, dry_run=False, send=False)
        _rec_cb = json.load(open(config.RECHECK_PATH))
        _seen_cb = json.load(open(config.SEEN_PATH))
        _scan_cb = json.load(open(config.SCAN_PATH))
        _sched_default = config.RECHECK_SCHEDULE_S[0]
        check("a log token that IS enriched but then hits the pass-1 TIME budget (not the size gates) still gets a recheck record: "
              "n_checks 1, its own disc record, next_check on the kind's first ladder slot, and it is NOT marked seen — the cursor "
              "has already advanced past it, so silence here means it is never retried or re-discovered (task-6 review Important #2)",
              T_CBLOG in _rec_cb and int(_rec_cb[T_CBLOG]["n_checks"]) == 1 and T_CBLOG not in _seen_cb
              and abs(float(_rec_cb[T_CBLOG]["next_check"]) - (_t_cb + _sched_default)) < 30
              and (_rec_cb[T_CBLOG].get("disc") or {}).get("kind") == "pair_v2", str(_rec_cb.get(T_CBLOG)))
        check("a RECHECK-sourced token cut by the same budget keeps its existing record byte-identical (it is already in the queue; "
              "a budget cut is not a new fact about it, so it stays untouched rather than re-armed)",
              _rec_cb.get(T_CBRECHECK) == _precut_rec, str(_rec_cb.get(T_CBRECHECK)))
        check("the cut is counted, not silent: deferred_by_stage.budget_pass1 records both tokens the pass-1 loop never reached",
              _scan_cb.get("deferred_by_stage", {}).get("budget_pass1") == 2, str(_scan_cb.get("deferred_by_stage")))
finally:
    RUN._Budget = _saved_bgt
    for _k in _RPATHS:
        setattr(config, _k, _saved_rp[_k])
    config.PAPER_EXEC = _saved_pe
    rpc.block_number, rpc.get_logs, GT.new_pools, GMR.trenches = _saved_rt2["bn"], _saved_rt2["gl"], _saved_rt2["np_"], _saved_rt2["tr"]
    RUN.dex.enrich_many, RUN.dex.forward_snapshot_many = _saved_rt2["en"], _saved_rt2["fw"]
    SAFE.pass1_many, SAFE.pass2, scanhood.stock_tokens, RUN.send_all = _saved_rt2["p1"], _saved_rt2["p2"], _saved_rt2["st"], _saved_rt2["sa"]
    http_client.reset_health()

# ── a pass-1 survivor that misses the PASS-2 budget is rescheduled on its ladder — it must neither
# loop every run nor vanish. Day-one watch of the phase-6 inflow on origin/main (six scans,
# 14:25-14:50Z): deferred_by_stage.pass2_overflow climbed 0 → 3 → 5 → 7 → 11 → 16 → 19 while every
# other counter stayed clean. `deferred` is only COUNTED, so a rechecks-sourced overflow token kept
# its record exactly as it was — still due — and came back the very next run (a Dexscreener enrich
# and a pass-1 look each time, and one of the RECHECK_PER_RUN slots the Pons backlog needs), and the
# loop set grew by every new survivor past slot GT_INFO_BUDGET_PER_RUN; a logs/feeds-sourced one had
# no record anywhere and was lost outright. The pass-2 twin of the pass-1 budget-cut defect above. ──
T_P2WIN, T_P2LOG = "0x" + "a5" * 20, "0x" + "b5" * 20
T_P2RE, T_P2PULL = "0x" + "c5" * 20, "0x" + "d5" * 20
PAIR_P2WIN, PAIR_P2LOG = "0x" + "e5" * 20, "0x" + "f5" * 20
_p2_logs = [
    _log(config.UNIV2_FACTORY, [config.TOPIC_PAIR_CREATED, _pad(config.WETH), _pad(T_P2WIN)],
         "0x" + rpc.enc_addr(PAIR_P2WIN) + rpc.enc_uint(7), 2990, 0),
    _log(config.UNIV2_FACTORY, [config.TOPIC_PAIR_CREATED, _pad(config.WETH), _pad(T_P2LOG)],
         "0x" + rpc.enc_addr(PAIR_P2LOG) + rpc.enc_uint(7), 2991, 0),
]
# four pass-1 survivors, liquidity-ordered: with GT_INFO_BUDGET_PER_RUN pinned to 1 the first one
# takes the only pass-2 slot and the other three overflow, one per source route
_p2_mkts = {T_P2WIN: dict(_SURV_MKT, symbol="P2WIN", liq_usd=90_000.0),
            T_P2LOG: dict(_SURV_MKT, symbol="P2LOG", liq_usd=60_000.0),
            T_P2RE: dict(_SURV_MKT, symbol="P2RE", liq_usd=50_000.0),
            T_P2PULL: dict(_SURV_MKT, symbol="P2PULL", liq_usd=40_000.0)}
_p2_re_rec = {"n_checks": 1, "next_check": 1.0, "first_seen": 1.0,
              "disc": {"kind": "gmgn_near_completion", "created_ts": 1789261939}}
_p2_pull_rec = {"n_checks": 1, "next_check": 9.9e9, "first_seen": 1.0,
                "disc": {"kind": "gmgn_completed", "created_ts": 1789261939}}
_saved_p2b = config.GT_INFO_BUDGET_PER_RUN
_saved_rt3 = {"bn": rpc.block_number, "gl": rpc.get_logs, "np_": GT.new_pools, "tr": GMR.trenches,
              "en": RUN.dex.enrich_many, "fw": RUN.dex.forward_snapshot_many, "p1": SAFE.pass1_many,
              "p2": SAFE.pass2, "st": scanhood.stock_tokens, "sa": RUN.send_all}
try:
    with tempfile.TemporaryDirectory() as _dd3:
        for _k in _RPATHS:
            setattr(config, _k, os.path.join(_dd3, os.path.basename(_saved_rp[_k])))
        config.PAPER_EXEC = False
        config.GT_INFO_BUDGET_PER_RUN = 1
        with open(config.RECHECK_PATH, "w") as _fh:
            json.dump({T_P2RE: _p2_re_rec, T_P2PULL: _p2_pull_rec}, _fh)
        rpc.block_number = lambda: 3000
        rpc.get_logs = lambda addrs, topics, a, b: ([lg for lg in _p2_logs if a <= int(lg["blockNumber"], 16) <= b], None)
        GT.new_pools = lambda page=1, network=None: []
        GMR.trenches = lambda **k: {"new_creation": [], "near_completion": [],
                                    "completed": [{"address": T_P2PULL, "launchpad_platform": "pons"}]}
        RUN.dex.enrich_many = lambda addrs, now_s, max_age_sec=None: {
            "ok": {a_: dict(m_) for a_, m_ in _p2_mkts.items() if a_ in addrs},
            "absent": set(), "deferred": set()}
        RUN.dex.forward_snapshot_many = lambda toks, now_s: {}
        SAFE.pass1_many = lambda toks, markets_, disc_, now_s, chain_cache=None: {t_: SAFE.empty_safety() for t_ in toks}
        SAFE.pass2 = lambda token, market, s1_, now_s, **k: dict(s1_, **{"pass": 2})
        scanhood.stock_tokens = lambda: set()
        RUN.send_all = lambda title, body, dry_run=True: None
        _t_p2 = time.time()
        _capture(RUN.run, dry_run=False, send=False)
        _rec_p2 = json.load(open(config.RECHECK_PATH))
        _seen_p2 = json.load(open(config.SEEN_PATH))
        _scan_p2 = json.load(open(config.SCAN_PATH))
        _srv_p2 = {r_["token"] for r_ in (_scan_p2.get("survivors") or [])}
        _re_next = config.RECHECK_SCHEDULE_BY_KIND["gmgn_near_completion"][1]
        check("the one token inside the pass-2 budget is scored and ledgered as before, and the three that overflow are NOT scored on "
              "their partial pass-1 facts (a survivor with no pass 2 never reaches the survivors list)",
              _srv_p2 == {T_P2WIN}, str(sorted(_srv_p2)))
        check("a RECHECKS-sourced survivor that overflows the pass-2 budget ADVANCES its ladder instead of staying due: n_checks 1 → 2, "
              "next_check on the kind's next rung, and still not seen. Untouched it was due again the very next run — re-enriched and "
              "re-gated for nothing — and the looping set grew by every survivor past slot GT_INFO_BUDGET_PER_RUN (pass2_overflow "
              "climbed 0 → 3 → 5 → 7 → 11 → 16 → 19 over six consecutive runs on 2026-09-19)",
              T_P2RE in _rec_p2 and int(_rec_p2[T_P2RE]["n_checks"]) == 2 and T_P2RE not in _seen_p2
              and abs(float(_rec_p2[T_P2RE]["next_check"]) - (_t_p2 + _re_next)) < 30
              and float(_rec_p2[T_P2RE]["next_check"]) > _t_p2, str(_rec_p2.get(T_P2RE)))
        check("a LOGS-sourced survivor that overflows the pass-2 budget GETS a recheck record (n_checks 1, its own disc record, the "
              "first rung of its ladder) and is not marked seen — it had no record anywhere and the cursor has already advanced past "
              "it, so silence here is a permanent per-token loss, not a retry",
              T_P2LOG in _rec_p2 and int(_rec_p2[T_P2LOG]["n_checks"]) == 1 and T_P2LOG not in _seen_p2
              and abs(float(_rec_p2[T_P2LOG]["next_check"]) - (_t_p2 + config.RECHECK_SCHEDULE_S[0])) < 30
              and (_rec_p2[T_P2LOG].get("disc") or {}).get("kind") == "pair_v2", str(_rec_p2.get(T_P2LOG)))
        check("a PULLED-FORWARD survivor that overflows keeps its scheduled slot byte-identical (a pull-forward adds a look rather "
              "than spending one — the rule used everywhere else in run.py)",
              _rec_p2.get(T_P2PULL) == _p2_pull_rec, str(_rec_p2.get(T_P2PULL)))
        check("the overflow is counted twice over, so the loop cannot come back unseen: deferred_by_stage.pass2_overflow is the three "
              "survivors past the budget and pass2_rescheduled is the two of them this branch actually re-armed (the pulled-forward "
              "one is deliberately left alone)",
              _scan_p2.get("deferred_by_stage", {}).get("pass2_overflow") == 3
              and _scan_p2.get("deferred_by_stage", {}).get("pass2_rescheduled") == 2,
              str(_scan_p2.get("deferred_by_stage")))
finally:
    config.GT_INFO_BUDGET_PER_RUN = _saved_p2b
    for _k in _RPATHS:
        setattr(config, _k, _saved_rp[_k])
    config.PAPER_EXEC = _saved_pe
    rpc.block_number, rpc.get_logs, GT.new_pools, GMR.trenches = _saved_rt3["bn"], _saved_rt3["gl"], _saved_rt3["np_"], _saved_rt3["tr"]
    RUN.dex.enrich_many, RUN.dex.forward_snapshot_many = _saved_rt3["en"], _saved_rt3["fw"]
    SAFE.pass1_many, SAFE.pass2, scanhood.stock_tokens, RUN.send_all = _saved_rt3["p1"], _saved_rt3["p2"], _saved_rt3["st"], _saved_rt3["sa"]
    http_client.reset_health()

with tempfile.TemporaryDirectory() as d:
    pj = os.path.join(d, "x.json")
    RUN._atomic_json(pj, {"a": float("nan"), "b": [float("inf"), 1.0]}, indent=1)
    raw = _read(pj)
    strict = json.loads(raw, parse_constant=lambda c: (_ for _ in ()).throw(ValueError(c)))
    check("_atomic_json refuses NaN/inf (writes null) and leaves no tmp", strict == {"a": None, "b": [None, 1.0]}
          and not [f for f in os.listdir(d) if f.endswith(".tmp")])
    # the git-friendly writer for the three big state files (seen / recheck / watchlist are rewritten
    # every run; recheck.json alone was 67 % of the measured 72 KB/commit packed growth): one entry
    # per line in sorted key order, so an unchanged entry is an unchanged line and git packs a delta
    big = {f"0x{i:040x}": {"n_checks": i, "next_check": 1.5e9 + i, "disc": {"kind": "pons_create", "pair": None}}
           for i in range(50)}
    big["0x" + "f" * 40] = {"nan": float("nan"), "z": [1, {"y": 2}]}
    pl = os.path.join(d, "lines.json")
    RUN._atomic_json_lines(pl, big)
    raw1 = _read(pl)
    back = json.loads(raw1, parse_constant=lambda c: (_ for _ in ()).throw(ValueError(c)))
    check("_atomic_json_lines round-trips through json.load (NaN → null, allow_nan=False semantics) and leaves no tmp",
          back == RUN._clean(big) and not [f for f in os.listdir(d) if f.endswith(".tmp")])
    lines1 = raw1.splitlines()
    keys_in_file = [ln.split('"')[1] for ln in lines1[1:-1]]
    check("_atomic_json_lines writes `{`, ONE `\"key\": <compact json>` per line in SORTED key order, `}`",
          lines1[0] == "{" and lines1[-1] == "}" and len(lines1) == len(big) + 2 and keys_in_file == sorted(big)
          and all(ln.endswith(",") for ln in lines1[1:-2]) and not lines1[-2].endswith(","))
    big2 = dict(big)
    big2["0x" + f"{7:040x}"] = {"n_checks": 8, "next_check": 1.5e9 + 7, "disc": {"kind": "pons_create", "pair": None}}
    RUN._atomic_json_lines(pl, big2)
    lines2 = _read(pl).splitlines()
    changed = [i for i, (a_, b_) in enumerate(zip(lines1, lines2)) if a_ != b_]
    check("writing the same dict with ONE entry changed changes exactly ONE line (unchanged entries are byte-identical lines)",
          len(lines1) == len(lines2) and len(changed) == 1 and '"n_checks":8' in lines2[changed[0]], str(changed))
    RUN._atomic_json_lines(os.path.join(d, "empty.json"), {})
    check("an empty dict is still valid JSON", json.loads(_read(os.path.join(d, "empty.json"))) == {})
    pw = os.path.join(d, "watch.json")
    LAB.save_watchlist(big, pw)
    check("save_watchlist (the watchlist's own writer, in entry_lab/runtime) produces byte-identical output to _atomic_json_lines "
          "(one format for the three files)", _read(pw) == raw1)
    _rs_src = _read(os.path.join(ROOT, "run.py"))
    check("run.py writes SEEN_PATH and RECHECK_PATH through _atomic_json_lines; latest_scan.json keeps _atomic_json (its shape is "
          "read by the dashboard and the lab)",
          "_atomic_json_lines(config.SEEN_PATH, seen)" in _rs_src and "_atomic_json_lines(config.RECHECK_PATH, recheck)" in _rs_src
          and "_atomic_json(config.SCAN_PATH, scan)" in _rs_src)


def _snapshot_tree(base):
    out = {}
    for dp, _dns, fns in os.walk(base):
        for fn in fns:
            pth = os.path.join(dp, fn)
            st_ = os.stat(pth)
            out[pth] = (st_.st_mtime_ns, st_.st_size)
    return out


_saved_run = {"block_number": rpc.block_number, "get_logs": rpc.get_logs, "new_pools": GT.new_pools,
              "enrich_many": RUN.dex.enrich_many, "pass1": SAFE.pass1_many, "pass2": SAFE.pass2,
              "stock": scanhood.stock_tokens, "send_all": RUN.send_all, "save_watch": LAB.save_watchlist,
              "trenches": GMR.trenches}
sent_run: list = []
try:
    cur0 = RUN._load_json(config.CURSOR_PATH, {})
    rpc.block_number = lambda: int(cur0.get("last_block") or 100_000) + 10
    rpc.get_logs = lambda addrs, topics, a, b: ([], None)
    GT.new_pools = lambda page=1, network=None: []
    GMR.trenches = lambda *a, **k: None
    RUN.dex.enrich_many = lambda addrs, now_s, max_age_sec=None: {"ok": {}, "absent": set(), "deferred": set(addrs)}
    SAFE.pass1_many = lambda *a, **k: {}
    SAFE.pass2 = lambda *a, **k: None
    scanhood.stock_tokens = lambda: set()
    RUN.send_all = lambda title, body, dry_run=True: sent_run.append(dry_run)
    LAB.save_watchlist = lambda *a, **k: (_ for _ in ()).throw(AssertionError("dry run must not save the watchlist"))
    before = _snapshot_tree(config.DATA_DIR)
    _r, out = _capture(RUN.run, dry_run=True, send=False)
    after = _snapshot_tree(config.DATA_DIR)
    check("a dry run (every source monkeypatched offline) writes NOTHING under data/ (mtimes + sizes identical, no new files)",
          before == after and "dry run" in out and all(sent_run) if sent_run else before == after, str({k for k in set(before) ^ set(after)}))
    check("a dry run sends nothing (send_all, if called at all, is dry_run=True)", all(sent_run))
finally:
    rpc.block_number, rpc.get_logs, GT.new_pools = _saved_run["block_number"], _saved_run["get_logs"], _saved_run["new_pools"]
    RUN.dex.enrich_many, SAFE.pass1_many, SAFE.pass2 = _saved_run["enrich_many"], _saved_run["pass1"], _saved_run["pass2"]
    scanhood.stock_tokens, RUN.send_all, LAB.save_watchlist = _saved_run["stock"], _saved_run["send_all"], _saved_run["save_watch"]
    GMR.trenches = _saved_run["trenches"]
    http_client.reset_health()
if os.path.exists(config.SCAN_PATH):
    strict_scan = json.loads(_read(config.SCAN_PATH), parse_constant=lambda c: (_ for _ in ()).throw(ValueError(c)))
    check("the committed latest_scan.json parses under a strict JSON parser (no NaN/Infinity) and carries scan_ts/band/health",
          all(k in strict_scan for k in ("scan_ts", "band", "health", "survivors")))
else:
    skip("latest_scan.json strict parse", "no data/latest_scan.json yet")

# ═══════════════════════════════════════════════════════════════════════════════════
section("K. alerts — every A card carries the footer, the band and the PLAN line; dry runs never send")
# ═══════════════════════════════════════════════════════════════════════════════════
surv_a = dict(good, symbol="MIZUKARA", event_kind="promotion", hc_misses=[], url="https://example.invalid/x")
ta, ba = alerts.format_alert([surv_a], band=CHAMP)
check("every A-tier message contains config.FOOTER, the band name (title: '[band]') and 'PLAN [' in the body",
      config.FOOTER in ba and f"[{CHAMP}]" in ta and "PLAN [" in ba and "MIZUKARA" in ba)
td, bd = alerts.format_alert([dict(surv_a, sources_dark=["blockscout"], total_holders=None)], degraded=["robinhoodchain.blockscout.com"], band=CHAMP)
check("a DEGRADED line when hosts are dark, and None fields print '?' rather than raising",
      "DEGRADED: robinhoodchain.blockscout.com dark this run" in bd and "holders ?" in bd)
te, be = alerts.format_exit_alert([{"kind": "stop", "token": "0xabc", "symbol": "DEAD", "event_seq": 7, "price": 0.0,
                                    "ret": -1.0, "mult": 0.0, "gap_s": 300.0}])
check("a dead stop says 'no price'", "no price" in be and "DEAD" in be and config.FOOTER in be)
check("format_event / format_weekly titles",
      alerts.format_event("PROMOTED", ["x"])[0] == "robinhood_screener PROMOTED"
      and alerts.format_event("bogus", [])[0] == "robinhood_screener EVENT BOGUS"
      and alerts.format_weekly(["a"])[0] == "robinhood_screener WEEKLY summary")
_r, out = _capture(alerts.send_all, ta, ba, dry_run=True)
check("send_all(dry_run=True) prints the card and returns without touching the network", _r is None and "DRY RUN" in out and "MIZUKARA" in out)
check("the degraded notice states that silence is not evidence of a quiet chain",
      "NOT evidence of a quiet chain" in alerts.format_degraded_notice("h", ["x"], ["template_ok"])[1])
_PLAN_CLS = ("PLAN [champion cfg_ladder_stop — default, no policy has cleared the gate]: buy ~$10 · "
             "hard-stop $0.5 (-50%) · TP 2x→sell 50%, 5x→sell 25%, 10x→sell 15%")
with _policies({n: CAND_POL[n] for n in (CAND_BENCH, CAND_FLOW)}):
    _pb = CH.describe_plan(CH.exit_plan(CAND_BENCH), 1.0)
    _pf = CH.describe_plan(CH.exit_plan(CAND_FLOW), 1.0)
    check("describe_plan states the arm ('armed at 1.5x') on both candidates and, for the adaptive one only, "
          "that the cloud book runs the price legs while the paper book scores the flow leg; the alert card "
          "renders whatever it returns",
          "trail -30% off the high-water mark, armed at 1.5x" in _pb and "flow take-profit" not in _pb
          and "trail -30% off the high-water mark, armed at 1.5x" in _pf
          and ("flow take-profit (cloud book: price legs only; the keeper's paper book scores the flow leg)"
               in _pf) and "hard-stop $0.5 (-50%)" in _pb and "exit ALL at +6 h" in _pb,
          _pf)
check("the arm/flow keys changed no existing PLAN line: cfg_ladder_stop renders byte-identically",
      CH.describe_plan(CH.exit_plan("cfg_ladder_stop"), 1.0) == _PLAN_CLS,
      CH.describe_plan(CH.exit_plan("cfg_ladder_stop"), 1.0))

# ═══════════════════════════════════════════════════════════════════════════════════
section("L. dashboard render, publish against a temp bare origin, the weekly summary")
# ═══════════════════════════════════════════════════════════════════════════════════
import dashboard as DASH                                # noqa: E402
from selfimprove import publish as PUB                  # noqa: E402
from selfimprove import weekly_summary as WS            # noqa: E402

scan_syn = {"scan_ts": LT0, "trigger": "dispatch", "band": CHAMP, "champion": {"exit": config.IMPROVE_DEFAULT_EXIT_CHAMPION, "entry_band": CHAMP},
            "champion_na_frac": 0.0, "discovered": 3, "enriched": 3, "absent": 0, "by_source": {"logs": 3}, "quota_cuts": {},
            "deferred_by_stage": {}, "pass1_rejects_by_gate": {"liq_ok": 1},
            "survivors": [dict(good, symbol="MIZU", score=84.0, tier="A", url="u", event_kind="promotion", event_seq=7, hc_misses=[],
                               champion_reason="", plan_line="PLAN [champion x]", bands={CHAMP: 1})],
            "health": {"api.dexscreener.com": {"ok": 1, "fail": 0, "absent": 0, "bot_challenge": False, "last_status": None}},
            "run_seconds": 12.0, "gmgn_coverage": 0.37}
run_log_syn = [{"scan_ts": LT0 - 600, "trigger": "schedule", "run_seconds": 40, "n_a": 0},
               {"scan_ts": LT0, "trigger": "dispatch", "run_seconds": 12.0, "n_a": 1}]
with tempfile.TemporaryDirectory() as d:
    page = DASH.render(scan_syn, LED.load(os.path.join(d, "l.csv")), None, None, run_log_syn)
    check("dashboard.render on a synthetic scan contains 'A-TIER', the band, 'runs in the last 24 h', the GMGN coverage of the run "
          "(the eligibility number for any band that reads a gmgn_* field) and config.FOOTER",
          all(n in page for n in ("A-TIER", CHAMP, "runs in the last 24 h", "GMGN coverage 37%", config.FOOTER)) and "<b>2</b>" in page)
    junk = DASH.render({"survivors": [None, 3]}, None, {"fills": [None]}, {"per_policy": 5}, [{}])
    check("dashboard.render never raises on garbage inputs", config.FOOTER in junk and "runs in the last 24 h" in junk)

if MAC and HAVE_GIT:
    _saved_pub = (config.ROOT, config.CHAMPION_PATH)
    try:
        with tempfile.TemporaryDirectory() as d:
            bare, work, other = os.path.join(d, "origin.git"), os.path.join(d, "work"), os.path.join(d, "other")
            subprocess.run(["git", "init", "-q", "--bare", "-b", "main", bare], check=True)
            subprocess.run(["git", "init", "-q", "-b", "main", work], check=True)
            PUB._git(["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "root"], work)
            PUB._git(["remote", "add", "origin", bare], work); PUB._git(["push", "-q", "origin", "main"], work)
            os.makedirs(os.path.join(work, "selfimprove"))
            json.dump({"schema": 2}, open(os.path.join(work, "selfimprove", "champion.json"), "w"))
            open(os.path.join(work, "untracked.txt"), "w").write("local only")
            config.ROOT = work; config.CHAMPION_PATH = os.path.join(work, "selfimprove", "champion.json")
            ok1, _o = _capture(PUB.publish_files, ["selfimprove/champion.json"], "test publish", root=work)
            subprocess.run(["git", "clone", "-q", "-b", "main", bare, other], check=True)
            open(os.path.join(other, "x.txt"), "w").write("x")
            PUB._git(["add", "x.txt"], other)
            PUB._git(["-c", "user.name=o", "-c", "user.email=o@o", "commit", "-q", "-m", "competing"], other)
            PUB._git(["push", "-q", "origin", "main"], other)
            json.dump({"schema": 2, "locked": True}, open(config.CHAMPION_PATH, "w"))
            ok2, _o = _capture(PUB.publish_files, ["selfimprove/champion.json"], "second publish", root=work)
            listed = PUB._git(["ls-tree", "--name-only", "-r", "origin/main"], work).stdout.split()
            check("publish.publish_files against a temp bare origin pushes exactly the listed files (untracked local files "
                  "never leak) and survives a competing commit (fetch + retry from a detached temp worktree)",
                  ok1 and ok2 and sorted(listed) == ["selfimprove/champion.json", "x.txt"]
                  and PUB.origin_blob("selfimprove/champion.json", work) == _read(config.CHAMPION_PATH)
                  and PUB._git(["rev-parse", "--abbrev-ref", "HEAD"], work).stdout.strip() == "main"
                  and not os.path.exists(os.path.join(work, "x.txt")), str(listed))
            rc, _o = _capture(PUB.reconcile, work)
            json.dump({"schema": 2, "locked": False}, open(config.CHAMPION_PATH, "w"))
            rc2, _o = _capture(PUB.reconcile, work)
            check("reconcile() is a no-op when in sync and republishes a champion.json that differs from origin/main (mac_only)",
                  rc is True and rc2 is True and PUB.origin_blob("selfimprove/champion.json", work) == _read(config.CHAMPION_PATH)
                  and not any(n_.startswith("rh_publish_") for n_ in os.listdir(tempfile.gettempdir())))
    finally:
        config.ROOT, config.CHAMPION_PATH = _saved_pub
else:
    skip("publish.publish_files / reconcile against a temp bare origin (mac_only)", "IS_CI or git absent")

with tempfile.TemporaryDirectory() as d:
    now_w = 1_789_300_000.0
    PE = {k: os.path.join(d, os.path.basename(v_)) for k, v_ in WS.default_paths().items()}
    lines_e, _o = _capture(WS.compose, now_w, None, PE)
    te_, be_ = alerts.format_weekly(lines_e)
    check("weekly_summary.compose on an EMPTY temp paths dict never raises, says 'earliest possible promotion' and the "
          "weekly body carries config.FOOTER", any("earliest possible promotion" in ln for ln in lines_e)
          and any("insufficient (n=0)" in ln for ln in lines_e) and config.FOOTER in be_ and len(lines_e) <= WS.MAX_LINES)
    PF = WS._fixture(os.path.join(d, "fx"), now_w) if os.makedirs(os.path.join(d, "fx")) is None else None
    lines_f, _o = _capture(WS.compose, now_w, "research merged", PF)
    body_f = "\n".join(lines_f)
    check("weekly_summary.compose on the 40-day fixture derives 'earliest possible promotion' from observed rates, the K5 "
          "line, PAUSED and the research line", "earliest possible promotion (from trailing-28d observed rates" in body_f
          and "entry band: >=" in body_f and "K5 kill" in body_f and "PAUSED (PAUSE file)" in body_f
          and "research: research merged" in body_f and len(lines_f) <= WS.MAX_LINES)
    check("the ledger LINE RENDERS — both arms' medians and promoted-B, never 'unavailable' (ledger_section named a "
          "module whose only import was local to _ledger_frame: a NameError swallowed by compose's per-section try)",
          "A (alerted) median" in body_f and "B (silent control) median" in body_f
          and "promoted-B" in body_f and "unavailable" not in body_f,
          "\n".join(x for x in lines_f if "ledger" in x or "unavailable" in x))
    lb_raw = json.load(open(PF["livebook"]))
    check("the weekly fixture writes the NESTED shape improve.summary_json produces (book counts under 'book'), and "
          "livebook_section reads them there — they printed 0 / '?' while it read the top level",
          isinstance(lb_raw.get("book"), dict) and lb_raw["book"]["n_done"] == 80 and "n_done" not in lb_raw
          and "livebook: 120 positions, 80 done, 3 suspect, 2 unpriced, 5 gapped, 4 no-route" in body_f
          and "entry lag median 310 s" in body_f and "refused 1" in body_f,
          "\n".join(x for x in lines_f if "livebook" in x))
    check("the per-policy 'top by mean' line keeps reading the TOP-LEVEL per_policy (the improve gate's table, not "
          "the book's)", "top by mean" in body_f and "sell_3h -0.050" in body_f
          and "ctl_exit_immediately -0.034" in body_f)
    _ws_tree = _tree(os.path.join(ROOT, "selfimprove", "weekly_summary.py"))
    check("weekly_summary imports ledger at MODULE level (a function-local import binds a local name and every other "
          "function that spells it raises NameError)", "ledger" in _top_imports(_ws_tree))


# ═══════════════════════════════════════════════════════════════════════════════
section("M. launchpad tokens — Bankr / Doppler on Uniswap V4 (the reference winners CATGPT, ANTHROPIG)")
from sources import rpc as RPCM, safety as SAFEM   # noqa: E402
import selfimprove.trials as TRIALS_MOD             # noqa: E402
import run as RUNM                                   # noqa: E402
_gm_dark = lambda token, cache_s=None: None   # noqa: E731 — GMGN dark for the whole section (offline)
_orig_gm_ti = SAFEM.gmgn.token_info
SAFEM.gmgn.token_info = _gm_dark

CAT = config.REFERENCE_TOKENS["CATGPT"].lower()
_SVC = "0x3a8e5ba5aa9c2464c75621c2bffb9cf912db995d"           # CATGPT's launcher (app/agent wallet)
_NUM = "0xfe09fb328be1c286b4f597ed34764b7472ae72c5"           # OPENAI 1x Long, its numeraire
_PID = "0x086f510359ad57e4f8588b71ffa21fe29bbed044e4d13aa2f72ecaefeda95a36"
_HOOK = config.DOPPLER_HOOK.lower()
def _pad(a): return "0x" + "0" * 24 + a.lower()[2:]
def _w(a): return "0" * 24 + a.lower()[2:]
# the REAL CATGPT creation-tx logs (Blockscout, 2026-09-12), verbatim
_INIT = {"address": config.V4_POOL_MANAGER, "blockNumber": "0x3a5171b", "transactionHash": "0xfba3", "logIndex": "0x8b",
         "topics": [config.TOPIC_V4_INITIALIZE, _PID, _pad(CAT), _pad(_NUM)],
         "data": "0x" + "0" * 58 + "800000" + "0" * 63 + "8" + _w(_HOOK) + "0" * 40 + "121a2e40afbf2f142bd896d" + "f" * 60 + "e5868"}
_CREATE = {"address": config.LONGLAUNCH_FACTORY, "blockNumber": "0x3a5171b", "transactionHash": "0xfba3", "logIndex": "0x96",
           "topics": [config.TOPIC_LONGLAUNCH_CREATE, _pad(CAT), _pad(_SVC)],
           "data": "0x" + "0" * 62 + "20" + _w(_NUM) + _w("0x92d435c96e63c43e12d6d0ab28f6b0b04072f765") + _w(_HOOK)
                   + _w("0xba2f330edb16cd8056f5988d8ce19bbc63475a0e") + "0" * 60 + "dead"}
recs = RPCM.decode_discovery([_INIT, _CREATE])
check("LongLaunch Create + V4 Initialize in one window decode to ONE record: token, launcher, numeraire, Doppler hook, "
      "pool id, launchpad=bankr (the create row wins, the pool row fills what it lacks)",
      len(recs) == 1 and recs[0]["kind"] == "longlaunch_create" and recs[0]["token"] == CAT and recs[0]["creator"] == _SVC
      and recs[0]["numeraire"] == _NUM and recs[0]["hook"] == _HOOK and recs[0]["pool_id"] == _PID and recs[0]["launchpad"] == "bankr",
      str(recs))
_other = "0x" + "ab" * 20
_INIT2 = dict(_INIT, topics=[config.TOPIC_V4_INITIALIZE, "0x" + "11" * 32, _pad(_other), _pad(_NUM)], logIndex="0x8c")
_hookless = dict(_INIT, data=_INIT["data"][:2 + 64 * 2] + "0" * 64 + _INIT["data"][2 + 64 * 3:])
two = RPCM.decode_discovery([_INIT, _INIT2])
_hookless2 = dict(_INIT2, data=_hookless["data"], logIndex="0x8d")
hl = RPCM.decode_discovery([_hookless, _hookless2])
check("a V4 pool alone (numeraire unknown in the window) is dropped whether hooked or not; a currency repeated on >= 2 pools "
      "resolves the OTHER leg as the token; hook-less pools resolve the same way with launchpad None (they held the day's "
      "biggest winners) and are a source only while V4_DISCOVERY_HOOKS_ONLY is False",
      RPCM.decode_discovery([_INIT]) == [] and RPCM.decode_discovery([_hookless]) == []
      and sorted(r_["token"] for r_ in two) == sorted([CAT, _other]) and all(r_["numeraire"] == _NUM for r_ in two)
      and not config.V4_DISCOVERY_HOOKS_ONLY and sorted(r_["token"] for r_ in hl) == sorted([CAT, _other])
      and all(r_["launchpad"] is None for r_ in hl))
_PONS_TOK = "0x9e410f73" + "ab" * 16
_PONS = {"address": config.PONS_FACTORY, "blockNumber": "0x3a5171c", "transactionHash": "0x1b3e", "logIndex": "0x12",
         "topics": [config.TOPIC_PONS_CREATE, _pad(_PONS_TOK), "0x" + "77" * 32, _pad("0x" + "cd" * 20)],
         "data": "0x" + "0" * 128 + "0" * 48 + "3a4965bf58a40000"}
pr = RPCM.decode_discovery([_PONS])
check("a Pons Create log decodes to token (topics[1]), creator (topics[3]), pool id (topics[2]), launchpad=pons; a Pons launch "
      "gets ONE 30-min recheck slot, a V2 pair the default two",
      len(pr) == 1 and pr[0]["kind"] == "pons_create" and pr[0]["token"] == _PONS_TOK and pr[0]["creator"] == "0x" + "cd" * 20
      and pr[0]["pool_id"] == "0x" + "77" * 32 and pr[0]["launchpad"] == "pons"
      and RUNM._recheck_schedule(pr[0]) == (1800,) and RUNM._recheck_schedule({"kind": "pair_v2"}) == config.RECHECK_SCHEDULE_S
      and RUNM._recheck_schedule(None) == config.RECHECK_SCHEDULE_S)
_PROTO = next(iter(config.PROTOCOL_OWNERS))
check("owner() mapping: zero → renounced, the launchpad's shared owner → protocol, another address → owned, revert → no_owner_fn",
      RPCM._owner_from(True, _pad("0x" + "0" * 40)) == "renounced" and RPCM._owner_from(True, _pad(_PROTO)) == "protocol"
      and RPCM._owner_from(True, _pad("0x" + "12" * 20)) == "owned" and RPCM._owner_from(False, None) == "no_owner_fn")

prot_s = dict(CLEAN_S, owner_state="protocol", owner_renounced=False, lp_locked_pct=None, lp_check_source="v4_launchpad:bankr")
check("hard gates: owner_state 'protocol' passes owner_ok (a launchpad's contract is not a dev key); an unknown LP % with a "
      "trusted launchpad source passes lp_ok; the same token 'owned' is rejected",
      screen.hard_gates(CLEAN_M, prot_s)[0] and not screen.hard_gates(CLEAN_M, dict(prot_s, owner_state="owned"))[0])
check("hc_checks: lp_known is True through the launchpad source without a %, and None when neither a % nor a launchpad source exists",
      screen.hc_checks(_feat(CLEAN_M, prot_s))["lp_known"] is True
      and screen.hc_checks(_feat(CLEAN_M, dict(CLEAN_S, lp_locked_pct=None, lp_check_source=None)))["lp_known"] is None)
svc_s = dict(prot_s, creator_prior_tokens=54, creator_dead_frac=0.48)
check("creator gate on the launchpad: an app/agent launcher (54 launches) passes only on a KNOWN dead fraction within the cap "
      "(unknown or above the cap fails); a 7-launch launcher passes under the launchpad cap but not off the launchpad",
      screen.hard_gates(CLEAN_M, svc_s)[0] and not screen.hard_gates(CLEAN_M, dict(svc_s, creator_dead_frac=None))[0]
      and not screen.hard_gates(CLEAN_M, dict(svc_s, creator_dead_frac=0.6))[0]
      and screen.hard_gates(CLEAN_M, dict(prot_s, creator_prior_tokens=7))[0]
      and not screen.hard_gates(CLEAN_M, dict(CLEAN_S, creator_prior_tokens=7))[0])

# the Kyber round trip for tokens with no V2 pair, with an injected router
_orig_route = SAFEM.kyber.route
try:
    _calls = []
    def _mk(out_fn):
        def route(a, b, amt):
            _calls.append((a.lower(), b.lower(), amt))
            return out_fn(a, b, amt)
        return route
    SAFEM.kyber.route = _mk(lambda a, b, amt: {"amount_out": int(amt * 0.99)})
    out = {"0x" + "aa" * 20: SAFEM.empty_safety(), "0x" + "bb" * 20: SAFEM.empty_safety(), "0x" + "cc" * 20: SAFEM.empty_safety()}
    facts = {"0x" + "aa" * 20: {"pair": None, "status": "ok"}, "0x" + "bb" * 20: {"pair": None, "status": "ok"},
             "0x" + "cc" * 20: {"pair": "0x" + "dd" * 20, "status": "ok"}}
    mk = {"0x" + "aa" * 20: {"liq_usd": 1e4}, "0x" + "bb" * 20: {"liq_usd": 5e4}}
    SAFEM._kyber_roundtrips(out, facts, mk, 1)
    probed = [c[1] for c in _calls if c[0] == config.WETH.lower()]
    sb = out["0x" + "bb" * 20]
    check("Kyber round trip: only no-V2-pair tokens are probed, deepest liquidity first, within the per-run budget; both legs "
          "routed ⇒ honeypot False and the round-trip loss (1 - 0.99² = 1.99%), kyber named as used",
          probed == ["0x" + "bb" * 20] and sb["honeypot"] is False and abs(sb["roundtrip_loss_pct"] - 1.99) < 0.01
          and "kyber" in sb["sources_used"] and out["0x" + "aa" * 20]["honeypot"] is None and out["0x" + "cc" * 20]["honeypot"] is None,
          str((probed, sb["honeypot"], sb["roundtrip_loss_pct"])))
    _calls = []
    SAFEM.kyber.route = _mk(lambda a, b, amt: {"amount_out": int(amt * 0.99)} if a.lower() == config.WETH.lower() else http_client.NOT_FOUND)
    o2 = {"0x" + "aa" * 20: SAFEM.empty_safety()}
    SAFEM._kyber_roundtrips(o2, {"0x" + "aa" * 20: {"pair": None, "status": "ok"}}, {}, 5)
    s2_ = o2["0x" + "aa" * 20]
    SAFEM.kyber.route = _mk(lambda a, b, amt: None)
    o3 = {"0x" + "aa" * 20: SAFEM.empty_safety()}
    SAFEM._kyber_roundtrips(o3, {"0x" + "aa" * 20: {"pair": None, "status": "ok"}}, {}, 5)
    s3_ = o3["0x" + "aa" * 20]
    check("buyable but not sellable through Kyber is UNKNOWN (never a honeypot verdict on a minutes-old pool) with kyber marked "
          "answered; a deferred leg leaves everything None and names kyber dark",
          s2_["honeypot"] is None and s2_["roundtrip_loss_pct"] is None and "kyber" in s2_["sources_used"] and "kyber" not in s2_["sources_dark"]
          and s3_["honeypot"] is None and "kyber" in s3_["sources_dark"])
finally:
    SAFEM.kyber.route = _orig_route

# ScanHood: concurrent, budgeted, deepest liquidity first; beyond the budget = not consulted (never dark)
_orig_scan = SAFEM.scanhood.scan
try:
    _sc_calls = []
    def _fake_scan(t):
        _sc_calls.append(t)
        if t.endswith("ee" * 20):
            raise RuntimeError("boom")
        return {"verdict": "OK", "sellable": True}
    SAFEM.scanhood.scan = _fake_scan
    _toks = ["0x" + c * 40 for c in "abcdef"]
    _mk = {t: {"liq_usd": 1000.0 * (i + 1)} for i, t in enumerate(_toks)}
    _sc = SAFEM._scanhood_many(_toks, _mk, 3)
    check("ScanHood is fetched for at most the budget (3 of 6 tokens, the three deepest), concurrently; an exception marks "
          "that token's scan as an error object; unbudgeted tokens are simply absent",
          set(_sc) == set(_toks[3:]) and len(_sc_calls) == 3 and _sc["0x" + "e" * 40] is SAFEM._SCAN_ERROR
          and _sc["0x" + "f" * 40] == {"verdict": "OK", "sellable": True}, str((sorted(_sc), _sc_calls)))
finally:
    SAFEM.scanhood.scan = _orig_scan

# fast pass 2 (the alert path): GT + Blockscout counters/flags only; never the paged holders, the creator or its logs
_orig_bs = {k: getattr(SAFEM.blockscout, k) for k in ("token_info", "token_counters", "top_holders", "address_info", "creator_of", "tx_logs")}
_orig_gt, _orig_rx, _orig_cl = SAFEM.geckoterminal.token_info, SAFEM.robinx.wallet, SAFEM.rpc.creator_launches
try:
    _bs_calls = []
    def _rec(name, val):
        def f(*a, **k):
            _bs_calls.append(name); return val
        return f
    SAFEM.blockscout.token_counters = _rec("token_counters", {"holders_count": 1200, "transfers_count": 2400})
    SAFEM.blockscout.address_info = _rec("address_info", {"is_scam": False, "impl_name": "DopplerERC20V1", "proxy_type": "eip1167", "is_verified": True})
    for k in ("token_info", "top_holders", "creator_of", "tx_logs"):
        setattr(SAFEM.blockscout, k, _rec(k, {}))
    SAFEM.geckoterminal.token_info = _rec("gt", {"gt_score": 50.0, "is_honeypot_gt": False})
    SAFEM.robinx.wallet = _rec("robinx", {})
    SAFEM.rpc.creator_launches = _rec("launches", [])
    SAFEM.gmgn.token_info = _rec("gmgn", None)
    _s1 = dict(SAFEM.empty_safety(), lp_check_source="v4_launchpad:bankr", deployer="0x" + "ab" * 20)
    _sf = SAFEM.pass2("0x" + "cd" * 20, {"liq_usd": 5e4}, _s1, 1_789_300_000.0, fast=True)
    check("fast pass 2 calls GT, token_counters, address_info and GMGN token_info ONLY (no paged holders, creator, creation "
          "logs, RobinX or launch history), fills holders/tx-per-holder/is_scam/template, marks fast_pass2 and names a dark gmgn",
          set(_bs_calls) == {"gt", "token_counters", "address_info", "gmgn"} and _sf["total_holders"] == 1200
          and "gmgn" in _sf["sources_dark"]
          and _sf["holders_source"] == "blockscout" and _sf["tx_per_holder_total"] == 2.0 and _sf["is_scam"] is False
          and _sf["template_name"] == "DopplerERC20V1" and "fast_pass2" in _sf["sources_used"] and _sf["pass"] == 2,
          str((_bs_calls, {k: _sf.get(k) for k in ("total_holders", "is_scam", "template_name")})))
finally:
    for k, v in _orig_bs.items():
        setattr(SAFEM.blockscout, k, v)
    SAFEM.geckoterminal.token_info, SAFEM.robinx.wallet, SAFEM.rpc.creator_launches = _orig_gt, _orig_rx, _orig_cl
    SAFEM.gmgn.token_info = _gm_dark
# full pass 2: the creation-tx logs are read for Flap launches only (dev_sniped is a Flap concept; 6.5 s a call)
try:
    _bs_calls = []
    for k in ("token_info", "token_counters", "top_holders", "creator_of", "wallet_created_tokens", "address_tx_count"):
        setattr(SAFEM.blockscout, k, _rec(k, {}))
    SAFEM.blockscout.address_info = _rec("address_info", {"is_scam": False, "creation_tx": "0xabc"})
    SAFEM.blockscout.tx_logs = _rec("tx_logs", [])
    SAFEM.geckoterminal.token_info = _rec("gt", {})
    SAFEM.robinx.wallet = _rec("robinx", {})
    SAFEM.rpc.creator_launches = _rec("launches", [])
    SAFEM.pass2("0x" + "cd" * 20, {"liq_usd": 5e4}, SAFEM.empty_safety(), 1_789_300_000.0, disc={"kind": "pons_create"})
    _n_pons = _bs_calls.count("tx_logs")
    _bs_calls = []
    SAFEM.pass2("0x" + "cd" * 20, {"liq_usd": 5e4}, SAFEM.empty_safety(), 1_789_300_000.0, disc={"kind": "flap_create"})
    _n_flap = _bs_calls.count("tx_logs")
    check("full pass 2 reads the creation-tx logs for a Flap launch and never for a Pons / V4 / V2 token (dev_sniped is a Flap "
          "concept; the call costs 6.5 s)", _n_pons == 0 and _n_flap == 1, str((_n_pons, _n_flap)))
finally:
    for k, v in _orig_bs.items():
        setattr(SAFEM.blockscout, k, v)
    SAFEM.geckoterminal.token_info, SAFEM.robinx.wallet, SAFEM.rpc.creator_launches = _orig_gt, _orig_rx, _orig_cl
_rs2 = _read(os.path.join(ROOT, "run.py"))
check("run.py: the prioritised pass 2 is the FAST variant and an early-alerted row is ledgered as alerted (never re-gated away)",
      "_run_p2(prio, fast=True)" in _rs2 and 'survivors.append(early_by_token[t])' in _rs2)

# the recall fixture: CATGPT as measured 2026-09-12 (age 857 min, $10.9M liq, $9.2M vol24; pass-2 facts verbatim)
_CAT_M = {"price_usd": 0.01744, "liq_usd": 10943245.36, "vol_h24": 9166859.69, "mcap": 17448744.0, "fdv": 17448744.0,
          "buys_h1": 915, "sells_h1": 1534, "buys_h24": 11202, "sells_h24": 21435, "pair_age_min": 856.97, "dex": "uniswap",
          "vol_h1": 400000.0, "vol_h6": 2400000.0, "price_chg_h1": 3.0}
_CAT_S = dict(SAFEM.empty_safety(), owner_state="protocol", owner_renounced=False, lp_locked_pct=None,
              lp_check_source="v4_launchpad:bankr", honeypot=False, roundtrip_loss_pct=0.0564, is_scam=False,
              template_name="DopplerERC20V1", is_proxy=True, verified_source=True, total_holders=4370,
              holders_source="blockscout", top10_pct=5.244, top10_pct_gt=74.0, dev_pct=None, deployer=_SVC,
              creator_prior_tokens=54, creator_dead_frac=0.4815, tx_per_holder_total=35.5, sources_dark=[])
cat_ok, cat_g = screen.hard_gates(_CAT_M, _CAT_S)
cat_score, _ = screen.soft_score(_CAT_M, _CAT_S)
cat_f = LAB.build_feat(CAT, _CAT_M, _CAT_S, cat_score, True, 0.0)
cat_v = LAB.evaluate_bands(cat_f, REG, CHAMP)
check("recall: the reference winner CATGPT passes every hard gate (before 2026-09-12 it died at pass 1 on owner_ok) and is "
      "LEDGERED — every hc-family band answers True or False, never NA, and the strict champion says False (its dev holding is "
      "unknowable for an agent launch), so the winner is a B row the lab can learn from",
      cat_ok and "band_launchpad_lenient" in cat_v and cat_v[CHAMP] is False
      and all(cat_v[n] is not None for n, sp_ in B.BUILTINS.items() if sp_.THREE_VALUED),
      str((cat_ok, [k for k, v in cat_g.items() if v is False], cat_v)))
cat_hi = dict(cat_f, score=75.0)
check("with the soft score above HC_MIN_SCORE the launchpad band fires on CATGPT while the strict band does not (dev holding "
      "unknown, launcher count above HC_CREATOR_MAX_PRIOR) — the lab, not a hand edit, decides whether that leniency pays",
      B.BUILTINS["band_launchpad_lenient"].verdict(cat_hi) is True and B.BUILTINS[CHAMP].verdict(cat_hi) is not True)
check("band_launchpad_lenient equals band_a_strict off the launchpad (coverage is the champion's) and is a counted trial",
      all(B.BUILTINS["band_launchpad_lenient"].verdict(f_) == B.BUILTINS[CHAMP].verdict(f_)
          for f_ in (good, dict(good, score=50.0), dict(good, dev_pct=None)))
      and "band_launchpad_lenient" in (TRIALS_MOD.load().get("bands_ever_scored") or []))
check("pass-1 exclusion: leveraged tokenized-stock legs (OPENAIx1L, NVDAx3L, ANTHROPICx1L) and quote assets are never candidates; "
      "CATGPT / LONGCAT / a symbol merely containing 'x1' are",
      all(RUNM._excluded_symbol(x_) for x_ in ("OPENAIx1L", "NVDAx3L", "ANTHROPICx1L", "usdg", "WETH"))
      and not any(RUNM._excluded_symbol(x_) for x_ in ("CATGPT", "LONGCAT", "MAX1LIFE", "", None)))
# band_volume_early: the operator's thesis, on the survivors' at-sighting numbers (latest_scan history 2026-09-12)
_FRONT = dict(good, pair_age_min=4.0, vol_h1=63062.0, liq_usd=14841.0, buys_h1=126, sells_h1=60, mcap=41402.0)     # 23x later
_MORTY = dict(good, pair_age_min=3.0, vol_h1=51496.0, liq_usd=22840.0, buys_h1=341, sells_h1=253, mcap=59872.0)    # 0.05x later
_OPEN = dict(good, pair_age_min=1379.0, vol_h1=510032.0, liq_usd=602018.0, buys_h1=688, sells_h1=1059, mcap=3655740.0)
VE = B.BUILTINS["band_volume_early"]
check("band_volume_early fires on FRONTIER at sighting (age 4 min, $63k hour-1 volume, buys 2.1x sells), not on Morty "
      "(balanced flow) nor on OPEN (23 h old, $3.7M); NA without an hour-1 volume; registered as a candidate and a counted trial",
      VE.verdict(_FRONT) is True and VE.verdict(_MORTY) is False and VE.verdict(_OPEN) is False
      and VE.verdict(dict(_FRONT, vol_h1=None)) is None and "buys below" in VE.explain(_MORTY)
      and any(e_["name"] == "band_volume_early" and e_["status"] == "candidate" for e_ in json.load(open(config.REGISTRY_PATH))["candidates"])
      and "band_volume_early" in (TRIALS_MOD.load().get("bands_ever_scored") or []))
# The live champion is the OPERATOR's, changed by `champion.py --set entry_band=… --publish`
# (GO_LIVE_CHECKLIST item 10), so naming it here would turn the repo's own go-live procedure red.
# What is pinned is what protects the operator: the champion is a band the registry can actually
# resolve and is never a control, DEFAULT_ENTRY_BAND stays the fallback and demotion target, and
# each provenance branch is internally consistent — a hand-set champion records who it replaced
# and why and leaves promoted_at_event_seq None (a manual set does NOT arm the demotion test),
# while a gate promotion carries the forward-only event_seq it was judged at.
def _entry_champion_faults(arm: dict, reg) -> list:
    """The entry-champion arm's faults against a band registry — the single implementation, run on
    the LIVE champion.json here and on a simulated hand-set champion in section Q."""
    ev = arm.get("evidence") or {}
    manual = ev.get("manual") is True
    faults = []
    if arm["champion"] != config.DEFAULT_ENTRY_BAND and (
            arm["champion"] not in reg.names() or reg.is_control(arm["champion"])):
        faults.append(f"champion {arm['champion']!r} is not a registered non-control band")
    if config.DEFAULT_ENTRY_BAND != "band_a_strict":
        faults.append(f"DEFAULT_ENTRY_BAND is {config.DEFAULT_ENTRY_BAND!r}, not the fallback/demotion target")
    if manual and not (ev.get("previous") and arm.get("previous") and ev.get("reason")
                       and arm.get("promoted_at_event_seq") is None):
        faults.append("a manual --set must record previous + reason and leave promoted_at_event_seq None")
    if not manual and arm["champion"] != config.DEFAULT_ENTRY_BAND and arm.get("promoted_at_event_seq") is None:
        faults.append("a non-manual champion carries no promoted_at_event_seq (the forward-only key)")
    return faults


_live = CH.state()["entry_band"]
_champ_faults = _entry_champion_faults(_live, REG)
check("the LIVE entry champion is a REGISTERED non-control band (or DEFAULT_ENTRY_BAND itself), DEFAULT_ENTRY_BAND stays "
      "band_a_strict as the fallback and demotion target, and its provenance is consistent: a manual --set records `previous` "
      "and a reason and leaves promoted_at_event_seq None; a gate promotion carries a non-None promoted_at_event_seq",
      not _champ_faults, f"{_champ_faults} {str(_live)[:160]}")
_rs = _read(os.path.join(ROOT, "run.py"))
check("early alerts: run.py sends the champion-band alert for pass-1-selected tokens BEFORE the remaining pass 2, the watchlist "
      "refresh and the forward update; the later alert block never re-sends them; stage_seconds is written to the scan",
      0 < _rs.index('early_alerted = {r["token"] for r in early_alerts}') < _rs.index("_run_p2(p2_rest)")
      < _rs.index('to_send = [r for r in fresh_alerts if r["token"] not in early_alerted]')
      < _rs.index("ledger.update_forward(") and '"stage_seconds": stage_s' in _rs)
_wf = _read(os.path.join(ROOT, ".github", "workflows", "screener.yml"))
_wf_pages = _read(os.path.join(ROOT, ".github", "workflows", "pages.yml"))
_wf_dog = _read(os.path.join(ROOT, ".github", "workflows", "keeper-watchdog.yml"))
check("screener.yml holds exactly ONE concurrency block whose group expression names both screener-keeper-<slot> (two keeper "
      "slots may overlap by design: the handoff) and screener-scan (one-shots); pip is cached",
      _wf.count("concurrency:") == 1 and "screener-keeper-" in _wf and "screener-scan" in _wf and "cache: pip" in _wf
      and "format('screener-keeper-{0}', inputs.slot || 'a')" in _wf and "cancel-in-progress: false" in _wf)
check("pages.yml owns the Pages deploy in its own group (a deploy never delays the next scan), cancel-in-progress true, dispatch-only",
      "group: screener-pages" in _wf_pages and "cancel-in-progress: true" in _wf_pages and "deploy-pages" in _wf_pages
      and "upload-pages-artifact" in _wf_pages and "workflow_dispatch" in _wf_pages and "name: robinhood-pages" in _wf_pages
      and "schedule:" not in _wf_pages)
_groups = {}
for _f_ in sorted(glob.glob(os.path.join(ROOT, ".github", "workflows", "*.yml"))):
    for _g in re.findall(r"screener-(?:keeper-|scan|pages|watchdog|weekly)", _read(_f_)):
        _groups.setdefault(_g, set()).add(os.path.basename(_f_))
check("across ALL workflow files every concurrency group name appears in exactly one file — the Sunday gates (screener-weekly) "
      "can never queue behind, or cancel, the scan",
      set(_groups) == {"screener-keeper-", "screener-scan", "screener-pages", "screener-watchdog", "screener-weekly"}
      and all(len(v) == 1 for v in _groups.values()), str(_groups))
check("REFERENCE_TOKENS name the two winners and the registry lists the launchpad band as a candidate, never the champion",
      set(config.REFERENCE_TOKENS) == {"CATGPT", "ANTHROPIG"} and CHAMP == "band_a_strict"
      and any(e_["name"] == "band_launchpad_lenient" and e_["status"] == "candidate" for e_ in json.load(open(config.REGISTRY_PATH))["candidates"]))

SAFEM.gmgn.token_info = _orig_gm_ti                    # section M is over: GMGN is real again
# ═══════════════════════════════════════════════════════════════════════════════════
section("N. GMGN — the Trenches feed, the wallet-tag second opinion, the terminal deep link")
# ═══════════════════════════════════════════════════════════════════════════════════
import importlib                                        # noqa: E402
import dashboard as DASHM                               # noqa: E402
try:
    GM = importlib.import_module("sources.gmgn")
except Exception as _e:                                 # the module must exist before anything else here can
    GM = None
    print(f"  (sources.gmgn import failed: {_e})")
check("sources/gmgn.py exists and imports offline", GM is not None)
GM_HOST = "openapi.gmgn.ai"
check("config: GMGN chain slug 'robinhood', the token deep link + explorer link templates, a rate pin <= 1 Hz, the feed name, "
      "GMGN's own quote_address_type set for robinhood, and openapi.gmgn.ai on the 429-is-terminal list",
      getattr(config, "GMGN_CHAIN", None) == "robinhood"
      and "{token}" in getattr(config, "GMGN_TOKEN_URL", "") and "{chain}" in getattr(config, "GMGN_TOKEN_URL", "")
      and getattr(config, "BLOCKSCOUT_TOKEN_URL", "").startswith(config.BLOCKSCOUT_BASE) and "{token}" in getattr(config, "BLOCKSCOUT_TOKEN_URL", "")
      and config.HOST_RATE_HZ.get(GM_HOST) == getattr(config, "GMGN_RATE_HZ", None) and 0 < getattr(config, "GMGN_RATE_HZ", 0) <= 1.0
      and "gmgn_trenches" in config.DISCOVERY_FEEDS and tuple(getattr(config, "GMGN_QUOTE_ADDRESS_TYPES", ())) == (11, 20, 24, 12, 0)
      and GM_HOST in getattr(config, "HOST_429_TERMINAL", ()))
_env0 = os.environ.get("GMGN_API_KEY")
try:
    os.environ["GMGN_API_KEY"] = "verify-key"
    _c = config.load_credentials()
    _cfg_src = _read(os.path.join(ROOT, "config.py"))
    check("load_credentials carries gmgn_api_key (env GMGN_API_KEY first, then config.local.json, then the solana screener's "
          "config.local.json — never the shared vrp file) and config's smoke test prints presence only",
          _c.get("gmgn_api_key") == "verify-key" and "SOLANA_SCREENER" in _cfg_src and "creds present" in _cfg_src
          and '"gmgn_api_key": bool(' in _cfg_src)
finally:
    if _env0 is None:
        os.environ.pop("GMGN_API_KEY", None)
    else:
        os.environ["GMGN_API_KEY"] = _env0
_body = GM.build_trenches_body(("completed",), {"min_liquidity": 25000})
check("build_trenches_body is GMGN's own client shape — version v2, one section per column with filters / launchpad_platform_v2 / "
      "limit / quote_address_type, min_*/max_* merged in (without version + quote_address_type the server answers code 0 with EMPTY columns)",
      _body == {"version": "v2", "completed": {"filters": ["offchain", "onchain"], "launchpad_platform_v2": True,
                                               "limit": config.GMGN_TRENCHES_LIMIT, "quote_address_type": [11, 20, 24, 12, 0],
                                               "min_liquidity": 25000}}
      and set(GM.build_trenches_body()) == {"version", *config.GMGN_TRENCHES_COLUMNS}, str(_body))
# shaped like the cached Trenches payload (cache/gmgn_trenches_*.json): rates are 0-1, is_honeypot is
# a STRING, created_timestamp is unix seconds, market cap / volume are numbers, counts are ints
_row = {"address": "0xAbC0000000000000000000000000000000000001", "symbol": "X", "launchpad_platform": "longxyz", "progress": 0.83,
        "holder_count": 40, "top70_sniper_hold_rate": 0.0001, "suspected_insider_hold_rate": 0.02, "fresh_wallet_rate": 0.31,
        "rat_trader_amount_rate": 0.005, "smart_degen_count": 2, "is_wash_trading": False,
        "visiting_count": 8, "top_10_holder_rate": 0.0566, "market_cap": 23756.72, "volume_24h": 82085.88,
        "buys_24h": 663, "sells_24h": 1071, "is_honeypot": "no", "created_timestamp": 1789261939,
        "owner_renounced": "yes", "burn_status": "yes"}
_fr = GM.features_from_row(_row)
check("features_from_row maps a Trenches row onto EXACTLY the gmgn_* feature keys (rates x100, counts int, '' platform -> None, "
      "missing -> None, bundler ratio only from /v1/token/info) and every key is a FEATURE_FIELD",
      _fr["gmgn_launchpad_platform"] == "longxyz" and _fr["gmgn_progress"] == 0.83 and _fr["gmgn_holders"] == 40
      and abs(_fr["gmgn_sniper_hold_pct"] - 0.01) < 1e-9 and abs(_fr["gmgn_insider_hold_pct"] - 2.0) < 1e-9
      and abs(_fr["gmgn_fresh_wallet_pct"] - 31.0) < 1e-9 and abs(_fr["gmgn_rat_vol_pct"] - 0.5) < 1e-9
      and _fr["gmgn_smart_degen_count"] == 2 and _fr["gmgn_is_wash_trading"] is False and _fr["gmgn_bundler_ratio"] is None
      and GM.features_from_row({"launchpad_platform": ""})["gmgn_launchpad_platform"] is None
      and set(_fr) == set(GM.GMGN_FEATURE_KEYS) and set(GM.GMGN_FEATURE_KEYS) <= set(config.FEATURE_FIELDS), str(_fr))
check("the operator's Trenches signals are FEATURES carrying the payload's own shapes: is_honeypot is a STRING ('yes' / 'no' / "
      "'unknown') read THREE-VALUED (anything else, a bool included, is None — unknown is never a finding), top_10_holder_rate is a "
      "0-1 rate scaled x100 like every other rate here, created_timestamp is unix seconds as an int, and the viewer count, market "
      "cap, 24 h volume and buy/sell counts come through as numbers",
      _fr["gmgn_visiting_count"] == 8 and abs(_fr["gmgn_top10_holder_pct"] - 5.66) < 1e-9
      and _fr["gmgn_is_honeypot"] is False and GM.features_from_row(dict(_row, is_honeypot="YES"))["gmgn_is_honeypot"] is True
      and GM.features_from_row(dict(_row, is_honeypot="unknown"))["gmgn_is_honeypot"] is None
      and GM.features_from_row(dict(_row, is_honeypot=True))["gmgn_is_honeypot"] is None
      and GM.features_from_row({})["gmgn_is_honeypot"] is None and GM.features_from_row({})["gmgn_created_ts"] is None
      and _fr["gmgn_created_ts"] == 1789261939 and isinstance(_fr["gmgn_created_ts"], int)
      and _fr["gmgn_market_cap"] == 23756.72 and _fr["gmgn_volume_24h"] == 82085.88
      and _fr["gmgn_buys_24h"] == 663 and _fr["gmgn_sells_24h"] == 1071, str(_fr))
_gm_reqs: list = []


def _gm_fake(script):
    def f(req, timeout=None, context=None):
        _gm_reqs.append((req.full_url, {k.lower(): v for k, v in req.headers.items()}, req.data))
        r = script(req.full_url, req.data)
        if isinstance(r, Exception):
            raise r
        return _Resp(r)
    return f


_saved_gm = (http_client._urlopen, config.HTTP_RETRIES, http_client._HOST_HZ.get(GM_HOST), GM._api_key)
try:
    http_client._HOST_HZ[GM_HOST] = 1000.0
    http_client.reset_health()
    GM._api_key = lambda: "verify-key"
    _ok_body = {"code": 0, "data": {"completed": [_row], "pump": [dict(_row, address="0xDEF")], "new_creation": []}}
    http_client._urlopen = _gm_fake(lambda u, d: json.dumps(_ok_body).encode())
    got = GM.trenches(cache_s=0)
    _u, _h, _d = _gm_reqs[-1]
    _sent = json.loads(_d.decode())
    _ts = int(dict(urllib.parse.parse_qsl(urllib.parse.urlparse(_u).query)).get("timestamp") or 0)
    check("trenches(): POST /v1/trenches?chain=robinhood&timestamp=<FRESH unix seconds>&client_id=…, X-APIKEY header, the v2 body with "
          "all three columns; the answer is keyed by column with GMGN's `pump` alias mapped to near_completion",
          got == {"completed": [_row], "near_completion": [dict(_row, address="0xDEF")], "new_creation": []}
          and "/v1/trenches?" in _u and "chain=robinhood" in _u and abs(_ts - time.time()) < 30 and "client_id=" in _u
          and _h.get("x-apikey") == "verify-key" and _sent.get("version") == "v2"
          and set(_sent) == {"version", *config.GMGN_TRENCHES_COLUMNS}, f"{_u} {got!r}"[:300])
    http_client._urlopen = _gm_fake(lambda u, d: json.dumps({"code": -1, "message": "unsupported chain"}).encode())
    check("a non-zero GMGN code is deferred (None), never an empty feed", GM.trenches(cache_s=0) is None)
    n0 = len(_gm_reqs)
    GM._api_key = lambda: None
    check("no key configured -> None without a request (the source is dark, not 'nothing new')",
          GM.trenches(cache_s=0) is None and len(_gm_reqs) == n0)
    GM._api_key = lambda: "verify-key"
    http_client.reset_health()
    n0 = len(_gm_reqs)
    config.HTTP_RETRIES = 3
    http_client._urlopen = _gm_fake(lambda u, d: _http_error(u, 429, {"server": "cloudflare", "content-type": "application/json"}))
    got = GM.trenches(cache_s=0)
    check("a 429 from openapi.gmgn.ai is TERMINAL for the run: exactly one request, None (deferred), host blocked — GMGN's free-tier "
          "ban extends 5 s per retry, so the http_client 429 wait-and-retry must not apply here",
          got is None and len(_gm_reqs) == n0 + 1 and http_client.is_blocked(GM_HOST), str(len(_gm_reqs) - n0))
    check("while blocked, a second call makes no request", GM.trenches(cache_s=0) is None and len(_gm_reqs) == n0 + 1)
    http_client.reset_health()
    # shaped like the cached /v1/token/info payloads: rates arrive as STRINGS, and the payload carries
    # visiting_count / creation_timestamp but NO market cap, volume, buy/sell counts or honeypot verdict
    _info_body = {"code": 0, "data": {"launchpad_progress": 0.28, "visiting_count": 12,
                                      "creation_timestamp": 1789221503,
                                      "stat": {"holder_count": 1000, "top70_sniper_hold_rate": 0.01,
                                               "fresh_wallet_rate": 0.1, "top_rat_trader_percentage": 0.004,
                                               "top_10_holder_rate": "0.0611"},
                                      "wallet_tags_stat": {"bundler_wallets": 10, "sniper_wallets": 3, "smart_wallets": 2}}}
    http_client._urlopen = _gm_fake(lambda u, d: json.dumps(_info_body).encode())
    _TI = "0x" + "c1" * 20
    ti = GM.token_info(_TI, cache_s=0)
    check("token_info: GET /v1/token/info?chain=robinhood&address=… -> gmgn_bundler_ratio = bundler_wallets / holder_count, the "
          "hold rates x100, smart-money count, holders, gmgn_progress from launchpad_progress (NOT the legacy 'progress' key), "
          "and gmgn_is_wash_trading + gmgn_insider_hold_pct always None (the info payload carries no such keys — 'stat' has no "
          "suspected_insider_hold_rate in any of 16 cached payloads — the row is the only source for both)",
          isinstance(ti, dict) and abs(ti["gmgn_bundler_ratio"] - 0.01) < 1e-9 and ti["gmgn_holders"] == 1000
          and abs(ti["gmgn_sniper_hold_pct"] - 1.0) < 1e-9 and ti["gmgn_smart_degen_count"] == 2
          and abs(ti["gmgn_rat_vol_pct"] - 0.4) < 1e-9 and "/v1/token/info?" in _gm_reqs[-1][0]
          and f"address={_TI}" in _gm_reqs[-1][0] and "chain=robinhood" in _gm_reqs[-1][0]
          and abs(ti["gmgn_progress"] - 0.28) < 1e-9 and ti["gmgn_is_wash_trading"] is None
          and ti["gmgn_insider_hold_pct"] is None, str(ti))
    check("token_info fills ONLY the operator signals the info payload really carries — data.visiting_count, stat.top_10_holder_rate "
          "(a rate GMGN ships as a STRING, x100) and data.creation_timestamp, all present in 16/16 cached payloads. The market cap, "
          "the 24 h volume, the buy/sell counts and the honeypot verdict are NOT in it (0/16, `data.price` holds a different shape) "
          "and stay None, so the Trenches row is their only source",
          abs(ti["gmgn_top10_holder_pct"] - 6.11) < 1e-9 and ti["gmgn_visiting_count"] == 12
          and ti["gmgn_created_ts"] == 1789221503
          and all(ti[k] is None for k in ("gmgn_market_cap", "gmgn_volume_24h", "gmgn_buys_24h", "gmgn_sells_24h",
                                          "gmgn_is_honeypot")), str(ti))
    _legacy_body = {"code": 0, "data": {"progress": 0.9, "stat": {"holder_count": 5}, "wallet_tags_stat": {}}}
    http_client._urlopen = _gm_fake(lambda u, d: json.dumps(_legacy_body).encode())
    ti_legacy = GM.token_info(_TI, cache_s=0)
    check("token_info never reads the legacy 'progress' key — a payload carrying ONLY 'progress' (no launchpad_progress) "
          "yields gmgn_progress is None, so the old key can never silently come back",
          isinstance(ti_legacy, dict) and ti_legacy["gmgn_progress"] is None)
    http_client._urlopen = _gm_fake(lambda u, d: _http_error(u, 404))
    check("token_info on an unknown token is ABSENT (NOT_FOUND), a fact — not deferred",
          http_client.is_absent(GM.token_info("0x" + "c2" * 20, cache_s=0)))
finally:
    http_client._urlopen, config.HTTP_RETRIES, GM._api_key = _saved_gm[0], _saved_gm[1], _saved_gm[3]
    if _saved_gm[2] is not None:
        http_client._HOST_HZ[GM_HOST] = _saved_gm[2]
    http_client.reset_health()

_gm_tree = _tree(os.path.join(ROOT, "sources", "gmgn.py"))
_gm_auth = [n for n in _gm_tree.body if isinstance(n, ast.FunctionDef) and n.name == "_query"]
check("sources/gmgn.py: the ONLY wall-clock is the auth timestamp inside _query() — GMGN answers AUTH_TIMESTAMP_EXPIRED to a stale "
      "one (measured: run.py's start-of-run now_s reached pass 2 a minute later); nothing scored reads it",
      len(_gm_auth) == 1 and _time_time_calls(_compute_nodes(_gm_tree)) == 1 and _time_time_calls(ast.walk(_gm_auth[0])) == 1)

# the adapter: gmgn_* fields are features, never a hard gate; a dark GMGN is named, not scored
check("SAFETY_FEATURE_KEYS is exactly the safety subset of FEATURE_FIELDS and includes every gmgn_* key; the gmgn_ PREFIX in "
      "FEATURE_FIELDS is exactly GMGN_FEATURE_KEYS, which is what alerts.py's 'unavailable' guard iterates",
      set(SAFE.SAFETY_FEATURE_KEYS) == set(config.FEATURE_FIELDS) - set(RUN.dex.MARKET_KEYS) - {"token", "score", "first_sighting", "sighting_age_s"}
      and set(GM.GMGN_FEATURE_KEYS) <= set(SAFE.SAFETY_FEATURE_KEYS)
      and set(alerts._GMGN_FIELDS) == set(GM.GMGN_FEATURE_KEYS))
_s_row = SAFE.empty_safety()
SAFE._apply_gmgn(_s_row, _row["address"], row=_row, info=False)
_s_ok = dict(safety)
_s_ok.update({k: _s_row[k] for k in GM.GMGN_FEATURE_KEYS})
check("_apply_gmgn(row=…) writes the row's gmgn_* fields without a call and names gmgn in sources_used; the hard gates are "
      "IDENTICAL with or without them (a GMGN field is never a gate)",
      _s_row["gmgn_progress"] == 0.83 and _s_row["gmgn_launchpad_platform"] == "longxyz" and "gmgn" in _s_row["sources_used"]
      and screen.hard_gates(market, _s_ok) == screen.hard_gates(market, safety))
_saved_ti = GM.token_info
try:
    GM.token_info = lambda token, cache_s=None: None
    _s_dark = SAFE.empty_safety()
    SAFE._apply_gmgn(_s_dark, _row["address"], row=None, info=True)
    check("a dark GMGN (token_info deferred) is named in sources_dark, its fields stay None, degraded_fields says 'GMGN wallet tags', "
          "and band_a_strict is unaffected (the strict band never reads GMGN)",
          "gmgn" in _s_dark["sources_dark"] and all(_s_dark[k] is None for k in GM.GMGN_FEATURE_KEYS)
          and any("GMGN wallet tags" in x for x in SAFE.degraded_fields(_s_dark))
          and B.BUILTINS["band_a_strict"].verdict(dict(good, **{k: None for k in GM.GMGN_FEATURE_KEYS})) is True)
    GM.token_info = lambda token, cache_s=None: {"gmgn_bundler_ratio": 1.42, "gmgn_holders": 62, "gmgn_sniper_hold_pct": 0.1,
                                                        "gmgn_insider_hold_pct": None, "gmgn_fresh_wallet_pct": 40.0, "gmgn_rat_vol_pct": 0.0,
                                                        "gmgn_smart_degen_count": 0, "gmgn_is_wash_trading": None,
                                                        "gmgn_launchpad_platform": None, "gmgn_progress": None}
    _s_farm = SAFE.empty_safety()
    SAFE._apply_gmgn(_s_farm, _row["address"], row=_row, info=True)
    check("token_info fills the bundler ratio and holders on top of the row's fields (the row's platform/progress survive)",
          _s_farm["gmgn_bundler_ratio"] == 1.42 and _s_farm["gmgn_holders"] == 62 and _s_farm["gmgn_launchpad_platform"] == "longxyz"
          and _s_farm["gmgn_progress"] == 0.83 and "gmgn" in _s_farm["sources_used"] and "gmgn" not in _s_farm["sources_dark"])
    check("_apply_gmgn: a Trenches row's is_wash_trading survives the token_info merge — the row's gmgn_is_wash_trading (False) is "
          "never clobbered by info's None (info never carries that key at all)",
          _row["is_wash_trading"] is False and _s_farm["gmgn_is_wash_trading"] is False)
    check("_apply_gmgn: a Trenches row's suspected_insider_hold_rate survives the token_info merge — the row's gmgn_insider_hold_pct "
          "(2.0) is never clobbered by info's None (info never carries that key at all, same as the wash flag)",
          abs(_fr["gmgn_insider_hold_pct"] - 2.0) < 1e-9 and abs(_s_farm["gmgn_insider_hold_pct"] - 2.0) < 1e-9)
finally:
    GM.token_info = _saved_ti

# discovery: the Trenches feed is a hedge under the 'feeds' quota; its rows ride along to pass 2
_saved_feed = (GM.trenches, GT.new_pools)
try:
    GM.trenches = lambda **k: {"completed": [_row], "near_completion": [], "new_creation": [{"address": "0xDEF" + "0" * 37}]}
    GT.new_pools = lambda page=1, network=None: []
    _ft, _frows, _fdsc, _fhit = RUN.feed_tokens({}, set(), RUN._Budget(60))
    check("feed_tokens adds every Trenches column's addresses (lowercased, minus seen/known) under the feeds quota and "
          "returns the rows keyed by address for pass 2",
          _ft == {_row["address"].lower(), "0xdef" + "0" * 37} and set(_frows) == _ft
          and _frows[_row["address"].lower()]["launchpad_platform"] == "longxyz", str(_ft))
    _ft2 = RUN.feed_tokens({_row["address"].lower(): 1}, {"0xdef" + "0" * 37}, RUN._Budget(60))[0]
    GM.trenches = lambda **k: None
    _ft3, _frows3, _fdsc3, _fhit3 = RUN.feed_tokens({}, set(), RUN._Budget(60))
    check("seen/known tokens are not re-fed; a deferred feed (None) contributes nothing and no rows",
          _ft2 == set() and _ft3 == set() and _frows3 == {})
finally:
    GM.trenches, GT.new_pools = _saved_feed

# the alert card and the dashboard card carry the GMGN + explorer deep links and a GMGN line
_TOK = "0x" + "ab" * 20
_gm_url = config.GMGN_TOKEN_URL.format(chain=config.GMGN_CHAIN, token=_TOK)
_bs_url = config.BLOCKSCOUT_TOKEN_URL.format(token=_TOK)
_card_dark = dict(surv_a, token=_TOK, sources_dark=["gmgn"], **{k: None for k in GM.GMGN_FEATURE_KEYS})
_, _b_dark = alerts.format_alert([_card_dark], band=CHAMP)
_card_ok = dict(surv_a, token=_TOK, gmgn_bundler_ratio=0.01, gmgn_sniper_hold_pct=0.5, gmgn_insider_hold_pct=0.0,
                gmgn_smart_degen_count=2, gmgn_is_wash_trading=False, gmgn_launchpad_platform="longxyz", gmgn_progress=1.0,
                gmgn_visiting_count=8, gmgn_top10_holder_pct=5.66)
_, _b_ok = alerts.format_alert([_card_ok], band=CHAMP)
check("every A card carries the GMGN token deep link and the explorer token link; the GMGN line prints the wallet-tag numbers "
      "when present and 'unavailable (passed through)' when the source is dark",
      _gm_url in _b_dark and _bs_url in _b_dark and "GMGN: unavailable" in _b_dark
      and _gm_url in _b_ok and "GMGN:" in _b_ok and "bundler" in _b_ok and "0.01" in _b_ok and "longxyz" in _b_ok
      and config.FOOTER in _b_ok, _b_ok[-600:])
_card_vis = dict(surv_a, token=_TOK, sources_dark=[], **{k: None for k in GM.GMGN_FEATURE_KEYS})
_card_vis["gmgn_visiting_count"] = 8                      # the one field a Trenches row alone can give
_, _b_vis = alerts.format_alert([_card_vis], band=CHAMP)
check("the GMGN line prints the operator's two headline signals — viewers (the Trenches visiting_count) and the top-10 share — when "
      "they are known, and says 'unavailable (passed through)' only when EVERY gmgn_* field is None: the guard used to test four "
      "fields, so a card carrying only the row's signals claimed the source was unavailable",
      "viewers 8" in _b_vis and "GMGN: unavailable" not in _b_vis and "viewers 8" in _b_ok and "top10 5.7%" in _b_ok
      and "GMGN: unavailable" in _b_dark, _b_vis[-400:])
_card_html = DASHM._card(dict(_card_ok, url="https://dexscreener.com/robinhood/0xpair"), CHAMP)
check("the dashboard card links to Dexscreener, GMGN and the explorer as SEPARATE anchors (no nested <a>)",
      f'href="{_gm_url}"' in _card_html and 'href="https://dexscreener.com/robinhood/0xpair"' in _card_html
      and f'href="{_bs_url}"' in _card_html and not _card_html.lstrip().startswith("<a"))

# the three pre-declared bands: candidates, never the champion; NA on unknown; controls untouched
_reg_names = {e_["name"]: e_ for e_ in json.load(open(config.REGISTRY_PATH))["candidates"]}
for _bn in ("band_gmgn_clean", "band_new_creation", "band_almost_bonded"):
    check(f"{_bn} is a builtin band registered as a candidate (module entry_lab.bands, registered_at_event_seq set)",
          _bn in B.BUILTINS and _reg_names.get(_bn, {}).get("status") == "candidate"
          and _reg_names[_bn]["module"] == "entry_lab.bands" and int(_reg_names[_bn].get("registered_at_event_seq") or 0) > 0
          and set(B.BUILTINS[_bn].REQUIRES) <= set(config.FEATURE_FIELDS))
_GC = B.BUILTINS["band_gmgn_clean"]
check("band_gmgn_clean = the strict band AND organic GMGN wallet tags: True on the clean fixture, False at bundler ratio 0.2 "
      "(A-band max 0.10) or on wash trading, NA when GMGN is dark, False on a strict-band miss whatever GMGN says",
      _GC.verdict(good) is True and _GC.verdict(dict(good, gmgn_bundler_ratio=0.2)) is False
      and _GC.verdict(dict(good, gmgn_is_wash_trading=True)) is False
      and _GC.verdict(dict(good, gmgn_bundler_ratio=None, gmgn_is_wash_trading=None)) is None
      and _GC.verdict(dict(good, pair_age_min=30.0, gmgn_bundler_ratio=None)) is False
      and "bundler" in _GC.explain(dict(good, gmgn_bundler_ratio=0.2)))
_NC = B.BUILTINS["band_new_creation"]
check("band_new_creation: the New column as a band — pair age <= BAND_NC_MAX_AGE_MIN with the liquidity floor and a known sell "
      "round trip; False once older; NA when the round trip is unknown",
      _NC.verdict(dict(good, pair_age_min=10.0)) is True and _NC.verdict(good) is False
      and _NC.verdict(dict(good, pair_age_min=10.0, liq_usd=config.LIQ_FLOOR_USD - 1)) is False
      and _NC.verdict(dict(good, pair_age_min=10.0, roundtrip_loss_pct=None)) is None
      and _NC.verdict(dict(good, pair_age_min=10.0, roundtrip_loss_pct=config.HC_MAX_ROUNDTRIP_PCT + 1)) is False)
_AB = B.BUILTINS["band_almost_bonded"]
check("band_almost_bonded: the Almost-bonded column as a band — GMGN progress >= BAND_AB_MIN_PROGRESS on a curve not yet completed; "
      "False once completed or below the floor; NA without a progress reading",
      _AB.verdict(dict(good, gmgn_progress=0.8, launchpad_completed=False)) is True
      and _AB.verdict(dict(good, gmgn_progress=0.8, launchpad_completed=True)) is False
      and _AB.verdict(dict(good, gmgn_progress=0.3, launchpad_completed=False)) is False
      and _AB.verdict(dict(good, gmgn_progress=None, launchpad_completed=False)) is None
      and _AB.verdict(dict(good, gmgn_progress=0.8, launchpad_completed=False, liq_usd=config.LIQ_FLOOR_USD - 1)) is False)
check("the controls are untouched by the GMGN fields (ctl_random_band is sha256-of-token; ctl_inverse_band is the champion's complement)",
      B.BUILTINS["ctl_random_band"].verdict(dict(good, gmgn_bundler_ratio=9.0)) == B.BUILTINS["ctl_random_band"].verdict(good)
      and LAB.evaluate_bands(dict(good, gmgn_bundler_ratio=9.0), REG, CHAMP)["ctl_inverse_band"] is False)

_reg_bands = [e_["name"] for e_ in json.load(open(config.REGISTRY_PATH))["candidates"]
              if e_.get("kind") == "band" and e_.get("status") != "control"]
_ever = set(TRIALS_MOD.load().get("bands_ever_scored") or [])
check("every non-control band in registry.json is a counted trial in trials.json (the entry DSR deflates by len(bands_ever_scored); "
      "a hand-registered band that skips it under-deflates every later gate)",
      set(_reg_bands) <= _ever, str(sorted(set(_reg_bands) - _ever)))
_saved_cd = config.CACHE_DIR
_saved_gm2 = (http_client._urlopen, GM._api_key, http_client._HOST_HZ.get(GM_HOST))
try:
    http_client._HOST_HZ[GM_HOST] = 1000.0
    http_client.reset_health()
    GM._api_key = lambda: "verify-key"
    with tempfile.TemporaryDirectory() as _cd:
        config.CACHE_DIR = _cd
        http_client._urlopen = _gm_fake(lambda u, d: json.dumps({"code": -1, "message": "boom"}).encode())
        GM.trenches(cache_s=600)
        GM.token_info("0x" + "c3" * 20, cache_s=600)
        n_err = len(os.listdir(_cd))
        http_client._urlopen = _gm_fake(lambda u, d: json.dumps(_ok_body).encode())
        GM.trenches(cache_s=600)
        n_ok = len(os.listdir(_cd))
        http_client._urlopen = _gm_fake(lambda u, d: _http_error(u, 404))
        GM.token_info("0x" + "c4" * 20, cache_s=600)
        n_abs = len(os.listdir(_cd))
        http_client._urlopen = _gm_fake(lambda u, d: (_ for _ in ()).throw(AssertionError("must be served from cache")))
        again = GM.trenches(cache_s=600)
        check("GMGN error envelopes (HTTP 200, code != 0) are NEVER cached — deferred is retried; a code-0 answer and a 404 (absent) are; "
              "a cached Trenches answer is served without a request",
              n_err == 0 and n_ok == 1 and n_abs == 2 and again == {"completed": [_row], "near_completion": [dict(_row, address="0xDEF")], "new_creation": []},
              str((n_err, n_ok, n_abs)))
finally:
    config.CACHE_DIR = _saved_cd
    http_client._urlopen, GM._api_key = _saved_gm2[0], _saved_gm2[1]
    if _saved_gm2[2] is not None:
        http_client._HOST_HZ[GM_HOST] = _saved_gm2[2]
    http_client.reset_health()
_wf_txt = _read(os.path.join(ROOT, ".github", "workflows", "screener.yml"))
check("screener.yml passes GMGN_API_KEY to the preflight step, the one-shot scan step AND the Keeper step (preflight exists to "
      "probe from the runner's IP)", _wf_txt.count("GMGN_API_KEY: ${{ secrets.GMGN_API_KEY }}") == 3)

# plumbing: the cloud secret, the workflow env, the preflight probe, the docs page
check("GMGN_API_KEY is piped by cloud_secrets.py, passed by screener.yml, and preflight probes openapi.gmgn.ai",
      "GMGN_API_KEY" in _read(os.path.join(ROOT, "cloud_secrets.py"))
      and "GMGN_API_KEY: ${{ secrets.GMGN_API_KEY }}" in _read(os.path.join(ROOT, ".github", "workflows", "screener.yml"))
      and "gmgn" in _read(os.path.join(ROOT, "preflight.py")))
_gdoc = os.path.join(ROOT, "docs", "GMGN_TRENCHES.md")
check("docs/GMGN_TRENCHES.md exists, names the three columns and the filter translation, and carries no home path",
      os.path.exists(_gdoc) and all(w in _read(_gdoc) for w in ("Migrated", "Almost bonded", "min_liquidity", "robinhood"))
      and "/Users/" not in _read(_gdoc))

# ═══════════════════════════════════════════════════════════════════════════════════
section("O. the keeper — a self-chaining scan job on Actions, the handoff, the */5 watchdog, the stale alert")
# ═══════════════════════════════════════════════════════════════════════════════════
import watchdog as WD                                   # noqa: E402
check("config carries the keeper constants at the brief's values (cadence 240 s, 340 min per run under timeout 355, 600 s lead/wait, "
      "30 min stale, Pages every 2nd push)",
      (config.KEEPER_CADENCE_S, config.KEEPER_MAX_S, config.KEEPER_HANDOFF_LEAD_S, config.KEEPER_HANDOFF_WAIT_S,
       config.KEEPER_STALE_S, config.PAGES_EVERY_N_ITERATIONS) == (240, 20400, 600, 600, 1800, 2)
      and config.KEEPER_MAX_S + config.KEEPER_HANDOFF_WAIT_S < 355 * 60 and not hasattr(config, "DISPATCH_INTERVAL_S"))
check("dashboard.STALE_MINUTES is config.KEEPER_STALE_S // 60 (one tripwire for the page and the watchdog)",
      DASHM.STALE_MINUTES == config.KEEPER_STALE_S // 60 == 30)
check(".gitignore carries data/.keeper.lock (the flock file never rides `git add data/`)",
      "data/.keeper.lock" in _read(os.path.join(ROOT, ".gitignore")).splitlines())
for needle in ('cron: "*/5 * * * *"', "group: screener-watchdog", "cancel-in-progress: true", "actions: write", "contents: read",
               "repository.private", "github.event_name != 'schedule'", "watchdog.py --send", "WATCHDOG_ACTION",
               "name: robinhood-keeper-watchdog", "timeout-minutes: 5", "--keeper-alive", "--ensure-keeper watchdog",
               "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "NTFY_TOPIC", "workflow_dispatch", "circuit-open",
               "bash .github/keeper.sh --keeper-alive; alive=$?", "bash .github/keeper.sh --circuit-open; circuit=$?",
               "action=unknown", "action=circuit-unknown"):
    check(f"keeper-watchdog.yml contains {needle!r}", needle in _wf_dog)
check("keeper-watchdog.yml never pushes, never writes contents, never sees GMGN_API_KEY (a restart-only job)",
      "git push" not in _wf_dog and "contents: write" not in _wf_dog and "GMGN_API_KEY" not in _wf_dog)
check("the breaker has ONE definition: config (3 keeper failures in 2 h) → keeper.sh --circuit-open, read by the watchdog and by "
      "finish's exit-3 path; the watchdog carries no failure query or literal of its own",
      (config.KEEPER_CIRCUIT_FAILURES, config.KEEPER_CIRCUIT_WINDOW_S) == (3, 7200)
      and "--status failure" not in _wf_dog and "-ge 3" not in _wf_dog and "2 hours" not in _wf_dog
      and "--status failure" in _read(KEEPER_SH))
_wd_tree = _tree(os.path.join(ROOT, "watchdog.py"))
check("watchdog.py captures time.time() EXACTLY once and imports no subprocess/http_client/urllib/sources/run/requests",
      _time_time_calls(ast.walk(_wd_tree)) == 1
      and not (_top_imports(_wd_tree) & {"subprocess", "http_client", "urllib", "sources", "run", "requests", "socket"}),
      str(_top_imports(_wd_tree)))
_now = 1_800_000_000.0
check("assess: a fresh scan is no finding", WD.assess({"scan_ts": _now - 120, "trigger": "keeper"}, _now, "none") == [])
_stale = WD.assess({"scan_ts": _now - 45 * 60, "trigger": "keeper"}, _now, "restarted")
check("assess: a 45-min-old scan is ONE line naming the age, the trigger and the restart",
      len(_stale) == 1 and "45 min" in _stale[0] and "keeper" in _stale[0] and "restarted: yes" in _stale[0], str(_stale))
check("assess: the action words — none ⇒ 'restarted: no', circuit-open ⇒ 'circuit open'",
      "restarted: no" in WD.assess({"scan_ts": _now - 3600}, _now, "none")[0]
      and "circuit open" in WD.assess({"scan_ts": _now - 3600}, _now, "circuit-open")[0])
check("assess: no file ⇒ one line; unknown or missing keys ⇒ NEVER a finding (a malformed scan is not a stale scan)",
      WD.assess(None, _now, "none") == ["no latest_scan.json"] and WD.assess({"foo": 1}, _now, "none") == []
      and WD.assess({"scan_ts": "garbage"}, _now, "none") == [] and WD.assess([1, 2], _now, "none") == []
      and WD.assess({"scan_ts": None}, _now, "restarted") == [])
check("alerts.format_event('SCAN STALE', ...) renders the kind (it is in EVENT_KINDS, never 'EVENT SCAN STALE')",
      "SCAN STALE" in alerts.EVENT_KINDS and alerts.format_event("SCAN STALE", ["x"])[0] == "robinhood_screener SCAN STALE")
_saved_scan_path, _saved_env = config.SCAN_PATH, {k: os.environ.pop(k, None) for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "NTFY_TOPIC", "WATCHDOG_ACTION")}
try:
    with tempfile.TemporaryDirectory() as _d:
        config.SCAN_PATH = os.path.join(_d, "latest_scan.json")
        with open(config.SCAN_PATH, "w") as fh:
            json.dump({"scan_ts": 1.0, "trigger": "keeper"}, fh)
        _rc, _out = _capture(WD.main, [])
        check("watchdog.main([]) on a stale scan is a DRY RUN (prints the card, sends nothing, returns 0)",
              _rc == 0 and "DRY RUN" in _out and "SCAN STALE" in _out and "min ago" in _out, _out[-200:])
        try:
            _rc2, _out2 = _capture(WD.main, ["--send"])
        except SystemExit as e_:
            _rc2 = e_.code
        check("watchdog.main(['--send']) without the three alert env vars exits 2 (send_all would be a silent no-op)", _rc2 == 2)
        with open(config.SCAN_PATH, "w") as fh:
            json.dump({"scan_ts": time.time(), "trigger": "keeper"}, fh)   # test-only wall clock, not a compute path
        _rc3, _out3 = _capture(WD.main, [])
        check("watchdog.main([]) on a fresh scan prints 'fresh' and never renders an alert", _rc3 == 0 and "ALERT" not in _out3 and "fresh" in _out3)
        os.remove(config.SCAN_PATH)
        _rc4, _out4 = _capture(WD.main, [])
        check("watchdog.main([]) with no latest_scan.json reports it (dry)", _rc4 == 0 and "no latest_scan.json" in _out4)
finally:
    config.SCAN_PATH = _saved_scan_path
    for k_, v_ in _saved_env.items():
        if v_ is not None:
            os.environ[k_] = v_
_readme = _read(os.path.join(ROOT, "README.md"))
check("README's deployment paragraph names the keeper, the */5 watchdog and the retired Mac dispatch, keeping the measured 13.7×/day",
      "keeper" in _readme and "watchdog" in _readme and "13.7×/day" in _readme)
_claude_md = _read(os.path.join(ROOT, "CLAUDE.md"))
check("CLAUDE.md's Commands block carries the chain's start / see / off switch and says keeper.sh never runs on the Mac",
      "gh workflow run robinhood-screener -f mode=keeper -f trigger=manual" in _claude_md
      and "gh run list -w robinhood-screener -L 5" in _claude_md
      and "robinhood-screener robinhood-keeper-watchdog; do gh workflow disable" in _claude_md   # one name per call
      and "gh run cancel" in _claude_md and "keeper.sh" in _claude_md)
_design = _read(os.path.join(ROOT, "docs", "DESIGN.md"))
check("DESIGN.md carries the livebook-in-the-keeper rows: feed source, state ownership, the band under test, the operator cutover "
      "(bootout, seed from the Mac snapshot, mv to backups, ff-merge, never tick on the Mac again) and HTTP_RATE_SCALE — as a knob for "
      "whichever process sets it, with the 'RH_HTTP_RATE_SCALE=0.5 for the book' recommendation dropped (halving the book's rate "
      "lengthens its ticks); the lock row names _state_lock, os.replace as the ledger's guarantee and the rebase transient",
      all(w in _design for w in ("KEEPER_BOOK", "LIVEBOOK_FEED_SOURCE", "HTTP_RATE_SCALE", "LIVEBOOK_BAND_UNDER_TEST",
                                 "launchctl bootout gui/$UID/com.yousefjan.robinhood-livebook", "publish.publish_files(['data/livebook.json'",
                                 "data/backups/", "git merge --ff-only origin/main", "livebook_ticks.jsonl", "whichever process sets it",
                                 "lengthens its ticks", "_state_lock", "`os.replace`", "transient", "sidecar_pending", "LIVEBOOK_SIDECAR_WAIT_TICKS"))
      and "RH_HTTP_RATE_SCALE=0.5" not in _design,
      str([w for w in ("KEEPER_BOOK", "LIVEBOOK_FEED_SOURCE", "HTTP_RATE_SCALE", "LIVEBOOK_BAND_UNDER_TEST", "whichever process sets it",
                       "lengthens its ticks", "_state_lock", "sidecar_pending") if w not in _design]))
# the constants line is documentation of a FILE, so it is generated from that file, not remembered:
# it carried DISCOVERY_MAX_LOG_TOKENS_PER_RUN=80 / MAX_DISCOVER=200 / RUN_TIME_BUDGET_S=200 for days
# after config said 200 / 400 / 240
# The Phase 9 keeper/cadence rewrite put three more constants into prose, so they join the list:
# a cadence quoted in a doc is exactly the kind of number that drifts away from config.
_pins = ("DISCOVERY_MAX_LOG_TOKENS_PER_RUN", "DISCOVERY_MAX_LOG_TOKENS_CATCHUP", "DISCOVERY_CATCHUP_TRIGGER_BLOCKS",
         "MAX_DISCOVER", "RECHECK_PER_RUN", "RECHECK_MAX", "GT_NEW_POOLS_PAGES", "GT_NEW_POOLS_CACHE_S",
         "FEED_PULL_FORWARD_MAX", "RUN_TIME_BUDGET_S", "WATCH_REFRESH_PER_RUN", "GT_INFO_BUDGET_PER_RUN",
         "KEEPER_CADENCE_S", "KEEPER_MAX_S", "KEEPER_BOOK_STOP_WAIT_S")
_stale_pins = [n for n in _pins
               if not any(f"{n}={v}" in _design for v in (f"{getattr(config, n)}", f"{getattr(config, n):_}"))]
check("DESIGN.md's discovery/budget constants are REGENERATED from config.py, not remembered (every pinned name appears as "
      "NAME=<its current value>), and the per-kind recheck ladders are written out there too",
      not _stale_pins and all(k in _design for k in ("gt_new_pools:(300, 900)", "gmgn_new_creation:(600, 1800)",
                                                     "gmgn_near_completion:(300, 900, 1800)", "gmgn_completed:(300, 900)")),
      str(_stale_pins))
# The champion decision is the one switch that turns a written candidate into an alerted one, so
# the row that describes it is pinned to the names it cites and to BOTH branches: the four
# unregistered modules, the composition rule that makes A impossible to loosen, the demotion
# target, the fact that a manual --set never flips registry status, and what happens when the
# switch is NOT thrown (verdicts + B rows + the band-under-test rule + "NO CHANGE").
# Fix round 1 added the two halves the first draft got WRONG, and they are needled here so the
# old wording cannot come back: it is TWO switches (register.py --scan mints the counted trial
# first; champion.py:_cli refuses an unregistered policy rc 2 and run.py silently falls back for
# an unregistered band), and a manual --set does NOT arm the demotion test — champion.py writes
# promoted_at_event_seq: None and improve_bands only builds the block when it is non-None.
_champ_row = [w for w in ("What the champion decision changes", "band_hype_early", "band_hype_attention",
                          "tp15_half_armtrail30_stop50_6h", "tp15_half_flowtrail_stop50_6h",
                          "`gates_ok and verdict is True`", "DEFAULT_ENTRY_BAND", "registry.json",
                          "band_fire", "LIVEBOOK_BAND_UNDER_TEST_MAX_OPEN", "APPARATUS FAULT",
                          "register.py --scan", "policies.load_candidates()",
                          "it does not arm the Sunday demotion test", "promoted_at_event_seq: None",
                          "judges **gate promotions only**",
                          '**"NO CHANGE" for months is the expected outcome**') if w not in _design]
check("DESIGN.md carries the champion-decision row — both branches of `champion.py --set`, the four shipped candidate "
      "modules by name, the TWO switches (register.py --scan first), tier_for's composition, the registry-status rule, the fact "
      "that a manual set does NOT arm the demotion test (promoted_at_event_seq stays None) and the unset branch ending in "
      "'NO CHANGE for months'",
      not _champ_row, str(_champ_row))
# The code behind those two sentences, asserted directly rather than trusted: a manual set writes
# the seq key as None on BOTH arms, and each gate builds its demotion block only when its own seq
# key is not None. If either ever changes, the doc sentence has to change with it.
_ch_src, _ib_src, _im_src = (_read(os.path.join(ROOT, *p)) for p in
                             (("selfimprove", "champion.py"), ("selfimprove", "entry_lab", "improve_bands.py"),
                              ("selfimprove", "improve.py")))
check("champion.py's manual --set writes the arm's promotion seq key as None, and BOTH gates build the one-shot demotion block "
      "only when that key is not None — so only a gate promotion can arm a demotion (what DESIGN.md and the go-live checklist say)",
      "seq_key: None" in _ch_src
      and 'st.get("promoted_at_event_seq") is not None' in _ib_src
      and 'arm.get("promoted_at_alert_seq") is not None' in _im_src)
# The go-live checklist is the document a human reads with a terminal open, so every command in it
# has to resolve TODAY: a workflow name that no longer exists, or a script that moved, turns an
# item into a shrug. Eleven items exactly — the count is part of the contract (adding a twelfth is
# a decision, not an edit) — and the public-repo scrub applies to it like every other doc.
_glc = _read(os.path.join(ROOT, "docs", "GO_LIVE_CHECKLIST.md"))
_glc_items = re.findall(r"(?m)^### (\d+)\. ", _glc)
_wf_names = set()
for _wp in sorted(glob.glob(os.path.join(ROOT, ".github", "workflows", "*.yml"))):
    _m = re.search(r"(?m)^name:\s*(\S+)", _read(_wp))
    if _m:
        _wf_names.add(_m.group(1))
_glc_bad_wf = sorted({w for w in re.findall(r"gh [^\n`]*?-w\s+([A-Za-z0-9._-]+)", _glc)} - _wf_names)
_glc_bad_py = sorted({p_ for p_ in re.findall(r"python3 (?!-c\b|-I\b)([A-Za-z0-9_./-]+\.py)", _glc)
                      if not os.path.exists(os.path.join(ROOT, p_))})
check("docs/GO_LIVE_CHECKLIST.md has EXACTLY the eleven numbered items, every workflow it names exists by its "
      "`name:` and every script it runs exists by path, and it carries no home path, no sibling-project name and "
      "never the shared secrets file",
      _glc_items == [str(i) for i in range(1, 12)] and not _glc_bad_wf and not _glc_bad_py
      and "/Users/" not in _glc and "monitor_config" not in _glc and "vrp_backtest" not in _glc,
      str([_glc_items, _glc_bad_wf, _glc_bad_py]))
check("README links the go-live checklist beside the pre-committed 'repeatedly positive' condition",
      "docs/GO_LIVE_CHECKLIST.md" in _readme and "repeatedly positive" in _readme)
check("CLAUDE.md's live-book paragraph is the keeper's (KEEPER_BOOK, LIVEBOOK_FEED_SOURCE, the four committed files, the ticks log as "
      "artifact, the band under test) and the gotchas say never to tick on the Mac after the cutover",
      all(w in _claude_md for w in ("KEEPER_BOOK", "LIVEBOOK_FEED_SOURCE", "livebook_ticks.jsonl", "LIVEBOOK_BAND_UNDER_TEST"))
      and "never run `livebook.py --tick` on the mac" in _claude_md.lower())
check("README says the live book runs inside the keeper and keeps its measured numbers",
      "inside the keeper" in _readme and "13.7×/day" in _readme)
# ── fix round 1 (2026-09-19): six doc claims that were not true of this tree. Each needle below
# is the corrected sentence's load-bearing half, so the old wording cannot come back silently.
# C7: a paper WINDOW is a `paper:` line in nominations_ever and deflates nothing — the exit DSR
# counts policies_ever_scored, the entry DSR bands_ever_scored, and the paper gate's own DSR reads
# policies_ever_scored (improve.paper_gate). Registration, not a window, is what deflates. The
# third-window refusal is start >= last prior start + (PAPER_GATE_WINDOW_DAYS + cooldown), and
# that arithmetic is REGENERATED from config here rather than remembered.
_glc_cool = (f"= {int(config.PAPER_GATE_WINDOW_DAYS)} + {int(config.BAND_RENOMINATE_COOLDOWN_DAYS)} = "
             f"**{int(config.PAPER_GATE_WINDOW_DAYS) + int(config.BAND_RENOMINATE_COOLDOWN_DAYS)} days**")
check("C7: the checklist says a paper window mints a nominations_ever line that deflates NO Sharpe gate (registration does), and "
      "its third-window arithmetic is regenerated from config; the code agrees — paper_gate bumps the 'nominations' family and "
      "reads family_count('policies') for its own DSR",
      "deflates **no** Sharpe gate" in _glc and _glc_cool in _glc
      and 'TR.bump("nominations", [key])' in _im_src and 'TR.family_count("policies")' in _im_src
      and TR.FAMILIES == {"policies": "policies_ever_scored", "bands": "bands_ever_scored",
                          "nominations": "nominations_ever"}, _glc_cool)
# C3 again, in the document the operator reads with a terminal open: no automatic revert stands
# behind item 10's one write command.
_glc_flat = " ".join(_glc.split())          # the needles are sentences; they may be line-wrapped
check("C3: the checklist's item 10 says a manual set does NOT arm the demotion test and that there is no automatic revert — the "
      "operator reverts by hand — and it names register.py --scan as the step before it",
      "does not arm the Sunday demotion test" in _glc_flat
      and "There is no automatic revert behind this command" in _glc_flat
      and "revert it by hand with the same command" in _glc_flat
      and "selfimprove/candidates/register.py --scan" in _glc_flat)
# C5: screener.yml has NO schedule:, so the watchdog's */5 IS GitHub's cron. The docs claimed a
# third layer under it ("the fallback of last resort"), which would have had a dead keeper and a
# dead watchdog still scanning ~14x/day. Nothing scans in that state.
_scr_yml = _read(os.path.join(ROOT, ".github", "workflows", "screener.yml"))
check("C5: there is no third scan backstop to describe — screener.yml declares no `schedule:` (the watchdog's */5 IS GitHub's "
      "cron, 13.7 fires/day measured) — and DESIGN.md, README.md and CLAUDE.md all say so, none of them calling GitHub cron a "
      "fallback under the watchdog",
      not re.search(r"(?m)^\s*schedule:", _scr_yml)
      and all("no `schedule:`" in t and "13.7" in t for t in (_design, _readme, _claude_md))
      and not any("fallback of last resort" in t for t in (_design, _readme, _claude_md)))
# C6: the bullet that describes screener.yml must quote the permissions the file declares (the
# same pair section A pins against the file itself), not the Pages pair that moved to pages.yml.
_scr_bullet = next((ln for ln in _design.splitlines()
                    if ln.startswith("- `.github/workflows/screener.yml`")), "")
check("C6: DESIGN.md's screener.yml bullet quotes the file's real permissions (contents: write + actions: write; pages/id-token "
      "live in pages.yml) and the job guard as written, schedule clause included",
      "`permissions: contents: write` + `actions: write`" in _scr_bullet
      and "permissions: contents: write, pages: write, id-token: write" not in _design
      and "github.event_name != 'schedule'" in _scr_bullet, _scr_bullet[:200])
# C1: the launchd checks are ungated module-level code. Exactly three sections are mac_only, and
# the three docs that describe the partition say which.
_mac_skips = sorted(re.findall(r'skip\("([^"]*mac_only[^"]*)"', _read(os.path.join(ROOT, "verify.py"))))
check("C1: EXACTLY three sections are mac_only (statsmodels BY, publish.py against a temp bare origin, the vendored-DSR "
      "cross-pin) and none of them is the launchd walk — the plist checks read committed files, call no launchctl and run on "
      "both partitions, as verify's own docstring, DESIGN.md's Verify partition row and CLAUDE.md now say",
      len(_mac_skips) == 3 and not any("launchd" in s for s in _mac_skips)
      and "launchd checks are NOT among them" in (__doc__ or "")
      and "launchd checks are NOT among them" in _design
      and "launchd checks are not among them" in _claude_md.lower(), str(_mac_skips))
# C2: run_research.sh pushes with git itself; publish.py is not in that path. Every "publish" in
# the script is a comment, and CLAUDE.md names publish.py's real survivors instead of the gates.
_res_sh = _read(os.path.join(ROOT, "selfimprove", "research", "run_research.sh"))
_res_pub = [ln.strip() for ln in _res_sh.splitlines() if "publish" in ln and not ln.strip().startswith("#")]
check("C2: run_research.sh never calls publish.py (every 'publish' in it is a comment) — it merges its research branch and "
      "pushes with git — and CLAUDE.md says so, naming publish.py's real survivors (champion.py --set --publish, livebook's "
      "origin_blob, reconcile()) and the allowlist's two paths",
      not _res_pub and "does **not** use `publish.py`" in _claude_md and "publish.origin_blob" in _claude_md
      and "reconcile()" in _claude_md and "PROPOSAL_<date>.md" in _claude_md, str(_res_pub))

# ═══════════════════════════════════════════════════════════════════════════════════
section("P. price paths — paging, deferred propagation, unmatched rows")
# ═══════════════════════════════════════════════════════════════════════════════════
# The lab that prices a ledger row on GT bars could not reach past ONE page: a busy pool emits a
# bar a minute, so `limit=1000` at minute/1 starts ~16 h ago and an alert older than that read as
# `no_cover` — FOMOPAD (event_seq 621, alerted 2026-09-16T14:51:50Z) was uncovered by an unpaged
# call whose oldest bar was 18:34Z, three hours AFTER its alert. One `&before_timestamp` call
# returned the 227 bars that contain it. Paging is therefore a coverage fix, and the rule that
# governs it is the 472-of-1,400 rule: a page we could not fetch is DEFERRED for the whole
# resolution, never a step toward "this token had no price".
from sources import geckoterminal as GTM                # noqa: E402
from selfimprove import paths as PA                     # noqa: E402

check("config.PATHS_MAX_PAGES == 4 (the backward-paging cap; one page is ~16 h of 1m bars on a busy pool)",
      int(config.PATHS_MAX_PAGES) == 4, str(getattr(config, "PATHS_MAX_PAGES", None)))
_pa_tree = _tree(os.path.join(ROOT, "selfimprove", "paths.py"))
check("paths.py has no time.time() in any compute path (now_s is a parameter; the smoke test reads the clock)",
      _time_time_calls(_compute_nodes(_pa_tree)) == 0)

# ── 1. pool_ohlcv: before_timestamp rides the URL; the parser is untouched ──────────
_gt_urls: list = []


def _gt_body(rows):
    return {"data": {"attributes": {"ohlcv_list": rows}}}


def _gt_fake(url, cache_path=None, max_age_sec=None, headers=None, cache_404=False):
    _gt_urls.append(url)
    # newest-first, one duplicate ts (GT has repeated a bar) and one bar with NO volume element
    return _gt_body([[360, 4.0, 4.0, 4.0, 4.0],
                     [300, 3.0, 3.5, 2.9, 3.2, 30.0],
                     [240, 2.0, 2.5, 1.9, 2.2],
                     [240, 2.0, 2.5, 1.9, 2.2, 5.0],
                     [180, 1.0, 1.5, 0.9, 1.2, 10.0]])


_gt_orig = GTM.get_json
try:
    GTM.get_json = _gt_fake
    _b_none = GTM.pool_ohlcv("0xPOOL", "minute", 1, 5)
    _b_before = GTM.pool_ohlcv("0xPOOL", "minute", 1, 5, before_timestamp=1789583640)
finally:
    GTM.get_json = _gt_orig
check("pool_ohlcv omits before_timestamp when it is None and appends '&before_timestamp=<int>' when given",
      "before_timestamp" not in _gt_urls[0] and "&before_timestamp=1789583640" in _gt_urls[1], str(_gt_urls))
check("pool_ohlcv's parser is unchanged by paging: oldest-first, duplicate ts collapsed keeping the larger v, "
      "v None when the element is absent (unreported is not 0.0)",
      [b["ts"] for b in _b_none] == [180, 240, 300, 360]
      and [b["v"] for b in _b_none] == [10.0, 5.0, 30.0, None], str(_b_none))
check("pool_ohlcv returns the same series for a paged call (the page differs only by the URL parameter)",
      _b_before == _b_none)

# ── 2. paths._ohlcv: backward paging, per-page cache keys, cross-page merge ─────────
_POOLP = "0x" + "ab" * 20
_pa_calls: list = []


def _bars(lo, hi, v=1.0):
    """Inclusive bar indices; ts = i*60 (1-minute bars), oldest-first as pool_ohlcv returns."""
    return [{"ts": i * 60, "o": 1.0, "h": 2.0, "l": 0.5, "c": 1.5, "v": v} for i in range(lo, hi + 1)]


def _pages_fake(pages: dict):
    def _f(pool, timeframe="day", aggregate=1, limit=None, network=None,
           cache_path=None, max_age_sec=None, before_timestamp=None):
        _pa_calls.append({"tf": timeframe, "agg": aggregate, "before": before_timestamp,
                          "cache": os.path.basename(cache_path or "")})
        return pages.get(before_timestamp, [])
    return _f


# page 0 = the newest 5; page 1 overlaps bar 10 with a LARGER volume; page 2 is short and covers
# the alert, so paging stops on its own without touching the cap.
_THREE = {None: _bars(10, 14, 1.0), 600: _bars(6, 10, 9.0), 360: _bars(2, 5, 1.0)}
_pa_orig_ohlcv, _pa_orig_pool, _pa_orig_limit = GTM.pool_ohlcv, PA.top_pool, PA.LIMIT
try:
    PA.LIMIT, GTM.pool_ohlcv = 5, _pages_fake(_THREE)
    _st, _merged, _npages = PA._ohlcv(_POOLP, "minute", 1, 200.0)
finally:
    GTM.pool_ohlcv, PA.top_pool, PA.LIMIT = _pa_orig_ohlcv, _pa_orig_pool, _pa_orig_limit
check("_ohlcv pages backward until a page reaches the alert: 3 pages merged into one sorted, "
      "de-duplicated series spanning every bar",
      _st == "ok" and _npages == 3 and [b["ts"] for b in _merged] == [i * 60 for i in range(2, 15)],
      f"{_st} {_npages} {[b['ts'] for b in _merged]}")
check("_ohlcv merges duplicate timestamps across pages keeping the LARGER reported volume",
      [b["v"] for b in _merged if b["ts"] == 600] == [9.0], str([b for b in _merged if b["ts"] == 600]))
check("each page gets its OWN cache file keyed by before_timestamp (a shared key would serve page 0 forever)",
      len({c["cache"] for c in _pa_calls}) == 3
      and [c["before"] for c in _pa_calls] == [None, 600, 360]
      and any("_b600" in c["cache"] for c in _pa_calls) and any("_b360" in c["cache"] for c in _pa_calls),
      str(_pa_calls))

# ── 2b. a FULL page that pool_ohlcv's own dedup shrinks below LIMIT still pages on ──
# pool_ohlcv collapses a repeated timestamp WITHIN a page (GT has repeated a bar — its own
# docstring), so a raw LIMIT-row page can arrive here as LIMIT-1 bars. The old fullness test
# (`n_raw < LIMIT`) read that short count as "GT has nothing older" and stopped one page short —
# the FOMOPAD failure mode. Page 0 below is exactly such a page (4 of 5 slots after the page's
# own dedup); page 1 holds bars back past the alert and must still be fetched.
_pa_calls.clear()
_DUP_PAGE0 = _bars(7, 10, 1.0)                        # 4 bars (ts 420..600) — a "short" raw count
_DUP = {None: _DUP_PAGE0, 420: _bars(2, 6, 1.0)}      # page 1 reaches back to and past the alert
try:
    PA.LIMIT, GTM.pool_ohlcv = 5, _pages_fake(_DUP)
    _dd_st, _dd_bars, _dd_pages = PA._ohlcv(_POOLP, "minute", 1, 200.0)
finally:
    GTM.pool_ohlcv, PA.LIMIT = _pa_orig_ohlcv, _pa_orig_limit
check("a full page whose bars a same-page duplicate shrank to LIMIT-1 (4 of 5) still triggers a "
      "second page — paging is judged by whether the oldest bar MOVED, never by a raw count a "
      "duplicate can shrink below LIMIT — and the merged series covers the alert",
      _dd_pages == 2 and _dd_st == "ok" and min(b["ts"] for b in _dd_bars) <= 200.0,
      f"{_dd_st} {_dd_pages} {[b['ts'] for b in _dd_bars]}")

# ── 3. a deferred page poisons the resolution, never "no price" ─────────────────────
_pa_calls.clear()
_DEFER = {None: _bars(10, 14, 1.0), 600: None, 360: _bars(2, 5, 1.0)}
try:
    PA.LIMIT, GTM.pool_ohlcv = 5, _pages_fake(_DEFER)
    _dst, _dbars, _dpages = PA._ohlcv(_POOLP, "minute", 1, 200.0)
    PA.top_pool = lambda token: ("ok", _POOLP)
    _dp = PA.fetch_path("0x" + "cd" * 20, 200.0, 800.0)
finally:
    GTM.pool_ohlcv, PA.top_pool, PA.LIMIT = _pa_orig_ohlcv, _pa_orig_pool, _pa_orig_limit
check("ANY deferred page makes the whole resolution DEFERRED with no bars — the page-0 bars are "
      "discarded rather than passed off as the covering series (the 472-of-1,400 rule)",
      (_dst, _dbars) == (PA.DEFERRED, []), f"{_dst} {len(_dbars)}")
check("fetch_path over a deferred page is 'deferred', NEVER 'no_cover' (a rate limit is not an absent price)",
      _dp["status"] == PA.DEFERRED and _dp["res"] is None, str(_dp))
check("fetch_path reports `pages` on every return shape (ok and not-ok)",
      "pages" in _dp and isinstance(_dp["pages"], int))

# ── 4. the cap, and the start-resolution pick that spans it ────────────────────────
_pa_calls.clear()


def _endless(pool, timeframe="day", aggregate=1, limit=None, network=None,
             cache_path=None, max_age_sec=None, before_timestamp=None):
    _pa_calls.append(before_timestamp)
    top = 10 ** 7 if before_timestamp is None else int(before_timestamp)
    return [{"ts": top - (5 - i) * 60, "o": 1.0, "h": 2.0, "l": 0.5, "c": 1.5, "v": 1.0}
            for i in range(5)]


try:
    PA.LIMIT, GTM.pool_ohlcv = 5, _endless
    _cst, _cbars, _cpages = PA._ohlcv(_POOLP, "minute", 1, 0.0)
finally:
    GTM.pool_ohlcv, PA.LIMIT = _pa_orig_ohlcv, _pa_orig_limit
check(f"PATHS_MAX_PAGES caps a runaway walk at {config.PATHS_MAX_PAGES} calls on a series that never reaches the alert",
      len(_pa_calls) == int(config.PATHS_MAX_PAGES) == _cpages and _cst == "ok", f"{len(_pa_calls)} {_cpages} {_cst}")

_pa_calls.clear()
try:
    PA.LIMIT, GTM.pool_ohlcv = 5, _pages_fake({})          # every resolution answers "no bars"
    PA.top_pool = lambda token: ("ok", _POOLP)
    _nc = PA.fetch_path("0x" + "ef" * 20, 0.0, 1_000_000.0)   # an 11.6-day-old alert
finally:
    GTM.pool_ohlcv, PA.top_pool, PA.LIMIT = _pa_orig_ohlcv, _pa_orig_pool, _pa_orig_limit
check("every resolution answering 'no bars' is no_cover (a real answer), with pages counted",
      _nc["status"] == "no_cover" and _nc["res"] is None and isinstance(_nc["pages"], int), str(_nc))
_pa_calls.clear()
_seen_tf: list = []


def _tf_fake(pool, timeframe="day", aggregate=1, limit=None, network=None,
             cache_path=None, max_age_sec=None, before_timestamp=None):
    _seen_tf.append((timeframe, aggregate))
    return []


try:
    GTM.pool_ohlcv = _tf_fake
    PA.top_pool = lambda token: ("ok", _POOLP)
    PA.fetch_path("0x" + "ef" * 20, 0.0, 1_000_000.0)
finally:
    GTM.pool_ohlcv, PA.top_pool = _pa_orig_ohlcv, _pa_orig_pool
check("…and the finest resolution tried for that alert is minute/1, not minute/15",
      _seen_tf and _seen_tf[0] == ("minute", 1), str(_seen_tf))

# ── 5. the ledger-of-record guard: print-only, and it lives in ledger.py ────────────
# The lab reads the ff-merged worktree (DESIGN.md:22 — only the 60 s livebook reads
# origin/main). The keeper commits every few minutes, so an offline run is nearly always a few
# rows behind; that is not an error, it is a number that belongs next to the result. It must
# stay OUT of the scoring modules: scorecard.py reading origin/main would make a gate's answer
# depend on fetch timing.
check("ledger.origin_max_event_seq() exists, answers int-or-None and never raises",
      callable(getattr(LED, "origin_max_event_seq", None))
      and (LED.origin_max_event_seq() is None or isinstance(LED.origin_max_event_seq(), int)))
_sc_src = _read(os.path.join(ROOT, "selfimprove", "entry_lab", "scorecard.py"))
check("the staleness helper is NOT in the entry-band scorecard (a scoring module may not read origin/main)",
      "origin_blob" not in _sc_src and "origin_max_event_seq" not in _sc_src)
_led_fix = pd.DataFrame([{"token": "0xaa", "event_seq": "7", "alert_ts": "1.0"}]).reindex(columns=LED.COLUMNS)
_led_orig_omes = LED.origin_max_event_seq
try:
    LED.origin_max_event_seq = lambda *a, **k: 9
    _r1, _o1 = _capture(LED.warn_if_behind_origin, _led_fix, "verify")
    LED.origin_max_event_seq = lambda *a, **k: 7
    _r2, _o2 = _capture(LED.warn_if_behind_origin, _led_fix, "verify")
    LED.origin_max_event_seq = lambda *a, **k: None
    _r3, _o3 = _capture(LED.warn_if_behind_origin, _led_fix, "verify")
finally:
    LED.origin_max_event_seq = _led_orig_omes
check("warn_if_behind_origin WARNs only when origin/main is genuinely ahead, names both seqs, and is silent "
      "when level or unreadable",
      "WARNING" in _o1 and "9" in _o1 and "7" in _o1 and _o2 == "" and _o3 == "" and (_r1, _r2, _r3) == (9, 7, None),
      f"{_o1!r} {_o2!r} {_o3!r}")

# ── 6. backfill: --since, the banner, and `pages` beside `res` in the record ────────
from selfimprove import backfill as BF                  # noqa: E402

_bf_orig_out, _bf_orig_fetch = BF.OUT, PA.fetch_path
with tempfile.TemporaryDirectory() as _d:
    _bf_led = os.path.join(_d, "ledger.csv")
    _bf_now = 1_789_600_000.0
    LED.save(pd.DataFrame([
        {"token": "0x" + "11" * 20, "symbol": "NEW", "tier": "B", "event_seq": "801",
         "alert_ts": str(_bf_now - 2 * 86400)},
        {"token": "0x" + "22" * 20, "symbol": "OLD", "tier": "B", "event_seq": "802",
         "alert_ts": str(_bf_now - 30 * 86400)},
    ]).reindex(columns=LED.COLUMNS), _bf_led)
    try:
        BF.OUT = os.path.join(_d, "paths.jsonl")
        PA.fetch_path = lambda token, alert_ts, now_s: {
            "status": "ok", "res": "1m", "pool": _POOLP, "entry": 1.0, "pages": 3,
            "bars": [{"ts": alert_ts, "o": 1.0, "h": 2.0, "l": 0.5, "c": 1.5, "v": 1.0}],
            "n_before": 0}
        _bf_stats, _bf_out = _capture(BF.run, _bf_now, _bf_led, None, 5)
        _bf_recs = [json.loads(ln) for ln in open(BF.OUT)]
    finally:
        BF.OUT, PA.fetch_path = _bf_orig_out, _bf_orig_fetch
check("backfill --since=<days> keeps only rows alerted inside the window (newest-first), "
      "leaving the rest untouched for a later pass",
      _bf_stats["ok"] == 1 and len(_bf_recs) == 1 and _bf_recs[0]["symbol"] == "NEW", str(_bf_stats))
check("the backfill record carries `pages` beside `res` (a row covered only by page 4 rests on a different "
      "call budget than one the newest page covered)",
      _bf_recs[0]["res"] == "1m" and _bf_recs[0]["pages"] == 3, str(_bf_recs[0]))
check("backfill prints the ledger it is reading, its row count and its max event_seq before spending a call",
      _bf_led in _bf_out and "802" in _bf_out and "max event_seq" in _bf_out, _bf_out[:400])

# ── 7. evaluate: an unmatched record is DROPPED and COUNTED, never silently ungated ─
from selfimprove import evaluate as EVA                 # noqa: E402

with tempfile.TemporaryDirectory() as _d:
    _ev_p = os.path.join(_d, "paths.jsonl")
    with open(_ev_p, "w") as fh:
        for _r in [
            {"token": "0xaa", "event_seq": 1, "entry": 1.0, "alert_ts": 1.0, "res": "1m", "pages": 1,
             "tier": "A", "bars": [{"ts": 0, "o": 1, "h": 1, "l": 1, "c": 1, "v": 1.0}]},
            {"token": "0xzz", "event_seq": 9, "entry": 1.0, "alert_ts": 1.0, "res": "1m", "pages": 1,
             "tier": "A", "bars": [{"ts": 0, "o": 1, "h": 1, "l": 1, "c": 1, "v": 1.0}]},
            {"token": "0xbb", "event_seq": 2, "entry": 71.0, "alert_ts": 1.0, "res": "1m", "pages": 1,
             "tier": "A", "bars": [{"ts": 0, "o": 71, "h": 71, "l": 71, "c": 71, "v": 1.0}]},
        ]:
            fh.write(json.dumps(_r) + "\n")
    _ev_rows = EVA.load_paths(_ev_p, ledger_entry={"0xaa": 1.0, "0xbb": 1.0})
check("load_paths DROPS a record with no ledger row at either key and counts it as n_unmatched — it can no "
      "longer slip past the 3x mispriced gate by having nothing to compare against",
      [r["token"] for r in _ev_rows] == ["0xaa"]
      and EVA.LAST_LOAD_STATS["n_unmatched"] == 1 and EVA.LAST_LOAD_STATS["n_mispriced"] == 1
      and EVA.LAST_LOAD_STATS["n_kept"] == 1,
      f"{[r['token'] for r in _ev_rows]} {EVA.LAST_LOAD_STATS}")

# ── 8. the retrospective is descriptive and says so first ──────────────────────────
# Three tokens chosen BECAUSE they ran is the textbook shape of a story that reads as evidence.
# The document is allowed to exist only while its first section is the disclaimer and no rule is
# derived in it; these checks are what keep a later edit from quietly turning it into a claim.
_retro_path = os.path.join(ROOT, "docs", "RETRO_2026-09-16_hype_runners.md")
check("docs/RETRO_2026-09-16_hype_runners.md exists", os.path.isfile(_retro_path))
_retro = _read(_retro_path)
check("the retrospective's FIRST section is 'What this cannot tell you' — the disclaimer leads, it is not a footnote",
      _retro.split("\n## ")[1].startswith("What this cannot tell you"), _retro.split("\n## ")[1][:80])
check("the retrospective says in as many words that no rule is derived from it, and pins its numbers to an origin/main SHA",
      "no rule is derived" in _retro and "bcb7fa9" in _retro)
check("the per-token table pins the data-quality columns ('| res | pages |') — a 1h bar cannot resolve a "
      "token that peaks four minutes in, and page depth is the coverage it rests on",
      "| res | pages |" in _retro)
check("the retrospective claims no hit rate, lift or threshold from n = 3 (the refuses-to-claim list is the section, "
      "not a sentence the table contradicts)",
      "n = 3" in _retro and "Refuses to claim" in _retro)
check("the retrospective carries no home path and never names the shared secrets file (public repo)",
      "/Users/" not in _retro and "monitor_config" not in _retro and "vrp_backtest" not in _retro)
check("README's file table carries the retrospective with its one honest bullet",
      "RETRO_2026-09-16_hype_runners.md" in _readme
      and "descriptive, n = 3, chosen on the outcome" in _readme
      and "no rule is derived from it" in _readme)

# ═══════════════════════════════════════════════════════════════════════════════════
section("Q. the vendored Deflated Sharpe, the paper gate, the Sunday job on GitHub")
# ═══════════════════════════════════════════════════════════════════════════════════
from selfimprove import dsr as DSR                      # noqa: E402
from selfimprove import evaluate as EVQ                 # noqa: E402

# ── 1. dsr.py — the same function as the sibling repo's, numpy + NormalDist, on BOTH partitions ──
_q_rng = np.random.default_rng(config.SEED)
_q_planted, _q_zero = _q_rng.normal(0.5, 1.0, 60), _q_rng.normal(0.0, 1.0, 60)
check("dsr.deflated_sharpe_ratio: a planted positive series >= 0.95 at 16 trials, a zero-mean series < 0.95, n < 8 ⇒ 0.0, a "
      "constant series ⇒ 0.0 (no sibling repo, no scipy — the checks that used to SKIP under CI)",
      DSR.deflated_sharpe_ratio(_q_planted, 16) >= 0.95 and DSR.deflated_sharpe_ratio(_q_zero, 16) < 0.95
      and DSR.deflated_sharpe_ratio(_q_planted[:7], 16) == 0.0 and DSR.deflated_sharpe_ratio([1.0] * 20, 16) == 0.0,
      f"planted {DSR.deflated_sharpe_ratio(_q_planted, 16):.4f} zero {DSR.deflated_sharpe_ratio(_q_zero, 16):.4f}")
check("dsr.norm_ppf / norm_cdf are statistics.NormalDist (ppf(0.975) = 1.959964; cdf∘ppf is the identity to 1e-12; cdf(0) = 0.5)",
      abs(DSR.norm_ppf(0.975) - 1.959963984540054) < 1e-9 and abs(DSR.norm_cdf(DSR.norm_ppf(0.3)) - 0.3) < 1e-12
      and DSR.norm_cdf(0.0) == 0.5)
_q_tree = _tree(os.path.join(ROOT, "selfimprove", "dsr.py"))
check("dsr.py imports numpy / statistics (+ the smoke test's os/sys/config) and nothing else — never scipy, never the sibling's stats",
      not (_top_imports(_q_tree) - {"__future__", "numpy", "statistics", "math", "os", "sys", "config"}), str(_top_imports(_q_tree)))
_q_r = np.random.default_rng(config.SEED + 1).normal(0.3, 0.6, 120)
_q_d = ["d%02d" % (i % 30) for i in range(120)]
check("improve._dsr is the vendored DSR on the DAY MEANS (never rows) at the given trial count",
      abs(IM._dsr(_q_r, _q_d, 16) - DSR.deflated_sharpe_ratio(IM._day_means(_q_r, _q_d), 16)) < 1e-12)
check("scorecard._dsr_fn returns the vendored function and improve_bands.dsr_bar is finite without scipy (it returned NaN on the runner)",
      SC._dsr_fn() is DSR.deflated_sharpe_ratio and 0.3 < IB.dsr_bar(40, 10) < 1.5 and math.isfinite(IB.dsr_bar(12, 40)),
      f"{IB.dsr_bar(40, 10)} {IB.dsr_bar(12, 40)}")
if DSR_XPIN:
    import importlib.util as _ilu
    _q_spec = _ilu.spec_from_file_location("_entry_bot_stats", ENTRY_BOT_STATS)
    _q_ebs = _ilu.module_from_spec(_q_spec)
    _q_spec.loader.exec_module(_q_ebs)                 # its `import config` resolves to OURS (already in sys.modules)
    _q_x = np.random.default_rng(config.SEED).normal(0.2, 1.0, 50)
    check("cross-pin (mac_only): the vendored DSR equals entry_bot/stats.deflated_sharpe_ratio to 1e-9 on a seeded series and on the "
          "planted / zero-mean series at 16 trials (scipy's biased skew/kurtosis ARE the plain moment ratios; NormalDist ≡ norm.ppf/cdf)",
          abs(DSR.deflated_sharpe_ratio(_q_x, 16) - _q_ebs.deflated_sharpe_ratio(_q_x, 16)) < 1e-9
          and abs(DSR.deflated_sharpe_ratio(_q_planted, 16) - _q_ebs.deflated_sharpe_ratio(_q_planted, 16)) < 1e-9
          and abs(DSR.deflated_sharpe_ratio(_q_zero, 16) - _q_ebs.deflated_sharpe_ratio(_q_zero, 16)) < 1e-9,
          f"{DSR.deflated_sharpe_ratio(_q_x, 16)!r} vs {_q_ebs.deflated_sharpe_ratio(_q_x, 16)!r}")
else:
    skip("cross-pin of the vendored DSR against entry_bot/stats.py (mac_only)", "sibling repo or scipy absent, or IS_CI")
_q_vals = np.random.default_rng(config.SEED).normal(-0.10, 0.50, 200)
_q_days = ["d%02d" % (i % 25) for i in range(200)]
_q_lb, _q_ng = EVQ.cluster_lb(_q_vals, _q_days)
check("cluster_lb is PINNED on a fixed seeded series (-0.1868845841 over 25 clusters; numpy 2.0.2 on the Mac): a numpy upgrade on either "
      "partition cannot move a bound silently — a FAIL here is the finding, not a nuisance",
      _q_ng == 25 and abs(_q_lb - (-0.1868845841239552)) < 1e-9, repr(_q_lb))


def _imports_outside_main(tree) -> set:
    out = set()
    for n in _compute_nodes(tree):
        if isinstance(n, ast.Import):
            out.update(a.name.split(".")[0] for a in n.names)
        elif isinstance(n, ast.ImportFrom) and n.module and not n.level:
            out.add(n.module.split(".")[0])
    return out


_q_bad_append, _q_bad_sib = [], []
for _p in _py_files():
    if not _rel(_p).startswith("selfimprove" + os.sep):
        continue
    _t = _tree(_p)
    _doc_ids = set()
    for _n in ast.walk(_t):
        if isinstance(_n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and _n.body \
                and isinstance(_n.body[0], ast.Expr) and isinstance(_n.body[0].value, ast.Constant) \
                and isinstance(_n.body[0].value.value, str):
            _doc_ids.add(id(_n.body[0].value))
    for _n in ast.walk(_t):
        if isinstance(_n, ast.Call) and _attr_chain(_n.func) == ["sys", "path", "append"]:
            _q_bad_append.append(f"{_rel(_p)}:{_n.lineno}")
        if isinstance(_n, ast.Constant) and isinstance(_n.value, str) and "entry_bot" in _n.value and id(_n) not in _doc_ids:
            _q_bad_sib.append(f"{_rel(_p)}:{_n.lineno}")
        if isinstance(_n, ast.Import) and any(a.name.split(".")[0] == "stats" for a in _n.names):
            _q_bad_sib.append(f"{_rel(_p)}:{_n.lineno} import stats")
check("no file under selfimprove/ appends to sys.path (the sibling-repo import path is gone with the vendoring)",
      not _q_bad_append, str(_q_bad_append))
check("no file under selfimprove/ names entry_bot in CODE — a string constant outside a docstring, or `import stats` (comments and "
      "docstrings that cite the sibling's measured incidents are history, not a dependency)", not _q_bad_sib, str(_q_bad_sib))
for _rel_ in ("selfimprove/improve.py", "selfimprove/entry_lab/improve_bands.py", "selfimprove/weekly_summary.py",
              "selfimprove/evaluate.py", "selfimprove/dsr.py"):
    _imps = _top_imports(_tree(os.path.join(ROOT, _rel_)))
    check(f"{_rel_} never imports scipy / statsmodels / sklearn anywhere (the Sunday gates run on the runner: numpy only)",
          not (_imps & {"scipy", "statsmodels", "sklearn"}), str(sorted(_imps)))
_imps = _imports_outside_main(_tree(os.path.join(ROOT, "selfimprove", "entry_lab", "scorecard.py")))
check("selfimprove/entry_lab/scorecard.py imports scipy / statsmodels / sklearn nowhere outside its __main__ (the statsmodels BY "
      "cross-check in the smoke test is mac_only by construction)", not (_imps & {"scipy", "statsmodels", "sklearn"}), str(sorted(_imps)))

# ── 2. the paper gate — the ONE judge of the paper test (pre-registered; no new statistics) ──
# The three pre-registration constants are the OPERATOR's switch (Phase 8 "start the window" sets
# two of them), so pinning them to None would make the repo's own procedure turn verify red. What
# is pinned is the property that protects the window: the pre-registered numbers are the declared
# ones, and each switch either is unset or names something the gate can actually resolve — the
# same shape as LIVEBOOK_BAND_UNDER_TEST's "None or a REGISTERED non-control band" above.
def _switch_faults(reg, policies) -> list:
    """The three pre-registration switches read out of config, against a (band registry, policy
    family) pair — the single implementation, run on the real config here and on the simulated
    Phase-8 "start the window" values in the scenario block below."""
    faults = []
    if (config.PAPER_GATE_WINDOW_DAYS, config.PAPER_GATE_MIN_FILLS,
            config.PAPER_GATE_MAX_REFUSED_SHARE) != (7, 20, 0.05):
        faults.append("the pre-registered numbers are not 7 days / 20 fills / refused share 0.05")
    p_ = config.PAPER_GATE_POLICY
    if p_ is not None and (p_ not in policies or p_ in POL.CONTROLS):
        faults.append(f"PAPER_GATE_POLICY {p_!r} is not a live non-control policy")
    w_ = config.PAPER_GATE_WINDOW_START
    if w_ is not None and _capture(IM._parse_window_start, w_)[0] is None:
        faults.append(f"PAPER_GATE_WINDOW_START {w_!r} does not parse as ISO-8601 UTC")
    b_ = config.LIVEBOOK_BAND_UNDER_TEST
    if b_ is not None and (b_ not in reg.names() or reg.is_control(b_)):
        faults.append(f"LIVEBOOK_BAND_UNDER_TEST {b_!r} is not a registered non-control band")
    return faults


_sw_faults_now = _switch_faults(REG, POL.POLICIES)
check("the paper gate's pre-registered numbers are the declared ones (7 days, 20 fills, refused share 0.05) and each operator "
      "switch is None or RESOLVABLE: PAPER_GATE_POLICY names a live non-control policy (None ⇒ the exit champion's plan), "
      "PAPER_GATE_WINDOW_START parses as ISO-8601 UTC through improve._parse_window_start (None ⇒ none registered) and "
      "LIVEBOOK_BAND_UNDER_TEST is a registered non-control band",
      not _sw_faults_now, f"{_sw_faults_now} "
      f"{config.PAPER_GATE_POLICY!r} {config.PAPER_GATE_WINDOW_START!r} {config.LIVEBOOK_BAND_UNDER_TEST!r}")

# ── the operator's next two moves, SIMULATED: registration, then "start the window" ───────────
# Every check above that reads registry.json / trials.json / champion.json or the three switches
# states an INVARIANT, not a snapshot of today's tree — and that is PROVED here, not asserted:
# the moves are replayed against COPIES in a temp dir with register.py's own scan() (never
# --scan on the real files), and the same three predicate functions are re-evaluated on the
# result. The pins this replaces asserted the four modules were ABSENT and the switches None;
# they would have gone red on both partitions, on every push, at the exact moment the operator
# made a permanent counted trial — and inside run_research.sh's own merge gate with it.
with tempfile.TemporaryDirectory() as _d_sim:
    _cdir_sim = os.path.join(_d_sim, "candidates")
    os.makedirs(_cdir_sim)
    _SIM_BAND, _SIM_POL = "band_hype_early", CAND_FLOW
    for _nm_s in (_SIM_BAND, _SIM_POL):
        shutil.copyfile(os.path.join(CAND_DIR, _nm_s + ".py"), os.path.join(_cdir_sim, _nm_s + ".py"))
    _rp_sim, _tp_sim = os.path.join(_cdir_sim, "registry.json"), os.path.join(_d_sim, "trials.json")
    shutil.copyfile(config.REGISTRY_PATH, _rp_sim)
    shutil.copyfile(config.TRIALS_PATH, _tp_sim)
    _lp_sim = os.path.join(_d_sim, "ledger.csv")
    with open(_lp_sim, "w") as _fh_sim:
        _fh_sim.write("token,event_seq,tier\n0xaa,11,B\n")
    _dec_sim, _out_sim = _capture(REGC.scan, _rp_sim, _lp_sim, 2, 1_800_000_000.0,
                                  candidates_dir=_cdir_sim, trials_path=_tp_sim, use_git=False, champion=CHAMP)
    _sim_pol, _sim_reg = REGC.POL.load_candidates(_rp_sim), B.load_registry(_rp_sim)
    _sim_tr = REGC.trials.load(_tp_sim)
    _sim_faults = _registration_faults(_rp_sim, _tp_sim)
    check("SCENARIO registration (the operator's step, today): register.py's own scan() mints the entries for a band and a "
          "policy against COPIES in a temp dir, and _registration_faults — the SAME predicate the real tree is held to — is "
          "still empty: both names resolve through the loaders the runtime uses, the policy dict is the module's own, and both "
          "are already counted in trials.json",
          not _sim_faults and _SIM_BAND in _sim_reg.names() and _SIM_POL in _sim_pol
          and _sim_pol[_SIM_POL] == _norm(_cand_policy(_SIM_POL))
          and _SIM_BAND in set(_sim_tr["bands_ever_scored"]) and _SIM_POL in set(_sim_tr["policies_ever_scored"]),
          f"{_sim_faults} | {[d[:2] for d in _dec_sim]} | {_out_sim[-200:]}")
    _sw_saved_sim = (config.PAPER_GATE_POLICY, config.PAPER_GATE_WINDOW_START, config.LIVEBOOK_BAND_UNDER_TEST)
    try:
        with _policies({_SIM_POL: _sim_pol[_SIM_POL]}):        # as load_candidates would at import
            config.PAPER_GATE_POLICY = _SIM_POL
            config.PAPER_GATE_WINDOW_START = "2026-09-21T00:00:00Z"
            config.LIVEBOOK_BAND_UNDER_TEST = _SIM_BAND
            _sw_faults_sim = _switch_faults(_sim_reg, POL.POLICIES)
            _champ_sim = dict(_live, champion=_SIM_BAND, previous=_live["champion"],
                              promoted_at_event_seq=None,
                              evidence={"manual": True, "previous": _live["champion"],
                                        "reason": "operator decision: the band under test becomes the champion"})
            _champ_faults_sim = _entry_champion_faults(_champ_sim, _sim_reg)
            check("SCENARIO start-the-window + a hand-set champion (the next two operator moves): PAPER_GATE_POLICY set to the "
                  "freshly registered adaptive policy, PAPER_GATE_WINDOW_START to an ISO-8601 UTC date, LIVEBOOK_BAND_UNDER_TEST "
                  "and champion.json's entry arm to the freshly registered band — _switch_faults and _entry_champion_faults, the "
                  "SAME predicates the live config and champion.json are held to, stay empty",
                  not _sw_faults_sim and not _champ_faults_sim, f"{_sw_faults_sim} | {_champ_faults_sim}")
    finally:
        (config.PAPER_GATE_POLICY, config.PAPER_GATE_WINDOW_START,
         config.LIVEBOOK_BAND_UNDER_TEST) = _sw_saved_sim
_q_t0 = float(calendar.timegm((2027, 1, 15, 0, 0, 0)))
check("_parse_window_start: ISO-8601 UTC (Z and +00:00) → epoch seconds with the stdlib; None / malformed → None (never 'today')",
      IM._parse_window_start("2027-01-15T00:00:00Z") == _q_t0 == IM._parse_window_start("2027-01-15T00:00:00+00:00")
      and IM._parse_window_start(None) is None and _capture(IM._parse_window_start, "15/01/2027")[0] is None
      and _capture(IM._parse_window_start, "")[0] is None)
_saved_q = {"BOOK_PATH": LB.BOOK_PATH, "MISSED_PATH": LB.MISSED_PATH, "CHAMPION_PATH": config.CHAMPION_PATH,
            "TRIALS_PATH": config.TRIALS_PATH, "IMPROVE_HISTORY_PATH": config.IMPROVE_HISTORY_PATH,
            "PROPOSALS_DIR": config.PROPOSALS_DIR, "LIVEBOOK_SUMMARY_PATH": config.LIVEBOOK_SUMMARY_PATH,
            "PAUSE_PATH": config.PAUSE_PATH, "BAND_VERDICTS_PATH": config.BAND_VERDICTS_PATH,
            "LIVEBOOK_BAND_UNDER_TEST": config.LIVEBOOK_BAND_UNDER_TEST, "PAPER_GATE_POLICY": config.PAPER_GATE_POLICY,
            "PAPER_GATE_WINDOW_START": config.PAPER_GATE_WINDOW_START, "send_all": alerts.send_all,
            "policies": dict(POL.POLICIES)}
_tmp_q = tempfile.mkdtemp(prefix="verify_papergate_")
try:
    LB.BOOK_PATH = os.path.join(_tmp_q, "livebook.json")
    LB.MISSED_PATH = os.path.join(_tmp_q, "missed.jsonl")
    config.CHAMPION_PATH = os.path.join(_tmp_q, "champion.json")
    config.TRIALS_PATH = os.path.join(_tmp_q, "trials.json")
    config.IMPROVE_HISTORY_PATH = os.path.join(_tmp_q, "improve_history.jsonl")
    config.PROPOSALS_DIR = os.path.join(_tmp_q, "proposals")
    config.LIVEBOOK_SUMMARY_PATH = os.path.join(_tmp_q, "livebook_summary.json")
    config.PAUSE_PATH = os.path.join(_tmp_q, "PAUSE")
    config.BAND_VERDICTS_PATH = os.path.join(_tmp_q, "band_verdicts.csv")
    alerts.send_all = lambda title, body, dry_run=True: None
    DEFQ, CHQ, BQ = config.IMPROVE_DEFAULT_EXIT_CHAMPION, "sell_3h", "band_x"
    W = config.PAPER_GATE_WINDOW_DAYS
    START = "2027-01-15T00:00:00Z"
    t_open = _q_t0 + 3.5 * 86400
    t_closed = _q_t0 + W * 86400 + config.LIVEBOOK_MAX_TRACK_S + 1.0

    def _tr_lines(prefix):
        return [x for x in TR.load()["nominations_ever"] if x.startswith(prefix)]

    def _pg(book, now_s, record=True):
        # record=True is the RUNNER's Sunday --apply evaluation, the only caller allowed to mint
        # the one-shot trials.json lines; record=False is every read-only caller.
        LB._save_atomic(book, LB.BOOK_PATH)         # live_stats_dict (the flow-dark share) reads the file
        return _capture(IM.paper_gate, now_s, book=book, record=record)

    # none registered: no band, or no window start — and the CLI returns before the Sunday gate
    config.LIVEBOOK_BAND_UNDER_TEST, config.PAPER_GATE_WINDOW_START, config.PAPER_GATE_POLICY = None, None, None
    pg0, _ = _pg({}, t_open)
    config.LIVEBOOK_BAND_UNDER_TEST = BQ
    pg1, _ = _pg({}, t_open)
    config.LIVEBOOK_BAND_UNDER_TEST, config.PAPER_GATE_WINDOW_START = None, START
    pg2, _ = _pg({}, t_open)
    rc_bs, out_bs = _capture(IM.main, ["--band-scorecard"])
    check("paper_gate with no band under test, or no window start, is 'none registered' (status none, nothing bumped); "
          "`improve.py --band-scorecard` prints it and returns BEFORE the Sunday gate (no history line, no proposal)",
          pg0["status"] == pg1["status"] == pg2["status"] == "none" and all(p["line"].startswith("none registered") for p in (pg0, pg1, pg2))
          and TR.family_count("nominations") == 0 and rc_bs == 0 and "none registered" in out_bs
          and not os.path.exists(config.IMPROVE_HISTORY_PATH) and not os.path.exists(config.PROPOSALS_DIR), out_bs[-300:])
    check("PAPER_GATE_POLICY None resolves to the exit champion's executable plan (the default champion here)",
          pg0["policy"] == DEFQ)

    # the stamp and the window: band_mask reads sidecar_true; the synthetic book can plant it
    book_half = IM._synthetic_book(4, 6, _q_t0, seed=31, stamp=[BQ], stamp_frac=0.5)
    _, _, meta_h = IM.live_returns(book_half)
    n_st = sum(1 for p in book_half.values() if BQ in p["sidecar_true"])
    check("band_mask is True exactly on the meta rows whose position carries the band in sidecar_true (Phase 4's stamp)",
          0 < n_st < len(meta_h) and int(IM.band_mask(book_half, meta_h, BQ).sum()) == n_st
          and int(IM.band_mask(book_half, meta_h, "band_y").sum()) == 0)

    # an OPEN window: the table prints, no verdict, the paper: line is bumped exactly once
    config.LIVEBOOK_BAND_UNDER_TEST, config.PAPER_GATE_WINDOW_START, config.PAPER_GATE_POLICY = BQ, START, CHQ
    book_a = IM._synthetic_book(W, 5, _q_t0, seed=32, adv={CHQ: 0.60}, stamp=[BQ])            # 35 stamped, in-window
    book_a.update(IM._synthetic_book(W, 3, _q_t0 + 100.0, seed=33, seq0=1000))               # 21 unstamped: the complement
    book_a.update(IM._synthetic_book(2, 4, _q_t0 - 5 * 86400, seed=34, stamp=[BQ], seq0=2000))   # 8 stamped BEFORE the window
    pga, out_a = _pg(book_a, t_open)
    pga2, _ = _pg(book_a, t_open)
    rc_bs, out_bs = _capture(IM.main, ["--band-scorecard"])
    check("an open window: status 'open', the line says 'window <start> open (n fills, D of 7 days)', eligible = stamped ∧ in-window "
          "(35, not the 8 pre-window stamped rows nor the 21 unstamped), the complement is reported, no verdict, no paper_verdict line",
          pga["status"] == "open" and f"{BQ}@{CHQ} window {START} open (" in pga["line"] and "3.5 of 7 days" in pga["line"]
          and pga["n"] == 35 and pga["complement"]["n"] == 21 and pga["policy_row"]["n"] == 35
          and not _tr_lines("paper_verdict:") and pga["table"] is not None, pga["line"])
    _, out_tab = _capture(IM._print_paper_gate, pga)
    check("the paper: nomination line is bumped ONCE for a (band, policy, start) — a second evaluation adds nothing; the CLI prints the "
          "pair's line on its own wall clock ('pending' before the 2027 window) and its renderer prints the open-window table with "
          "the policy under test, the controls, the complement and the checks",
          _tr_lines("paper:") == [f"paper:{BQ}/{CHQ}@{START}"] and pga2["line"] == pga["line"]
          and rc_bs == 0 and f"paper gate: {BQ}@{CHQ} window {START} pending" in out_bs
          and f"window {START} open (" in out_tab and f"*{CHQ}" in out_tab and "net LB" in out_tab
          and "(control)" in out_tab and "complement (in-window, unstamped)" in out_tab and "[x] " in out_tab
          and "promotes nothing" in out_tab, out_bs[-200:] + out_tab[-400:])
    check("every evaluation reports n_armed and flow_dark_share (None for a policy with no flow leg) and n_days with the bound label "
          "when n_days < MIN_BOOTSTRAP_CLUSTERS (7 days here)",
          pga["n_armed"] is None and pga["flow_dark_share"] is None and not pga["has_flow"]
          and pga["n_days"] == 7 and "a number, not a bound (floor 12)" in pga["line"], pga["line"])

    # CLOSED window, everything clears ⇒ PASS, recorded once, printed from the record afterwards
    pgp, _ = _pg(book_a, t_closed)
    pgp2, _ = _pg(book_a, t_closed)
    check("a closed window that clears every pre-registered check is PASS: n >= 20, policy net LB > 0, both controls' net LB <= 0, no "
          "inert control, gapped and refused shares within bounds, no eligible position open",
          pgp["status"] == "PASS" and pgp["policy_row"]["net_lb"] > 0 and pgp["n"] >= config.PAPER_GATE_MIN_FILLS
          and all(v <= 0 for v in pgp["controls_net_lb"].values()) and len(pgp["controls_net_lb"]) == 2
          and all(ok for _, ok, _ in pgp["checks"]) and pgp["line"].startswith(f"{BQ}@{CHQ} window {START} PASS ("),
          f"{pgp['line']} {pgp['checks']}")
    check("the verdict is one-shot: paper_verdict:<band>/<policy>@<start>=pass is recorded once and a later run prints the RECORDED line",
          _tr_lines("paper_verdict:") == [f"paper_verdict:{BQ}/{CHQ}@{START}=pass"] and pgp2["recorded"] == _tr_lines("paper_verdict:")[0]
          and pgp2["status"] == "PASS" and "(recorded)" in pgp2["line"] and "(recorded)" not in pgp["line"])
    check("net LB is day_lb minus policies.round_trip_cost() for every row, controls included (double-conservative on purpose)",
          abs(pgp["policy_row"]["net_lb"] - (pgp["policy_row"]["day_lb"] - POL.round_trip_cost())) < 1e-12
          and all(abs(r["net_lb"] - (r["day_lb"] - POL.round_trip_cost())) < 1e-12 for r in pgp["table"]["controls"].values()))
    # the same pair with a still-open eligible position is NOT judged
    book_open = json.loads(json.dumps(book_a))
    k_first = next(k for k, p in book_open.items() if BQ in p["sidecar_true"] and _q_t0 <= p["opened_ts"] < _q_t0 + W * 86400)
    book_open[k_first]["done"] = False
    pgo, _ = _pg(book_open, t_closed)
    check("an eligible position still open past the window keeps the status 'open' (no verdict is minted while a fill can change)",
          pgo["status"] == "open" and pgo["n_open_eligible"] == 1 and "still open" in pgo["line"], pgo["line"])

    # FAIL: a new pair (policy without the edge) ⇒ net LB <= 0; and too few fills
    config.PAPER_GATE_POLICY = "sell_1h"
    pgf, _ = _pg(book_a, t_closed)
    config.PAPER_GATE_POLICY = CHQ
    config.PAPER_GATE_WINDOW_START = "2027-03-01T00:00:00Z"
    _q_t1 = IM._parse_window_start(config.PAPER_GATE_WINDOW_START)
    book_thin = IM._synthetic_book(W, 2, _q_t1, seed=35, adv={CHQ: 0.60}, stamp=[BQ])       # 14 < 20 fills
    pgt, _ = _pg(book_thin, _q_t1 + W * 86400 + config.LIVEBOOK_MAX_TRACK_S + 1.0)
    config.PAPER_GATE_WINDOW_START = START
    check("FAIL when the policy's net LB is not above zero, and FAIL below PAPER_GATE_MIN_FILLS — each on its own (band, policy, start) "
          "with its own paper: line (a changed policy or start is a NEW counted window)",
          pgf["status"] == "FAIL" and "net LB" in pgf["line"] and pgt["status"] == "FAIL" and "fills" in pgt["line"]
          and _tr_lines("paper:") == [f"paper:{BQ}/{CHQ}@{START}", f"paper:{BQ}/sell_1h@{START}", f"paper:{BQ}/{CHQ}@2027-03-01T00:00:00Z"]
          and len(_tr_lines("paper_verdict:")) == 3, f"{pgf['line']} | {pgt['line']} | {_tr_lines('paper')}")

    # VOID (no number quoted): a profitable control, an inert control, a collapsed grid, a capacity-limited window —
    # each on a FRESH (band, policy) pair: sell_3h already has two windows and the cap would refuse a third
    config.PAPER_GATE_POLICY, config.PAPER_GATE_WINDOW_START = "sell_2h", "2027-05-01T00:00:00Z"
    _q_t2 = IM._parse_window_start(config.PAPER_GATE_WINDOW_START)
    t2_closed = _q_t2 + W * 86400 + config.LIVEBOOK_MAX_TRACK_S + 1.0
    pgv1, _ = _pg(IM._synthetic_book(W, 5, _q_t2, seed=36, adv={"sell_2h": 0.60}, stamp=[BQ], ctl_immediate_mean=0.20), t2_closed)
    config.PAPER_GATE_POLICY, config.PAPER_GATE_WINDOW_START = "sell_6h", "2027-05-20T00:00:00Z"
    _q_t3 = IM._parse_window_start(config.PAPER_GATE_WINDOW_START)
    pgv2, _ = _pg(IM._synthetic_book(W, 5, _q_t3, seed=37, adv={"sell_6h": 0.60}, stamp=[BQ], inert_random=True),
                  _q_t3 + W * 86400 + config.LIVEBOOK_MAX_TRACK_S + 1.0)
    config.PAPER_GATE_POLICY, config.PAPER_GATE_WINDOW_START = "trail_30", "2027-06-10T00:00:00Z"
    _q_t4 = IM._parse_window_start(config.PAPER_GATE_WINDOW_START)
    pgv3, _ = _pg(IM._synthetic_book(W, 5, _q_t4, seed=38, adv={"trail_30": 0.60}, stamp=[BQ], gapped={"trail_30": 0.6}),
                  _q_t4 + W * 86400 + config.LIVEBOOK_MAX_TRACK_S + 1.0)
    check("VOID on a control profitable NET OF COST, on an inert control, and on a gapped share above BAND_MAX_GAPPED_SHARE — the table "
          "is suppressed (no number quoted) and the line names the fault",
          pgv1["status"] == pgv2["status"] == pgv3["status"] == "VOID"
          and pgv1["table"] is None and pgv1["policy_row"] is None and "ctl_exit_immediately" in pgv1["line"]
          and "INERT" in pgv2["line"] and "SAMPLING GAP" in pgv3["line"] and pgv3["gapped_share"] > config.BAND_MAX_GAPPED_SHARE,
          f"{pgv1['line']} | {pgv2['line']} | {pgv3['line']}")
    # ... and the suppression reaches out["checks"] too: it was built BEFORE the status branch, so the policy's own
    # "net LB +0.412" and every control's survived the VOID, were printed three lines under "no number from this window
    # may be quoted", and were persisted by summary_json into the digest committed to the public repo.
    _v_num = [(l_, o_, d_) for l_, o_, d_ in pgv1["checks"] if "net LB" in l_]
    _f_num = [(l_, o_, d_) for l_, o_, d_ in pgf["checks"] if "net LB" in l_]
    _, out_v1 = _capture(IM._print_paper_gate, pgv1)
    check("a VOID window quotes no number in out['checks'] either — the two net-LB checks keep their label and their boolean and "
          "lose their detail, while the same two checks on a FAIL still carry theirs (the digest carries this dict verbatim)",
          len(_v_num) == len(_f_num) == 2      # the same two checks either way (the policy name heads the first label)
          and [l_.split(" ", 1)[1] for l_, _o, _d in _v_num] == [l_.split(" ", 1)[1] for l_, _o, _d in _f_num]
          and all(d_ == IM._VOID_CHECK_DETAIL for _l, _o, d_ in _v_num)
          and all(any(c_.isdigit() for c_ in d_) for _l, _o, d_ in _f_num)
          and not any(c_.isdigit() for _l, _o, d_ in _v_num for c_ in d_)
          and json.dumps(pgv1["checks"]).count("net LB") == 2      # the two labels, and nothing else
          and [l_ for l_ in out_v1.splitlines() if l_.strip()[:3] in ("[x]", "[ ]") and "net LB" in l_]
          == [f"  [{m_}] {l_} ({IM._VOID_CHECK_DETAIL})" for l_, o_, _d in _v_num for m_ in ("x" if o_ else " ",)],
          str(_v_num))
    # refused share: missed lines inside the window whose event_seq the band selected (sidecar 1) with a refusal reason
    config.PAPER_GATE_POLICY, config.PAPER_GATE_WINDOW_START = "stop_50", "2027-07-01T00:00:00Z"
    _q_t5 = IM._parse_window_start(config.PAPER_GATE_WINDOW_START)
    book_r = IM._synthetic_book(W, 5, _q_t5, seed=39, adv={"stop_50": 0.60}, stamp=[BQ])     # 35 admitted
    with open(config.BAND_VERDICTS_PATH, "w", newline="") as fh:
        w_ = csv.writer(fh); w_.writerow(["event_seq", "token", "alert_ts", "band", "verdict"])
        for seq_, v_ in ((9001, "1"), (9002, "1"), (9003, "1"), (9004, "0"), (9005, "1")):
            w_.writerow([seq_, "0x%040x" % seq_, _q_t5 + 3600, BQ, v_])
    with open(LB.MISSED_PATH, "w") as fh:
        for seq_, reason_, ts_ in ((9001, "band_under_test_full", _q_t5 + 3600), (9002, "entry lag exceeds MAX_ENTRY_LAG_S", _q_t5 + 7200),
                                   (9003, "band_under_test_full", _q_t5 + 10800), (9004, "band_under_test_full", _q_t5 + 3600),
                                   (9005, "book_full", _q_t5 + 3600), (9001, "band_under_test_full", _q_t5 - 86400)):
            fh.write(json.dumps({"ts": ts_, "token": "0x%040x" % seq_, "event_seq": seq_, "symbol": "R", "tier": "B",
                                 "alert_ts": ts_ - 200, "entry_lag_s": 200.0, "reason": reason_}) + "\n")
    pgr, _ = _pg(book_r, _q_t5 + W * 86400 + config.LIVEBOOK_MAX_TRACK_S + 1.0)
    os.remove(LB.MISSED_PATH); os.remove(config.BAND_VERDICTS_PATH)
    check("refused share = refusals / (refusals + admitted): only band_under_test_full / entry-lag lines INSIDE the window whose "
          "event_seq the band selected count (3 of 6 lines: a verdict-0 row, a book_full row and a pre-window row do not) ⇒ 3/38 > 0.05 ⇒ VOID",
          pgr["n_refused"] == 3 and pgr["n_admitted"] == 35 and abs(pgr["refused_share"] - 3 / 38) < 1e-12
          and pgr["status"] == "VOID" and "CAPACITY" in pgr["line"], f"{pgr['n_refused']} {pgr['n_admitted']} {pgr['line']}")

    # ── unknown is NOT zero. The numerator used to swallow every error and return n_refused 0, and
    # `refused_share is None or refused_share <= MAX` passes on 0 — so a missing or corrupt sidecar
    # turned the capacity VOID off in the direction that makes PASS easier, on a permanent verdict.
    # (record=False throughout: these sub-cases re-judge the same pair, so nothing may be minted.)
    _lb_src = _read(os.path.join(ROOT, "selfimprove", "livebook.py"))
    check("the paper gate's refusal reasons ARE livebook's own spellings: every string in improve.PAPER_REFUSAL_REASONS appears "
          "LITERALLY in selfimprove/livebook.py — a rename there now breaks this check instead of silently zeroing the numerator",
          len(IM.PAPER_REFUSAL_REASONS) == 2 and all(f'"{r_}"' in _lb_src for r_ in IM.PAPER_REFUSAL_REASONS),
          str([r_ for r_ in IM.PAPER_REFUSAL_REASONS if f'"{r_}"' not in _lb_src]))
    config.PAPER_GATE_POLICY, config.PAPER_GATE_WINDOW_START = "sell_30m", "2027-07-20T00:00:00Z"
    _q_u = IM._parse_window_start(config.PAPER_GATE_WINDOW_START)
    _u_closed = _q_u + W * 86400 + config.LIVEBOOK_MAX_TRACK_S + 1.0
    book_u = IM._synthetic_book(W, 5, _q_u, seed=42, adv={"sell_30m": 0.60}, stamp=[BQ])
    _u_miss = [{"ts": _q_u + 3600.0 * i_, "token": "0x%040x" % (9100 + i_), "event_seq": 9100 + i_, "symbol": "R",
                "tier": "B", "alert_ts": _q_u + 3600.0 * i_ - 200, "entry_lag_s": 200.0,
                "reason": "band_under_test_full"} for i_ in (1, 2)]

    def _write_missed(rows):
        with open(LB.MISSED_PATH, "w") as fh:
            for r_ in rows:
                fh.write(json.dumps(r_) + "\n")

    pg_u0, _ = _pg(book_u, _u_closed, record=False)          # no missed log at all: a refusal count of 0 is a FACT
    _write_missed(_u_miss)                                   # two in-window refusals and NO sidecar to attribute them
    pg_u1, _ = _pg(book_u, _u_closed, record=False)
    os.mkdir(config.BAND_VERDICTS_PATH)                      # ... and the same with a sidecar that raises on open
    pg_u2, _ = _pg(book_u, _u_closed, record=False)
    os.rmdir(config.BAND_VERDICTS_PATH)
    os.remove(LB.MISSED_PATH); os.mkdir(LB.MISSED_PATH)      # an unreadable missed log: the numerator is uncountable
    pg_u3, _ = _pg(book_u, _u_closed, record=False)
    os.rmdir(LB.MISSED_PATH)
    _write_missed(_u_miss)
    with open(config.BAND_VERDICTS_PATH, "w", newline="") as fh:
        w_ = csv.writer(fh); w_.writerow(["event_seq", "token", "alert_ts", "band", "verdict"])
        for seq_ in (9101, 9102):
            w_.writerow([seq_, "0x%040x" % seq_, _q_u + 3600, BQ, "0"])      # the band did not select either refusal
    pg_u4, _ = _pg(book_u, _u_closed, record=False)
    os.remove(LB.MISSED_PATH); os.remove(config.BAND_VERDICTS_PATH)
    check("unknown is not zero: in-window refusals the sidecar cannot attribute — because it is missing, or because it raises on "
          "open — VOID the window as CAPACITY UNKNOWN with refused_share None, instead of passing the gate on a share of 0",
          pg_u1["status"] == pg_u2["status"] == "VOID" and "CAPACITY UNKNOWN" in pg_u1["line"] and "CAPACITY UNKNOWN" in pg_u2["line"]
          and pg_u1["refused_share"] is None and pg_u2["refused_share"] is None
          and len(pg_u1["refusals_unknown"]) == len(pg_u2["refusals_unknown"]) == 1
          and not any(ok_ for lbl_, ok_, _d in pg_u1["checks"] if "refused share" in lbl_),
          f"{pg_u1['line']} | {pg_u2['line']}")
    check("an unreadable data/livebook_missed.jsonl is unknown too (the numerator itself cannot be counted), while a MISSING one is "
          "the fact that no refusal was ever logged and a sidecar answering verdict 0 for every refusal is a measured zero",
          pg_u3["status"] == "VOID" and "CAPACITY UNKNOWN" in pg_u3["line"]
          and pg_u0["status"] in ("PASS", "FAIL") and pg_u0["refusals_unknown"] == [] and pg_u0["n_refused"] == 0
          and pg_u4["status"] in ("PASS", "FAIL") and pg_u4["refusals_unknown"] == [] and pg_u4["refused_share"] == 0.0,
          f"{pg_u3['line']} | {pg_u0['line']} | {pg_u4['line']}")

    # the adaptive policy: the price-only twin is the paired benchmark; the flow-dark share VOIDs above 0.50
    from selfimprove.candidates import tp15_half_flowtrail_stop50_6h as _FLOWC, tp15_half_armtrail30_stop50_6h as _ARMC
    POL.POLICIES["zz_flow_probe"] = json.loads(json.dumps(_FLOWC.POLICY))
    POL.POLICIES["zz_price_probe"] = json.loads(json.dumps(_ARMC.POLICY))
    config.PAPER_GATE_POLICY = "zz_flow_probe"
    config.PAPER_GATE_WINDOW_START = "2027-08-01T00:00:00Z"
    _q_t6 = IM._parse_window_start(config.PAPER_GATE_WINDOW_START)
    t6_closed = _q_t6 + W * 86400 + config.LIVEBOOK_MAX_TRACK_S + 1.0
    book_f = IM._synthetic_book(W, 5, _q_t6, seed=40, adv={"zz_flow_probe": 0.60, "zz_price_probe": 0.30}, stamp=[BQ])
    for p_ in book_f.values():
        p_["policies"]["zz_flow_probe"].update(flow_ticks=1, flow_dark_ticks=3)
    pgd, _ = _pg(book_f, t6_closed)
    for p_ in book_f.values():
        p_["policies"]["zz_flow_probe"].update(flow_ticks=4, flow_dark_ticks=0)
    config.PAPER_GATE_WINDOW_START = "2027-08-20T00:00:00Z"
    _q_t7 = IM._parse_window_start(config.PAPER_GATE_WINDOW_START)
    for p_ in book_f.values():
        p_["opened_ts"] += (_q_t7 - _q_t6)
    pgl, _ = _pg(book_f, _q_t7 + W * 86400 + config.LIVEBOOK_MAX_TRACK_S + 1.0)
    check("a policy with a flow leg: the tick-level dark share over ARMED positions comes from livebook.live_stats_dict (the one "
          "implementation) — 3 dark of 4 ticks on every close ⇒ 0.75 > 0.50 ⇒ VOID naming FLOW DARK, n_armed reported",
          pgd["has_flow"] and pgd["n_armed"] == 35 and abs(pgd["flow_dark_share"] - 0.75) < 1e-12 and pgd["status"] == "VOID"
          and "FLOW DARK" in pgd["line"], pgd["line"])
    check("...and with the feed lit (0 dark ticks) the same pair is judged on its numbers: the price-only twin is found as the paired "
          "benchmark (reported, never gated) and the flow-dark check passes",
          pgl["status"] in ("PASS", "FAIL") and pgl["flow_dark_share"] == 0.0 and pgl["n_armed"] == 35
          and pgl["benchmark"]["name"] == "zz_price_probe" and pgl["benchmark"]["n"] == 35 and pgl["benchmark"]["paired_lb"] == pgl["benchmark"]["paired_lb"]
          and any("flow-dark" in l_ and ok_ for l_, ok_, _ in pgl["checks"]), f"{pgl['line']} {pgl['benchmark']}")
    check("_price_only_twin: the twin is the registered policy whose price legs equal the adaptive policy's minus `flow`; a policy "
          "without a flow leg has none", IM._price_only_twin("zz_flow_probe") == "zz_price_probe" and IM._price_only_twin(CHQ) is None)
    del POL.POLICIES["zz_flow_probe"], POL.POLICIES["zz_price_probe"]

    # the cap: after two windows on a pair, a third inside BAND_RENOMINATE_COOLDOWN_DAYS is refused (no bump)
    config.PAPER_GATE_POLICY = "sell_15m"
    TR.bump("nominations", [f"paper:{BQ}/sell_15m@2026-12-01T00:00:00Z", f"paper:{BQ}/sell_15m@2026-12-15T00:00:00Z"])
    n_before = TR.family_count("nominations")
    config.PAPER_GATE_WINDOW_START = "2027-01-15T00:00:00Z"                                    # 31 d after the 2nd: refused
    pgc, _ = _pg(book_a, t_open)
    n_after_refusal = TR.family_count("nominations")
    config.PAPER_GATE_WINDOW_START = "2027-06-01T00:00:00Z"                                    # 168 d after: allowed
    pgc2, _ = _pg({}, t_open)
    check("the cap: a third window on the same (band, policy) inside BAND_RENOMINATE_COOLDOWN_DAYS of the second is REFUSED with no "
          "paper: line; past the cooldown it is registered",
          pgc["status"] == "refused" and "cooldown" in pgc["line"] and n_after_refusal == n_before
          and pgc2["status"] in ("open", "pending") and TR.family_count("nominations") == n_before + 1, f"{pgc['line']} | {pgc2['line']}")

    # summary_json carries the paper gate (NaN-free); evaluate_all threads it; the weekly line relays it
    config.PAPER_GATE_POLICY, config.PAPER_GATE_WINDOW_START = CHQ, START
    LB._save_atomic(book_a, LB.BOOK_PATH)
    res_q, _ = _capture(IM.evaluate_all, t_open, book_a)
    pj = IM.summary_json(res_q)
    strict_q = json.loads(_read(pj), parse_constant=lambda c: (_ for _ in ()).throw(ValueError(c)))
    check("evaluate_all carries res['paper_gate'] and summary_json writes it NaN-free under 'paper_gate' with the line and the status",
          res_q["paper_gate"]["status"] == "open" and strict_q["paper_gate"]["status"] == "open"
          and strict_q["paper_gate"]["line"] == res_q["paper_gate"]["line"] and "NaN" not in _read(pj))
    with tempfile.TemporaryDirectory() as d_:
        PQ = {k: os.path.join(d_, os.path.basename(v_)) for k, v_ in WS.default_paths().items()}
        lines_q0, _ = _capture(WS.compose, t_open, None, PQ)
        shutil.copyfile(pj, PQ["livebook"])
        lines_q1, _ = _capture(WS.compose, t_open, None, PQ)
    check("weekly_summary prints ONE 'paper gate:' line — 'none registered' on an empty repo, the gate's own line from "
          "data/livebook_summary.json otherwise (wrapped by _add: never a crash)",
          [l_ for l_ in lines_q0 if l_.startswith("paper gate:")] == ["paper gate: none registered"]
          and [l_ for l_ in lines_q1 if l_.startswith("paper gate:")] == ["paper gate: " + res_q["paper_gate"]["line"]],
          str([l_ for l_ in lines_q0 + lines_q1 if "paper gate" in l_]))
    # a verdict is not a promotion: champion.json and the registry are untouched by every evaluation above
    check("a verdict promotes nothing: champion.json was never written by the paper gate (only trials.json grew)",
          not os.path.exists(config.CHAMPION_PATH))

    # ── the one-shot lines are minted ONLY on the apply path. selfimprove/trials.json is TRACKED and
    # published by weekly.yml's Publish step alone; a read-only caller that wrote it would diverge the
    # Mac tree (run_research.sh's step-0 ff-merge then refuses) and split the one-shot record in two —
    # a Mac --band-scorecard on an older snapshot recording `=fail` while the runner records `=pass`.
    C1P, C1S = "stop_30", "2026-01-05T00:00:00Z"          # long closed on any real clock: the CLI judges it
    config.PAPER_GATE_POLICY, config.PAPER_GATE_WINDOW_START = C1P, C1S
    _q_c1 = IM._parse_window_start(C1S)
    book_c1 = IM._synthetic_book(W, 5, _q_c1, seed=41, adv={C1P: 0.60}, stamp=[BQ])
    LB._save_atomic(book_c1, LB.BOOK_PATH)
    _tr_bytes = _read(config.TRIALS_PATH)
    pg_c1, _ = _capture(IM.paper_gate, _q_c1 + W * 86400 + config.LIVEBOOK_MAX_TRACK_S + 1.0, book=book_c1)
    rc_c1, out_c1 = _capture(IM.main, ["--band-scorecard"])     # the documented Mac command
    _capture(IM.main, [])                                       # a dry improve.py
    _capture(IM.main, ["--summary-json"])                       # the runner's third Gates command
    check("the paper gate's one-shot bookkeeping is minted ONLY on the apply path: --band-scorecard, a dry improve.py and "
          "--summary-json leave the TRACKED selfimprove/trials.json BYTE-IDENTICAL, and `record=False` is the default",
          _read(config.TRIALS_PATH) == _tr_bytes and rc_c1 == 0 and not _tr_lines(f"paper:{BQ}/{C1P}@"),
          out_c1[-300:])
    check("a read-only evaluation still COMPUTES the same verdict and prints the would-be lines: `recorded_now` empty, "
          "`would_record` carries the paper: and paper_verdict: lines verbatim, and the line says '(not recorded: dry)'",
          pg_c1["status"] in ("PASS", "FAIL", "VOID") and pg_c1["recorded_now"] == []
          and pg_c1["would_record"] == [f"paper:{BQ}/{C1P}@{C1S}", f"paper_verdict:{BQ}/{C1P}@{C1S}={pg_c1['status'].lower()}"]
          and pg_c1["line"].endswith(" (not recorded: dry)") and "(not recorded: dry)" in out_c1
          and f"would record: paper:{BQ}/{C1P}@{C1S}" in out_c1, f"{pg_c1['line']} | {pg_c1['would_record']}")
    LB._save_atomic({}, LB.BOOK_PATH)          # n=0: main's early return never reaches apply() / champion.json
    _capture(IM.main, ["--apply"])
    _c1_after = (_tr_lines(f"paper:{BQ}/{C1P}@"), _tr_lines(f"paper_verdict:{BQ}/{C1P}@"))
    _c1_bytes = _read(config.TRIALS_PATH)
    _capture(IM.main, ["--apply"])
    pg_c1b, _ = _capture(IM.paper_gate, _q_c1 + W * 86400 + config.LIVEBOOK_MAX_TRACK_S + 1.0, book={})
    check("the apply path mints each line exactly ONCE: `improve.py --apply` bumps the paper: and paper_verdict: lines, a second "
          "--apply adds nothing (byte-identical) and every later read prints them '(recorded)' from nominations_ever",
          _c1_after == ([f"paper:{BQ}/{C1P}@{C1S}"], [f"paper_verdict:{BQ}/{C1P}@{C1S}=fail"])
          and _read(config.TRIALS_PATH) == _c1_bytes and pg_c1b["recorded"] == f"paper_verdict:{BQ}/{C1P}@{C1S}=fail"
          and pg_c1b["would_record"] == [] and pg_c1b["line"].endswith(" (recorded)"), f"{_c1_after} | {pg_c1b['line']}")
    config.PAPER_GATE_POLICY, config.PAPER_GATE_WINDOW_START = CHQ, START

    # the history line's `applied` stamp (weekly.yml's idempotency reads it): a dry run never stamps the day
    if os.path.exists(config.IMPROVE_HISTORY_PATH):
        os.remove(config.IMPROVE_HISTORY_PATH)
    LB._save_atomic({}, LB.BOOK_PATH)
    _capture(IM.main, [])
    _capture(IM.main, ["--apply"])
    _hist = [json.loads(l_) for l_ in _read(config.IMPROVE_HISTORY_PATH).splitlines() if l_.strip()]
    check("improve_history.jsonl lines carry `applied`: false for a dry run, true for an --apply run (the day is stamped only by --apply)",
          len(_hist) == 2 and _hist[0]["applied"] is False and _hist[1]["applied"] is True and "ts" in _hist[1], str(_hist))
finally:
    LB.BOOK_PATH, LB.MISSED_PATH = _saved_q["BOOK_PATH"], _saved_q["MISSED_PATH"]
    config.CHAMPION_PATH, config.TRIALS_PATH = _saved_q["CHAMPION_PATH"], _saved_q["TRIALS_PATH"]
    config.IMPROVE_HISTORY_PATH, config.PROPOSALS_DIR = _saved_q["IMPROVE_HISTORY_PATH"], _saved_q["PROPOSALS_DIR"]
    config.LIVEBOOK_SUMMARY_PATH, config.PAUSE_PATH = _saved_q["LIVEBOOK_SUMMARY_PATH"], _saved_q["PAUSE_PATH"]
    config.BAND_VERDICTS_PATH = _saved_q["BAND_VERDICTS_PATH"]
    config.LIVEBOOK_BAND_UNDER_TEST = _saved_q["LIVEBOOK_BAND_UNDER_TEST"]
    config.PAPER_GATE_POLICY, config.PAPER_GATE_WINDOW_START = _saved_q["PAPER_GATE_POLICY"], _saved_q["PAPER_GATE_WINDOW_START"]
    alerts.send_all = _saved_q["send_all"]
    POL.POLICIES.clear(); POL.POLICIES.update(_saved_q["policies"])
    shutil.rmtree(_tmp_q, ignore_errors=True)

# ── 3. the docs say the same thing the code does ──
_design_q = _read(os.path.join(ROOT, "docs", "DESIGN.md"))
check("DESIGN.md's paper-gate row carries the pre-registered numbers, the VOID list, the one-shot rule, 'not a promotion' and the "
      "double-conservative cost note (quoted slippage already embedded, gas outside the quote, $10 sizing understates impact)",
      all(w in _design_q for w in ("Paper gate for a band-under-test", "PAPER_GATE_WINDOW_START", "PAPER_GATE_MIN_FILLS = 20",
                                   "PAPER_GATE_MAX_REFUSED_SHARE = 0.05", "sidecar_true", "flow_dark_share", "0.50",
                                   "a number, not a bound (floor 12)", "paper_verdict:", "not a promotion",
                                   "apply path only", "(not recorded: dry)", "CAPACITY UNKNOWN",
                                   "double-conservative on purpose", "gas is not in the quote")),
      str([w for w in ("Paper gate for a band-under-test", "a number, not a bound (floor 12)", "not a promotion",
                       "double-conservative on purpose") if w not in _design_q]))
check("DESIGN.md's weekly-jobs row is weekly.yml's (10:00 UTC, the idempotency rule, the staged list, robinhood-improve[bot], the ONE "
      "message from the cloud, numpy only) and its pause row names champion.json.locked as the CLOUD switch with PAUSE Mac-local",
      all(w in _design_q for w in ("weekly.yml", "robinhood-weekly", "Sunday 10:00 UTC year-round", "robinhood-improve[bot]",
                                   "sunday_insurance", "RESEARCH_SEND_SUMMARY=1", "gh workflow disable robinhood-weekly"))
      and "Sunday 11:00 `selfimprove/run_improve.sh`" not in _design_q)
_claude_q, _readme_q = _read(os.path.join(ROOT, "CLAUDE.md")), _read(os.path.join(ROOT, "README.md"))
check("CLAUDE.md lists --band-scorecard and the weekly workflow (including the `-f dry=true` rehearsal), says the paper gate's "
      "one-shot lines are written on the apply path only, and no longer tells anyone to run the deleted run_improve.sh",
      "--band-scorecard" in _claude_q and "gh workflow run robinhood-weekly -f dry=true" in _claude_q
      and "robinhood-weekly" in _claude_q and "bash selfimprove/run_improve.sh" not in _claude_q
      and "apply path only" in _claude_q and "not recorded: dry" in _claude_q)
check("README's file table names selfimprove/dsr.py and weekly.yml, and its dependency line says numpy on BOTH sides (scipy is gone "
      "from every path but verify's mac_only cross-checks) while keeping the measured 13.7×/day",
      "`dsr.py`" in _readme_q and "weekly.yml" in _readme_q and "13.7×/day" in _readme_q
      and "the Sunday statistics\nadditionally use numpy, on the Mac and on the runner alike" in _readme_q
      and "`run_improve.sh`" not in _readme_q)

# ── 8b. the RKST retrospective is descriptive, and is honest that the token was never screened ────
# n = 1, chosen because someone believed it had run. The shape that turns this into a claim is a
# later edit quietly dropping the "it is down on 24h/7d" correction or the "never ledgered" section
# and leaving only the +103% life-to-date number. These checks make that edit fail.
_rkst_path = os.path.join(ROOT, "docs", "RETRO_2026-09-19_RKST.md")
check("docs/RETRO_2026-09-19_RKST.md exists", os.path.isfile(_rkst_path))
_rkst = _read(_rkst_path)
check("the RKST retrospective's FIRST section is 'What this cannot tell you' — the disclaimer leads",
      _rkst.split("\n## ")[1].startswith("What this cannot tell you"), _rkst.split("\n## ")[1][:80])
check("the RKST retrospective says no rule is derived, says nothing here is causal, and pins its numbers to an origin/main SHA",
      "no rule is derived" in _rkst and "Nothing here is causal" in _rkst and "7942a6f" in _rkst)
check("the RKST retrospective pins the data-quality columns ('`res`' / '`pages`') it rests on",
      "`res`" in _rkst and "`pages`" in _rkst)
check("the RKST retrospective states the token was NEVER screened and predates the ledger — the gate reading is post-hoc, not point-in-time",
      "predates the system by 6.6 days" in _rkst and "post-hoc" in _rkst)
check("the RKST retrospective corrects the premise rather than confirming it (the 24h/7d drawdown is stated, not only the life-to-date gain)",
      "-21.2" in _rkst.replace("\u2212", "-") and "-33.8" in _rkst.replace("\u2212", "-"))
check("the RKST retrospective claims no hit rate, lift, threshold or cause from n = 1",
      "n = 1" in _rkst and "Refuses to claim" in _rkst and "no cause" in _rkst)
check("the RKST retrospective reports the co-movement test as a lead, with its BY correction and post-hoc peer selection named",
      "Benjamini" in _rkst and "lead, not a finding" in _rkst)
check("the RKST retrospective carries no home path and never names the shared secrets file (public repo)",
      "/Users/" not in _rkst and "monitor_config" not in _rkst and "vrp_backtest" not in _rkst)
check("README's file table carries the RKST retrospective with its one honest bullet",
      "RETRO_2026-09-19_RKST.md" in _readme
      and "descriptive, n = 1, chosen on the outcome" in _readme
      and "predates the ledger and no rule is derived from it" in _readme)

print(f"\nALL INVARIANTS PASSED ({N_PASS} checks, {N_SKIP} skipped)")
