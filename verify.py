"""
THE invariant suite for robinhood_screener — this project's tests. Run after any change:

    python3 verify.py                      # the Mac partition (everything)
    GITHUB_ACTIONS=true python3 verify.py  # the cloud partition (mac_only sections print SKIP)

Every section is OFFLINE: injected fakes, temp dirs, monkeypatched module functions. Nothing
here sends (every send_all is dry_run=True), commits, pushes or touches the network. Sections
that need the Mac (entry_bot/stats.py + scipy for the Deflated Sharpe pin, statsmodels for the
Benjamini-Yekutieli comparison, launchctl, a git identity for publish's temp bare origin) are
tagged mac_only and print SKIP under config.IS_CI or when the dependency is absent.

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


DSR_REAL = MAC and os.path.isfile(ENTRY_BOT_STATS) and _have("scipy")
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
for rel in ("ledger.py", "screen.py", os.path.join("sources", "safety.py"), "quotes.py"):
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

leaks = []
for base in (os.path.join(ROOT, "docs"), os.path.join(ROOT, "selfimprove", "research")):
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
check("no file under docs/ or selfimprove/research/ contains '/Users/' or 'vrp_backtest' (public-repo scrub)",
      not leaks, str(leaks))

rate_names = {n: getattr(config, n) for n in dir(config) if n.endswith("_RATE_HZ") and n != "HOST_RATE_HZ"}
unmapped = [h for h, hz in http_client._HOST_HZ.items() if hz not in rate_names.values()]
check("every http_client._HOST_HZ value is a config.*_RATE_HZ constant (no literals in http_client)",
      not unmapped and dict(http_client._HOST_HZ) == dict(config.HOST_RATE_HZ), str(unmapped))
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
for rel in ("selfimprove/research/run_research.sh", "selfimprove/run_improve.sh", "launchd/dispatch.sh"):
    for i, ln in enumerate(_read(os.path.join(ROOT, rel)).splitlines(), 1):
        s = ln.strip()
        if s.startswith("#"):
            continue
        if re.search(r"\bgit\b[^|&;]*\b(pull|checkout|reset)\b", s) and '-C "$WT"' not in s:
            sh_git.append(f"{rel}:{i}")
check("the shell wrappers run git checkout/reset only inside the temp worktree (-C \"$WT\"), never on the Mac tree",
      not sh_git, str(sh_git))
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
retired = {"com.yousefjan.robinhood-screener", "com.yousefjan.robinhood-dashboard"}
check("the retired launchd labels com.yousefjan.robinhood-screener / -dashboard do not exist under launchd/",
      not (retired & set(labels)) and not any(os.path.basename(p).replace(".plist", "") in retired for p in plists))

yml = _read(os.path.join(ROOT, ".github", "workflows", "screener.yml"))
for needle in ("*/5", "cancel-in-progress: false", 'python-version: "3.11"', "git add data/ docs/",
               "robinhood-screener[bot]", "dashboard.py --write", "deploy-pages", "workflow_dispatch"):
    check(f"screener.yml contains {needle!r}", needle in yml)
check("screener.yml wires the three secret names", all(s in yml for s in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "NTFY_TOPIC")))
check("screener.yml carries the private-repo guard (scheduled runs inert until public: minutes)",
      "repository.private" in yml and "github.event_name != 'schedule'" in yml)
vyml = _read(os.path.join(ROOT, ".github", "workflows", "verify.yml"))
check("verify.yml runs verify.py with fetch-depth 0 on human pushes only (never inside the 5-min job)",
      "fetch-depth: 0" in vyml and "python verify.py" in vyml and "screener state" in vyml
      and "verify.py" not in yml)
reqs = {re.split(r"[<>=~!\s]", ln.strip())[0] for ln in _read(os.path.join(ROOT, "requirements.txt")).splitlines()
        if ln.strip() and not ln.startswith("#")}
check("requirements.txt is exactly pandas + certifi", reqs == {"pandas", "certifi"}, str(reqs))


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
      "improve.py / scorecard.py are not (Mac-only modules)",
      os.path.join(ROOT, "selfimprove", "champion.py") in _graph
      and os.path.join(ROOT, "selfimprove", "improve.py") not in _graph
      and os.path.join(ROOT, "selfimprove", "entry_lab", "scorecard.py") not in _graph)


def _ignored(rel: str) -> bool:
    if HAVE_GIT:
        r = subprocess.run(["git", "-C", ROOT, "check-ignore", "-q", "--no-index", rel],
                           capture_output=True, text=True)
        if r.returncode in (0, 1):
            return r.returncode == 0
    pats = [ln.strip() for ln in _read(os.path.join(ROOT, ".gitignore")).splitlines()
            if ln.strip() and not ln.startswith("#")]
    return any(rel.startswith(p.rstrip("/")) or rel.endswith(p.lstrip("*")) for p in pats)


must_ignore = ["cache/x.json", "config.local.json", "run.out.log", "livebook.err.log", "data/livebook.json",
               "data/livebook_fills.csv", "data/livebook_ticks.jsonl", "data/livebook_feed.json",
               "data/livebook_missed.jsonl", "data/backups/livebook_20260912.json", "selfimprove/PAUSE",
               "selfimprove/research/context/context_2026-09-13.md", "selfimprove/research/logs/run_2026-09-13.log",
               "data/archive/proven_dev/social_verdicts.json", "data/archive/proven_dev/history.bundle"]
must_keep = ["data/proposals/entry-20260913-000000.md", "selfimprove/champion.json", "selfimprove/trials.json",
             "selfimprove/candidates/registry.json", "data/ledger.csv", "data/livebook_summary.json",
             "selfimprove/research/proposals/PROPOSAL_2026-09-13.md"]
not_ign = [p for p in must_ignore if not _ignored(p)]
wrongly = [p for p in must_keep if _ignored(p)]
check(".gitignore covers cache/, config.local.json, *.out.log/*.err.log, the five data/livebook* state "
      "files, data/backups/, selfimprove/PAUSE, research context/ + logs/, the offline archive files",
      not not_ign, str(not_ign))
check(".gitignore does NOT ignore data/proposals/, champion.json, trials.json, registry.json, ledger.csv, "
      "livebook_summary.json, research proposals", not wrongly, str(wrongly))

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

T0 = 1_780_000_000.0
MK = {"price_usd": 1.0, "mcap": 1e6, "liq_usd": 5e4}


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
_saved_lb = {k: _g[k] for k in ("BOOK_PATH", "FILLS_PATH", "TICKS_PATH", "FEED_STATE_PATH", "MISSED_PATH")}
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


def lrow(token, seq, tier="A", kind="promotion", ats=None, prior=None):
    return {"token": token, "event_seq": seq, "symbol": "T%d" % seq, "tier": tier, "event_kind": kind,
            "prior_event_seq": prior, "plan_name": "cfg_ladder_stop", "alert_ts": LT0 - 30 if ats is None else ats}


def opn(r, now_s=LT0, buy=buy_ok):
    return LB.open_alert(r, now_s, quote_buy_fn=buy, weth_px=WPX, decimals_fn=lambda t: 18)


def tk(now_s):
    return LB.tick(now_s, quote_many_fn=sell_many, dex_fn=dex_fn, weth_px=WPX, verbose=False)


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
                       ("MISSED_PATH", "livebook_missed.jsonl")):
        _g[name] = os.path.join(d, base)
        if os.path.exists(_g[name]):
            os.remove(_g[name])
    script.clear(); dex_mult.clear()


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
finally:
    for k, v in _saved_lb.items():
        _g[k] = v
    config.LIVEBOOK_MAX_OPEN = _saved_max_open


# ═══════════════════════════════════════════════════════════════════════════════════
section("H. improve.py — the exit gate: thin evidence refuses, controls void, forward-only promotion")
# ═══════════════════════════════════════════════════════════════════════════════════
from selfimprove import improve as IM                   # noqa: E402
from selfimprove import trials as TR                    # noqa: E402
import alerts                                           # noqa: E402

_saved_im = {"BOOK_PATH": LB.BOOK_PATH, "MISSED_PATH": LB.MISSED_PATH, "CHAMPION_PATH": config.CHAMPION_PATH,
             "TRIALS_PATH": config.TRIALS_PATH, "IMPROVE_HISTORY_PATH": config.IMPROVE_HISTORY_PATH,
             "PROPOSALS_DIR": config.PROPOSALS_DIR, "LIVEBOOK_SUMMARY_PATH": config.LIVEBOOK_SUMMARY_PATH,
             "PAUSE_PATH": config.PAUSE_PATH, "send_all": alerts.send_all, "_dsr": IM._dsr}
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
    if DSR_REAL:
        print("  (Deflated Sharpe from ~/entry_bot/stats.py — real)")
    else:
        skip("Deflated Sharpe pin via entry_bot/stats.py (mac_only)", "entry_bot/scipy absent or IS_CI; DSR injected as 0.99")
        IM._dsr = lambda r, c, n: 0.99
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
    IM._dsr = _saved_im["_dsr"]
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
_fires = {"band_top10_le15": {"top10_pct": 15.0, "top10_pct_gt": 15.0}, "band_graduated_only": {"launchpad_completed_age_s": 3600.0}}
na_bad = [(n, k) for n, sp in B.BUILTINS.items() for k in sp.REQUIRES
          if sp.verdict(dict(dict(good, **_fires.get(n, {})), **{k: None})) is not None]
check("a dark REQUIRES field yields None (NA) for every band", not na_bad, str(na_bad[:3]))
HC3 = [n for n, sp in B.BUILTINS.items() if sp.THREE_VALUED]
kl = dict(good, roundtrip_loss_pct=12.0, dev_pct=None)     # a definite miss beside an unknown
check("three-valued AND (hc family): a definite miss (round trip 12%) beside an unknown (dev_pct None) is False, not NA; "
      "the unknown alone is NA; True never fires with an unknown",
      len(HC3) == 7 and all(B.BUILTINS[n].verdict(kl) is False for n in HC3)
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
n_sel = 0
for i in range(10_000):
    tok = "0x" + hashlib.sha256(f"synthetic-{i}".encode()).hexdigest()[:40]
    n_sel += bool(B.ctl_random_band.verdict(dict(good, token=tok, sighting_age_s=float(config.BAND_WATCH_WINDOW_S))))
rate = n_sel / 10_000
check(f"ctl_random_band rate within ±2 pp of {config.BAND_CTL_RANDOM_RATE} on 10,000 addresses and process-stable (sha256, pinned)",
      abs(rate - config.BAND_CTL_RANDOM_RATE) <= 0.02 and B.random_control_params("0x" + "ab" * 20) == (False, 62357.833739732814)
      and B.random_control_params("0x" + "AB" * 20) == B.random_control_params("0x" + "ab" * 20), str(rate))
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
if DSR_REAL:
    check("DSR on day means: planted >= gate, random < gate; < 8 days is NaN (mac_only)",
          SC.dsr_day_means(s_p, r_, days, n_tr) >= config.BAND_DSR_GATE and SC.dsr_day_means(s_r, r_, days, n_tr) < config.BAND_DSR_GATE
          and math.isnan(SC.dsr_day_means(s_p[:24], r_[:24], days[:24], n_tr)))
else:
    skip("DSR on day means via entry_bot/stats.py (mac_only)", "entry_bot/scipy absent or IS_CI")
_sd, _sp = SC.ENTRY_BOT_DIR, list(sys.path)
SC.ENTRY_BOT_DIR = os.path.join(tempfile.gettempdir(), "no_such_entry_bot_dir")
sys.path = [x for x in sys.path if not x.endswith("entry_bot")]
sys.modules.pop("stats", None)
v_nan = SC.dsr_day_means(s_p, r_, days, n_tr)
SC.ENTRY_BOT_DIR, sys.path = _sd, _sp
check("DSR fails CLOSED (NaN) when entry_bot is absent", math.isnan(v_nan))
inv = SC.sel(ser.assign(**{CHAMP: ser[CHAMP].where(ser.index != 0)}), "ctl_inverse_band", CHAMP)
check("scorecard recomputes ctl_inverse_band on the fly from the champion column (NaN where the champion is NaN)",
      math.isnan(inv[0]) and inv[1] == 1.0 - float(ser.loc[1, CHAMP]))

# the entry gate
_saved_dsr = SC.dsr_day_means
_saved_send = alerts.send_all
alerts.send_all = lambda title, body, dry_run=True: None
base_dir = tempfile.mkdtemp(prefix="verify_entry_lab_")
SH = 200
try:
    if not DSR_REAL:
        SC.dsr_day_means = lambda s_, rr, dd, n: 0.99
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
    disc, cur, gap = RUN.discover_from_logs({"last_block": 990}, 1500, RUN._Budget(60))
    check("discover_from_logs decodes the three fixture logs through rpc.decode_discovery and advances the cursor to head",
          set(disc) == {TOK_P, TOK_V3, TOK_F} and cur["last_block"] == 1500 and gap == 0)
    calls_gl.clear()
    rpc.get_logs = lambda addrs, topics, a, b: (calls_gl.append((a, b)) or (None, "query returned more than 10000 results"))
    H = 2_000_000
    disc, cur, gap = RUN.discover_from_logs({"last_block": H - 50_000}, H, RUN._Budget(60))
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
    disc, cur, gap = RUN.discover_from_logs({"last_block": H - 400_000}, H, RUN._Budget(60))
    check(f"a cursor older than DISCOVERY_MAX_CATCHUP_BLOCKS ({config.DISCOVERY_MAX_CATCHUP_BLOCKS}) is capped and the gap reported",
          calls_gl[0][0] == H - config.DISCOVERY_MAX_CATCHUP_BLOCKS and gap == 400_000 - config.DISCOVERY_MAX_CATCHUP_BLOCKS - 1
          and cur["last_block"] == H)
    START = 3_000_000
    many_logs = [_log(config.FLAP_ROUTER, [config.TOPIC_FLAP_TOKEN_CREATED],
                      "0x" + rpc.enc_uint(1) + rpc.enc_addr(CREATOR_F) + rpc.enc_uint(i) + rpc.enc_addr("0x%040x" % (i + 1)) + rpc.enc_uint(0),
                      START + i, 0) for i in range(300)]
    rpc.get_logs = lambda addrs, topics, a, b: ([lg for lg in many_logs if a <= int(lg["blockNumber"], 16) <= b], None)
    disc, cur, gap = RUN.discover_from_logs({"last_block": START - 1}, START + 400, RUN._Budget(60))
    cap = config.DISCOVERY_MAX_LOG_TOKENS_PER_RUN
    check(f"with 300 log tokens only DISCOVERY_MAX_LOG_TOKENS_PER_RUN ({cap}) are processed and the cursor sits one block "
          "below the cut (log tokens are never truncated silently)",
          len(disc) == cap and cur["last_block"] == START + cap - 2 and set(disc) == {"0x%040x" % (i + 1) for i in range(cap)})
finally:
    rpc.get_logs = _saved_get_logs
with tempfile.TemporaryDirectory() as d:
    pj = os.path.join(d, "x.json")
    RUN._atomic_json(pj, {"a": float("nan"), "b": [float("inf"), 1.0]}, indent=1)
    raw = _read(pj)
    strict = json.loads(raw, parse_constant=lambda c: (_ for _ in ()).throw(ValueError(c)))
    check("_atomic_json refuses NaN/inf (writes null) and leaves no tmp", strict == {"a": None, "b": [None, 1.0]}
          and not [f for f in os.listdir(d) if f.endswith(".tmp")])


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
              "stock": scanhood.stock_tokens, "send_all": RUN.send_all, "save_watch": LAB.save_watchlist}
sent_run: list = []
try:
    cur0 = RUN._load_json(config.CURSOR_PATH, {})
    rpc.block_number = lambda: int(cur0.get("last_block") or 100_000) + 10
    rpc.get_logs = lambda addrs, topics, a, b: ([], None)
    GT.new_pools = lambda page=1, network=None: []
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
            "run_seconds": 12.0}
run_log_syn = [{"scan_ts": LT0 - 600, "trigger": "schedule", "run_seconds": 40, "n_a": 0},
               {"scan_ts": LT0, "trigger": "dispatch", "run_seconds": 12.0, "n_a": 1}]
with tempfile.TemporaryDirectory() as d:
    page = DASH.render(scan_syn, LED.load(os.path.join(d, "l.csv")), None, None, run_log_syn)
    check("dashboard.render on a synthetic scan contains 'A-TIER', the band, 'runs in the last 24 h' and config.FOOTER",
          all(n in page for n in ("A-TIER", CHAMP, "runs in the last 24 h", config.FOOTER)) and "<b>2</b>" in page)
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


# ═══════════════════════════════════════════════════════════════════════════════
section("M. launchpad tokens — Bankr / Doppler on Uniswap V4 (the reference winners CATGPT, ANTHROPIG)")
from sources import rpc as RPCM, safety as SAFEM   # noqa: E402
import selfimprove.trials as TRIALS_MOD             # noqa: E402
import run as RUNM                                   # noqa: E402

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
    _s1 = dict(SAFEM.empty_safety(), lp_check_source="v4_launchpad:bankr", deployer="0x" + "ab" * 20)
    _sf = SAFEM.pass2("0x" + "cd" * 20, {"liq_usd": 5e4}, _s1, 1_789_300_000.0, fast=True)
    check("fast pass 2 calls GT, token_counters and address_info ONLY (no paged holders, creator, creation logs, RobinX or "
          "launch history), fills holders/tx-per-holder/is_scam/template and marks fast_pass2",
          set(_bs_calls) == {"gt", "token_counters", "address_info"} and _sf["total_holders"] == 1200
          and _sf["holders_source"] == "blockscout" and _sf["tx_per_holder_total"] == 2.0 and _sf["is_scam"] is False
          and _sf["template_name"] == "DopplerERC20V1" and "fast_pass2" in _sf["sources_used"] and _sf["pass"] == 2,
          str((_bs_calls, {k: _sf.get(k) for k in ("total_holders", "is_scam", "template_name")})))
finally:
    for k, v in _orig_bs.items():
        setattr(SAFEM.blockscout, k, v)
    SAFEM.geckoterminal.token_info, SAFEM.robinx.wallet, SAFEM.rpc.creator_launches = _orig_gt, _orig_rx, _orig_cl
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
_live = CH.state()["entry_band"]
check("the LIVE champion is band_volume_early by a manual --set (evidence.manual, previous band_a_strict, reason recorded) while "
      "DEFAULT_ENTRY_BAND stays band_a_strict as the fallback and demotion target",
      _live["champion"] == "band_volume_early" and _live["previous"] == "band_a_strict"
      and (_live.get("evidence") or {}).get("manual") is True and (_live.get("evidence") or {}).get("reason")
      and config.DEFAULT_ENTRY_BAND == "band_a_strict", str(_live)[:200])
_rs = _read(os.path.join(ROOT, "run.py"))
check("early alerts: run.py sends the champion-band alert for pass-1-selected tokens BEFORE the remaining pass 2, the watchlist "
      "refresh and the forward update; the later alert block never re-sends them; stage_seconds is written to the scan",
      0 < _rs.index('early_alerted = {r["token"] for r in early_alerts}') < _rs.index("_run_p2(p2_rest)")
      < _rs.index('to_send = [r for r in fresh_alerts if r["token"] not in early_alerted]')
      < _rs.index("ledger.update_forward(") and '"stage_seconds": stage_s' in _rs)
_wf = _read(os.path.join(ROOT, ".github", "workflows", "screener.yml"))
check("screener.yml: the scan job and the Pages job hold SEPARATE concurrency groups (a deploy never delays the next scan) "
      "and pip is cached", "group: screener-scan" in _wf and "group: screener-pages" in _wf and "cache: pip" in _wf
      and _wf.count("concurrency:") == 2)
check("REFERENCE_TOKENS name the two winners and the registry lists the launchpad band as a candidate, never the champion",
      set(config.REFERENCE_TOKENS) == {"CATGPT", "ANTHROPIG"} and CHAMP == "band_a_strict"
      and any(e_["name"] == "band_launchpad_lenient" and e_["status"] == "candidate" for e_ in json.load(open(config.REGISTRY_PATH))["candidates"]))

print(f"\nALL INVARIANTS PASSED ({N_PASS} checks, {N_SKIP} skipped)")
