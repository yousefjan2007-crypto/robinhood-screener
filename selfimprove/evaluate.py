"""
Score every pre-declared exit policy on the ledger's real price paths, with the guards.

PORTED from solana_screener/selfimprove/evaluate.py (2026-09-12). Same criterion, same guards;
records are keyed by `token` (EVM address), the trial counter is delegated to
selfimprove/trials.py, and every constant comes from THIS repo's config (entry_bot/stats.py is
imported by sys.path APPEND so it reads our FDR_Q / BOOTSTRAP_REPS / SEED).

THE DECISION CRITERION, stated before any number is computed: a policy is interesting only if its
DAY-CLUSTERED bootstrap 2.5th percentile beats `hold_to_end`'s, at >= config.MIN_BOOTSTRAP_CLUSTERS
distinct alert days, on the PESSIMISTIC within-bar reading. Means are reported for context and are
never the criterion — `~/entry_bot/CLAUDE.md` documents a rule whose mean was +0.577 and whose
clustered lower bound was -0.228 at n=10.

WHY THE CLUSTER IS THE ALERT DAY. The trade is not the independent unit. Alerts arrive in bursts
(the cloud scan fires every few minutes and on solana 43 of 60 live-path survivors landed in a
five-day window), and every token alerted on the same day shares one market regime. An IID
bootstrap over 900 rows would treat one good afternoon as 900 independent observations. This is
the same correction `entry_bot/replay.py:_boot` applies by resampling deployers rather than
trades, which moved a bound from -0.092 to -0.126 on the same rows.

WHY RESOLUTION IS STRATIFIED. `paths.py` falls back to coarser bars for older alerts. An hour-long
bar can contain an entire pump and dump, so its pessimistic/optimistic bracket can span most of
the outcome space — the smoke test in policies.py shows a single ambiguous bar producing -0.510
vs +0.225. Pooling 1m and 1h rows without saying so would quote fake precision, so every table
here reports the resolution mix and the 1m-only subset separately.

TRIALS ACCUMULATE ACROSS RUNS. `selfimprove/trials.json` (config.TRIALS_PATH, owned by
selfimprove/trials.py) carries the total number of policies ever scored by this module. Deflated
Sharpe is computed against that cumulative count, not against len(POLICIES), because a loop that
runs weekly and proposes changes is itself a trial generator. `signal_lab/registry.py` hardcodes
`n_trials = 50` and therefore cannot see its own searching; that is the specific failure this
file is written to avoid.
"""
from __future__ import annotations

import json
import os
import sys
import time
from collections import Counter

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config                                  # noqa: E402

# entry_bot goes at the END, never the front. Both repos have a top-level `config.py`, so
# inserting entry_bot first makes `import config` resolve to ITS config — which is the "dual
# imports" trap entry_bot/CLAUDE.md documents (two module objects for the same name). Appending
# means our config wins and entry_bot/stats.py reads OUR FDR_Q / BOOTSTRAP_REPS / SEED, which is
# exactly the intent: borrow the guards, keep our own constants.
sys.path.append(os.path.join(config.HOME, "entry_bot"))        # for stats.py only

from selfimprove import policies as POL        # noqa: E402

PATHS = os.path.join(config.CACHE_DIR, "paths.jsonl")  # regenerable cache, not committed state
TRIALS = config.TRIALS_PATH


def _ledger_entry_prices() -> dict:
    """Dexscreener entry prices from the ledger (for the pool-mismatch gate), keyed BOTH ways:
    (token, event_seq) for the exact event, and token alone as the fallback for a record with no
    event_seq. A token carries several events (sighting → promotion, hours apart at different
    prices), so the tuple key is the one that is actually right; the plain key is last-wins."""
    try:
        import ledger as LED
        led = LED.load()
        out: dict = {}
        for tok, seq, p in zip(led["token"], led["event_seq"], led["entry_price"]):
            t = str(tok).lower()
            px = LED._num(p)
            out[t] = px
            try:
                out[(t, int(float(seq)))] = px
            except (TypeError, ValueError):
                pass
        return out
    except Exception:
        return {}


def _lookup_entry(ledger_entry: dict, token: str, event_seq) -> float | None:
    try:
        v = ledger_entry.get((token, int(float(event_seq))))
        if v is not None:
            return v
    except (TypeError, ValueError):
        pass
    return ledger_entry.get(token)


def load_paths(path: str | None = None, ledger_entry: dict | None = None) -> list:
    """paths.jsonl records, SANITIZED (2026-09 audit on solana — both modes were found live):
    - bars with v == 0 are dropped: a dead pool can print phantom highs on zero-volume
      bars (STABLECAT showed an 804x high nobody could have transacted at);
    - a record is dropped wholesale when its entry disagrees with the ledger's
      Dexscreener entry price by > config.PATHS_MISPRICED_TOL (3x): GeckoTerminal picked a
      mispriced thin pool (Girlet's was 71x off, inflating every multiple by 71x).
    ledger_entry is injectable for tests ({token: price} and/or {(token, event_seq): price});
    None loads the real ledger."""
    p = path or PATHS
    if not os.path.exists(p):
        return []
    if ledger_entry is None:
        ledger_entry = _ledger_entry_prices()
    tol = float(config.PATHS_MISPRICED_TOL)
    out = []
    with open(p) as fh:
        for line in fh:
            try:
                r = json.loads(line)
            except Exception:
                continue
            try:
                entry = float(r.get("entry", 0) or 0)
            except (TypeError, ValueError):
                continue
            if entry <= 0:
                continue
            token = str(r.get("token") or "").lower()
            le = _lookup_entry(ledger_entry, token, r.get("event_seq"))
            if le and le > 0 and not (1.0 / tol <= entry / le <= tol):
                continue
            # drop only bars whose volume is literally ZERO (phantom prints). A bar
            # with v=None had no volume element in the feed at all — "missing" is not
            # "zero", and dropping it would render a feed failure as silent absence.
            bars = [b for b in r.get("bars") or []
                    if isinstance(b, dict) and (b.get("v") is None or b["v"] > 0)]
            if bars:
                out.append(dict(r, token=token, bars=bars, n_bars=len(bars)))
    return out


def day_of(ts: float) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(ts))     # UTC — the alert day is a UTC day


def cluster_lb(vals: np.ndarray, clusters: list, reps: int | None = None,
               seed: int | None = None) -> tuple[float, int]:
    """(config.BOOTSTRAP_ALPHA quantile of the mean under a CLUSTER bootstrap, n_clusters).

    Implemented via per-cluster (sum, count) so a resample is exact and cheap: the mean of a
    cluster-resample is sum(picked sums)/sum(picked counts), no row concatenation. Batched 250
    reps at a time so the (reps × n_clusters) index matrix never gets large.
    """
    reps = reps or config.BOOTSTRAP_REPS
    vals = np.asarray(vals, dtype=float)
    if vals.size == 0:
        return float("nan"), 0
    groups: dict = {}
    for v, c in zip(vals, clusters):
        groups.setdefault(c, []).append(v)
    sums = np.array([float(np.sum(g)) for g in groups.values()])
    cnts = np.array([len(g) for g in groups.values()], dtype=float)
    ng = sums.size
    if ng == 0:
        return float("nan"), 0
    rng = np.random.default_rng(config.SEED if seed is None else seed)
    parts = []
    for start in range(0, reps, 250):
        k = min(250, reps - start)
        pick = rng.integers(0, ng, size=(k, ng))
        parts.append(sums[pick].sum(axis=1) / cnts[pick].sum(axis=1))
    m = np.concatenate(parts)
    return float(np.quantile(m, config.BOOTSTRAP_ALPHA)), ng


def bump_trials(policy_names) -> int:
    """Cumulative count of DISTINCT hypotheses ever scored by this module.

    Counted per distinct policy NAME ever seen, not per run. Re-scoring the same pre-declared
    family on refreshed data is one hypothesis re-measured, not fifteen new ones — inflating the
    count every week would deflate the Sharpe toward zero and make the gate meaningless. But
    ADDING a policy is a genuinely new trial and is counted forever, including after it is later
    deleted: you cannot un-look at a result. That asymmetry is the whole point, and it is what
    `signal_lab/registry.py`'s hardcoded `n_trials = 50` cannot express.

    The ledger of names lives in selfimprove/trials.py (`trials.bump('policies', names)`), which
    keeps the exit-policy family and the entry-band family in one file so each deflates by its
    own count. Imported lazily so this module still loads (and its self-test still runs) before
    that file exists; if it is missing we fall back to the count of names passed in and SAY SO —
    an under-deflated Sharpe printed with a warning beats a crash in the weekly loop.
    """
    names = list(policy_names)
    try:
        from selfimprove import trials as TR
    except Exception as e:
        print(f"  [evaluate] WARNING selfimprove/trials.py unavailable ({type(e).__name__}: {e}); "
              f"n_trials = {len(set(names))} for THIS run only — the cumulative count is NOT "
              f"being kept, so the deflated Sharpe below is under-deflated.")
        return len(set(names))
    try:
        return int(TR.bump("policies", names))
    except Exception as e:
        print(f"  [evaluate] WARNING trials.bump failed ({type(e).__name__}: {e}); "
              f"n_trials = {len(set(names))} for this run only (under-deflated).")
        return len(set(names))


def score(rows: list, pessimistic: bool = True) -> dict:
    """{policy: {ret: array, days: list, res: list}} for one within-bar reading."""
    out = {}
    for name, pol in POL.POLICIES.items():
        rets, days, res = [], [], []
        for r in rows:
            try:
                v = POL.simulate(r["entry"], r["bars"], pol, pessimistic=pessimistic)
            except Exception:
                v = None
            if v is None or (isinstance(v, float) and np.isnan(v)):
                continue
            rets.append(v)
            days.append(day_of(r["alert_ts"]))
            res.append(r.get("res"))
        out[name] = {"ret": np.array(rets, dtype=float), "days": days, "res": res}
    return out


def paired_reality_check(A: np.ndarray, reps: int | None = None,
                         seed: int | None = None) -> dict:
    """White's reality check, PAIRED, over an advantage matrix A[policy, row].

    entry_bot/stats.reality_check takes {rule: row-mask} and scores each rule's excess over the
    grand mean — correct for SELECTION rules (which rows to trade), wrong here: every exit policy
    trades the SAME rows and differs only in P&L, so all-true masks give every rule an excess of
    exactly zero and a trivial p=1.0000. The first solana version of this file did exactly that.
    The right statistic is each policy's mean ADVANTAGE over hold_to_end on the same rows:
    resample rows in circular blocks (alerts arrive in bursts), recentre by the observed
    advantage to impose the null, and take the max across policies within each resample, so the
    p-value already pays for having searched all of them.

    Returns {p, block, best (row index into A), obs (per-policy observed advantage)}.
    """
    A = np.asarray(A, dtype=float)
    reps = reps or config.BOOTSTRAP_REPS
    obs = A.mean(axis=1)
    nn = A.shape[1]
    block = max(5, min(50, nn // 20))
    rng = np.random.default_rng(config.SEED + 3 if seed is None else seed)
    nblk = int(np.ceil(nn / block))
    null_max = np.empty(reps)
    for i in range(reps):
        starts = rng.integers(0, nn, size=nblk)
        idx = np.concatenate([(np.arange(s, s + block) % nn) for s in starts])[:nn]
        null_max[i] = float(np.max(A[:, idx].mean(axis=1) - obs))
    return {"p": float((null_max >= obs.max()).mean()), "block": block,
            "best": int(np.argmax(obs)), "obs": obs}


def table(rows: list, label: str, n_trials: int) -> None:
    import stats as ST                              # entry_bot/stats.py (sys.path APPEND above)
    if not rows:
        print(f"\n{label}: no priced rows yet")
        return
    print(f"\n{'='*104}\n{label}   n={len(rows)}   "
          f"resolution mix={dict(Counter(r.get('res') for r in rows))}   "
          f"days={len({day_of(r['alert_ts']) for r in rows})}\n{'='*104}")
    pess, opt = score(rows, True), score(rows, False)
    base_lb, _ = cluster_lb(pess["hold_to_end"]["ret"], pess["hold_to_end"]["days"])
    print(f"  {'policy':<24s} {'mean(pess)':>11s} {'mean(opt)':>10s} {'dayLB(pess)':>12s} "
          f"{'days':>5s} {'>0':>6s} {'vs hold':>9s}")
    ranked = []
    for name in POL.POLICIES:
        p, o = pess[name]["ret"], opt[name]["ret"]
        if p.size == 0:
            continue
        lb, nd = cluster_lb(p, pess[name]["days"])
        ranked.append((lb, name, p, o, nd))
    if not ranked:
        print("  no policy produced a finite return on these rows")
        return
    for lb, name, p, o, nd in sorted(ranked, key=lambda x: -x[0]):
        star = " *" if (lb > base_lb and nd >= config.MIN_BOOTSTRAP_CLUSTERS) else ""
        print(f"  {name:<24s} {p.mean():>+11.3f} {o.mean():>+10.3f} {lb:>+12.3f} "
              f"{nd:>5} {np.mean(p > 0):>6.1%} {lb - base_lb:>+9.3f}{star}")
    print(f"  baseline hold_to_end dayLB = {base_lb:+.3f}; "
          f"'*' = beats it with >= {config.MIN_BOOTSTRAP_CLUSTERS} day-clusters")

    # The paired reality check (see paired_reality_check for why the entry_bot one is the
    # wrong shape here). Needs the policies to be scored on the SAME rows as hold_to_end.
    best = max(ranked, key=lambda x: x[0])
    base = pess["hold_to_end"]["ret"]
    names = [n for _, n, _, _, _ in ranked
             if n != "hold_to_end" and pess[n]["ret"].size == base.size]
    if names and base.size >= 30:
        A = np.array([pess[n]["ret"] - base for n in names])
        rc = paired_reality_check(A)
        print(f"  PAIRED reality check (advantage over hold_to_end, block={rc['block']}): best "
              f"{names[rc['best']]!r} +{rc['obs'].max():.3f}, p={rc['p']:.4f} over "
              f"{len(names)} policies")
    else:
        print("  paired reality check skipped (need >= 30 priced rows)")
    # DSR ON ROWS, NOT DAYS. deflated_sharpe_ratio needs the per-observation return series for
    # its skew/kurtosis correction; the day-cluster structure is handled by the bound above,
    # not here. n_trials is the CUMULATIVE count from trials.json, never len(POLICIES).
    dsr = ST.deflated_sharpe_ratio(best[2].tolist(), n_trials)
    print(f"  deflated Sharpe of {best[1]!r} at {n_trials} CUMULATIVE trials: {dsr:.3f} "
          f"(signal_lab promotes at >= 0.95)")


def main() -> None:
    rows = load_paths()
    if not rows:
        print(f"no paths yet — run `python3 selfimprove/backfill.py` first ({PATHS})")
        return
    n_trials = bump_trials(list(POL.POLICIES))
    print(f"priced rows: {len(rows)}   policies this run: {len(POL.POLICIES)}   "
          f"cumulative trials: {n_trials}")
    table(rows, "ALL priced alerts", n_trials)
    fine = [r for r in rows if r.get("res") == "1m"]
    if fine:
        table(fine, "1m-resolution subset (tightest bracket)", n_trials)
    a = [r for r in rows if (r.get("tier") or "") == "A"]
    if len(a) >= 5:
        table(a, "tier A only (the alerted band)", n_trials)


def _selftest() -> None:
    """OFFLINE. Three synthetic paths through every policy and control, the cluster bound on a
    planted effect and on a null, the sanitizer, and the paired reality check on pure noise."""
    import tempfile

    def bar(ts, o, h, l, c, v=1.0):
        return {"ts": ts, "o": o, "h": h, "l": l, "c": c, "v": v}

    print("evaluate self-test (offline)")
    # 1. three paths: up-then-dead (the ledger's characteristic shape), flat, gap-through-stop
    up_then_dead = [bar(0, 1.0, 1.6, 0.95, 1.5), bar(1800, 1.5, 2.4, 1.4, 2.2),
                    bar(3600, 2.2, 2.3, 0.4, 0.5), bar(7200, 0.5, 0.5, 0.01, 0.02)]
    flat = [bar(0, 1.0, 1.01, 0.99, 1.0), bar(600, 1.0, 1.01, 0.99, 1.0),
            bar(1200, 1.0, 1.01, 0.99, 1.0)]
    gap_stop = [bar(0, 1.0, 1.05, 0.98, 1.0), bar(60, 0.30, 0.32, 0.20, 0.25),
                bar(120, 0.25, 0.26, 0.10, 0.12)]      # opens 70% down: fill is the open, not the stop
    fam = dict(POL.POLICIES, **POL.CONTROLS)
    print(f"  {'policy':<24s} {'up→dead':>9s} {'flat':>9s} {'gap-stop':>9s}   (pessimistic)")
    for name, pol in fam.items():
        vals = [POL.simulate(1.0, path, pol, pessimistic=True) for path in (up_then_dead, flat, gap_stop)]
        assert all(np.isfinite(v) for v in vals), (name, vals)
        print(f"  {name:<24s} {vals[0]:>+9.3f} {vals[1]:>+9.3f} {vals[2]:>+9.3f}")
    cost = POL.round_trip_cost()
    assert POL.simulate(1.0, up_then_dead, POL.POLICIES["hold_to_end"]) < -0.9
    assert abs(POL.simulate(1.0, flat, POL.POLICIES["hold_to_end"]) - (-cost)) < 1e-9
    # a stop through a gap must fill at the OPEN (0.30), never at the stop level (0.50)
    g = POL.simulate(1.0, gap_stop, POL.POLICIES["stop_50"])
    assert abs(g - (0.30 * (1 - cost) - 1.0)) < 1e-9, g
    print(f"  gap-through-stop fills at the open: stop_50 = {g:+.3f} (stop level would be "
          f"{0.5*(1-cost)-1:+.3f})")

    # 2. score() over rows built from those paths, every policy finite
    rows = [{"token": f"0x{i:040x}", "entry": 1.0, "alert_ts": 1_700_000_000 + i * 86400,
             "res": "1m", "tier": "A", "bars": [up_then_dead, flat, gap_stop][i % 3]}
            for i in range(9)]
    sc = score(rows, True)
    assert all(v["ret"].size == 9 for v in sc.values())
    print(f"  score(): {len(sc)} policies × {len(rows)} rows, all finite")

    # 3. cluster_lb: planted +0.5 mean over 50 clusters clears zero; a zero-mean fixture does not
    rng = np.random.default_rng(config.SEED)
    clusters = [f"d{i}" for i in range(50) for _ in range(6)]
    planted = rng.normal(0.5, 1.0, size=300)
    lb_p, ng = cluster_lb(planted, clusters, reps=2000)
    null = rng.normal(0.0, 1.0, size=300)
    lb_0, _ = cluster_lb(null, clusters, reps=2000)
    print(f"  cluster_lb: planted +0.5 → LB {lb_p:+.3f} over {ng} clusters (>0: {lb_p > 0});  "
          f"zero-mean → LB {lb_0:+.3f} (>0: {lb_0 > 0})")
    assert ng == 50 and lb_p > 0 and lb_0 < 0
    # determinism: the same seed gives the same bound
    assert cluster_lb(planted, clusters, reps=2000)[0] == lb_p

    # 4. load_paths sanitizer: v=0 dropped, v=None kept, 3x mispriced record dropped
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "paths.jsonl")
        recs = [
            {"token": "0xaa", "event_seq": 1, "entry": 1.0, "alert_ts": 1.0, "res": "1m", "tier": "A",
             "bars": [bar(0, 1, 1, 1, 1, 0.0), bar(60, 1, 1, 1, 1, None), bar(120, 1, 1, 1, 1, 2.0)]},
            {"token": "0xbb", "event_seq": 2, "entry": 71.0, "alert_ts": 1.0, "res": "1m", "tier": "A",
             "bars": [bar(0, 71, 71, 71, 71)]},                  # 71x off the ledger → dropped
            {"token": "0xcc", "event_seq": 3, "entry": 0.0, "alert_ts": 1.0, "res": "1m", "tier": "A",
             "bars": [bar(0, 1, 1, 1, 1)]},                      # no entry → dropped
            {"token": "0xdd", "event_seq": 4, "entry": 2.5, "alert_ts": 1.0, "res": "1m", "tier": "A",
             "bars": [bar(0, 1, 1, 1, 1)]},                      # 2.5x: inside the 3x tolerance
        ]
        with open(p, "w") as fh:
            for r in recs:
                fh.write(json.dumps(r) + "\n")
            fh.write("not json\n")
        got = load_paths(p, ledger_entry={"0xaa": 1.0, "0xbb": 1.0, "0xdd": 1.0,
                                          ("0xdd", 4): 1.0})
        toks = [r["token"] for r in got]
        print(f"  load_paths: kept {toks}; 0xaa bars after sanitizing = {got[0]['n_bars']} "
              f"(v=0 dropped, v=None kept)")
        assert toks == ["0xaa", "0xdd"] and got[0]["n_bars"] == 2
        assert got[0]["bars"][0]["v"] is None

    # 5. the paired reality check on a NOISE universe: 12 zero-skill policies over 240 rows
    # must NOT come out significant, even though the best of them looks good on its own
    A = rng.normal(0.0, 1.0, size=(12, 240))
    rc = paired_reality_check(A, reps=1000)
    print(f"  paired reality check on 12 noise policies: best obs +{rc['obs'].max():.3f}, "
          f"block={rc['block']}, p={rc['p']:.3f}")
    assert rc["p"] > 0.05, rc
    # and a planted +0.3 advantage on one policy IS detected
    A2 = A.copy()
    A2[3] += 0.3
    rc2 = paired_reality_check(A2, reps=1000)
    print(f"  ...with +0.3 planted on policy 3: best={rc2['best']}, p={rc2['p']:.3f}")
    assert rc2["best"] == 3 and rc2["p"] < 0.05, rc2

    # 6. table() end to end on synthetic rows (30+ so the reality check runs), then trials
    rows30 = [{"token": f"0x{i:040x}", "entry": 1.0, "alert_ts": 1_700_000_000 + i * 43200,
               "res": "1m", "tier": "A", "bars": [up_then_dead, flat, gap_stop][i % 3]}
              for i in range(36)]
    table(rows30, "SYNTHETIC 36 rows (self-test — not data)", n_trials=len(POL.POLICIES))
    try:
        from selfimprove import trials as TR   # noqa: F401
        print(f"\n  trials.py present: bump_trials would count {len(POL.POLICIES)} names "
              f"(not bumped by the self-test)")
    except Exception as e:
        print(f"\n  SKIP bump_trials: selfimprove/trials.py not importable ({type(e).__name__})")
    print("  OK — self-test assertions hold.")


if __name__ == "__main__":
    _selftest()                      # always, offline
    if os.path.exists(PATHS) and "--selftest" not in sys.argv[1:]:
        print("\n=== live paths ===")
        main()
    else:
        print(f"\n(no {PATHS} yet — live evaluation skipped; run selfimprove/backfill.py first)")
