"""
The self-improvement loop for the EXIT arm: re-score every policy on LIVE evidence, judge ONE
pre-registered hypothesis, and change the champion only through a gate that prices in every
hypothesis ever tried. Ported from solana_screener/selfimprove/improve.py (2026-09-12); the
port is autonomous behind the gate (--apply writes selfimprove/champion.json through
champion.write_state, the sole writer) and adds a pre-registered one-shot DEMOTION test.

WHAT "SELF-IMPROVING" CAN HONESTLY MEAN HERE, because the phrase invites the exact failure this
workspace exists to avoid. It does NOT mean an agent that searches harder until it finds alpha —
on the sibling screener's ledger, 0 of 1,089 bucket rules survived multiple-testing correction at
any q. It means a loop that:

  1. accumulates genuinely OUT-OF-SAMPLE evidence 24/7 (selfimprove/livebook.py),
  2. re-scores a PRE-DECLARED family on it,
  3. changes a champion only through a gate that prices in every hypothesis ever tried,
  4. and writes a proposal a human reads — every change it makes is a file diff with evidence.

The improvement is in the ESTIMATE, and in the system's willingness to be proven wrong. That is
the only kind that compounds instead of overfitting.

=========================== THE FAILURE MODE THIS FILE IS BUILT AGAINST ===========================
A loop that re-scores 16 correlated policies every week and adopts whichever currently leads is
running a maximum over correlated series. Under a PURE NULL that maximum drifts upward forever,
and the loop will confidently converge on noise. Three guards, and none is optional:

  * The champion changes only on a PAIRED test, never on a leaderboard position. Every policy
    trades the same entries by construction (livebook takes ONE shared buy fill), so
    challenger-minus-champion is a paired difference with far lower variance than either series
    — that pairing is the live book's real statistical advantage and it is what makes a decision
    possible at n in the hundreds rather than the tens of thousands.
  * `times led` is reported across runs precisely so a lead can be seen for what it is. A
    policy that has led 6 weeks running out of noise looks identical to one that leads for a
    reason; only the paired bound separates them. Leading is not evidence.
  * Trials accumulate FOREVER via selfimprove/trials.json, including for policies later deleted.
    You cannot un-look at a result. signal_lab/registry.py hardcodes n_trials = 50 and therefore
    cannot see its own searching; that is the specific bug this file refuses to inherit.
==================================================================================================

THE GATE IS DELIBERATELY HARDER THAN "BEATS THE CHAMPION". A policy that merely loses less than
the champion does not justify automating anything — the honest prior is that this whole class of
trade is -EV. So promotion also requires the challenger to be profitable ON ITS OWN bound and to
beat the best NEGATIVE CONTROL on its own bound. The loop is allowed to promote; it is simply
not allowed to promote something that loses money slowly.

PROMOTION IS FORWARD-ONLY AND JUDGED ONCE. The leaderboard winner is NOMINATED (free — a
nomination is not a claim) and then tested only on the pre-registered PREFIX of its forward
sample: the earliest positions with alert_seq > nominated_at_alert_seq AND opened_ts >
nominated_ts (the alert_seq counter restarts if the book is rebuilt), in opened_ts order, until
both IMPROVE_FWD_MIN_POSITIONS and IMPROVE_FWD_MIN_DAYS are met. Later positions are excluded
even after they complete, so the bound is computed exactly once on exactly one sample —
recomputing a 2.5% bound on a growing sample and promoting at the first pass is a sequential
test with inflated type-I error. A nominee that fails on its matured prefix is terminally
FAILED and cannot be renominated inside IMPROVE_RENOMINATE_COOLDOWN_DAYS.

DEMOTION IS THE SAME TEST POINTED AT THE CHAMPION. A promoted (non-default) champion is judged
ONCE on its own forward prefix after promoted_at_alert_seq / promoted_at_ts: if its own
day-clustered bound fails to beat the best control's on those rows it reverts to
config.IMPROVE_DEFAULT_EXIT_CHAMPION. Reversal needs no positive proof, only failure to keep
beating the control.

APPARATUS FAULTS VOID THE RUN. A control that is INERT (bit-identical to the champion — it
cannot detect anything) or PROFITABLE on its own clustered bound means the gate is measuring
its own machinery: the per-policy table is suppressed and no number from the run may be quoted.

    python3 selfimprove/improve.py                 # dry: evaluate, decide, proposal + history
    python3 selfimprove/improve.py --apply --send  # the Sunday job: write champion.json, alert
    python3 selfimprove/improve.py --summary-json  # data/livebook_summary.json only (no gate)
    python3 selfimprove/improve.py --band-scorecard  # the PAPER GATE alone (see paper_gate); writes nothing
    python3 selfimprove/improve.py --quiet         # same as dry, no stdout

THE PAPER GATE (paper_gate, 2026-09-17) is the ONE judge of the paper test — a pre-registered
window of config.PAPER_GATE_WINDOW_DAYS over one named (band, policy) pair, judged once as
PASS / FAIL / VOID with the statistics above and nothing new. It is not a promotion: it writes
only the `paper:` / `paper_verdict:` lines of trials.json, and only on the --apply path — every
read-only caller computes the same verdict and prints it "(not recorded: dry)".

No wall-clock in any compute path: main() reads time.time() ONCE and threads now_s through.
Not financial advice.
"""
from __future__ import annotations

import calendar
import csv
import json
import math
import os
import sys
import time
from collections import Counter
from statistics import NormalDist

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config                                   # noqa: E402
import alerts                                   # noqa: E402
from selfimprove import champion                # noqa: E402
from selfimprove import dsr as DSR              # noqa: E402  (the vendored Deflated Sharpe: numpy only)
from selfimprove import evaluate as EV          # noqa: E402
from selfimprove import livebook as LB          # noqa: E402
from selfimprove import policies as POL         # noqa: E402
from selfimprove import trials as TR            # noqa: E402

RANDOM_EXIT_ADMISSIBLE = ("random_exit", "no_route")   # the rows ctl_random_exit actually acted on
_CONTROLS = getattr(POL, "CONTROLS", {})


# ── the book → aligned return arrays ─────────────────────────────────────────────
def _day(ts) -> str:
    return EV.day_of(float(ts))


def _names() -> list:
    return list(POL.POLICIES) + list(_CONTROLS)


def _scorable(p) -> bool:
    """A COMPLETED position that can be scored: done, a real cost basis, and retired neither as
    suspect (quote-integrity gate) nor unpriced (30 consecutive deferred quotes)."""
    return (isinstance(p, dict) and bool(p.get("done"))
            and (LB._num(p.get("cost_usd"), 0.0) or 0.0) > 0
            and not p.get("suspect") and not p.get("unpriced"))


def _completed(book: dict) -> list:
    """Scorable positions in (opened_ts, alert_seq) order — the order every prefix is cut in."""
    rows = [p for p in book.values() if _scorable(p)]
    rows.sort(key=lambda p: (float(LB._num(p.get("opened_ts"), 0.0) or 0.0),
                             int(LB._num(p.get("alert_seq"), 0) or 0)))
    return rows


def _state_ret(p: dict, name: str, st) -> float:
    """One policy's realized return on one position, or NaN when the state is not evidence:
    missing; BACKFILLED mid-flight (fabricated economics, tradeable for continuity, never
    scoreable); GAPPED (a market exit after a tick gap over LIVEBOOK_MAX_SCORABLE_GAP_S is not
    what the policy would have done — the Mac was asleep); or, for ctl_random_exit, a close the
    control did not itself decide (solana's poisoned era: the live control read as hold_to_end's
    alias on 294/294 positions — only rows it acted on, or dead-coin closes identical for
    everyone by construction, are admissible)."""
    if not isinstance(st, dict) or not st.get("closed"):
        return float("nan")
    if st.get("backfilled_ts") is not None or st.get("gapped"):
        return float("nan")
    if name == "ctl_random_exit" and st.get("close_reason") not in RANDOM_EXIT_ADMISSIBLE:
        return float("nan")
    cost = float(p["cost_usd"])
    return float(st.get("realized_usd") or 0.0) / cost - 1.0


def live_returns(book: dict | None = None, counts: dict | None = None) -> tuple:
    """({policy_or_control: np.ndarray aligned over completed positions}, days, meta).

    Only COMPLETED positions count. An open position has no return yet, and marking it at the
    current price would let a policy's score drift with the market between runs. `days` is the
    UTC day of opened_ts (the cluster); `meta` carries {key, token, event_seq, event_kind, tier,
    ledger_tier, opened_ts, alert_seq, alert_ts} per row. Pass a dict as `counts` to receive
    n_suspect / n_unpriced (position-level) and n_gapped / n_backfilled per policy.
    """
    if book is None:
        book = LB._load(LB.BOOK_PATH, {})
    positions = [p for p in book.values() if isinstance(p, dict)]
    done = [p for p in positions if p.get("done")]
    rows = _completed(book)
    days = [_day(p["opened_ts"]) for p in rows]
    meta = [{"key": LB._pos_key(p["token"], p["event_seq"]), "token": p["token"],
             "event_seq": p["event_seq"], "event_kind": str(p.get("event_kind") or ""),
             "tier": str(p.get("tier") or ""), "ledger_tier": str(p.get("ledger_tier") or ""),
             "opened_ts": float(p["opened_ts"]), "alert_seq": int(p.get("alert_seq") or 0),
             "alert_ts": LB._num(p.get("alert_ts"))} for p in rows]
    rets: dict = {}
    n_gapped: dict = {}
    n_backfilled: dict = {}
    n_inadmissible: dict = {}
    for name in _names():
        arr, ng, nb, ni = [], 0, 0, 0
        for p in rows:
            st = (p.get("policies") or {}).get(name)
            if isinstance(st, dict):
                if st.get("backfilled_ts") is not None:
                    nb += 1
                elif st.get("gapped"):
                    ng += 1
                elif name == "ctl_random_exit" and st.get("close_reason") not in RANDOM_EXIT_ADMISSIBLE:
                    ni += 1
            arr.append(_state_ret(p, name, st))
        rets[name] = np.array(arr, dtype=float)
        n_gapped[name], n_backfilled[name], n_inadmissible[name] = ng, nb, ni
    if counts is not None:
        counts.update({"n_positions": len(positions), "n_done": len(done),
                       "n_open": len(positions) - len(done), "n_scorable": len(rows),
                       "n_suspect": sum(1 for p in done if p.get("suspect")),
                       "n_unpriced": sum(1 for p in done if p.get("unpriced")),
                       "n_gapped": n_gapped, "n_backfilled": n_backfilled,
                       "n_inadmissible": n_inadmissible})
    return rets, days, meta


def _in_stratum_a(p: dict) -> bool:
    """A stratum = a champion promotion, or a first sighting the champion band tiered A."""
    kind = str(p.get("event_kind") or "")
    return kind == "promotion" or (kind == "first_sighting" and str(p.get("tier") or "") == "A")


def _a_mask(meta: list) -> np.ndarray:
    return np.array([_in_stratum_a(m) for m in meta], dtype=bool)


# ── the bounds ───────────────────────────────────────────────────────────────────
def cluster_boot(vals: np.ndarray, clusters: list, reps: int | None = None,
                 seed: int | None = None) -> tuple:
    """NaN-aware day-clustered bootstrap config.BOOTSTRAP_ALPHA quantile of the mean, and the
    cluster count. Delegates to evaluate.cluster_lb on the non-NaN rows."""
    vals = np.asarray(vals, dtype=float)
    ok = ~np.isnan(vals)
    if not ok.any():
        return float("nan"), 0
    return EV.cluster_lb(vals[ok], [c for c, k in zip(clusters, ok) if k], reps=reps, seed=seed)


def paired_lb(challenger: np.ndarray, champion_r: np.ndarray, clusters: list,
              seed: int | None = None) -> float:
    """Day-clustered lower bound on the PAIRED difference (challenger - champion) over the rows
    where BOTH are scorable. Paired because both policies traded the same entries at the same
    instants, so the position-level difference removes all the token-selection variance the two
    series share. This is the whole reason a live book can decide anything at n in the hundreds."""
    d = np.asarray(challenger, dtype=float) - np.asarray(champion_r, dtype=float)
    lb, _ = cluster_boot(d, clusters, seed=(config.SEED + 17 if seed is None else seed))
    return lb


def _day_means(r: np.ndarray, clusters: list) -> list:
    dm: dict = {}
    for v, d in zip(r, clusters):
        if not np.isnan(v):
            dm.setdefault(d, []).append(v)
    return [float(np.mean(v)) for v in dm.values()]


def _dsr(r: np.ndarray, clusters: list, n_trials: int) -> float:
    """Deflated Sharpe on DAY MEANS, not rows. selfimprove/dsr.py scales the z-score by
    sqrt(n-1), so feeding 639 rows where the honest cluster count is 41 days inflates the
    statistic ~4x (measured false-pass rate of DSR >= 0.95 under a zero-mean day-clustered null:
    17.0% over rows, 0.0% over day means). NaN fails closed on any failure."""
    try:
        return float(DSR.deflated_sharpe_ratio(_day_means(r, clusters), int(n_trials)))
    except Exception:
        return float("nan")


def implied_sharpe_bar(n_days: int, n_trials: int, gate: float | None = None) -> float:
    """The per-day Sharpe a NORMAL day-mean series needs for the DSR gate to pass at these
    floors — the bar the gate actually sets, printed beside every DSR so a reader can see how
    far a policy is from it. Same expected-maximum formula as selfimprove/dsr.py, solved by
    bisection with skew 0 / kurtosis 3 (a real skewed series needs MORE)."""
    gate = config.IMPROVE_DSR_GATE if gate is None else gate
    n, z = int(n_days), max(int(n_trials), 1)
    if n < 8:
        return float("nan")
    nd = NormalDist()
    e = 0.5772156649
    emax = 0.0 if z == 1 else (1.0 / math.sqrt(n)) * (
        (1 - e) * nd.inv_cdf(1 - 1.0 / z) + e * nd.inv_cdf(1 - 1.0 / (z * math.e)))

    def _dsr_of(sr):
        return nd.cdf((sr - emax) * math.sqrt(n - 1) / math.sqrt(1 + sr * sr / 2.0))

    lo, hi = 0.0, 20.0
    if _dsr_of(hi) < gate:
        return float("inf")
    for _ in range(80):
        mid = 0.5 * (lo + hi)
        if _dsr_of(mid) >= gate:
            hi = mid
        else:
            lo = mid
    return float(hi)


# ── scoring tables ───────────────────────────────────────────────────────────────
def _row(r: np.ndarray, clusters: list, n_trials: int) -> dict:
    lb, nd = cluster_boot(r, clusters)
    ok = ~np.isnan(r)
    return {"n": int(ok.sum()),
            "mean": float(np.nanmean(r)) if ok.any() else float("nan"),
            "median": float(np.nanmedian(r)) if ok.any() else float("nan"),
            # over non-NaN only: nanmean(r > 0) would count a NaN position as a loss
            "win_rate": float(np.mean(r[ok] > 0)) if ok.any() else float("nan"),
            "day_lb": lb, "n_days": nd, "dsr": _dsr(r, clusters, n_trials)}


def _score_table(rets: dict, days: list, mask: np.ndarray, champ: str, n_trials: int,
                 counts: dict | None = None) -> dict:
    """Per-policy and per-control rows on the masked positions, controls on the SAME rows, plus
    the inert-control self-test. Returns {policies, controls, inert_controls, n, n_days}."""
    idx = np.where(np.asarray(mask, dtype=bool))[0]
    d = [days[i] for i in idx]
    sub = {k: v[idx] for k, v in rets.items()}
    base = sub.get(champ)
    counts = counts or {}
    out = {"policies": {}, "controls": {}, "inert_controls": [], "n": int(len(idx)),
           "n_days": len(set(d))}

    def _extra(name: str) -> dict:
        return {"n_gapped": int((counts.get("n_gapped") or {}).get(name, 0)),
                "n_backfilled": int((counts.get("n_backfilled") or {}).get(name, 0)),
                "n_suspect": int(counts.get("n_suspect", 0)),
                "n_unpriced": int(counts.get("n_unpriced", 0))}

    for name in POL.POLICIES:
        r = sub[name]
        row = _row(r, d, n_trials)
        if base is not None and name != champ:
            both = ~(np.isnan(r) | np.isnan(base))
            row["paired_mean"] = float(np.mean((r - base)[both])) if both.any() else float("nan")
            row["paired_lb"] = paired_lb(r, base, d) if both.any() else float("nan")
        row.update(_extra(name))
        out["policies"][name] = row
    # Negative controls on the SAME rows. If one of these clears the gate, the gate is measuring
    # its own machinery and decide() refuses to emit anything. They are scored but NOT eligible
    # to be promoted, which is why they live in POL.CONTROLS rather than POL.POLICIES.
    for cname in _CONTROLS:
        cr = sub[cname]
        if not (~np.isnan(cr)).any():
            continue
        row = _row(cr, d, n_trials)
        if base is not None:
            both = ~(np.isnan(cr) | np.isnan(base))
            row["paired_mean"] = float(np.mean((cr - base)[both])) if both.any() else float("nan")
            row["paired_lb"] = paired_lb(cr, base, d) if both.any() else float("nan")
            # Apparatus self-test (solana 2026-09): random_exit was implemented only in the bar
            # simulator, so the LIVE control was bit-identical to the champion on 294/294
            # positions — an alias of the champion cannot detect anything.
            if both.any() and np.allclose(cr[both], base[both]):
                out["inert_controls"].append(cname)
        row.update(_extra(cname))
        out["controls"][cname] = row
    return out


def _forward_prefix(book: dict, meta: list, vals: np.ndarray, days: list, after_seq,
                    after_ts, base_mask: np.ndarray, stratum_a_only: bool) -> dict:
    """The PRE-REGISTERED PREFIX: the earliest forward positions (opened_ts order) with
    alert_seq > after_seq AND opened_ts > after_ts whose value is scorable, fixed by rule —
    the walk stops the moment both floors are met, so later positions are excluded even after
    they complete. A still-OPEN forward position that opened before the prefix's last row would
    enter the prefix once it completes, so the prefix is not `matured` until none is ahead."""
    fmask = np.zeros(len(meta), dtype=bool)
    pref_days: set = set()
    last_opened = None
    try:
        after_seq = int(after_seq)
        after_ts = float(after_ts if after_ts is not None else -1.0)
    except (TypeError, ValueError):
        return {"mask": fmask, "n": 0, "n_days": 0, "matured": False, "n_open_ahead": 0,
                "floors": False}
    for j, m in enumerate(meta):
        if not base_mask[j] or m["alert_seq"] <= after_seq or m["opened_ts"] <= after_ts:
            continue
        if np.isnan(vals[j]):
            continue
        if fmask.sum() >= config.IMPROVE_FWD_MIN_POSITIONS and \
                len(pref_days) >= config.IMPROVE_FWD_MIN_DAYS:
            break
        fmask[j] = True
        pref_days.add(days[j])
        last_opened = m["opened_ts"]
    n, nd = int(fmask.sum()), len(pref_days)
    floors = n >= config.IMPROVE_FWD_MIN_POSITIONS and nd >= config.IMPROVE_FWD_MIN_DAYS
    open_ahead = 0
    if floors and last_opened is not None:
        for p in book.values():
            if not isinstance(p, dict) or p.get("done"):
                continue
            if stratum_a_only and not _in_stratum_a(p):
                continue
            seq = int(LB._num(p.get("alert_seq"), 0) or 0)
            ots = float(LB._num(p.get("opened_ts"), 0.0) or 0.0)
            if seq > after_seq and ots > after_ts and ots < last_opened:
                open_ahead += 1
    return {"mask": fmask, "n": n, "n_days": nd, "floors": floors,
            "n_open_ahead": open_ahead, "matured": bool(floors and open_ahead == 0)}


def _exit_arm() -> dict:
    return dict(champion.state()["exit"])


def evaluate_all(now_s: float, book: dict | None = None,
                 stratum: str | None = None, record_paper: bool = False) -> dict:
    """Score the family on the live book. Pure except for the trial bump (scoring IS a trial —
    the count only grows and is idempotent for the same names). `record_paper` is the paper
    gate's apply-path flag and is False everywhere but `improve.py --apply` (see paper_gate)."""
    stratum = (stratum or config.IMPROVE_SCORE_STRATUM or "pooled")
    if book is None:
        book = LB._load(LB.BOOK_PATH, {})
    counts: dict = {}
    rets, days, meta = live_returns(book, counts)
    arm = _exit_arm()
    champ = arm.get("champion") or config.IMPROVE_DEFAULT_EXIT_CHAMPION
    if champ not in POL.POLICIES:
        print(f"  [improve] champion {champ!r} is not a registered policy; scoring against "
              f"{config.IMPROVE_DEFAULT_EXIT_CHAMPION}")
        champ = config.IMPROVE_DEFAULT_EXIT_CHAMPION
    TR.bump("policies", list(POL.POLICIES))
    n_trials = TR.family_count("policies")
    n_all = len(meta)
    amask = _a_mask(meta)
    a_only = stratum == "A"
    score_mask = amask if a_only else np.ones(n_all, dtype=bool)
    # max over the WHOLE book, open positions included: an open position at nomination time
    # predates the pre-registration and must never count as forward evidence.
    all_pos = [p for p in book.values() if isinstance(p, dict)]
    max_seq = max([int(LB._num(p.get("alert_seq"), -1) or -1) for p in all_pos] or [-1])
    max_opened = max([float(LB._num(p.get("opened_ts"), 0.0) or 0.0) for p in all_pos] or [0.0])
    res = {"ts": float(now_s), "champion": champ, "stratum": stratum,
           "n_positions": int(score_mask.sum()),
           "n_days": len({days[i] for i in np.where(score_mask)[0]}),
           "n_trials": int(n_trials), "policies": {}, "controls": {}, "inert_controls": [],
           "max_alert_seq": max_seq, "max_opened_ts": max_opened, "forward_only": False,
           "nomination": None, "demotion": None, "strata": {}, "counts": counts,
           "exit_state": {k: arm.get(k) for k in (
               "champion", "promoted_ts", "promoted_at_alert_seq", "promoted_at_ts", "previous",
               "nominee", "nominated_at_alert_seq", "nominated_ts", "failed_nominee",
               "failed_ts", "failed_at_alert_seq", "demotion_judged")},
           "dsr_bar": {"n_days": int(config.IMPROVE_FWD_MIN_DAYS), "n_trials": int(n_trials),
                       "sharpe": implied_sharpe_bar(config.IMPROVE_FWD_MIN_DAYS, n_trials)}}
    try:
        res["book"] = LB.live_stats_dict(now_s)
    except Exception as exc:
        res["book"] = {"error": str(exc)[:120]}
    # the paper gate rides every evaluation ("none registered" until the operator's window
    # commit) and never touches the verdict below. Only the apply path mints its one-shot
    # trials.json lines — every other evaluation computes and prints them, and writes nothing.
    try:
        res["paper_gate"] = paper_gate(now_s, book=book, counts=counts, record=record_paper)
    except Exception as exc:
        print(f"  [improve] paper gate failed: {type(exc).__name__}: {exc}")
        res["paper_gate"] = {"status": "error", "line": f"unavailable ({type(exc).__name__}: {str(exc)[:60]})"}
    if n_all == 0:
        return res
    main = _score_table(rets, days, score_mask, champ, n_trials, counts)
    res["policies"], res["controls"] = main["policies"], main["controls"]
    res["inert_controls"] = main["inert_controls"]
    if champ in res["policies"]:
        res["champ_lb"] = res["policies"][champ]["day_lb"]
    a_tab = _score_table(rets, days, amask, champ, n_trials, counts)
    res["strata"]["A"] = {"n": a_tab["n"], "n_days": a_tab["n_days"],
                          "policies": a_tab["policies"], "controls": a_tab["controls"],
                          "inert_controls": a_tab["inert_controls"]}

    # ── forward-only nomination: the nominee's promotion-facing row is recomputed on its
    # pre-registered prefix; once matured, that sample REPLACES its table row.
    nominee = arm.get("nominee")
    nom_seq, nom_ts = arm.get("nominated_at_alert_seq"), arm.get("nominated_ts")
    if nominee and nominee in rets and nom_seq is not None:
        pref = _forward_prefix(book, meta, rets[nominee], days, nom_seq, nom_ts, score_mask, a_only)
        res["nomination"] = {"nominee": nominee, "nominated_at_alert_seq": nom_seq,
                             "nominated_ts": nom_ts, "n_forward": pref["n"],
                             "days_forward": pref["n_days"], "matured": pref["matured"],
                             "n_open_ahead": pref["n_open_ahead"],
                             "floors": {"positions": int(config.IMPROVE_FWD_MIN_POSITIONS),
                                        "days": int(config.IMPROVE_FWD_MIN_DAYS)}}
        if pref["matured"]:
            fm = pref["mask"]
            tab = _score_table(rets, days, fm, champ, n_trials, counts)
            row = tab["policies"].get(nominee)
            if row is not None:
                row["forward_only"] = True
                res["policies"][nominee] = row
                res["forward_only"] = True
                res["nomination"]["controls_on_prefix"] = {
                    c: r["day_lb"] for c, r in tab["controls"].items()}

    # ── the pre-registered one-shot DEMOTION test on a promoted champion's own prefix
    default = config.IMPROVE_DEFAULT_EXIT_CHAMPION
    if (champ != default and arm.get("demotion_judged") is None
            and arm.get("promoted_at_alert_seq") is not None):
        pref = _forward_prefix(book, meta, rets[champ], days, arm["promoted_at_alert_seq"],
                               arm.get("promoted_at_ts"), score_mask, a_only)
        dem = {"champion": champ, "promoted_at_alert_seq": arm["promoted_at_alert_seq"],
               "promoted_at_ts": arm.get("promoted_at_ts"), "n_forward": pref["n"],
               "days_forward": pref["n_days"], "matured": pref["matured"],
               "n_open_ahead": pref["n_open_ahead"], "day_lb": float("nan"), "n_days": 0,
               "controls": {}, "ctl_name": None, "ctl_lb": float("nan")}
        if pref["n"] > 0:
            fm = pref["mask"]
            idx = np.where(fm)[0]
            d = [days[i] for i in idx]
            lb, nd = cluster_boot(rets[champ][idx], d)
            dem["day_lb"], dem["n_days"] = lb, nd
            dem["mean"] = float(np.nanmean(rets[champ][idx]))
            for cname in _CONTROLS:
                cr = rets[cname][idx]
                if (~np.isnan(cr)).any():
                    dem["controls"][cname] = cluster_boot(cr, d)[0]
            if dem["controls"]:
                dem["ctl_name"] = max(dem["controls"], key=lambda c: dem["controls"][c])
                dem["ctl_lb"] = dem["controls"][dem["ctl_name"]]
        res["demotion"] = dem
    return res


# ── the paper gate: the ONE judge of the paper test ──────────────────────────────
PAPER_REFUSAL_REASONS = ("band_under_test_full", "entry lag exceeds MAX_ENTRY_LAG_S")
PAPER_FLOW_DARK_MAX = 0.50            # the adaptive candidate's own kill line, read at tick level
_VOID_CHECK_DETAIL = "suppressed — no number from this window may be quoted"
_WINDOW_FORMATS = ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S+00:00", "%Y-%m-%dT%H:%MZ", "%Y-%m-%d")


def _parse_window_start(s) -> float | None:
    """config.PAPER_GATE_WINDOW_START (ISO-8601 UTC, e.g. "2026-09-21T00:00:00Z") → epoch
    seconds with the stdlib only; None when unset. A malformed string is None too, printed: a
    window that cannot be parsed is not registered — never "today"."""
    if s is None or not str(s).strip():
        return None
    txt = str(s).strip()
    for fmt in _WINDOW_FORMATS:
        try:
            return float(calendar.timegm(time.strptime(txt, fmt)))
        except ValueError:
            continue
    print(f"  [paper gate] PAPER_GATE_WINDOW_START {s!r} is not ISO-8601 UTC "
          f"(e.g. 2026-09-21T00:00:00Z); none registered")
    return None


def band_mask(book: dict, meta: list, band: str) -> np.ndarray:
    """True where the position's `sidecar_true` stamp (the bands whose sidecar verdict was 1 at
    the row's event_seq) names `band`, aligned to live_returns' meta rows."""
    return np.array([band in ((book.get(m["key"]) or {}).get("sidecar_true") or [])
                     for m in meta], dtype=bool)


def _policy_shape(pol: dict, drop=("flow",)) -> str:
    return json.dumps({k: v for k, v in (pol or {}).items() if k not in drop},
                      sort_keys=True, default=list)


def _price_only_twin(policy: str) -> str | None:
    """The registered policy whose price legs equal `policy`'s minus its `flow` block — the
    paired benchmark of an adaptive policy (the two differ in exactly the flow rule). None when
    `policy` carries no flow leg or no such twin is registered."""
    pol = POL.POLICIES.get(policy) or {}
    if not isinstance(pol.get("flow"), dict):
        return None
    want = _policy_shape(pol)
    for name, p in POL.POLICIES.items():
        if name != policy and not isinstance(p.get("flow"), dict) and _policy_shape(p) == want:
            return name
    return None


def _paper_refusals(start: float, end: float, band: str) -> dict:
    """The refusals the book logged inside the window for rows the band selected: lines of
    data/livebook_missed.jsonl with a PAPER_REFUSAL_REASONS reason (the sub-cap, or the entry
    lag), whose event_seq has a sidecar verdict 1 for `band` (data/band_verdicts.csv, read ONCE)
    and whose ts is inside [start, end).

    UNKNOWN IS NOT ZERO. This is the capacity VOID's numerator, and the first version swallowed
    every error and returned 0 — which `refused_share is None or refused_share <= MAX` passes,
    so a missing or corrupt sidecar silently turned the check off in the direction that makes
    PASS easier, on a verdict that is permanent. Every positive finding of unreadability is now
    reported in `unknown` and VOIDs the window instead:
      - the missed log EXISTS but cannot be read, or carries unparseable lines — the numerator
        itself cannot be counted;
      - the sidecar is missing or raises AND there is at least one in-window refusal line to
        attribute (a refusal with no ts or no event_seq counts the same way).
    A MISSING missed log is not unknown: the file is only written when a refusal happens, so
    "no file" is the fact that none was ever logged, and 0 is then a measured zero — as it is
    when the sidecar answers verdict 0 for every refusal it was asked about."""
    unknown: list = []
    selected: set | None = set()
    side_why = ""
    n_side = 0
    try:
        with open(config.BAND_VERDICTS_PATH, newline="") as fh:
            for r in csv.DictReader(fh):
                if str(r.get("band")) != band or str(r.get("verdict")).strip() != "1":
                    continue
                try:
                    selected.add(int(float(r.get("event_seq"))))
                    n_side += 1
                except (TypeError, ValueError):
                    continue
    except FileNotFoundError:
        selected, side_why = None, f"the band verdict sidecar is missing ({config.BAND_VERDICTS_PATH})"
    except Exception as exc:
        selected, side_why = None, f"the band verdict sidecar is unreadable ({exc})"
        print(f"  [paper gate] sidecar unreadable ({exc}); the refusal share is UNKNOWN, not 0")
    n_ref = n_lines = n_unattributed = n_bad = 0
    if os.path.exists(LB.MISSED_PATH):
        try:
            with open(LB.MISSED_PATH) as fh:
                for ln in fh:
                    if not ln.strip():
                        continue
                    n_lines += 1
                    try:
                        m = json.loads(ln)
                    except Exception:
                        n_bad += 1                  # its reason cannot be read: it may be a refusal
                        continue
                    reason = str(m.get("reason") or "")
                    if not any(reason.startswith(x) for x in PAPER_REFUSAL_REASONS):
                        continue
                    ts = LB._num(m.get("ts"), LB._num(m.get("alert_ts")))
                    try:
                        seq = int(float(m.get("event_seq")))
                    except (TypeError, ValueError):
                        seq = None
                    if ts is None or seq is None:
                        n_unattributed += 1         # a refusal we cannot place in time or match
                    elif not start <= ts < end:
                        continue
                    elif selected is None:
                        n_unattributed += 1         # in the window, and no sidecar to attribute it
                    elif seq in selected:
                        n_ref += 1
        except Exception as exc:
            unknown.append(f"data/livebook_missed.jsonl is unreadable ({exc})")
            print(f"  [paper gate] missed log unreadable ({exc}); the refusal share is UNKNOWN, not 0")
    if n_bad:
        unknown.append(f"{n_bad} unparseable line(s) in data/livebook_missed.jsonl")
    if n_unattributed:
        unknown.append(f"{n_unattributed} refusal line(s) in data/livebook_missed.jsonl could not be "
                       f"attributed to {band}" + (f" — {side_why}" if selected is None else ""))
    return {"n_refused": n_ref, "n_lines": n_lines, "n_sidecar_rows": n_side,
            "n_unattributed": n_unattributed, "unknown": unknown}


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(ts)))


def paper_gate(now_s: float, *, book: dict | None = None, counts: dict | None = None,
               record: bool = False) -> dict:
    """The ONE judge of the paper test. Pre-registered by three config constants — the band
    (LIVEBOOK_BAND_UNDER_TEST), the policy (PAPER_GATE_POLICY, None ⇒ the exit champion's plan)
    and the window start (PAPER_GATE_WINDOW_START) — and computed with the machinery above and
    nothing new: eligible = stamped ∧ WINDOW_START <= opened_ts < WINDOW_START + WINDOW_DAYS,
    `_score_table` on those rows (the exit champion as the paired base, both exit controls on the
    SAME rows, the inert-control self-test), net_lb = day_lb − policies.round_trip_cost() for
    every row. Double-conservative on purpose: live returns already embed the quoted slippage,
    but gas is not in the quote and $10 sizing understates impact.

    The verdict, judged ONCE when the window has closed (now >= start + WINDOW_DAYS +
    LIVEBOOK_MAX_TRACK_S) and no eligible position is still open:
      PASS iff n >= PAPER_GATE_MIN_FILLS ∧ net_lb[policy] > 0 ∧ both exit controls' net_lb <= 0
           ∧ no inert control ∧ gapped share <= BAND_MAX_GAPPED_SHARE ∧ refused share <=
           PAPER_GATE_MAX_REFUSED_SHARE (∧ flow-dark share <= 0.50 for a policy with a flow leg);
      VOID (no number quoted) when a control's net_lb > 0, a control is inert, either share is
           exceeded, the refusal share cannot be MEASURED at all (unknown is not zero — see
           _paper_refusals), or the flow leg was dark on more than half of its armed ticks (the tick-level
           share over armed positions from livebook.live_stats_dict — a close-level count cannot
           see a rule that never ran when it mattered);
      FAIL otherwise.
    REPORTED, never gated: DSR and the implied Sharpe bar, the paired LB of the adaptive policy
    against its price-only twin, the band controls from the stamp ("capacity-selected"), means /
    medians / win rates, the in-window unstamped complement, n_armed and flow_dark_share on every
    evaluation. Every verdict line carries n_days and, below MIN_BOOTSTRAP_CLUSTERS, the words
    "a number, not a bound (floor 12)". While the window is open the same table prints with
    "window <start> open (n fills, D of W days)" and no verdict.

    One-shot bookkeeping in trials.json (append-only, a visible counted trial): `paper:<band>/
    <policy>@<start>` on the first evaluation of a window, `paper_verdict:...=<pass|fail|void>`
    once after closure; later runs print the recorded line. A changed band / policy / start is a
    new `paper:` line; after two windows on the same pair a third inside
    BAND_RENOMINATE_COOLDOWN_DAYS is refused. A verdict promotes nothing — champion.json,
    registry.json and the Sunday gates never read it.

    `record` is what makes that bookkeeping ONE-SHOT ACROSS CHECKOUTS, and only the APPLY path
    passes it: weekly.yml's Gates step runs `improve.py --apply --send` on the runner, whose
    Publish step is the only thing that stages the TRACKED selfimprove/trials.json. Every other
    caller — `--band-scorecard` on the Mac, a dry improve.py, `--summary-json`, weekly_summary —
    computes the identical verdict, prints the would-be lines in `would_record` marked
    "(not recorded: dry)", and writes NOTHING. Two reasons: a tracked file written and left
    uncommitted on the Mac makes the tree diverge (run_research.sh's step-0 ff-merge then
    refuses and the Mac stays silently stale), and the verdict is permanent — whoever evaluated
    first would mint it, so a Mac run on an older pulled snapshot could record `=fail` while the
    runner records `=pass` from the fresh committed book, with no way to tell which is
    authoritative. The one-shot and cap logic still READ trials.load()["nominations_ever"]
    exactly as before, so a recorded line prints "(recorded)" on every later run either way.

    `book` defaults to the file at livebook.BOOK_PATH; the flow-dark share is read through
    livebook.live_stats_dict (the one implementation), which reads that file.
    """
    W = int(config.PAPER_GATE_WINDOW_DAYS)
    band = config.LIVEBOOK_BAND_UNDER_TEST or None
    start_str = config.PAPER_GATE_WINDOW_START
    start = _parse_window_start(start_str)
    policy = config.PAPER_GATE_POLICY or champion.exit_plan()["name"]
    cost = float(POL.round_trip_cost())
    out = {"status": "none", "band": band, "policy": policy, "window_start": start_str,
           "window_days": W, "min_fills": int(config.PAPER_GATE_MIN_FILLS), "cost": cost,
           "line": "none registered", "n": 0, "n_days": 0, "n_open_eligible": 0, "n_admitted": 0,
           "days_elapsed": None, "table": None, "policy_row": None, "controls_net_lb": {},
           "benchmark": None, "band_controls": {}, "complement": None, "gapped_share": None,
           "refused_share": None, "n_refused": 0, "refusals_unknown": [],
           "has_flow": False, "n_armed": None,
           "flow_dark_share": None, "checks": [], "void": [], "reasons": [],
           "nomination_key": None, "recorded": None, "bound_label": "",
           "record": bool(record), "recorded_now": [], "would_record": []}
    if band is None or start is None:
        why = ("no band under test (config.LIVEBOOK_BAND_UNDER_TEST)" if band is None
               else "no window start (config.PAPER_GATE_WINDOW_START)")
        out["line"], out["reasons"] = f"none registered ({why})", [why]
        return out
    if policy not in POL.POLICIES or policy in _CONTROLS:
        why = f"policy {policy!r} is not a registered non-control policy"
        out["line"], out["reasons"] = f"none registered ({why})", [why]
        return out
    end = start + W * 86400.0
    closed_at = end + float(config.LIVEBOOK_MAX_TRACK_S)
    key = f"paper:{band}/{policy}@{start_str}"
    out.update({"start_ts": start, "end_ts": end, "closed_at_ts": closed_at, "nomination_key": key})

    # ── one-shot bookkeeping: the cap, then the `paper:` line (once per band/policy/start)
    tr = TR.load()
    known = key in tr["nominations_ever"]
    prefix = f"paper:{band}/{policy}@"
    prior = []
    for name in tr["nominations_ever"]:
        if name.startswith(prefix) and name != key:
            t = _parse_window_start(name[len(prefix):])
            if t is not None:
                prior.append(t)
    cooldown_s = (W + config.BAND_RENOMINATE_COOLDOWN_DAYS) * 86400.0
    if not known and len(prior) >= 2 and start < max(prior) + cooldown_s:
        why = (f"a third window on {band}@{policy} inside the {config.BAND_RENOMINATE_COOLDOWN_DAYS}-day "
               f"cooldown after two ({len(prior)} prior windows, the last {_iso(max(prior))}; "
               f"earliest allowed start {_iso(max(prior) + cooldown_s)})")
        out.update({"status": "refused", "line": f"refused: {why}", "reasons": [why]})
        return out
    if not known:
        if record:
            TR.bump("nominations", [key])
            out["recorded_now"].append(key)
        else:
            out["would_record"].append(key)      # the apply path mints it; this caller writes nothing
    vprefix = f"paper_verdict:{band}/{policy}@{start_str}="
    recorded = next((x for x in tr["nominations_ever"] if x.startswith(vprefix)), None)
    out["recorded"] = recorded

    # ── the sample: stamped ∧ in-window, completed and scorable (live_returns' rows)
    if book is None:
        book = LB._load(LB.BOOK_PATH, {})
    counts = {} if counts is None else counts
    rets, days, meta = live_returns(book, counts)
    stamped = band_mask(book, meta, band)
    in_win = np.array([start <= m["opened_ts"] < end for m in meta], dtype=bool)
    elig = stamped & in_win
    n_open_elig = n_admitted = 0
    for p in book.values():
        if not isinstance(p, dict) or band not in (p.get("sidecar_true") or []):
            continue
        ots = float(LB._num(p.get("opened_ts"), 0.0) or 0.0)
        if start <= ots < end:
            n_admitted += 1
            if not p.get("done"):
                n_open_elig += 1
    champ = champion.exit_champion()
    n_trials = TR.family_count("policies")
    idx = np.where(elig)[0]
    if idx.size:
        tbl = _score_table(rets, days, elig, champ, n_trials, counts)
    else:
        tbl = {"policies": {}, "controls": {}, "inert_controls": [], "n": 0, "n_days": 0}
    for t in (tbl["policies"], tbl["controls"]):
        for r in t.values():
            r["net_lb"] = r["day_lb"] - cost
    prow = tbl["policies"].get(policy)
    n = int(prow["n"]) if prow else 0
    n_days = int(prow["n_days"]) if prow else 0
    if prow is not None:
        prow["sharpe_bar"] = implied_sharpe_bar(n_days, n_trials)
        prow["n_trials"] = int(n_trials)
    n_gapped = sum(1 for i in idx if (((book.get(meta[i]["key"]) or {}).get("policies") or {})
                                      .get(policy) or {}).get("gapped"))
    gapped_share = (n_gapped / idx.size) if idx.size else None
    ref = _paper_refusals(start, end, band)
    n_ref = ref["n_refused"]
    # unknown is not zero: a share that cannot be measured is None (and VOIDs below), never 0
    refused_share = (None if ref["unknown"]
                     else (n_ref / (n_ref + n_admitted)) if (n_ref + n_admitted) else None)
    has_flow = isinstance((POL.POLICIES.get(policy) or {}).get("flow"), dict)
    n_armed = flow_dark = None
    if has_flow:
        try:
            ls = (LB.live_stats_dict(now_s).get("per_policy") or {}).get(policy) or {}
            n_armed, flow_dark = ls.get("n_armed"), ls.get("flow_dark_share")
        except Exception as exc:
            print(f"  [paper gate] live_stats_dict failed ({exc}); flow-dark share unknown")
    # reported, never gated
    twin = _price_only_twin(policy)
    if twin and twin in rets:
        a, b = rets[policy][idx], rets[twin][idx]
        both = ~(np.isnan(a) | np.isnan(b))
        d_idx = [days[i] for i in idx]
        out["benchmark"] = {"name": twin, "n": int(both.sum()),
                            "paired_mean": float(np.mean((a - b)[both])) if both.any() else float("nan"),
                            "paired_lb": paired_lb(a, b, d_idx) if both.any() else float("nan"),
                            "note": "adaptive minus price-only twin on the same rows; reported, never gated"}
    ctl_bands = sorted({str(b) for i in np.where(in_win)[0]
                        for b in ((book.get(meta[i]["key"]) or {}).get("sidecar_true") or [])
                        if str(b).startswith("ctl_")})
    for cname in ctl_bands:
        ci = np.where(in_win & band_mask(book, meta, cname))[0]
        if not ci.size:
            continue
        row = _row(rets[policy][ci], [days[i] for i in ci], n_trials)
        row["net_lb"] = row["day_lb"] - cost
        row["label"] = ("capacity-selected: the sidecar stamp on ADMITTED rows, not the control's "
                        "full selection")
        out["band_controls"][cname] = row
    ci = np.where((~stamped) & in_win)[0]
    if ci.size:
        crow = _row(rets[policy][ci], [days[i] for i in ci], n_trials)
        crow["net_lb"] = crow["day_lb"] - cost
        out["complement"] = {"n": int(ci.size), "n_days": len({days[i] for i in ci}), "policy": crow,
                             "note": "in-window rows the band did NOT select — context, never gated"}

    # ── the verdict
    days_elapsed = max(0.0, min(float(W), (now_s - start) / 86400.0))
    label = ("" if n_days >= config.MIN_BOOTSTRAP_CLUSTERS
             else f"a number, not a bound (floor {config.MIN_BOOTSTRAP_CLUSTERS})")
    out.update({"n": n, "n_days": n_days, "n_open_eligible": n_open_elig, "n_admitted": n_admitted,
                "days_elapsed": days_elapsed, "table": tbl, "policy_row": prow,
                "controls_net_lb": {c: r["net_lb"] for c, r in tbl["controls"].items()},
                "gapped_share": gapped_share, "refused_share": refused_share, "n_refused": n_ref,
                "refusals_unknown": list(ref["unknown"]),
                "has_flow": has_flow, "n_armed": n_armed, "flow_dark_share": flow_dark,
                "bound_label": label})
    void = []
    if tbl["inert_controls"]:
        void.append("INERT CONTROL: " + ", ".join(tbl["inert_controls"])
                    + " is bit-identical to the champion on every eligible position")
    prof = [c for c, r in tbl["controls"].items() if r["net_lb"] > 0]
    if prof:
        void.append("CONTROL PROFITABLE NET OF COST: " + ", ".join(
            f"{c} net LB {tbl['controls'][c]['net_lb']:+.3f}" for c in prof))
    if gapped_share is not None and gapped_share > config.BAND_MAX_GAPPED_SHARE:
        void.append(f"SAMPLING GAP: gapped share {gapped_share:.3f} > BAND_MAX_GAPPED_SHARE "
                    f"{config.BAND_MAX_GAPPED_SHARE}")
    if ref["unknown"]:
        void.append("CAPACITY UNKNOWN: " + "; ".join(ref["unknown"])
                    + " — a refusal share that cannot be measured is not a share of zero")
    if refused_share is not None and refused_share > config.PAPER_GATE_MAX_REFUSED_SHARE:
        void.append(f"CAPACITY: refused share {refused_share:.3f} > PAPER_GATE_MAX_REFUSED_SHARE "
                    f"{config.PAPER_GATE_MAX_REFUSED_SHARE} ({n_ref} refused / {n_admitted} admitted)")
    if has_flow and flow_dark is not None and flow_dark > PAPER_FLOW_DARK_MAX:
        void.append(f"FLOW DARK: tick-level dark share over armed positions {flow_dark:.3f} > "
                    f"{PAPER_FLOW_DARK_MAX:.2f} (n_armed {n_armed}) — the rule was mostly not running "
                    f"when it mattered")
    net = prow["net_lb"] if prow else float("nan")
    closed = now_s >= closed_at
    ctl_ok = all(c in tbl["controls"] and tbl["controls"][c]["net_lb"] <= 0 for c in _CONTROLS)
    # the 4th field says the detail quotes a PERFORMANCE number of this window — the VOID branch
    # blanks exactly those, keeping the label and the boolean. The apparatus details (days, fills,
    # shares, control names) are facts about the machinery and stay whatever the verdict is.
    checks = [
        (f"window closed (now >= start + {W} d + LIVEBOOK_MAX_TRACK_S)", closed, f"{days_elapsed:.1f} of {W} days", False),
        ("no eligible position still open", n_open_elig == 0, f"{n_open_elig} open", False),
        (f">= {config.PAPER_GATE_MIN_FILLS} closed fills", n >= config.PAPER_GATE_MIN_FILLS, f"{n} fills", False),
        (f"{policy} net LB > 0 (day-clustered 2.5% bound minus the {cost:.3f} round trip)", bool(net > 0),
         f"net LB {_fmt(net)}", True),
        ("both exit controls net LB <= 0 on the same rows", ctl_ok,
         ", ".join(f"{c} {_fmt(r['net_lb'])}" for c, r in tbl["controls"].items()) or "no control scored", True),
        ("no inert control", not tbl["inert_controls"], ", ".join(tbl["inert_controls"]) or "none", False),
        (f"gapped share <= BAND_MAX_GAPPED_SHARE {config.BAND_MAX_GAPPED_SHARE}",
         gapped_share is None or gapped_share <= config.BAND_MAX_GAPPED_SHARE, _fmt(gapped_share, ".3f"), False),
        (f"refused share <= PAPER_GATE_MAX_REFUSED_SHARE {config.PAPER_GATE_MAX_REFUSED_SHARE}",
         not ref["unknown"] and (refused_share is None
                                 or refused_share <= config.PAPER_GATE_MAX_REFUSED_SHARE),
         f"{_fmt(refused_share, '.3f')} ({n_ref} refused / {n_admitted} admitted)"
         + ("; UNKNOWN: " + "; ".join(ref["unknown"]) if ref["unknown"] else ""), False),
    ]
    if has_flow:
        checks.append((f"flow-dark share over armed positions <= {PAPER_FLOW_DARK_MAX:.2f}",
                       flow_dark is None or flow_dark <= PAPER_FLOW_DARK_MAX,
                       f"{_fmt(flow_dark, '.3f')} over n_armed {n_armed}", False))
    out["checks"] = [[lbl, bool(ok), det] for lbl, ok, det, _q in checks]
    out["void"] = void
    head = f"{band}@{policy} window {start_str}"
    tail = f"n_days {n_days}" + (f", {label}" if label else "")
    if now_s < start:
        status = "pending"
        line = f"{head} pending (starts in {(start - now_s) / 86400.0:.1f} d)"
    elif not closed or n_open_elig > 0:
        status = "open"
        line = (f"{head} open ({n} fills, {days_elapsed:.1f} of {W} days"
                + (f", {n_open_elig} eligible still open" if closed else "") + f"; {tail})")
        if void:
            line += " — would VOID if judged now: " + void[0]
        out["reasons"] = [f"{lbl} — {det}" for lbl, ok, det, _q in checks if not ok]
    else:
        if recorded:
            status, suffix = recorded.rsplit("=", 1)[1].upper(), " (recorded)"
        else:
            status = "VOID" if void else ("PASS" if all(ok for _, ok, _, _q in checks) else "FAIL")
            vline = f"{vprefix}{status.lower()}"
            if record:
                TR.bump("nominations", [vline])
                out["recorded_now"].append(vline)
                suffix = ""
            else:
                out["would_record"].append(vline)
                suffix = " (not recorded: dry)"
        if status == "VOID":
            # No number from a void window may be quoted: the apparatus facts stay, the table
            # goes — and so does every performance number in `checks`, which is built BEFORE this
            # branch and is persisted whole by summary_json into the digest committed to the
            # public repo. Suppressing the table alone left the policy's own "net LB +0.412" and
            # each control's printed three lines under "no number from this window may be quoted".
            out.update({"table": None, "policy_row": None, "controls_net_lb": {}, "benchmark": None,
                        "band_controls": {}, "complement": None,
                        "checks": [[lbl, bool(ok), _VOID_CHECK_DETAIL if q else det]
                                   for lbl, ok, det, q in checks]})
            line = f"{head} VOID ({void[0] if void else 'recorded'}; n {n}, {tail}){suffix}"
            out["reasons"] = list(void)
        elif status == "PASS":
            line = f"{head} PASS (n {n}, {tail}; net LB {_fmt(net)}){suffix}"
            out["reasons"] = []
        else:
            failed = [f"{lbl} — {det}" for lbl, ok, det, _q in checks if not ok]
            line = f"{head} FAIL ({failed[0] if failed else 'recorded'}; n {n}, {tail}){suffix}"
            out["reasons"] = failed
    out["status"], out["line"] = status, line
    return out


def _print_paper_gate(pg: dict) -> None:
    print(f"paper gate: {pg['line']}")
    if pg["status"] in ("none", "refused", "pending", "error"):
        for r in pg.get("reasons") or []:
            print(f"  - {r}")
        return
    print(f"  band {pg['band']} · policy {pg['policy']} · window {pg['window_start']} + {pg['window_days']} d "
          f"(+ LIVEBOOK_MAX_TRACK_S to close) · cost bar {pg['cost']:.3f} per round trip (live returns already "
          f"embed the quoted slippage: double-conservative on purpose)")
    print(f"  eligible: {pg['n']} closed fills / {pg['n_days']} alert-days"
          + (f" — {pg['bound_label']}" if pg["bound_label"] else "")
          + f"; {pg['n_open_eligible']} still open; admitted {pg['n_admitted']}, refused {pg['n_refused']} "
          f"(share {_fmt(pg['refused_share'], '.3f')}); gapped share {_fmt(pg['gapped_share'], '.3f')}")
    print(f"  flow leg: {'yes' if pg['has_flow'] else 'no'}; n_armed {pg['n_armed']}; "
          f"flow_dark_share {_fmt(pg['flow_dark_share'], '.3f')}")
    if pg["table"] is None:
        print("  per-policy table suppressed — VOID: no number from this window may be quoted")
    else:
        print(f"\n  {'policy':<28s} {'n':>4s} {'mean':>8s} {'median':>8s} {'win':>6s} {'day LB':>8s} "
              f"{'net LB':>8s} {'days':>5s} {'DSR':>6s}")
        rows = sorted(pg["table"]["policies"].items(),
                      key=lambda kv: -(kv[1]["net_lb"] if math.isfinite(kv[1]["net_lb"]) else -1e9))
        for name, r in rows:
            mark = "*" if name == pg["policy"] else " "
            print(f" {mark}{name:<27s} {r['n']:>4} {_fmt(r['mean']):>8s} {_fmt(r['median']):>8s} "
                  f"{_fmt(r['win_rate'], '.2f'):>6s} {_fmt(r['day_lb']):>8s} {_fmt(r['net_lb']):>8s} "
                  f"{r['n_days']:>5} {_fmt(r.get('dsr'), '.3f'):>6s}")
        for name, r in pg["table"]["controls"].items():
            print(f"  {name:<27s} {r['n']:>4} {_fmt(r['mean']):>8s} {_fmt(r['median']):>8s} "
                  f"{_fmt(r['win_rate'], '.2f'):>6s} {_fmt(r['day_lb']):>8s} {_fmt(r['net_lb']):>8s} "
                  f"{r['n_days']:>5} {_fmt(r.get('dsr'), '.3f'):>6s}   (control)")
        prow = pg["policy_row"]
        if prow:
            print(f"  DSR {_fmt(prow.get('dsr'), '.3f')} at {prow.get('n_trials')} cumulative trials; implied per-day "
                  f"Sharpe bar {_fmt(prow.get('sharpe_bar'), '.2f')} (reported, never gated)")
        if pg["benchmark"]:
            b = pg["benchmark"]
            print(f"  paired vs price-only twin {b['name']}: mean {_fmt(b['paired_mean'])}, LB {_fmt(b['paired_lb'])} "
                  f"over {b['n']} rows (reported, never gated)")
        for c, r in pg["band_controls"].items():
            print(f"  band control {c} ({r['label']}): n {r['n']}, net LB {_fmt(r['net_lb'])}, {r['n_days']} days")
        if pg["complement"]:
            c = pg["complement"]
            print(f"  complement (in-window, unstamped): n {c['n']} / {c['n_days']} days, {pg['policy']} net LB "
                  f"{_fmt(c['policy']['net_lb'])} — context")
    for lbl, ok, det in pg["checks"]:
        print(f"  [{'x' if ok else ' '}] {lbl} ({det})")
    for v in pg["void"]:
        print(f"  VOID: {v}")
    if pg["recorded"]:
        print(f"  recorded: {pg['recorded']}")
    for x in pg.get("recorded_now") or []:
        print(f"  recorded now: {x}")
    for x in pg.get("would_record") or []:
        print(f"  would record: {x}   (not recorded: dry — only the Sunday --apply run writes trials.json)")
    print("  a verdict promotes nothing: champion.json, registry.json and the Sunday gates are untouched by it")


# ── the gate ─────────────────────────────────────────────────────────────────────
def _in_cooldown(res: dict, name: str) -> bool:
    ex = res.get("exit_state") or {}
    if ex.get("failed_nominee") != name or ex.get("failed_ts") is None:
        return False
    try:
        return (float(res["ts"]) - float(ex["failed_ts"])) < \
            config.IMPROVE_RENOMINATE_COOLDOWN_DAYS * 86400.0
    except (TypeError, ValueError):
        return False


def decide(res: dict) -> dict:
    """Apply the gate. Returns {promote, winner, nominate, nomination_failed, demote,
    demotion_judged, gate_broken, champion_under_review, reasons, checks}. Advisory: apply()
    is the only thing that acts on it, and only under --apply.

    REBUILT (solana, 2026-08-15) because the first version could not fail. It picked the
    challenger with the highest paired lower bound and then tested whether that bound exceeded
    zero — a maximum over a correlated family, tested as though it were a single hypothesis.
    Measured on 639 real rows with every policy's advantage recentred to exactly zero mean
    (preserving the 0.561 cross-policy correlation, the day structure and the skew): that
    procedure crosses zero 33.3% of the time at 12 alert-days and 20.0% at 41. More data does
    not fix it — it is a selection bias, not a variance problem. Hence:

    1. THE BAR IS THE BEST NEGATIVE CONTROL, NOT THE CHAMPION. Buying and selling in the same
       instant (pure round-trip cost, zero information) beat hold_to_end by a mile on solana.
       "Every exit beats holding" was never evidence that any exit is skilful.
    2. APPARATUS FAULTS VOID THE RUN ENTIRELY: an INERT control or a PROFITABLE control means
       the measurement is broken; the table is suppressed and no number may be quoted.
    3. PROMOTION IS FORWARD-ONLY: nominate on today's data, test on rows that postdate it, judge
       ONCE on the pre-registered prefix.
    4. DEMOTION is the same one-shot forward test aimed at a promoted champion.
    """
    champ = res["champion"]
    ctl = res.get("controls", {})
    reasons: list = []
    out = {"promote": False, "winner": None, "nominate": None, "nomination_failed": None,
           "demote": False, "demotion_judged": False, "gate_broken": False,
           "champion_under_review": False, "reasons": reasons, "checks": []}

    # (A) apparatus checks come first and are disqualifying. A run is void only on faults that
    # are actually about the machinery — NOT when a control merely beats the champion: with
    # ~-95% median fates NOT-holding genuinely beats holding, so a control beating the champion
    # is a fact about the asset class, and voiding on it short-circuited every solana run
    # before the real bar (promotion check #2) could ever act.
    broken = []
    if res.get("inert_controls"):
        broken.append("INERT CONTROL: " + ", ".join(res["inert_controls"]) +
                      " is bit-identical to the champion on every position — an alias "
                      "of the champion cannot detect anything")
    # Judgment-call arm, not a law of nature: in a sustained mania regime a random-hold
    # exit CAN be genuinely profitable net of costs, and this check would then void runs
    # in exactly the regime where the screen works. If it fires repeatedly during a
    # strong tape, re-examine the check before trusting it.
    profitable_ctl = [n for n, r in ctl.items() if (r.get("day_lb") or -9) > 0]
    if profitable_ctl:
        broken.append("CONTROL PROFITABLE ON ITS OWN BOUND: " + ", ".join(profitable_ctl) +
                      " — a do-nothing policy should not beat round-trip costs on honest "
                      "quotes; treat the measurement as corrupted (or the regime as one "
                      "this check cannot serve — see the comment above it)")
    if broken:
        out["gate_broken"] = True
        reasons[:] = ["THE GATE IS MEASURING ITS OWN MACHINERY; no number from this run may "
                      "be quoted."] + broken
        return out

    # (B) floors
    n_pos, n_days = res["n_positions"], res["n_days"]
    if n_pos < config.IMPROVE_MIN_POSITIONS:
        reasons.append(f"only {n_pos} completed positions (needs {config.IMPROVE_MIN_POSITIONS})")
    if n_days < config.IMPROVE_PROMOTE_MIN_CLUSTERS:
        reasons.append(f"only {n_days} alert-days (needs {config.IMPROVE_PROMOTE_MIN_CLUSTERS}; "
                       f"the 12-cluster floor measured 6.3% actual coverage against a nominal 2.5%)")
    if not res.get("forward_only"):
        reasons.append("scored on rows that already existed when the policy was named — this is "
                       "a RANKING, not a test. Nominate first, then test forward-only.")

    # (C) the candidate: a live nominee is judged ALONE — exactly ONE pre-registered
    # hypothesis is on trial, and judging today's leaderboard max instead would reintroduce
    # the family-wise maximum the nomination exists to remove.
    ctl_lb = max([r.get("day_lb") for r in ctl.values() if r.get("day_lb") is not None
                  and not np.isnan(r["day_lb"])] or [-1e9])
    ctl_name = (max(ctl, key=lambda n: ctl[n].get("day_lb") if ctl[n].get("day_lb") is not None
                    and not np.isnan(ctl[n]["day_lb"]) else -1e9) if ctl else None)
    nom = res.get("nomination")
    best, best_lb = None, -1e9
    if nom and nom["nominee"] in res["policies"] and nom["nominee"] != champ:
        best = nom["nominee"]
        best_lb = res["policies"][best].get("paired_lb", -1e9)
        if not res.get("forward_only"):
            reasons.append(f"nominee `{best}` awaiting its forward sample: "
                           f"{nom['n_forward']}/{config.IMPROVE_FWD_MIN_POSITIONS} positions "
                           f"across {nom['days_forward']}/{config.IMPROVE_FWD_MIN_DAYS} alert-days"
                           + (f"; {nom['n_open_ahead']} earlier-opened position(s) still tracking"
                              if nom.get("n_open_ahead") else ""))
    else:
        skipped = []
        for name, row in res["policies"].items():
            if name == champ or name in _CONTROLS or "paired_lb" not in row:
                continue
            if _in_cooldown(res, name):
                skipped.append(name)
                continue
            plb = row["paired_lb"]
            if plb is not None and not np.isnan(plb) and plb > best_lb:
                best, best_lb = name, plb
        if skipped:
            reasons.append(f"not eligible (failed nomination inside "
                           f"{config.IMPROVE_RENOMINATE_COOLDOWN_DAYS}-day cooldown): "
                           + ", ".join(skipped))
    if best is None:
        reasons.append("no challenger scored")
    else:
        row = res["policies"][best]
        plb = row.get("paired_lb", float("nan"))
        dlb = row.get("day_lb", float("nan"))
        dsr = row.get("dsr", float("nan"))
        checks = [
            ("beats the champion on a PAIRED day-clustered bound", bool(plb > 0),
             f"paired LB {plb:+.3f}"),
            (f"beats the best NEGATIVE CONTROL ({ctl_name}) on its own bound",
             bool(dlb > ctl_lb), f"own LB {dlb:+.3f} vs control {ctl_lb:+.3f}"),
            ("is profitable on its OWN day-clustered bound", bool(dlb > 0), f"own LB {dlb:+.3f}"),
            (f"deflated Sharpe >= {config.IMPROVE_DSR_GATE} on DAY MEANS at {res['n_trials']} "
             f"cumulative trials", bool(dsr >= config.IMPROVE_DSR_GATE), f"DSR {dsr:.3f}"),
            (f">= {config.IMPROVE_PROMOTE_MIN_CLUSTERS} alert-day clusters",
             bool(row["n_days"] >= config.IMPROVE_PROMOTE_MIN_CLUSTERS), f"{row['n_days']} days"),
            (f">= {config.IMPROVE_MIN_POSITIONS} completed positions",
             bool(n_pos >= config.IMPROVE_MIN_POSITIONS), f"{n_pos} positions"),
            ("tested FORWARD-ONLY on rows postdating its nomination",
             bool(res.get("forward_only")), "nomination/test split"),
        ]
        failed = [f"{lbl} — {detail}" for lbl, ok, detail in checks if not ok]
        out["winner"], out["checks"] = best, checks
        out["promote"] = not failed
        reasons.extend(failed)
        # (E) NOMINATE when the only failing check is the forward-only split, nothing is
        # nominated yet, and the sample floors are met. A nomination is free (it is not a
        # claim); from then on this policy alone is on trial, on rows it has never seen.
        only_fwd_blocked = bool(failed) and all("FORWARD-ONLY" in f for f in failed)
        if (nom is None and only_fwd_blocked and n_pos >= config.IMPROVE_MIN_POSITIONS
                and n_days >= config.IMPROVE_PROMOTE_MIN_CLUSTERS):
            out["nominate"] = best
        # The one-shot judgment: a nominee whose PRE-REGISTERED forward prefix has matured
        # and still fails any check is terminally FAILED — apply() clears the nomination and
        # records it, instead of the nominee wedging the loop forever as a silent zombie.
        if nom and res.get("forward_only") and failed:
            out["nomination_failed"] = nom["nominee"]

    # (D') DEMOTION — pre-registered, one-shot, on the champion's OWN forward prefix.
    dem = res.get("demotion")
    if dem:
        if dem["matured"]:
            out["demotion_judged"] = True
            if out["promote"]:
                reasons.append(f"demotion test of `{dem['champion']}` superseded by the promotion")
            elif not (dem["day_lb"] > dem["ctl_lb"]):
                out["demote"] = True
                reasons.append(f"DEMOTE `{dem['champion']}` → `{config.IMPROVE_DEFAULT_EXIT_CHAMPION}`: "
                               f"forward own LB {dem['day_lb']:+.3f} <= best control "
                               f"({dem['ctl_name']}) {dem['ctl_lb']:+.3f} on {dem['n_forward']} "
                               f"positions / {dem['days_forward']} alert-days")
            else:
                reasons.append(f"champion `{dem['champion']}` kept: forward own LB "
                               f"{dem['day_lb']:+.3f} > best control ({dem['ctl_name']}) "
                               f"{dem['ctl_lb']:+.3f} (judged once; not repeated)")
        elif dem["n_forward"] > 0 and not np.isnan(dem["day_lb"]) and \
                not np.isnan(dem["ctl_lb"]) and dem["day_lb"] < dem["ctl_lb"]:
            out["champion_under_review"] = True
            reasons.append(f"champion `{dem['champion']}` UNDER REVIEW (informational): forward "
                           f"own LB {dem['day_lb']:+.3f} below best control {dem['ctl_lb']:+.3f} "
                           f"at {dem['n_forward']}/{config.IMPROVE_FWD_MIN_POSITIONS} positions, "
                           f"{dem['days_forward']}/{config.IMPROVE_FWD_MIN_DAYS} days — not judged yet")
    if out["demote"] and out["nominate"]:
        reasons.append(f"nomination of `{out['nominate']}` deferred: the champion changes this run")
        out["nominate"] = None
    return out


# ── outputs ──────────────────────────────────────────────────────────────────────
def _fmt(v, spec: str = "+.3f") -> str:
    try:
        if v is None or (isinstance(v, float) and not math.isfinite(v)):
            return "nan"
        return format(v, spec)
    except (TypeError, ValueError):
        return "?"


def _table_lines(policies: dict, controls: dict, champ: str) -> list:
    lines = ["| policy | n | mean | day LB | paired vs champ | paired LB | DSR | gapped |",
             "|---|---|---|---|---|---|---|---|"]
    order = sorted(policies.items(), key=lambda kv: -(kv[1].get("paired_lb")
                                                     if kv[1].get("paired_lb") is not None
                                                     and not np.isnan(kv[1]["paired_lb"]) else -1e9))
    for name, r in order:
        mark = " **(champion)**" if name == champ else (" *(forward prefix)*"
                                                        if r.get("forward_only") else "")
        lines.append(f"| `{name}`{mark} | {r['n']} | {_fmt(r['mean'])} | {_fmt(r['day_lb'])} | "
                     f"{_fmt(r.get('paired_mean'))} | {_fmt(r.get('paired_lb'))} | "
                     f"{_fmt(r.get('dsr'), '.3f')} | {r.get('n_gapped', 0)} |")
    for name, r in controls.items():
        lines.append(f"| `{name}` (control) | {r['n']} | {_fmt(r['mean'])} | {_fmt(r['day_lb'])} | "
                     f"{_fmt(r.get('paired_mean'))} | {_fmt(r.get('paired_lb'))} | "
                     f"{_fmt(r.get('dsr'), '.3f')} | {r.get('n_gapped', 0)} |")
    return lines


def _dsr_sentence(res: dict) -> str:
    b = res.get("dsr_bar") or {}
    return (f"DSR bar: at {b.get('n_days')} alert-days and {b.get('n_trials')} cumulative trials, "
            f"the DSR >= {config.IMPROVE_DSR_GATE} gate needs a per-day Sharpe of about "
            f"{_fmt(b.get('sharpe'), '.2f')} on day means (normal approximation; a skewed "
            f"series needs more).")


def write_proposal(res: dict, verdict: dict) -> str:
    os.makedirs(config.PROPOSALS_DIR, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime(res["ts"]))
    path = os.path.join(config.PROPOSALS_DIR, f"proposal-{stamp}.md")
    champ = res["champion"]
    bk = res.get("book") or {}
    cnt = res.get("counts") or {}
    lag = bk.get("entry_lag_median_s")
    lines = [f"# livebook proposal — {stamp} UTC", "",
             f"- champion: `{champ}`",
             f"- completed positions scored: **{res['n_positions']}** across "
             f"**{res['n_days']}** alert-days (stratum `{res.get('stratum')}`)",
             f"- cumulative distinct hypotheses ever scored: **{res['n_trials']}**",
             f"- book: {bk.get('n_positions', cnt.get('n_positions', 0))} positions, "
             f"{bk.get('n_done', cnt.get('n_done', 0))} done, {bk.get('n_open', cnt.get('n_open', 0))} open; "
             f"suspect {cnt.get('n_suspect', 0)}, unpriced {cnt.get('n_unpriced', 0)}, "
             f"gapped (any policy) {bk.get('n_gapped', '?')}, no-route {bk.get('n_no_route', '?')}, "
             f"refused {bk.get('n_missed', '?')}",
             f"- entry-lag median: {('%.0f s' % lag) if isinstance(lag, (int, float)) else '?'}",
             f"- paper gate: {(res.get('paper_gate') or {}).get('line', 'none registered')} (not a promotion)",
             ""]
    ex = res.get("exit_state") or {}
    if ex.get("nominee"):
        nom = res.get("nomination") or {}
        lines += [f"- nominee on trial: `{ex['nominee']}` since alert_seq "
                  f"{ex.get('nominated_at_alert_seq')} — forward prefix "
                  f"{nom.get('n_forward', 0)}/{config.IMPROVE_FWD_MIN_POSITIONS} positions, "
                  f"{nom.get('days_forward', 0)}/{config.IMPROVE_FWD_MIN_DAYS} days, "
                  f"{'MATURED' if nom.get('matured') else 'not matured'}", ""]
    if verdict.get("gate_broken"):
        lines += ["**Per-policy table suppressed — this run is VOID** (apparatus fault; see "
                  "blocking reasons below). No number from this run may be quoted; quoting "
                  "its numbers anywhere defeats the point of voiding the run.", ""]
    elif not res["policies"]:
        lines += ["No completed positions yet. The book is still filling.", ""]
    else:
        lines += _table_lines(res["policies"], res["controls"], champ) + [""]
        a = (res.get("strata") or {}).get("A")
        if a:
            lines += [f"### A stratum (promotions + A-tier first sightings): {a['n']} positions "
                      f"across {a['n_days']} alert-days", ""]
            if a["n"]:
                lines += _table_lines(a["policies"], a["controls"], champ) + [""]
            else:
                lines += ["(no completed A-stratum positions yet)", ""]
        lines += [_dsr_sentence(res), ""]
    dem = res.get("demotion")
    if dem and not verdict.get("gate_broken"):
        lines += [f"### Demotion test on `{dem['champion']}` (forward prefix after alert_seq "
                  f"{dem['promoted_at_alert_seq']}): {dem['n_forward']}/{config.IMPROVE_FWD_MIN_POSITIONS} "
                  f"positions, {dem['days_forward']}/{config.IMPROVE_FWD_MIN_DAYS} days, "
                  f"{'MATURED' if dem['matured'] else 'not matured'}; own LB {_fmt(dem['day_lb'])} "
                  f"vs best control {dem.get('ctl_name')} {_fmt(dem['ctl_lb'])}", ""]
    if verdict.get("promote"):
        lines += [f"## PROMOTE → `{verdict['winner']}`", "", "Every check passed:", ""]
    elif verdict.get("demote"):
        lines += [f"## DEMOTE `{champ}` → `{config.IMPROVE_DEFAULT_EXIT_CHAMPION}`", ""]
    elif verdict.get("nominate"):
        lines += [f"## NOMINATE `{verdict['nominate']}`", "",
                  "Cleared every bar except the forward-only split; its forward trial starts at "
                  f"alert_seq {res.get('max_alert_seq')}.", ""]
    else:
        lines += ["## NO CHANGE", "",
                  (f"Best challenger by paired bound: `{verdict.get('winner')}`"
                   if verdict.get("winner") else "No challenger."), ""]
    if verdict.get("checks"):
        lines += [f"Checks for `{verdict['winner']}`:", ""]
        for lbl, ok, detail in verdict["checks"]:
            lines.append(f"- [{'x' if ok else ' '}] {lbl} ({detail})")
        lines.append("")
    if verdict.get("nomination_failed"):
        lines += [f"**NOMINATION FAILED**: `{verdict['nomination_failed']}` judged once on its "
                  f"pre-registered forward prefix and did not clear; nomination cleared "
                  f"(cooldown {config.IMPROVE_RENOMINATE_COOLDOWN_DAYS} days).", ""]
    if verdict.get("reasons"):
        lines += ["Blocking / notes:", ""] + [f"- {r}" for r in verdict["reasons"]] + [""]
    lines += ["---", "",
              "Generated by improve.py. Applied only under --apply and only when every check "
              "passed and every control failed. Trials accumulate forever in "
              "selfimprove/trials.json — you cannot un-look at a result."]
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w") as fh:
        fh.write("\n".join(lines) + "\n")
    os.replace(tmp, path)
    return path


def _append_history(res: dict, verdict: dict, published=None, paused: bool = False,
                    applied: bool = False) -> None:
    """One line per run. `applied` = this line was written by an --apply run (paused or not:
    it had the authority and reported); weekly.yml's idempotency step reads the last line's
    date and `applied`, so a dry run never stamps the day."""
    line = {"ts": res["ts"], "n": res["n_positions"], "days": res["n_days"],
            "champion": res["champion"], "winner": verdict.get("winner"),
            "promote": bool(verdict.get("promote")), "nominated": verdict.get("nominate"),
            "demoted": bool(verdict.get("demote")), "gate_broken": bool(verdict.get("gate_broken")),
            "paused": bool(paused), "published": published, "applied": bool(applied)}
    try:
        os.makedirs(os.path.dirname(config.IMPROVE_HISTORY_PATH), exist_ok=True)
        with open(config.IMPROVE_HISTORY_PATH, "a") as fh:
            fh.write(json.dumps(LB._clean(line), allow_nan=False) + "\n")
    except Exception as exc:
        print(f"  [improve] history append failed: {exc}")


def _evidence(res: dict, verdict: dict, proposal_path: str | None) -> dict:
    """The seven numbers, the control name/LB, n_trials and the proposal path — what
    champion.json carries so a promotion can be audited without this run's stdout."""
    w = verdict.get("winner")
    row = (res.get("policies") or {}).get(w, {}) if w else {}
    ctl = res.get("controls", {})
    ctl_name = max(ctl, key=lambda n: ctl[n].get("day_lb") or -1e9) if ctl else None
    ev = {"paired_lb": row.get("paired_lb"), "paired_mean": row.get("paired_mean"),
          "day_lb": row.get("day_lb"), "dsr": row.get("dsr"), "n_days": row.get("n_days"),
          "n": row.get("n"), "n_positions": res["n_positions"],
          "forward_only": bool(res.get("forward_only")),
          "control": ctl_name, "control_day_lb": (ctl.get(ctl_name) or {}).get("day_lb"),
          "n_trials": res["n_trials"], "stratum": res.get("stratum"),
          "checks": [[lbl, bool(ok), detail] for lbl, ok, detail in verdict.get("checks", [])],
          "proposal_path": proposal_path}
    return LB._clean(ev)


def apply(res: dict, verdict: dict, now_s: float, send: bool = False,
          proposal_path: str | None = None) -> dict:
    """Act on a verdict: champion.json via champion.write_state (the sole writer), the trial
    ledger, event alerts. PAUSE / locked ⇒ report only. Never raises."""
    out = {"paused": False, "written": [], "events": []}
    dry = not send

    def _event(kind: str, lines: list) -> None:
        try:
            title, body = alerts.format_event(kind, lines)
            alerts.send_all(title, body, dry_run=dry)
        except Exception as exc:
            print(f"  [improve] event {kind} failed: {exc}")
        out["events"].append(kind)

    champ = res["champion"]
    default = config.IMPROVE_DEFAULT_EXIT_CHAMPION
    seq = res.get("max_alert_seq", -1)
    try:
        arm = _exit_arm()
        if champion.paused():
            out["paused"] = True
            what = ("PROMOTE " + str(verdict.get("winner")) if verdict.get("promote") else
                    "DEMOTE " + champ if verdict.get("demote") else
                    "NOMINATE " + str(verdict.get("nominate")) if verdict.get("nominate") else
                    "APPARATUS FAULT" if verdict.get("gate_broken") else "no change")
            _event("PAUSED", [f"exit gate: PAUSED (selfimprove/PAUSE or champion.json.locked)",
                              f"would have: {what}", f"champion stays `{champ}`",
                              f"{res['n_positions']} positions / {res['n_days']} alert-days",
                              f"proposal: {proposal_path}"])
            _append_history(res, verdict, paused=True, applied=True)
            return out
        if verdict.get("gate_broken"):
            _event("APPARATUS FAULT", ["exit gate VOID — no number from this run may be quoted"]
                   + [r for r in verdict["reasons"]] + [f"proposal: {proposal_path}"])
        elif verdict.get("promote"):
            w = verdict["winner"]
            ev = _evidence(res, verdict, proposal_path)
            champion.write_state(
                exit={"champion": w, "previous": champ, "promoted_ts": now_s,
                      "promoted_at_alert_seq": seq, "promoted_at_ts": now_s, "evidence": ev,
                      "nominee": None, "nominated_at_alert_seq": None, "nominated_ts": None,
                      "demotion_judged": None},
                history_line={"ts": now_s, "arm": "exit", "action": "promote", "from": champ,
                              "to": w, "proposal": proposal_path,
                              "evidence_summary": {"paired_lb": ev["paired_lb"],
                                                   "day_lb": ev["day_lb"], "dsr": ev["dsr"]}})
            out["written"].append("promote")
            _event("PROMOTED", [f"exit champion: `{champ}` → `{w}`",
                                f"paired LB {_fmt(ev['paired_lb'])}, own LB {_fmt(ev['day_lb'])} "
                                f"vs control {ev['control']} {_fmt(ev['control_day_lb'])}, "
                                f"DSR {_fmt(ev['dsr'], '.3f')} at {ev['n_trials']} trials",
                                f"{ev['n']} forward positions / {ev['n_days']} alert-days "
                                f"(book {res['n_positions']} / {res['n_days']})",
                                "its own demotion test starts now at alert_seq %s" % seq,
                                f"proposal: {proposal_path}"])
        else:
            if verdict.get("demote"):
                dem = res.get("demotion") or {}
                ev = LB._clean({"demotion": True, "day_lb": dem.get("day_lb"),
                                "control": dem.get("ctl_name"), "control_day_lb": dem.get("ctl_lb"),
                                "n": dem.get("n_forward"), "n_days": dem.get("days_forward"),
                                "promoted_at_alert_seq": dem.get("promoted_at_alert_seq"),
                                "n_trials": res["n_trials"], "proposal_path": proposal_path})
                champion.write_state(
                    exit={"champion": default, "previous": champ, "promoted_ts": now_s,
                          "promoted_at_alert_seq": seq, "promoted_at_ts": now_s, "evidence": ev,
                          "nominee": None, "nominated_at_alert_seq": None, "nominated_ts": None,
                          "demotion_judged": now_s},
                    history_line={"ts": now_s, "arm": "exit", "action": "demote", "from": champ,
                                  "to": default, "proposal": proposal_path,
                                  "evidence_summary": {"day_lb": ev["day_lb"],
                                                       "control_day_lb": ev["control_day_lb"]}})
                try:
                    TR.bump("nominations", [f"exit:demotion:{champ}@{dem.get('promoted_at_alert_seq')}"])
                except Exception as exc:
                    print(f"  [improve] trials bump failed: {exc}")
                out["written"].append("demote")
                _event("DEMOTED", [f"exit champion: `{champ}` → `{default}` (default)",
                                   f"forward own LB {_fmt(ev['day_lb'])} <= best control "
                                   f"{ev['control']} {_fmt(ev['control_day_lb'])} on {ev['n']} "
                                   f"positions / {ev['n_days']} alert-days",
                                   "reversal needs no positive proof, only failure to keep "
                                   "beating the control", f"proposal: {proposal_path}"])
            elif verdict.get("demotion_judged"):
                champion.write_state(
                    exit={"demotion_judged": now_s},
                    history_line={"ts": now_s, "arm": "exit", "action": "demotion_judged",
                                  "from": champ, "to": champ, "proposal": proposal_path})
                out["written"].append("demotion_judged")
            if verdict.get("nomination_failed"):
                nf = verdict["nomination_failed"]
                champion.write_state(
                    exit={"nominee": None, "nominated_at_alert_seq": None, "nominated_ts": None,
                          "failed_nominee": nf, "failed_ts": now_s,
                          "failed_at_alert_seq": arm.get("nominated_at_alert_seq")},
                    history_line={"ts": now_s, "arm": "exit", "action": "nomination_failed",
                                  "from": champ, "to": champ, "nominee": nf,
                                  "proposal": proposal_path})
                out["written"].append("nomination_failed")
            elif verdict.get("nominate") and not arm.get("nominee"):
                # never overwrite a live nomination: a pre-registration that can be silently
                # replaced is not a pre-registration
                nm = verdict["nominate"]
                champion.write_state(
                    exit={"nominee": nm, "nominated_at_alert_seq": seq, "nominated_ts": now_s},
                    history_line={"ts": now_s, "arm": "exit", "action": "nominate", "from": champ,
                                  "to": champ, "nominee": nm, "at_alert_seq": seq,
                                  "proposal": proposal_path})
                try:
                    TR.bump("nominations", [f"exit:{nm}@{seq}"])
                except Exception as exc:
                    print(f"  [improve] trials bump failed: {exc}")
                out["written"].append("nominate")
                _event("NOMINATED", [f"exit challenger `{nm}` nominated vs champion `{champ}`",
                                     f"forward-only trial starts at alert_seq {seq}; judged once at "
                                     f"{config.IMPROVE_FWD_MIN_POSITIONS} positions / "
                                     f"{config.IMPROVE_FWD_MIN_DAYS} alert-days",
                                     "a nomination is not a claim", f"proposal: {proposal_path}"])
    except Exception as exc:
        print(f"  [improve] apply failed: {type(exc).__name__}: {exc}")
        out["error"] = str(exc)[:200]
    _append_history(res, verdict, paused=False, applied=True)
    return out


def summary_json(res: dict, path: str | None = None) -> str:
    """data/livebook_summary.json — the weekly, committed, NaN-free digest the dashboard and
    weekly_summary.py read. Written atomically (NaN → None)."""
    path = path or config.LIVEBOOK_SUMMARY_PATH

    def _slim(t: dict) -> dict:
        return {name: {"n": r.get("n"), "mean": r.get("mean"), "day_lb": r.get("day_lb"),
                       "median": r.get("median"), "win_rate": r.get("win_rate"),
                       "paired_lb": r.get("paired_lb"), "dsr": r.get("dsr"),
                       "n_gapped": r.get("n_gapped")} for name, r in (t or {}).items()}

    a = (res.get("strata") or {}).get("A") or {}
    obj = {"ts": res["ts"], "champion": res["champion"], "stratum": res.get("stratum"),
           "n_positions": res["n_positions"], "n_days": res["n_days"],
           "n_trials": res.get("n_trials"), "per_policy": _slim(res.get("policies")),
           "controls": _slim(res.get("controls")), "inert_controls": res.get("inert_controls", []),
           "nomination": res.get("nomination"), "demotion": res.get("demotion"),
           "strata_A": {"n": a.get("n", 0), "n_days": a.get("n_days", 0)},
           "dsr_bar": res.get("dsr_bar"), "book": res.get("book"),
           "paper_gate": res.get("paper_gate"),
           "note": "Weekly livebook digest (improve.py --summary-json, the Sunday workflow). Bounds are "
                   "day-clustered bootstrap 2.5th percentiles; a bound from fewer than "
                   f"{config.MIN_BOOTSTRAP_CLUSTERS} clusters is not a bound. The paper gate is not a "
                   "promotion. Not financial advice."}
    LB._save_atomic(obj, path)
    return path


# ── entry point ──────────────────────────────────────────────────────────────────
def _print_report(res: dict, verdict: dict, path: str | None, mode: str) -> None:
    print(f"champion `{res['champion']}`  |  {res['n_positions']} completed positions across "
          f"{res['n_days']} alert-days (stratum {res.get('stratum')})  |  {res['n_trials']} "
          f"cumulative trials")
    if verdict.get("gate_broken"):
        print("\n  per-policy table suppressed: run is VOID (apparatus fault) — no number from "
              "this run may be quoted")
    elif res["policies"]:
        print(f"\n  {'policy':<24s} {'n':>4s} {'mean':>8s} {'day LB':>8s} "
              f"{'vs champ':>9s} {'paired LB':>10s} {'DSR':>6s} {'gapped':>6s}")
        rows = sorted(res["policies"].items(),
                      key=lambda kv: -(kv[1].get("paired_lb") if kv[1].get("paired_lb") is not None
                                       and not np.isnan(kv[1]["paired_lb"]) else -1e9))
        for name, r in rows:
            mark = "*" if name == res["champion"] else ("F" if r.get("forward_only") else " ")
            print(f" {mark}{name:<23s} {r['n']:>4} {_fmt(r['mean']):>8s} {_fmt(r['day_lb']):>8s} "
                  f"{_fmt(r.get('paired_mean')):>9s} {_fmt(r.get('paired_lb')):>10s} "
                  f"{_fmt(r.get('dsr'), '.3f'):>6s} {r.get('n_gapped', 0):>6}")
        for name, r in res["controls"].items():
            print(f"  {name:<23s} {r['n']:>4} {_fmt(r['mean']):>8s} {_fmt(r['day_lb']):>8s} "
                  f"{_fmt(r.get('paired_mean')):>9s} {_fmt(r.get('paired_lb')):>10s} "
                  f"{_fmt(r.get('dsr'), '.3f'):>6s} {r.get('n_gapped', 0):>6}   (control)")
        a = (res.get("strata") or {}).get("A") or {}
        print(f"\n  A stratum: {a.get('n', 0)} positions / {a.get('n_days', 0)} alert-days "
              f"(printed, not decided on while stratum = {res.get('stratum')})")
        print("  " + _dsr_sentence(res))
    would = "" if mode == "apply" else "would "
    if verdict.get("promote"):
        print(f"\n  {would}PROMOTE -> {verdict['winner']}  (all checks passed)")
    elif verdict.get("demote"):
        print(f"\n  {would}DEMOTE {res['champion']} -> {config.IMPROVE_DEFAULT_EXIT_CHAMPION}")
    else:
        print("\n  NO CHANGE. blocking / notes:")
    for r in verdict.get("reasons", []):
        print(f"    - {r}")
    if verdict.get("nomination_failed"):
        print(f"\n  {would}record NOMINATION FAILED -> {verdict['nomination_failed']} judged once on "
              f"its pre-registered forward prefix and did not clear")
    elif verdict.get("nominate"):
        print(f"\n  {would}NOMINATE -> {verdict['nominate']}  (forward-only trial from alert_seq "
              f"{res.get('max_alert_seq', -1)})")
    # A lead is not evidence. Show how often each policy has topped the table across runs, so a
    # six-week winning streak out of pure noise is visible as such rather than persuasive.
    try:
        if os.path.exists(config.IMPROVE_HISTORY_PATH):
            hist = [json.loads(ln) for ln in open(config.IMPROVE_HISTORY_PATH) if ln.strip()]
            leads = Counter(h.get("winner") for h in hist if h.get("winner"))
            if leads:
                print(f"\n  times led across {len(hist)} runs: {dict(leads.most_common(5))}  "
                      f"(leading is not evidence — only the paired bound is)")
    except Exception:
        pass
    pg = res.get("paper_gate")
    if pg:
        print(f"\n  paper gate: {pg.get('line')}  (not a promotion)")
    if path:
        print(f"\n  proposal written: {path}")


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    now_s = time.time()                          # ONCE; threaded through everything below
    quiet = "--quiet" in argv
    do_apply = "--apply" in argv
    send = "--send" in argv
    if "--band-scorecard" in argv:
        # the paper gate alone, and return BEFORE the Sunday gate: no proposal, no history
        # line, no champion read-modify-write — and, this being a read-only caller, no
        # trials.json write either (record=False: it prints the would-be lines instead)
        try:
            pg = paper_gate(now_s)
        except Exception as exc:
            print(f"  [improve] paper gate failed: {type(exc).__name__}: {exc}")
            return 0
        if not quiet:
            _print_paper_gate(pg)
        return 0
    # the paper gate's one-shot lines are minted on the APPLY path alone — the runner's Sunday
    # Gates step, whose trials.json the Publish step stages. --summary-json never applies.
    try:
        res = evaluate_all(now_s, record_paper=do_apply and "--summary-json" not in argv)
    except Exception as exc:
        print(f"  [improve] evaluate_all failed: {type(exc).__name__}: {exc}")
        return 0
    if "--summary-json" in argv:
        try:
            p = summary_json(res)
            if not quiet:
                print(f"summary written: {p}  ({res['n_positions']} positions / "
                      f"{res['n_days']} alert-days)")
        except Exception as exc:
            print(f"  [improve] summary_json failed: {exc}")
        return 0
    if res["n_positions"] == 0 and not res.get("policies"):
        verdict = {"promote": False, "winner": None, "reasons": ["insufficient (n=0)"]}
        _append_history(res, verdict, applied=do_apply)   # an --apply run stamps its day even at n=0
        if not quiet:
            print("insufficient (n=0) — no completed positions in the live book yet")
            print(f"  paper gate: {(res.get('paper_gate') or {}).get('line')}")
        return 0
    verdict = decide(res)
    try:
        path = write_proposal(res, verdict)
    except Exception as exc:
        print(f"  [improve] proposal write failed: {exc}")
        path = None
    if do_apply:
        out = apply(res, verdict, now_s, send=send, proposal_path=path)
        if not quiet:
            _print_report(res, verdict, path, "apply")
            print(f"\n  applied: {out}")
    else:
        _append_history(res, verdict)
        if not quiet:
            _print_report(res, verdict, path, "dry")
    return 0


# ── offline self-test ────────────────────────────────────────────────────────────
def _synthetic_book(n_days: int, per_day: int, t0: float, seed: int, adv: dict | None = None,
                    champ_mean: float = -0.10, ctl_mean: float = -0.05, base_sd: float = 0.5,
                    pol_sd: float = 0.15, seq0: int = 1, gapped: dict | None = None,
                    n_suspect: int = 0, n_unpriced: int = 0, inert_random: bool = False,
                    ctl_immediate_mean: float | None = None, stamp: list | None = None,
                    stamp_frac: float = 1.0) -> dict:
    """A synthetic live book: N = n_days*per_day completed positions, every policy sharing one
    position-level fate (so pairing is real) plus a planted per-policy advantage `adv`.
    `stamp` plants a `sidecar_true` list on a `stamp_frac` share of the positions (the paper
    gate's eligibility stamp); the rest carry []."""
    rng = np.random.default_rng(seed)
    adv = adv or {}
    gapped = gapped or {}
    stamp = list(stamp or [])
    book: dict = {}
    seq = seq0
    cost = float(config.STACK_USD * config.POSITION_PCT)
    ci_mean = ctl_mean if ctl_immediate_mean is None else ctl_immediate_mean
    k = 0
    for d in range(n_days):
        for i in range(per_day):
            opened = t0 + d * 86400.0 + 600.0 * (i + 1)
            token = "0x%040x" % int(rng.integers(1, 2 ** 62))
            shared = rng.normal(champ_mean, base_sd)
            kind = "promotion" if rng.random() < 0.15 else "first_sighting"
            tier = "A" if rng.random() < 0.4 else "B"
            pos = {"token": token, "event_seq": int(seq), "event_kind": kind, "symbol": "SYN",
                   "tier": tier, "ledger_tier": tier, "alert_ts": opened - 200.0,
                   "entry_lag_s": 200.0, "opened_ts": opened, "alert_seq": seq, "cost_usd": cost,
                   "tokens_raw": 10 ** 24, "entry_px": cost / 1e24, "last_tick_ts": opened + 6 * 3600,
                   "n_ticks": 360, "done": True, "suspect": False, "unpriced": False,
                   "max_gap_s": 60.0, "policies": {},
                   "sidecar_true": (list(stamp) if stamp and rng.random() < stamp_frac else [])}
            for name in POL.POLICIES:
                r = shared + adv.get(name, 0.0) + rng.normal(0.0, pol_sd)
                st = LB._new_policy_state()
                st.update(remaining=0.0, realized_usd=cost * (1.0 + r), closed=True,
                          closed_ts=opened + 3600, close_reason="time_exit", close_gap_s=60.0)
                frac = gapped.get(name, 0.0)
                if frac and rng.random() < frac:
                    st["gapped"] = True
                    st["close_gap_s"] = 900.0
                pos["policies"][name] = st
            champ_usd = pos["policies"][config.IMPROVE_DEFAULT_EXIT_CHAMPION]["realized_usd"]
            st = LB._new_policy_state()
            st.update(remaining=0.0, realized_usd=cost * (1.0 + ci_mean + rng.normal(0, 0.02)),
                      closed=True, closed_ts=opened + 60, close_reason="time_exit", close_gap_s=60.0)
            pos["policies"]["ctl_exit_immediately"] = st
            st = LB._new_policy_state()
            rr = 0.5 * (shared - champ_mean) + ctl_mean + rng.normal(0, 0.05)
            st.update(remaining=0.0, realized_usd=(champ_usd if inert_random else cost * (1.0 + rr)),
                      closed=True, closed_ts=opened + 1800, close_reason="random_exit", close_gap_s=60.0)
            pos["policies"]["ctl_random_exit"] = st
            if k < n_suspect:
                pos["suspect"] = True
            elif k < n_suspect + n_unpriced:
                pos["unpriced"] = True
            book[LB._pos_key(token, seq)] = pos
            seq += 1
            k += 1
    return book


def _selftest() -> None:
    import shutil
    import tempfile
    tmp = tempfile.mkdtemp(prefix="improve_selftest_")
    saved = {"BOOK_PATH": LB.BOOK_PATH, "MISSED_PATH": LB.MISSED_PATH,
             "CHAMPION_PATH": config.CHAMPION_PATH, "TRIALS_PATH": config.TRIALS_PATH,
             "IMPROVE_HISTORY_PATH": config.IMPROVE_HISTORY_PATH,
             "PROPOSALS_DIR": config.PROPOSALS_DIR,
             "LIVEBOOK_SUMMARY_PATH": config.LIVEBOOK_SUMMARY_PATH,
             "PAUSE_PATH": config.PAUSE_PATH, "send_all": alerts.send_all}
    LB.BOOK_PATH = os.path.join(tmp, "livebook.json")
    LB.MISSED_PATH = os.path.join(tmp, "missed.jsonl")
    config.CHAMPION_PATH = os.path.join(tmp, "champion.json")
    config.TRIALS_PATH = os.path.join(tmp, "trials.json")
    config.IMPROVE_HISTORY_PATH = os.path.join(tmp, "improve_history.jsonl")
    config.PROPOSALS_DIR = os.path.join(tmp, "proposals")
    config.LIVEBOOK_SUMMARY_PATH = os.path.join(tmp, "livebook_summary.json")
    config.PAUSE_PATH = os.path.join(tmp, "PAUSE")
    sent: list = []
    alerts.send_all = lambda title, body, dry_run=True: sent.append((title, dry_run))
    default = config.IMPROVE_DEFAULT_EXIT_CHAMPION
    CH = "sell_3h"                                # the policy the planted advantage goes to
    t0 = 1_800_000_000.0
    D, P = config.IMPROVE_PROMOTE_MIN_CLUSTERS + 2, 8   # 42 days × 8 = 336 >= 300 positions
    now = t0 + (D + 1) * 86400.0

    def _champ_file():
        return open(config.CHAMPION_PATH).read() if os.path.exists(config.CHAMPION_PATH) else ""

    def _run(book, now_s, **kw):
        LB._save_atomic(book, LB.BOOK_PATH)
        res = evaluate_all(now_s, book, **kw)
        return res, decide(res)

    try:
        print("improve.py self-test (synthetic book, offline; nothing here is data)\n")
        # G1 thin evidence refuses
        book = _synthetic_book(10, 5, t0, seed=1, adv={CH: 0.35})
        res, v = _run(book, now)
        assert not v["promote"] and v["nominate"] is None and not v["gate_broken"], v
        assert any("alert-days" in r for r in v["reasons"])
        assert any("positions" in r for r in v["reasons"])
        print(f"G1 thin evidence (n={res['n_positions']}, days={res['n_days']}): refused; "
              f"winner={v['winner']} paired_lb={_fmt(res['policies'][v['winner']]['paired_lb'])}")

        # G2 apparatus faults
        book = _synthetic_book(D, P, t0, seed=2, adv={CH: 0.35}, inert_random=True)
        res, v = _run(book, now)
        assert v["gate_broken"] and "ctl_random_exit" in res["inert_controls"], v
        print(f"G2a inert control ⇒ gate_broken: {res['inert_controls']}")
        book = _synthetic_book(D, P, t0, seed=3, adv={CH: 0.35}, ctl_immediate_mean=0.20)
        res, v = _run(book, now)
        assert v["gate_broken"] and "ctl_exit_immediately" in v["reasons"][1], v
        print(f"G2b profitable control (LB {_fmt(res['controls']['ctl_exit_immediately']['day_lb'])}) "
              f"⇒ gate_broken")
        path = write_proposal(res, v)
        txt = open(path).read()
        assert "no number from this run may be quoted" in txt.lower() and "| `sell_3h`" not in txt
        book = _synthetic_book(D, P, t0, seed=4, adv={CH: 0.35})         # controls beat the champion
        res, v = _run(book, now)
        c_lb = res["controls"]["ctl_exit_immediately"]["day_lb"]
        assert c_lb > res["champ_lb"] and c_lb < 0 and not v["gate_broken"], (c_lb, res["champ_lb"])
        print(f"G2c control beats champion (ctl LB {_fmt(c_lb)} > champ LB {_fmt(res['champ_lb'])}) "
              f"but is not profitable ⇒ NOT void")

        # G3 the bar is the control; nomination; judged alone; forward promotion
        book = _synthetic_book(D, P, t0, seed=5, adv={CH: 0.05})
        res, v = _run(book, now)
        row = res["policies"][CH]
        assert v["winner"] == CH and row["paired_lb"] > 0 and not v["promote"] and v["nominate"] is None
        assert not v["checks"][1][1], v["checks"][1]
        print(f"G3a challenger paired LB {_fmt(row['paired_lb'])} > 0 but own LB {_fmt(row['day_lb'])} "
              f"below control {_fmt(c_lb)} ⇒ no promotion, no nomination")
        book = _synthetic_book(D, P, t0, seed=6, adv={CH: 0.35})
        res, v = _run(book, now)
        assert v["winner"] == CH and v["nominate"] == CH and not v["promote"], v
        failed = [c[0] for c in v["checks"] if not c[1]]
        assert len(failed) == 1 and "FORWARD-ONLY" in failed[0]
        print(f"G3b clears every bar except forward-only ⇒ NOMINATE {CH} "
              f"(DSR {_fmt(res['policies'][CH]['dsr'], '.3f')}; the gate needs per-day Sharpe "
              f"~{_fmt(res['dsr_bar']['sharpe'], '.2f')})")
        before = _champ_file()
        out = apply(res, v, now, send=False, proposal_path=None)
        st = champion.state()["exit"]
        assert st["nominee"] == CH and st["nominated_at_alert_seq"] == res["max_alert_seq"], st
        assert "NOMINATED" in out["events"] and sent and sent[-1][1] is True   # dry_run=True
        assert TR.family_count("nominations") == 1
        nom_seq, nom_ts = st["nominated_at_alert_seq"], st["nominated_ts"]
        # a different policy now leads by a mile — the nominee is still judged ALONE
        book2 = _synthetic_book(D, P, t0, seed=7, adv={CH: 0.35, "trail_30": 0.80})
        res, v = _run(book2, now)
        assert v["winner"] == CH
        assert res["policies"]["trail_30"]["paired_lb"] > res["policies"][CH]["paired_lb"]
        assert any("awaiting its forward sample" in r for r in v["reasons"])
        print(f"G3c active nomination judged ALONE: winner={v['winner']} although trail_30 leads "
              f"(paired LB {_fmt(res['policies']['trail_30']['paired_lb'])})")
        # forward rows: alert_seq > nom_seq AND opened_ts > nom_ts, 46 days × 4/day
        fwd_t0 = now + 3600.0
        fwd = _synthetic_book(46, 4, fwd_t0, seed=8, adv={CH: 0.40}, seq0=nom_seq + 1)
        book3 = dict(book)
        book3.update(fwd)
        now2 = fwd_t0 + 47 * 86400.0
        res, v = _run(book3, now2)
        nomi = res["nomination"]
        assert nomi["matured"] and res["forward_only"] and res["policies"][CH].get("forward_only")
        assert nomi["n_forward"] >= config.IMPROVE_FWD_MIN_POSITIONS
        assert nomi["days_forward"] == config.IMPROVE_FWD_MIN_DAYS
        assert nomi["n_forward"] < len(fwd), "later forward positions must be EXCLUDED from the prefix"
        assert v["promote"] and v["winner"] == CH, v
        print(f"G3d forward prefix matured ({nomi['n_forward']} positions / {nomi['days_forward']} days "
              f"of {len(fwd)} forward rows; later rows excluded) ⇒ every check passed: "
              + ", ".join(f"{c[2]}" for c in v["checks"]))
        # G4 without --apply nothing is written
        before = _champ_file()
        write_proposal(res, v)
        _append_history(res, v)
        assert _champ_file() == before
        print("G4 dry run: champion.json unchanged")
        # G5 PAUSE ⇒ no write, PAUSED event
        open(config.PAUSE_PATH, "w").close()
        out = apply(res, v, now2, send=False)
        assert out["paused"] and _champ_file() == before and out["events"] == ["PAUSED"], out
        os.remove(config.PAUSE_PATH)
        print("G5 PAUSE: no write, PAUSED event")
        # the promotion under --apply
        path = write_proposal(res, v)
        out = apply(res, v, now2, send=False, proposal_path=path)
        st = champion.state()["exit"]
        assert st["champion"] == CH and st["previous"] == default and st["nominee"] is None
        assert st["promoted_at_alert_seq"] == res["max_alert_seq"] and st["promoted_at_ts"] == now2
        ev = st["evidence"]
        assert ev["paired_lb"] > 0 and ev["day_lb"] > ev["control_day_lb"]
        assert ev["dsr"] >= config.IMPROVE_DSR_GATE
        assert ev["forward_only"] and ev["proposal_path"] == path and "PROMOTED" in out["events"]
        assert champion.state()["history"][-1]["action"] == "promote"
        print(f"G3e --apply ⇒ champion.json: {default} → {st['champion']} with evidence "
              f"(paired LB {_fmt(ev['paired_lb'])}, own LB {_fmt(ev['day_lb'])}, DSR {_fmt(ev['dsr'], '.3f')})")

        # G6 gapped / suspect / unpriced are NaN and counted
        book = _synthetic_book(20, 5, t0, seed=9, gapped={"sell_15m": 0.5}, n_suspect=7, n_unpriced=4)
        counts: dict = {}
        rets, days, meta = live_returns(book, counts)
        assert counts["n_suspect"] == 7 and counts["n_unpriced"] == 4 and len(meta) == 100 - 11
        n15 = counts["n_gapped"]["sell_15m"]
        assert n15 > 0 and np.isnan(rets["sell_15m"]).sum() == n15
        assert not np.isnan(rets["hold_to_end"]).any()
        # a backfilled state is NaN too (row 0 = the first SCORABLE position, not the first key —
        # the first keys are the suspect ones)
        first = book[meta[0]["key"]]
        first["policies"]["sell_1h"]["backfilled_ts"] = t0
        rets, _, _ = live_returns(book)
        assert np.isnan(rets["sell_1h"][0])
        # ctl_random_exit inadmissible when it did not act
        first["policies"]["ctl_random_exit"]["close_reason"] = "time_exit"
        rets, _, _ = live_returns(book)
        assert np.isnan(rets["ctl_random_exit"][0])
        print(f"G6 exclusions: suspect {counts['n_suspect']}, unpriced {counts['n_unpriced']} dropped; "
              f"sell_15m gapped {counts['n_gapped']['sell_15m']} ⇒ NaN; backfilled ⇒ NaN; "
              f"random_exit non-acted ⇒ NaN")

        # G7 after the promotion the previous champion can be nominated as a challenger
        st = champion.state()["exit"]
        assert st["champion"] == CH
        # the champion is now sell_3h; plant the advantage on the OLD champion instead
        book = _synthetic_book(D, P, t0, seed=10, adv={default: 0.35},
                               seq0=st["promoted_at_alert_seq"] + 1)
        res, v = _run(book, now2 + 50 * 86400)
        assert res["champion"] == CH and v["winner"] == default and v["nominate"] == default, v
        print(f"G7 previous champion `{default}` is a challenger to `{CH}` and gets nominated")

        # G8 a failed nominee cannot be renominated inside the cooldown
        champion.write_state(exit={"failed_nominee": default,
                                   "failed_ts": (now2 + 50 * 86400) - 10 * 86400,
                                   "failed_at_alert_seq": 1})
        res, v = _run(book, now2 + 50 * 86400)
        assert v["winner"] != default and v["nominate"] != default
        assert any("cooldown" in r for r in v["reasons"])
        skipped_winner = v["winner"]
        champion.write_state(exit={"failed_ts": (now2 + 50 * 86400)
                                   - (config.IMPROVE_RENOMINATE_COOLDOWN_DAYS + 5) * 86400})
        res, v = _run(book, now2 + 50 * 86400)
        assert v["winner"] == default
        print(f"G8 failed nominee `{default}` inside the {config.IMPROVE_RENOMINATE_COOLDOWN_DAYS}-day "
              f"cooldown is skipped (winner falls to `{skipped_winner}`); eligible again after it")
        champion.write_state(exit={"failed_nominee": None, "failed_ts": None, "failed_at_alert_seq": None})

        # G9 demotion: the promoted champion's own forward prefix fails the control bar
        st = champion.state()["exit"]
        pseq, pts = st["promoted_at_alert_seq"], st["promoted_at_ts"]
        # pre-promotion rows (not forward) + forward rows where sell_3h loses to the control
        pre = _synthetic_book(5, 4, pts - 10 * 86400, seed=11, adv={CH: 0.35}, seq0=max(1, pseq - 19))
        fwd = _synthetic_book(46, 3, pts + 3600.0, seed=12, adv={CH: -0.10}, seq0=pseq + 1)
        book = dict(pre)
        book.update(fwd)
        now3 = pts + 48 * 86400.0
        res, v = _run(book, now3)
        dem = res["demotion"]
        assert dem and dem["matured"] and dem["n_forward"] >= config.IMPROVE_FWD_MIN_POSITIONS
        assert dem["day_lb"] <= dem["ctl_lb"] and v["demote"] and v["demotion_judged"], (dem, v)
        # the pre-promotion rows must not be in the prefix
        assert dem["n_forward"] <= len(fwd)
        # under review before maturity: a shorter forward sample
        short = dict(pre)
        short.update({k: p for k, p in fwd.items() if p["opened_ts"] < pts + 15 * 86400})
        res_s, v_s = _run(short, now3)
        assert not res_s["demotion"]["matured"] and not v_s["demote"] and v_s["champion_under_review"]
        ds = res_s["demotion"]
        print(f"G9a not matured ({ds['n_forward']} fwd / {ds['days_forward']} days): "
              f"champion under review, not judged")
        path = write_proposal(res, v)
        out = apply(res, v, now3, send=False, proposal_path=path)
        st = champion.state()["exit"]
        assert st["champion"] == default and st["previous"] == CH and st["demotion_judged"] == now3
        assert st["evidence"]["demotion"] and "DEMOTED" in out["events"]
        assert champion.state()["history"][-1]["action"] == "demote"
        print(f"G9b DEMOTED {CH} → {default}: forward own LB {_fmt(dem['day_lb'])} <= control "
              f"{dem['ctl_name']} {_fmt(dem['ctl_lb'])}; demotion_judged set")
        # a champion that KEEPS beating the control is judged once and kept
        champion.write_state(exit={"champion": CH, "previous": default, "promoted_at_alert_seq": pseq,
                                   "promoted_at_ts": pts, "promoted_ts": pts, "demotion_judged": None})
        fwd_ok = _synthetic_book(46, 3, pts + 3600.0, seed=13, adv={CH: 0.35}, seq0=pseq + 1)
        book = dict(pre)
        book.update(fwd_ok)
        res, v = _run(book, now3)
        assert res["demotion"]["matured"] and not v["demote"] and v["demotion_judged"]
        out = apply(res, v, now3, send=False)
        st = champion.state()["exit"]
        assert st["champion"] == CH and st["demotion_judged"] == now3 and "DEMOTED" not in out["events"]
        res2, _ = _run(book, now3 + 86400)
        assert res2["demotion"] is None, "the demotion test is one-shot"
        print(f"G9c a champion beating the control on its prefix (LB {_fmt(res['demotion']['day_lb'])}) "
              f"is kept; demotion_judged set; not re-judged next run")

        # G10 summary json is NaN-free; the proposal carries the honesty footer
        p = summary_json(res)
        raw = open(p).read()
        assert "NaN" not in raw and "Infinity" not in raw
        strict = json.loads(raw, parse_constant=lambda c: (_ for _ in ()).throw(ValueError(c)))
        assert strict["champion"] == CH and strict["per_policy"][CH]["n"] > 0
        assert strict["strata_A"]["n"] >= 0
        assert os.path.exists(path)
        txt = open(path).read()
        assert txt.rstrip().endswith("you cannot un-look at a result.")
        assert "Applied only under --apply" in txt
        assert "### A stratum" in txt and "DSR bar" in txt
        # the empty book path
        res0 = evaluate_all(now3, {})
        assert res0["n_positions"] == 0 and res0["policies"] == {}
        # a champion.json corrupt file → defaults, never a crash
        with open(config.CHAMPION_PATH, "w") as fh:
            fh.write("{nope")
        res0 = evaluate_all(now3, book)
        assert res0["champion"] == default
        print(f"G10 summary json strict-parses NaN-free ({len(raw)} bytes); proposal has the honesty "
              f"footer and the A-stratum table; empty book ⇒ n=0; corrupt champion.json ⇒ default")
        print(f"\n  trials.json: policies_ever_scored={TR.family_count('policies')} "
              f"nominations_ever={TR.family_count('nominations')} (only grows)")
        print("  OK — self-test assertions hold.")
    finally:
        LB.BOOK_PATH, LB.MISSED_PATH = saved["BOOK_PATH"], saved["MISSED_PATH"]
        config.CHAMPION_PATH, config.TRIALS_PATH = saved["CHAMPION_PATH"], saved["TRIALS_PATH"]
        config.IMPROVE_HISTORY_PATH = saved["IMPROVE_HISTORY_PATH"]
        config.PROPOSALS_DIR = saved["PROPOSALS_DIR"]
        config.LIVEBOOK_SUMMARY_PATH = saved["LIVEBOOK_SUMMARY_PATH"]
        config.PAUSE_PATH = saved["PAUSE_PATH"]
        alerts.send_all = saved["send_all"]
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    # no arguments: the offline self-test, then a dry pass on the live book (as evaluate.py);
    # --selftest: the self-test alone; any other flag: the real entry point.
    if "--selftest" in sys.argv[1:]:
        _selftest()
    elif len(sys.argv) == 1:
        _selftest()
        print("\n=== live book (dry) ===")
        sys.exit(main([]))
    else:
        sys.exit(main())
