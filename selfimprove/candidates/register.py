"""
register.py — the ONLY way a research candidate enters selfimprove/candidates/registry.json.

  --scan [--max-new N]   find modules under candidates/ not yet registered, validate each in a
                         subprocess, register at most N, bump selfimprove/trials.json
  --budget-remaining     print how many registrations this week may still make
  --selftest             offline smoke test in a temp dir (never touches the real registry)

WHY A SEPARATE VALIDATOR PROCESS. A candidate is Python written by a headless model on a
branch. It is imported here with `python3 -I -S` (no site-packages, no user site, no env, no
PYTHONPATH) and only the repo root on sys.path, AFTER bands.static_ok has walked its AST — so
the first time the module runs, it cannot reach numpy, pandas, sklearn, the network, the clock
or the environment even if the static check missed something. The validator runs TWICE with
different PYTHONHASHSEED values and the verdict vectors must agree: a band that leans on the
salted builtin hash() (the one non-determinism the AST cannot see) is refused on the spot.

WHY EVERY REGISTRATION IS COUNTED. The registered name goes into trials.json's family list and
deflates every later Deflated-Sharpe gate; RESEARCH_MAX_NEW_CANDIDATES_PER_WEEK and
RESEARCH_MAX_REGISTERED bound how fast that denominator can grow. registered_at_event_seq is
the ledger's max(event_seq) on origin/main at the moment of registration — the forward-only
key: rows at or before it were visible when the hypothesis was formed and can never be the
evidence that promotes it.

Checks per candidate (band): static_ok; NAME == file name and ^[a-z][a-z0-9_]{2,40}$; not a
registered/built-in name; KIND, RATIONALE, CONSUMED_DATA declared; REQUIRES non-empty and a
subset of FEATURE_FIELDS; verdict deterministic on 50 fixture dicts across two processes; NA
whenever a REQUIRES field is None; no undeclared dependency (a None in any NON-required field
must not make the body raise); not all-NA; not inert vs the champion on the fixtures.
(policy): KIND == "policy", POLICY declared, policies.validate_policy(NAME, POLICY) is None.

time.time() is called ONCE, in the CLI; nothing here sends.
"""
from __future__ import annotations

import csv
import json
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

import config                                     # noqa: E402
from selfimprove import trials                    # noqa: E402
from selfimprove import policies as POL           # noqa: E402
from selfimprove.entry_lab import bands as B      # noqa: E402

CANDIDATES_DIR = os.path.dirname(os.path.abspath(__file__))
SKIP_FILES = {"_template.py", "register.py", "__init__.py"}
NAME_RE = re.compile(r"^[a-z][a-z0-9_]{2,40}$")
N_FIXTURES = 50
SCHEMA = 2

# Runs under `python3 -I -S`: only the repo root on sys.path (inserted below), stdlib only.
_VALIDATOR_SRC = r'''
import sys, json, importlib.util
inp = json.load(sys.stdin)
sys.path.insert(0, inp["root"])
out = {"ok": False, "reason": "", "name": None, "kind": None}
try:
    from selfimprove.entry_lab import bands as B
    ok, why = B.static_ok(inp["path"])
    if not ok:
        out["reason"] = "static check failed: " + why
        print(json.dumps(out)); sys.exit(0)
    mod = B.load_candidate_module(inp["path"], inp["modname"])
    for attr in ("NAME", "KIND", "RATIONALE", "CONSUMED_DATA"):
        if not hasattr(mod, attr):
            out["reason"] = "missing " + attr
            print(json.dumps(out)); sys.exit(0)
    out["name"] = str(mod.NAME); out["kind"] = str(mod.KIND)
    out["rationale"] = str(mod.RATIONALE)
    out["consumed_data"] = [str(x) for x in (mod.CONSUMED_DATA or ())]
    out["proposal"] = getattr(mod, "PROPOSAL", None)
    if out["kind"] == "policy":
        pol = getattr(mod, "POLICY", None)
        if not isinstance(pol, dict):
            out["reason"] = "policy candidate must declare POLICY = {...}"
            print(json.dumps(out)); sys.exit(0)
        out["policy"] = pol; out["ok"] = True
        print(json.dumps(out)); sys.exit(0)
    if out["kind"] != "band":
        out["reason"] = "KIND must be 'band' or 'policy'"
        print(json.dumps(out)); sys.exit(0)
    if not hasattr(mod, "REQUIRES") or not callable(getattr(mod, "verdict", None)):
        out["reason"] = "band candidate must declare REQUIRES and verdict(feat)"
        print(json.dumps(out)); sys.exit(0)
    requires = [str(k) for k in mod.REQUIRES]
    if not requires:
        out["reason"] = "REQUIRES is empty: a band must declare what it reads"
        print(json.dumps(out)); sys.exit(0)
    bad = [k for k in requires if k not in B.config.FEATURE_FIELDS]
    if bad:
        out["reason"] = "REQUIRES not in FEATURE_FIELDS: " + ",".join(bad)
        print(json.dumps(out)); sys.exit(0)
    out["requires"] = requires
    spec = B.spec_from_module(mod, "candidate")
    fixtures = inp["fixtures"]
    v1 = [spec.verdict(f) for f in fixtures]
    v2 = [spec.verdict(f) for f in fixtures]
    if v1 != v2:
        out["reason"] = "verdict not deterministic within one process"
        print(json.dumps(out)); sys.exit(0)
    out["verdicts"] = v1
    # NA rule on every REQUIRES field, on the first fixture
    for k in requires:
        d = dict(fixtures[0]); d[k] = None
        if spec.verdict(d) is not None:
            out["reason"] = "not NA when " + k + " is None"
            print(json.dumps(out)); sys.exit(0)
    # undeclared dependency: a None in a NON-required field must not make the body raise
    for k in B.config.FEATURE_FIELDS:
        if k in requires:
            continue
        d = dict(fixtures[0]); d[k] = None
        try:
            mod.verdict(d)
        except Exception as exc:
            out["reason"] = "reads undeclared field " + k + " (" + type(exc).__name__ + " when None)"
            print(json.dumps(out)); sys.exit(0)
    out["ok"] = True
except SystemExit:
    raise
except Exception as exc:
    out["reason"] = "import/validation raised " + type(exc).__name__ + ": " + str(exc)[:200]
print(json.dumps(out))
'''


def _run_validator(path: str, modname: str, fixtures: list, hashseed: str) -> dict:
    payload = json.dumps({"root": ROOT, "path": path, "modname": modname, "fixtures": fixtures})
    env = {"PYTHONHASHSEED": hashseed, "PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    try:
        r = subprocess.run([sys.executable, "-I", "-S", "-c", _VALIDATOR_SRC], input=payload,
                           capture_output=True, text=True, timeout=120, env=env, cwd=ROOT)
    except Exception as exc:
        return {"ok": False, "reason": f"validator failed to run: {type(exc).__name__}: {exc}"}
    line = (r.stdout or "").strip().splitlines()
    if not line:
        return {"ok": False, "reason": f"validator produced no result (rc={r.returncode}): "
                                       f"{(r.stderr or '')[-300:]}"}
    try:
        return json.loads(line[-1])
    except Exception:
        return {"ok": False, "reason": f"validator output unreadable: {line[-1][:200]}"}


def fixtures(n: int = N_FIXTURES) -> list:
    """n JSON-serialisable feat dicts around the clean fixture, seeded (config.SEED)."""
    import numpy as np
    rng = np.random.default_rng(config.SEED)
    base = B.clean_fixture()
    out = []
    for i in range(n):
        f = dict(base)
        f.update({
            "score": float(rng.uniform(30, 95)), "top10_pct": float(rng.uniform(3, 35)),
            "top10_pct_gt": float(rng.uniform(3, 35)),
            "total_holders": int(rng.integers(100, 3000)),
            "pair_age_min": float(rng.uniform(5, 2000)),
            "liq_usd": float(rng.uniform(10_000, 200_000)),
            "vol_h24": float(rng.uniform(20_000, 900_000)),
            "creator_score": float(rng.uniform(0, 100)),
            "creator_prior_tokens": int(rng.integers(0, 4)),
            "launchpad_completed": bool(rng.uniform() < 0.6),
            "launchpad_completed_age_s": float(rng.uniform(0, 20_000)),
            "roundtrip_loss_pct": float(rng.uniform(0.5, 14)),
            "lp_locked_pct": float(rng.choice([100.0, 96.0, 92.0])),
            "owner_renounced": bool(rng.uniform() < 0.9),
            "dev_pct": float(rng.uniform(0, 5)), "dev_sniped": bool(rng.uniform() < 0.2),
            "holders_source": str(rng.choice(["blockscout", "gt"])),
            "holders_updated_age_s": float(rng.uniform(0, 20_000)),
            "buys_h1": int(rng.integers(10, 2000)), "sells_h1": int(rng.integers(10, 2000)),
            "sighting_age_s": float(rng.uniform(0, config.BAND_WATCH_WINDOW_S)),
            "token": "0x" + "".join(rng.choice(list("0123456789abcdef"), 40)),
            "first_sighting": bool(i == 0),
        })
        out.append(f)
    return out


def _champion_verdicts(reg, champion: str, fx: list) -> list:
    spec = reg.get(champion)
    if spec is None:
        return [None] * len(fx)
    return [spec.verdict(f) for f in fx]


def load_registry_raw(path: str) -> dict:
    if not os.path.exists(path):
        return {"schema": SCHEMA, "candidates": []}
    with open(path) as f:
        raw = json.load(f)
    raw.setdefault("schema", SCHEMA)
    raw.setdefault("candidates", [])
    return raw


def _atomic_json(path: str, obj) -> None:
    def clean(o):
        if isinstance(o, float) and (o != o or o in (float("inf"), float("-inf"))):
            return None
        if isinstance(o, dict):
            return {k: clean(v) for k, v in o.items()}
        if isinstance(o, (list, tuple)):
            return [clean(v) for v in o]
        return o
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w") as f:
        json.dump(clean(obj), f, indent=2, allow_nan=False)
        f.write("\n")
    os.replace(tmp, path)


def max_event_seq(ledger_path: str | None = None, use_git: bool = True) -> int:
    """max(event_seq) in origin/main:data/ledger.csv, falling back to the local ledger; 0 when
    empty or absent. exit 128 from git (no origin, no such path) is 'absent', silently."""
    text = None
    if use_git:
        try:
            r = subprocess.run(["git", "-C", ROOT, "show", "origin/main:data/ledger.csv"],
                               capture_output=True, text=True, timeout=30)
            if r.returncode == 0:
                text = r.stdout
        except Exception:
            text = None
    if text is None:
        p = ledger_path or config.LEDGER_PATH
        if os.path.exists(p):
            try:
                with open(p) as f:
                    text = f.read()
            except Exception:
                text = None
    if not text:
        return 0
    best = 0
    try:
        for row in csv.DictReader(text.splitlines()):
            try:
                best = max(best, int(float(row.get("event_seq") or 0)))
            except (TypeError, ValueError):
                continue
    except Exception:
        return best
    return best


def _taken_names(raw: dict) -> set:
    names = {c.get("name") for c in raw.get("candidates", [])}
    names |= set(B.BUILTINS) | set(POL.POLICIES) | set(POL.CONTROLS)
    return names


def validate_module(path: str, raw: dict, reg, champion: str, fx: list) -> tuple:
    """(ok, reason, info). info carries the validated fields for the registry entry."""
    modname = os.path.splitext(os.path.basename(path))[0]
    ok, why = B.static_ok(path)
    if not ok:
        return False, f"static check failed: {why}", {}
    r1 = _run_validator(path, modname, fx, "1")
    if not r1.get("ok"):
        return False, r1.get("reason") or "validator refused", {}
    name, kind = r1.get("name"), r1.get("kind")
    if name != modname:
        return False, f"NAME {name!r} must equal the file name {modname!r}", {}
    if not NAME_RE.match(str(name)):
        return False, f"NAME {name!r} does not match ^[a-z][a-z0-9_]{{2,40}}$", {}
    if name in _taken_names(raw):
        return False, "name already registered / built-in", {}
    if not str(r1.get("rationale") or "").strip():
        return False, "RATIONALE is empty", {}
    if kind == "policy":
        why = POL.validate_policy(name, r1.get("policy"))
        if why:
            return False, f"policy invalid: {why}", {}
        return True, "ok", r1
    # band: determinism across processes (PYTHONHASHSEED), not all-NA, not inert vs champion
    r2 = _run_validator(path, modname, fx, "2")
    if not r2.get("ok"):
        return False, r2.get("reason") or "validator refused on the second pass", {}
    v1, v2 = r1.get("verdicts") or [], r2.get("verdicts") or []
    if v1 != v2:
        return False, "verdict differs across processes (builtin hash() or other salted state)", {}
    if all(v is None for v in v1):
        return False, "NA on every fixture", {}
    if v1 == _champion_verdicts(reg, champion, fx):
        return False, f"inert: identical to the champion {champion} on {len(fx)} fixtures", {}
    return True, "ok", r1


def scan(registry_path: str, ledger_path: str | None, max_new: int, now_s: float,
         candidates_dir: str = CANDIDATES_DIR, trials_path: str | None = None,
         use_git: bool = True, champion: str | None = None) -> list:
    """Validate every unregistered module, register at most max_new, bump trials.
    Returns [(name, decision, reason)] and prints one line per decision."""
    raw = load_registry_raw(registry_path)
    reg = B.load_registry(registry_path)
    champion = champion or config.DEFAULT_ENTRY_BAND
    listed = {c.get("module", "").split(".")[-1] for c in raw["candidates"]}
    listed |= {c.get("name") for c in raw["candidates"]}
    files = sorted(f for f in os.listdir(candidates_dir)
                   if f.endswith(".py") and f not in SKIP_FILES and not f.startswith("."))
    todo = [f for f in files if os.path.splitext(f)[0] not in listed]
    n_registered_total = sum(1 for c in raw["candidates"] if c.get("status") != "control")
    budget = budget_remaining(registry_path, now_s)
    cap = max(0, min(int(max_new), budget))
    print(f"scan: {len(todo)} unregistered module(s); cap {cap} this run "
          f"(--max-new {max_new}, weekly budget {budget}, registered {n_registered_total}/"
          f"{config.RESEARCH_MAX_REGISTERED})")
    if not todo:
        return []
    fx = fixtures()
    seq = max_event_seq(ledger_path, use_git=use_git)
    decisions = []
    n_new = 0
    for fn in todo:
        name = os.path.splitext(fn)[0]
        path = os.path.join(candidates_dir, fn)
        if n_new >= cap:
            decisions.append((name, "refused", f"over the cap of {cap} new candidate(s) this run"))
            print(f"  refused  {name}: over the cap ({cap}) — left unregistered")
            continue
        ok, why, info = validate_module(path, raw, reg, champion, fx)
        if not ok:
            decisions.append((name, "refused", why))
            print(f"  refused  {name}: {why}")
            continue
        kind = info["kind"]
        entry = {"name": name, "kind": kind, "module": f"candidates.{name}", "status": "candidate",
                 "registered_ts": float(now_s), "registered_at_event_seq": int(seq),
                 "nominated_at_event_seq": None, "rationale": info.get("rationale"),
                 "requires": list(info.get("requires") or []) if kind == "band" else [],
                 "consumed_data": list(info.get("consumed_data") or []),
                 "proposal": info.get("proposal")}
        if kind == "policy":
            entry["policy"] = info.get("policy")
        raw["candidates"].append(entry)
        _atomic_json(registry_path, raw)
        family = "bands" if kind == "band" else "policies"
        count = trials.bump(family, [name], trials_path)
        n_new += 1
        decisions.append((name, "registered", f"{kind} seq={seq} trials[{family}]={count}"))
        print(f"  registered {name} ({kind}) at event_seq {seq}; {family} ever scored: {count}")
    return decisions


def budget_remaining(registry_path: str, now_s: float) -> int:
    raw = load_registry_raw(registry_path)
    cands = [c for c in raw["candidates"] if c.get("status") != "control"]
    if len(cands) >= config.RESEARCH_MAX_REGISTERED:
        return 0
    recent = sum(1 for c in cands
                 if float(c.get("registered_ts") or 0.0) > now_s - 7 * 86400)
    return max(0, config.RESEARCH_MAX_NEW_CANDIDATES_PER_WEEK - recent)


def _arg(argv: list, flag: str, default=None):
    if flag in argv:
        i = argv.index(flag)
        if i + 1 < len(argv):
            return argv[i + 1]
    return default


def _selftest() -> int:
    import tempfile
    tmpl = open(os.path.join(CANDIDATES_DIR, "_template.py")).read()
    with tempfile.TemporaryDirectory() as d:
        cdir = os.path.join(d, "candidates")
        os.makedirs(cdir)
        rp = os.path.join(cdir, "registry.json")
        tp = os.path.join(d, "trials.json")
        lp = os.path.join(d, "ledger.csv")
        with open(lp, "w") as f:
            f.write("token,event_seq,tier\n0xa,3,B\n0xb,17,A\n0xc,,B\n")
        # a registry with the built-ins only
        real = load_registry_raw(config.REGISTRY_PATH)
        _atomic_json(rp, {"schema": SCHEMA,
                          "candidates": [c for c in real["candidates"] if c.get("module") == B.BUILTIN_MODULE]})
        good_band = tmpl.replace('NAME = "band_template_example"', 'NAME = "band_liq_rt"')
        with open(os.path.join(cdir, "band_liq_rt.py"), "w") as f:
            f.write(good_band)
        with open(os.path.join(cdir, "sell_45m.py"), "w") as f:
            f.write('from __future__ import annotations\nimport config\nNAME = "sell_45m"\n'
                    'KIND = "policy"\nRATIONALE = "time exit between sell_30m and sell_1h"\n'
                    'CONSUMED_DATA = ("data/livebook_summary.json",)\nPOLICY = {"max_hold_s": 45 * 60}\n')
        with open(os.path.join(cdir, "band_uses_time.py"), "w") as f:
            f.write('import time\nNAME = "band_uses_time"\nKIND = "band"\nRATIONALE = "x"\n'
                    'CONSUMED_DATA = ()\nREQUIRES = ("score",)\ndef verdict(f):\n    return True\n')
        with open(os.path.join(cdir, "band_inert.py"), "w") as f:
            f.write('from selfimprove.entry_lab import bands\nNAME = "band_inert"\nKIND = "band"\n'
                    'RATIONALE = "same as the champion"\nCONSUMED_DATA = ()\n'
                    'REQUIRES = ("score",)\ndef verdict(f):\n    return bands.band_a_strict.verdict(f)\n')
        with open(os.path.join(cdir, "band_undeclared.py"), "w") as f:
            f.write('import config\nNAME = "band_undeclared"\nKIND = "band"\nRATIONALE = "reads score"\n'
                    'CONSUMED_DATA = ()\nREQUIRES = ("liq_usd",)\n'
                    'def verdict(f):\n    return f["score"] >= 50 and f["liq_usd"] > 0\n')
        with open(os.path.join(cdir, "band_hashy.py"), "w") as f:
            f.write('import config\nNAME = "band_hashy"\nKIND = "band"\nRATIONALE = "salted"\n'
                    'CONSUMED_DATA = ()\nREQUIRES = ("token",)\n'
                    'def verdict(f):\n    return hash(f["token"]) % 2 == 0\n')
        with open(os.path.join(cdir, "zz_band_third.py"), "w") as f:
            f.write(good_band.replace('NAME = "band_liq_rt"', 'NAME = "zz_band_third"'))
        with open(os.path.join(cdir, "bad_policy.py"), "w") as f:
            f.write('NAME = "bad_policy"\nKIND = "policy"\nRATIONALE = "x"\nCONSUMED_DATA = ()\n'
                    'POLICY = {"ladder": [[0.5, 0.5]]}\n')
        with open(os.path.join(cdir, "_template.py"), "w") as f:
            f.write(tmpl)
        now = 1_800_000_000.0
        assert budget_remaining(rp, now) == config.RESEARCH_MAX_NEW_CANDIDATES_PER_WEEK
        dec = scan(rp, lp, max_new=5, now_s=now, candidates_dir=cdir, trials_path=tp, use_git=False)
        dd = dict((n, (k, why)) for n, k, why in dec)
        print("decisions:", json.dumps({n: k for n, (k, _w) in dd.items()}, indent=1))
        assert dd["band_liq_rt"][0] == "registered" and dd["sell_45m"][0] == "registered"
        assert dd["band_uses_time"][0] == "refused" and "static" in dd["band_uses_time"][1]
        assert dd["band_inert"][0] == "refused" and "inert" in dd["band_inert"][1], dd["band_inert"]
        assert dd["band_undeclared"][0] == "refused" and "undeclared" in dd["band_undeclared"][1], dd["band_undeclared"]
        assert dd["band_hashy"][0] == "refused" and "across processes" in dd["band_hashy"][1], dd["band_hashy"]
        assert dd["bad_policy"][0] == "refused" and "policy invalid" in dd["bad_policy"][1]
        assert dd["zz_band_third"][0] == "refused" and "cap" in dd["zz_band_third"][1], dd["zz_band_third"]
        assert "_template" not in dd
        raw = load_registry_raw(rp)
        new = {c["name"]: c for c in raw["candidates"] if c["module"].startswith("candidates.")}
        assert set(new) == {"band_liq_rt", "sell_45m"}
        e = new["band_liq_rt"]
        assert e["registered_at_event_seq"] == 17 and e["registered_ts"] == now
        assert e["nominated_at_event_seq"] is None and e["status"] == "candidate"
        assert e["requires"] == ["liq_usd", "roundtrip_loss_pct"] and e["proposal"] is None
        assert new["sell_45m"]["policy"] == {"max_hold_s": 2700}
        assert trials.family_count("bands", tp) == 1 and trials.family_count("policies", tp) == 1
        assert budget_remaining(rp, now) == 0 and budget_remaining(rp, now + 8 * 86400) == 2
        # the registered band loads through the registry and the policy through policies.py
        reg2 = B.load_registry(rp)
        assert reg2.get("band_liq_rt") is not None and "band_liq_rt" in reg2.candidates()
        assert POL.load_candidates(rp) == {"sell_45m": {"max_hold_s": 2700}}
        # a second scan re-validates only what is left, registers nothing new (cap now 0)
        dec2 = scan(rp, lp, max_new=5, now_s=now + 60, candidates_dir=cdir, trials_path=tp, use_git=False)
        assert all(k == "refused" for _n, k, _w in dec2) and trials.family_count("bands", tp) == 1
        assert max_event_seq(lp, use_git=False) == 17
        assert max_event_seq(os.path.join(d, "none.csv"), use_git=False) == 0
        assert not [f for f in os.listdir(cdir) if f.endswith(".tmp")]
    print("OK — register.py selftest assertions hold.")
    return 0


def main(argv: list) -> int:
    import time
    now_s = time.time()                       # the ONE wall-clock read
    registry_path = _arg(argv, "--registry", config.REGISTRY_PATH)
    if "--selftest" in argv:
        return _selftest()
    if "--budget-remaining" in argv:
        print(budget_remaining(registry_path, now_s))
        return 0
    if "--scan" in argv:
        max_new = int(_arg(argv, "--max-new", config.RESEARCH_MAX_NEW_CANDIDATES_PER_WEEK))
        scan(registry_path, _arg(argv, "--ledger", config.LEDGER_PATH), max_new, now_s,
             candidates_dir=_arg(argv, "--candidates-dir", CANDIDATES_DIR),
             trials_path=_arg(argv, "--trials", None), use_git="--no-git" not in argv)
        return 0
    print("usage: register.py --scan [--max-new N] [--registry PATH] [--ledger PATH] "
          "[--candidates-dir PATH] [--trials PATH] [--no-git] | --budget-remaining | --selftest")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:] or ["--selftest"]))
