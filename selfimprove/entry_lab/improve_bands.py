"""
The entry-band gate: re-score every registered band on live ledger evidence, promote a champion
AUTONOMOUSLY when every check passes and every control fails, judge a nominee exactly once on a
pre-registered forward prefix, and judge a promoted champion exactly once for demotion.

WHAT "SELF-IMPROVING" CAN HONESTLY MEAN HERE. Not an agent that searches until it finds alpha —
on the sibling screener's ledger 0 of 1,089 bucket rules survived multiple-testing correction at
any q, and on solana the strict band was the best of 14 and every relaxation was worse (n=16 —
hold it loosely). It means a loop that (1) accumulates OUT-OF-SAMPLE evidence every few minutes
(the ledger's write-once forward returns, every band's verdict recorded at the SAME instant as
the entry price), (2) re-scores a PRE-DECLARED family on it, (3) changes the champion only
through a gate that prices in every hypothesis ever tried, and (4) writes a proposal a human can
read. The improvement is in the estimate and in the willingness to be proven wrong.

=========================== THE FAILURE MODE THIS FILE IS BUILT AGAINST ===========================
A loop that re-scores ten correlated bands weekly and adopts whichever leads is running a maximum
over correlated series; under a pure null that maximum drifts upward forever. Measured on the exit
book (2026-08-15): picking the best paired bound and testing it as one hypothesis crossed zero
33.3% of the time at 12 alert-days and 20.0% at 41. More data does not fix a selection bias.
Guards, none optional:
  * The champion changes only on a PAIRED, day-clustered, timing-stratified test vs the champion
    (scorecard.day_paired_lift_lb), never on a leaderboard position.
  * Promotion is FORWARD-ONLY: today's best is NOMINATED (free — not a claim), and judged ONCE on
    the earliest matured events with event_seq > nominated_at_event_seq — the prefix is fixed by
    rule (BAND_FWD_MIN_SELECTED / BAND_FWD_MIN_DAYS), not by when the cron happens to run, so it
    is not a sequential test. A failed nominee is cleared and sits out
    BAND_RENOMINATE_COOLDOWN_DAYS.
  * Two NEGATIVE CONTROLS are scored on the same rows. ctl_random_band fires on a sha256-chosen 10%
    at a per-token delay spread like a maturation band's; ctl_inverse_band is the champion's
    complement, recomputed on the fly. A control clearing ANY bar voids the run: no number from a
    void run may be quoted. So does a miscalibrated resampler (the champion's within-stratum
    shuffled false-positive rate above BAND_SHUFFLE_MAX_FP), a champion NA on > 20% of rows, and a
    collapsed scan grid (gapped share above BAND_MAX_GAPPED_SHARE: the forward cells were sampled
    hours after their horizon, so they are not forward returns).
  * Own-bound check #3 is NET OF COST: own_lb > BAND_OWN_LB_MIN = policies.round_trip_cost()
    (slippage both sides + the fixed L2 gas term). A band that is gross-positive and net-negative
    does not justify alerting anyone.
  * Deflated Sharpe on DAY MEANS at the family's cumulative trial count
    (selfimprove/trials.json, which only grows — you cannot un-look at a result).
  * DEMOTION is pre-registered and one-shot: a promoted (non-default) champion's own forward
    prefix after promoted_at_event_seq is judged once; selection_lift_lb <= 0 (or undefined)
    reverts to config.DEFAULT_ENTRY_BAND. Reversal needs no positive proof, only failure to keep
    beating the matched pool.
  * K5: at >= BAND_PROMOTE_MIN_CLUSTERS days and >= BAND_KILL_AFTER_DAYS of span, if no band
    (champion included) clears the cost bar on its own bound, the verdict line says so: the screen
    is not adding entry signal.
==================================================================================================

    python3 selfimprove/entry_lab/improve_bands.py                 # dry: evaluate, decide, proposal
    python3 selfimprove/entry_lab/improve_bands.py --apply         # write champion/registry/trials
    python3 selfimprove/entry_lab/improve_bands.py --apply --send  # + event alerts (PROMOTED …)

--send is EVENT alerts only (PROMOTED / DEMOTED / NOMINATED / APPARATUS FAULT / PAUSED); the weekly
summary is sent elsewhere. Publishing to origin/main is run_improve.sh's job (publish.py from a
detached worktree), not this module's. PAUSE file or champion.json.locked ⇒ evaluate and report
only. time.time() is called ONCE, in main(). Randomness only via np.random.default_rng(SEED+k).
"""
from __future__ import annotations

import json
import math
import os
import sys
import time

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import config                                        # noqa: E402
from selfimprove import champion as CH               # noqa: E402
from selfimprove import policies as POL              # noqa: E402
from selfimprove import trials as TR                 # noqa: E402
from selfimprove.entry_lab import bands as B         # noqa: E402
from selfimprove.entry_lab import scorecard as SC    # noqa: E402
from selfimprove.entry_lab import store              # noqa: E402

# config.BAND_OWN_LB_MIN is the net-of-cost bar; when config does not define it, the bar IS the
# round-trip cost (the plan's definition) — never zero.
BAND_OWN_LB_MIN = float(getattr(config, "BAND_OWN_LB_MIN", None) or POL.round_trip_cost())
FOOTER = ("Generated by improve_bands.py. Applied automatically only under --apply and only when "
          "every check passed and every control failed.")


def default_paths() -> dict:
    return {"ledger": config.LEDGER_PATH, "verdicts": config.BAND_VERDICTS_PATH,
            "champion": config.CHAMPION_PATH, "registry": config.REGISTRY_PATH,
            "trials": config.TRIALS_PATH, "proposals": config.PROPOSALS_DIR,
            "history": config.ENTRY_LAB_HISTORY_PATH, "pause": config.PAUSE_PATH}


def _paths(p: dict | None) -> dict:
    d = default_paths()
    d.update(p or {})
    return d


def _atomic_json(path: str, obj) -> None:
    def clean(o):
        if isinstance(o, float) and (o != o or o in (float("inf"), float("-inf"))):
            return None
        if isinstance(o, dict):
            return {k: clean(v) for k, v in o.items()}
        if isinstance(o, (list, tuple)):
            return [clean(v) for v in o]
        return o
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w") as f:
        json.dump(clean(obj), f, indent=2, allow_nan=False)
        f.write("\n")
    os.replace(tmp, path)


def _paused(paths: dict) -> bool:
    return os.path.exists(paths["pause"]) or bool(CH.state(paths["champion"]).get("locked"))


def _fin(v) -> bool:
    try:
        return v is not None and math.isfinite(float(v))
    except (TypeError, ValueError):
        return False


# ── the forward prefix ─────────────────────────────────────────────────────────────
def forward_prefix(series: pd.DataFrame, s: np.ndarray, after_seq: int) -> dict:
    """The pre-registered forward sample: the earliest matured events (seq order) with
    event_seq > after_seq, ALL rows included (the pool needs them), cut once the band has
    selected >= BAND_FWD_MIN_SELECTED rows across >= BAND_FWD_MIN_DAYS distinct days — the day on
    which both floors are met is included WHOLE (its same-day pool must not be cut in half).
    Returns {mask, n_selected, days_selected, complete}."""
    seqs = series["event_seq"].values.astype(int)
    days = series["day"].values
    mask = np.zeros(len(series), dtype=bool)
    n_sel, dsel = 0, set()
    order = np.argsort(seqs, kind="stable")
    cut_day = None
    for i in order:
        if seqs[i] <= after_seq:
            continue
        if cut_day is not None:
            # the floors were met on cut_day: keep the REST of that day so its same-day pool is
            # whole (a partial day would drop its stratum), and stop at the first later day
            if str(days[i]) != cut_day:
                break
            mask[i] = True
            if s[i] == 1:
                n_sel += 1
            continue
        mask[i] = True
        if s[i] == 1:
            n_sel += 1
            dsel.add(str(days[i]))
            if n_sel >= config.BAND_FWD_MIN_SELECTED and len(dsel) >= config.BAND_FWD_MIN_DAYS:
                cut_day = str(days[i])
    return {"mask": mask, "n_selected": n_sel, "days_selected": len(dsel),
            "complete": n_sel >= config.BAND_FWD_MIN_SELECTED and len(dsel) >= config.BAND_FWD_MIN_DAYS}


def dsr_bar(n_days: int, n_trials: int) -> float:
    """The per-day Sharpe a NORMAL day-mean series would need for DSR >= BAND_DSR_GATE over
    n_days at n_trials (skew 0, kurtosis 3): the bar the floors imply. NaN without scipy."""
    try:
        from scipy import stats as _st
    except Exception:
        return float("nan")
    n = max(int(n_days), 2)
    z = max(int(n_trials), 1)
    e = 0.5772156649
    if z == 1:
        emax = 0.0
    else:
        emax = (1.0 / math.sqrt(n)) * ((1 - e) * _st.norm.ppf(1 - 1.0 / z)
                                       + e * _st.norm.ppf(1 - 1.0 / (z * math.e)))
    # DSR = Φ((sr − emax)·√(n−1) / √(1 + (k−1)/4·sr²)) with k=3 ⇒ denom √(1 + sr²/2); solve numerically
    target = _st.norm.ppf(config.BAND_DSR_GATE)
    lo, hi = 0.0, 10.0
    for _ in range(60):
        mid = (lo + hi) / 2
        val = (mid - emax) * math.sqrt(n - 1) / math.sqrt(1 + mid * mid / 2)
        if val >= target:
            hi = mid
        else:
            lo = mid
    return float(hi)


# ── evaluate ───────────────────────────────────────────────────────────────────────
def evaluate_all(now_s: float, led=None, verdicts=None, reg=None, champion: str | None = None,
                 paths: dict | None = None, reps: int | None = None,
                 shuffle_reps: int | None = None) -> dict:
    """Everything decide() needs, from the committed files (or injected frames). Never raises:
    an unreadable input yields an empty result with n_events = 0."""
    P = _paths(paths)
    st = CH.state(P["champion"])["entry_band"]
    champ = champion or st.get("champion") or config.DEFAULT_ENTRY_BAND
    if reg is None:
        reg = B.load_registry(P["registry"])
    if champ not in reg.names():
        print(f"  [improve_bands] champion {champ!r} is not a registered band; using "
              f"{config.DEFAULT_ENTRY_BAND}")
        champ = config.DEFAULT_ENTRY_BAND
    metric = st.get("metric") or config.BAND_OUTCOME_METRIC
    res = {"ts": float(now_s), "champion": champ, "metric": metric, "n_events": 0, "n_days": 0,
           "n_excluded": {}, "n_excluded_dark": 0, "bands": {}, "controls": {}, "inert": [],
           "fp_champion": float("nan"), "champion_na_share": float("nan"), "max_event_seq": 0,
           "nomination": None, "forward_only": False, "trials_n": TR.family_count("bands", P["trials"]),
           "champ_state": dict(st), "demotion": None, "kill_verdict": None, "span_days": 0.0,
           "own_lb_min": BAND_OWN_LB_MIN, "default_band": config.DEFAULT_ENTRY_BAND,
           "gapped_share": float("nan"), "lag_unknown": 0}
    try:
        if led is None:
            import ledger as LED
            led = LED.load(P["ledger"])
        if verdicts is None:
            verdicts = store.pivot_verdicts(store.load_verdicts(P["verdicts"]))
        elif "band" in getattr(verdicts, "columns", []):
            verdicts = store.pivot_verdicts(verdicts)
    except Exception as exc:
        print(f"  [improve_bands] inputs unreadable ({type(exc).__name__}: {exc})")
        return res
    try:
        seqs = pd.to_numeric(led["event_seq"], errors="coerce").dropna()
        res["max_event_seq"] = int(seqs.max()) if len(seqs) else 0
    except Exception:
        res["max_event_seq"] = 0
    series, excl = SC.outcome_series(led, verdicts, metric=metric)
    # Sampling health of the run grid, over the rows that CARRY a lag cell:
    #     gapped_share = gapped / (gapped + rows kept with a lag cell).
    # It is deliberately NOT computed per band: a gapped row is dropped by outcome_series before
    # any verdict column is joined, so no band's selection can be attributed to it. lag_unknown
    # (every row written before the stamp existed) is reported SEPARATELY rather than folded in —
    # during the transition it dominates, and folding it in would read as a permanent sampling
    # fault when it is only the absence of a claim. The gate's own floors (150 selected rows, 40
    # retained days) keep it from deciding anything while that is true.
    n_gapped = int(excl.get("gapped", 0) or 0)
    res["lag_unknown"] = int(excl.get("lag_unknown", 0) or 0)
    denom = n_gapped + int(len(series))
    res["gapped_share"] = float(n_gapped) / denom if denom else float("nan")
    series, n_dark = SC.exclude_dark(series, SC.champion_sources(reg, champ))
    excl["dark"] = n_dark
    res["n_excluded"], res["n_excluded_dark"] = excl, n_dark
    res["n_events"] = int(len(series))
    if len(series) == 0:
        return res
    res["n_days"] = int(series["day"].nunique())
    ts = series["alert_ts"].values.astype(float)
    res["span_days"] = float((ts.max() - ts.min()) / 86400.0)
    r = series["r"].values.astype(float)
    days = [str(d) for d in series["day"].values]
    buckets = [int(b) for b in series["age_bucket"].values]
    s_champ = SC.sel(series, champ, champ)
    res["champion_na_share"] = float(1.0 - SC.coverage(s_champ))
    res["fp_champion"] = SC.shuffled_fp_rate(s_champ, r, days, buckets, reps=shuffle_reps)

    rows, ctls = SC.table(series, reg, champ, res["trials_n"], reps=reps)
    for x in rows:
        x["n_total"] = x["n"]
        res["bands"][x["band"]] = x
    for x in ctls:
        res["controls"][x["band"]] = x
        if x.get("inert_vs_champion"):
            res["inert"].append(x["band"])
    res["inert"] += [x["band"] for x in rows if x.get("inert_vs_champion")]

    # the live nominee: its promotion-facing row is recomputed on its pre-registered prefix
    nominee, nom_seq = st.get("nominee"), st.get("nominated_at_event_seq")
    if nominee and nominee in res["bands"] and nom_seq is not None:
        s_nom = SC.sel(series, nominee, champ)
        pref = forward_prefix(series, s_nom, int(nom_seq))
        res["nomination"] = {"nominee": nominee, "nominated_at_event_seq": int(nom_seq),
                             "nominated_ts": st.get("nominated_ts"),
                             "n_forward": pref["n_selected"], "days_forward": pref["days_selected"],
                             "complete": pref["complete"]}
        if pref["complete"]:
            sub = series[pref["mask"]].reset_index(drop=True)
            row = SC.band_row(sub, nominee, champ, "candidate", res["trials_n"], reps=reps)
            row["n_total"] = res["bands"][nominee]["n"]
            row["by_keep"] = res["bands"][nominee].get("by_keep")
            row["forward_only"] = True
            row["prefix_rows"] = int(pref["mask"].sum())
            res["bands"][nominee] = row
            res["forward_only"] = True

    # the one-shot demotion test on a promoted (non-default) champion
    if (champ != config.DEFAULT_ENTRY_BAND and st.get("demotion_judged") is None
            and st.get("promoted_at_event_seq") is not None):
        pref = forward_prefix(series, s_champ, int(st["promoted_at_event_seq"]))
        dem = {"ready": pref["complete"], "n_forward": pref["n_selected"],
               "days_forward": pref["days_selected"],
               "promoted_at_event_seq": int(st["promoted_at_event_seq"])}
        if pref["complete"]:
            sub = series[pref["mask"]].reset_index(drop=True)
            lb, mean, nd = SC.selection_lift_lb(SC.sel(sub, champ, champ), sub["r"].values.astype(float),
                                                [str(d) for d in sub["day"].values],
                                                [int(b) for b in sub["age_bucket"].values], reps=reps)
            dem.update({"lift_lb": lb, "lift_mean": mean, "days": nd,
                        "prefix_rows": int(pref["mask"].sum())})
        res["demotion"] = dem

    # K5 — no entry-side edge at all, net of cost, after enough time
    if res["n_days"] >= config.BAND_PROMOTE_MIN_CLUSTERS and res["span_days"] >= config.BAND_KILL_AFTER_DAYS:
        any_edge = any(_fin(x.get("own_lb")) and x["own_lb"] > BAND_OWN_LB_MIN for x in res["bands"].values())
        if not any_edge:
            n_sel = sum(int(x.get("n_total", x["n"])) for x in res["bands"].values())
            res["kill_verdict"] = (f"entry-side edge not found at n={n_sel}, days={res['n_days']}; "
                                   f"the screen is not adding entry signal")
    return res


# ── decide ─────────────────────────────────────────────────────────────────────────
def decide(res: dict) -> dict:
    """The gate. VOID (gate_broken) on any apparatus fault, then one candidate — the live nominee
    judged ALONE, else the eligible band with the highest paired lift LB — against nine checks.
    Advisory: apply() is the only thing that writes."""
    champ = res["champion"]
    out = {"promote": False, "winner": None, "nominate": None, "nomination_failed": None,
           "demote": False, "gate_broken": False, "reasons": [], "checks": [], "void": []}
    ctl = res.get("controls", {})
    bands = res.get("bands", {})
    if res.get("n_events", 0) == 0:
        out["reasons"].append("insufficient (n=0)")
        return out

    # apparatus faults first — disqualifying, and the table is suppressed
    void = []
    for n, x in ctl.items():
        if _fin(x.get("own_lb")) and x["own_lb"] > 0:
            void.append(f"CONTROL PROFITABLE ON ITS OWN BOUND: {n} own LB {x['own_lb']:+.3f} > 0")
        if _fin(x.get("paired_lb")) and x["paired_lb"] > 0:
            void.append(f"CONTROL BEATS THE CHAMPION ON THE PAIRED BOUND: {n} paired LB {x['paired_lb']:+.3f} > 0")
    if res.get("inert"):
        ctl_inert = [n for n in res["inert"] if n in ctl]
        if ctl_inert:
            void.append("INERT CONTROL: " + ", ".join(ctl_inert) +
                        " is identical to the champion on every shared row — an alias cannot detect anything")
    fp = res.get("fp_champion")
    if _fin(fp) and fp > config.BAND_SHUFFLE_MAX_FP:
        void.append(f"RESAMPLER MISCALIBRATED: the champion's within-stratum shuffled false-positive rate "
                    f"{fp:.3f} > {config.BAND_SHUFFLE_MAX_FP}")
    gs = res.get("gapped_share")
    if _fin(gs) and gs > config.BAND_MAX_GAPPED_SHARE:
        void.append(f"SAMPLING GAP: gapped share {gs:.3f} > BAND_MAX_GAPPED_SHARE "
                    f"({config.BAND_MAX_GAPPED_SHARE}) — of the rows carrying a lag cell, that share had "
                    f"their forward cell sampled more than {config.LEDGER_MAX_CELL_LAG_S:.0f} s after the "
                    f"horizon; the scan grid collapsed and these are not forward returns")
    na = res.get("champion_na_share")
    if _fin(na) and na > 0.20:
        void.append(f"CHAMPION NA ON {na:.0%} OF ROWS (> 20%): the inverse control and the paired test "
                    f"are undefined on too much of the sample")
    if void:
        out.update({"gate_broken": True, "void": void,
                    "reasons": ["THE GATE IS MEASURING ITS OWN MACHINERY; no number from this run may be quoted."] + void})
        return out

    # demotion verdict (pre-registered one-shot) — judged before any promotion
    dem = res.get("demotion")
    if dem and dem.get("ready"):
        lb = dem.get("lift_lb")
        out["demote"] = not (_fin(lb) and lb > 0)
        out["demotion_lb"] = lb
        if out["demote"]:
            out["reasons"].append(f"DEMOTION: champion `{champ}` forward lift LB "
                                  f"{lb if not _fin(lb) else format(lb, '+.3f')} <= 0 on its pre-registered "
                                  f"prefix ({dem.get('n_forward')} selected across {dem.get('days_forward')} days) "
                                  f"→ revert to `{res.get('default_band', config.DEFAULT_ENTRY_BAND)}`")
        else:
            out["reasons"].append(f"demotion test passed once: `{champ}` forward lift LB {lb:+.3f} > 0 "
                                  f"(judged, never re-judged)")

    # the candidate
    st = res.get("champ_state") or {}
    cooldown_s = config.BAND_RENOMINATE_COOLDOWN_DAYS * 86400
    failed, failed_ts = st.get("failed_nominee"), st.get("failed_ts")
    nom = res.get("nomination")
    ctl_lifts = [x["lift_lb"] for x in ctl.values() if _fin(x.get("lift_lb"))]
    ctl_lift_max = max(ctl_lifts) if ctl_lifts else float("-inf")
    ctl_best = (max((n for n in ctl if _fin(ctl[n].get("lift_lb"))), key=lambda n: ctl[n]["lift_lb"])
                if ctl_lifts else "none defined")
    best = None
    if nom and nom["nominee"] in bands:
        best = nom["nominee"]
        if not res.get("forward_only"):
            out["reasons"].append(f"nominee `{best}` awaiting its forward sample: "
                                  f"{nom['n_forward']}/{config.BAND_FWD_MIN_SELECTED} selected across "
                                  f"{nom['days_forward']}/{config.BAND_FWD_MIN_DAYS} alert-days")
    else:
        best_lb = float("-inf")
        for name, row in bands.items():
            if name == champ:
                continue
            if row.get("coverage", 0.0) < config.BAND_MIN_COVERAGE:
                continue
            if row.get("inert_vs_champion"):
                continue
            if failed == name and _fin(failed_ts) and res["ts"] - float(failed_ts) < cooldown_s:
                out["reasons"].append(f"`{name}` is on the re-nomination cooldown "
                                      f"({(res['ts'] - float(failed_ts)) / 86400:.0f} of "
                                      f"{config.BAND_RENOMINATE_COOLDOWN_DAYS} days since it failed)")
                continue
            plb = row.get("paired_lb")
            if _fin(plb) and plb > best_lb:
                best, best_lb = name, plb
    if best is None:
        out["reasons"].append("no eligible challenger (coverage, inertness, cooldown or no paired bound)")
        return out
    row = bands[best]
    out["winner"] = best
    n_total = int(row.get("n_total", row["n"]))
    checks = [
        ("1 beats the champion on the PAIRED day-clustered lift bound",
         _fin(row.get("paired_lb")) and row["paired_lb"] > 0,
         f"paired LB {SC._fmt(row.get('paired_lb'))}"),
        (f"2 beats the best NEGATIVE CONTROL ({ctl_best}) on the lift bound",
         _fin(row.get("lift_lb")) and row["lift_lb"] > ctl_lift_max,
         f"lift LB {SC._fmt(row.get('lift_lb'))} vs control {SC._fmt(ctl_lift_max) if ctl_lifts else 'undefined'}"),
        (f"3 profitable on its OWN bound NET of cost (> {BAND_OWN_LB_MIN:.3f} = round-trip cost)",
         _fin(row.get("own_lb")) and row["own_lb"] > BAND_OWN_LB_MIN,
         f"own LB {SC._fmt(row.get('own_lb'))}"),
        (f"4 deflated Sharpe >= {config.BAND_DSR_GATE} on DAY MEANS at {res.get('trials_n')} cumulative band trials",
         _fin(row.get("dsr")) and row["dsr"] >= config.BAND_DSR_GATE,
         f"DSR {SC._fmt(row.get('dsr'), '.3f')}"),
        (f"5 >= {config.BAND_PROMOTE_MIN_CLUSTERS} retained alert-days with a selected matured row",
         int(row.get("lift_days", 0)) >= config.BAND_PROMOTE_MIN_CLUSTERS,
         f"{row.get('lift_days', 0)} days"),
        (f"6 >= {config.BAND_MIN_SELECTED} selected matured rows",
         n_total >= config.BAND_MIN_SELECTED, f"{n_total} selected"),
        ("7 tested FORWARD-ONLY on the pre-registered prefix after its nomination",
         bool(row.get("forward_only")), "nomination/test split"),
        (f"8 survives Benjamini–Yekutieli at q={config.FDR_Q} over the candidate family",
         row.get("by_keep") is True, f"p {SC._fmt(row.get('p'), '.4f')}, BY {'keep' if row.get('by_keep') else 'drop'}"),
        (f"9 coverage >= {config.BAND_MIN_COVERAGE:.0%}",
         float(row.get("coverage", 0.0)) >= config.BAND_MIN_COVERAGE, f"coverage {row.get('coverage', 0.0):.2f}"),
    ]
    failed_checks = [f"{lbl} — {detail}" for lbl, ok, detail in checks if not ok]
    out["checks"] = checks
    out["reasons"] += failed_checks
    out["promote"] = not failed_checks and not out["demote"]
    if not failed_checks and out["demote"]:
        out["reasons"].append("champion demoted this run; the nominee is re-judged next run against the default")
    only_fwd = bool(failed_checks) and all(f.startswith("7 ") for f in failed_checks)
    floors = (int(row.get("lift_days", 0)) >= config.BAND_PROMOTE_MIN_CLUSTERS
              and n_total >= config.BAND_MIN_SELECTED)
    if nom is None and only_fwd and floors and not st.get("nominee"):
        out["nominate"] = best
    if nom and res.get("forward_only") and failed_checks:
        out["nomination_failed"] = nom["nominee"]
    return out


# ── proposal ───────────────────────────────────────────────────────────────────────
def write_proposal(res: dict, verdict: dict, paths: dict | None = None) -> str:
    P = _paths(paths)
    os.makedirs(P["proposals"], exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime(res["ts"]))
    path = os.path.join(P["proposals"], f"entry-{stamp}.md")
    champ = res["champion"]
    n_sel_champ = res["bands"].get(champ, {}).get("n_total", 0) if res.get("bands") else 0
    lines = [f"# entry-band proposal — {stamp} UTC", "",
             f"- champion: `{champ}`" + (" (default)" if champ == config.DEFAULT_ENTRY_BAND else ""),
             f"- matured events: **{res.get('n_events', 0)}** across **{res.get('n_days', 0)}** alert-days "
             f"(span {res.get('span_days', 0.0):.0f} days); champion selected {n_sel_champ}",
             f"- excluded: {res.get('n_excluded', {})} (dark = sources_dark ∩ the champion's sources, both arms)",
             f"- cumulative distinct band trials ever scored: **{res.get('trials_n')}** (Deflated Sharpe denominator)",
             f"- metric: `{res.get('metric')}`; bounds at the {config.BOOTSTRAP_ALPHA:.1%} quantile, "
             f"{config.BOOTSTRAP_REPS} day resamples", ""]
    if res.get("n_events", 0) == 0:
        lines += ["insufficient (n=0): no matured event rows yet.", ""]
    elif verdict.get("gate_broken"):
        lines += ["**Per-band table suppressed — this run is VOID**: no number from this run may be quoted.", ""]
        for v in verdict.get("void", []):
            lines.append(f"- {v}")
        lines.append("")
    else:
        rows = list(res["bands"].values())
        ctls = list(res["controls"].values())
        lines.append(SC.markdown(rows, ctls, {"champion": champ, "n_events": res["n_events"],
                                              "n_days": res["n_days"], "n_excluded": res["n_excluded"],
                                              "n_trials": res.get("trials_n"), "metric": res.get("metric"),
                                              "fp_champion": res.get("fp_champion")}))
        lines += ["### controls", ""]
        for n, x in res["controls"].items():
            lines.append(f"- `{n}`: n={x['n']}, own LB {SC._fmt(x.get('own_lb'))}, lift LB {SC._fmt(x.get('lift_lb'))}, "
                         f"paired LB vs champion {SC._fmt(x.get('paired_lb'))}, inert {'YES' if x.get('inert_vs_champion') else 'no'} "
                         f"— must fail every bar")
        lines.append(f"- champion shuffled false-positive rate: {SC._fmt(res.get('fp_champion'), '.3f')} "
                     f"(void above {config.BAND_SHUFFLE_MAX_FP}); champion NA share "
                     f"{SC._fmt(res.get('champion_na_share'), '.3f')} (void above 0.20)")
        lines.append(f"- sampling health: gapped share {SC._fmt(res.get('gapped_share'), '.3f')} over the rows "
                     f"that CARRY a lag cell (void above {config.BAND_MAX_GAPPED_SHARE}); "
                     f"{res.get('lag_unknown', 0)} row(s) written before the lag stamp existed are reported "
                     f"separately and scored by nobody")
        lines.append("")
    nom = res.get("nomination")
    if nom:
        lines += [f"### nominee `{nom['nominee']}` (pre-registered at event_seq {nom['nominated_at_event_seq']})",
                  "", f"- forward sample: {nom['n_forward']}/{config.BAND_FWD_MIN_SELECTED} selected across "
                      f"{nom['days_forward']}/{config.BAND_FWD_MIN_DAYS} alert-days"
                      + (" — COMPLETE, judged once below" if nom.get("complete") else " — still filling"), ""]
    dem = res.get("demotion")
    if dem:
        lines += [f"### demotion test on `{champ}` (promoted at event_seq {dem.get('promoted_at_event_seq')})", "",
                  f"- forward sample: {dem.get('n_forward')}/{config.BAND_FWD_MIN_SELECTED} selected across "
                  f"{dem.get('days_forward')}/{config.BAND_FWD_MIN_DAYS} days"
                  + (f"; forward lift LB {SC._fmt(dem.get('lift_lb'))} → "
                     f"{'DEMOTE' if verdict.get('demote') else 'keep'}" if dem.get("ready") else " — not yet judged"),
                  ""]
    if verdict.get("promote"):
        lines += [f"## PROMOTE → `{verdict['winner']}`", "", "Every check passed and every control failed:", ""]
    elif verdict.get("winner"):
        lines += [f"## NO CHANGE — candidate `{verdict['winner']}`", ""]
    else:
        lines += ["## NO CHANGE", ""]
    for lbl, ok, detail in verdict.get("checks", []):
        lines.append(f"- [{'x' if ok else ' '}] {lbl} ({detail})")
    if verdict.get("checks"):
        lines.append("")
    if verdict.get("nominate"):
        lines += [f"**NOMINATE** `{verdict['nominate']}` at event_seq {res.get('max_event_seq')} (recorded only "
                  f"under --apply): from then on it alone is on trial, on rows it has never seen.", ""]
    if verdict.get("nomination_failed"):
        lines += [f"**NOMINATION FAILED** `{verdict['nomination_failed']}`: judged once on its pre-registered "
                  f"forward prefix and did not clear; cleared, cooldown "
                  f"{config.BAND_RENOMINATE_COOLDOWN_DAYS} days.", ""]
    if verdict.get("demote"):
        lines += [f"**DEMOTED** `{champ}` → `{config.DEFAULT_ENTRY_BAND}` (forward lift bound did not stay above zero).", ""]
    if verdict.get("reasons"):
        lines += ["Blocking / notes:", ""] + [f"- {r}" for r in verdict["reasons"]] + [""]
    if res.get("kill_verdict"):
        lines += [f"**K5**: {res['kill_verdict']}", ""]
    w = res["bands"].get(verdict.get("winner") or "", {}) if res.get("bands") else {}
    lines += ["### sample honesty", "",
              f"- n={w.get('n', 0)} selected across {w.get('lift_days', 0)} alert-days for the candidate; "
              f"below {config.MIN_BOOTSTRAP_CLUSTERS} clusters is not a bound, and promotion needs "
              f"{config.BAND_PROMOTE_MIN_CLUSTERS} (12 clusters measured 6.3% actual coverage against a nominal 2.5%).",
              f"- DSR bar: a per-day Sharpe of ≥ {dsr_bar(config.BAND_PROMOTE_MIN_CLUSTERS, res.get('trials_n') or 1):.2f} "
              f"over {config.BAND_PROMOTE_MIN_CLUSTERS} days at {res.get('trials_n')} trials (normal day means) "
              f"is what check 4 implies at these floors.",
              "- Means are context; the decision criterion is a day-clustered lower bound, paired against the "
              "champion and stratified by sighting age. A-tier = survival odds, not predicted ROI. Not financial advice.",
              "", "---", "", FOOTER]
    with open(path, "w") as fh:
        fh.write("\n".join(lines) + "\n")
    return path


# ── apply ──────────────────────────────────────────────────────────────────────────
def _flip_registry(path: str, new_champ: str, old_champ: str) -> bool:
    """status: old champion -> candidate, new -> champion (atomic rewrite). False on failure."""
    try:
        with open(path) as f:
            raw = json.load(f)
        for e in raw.get("candidates", []):
            if e.get("kind") != "band":
                continue
            if e.get("name") == old_champ and e.get("status") == "champion":
                e["status"] = "candidate"
            if e.get("name") == new_champ:
                e["status"] = "champion"
        _atomic_json(path, raw)
        return True
    except Exception as exc:
        print(f"  [improve_bands] registry flip failed ({type(exc).__name__}: {exc})")
        return False


def _evidence(row: dict, verdict: dict) -> dict:
    ev = {k: (row.get(k) if _fin(row.get(k)) else None)
          for k in ("n", "n_total", "lift_days", "coverage", "mean", "own_lb", "lift_lb", "paired_lb",
                    "dsr", "p", "median_age_s")}
    ev["by_keep"] = row.get("by_keep")
    ev["forward_only"] = bool(row.get("forward_only"))
    ev["checks"] = [[lbl, bool(ok), detail] for lbl, ok, detail in verdict.get("checks", [])]
    ev["own_lb_min"] = BAND_OWN_LB_MIN
    return ev


def apply(res: dict, verdict: dict, now_s: float, send: bool = False,
          paths: dict | None = None) -> dict:
    """Write what decide() decided. Respects the PAUSE file / locked (report only). Returns
    {applied, paused, promoted, nominated, nomination_failed, demoted, demotion_judged, events}."""
    from alerts import format_event, send_all
    P = _paths(paths)
    out = {"applied": False, "paused": False, "promoted": None, "nominated": None,
           "nomination_failed": None, "demoted": None, "demotion_judged": False, "events": []}
    champ = res["champion"]

    def _event(kind: str, lines: list) -> None:
        out["events"].append(kind)
        title, body = format_event(kind, lines)
        send_all(title, body, dry_run=not send)

    would = [k for k, v in (("promote", verdict.get("promote")), ("nominate", verdict.get("nominate")),
                            ("nomination_failed", verdict.get("nomination_failed")),
                            ("demote", verdict.get("demote"))) if v]
    if _paused(P):
        out["paused"] = True
        _event("PAUSED", [f"entry gate: PAUSED (PAUSE file or champion.json.locked) — report only",
                          f"champion `{champ}`; would have: {', '.join(would) or 'nothing'}"])
        _history(res, verdict, out, P)
        return out
    if verdict.get("gate_broken"):
        _event("APPARATUS FAULT", ["entry gate VOID — the apparatus is measuring itself:"] + verdict.get("void", []))
        _history(res, verdict, out, P)
        return out

    dem = res.get("demotion")
    if dem and dem.get("ready"):
        st = CH.state(P["champion"])["entry_band"]
        if verdict.get("demote"):
            prev = champ
            CH.write_state(entry_band={"champion": config.DEFAULT_ENTRY_BAND, "previous": prev,
                                       "promoted_ts": None, "promoted_at_event_seq": None,
                                       "demotion_judged": now_s, "nominee": None,
                                       "evidence": {"demoted": prev, "forward_lift_lb": dem.get("lift_lb"),
                                                    "forward_lift_mean": dem.get("lift_mean"),
                                                    "n_forward": dem.get("n_forward"),
                                                    "days_forward": dem.get("days_forward"),
                                                    "promoted_at_event_seq": dem.get("promoted_at_event_seq"),
                                                    "prior_evidence": st.get("evidence")}},
                           history_line={"ts": now_s, "arm": "entry_band", "action": "demote", "from": prev,
                                         "to": config.DEFAULT_ENTRY_BAND,
                                         "reason": f"forward lift LB {SC._fmt(dem.get('lift_lb'))} <= 0"},
                           path=P["champion"])
            _flip_registry(P["registry"], config.DEFAULT_ENTRY_BAND, prev)
            TR.bump("nominations", [f"demotion:{prev}@{dem.get('promoted_at_event_seq')}"], P["trials"])
            out["demoted"] = prev
            out["applied"] = True
            _event("DEMOTED", [f"entry band: {prev} → {config.DEFAULT_ENTRY_BAND}",
                               f"forward lift LB {SC._fmt(dem.get('lift_lb'))} on {dem.get('n_forward')} selected "
                               f"across {dem.get('days_forward')} days after event_seq {dem.get('promoted_at_event_seq')}",
                               "Reversal needs no positive proof — only failure to keep beating the matched pool."])
        else:
            CH.write_state(entry_band={"demotion_judged": now_s},
                           history_line={"ts": now_s, "arm": "entry_band", "action": "demotion_kept",
                                         "champion": champ, "forward_lift_lb": dem.get("lift_lb")},
                           path=P["champion"])
            out["applied"] = True
        out["demotion_judged"] = True

    if verdict.get("promote") and verdict.get("winner"):
        winner = verdict["winner"]
        row = res["bands"].get(winner, {})
        CH.write_state(entry_band={"champion": winner, "previous": champ, "promoted_ts": now_s,
                                   "promoted_at_event_seq": res.get("max_event_seq"),
                                   "evidence": _evidence(row, verdict), "nominee": None,
                                   "nominated_at_event_seq": None, "nominated_ts": None,
                                   "demotion_judged": None, "metric": res.get("metric")},
                       history_line={"ts": now_s, "arm": "entry_band", "action": "promote", "from": champ,
                                     "to": winner,
                                     "evidence_summary": f"paired LB {SC._fmt(row.get('paired_lb'))}, own LB "
                                                         f"{SC._fmt(row.get('own_lb'))}, DSR {SC._fmt(row.get('dsr'), '.3f')}, "
                                                         f"n={row.get('n')} days={row.get('lift_days')}"},
                       path=P["champion"])
        _flip_registry(P["registry"], winner, champ)
        out["promoted"] = winner
        out["applied"] = True
        _event("PROMOTED", [f"entry band: {champ} → {winner}",
                            f"paired LB {SC._fmt(row.get('paired_lb'))}  lift LB {SC._fmt(row.get('lift_lb'))}  "
                            f"own LB {SC._fmt(row.get('own_lb'))} (bar {BAND_OWN_LB_MIN:.3f})  DSR {SC._fmt(row.get('dsr'), '.3f')}",
                            f"n={row.get('n')} selected across {row.get('lift_days')} alert-days, forward-only "
                            f"after event_seq {res.get('nomination', {}).get('nominated_at_event_seq') if res.get('nomination') else '?'}",
                            "Every check passed and every control failed. The next run starts its one-shot demotion clock."])
    elif verdict.get("nomination_failed"):
        name = verdict["nomination_failed"]
        CH.write_state(entry_band={"nominee": None, "nominated_at_event_seq": None, "nominated_ts": None,
                                   "failed_nominee": name, "failed_ts": now_s},
                       history_line={"ts": now_s, "arm": "entry_band", "action": "nomination_failed",
                                     "nominee": name, "reasons": verdict.get("reasons", [])},
                       path=P["champion"])
        out["nomination_failed"] = name
        out["applied"] = True
        _event("NOMINATION FAILED", [f"entry band nominee {name} judged once on its pre-registered forward prefix "
                                     f"and did not clear; cooldown {config.BAND_RENOMINATE_COOLDOWN_DAYS} days"]
               + [f"- {r}" for r in verdict.get("reasons", [])])
    elif verdict.get("nominate"):
        name = verdict["nominate"]
        seq = int(res.get("max_event_seq") or 0)
        CH.write_state(entry_band={"nominee": name, "nominated_at_event_seq": seq, "nominated_ts": now_s,
                                   "metric": res.get("metric")},
                       history_line={"ts": now_s, "arm": "entry_band", "action": "nominate", "nominee": name,
                                     "at_event_seq": seq},
                       path=P["champion"])
        TR.bump("nominations", [f"band:{name}@{seq}"], P["trials"])
        out["nominated"] = name
        out["applied"] = True
        _event("NOMINATED", [f"entry band nominee: {name} (champion stays {champ})",
                             f"forward-only trial starts at event_seq {seq}: judged once when "
                             f"{config.BAND_FWD_MIN_SELECTED} selected rows across {config.BAND_FWD_MIN_DAYS} days have matured"])
    _history(res, verdict, out, P)
    return out


def _history(res: dict, verdict: dict, applied: dict | None, P: dict) -> None:
    line = {"ts": res.get("ts"), "n_events": res.get("n_events", 0), "n_days": res.get("n_days", 0),
            "champion": res.get("champion"), "winner": verdict.get("winner"),
            "promote": bool(verdict.get("promote")), "nominated": verdict.get("nominate"),
            "demoted": bool(verdict.get("demote")), "gate_broken": bool(verdict.get("gate_broken")),
            "fp_champion": res.get("fp_champion") if _fin(res.get("fp_champion")) else None,
            "kill": res.get("kill_verdict"), "applied": bool((applied or {}).get("applied")),
            "paused": bool((applied or {}).get("paused"))}
    try:
        d = os.path.dirname(P["history"])
        if d:
            os.makedirs(d, exist_ok=True)
        with open(P["history"], "a") as fh:
            fh.write(json.dumps(line, allow_nan=False) + "\n")
    except Exception as exc:
        print(f"  [improve_bands] history append failed ({type(exc).__name__}: {exc})")


# ── CLI ────────────────────────────────────────────────────────────────────────────
def _print(res: dict, verdict: dict, applied: dict | None, path: str | None) -> None:
    print(f"entry gate: champion `{res['champion']}`  |  {res.get('n_events', 0)} matured events across "
          f"{res.get('n_days', 0)} alert-days  |  {res.get('trials_n')} cumulative band trials  |  "
          f"own-bound bar {BAND_OWN_LB_MIN:.3f} (net of cost)")
    if res.get("n_events", 0) == 0:
        print("  insufficient (n=0)")
    elif verdict.get("gate_broken"):
        print("  per-band table suppressed: run is VOID (apparatus fault)")
        for v in verdict.get("void", []):
            print(f"    - {v}")
    else:
        print(f"  {'band':<26s} {'n':>4s} {'days':>4s} {'mean':>7s} {'own LB':>7s} {'lift LB':>8s} "
              f"{'paired':>7s} {'DSR':>6s} {'p':>6s} {'BY':>4s} {'age':>6s}")
        for x in list(res["bands"].values()) + list(res["controls"].values()):
            mark = "*" if x["band"] == res["champion"] else ("c" if x["status"] == "control" else " ")
            age = x.get("median_age_s")
            print(f" {mark}{x['band']:<25s} {x['n']:>4} {x.get('lift_days', 0):>4} {SC._fmt(x['mean']):>7s} "
                  f"{SC._fmt(x['own_lb']):>7s} {SC._fmt(x['lift_lb']):>8s} {SC._fmt(x['paired_lb']):>7s} "
                  f"{SC._fmt(x['dsr'], '.3f'):>6s} {SC._fmt(x['p'], '.3f'):>6s} "
                  f"{'·' if x.get('by_keep') is None else ('keep' if x['by_keep'] else 'drop'):>4s} "
                  f"{('·' if not _fin(age) else format(age / 3600, '.1f') + 'h'):>6s}")
        print(f"  champion shuffled fp {SC._fmt(res.get('fp_champion'), '.3f')}  NA share "
              f"{SC._fmt(res.get('champion_na_share'), '.3f')}  gapped share "
              f"{SC._fmt(res.get('gapped_share'), '.3f')} (void above {config.BAND_MAX_GAPPED_SHARE})  "
              f"lag_unknown rows {res.get('lag_unknown', 0)}")
    if verdict.get("promote"):
        print(f"\n  PROMOTE -> {verdict['winner']}  (all checks passed, every control failed)")
    else:
        print("\n  NO CHANGE." + (f" candidate `{verdict['winner']}`" if verdict.get("winner") else ""))
    for lbl, ok, detail in verdict.get("checks", []):
        print(f"    [{'x' if ok else ' '}] {lbl} ({detail})")
    for r in verdict.get("reasons", []):
        print(f"    - {r}")
    if verdict.get("nominate"):
        print(f"  NOMINATE -> {verdict['nominate']} at event_seq {res.get('max_event_seq')}")
    if verdict.get("nomination_failed"):
        print(f"  NOMINATION FAILED -> {verdict['nomination_failed']}")
    if verdict.get("demote"):
        print(f"  DEMOTE -> {config.DEFAULT_ENTRY_BAND}")
    if res.get("kill_verdict"):
        print(f"  K5: {res['kill_verdict']}")
    if applied is not None:
        print(f"  applied: {applied}")
    if path:
        print(f"  proposal written: {path}")


def main(argv: list) -> int:
    apply_flag, send, quiet = "--apply" in argv, "--send" in argv, "--quiet" in argv
    now_s = time.time()
    res = evaluate_all(now_s)
    if res.get("n_events", 0) == 0:
        if not quiet:
            print("insufficient (n=0)")
        return 0
    verdict = decide(res)
    path = write_proposal(res, verdict)
    applied = apply(res, verdict, now_s, send=send) if apply_flag else None
    if applied is None:
        _history(res, verdict, None, _paths(None))
    if not quiet:
        _print(res, verdict, applied, path)
    return 0


# ── offline self-test ──────────────────────────────────────────────────────────────
def _fixture(d: str, n_days: int = 60, planted: str = "band_score60", planted_edge=True,
             per_day: int = 12, edge_until_seq: int | None = None, start_ts: float = 1_780_012_800.0,
             control_edge: bool = False, flat_edge: bool = False, seed: int | None = None,
             gapped_frac: float = 0.0) -> dict:
    """Write a ledger + sidecar + champion + registry + trials into `d` from scorecard's
    synthetic series. Returns the paths dict. edge_until_seq: rows after it lose the planted
    edge (demotion fixture); control_edge: ctl_random_band picks only the +3 rows (void fixture);
    flat_edge: the planted band's rows carry +0.02 (gross-positive, net-negative);
    gapped_frac: this share of rows (deterministic, every 1/gapped_frac-th by event_seq) carries a
    lag cell beyond LEDGER_MAX_CELL_LAG_S — the collapsed-scan-grid fixture."""
    import ledger as LED
    ser = SC.synthetic_series(n_days=n_days, per_day=per_day, planted=planted, seed=seed,
                              champion=config.DEFAULT_ENTRY_BAND, start_ts=start_ts, planted_edge=planted_edge)
    rng = np.random.default_rng((seed or config.SEED) + 5)
    if edge_until_seq is not None:
        late = (ser["event_seq"] > edge_until_seq) & (ser[planted] == 1.0)
        ser.loc[late, "r"] = np.where(rng.random(int(late.sum())) < 0.9, -0.9, 3.0)
    if control_edge:
        ser["ctl_random_band"] = (ser["r"] >= 3.0).astype(float)
    if flat_edge:
        ser.loc[ser[planted] == 1.0, "r"] = 0.02 + rng.normal(0, 0.01, int((ser[planted] == 1.0).sum()))
    # band-fire rows re-use the token of the same day's (j-8)th first sighting and sit
    # sighting_age_s after it, so outcome_series recomputes the same age bucket the series carries
    for d0 in range(0, len(ser), per_day):
        for j in range(min(8, per_day), per_day):
            i, i0 = d0 + j, d0 + j - 8
            ser.at[i, "token"] = ser.at[i0, "token"]
            ser.at[i, "alert_ts"] = float(ser.at[i0, "alert_ts"]) + float(ser.at[i, "sighting_age_s"])
    cols = {c: "" for c in LED.COLUMNS}
    every = int(round(1.0 / gapped_frac)) if gapped_frac > 0 else 0
    rows = []
    for _, x in ser.iterrows():
        rec = dict(cols)
        seq = int(x["event_seq"])
        late = bool(every and seq % every == 0)
        rec.update({"token": x["token"], "symbol": "SYN", "tier": "B", "band": config.DEFAULT_ENTRY_BAND,
                    "fired_band": "", "event_seq": seq, "event_kind": x["event_kind"],
                    "alert_ts": float(x["alert_ts"]), "entry_price": 1.0, "entry_mcap": 1e6, "entry_liq": 5e4,
                    "entry_score": 50.0, "plan_name": config.IMPROVE_DEFAULT_EXIT_CHAMPION, "sources_dark": "",
                    "ret_6h": float(x["r"]), "status": "open", "max_ret_seen": 0.0, "min_ret_seen": 0.0,
                    "lag_6h": (config.LEDGER_MAX_CELL_LAG_S * 10.0 if late else 60.0)})
        rows.append(rec)
    led = pd.DataFrame(rows, columns=LED.COLUMNS)
    P = {"ledger": os.path.join(d, "ledger.csv"), "verdicts": os.path.join(d, "band_verdicts.csv"),
         "champion": os.path.join(d, "champion.json"), "registry": os.path.join(d, "registry.json"),
         "trials": os.path.join(d, "trials.json"), "proposals": os.path.join(d, "proposals"),
         "history": os.path.join(d, "entry_lab_history.jsonl"), "pause": os.path.join(d, "PAUSE")}
    LED.save(led, P["ledger"])
    names = [n for n in B.BUILTINS if n != SC.INVERSE]
    store.append_verdicts([{"event_seq": int(x["event_seq"]), "token": x["token"], "alert_ts": float(x["alert_ts"]),
                            "verdicts": {n: bool(x[n]) for n in names}} for _, x in ser.iterrows()], P["verdicts"])
    _atomic_json(P["registry"], {"schema": 2, "candidates": [
        {"name": n, "kind": "band", "module": B.BUILTIN_MODULE,
         "status": "control" if s.KIND == "control" else ("champion" if n == config.DEFAULT_ENTRY_BAND else "candidate")}
        for n, s in B.BUILTINS.items()]})
    TR.bump("bands", list(B.BUILTINS), P["trials"])
    CH.write_state(entry_band={"champion": config.DEFAULT_ENTRY_BAND}, path=P["champion"])
    return P


def _selftest() -> None:
    import shutil
    import tempfile
    t_start = time.time()
    champ0 = config.DEFAULT_ENTRY_BAND
    planted = "band_score60"
    SH = 200                                            # shuffle reps in the self-test (config default = 1000)
    print(f"improve_bands self-test (offline; own-bound bar {BAND_OWN_LB_MIN:.3f})")
    base = tempfile.mkdtemp(prefix="rh_entry_lab_")
    try:
        # (i) the planted band clears everything except forward-only ⇒ NOMINATED; sticky on a second run
        d = os.path.join(base, "i"); os.makedirs(d)
        P = _fixture(d)
        now = 1_780_012_800.0 + 61 * 86400
        res = evaluate_all(now, paths=P, shuffle_reps=SH)
        v = decide(res)
        print(f"  (i) n={res['n_events']} days={res['n_days']} fp={res['fp_champion']:.3f} winner={v['winner']} "
              f"promote={v['promote']} nominate={v['nominate']}")
        assert res["n_events"] == 720 and res["n_days"] == 60 and v["winner"] == planted
        assert not v["promote"] and v["nominate"] == planted and not v["gate_broken"]
        failed = [c for c in v["checks"] if not c[1]]
        assert len(failed) == 1 and failed[0][0].startswith("7 "), failed
        assert res["bands"][planted]["n_total"] == 180 and res["bands"][planted]["lift_days"] == 60
        path = write_proposal(res, v, P)
        md = open(path).read()
        assert "**NOMINATE**" in md and FOOTER in md and "median age" in md and "DSR bar" in md
        assert "no number from this run may be quoted" not in md
        # (iii) dry: nothing written to champion.json
        _history(res, v, None, P)
        assert CH.state(P["champion"])["entry_band"].get("nominee") is None
        print("  (iii) dry run: champion.json untouched")
        a = apply(res, v, now, send=False, paths=P)
        st = CH.state(P["champion"])["entry_band"]
        assert a["nominated"] == planted and st["nominee"] == planted and st["nominated_at_event_seq"] == 720
        assert TR.load(P["trials"])["nominations_ever"] == [f"band:{planted}@720"]
        res2 = evaluate_all(now + 86400, paths=P, shuffle_reps=SH)
        v2 = decide(res2)
        assert res2["nomination"]["nominee"] == planted and not res2["forward_only"]
        assert v2["nominate"] is None and v2["winner"] == planted and not v2["promote"]
        assert any("awaiting its forward sample" in r for r in v2["reasons"])
        a2 = apply(res2, v2, now + 86400, send=False, paths=P)
        assert not a2["applied"] and CH.state(P["champion"])["entry_band"]["nominee"] == planted, "sticky"
        hist = [json.loads(l) for l in open(P["history"]) if l.strip()]
        assert len(hist) == 3 and hist[1]["nominated"] == planted and hist[1]["applied"]
        print(f"  (i) NOMINATED {planted}@720, sticky on the second run; history {len(hist)} lines")

        # (ii) after enough forward rows the nominee PROMOTES under --apply
        d2 = os.path.join(base, "ii"); os.makedirs(d2)
        P2 = _fixture(d2, n_days=105)
        CH.write_state(entry_band={"nominee": planted, "nominated_at_event_seq": 720, "nominated_ts": now},
                       path=P2["champion"])
        now2 = 1_780_012_800.0 + 106 * 86400
        res = evaluate_all(now2, paths=P2, shuffle_reps=SH)
        v = decide(res)
        nom = res["nomination"]
        print(f"  (ii) forward prefix: {nom['n_forward']} selected / {nom['days_forward']} days, complete={nom['complete']}; "
              f"row n={res['bands'][planted]['n']} prefix_rows={res['bands'][planted].get('prefix_rows')}; promote={v['promote']}")
        assert res["forward_only"] and nom["complete"] and nom["n_forward"] == 120 and nom["days_forward"] == 40
        assert res["bands"][planted]["forward_only"] and res["bands"][planted]["prefix_rows"] == 480
        assert v["promote"] and v["winner"] == planted and all(c[1] for c in v["checks"]), v["reasons"]
        before = json.load(open(P2["champion"]))
        a = apply(res, v, now2, send=False, paths=P2)
        st = CH.state(P2["champion"])["entry_band"]
        assert a["promoted"] == planted and st["champion"] == planted and st["previous"] == champ0
        assert st["promoted_at_event_seq"] == res["max_event_seq"] == 1260 and st["nominee"] is None
        assert st["evidence"]["paired_lb"] > 0 and st["evidence"]["forward_only"] and len(st["evidence"]["checks"]) == 9
        assert st["demotion_judged"] is None
        reg2 = json.load(open(P2["registry"]))
        status = {e["name"]: e["status"] for e in reg2["candidates"]}
        assert status[planted] == "champion" and status[champ0] == "candidate"
        hist = [json.loads(l) for l in open(P2["history"]) if l.strip()]
        assert hist[-1]["promote"] and hist[-1]["applied"] and hist[-1]["winner"] == planted
        assert CH.state(P2["champion"])["history"][-1]["action"] == "promote"
        assert "PROMOTED" in a["events"]
        assert "PROMOTE" in open(write_proposal(res, v, P2)).read()
        print(f"  (ii) PROMOTED {champ0} → {planted}: champion.json evidence written, registry flipped, history appended")

        # (iv) PAUSE file ⇒ report only
        open(P2["pause"], "w").write("")
        CH.write_state(entry_band={"champion": champ0, "previous": None, "promoted_ts": None,
                                   "promoted_at_event_seq": None, "nominee": planted,
                                   "nominated_at_event_seq": 720, "nominated_ts": now}, path=P2["champion"])
        snap = open(P2["champion"]).read()
        res = evaluate_all(now2, paths=P2, shuffle_reps=SH)
        v = decide(res)
        assert v["promote"]
        a = apply(res, v, now2, send=False, paths=P2)
        assert a["paused"] and not a["applied"] and a["events"] == ["PAUSED"]
        assert open(P2["champion"]).read() == snap, "paused: nothing written"
        os.remove(P2["pause"])
        print("  (iv) PAUSE file: report only, champion.json byte-identical")

        # (v) a random control with own_lb > 0 ⇒ gate_broken, table suppressed
        d5 = os.path.join(base, "v"); os.makedirs(d5)
        P5 = _fixture(d5, control_edge=True)
        res = evaluate_all(now, paths=P5, shuffle_reps=SH)
        v = decide(res)
        print(f"  (v) ctl_random_band own LB {res['controls']['ctl_random_band']['own_lb']:+.3f} ⇒ gate_broken={v['gate_broken']}")
        assert v["gate_broken"] and v["winner"] is None and not v["nominate"]
        md = open(write_proposal(res, v, P5)).read()
        assert "no number from this run may be quoted" in md and "| `band_score60` |" not in md
        a = apply(res, v, now, send=False, paths=P5)
        assert a["events"] == ["APPARATUS FAULT"] and not a["applied"]
        assert CH.state(P5["champion"])["entry_band"].get("nominee") is None

        # (vi) check 3 fails when own_lb is between 0 and the round-trip cost
        d6 = os.path.join(base, "vi"); os.makedirs(d6)
        P6 = _fixture(d6, flat_edge=True)
        res = evaluate_all(now, paths=P6, shuffle_reps=SH)
        v = decide(res)
        row = res["bands"][planted]
        c3 = v["checks"][2]
        print(f"  (vi) planted own LB {row['own_lb']:+.4f} (0 < LB < {BAND_OWN_LB_MIN:.3f}), lift LB {row['lift_lb']:+.3f}: "
              f"check 3 ok={c3[1]}")
        assert v["winner"] == planted and 0 < row["own_lb"] < BAND_OWN_LB_MIN and row["lift_lb"] > 0
        assert not c3[1] and c3[0].startswith("3 ") and v["nominate"] is None

        # (vii) a promoted non-default champion whose forward lift <= 0 is DEMOTED to the default
        d7 = os.path.join(base, "vii"); os.makedirs(d7)
        P7 = _fixture(d7, n_days=100, edge_until_seq=300)
        CH.write_state(entry_band={"champion": planted, "previous": champ0, "promoted_ts": now,
                                   "promoted_at_event_seq": 300, "evidence": {"paired_lb": 0.5}}, path=P7["champion"])
        raw = json.load(open(P7["registry"]))
        for e in raw["candidates"]:
            e["status"] = "champion" if e["name"] == planted else ("candidate" if e["name"] == champ0 else e["status"])
        _atomic_json(P7["registry"], raw)
        now7 = 1_780_012_800.0 + 101 * 86400
        res = evaluate_all(now7, paths=P7, shuffle_reps=SH)
        v = decide(res)
        dem = res["demotion"]
        print(f"  (vii) champion {res['champion']} demotion: ready={dem['ready']} lift LB {SC._fmt(dem.get('lift_lb'))} "
              f"on {dem['n_forward']}/{dem['days_forward']} ⇒ demote={v['demote']}")
        assert res["champion"] == planted and dem["ready"] and not (dem["lift_lb"] > 0) and v["demote"]
        assert not v["promote"] and not v["gate_broken"]
        a = apply(res, v, now7, send=False, paths=P7)
        st = CH.state(P7["champion"])["entry_band"]
        assert a["demoted"] == planted and st["champion"] == champ0 and st["previous"] == planted
        assert st["demotion_judged"] == now7 and st["evidence"]["demoted"] == planted
        status = {e["name"]: e["status"] for e in json.load(open(P7["registry"]))["candidates"]}
        assert status[champ0] == "champion" and status[planted] == "candidate"
        assert "DEMOTED" in a["events"] and TR.load(P7["trials"])["nominations_ever"] == [f"demotion:{planted}@300"]
        md = open(write_proposal(res, v, P7)).read()
        assert "**DEMOTED**" in md
        # judged once: the next run does not re-judge
        res_b = evaluate_all(now7 + 86400, paths=P7, shuffle_reps=SH)
        assert res_b["champion"] == champ0 and res_b["demotion"] is None
        # a champion that KEEPS its edge is judged once and kept (demotion_judged set, nothing else)
        d7b = os.path.join(base, "viib"); os.makedirs(d7b)
        P7b = _fixture(d7b, n_days=100)
        CH.write_state(entry_band={"champion": planted, "previous": champ0, "promoted_ts": now,
                                   "promoted_at_event_seq": 300}, path=P7b["champion"])
        res = evaluate_all(now7, paths=P7b, shuffle_reps=SH)
        v = decide(res)
        assert res["demotion"]["ready"] and res["demotion"]["lift_lb"] > 0 and not v["demote"]
        a = apply(res, v, now7, send=False, paths=P7b)
        st = CH.state(P7b["champion"])["entry_band"]
        assert a["demotion_judged"] and st["champion"] == planted and st["demotion_judged"] == now7
        assert evaluate_all(now7 + 1, paths=P7b, shuffle_reps=SH)["demotion"] is None
        print(f"  (vii) DEMOTED {planted} → {champ0} on a failed forward prefix; a kept champion is judged once only")

        # (viii) K5 on a long fixture with no edge
        d8 = os.path.join(base, "viii"); os.makedirs(d8)
        P8 = _fixture(d8, n_days=190, per_day=6, planted_edge=False)
        now8 = 1_780_012_800.0 + 191 * 86400
        res = evaluate_all(now8, paths=P8, shuffle_reps=SH)
        v = decide(res)
        print(f"  (viii) span {res['span_days']:.0f} days, {res['n_days']} days: K5 = {res['kill_verdict']!r}; "
              f"promote={v['promote']} nominate={v['nominate']} broken={v['gate_broken']}")
        assert res["kill_verdict"] and res["kill_verdict"].startswith("entry-side edge not found at n=")
        assert not v["promote"] and v["nominate"] is None and not v["gate_broken"]
        assert "**K5**" in open(write_proposal(res, v, P8)).read()
        # and the 60-day fixture (planted edge present, span < 180) carries none
        assert evaluate_all(now, paths=P, shuffle_reps=SH)["kill_verdict"] is None

        # (ix) a failed nominee cannot be renominated inside the cooldown
        d9 = os.path.join(base, "ix"); os.makedirs(d9)
        P9 = _fixture(d9)
        CH.write_state(entry_band={"failed_nominee": planted, "failed_ts": now - 10 * 86400}, path=P9["champion"])
        res = evaluate_all(now, paths=P9, shuffle_reps=SH)
        v = decide(res)
        print(f"  (ix) failed nominee on cooldown: winner={v['winner']} nominate={v['nominate']}")
        assert v["winner"] != planted and v["nominate"] != planted
        assert any("re-nomination cooldown" in r for r in v["reasons"])
        # ...and is eligible again after the cooldown
        CH.write_state(entry_band={"failed_ts": now - (config.BAND_RENOMINATE_COOLDOWN_DAYS + 1) * 86400},
                       path=P9["champion"])
        v_after = decide(evaluate_all(now, paths=P9, shuffle_reps=SH))
        assert v_after["winner"] == planted and v_after["nominate"] == planted
        # one-shot failure: a live nominee whose complete prefix fails ⇒ nomination_failed, cleared
        d9b = os.path.join(base, "ixb"); os.makedirs(d9b)
        P9b = _fixture(d9b, n_days=105, edge_until_seq=720)
        CH.write_state(entry_band={"nominee": planted, "nominated_at_event_seq": 720, "nominated_ts": now},
                       path=P9b["champion"])
        res = evaluate_all(now2, paths=P9b, shuffle_reps=SH)
        v = decide(res)
        assert res["forward_only"] and v["nomination_failed"] == planted and not v["promote"]
        a = apply(res, v, now2, send=False, paths=P9b)
        st = CH.state(P9b["champion"])["entry_band"]
        assert a["nomination_failed"] == planted and st["nominee"] is None and st["failed_nominee"] == planted
        assert st["failed_ts"] == now2 and st["champion"] == champ0
        print(f"  (ix) one-shot failure clears the nominee and starts the {config.BAND_RENOMINATE_COOLDOWN_DAYS}-day cooldown")

        # empty ledger ⇒ insufficient (n=0), nothing decided
        d0 = os.path.join(base, "empty"); os.makedirs(d0)
        P0 = dict(P, ledger=os.path.join(d0, "ledger.csv"), verdicts=os.path.join(d0, "v.csv"),
                  champion=os.path.join(d0, "champion.json"), proposals=os.path.join(d0, "proposals"))
        res0 = evaluate_all(now, paths=P0, shuffle_reps=SH)
        assert res0["n_events"] == 0 and decide(res0)["reasons"] == ["insufficient (n=0)"]
        assert "insufficient (n=0)" in open(write_proposal(res0, decide(res0), P0)).read()
        print("  empty ledger: insufficient (n=0)")
        print(f"  dsr bar at {config.BAND_PROMOTE_MIN_CLUSTERS} days / 10 trials: {dsr_bar(40, 10):.3f} per day")
        assert 0.3 < dsr_bar(40, 10) < 1.5
    finally:
        shutil.rmtree(base, ignore_errors=True)
    print(f"OK — improve_bands.py assertions hold ({time.time() - t_start:.1f}s). Nothing was sent.")


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args or "--selftest" in args:
        _selftest()                                  # always offline; temp dirs only
        if "--selftest" in args:
            sys.exit(0)
        print("\n=== live (dry) ===")
    sys.exit(main([a for a in args if a != "--selftest"]))
