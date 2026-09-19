"""
The ONE weekly summary — <= 30 lines composed from committed / derived files only, sent at the
end of selfimprove/research/run_research.sh whether research ran, was skipped, or was paused.

WHY ONE MESSAGE AND WHY IT NEVER FAILS. The Sunday chain (improve -> improve_bands -> research)
is three launchd jobs whose only human-facing output is this message. A job that crashes before
sending is indistinguishable from a quiet week, and on solana the improve job's silence hid a
dirty-tree deadlock for weeks. So every section here is try/except -> 'unavailable', an empty
repo prints 'insufficient (n=0)' sections, and the message is sent regardless. The --send path
sends event alerts NOWHERE else: PROMOTED / DEMOTED / NOMINATED are the improve jobs' business.

WHAT IT READS (all committed or derived; nothing live, nothing from the network):
  data/run_log.jsonl            -> measured runs/day in the trailing 7 days, dark share
  data/ledger.csv               -> events, alert-days, matured rows, A vs B 6h medians with n,
                                   promoted-B, suspect
  data/band_verdicts.csv        -> per-band verdict coverage; the champion's selected rows
  data/livebook_summary.json    -> positions / done / suspect / unpriced / gapped, entry lag,
                                   per-policy top 3 by mean and the two controls. improve.summary_json
                                   NESTS the book counts under "book" and keeps the gate's own
                                   per_policy / controls tables at the top level — read each from
                                   where it lives (_book()), or the counts print 0 and '?';
                                   its "paper_gate" key is the paper gate's one line, relayed
  selfimprove/champion.json     -> champions, nominees, last promoted/demoted history lines
  selfimprove/trials.json       -> trial counts (the DSR denominators)
  data/entry_lab_history.jsonl, selfimprove/improve_history.jsonl -> last verdicts
  data/proposals/entry-*.md, proposal-*.md -> the newest 'Blocking:' lines, verbatim
  selfimprove/PAUSE / champion.json.locked -> the PAUSED flag

TWO DERIVED LINES THAT MUST NEVER BE CONSTANTS:
  * 'earliest possible promotion' is recomputed from the OBSERVED trailing-28-day per-day
    selected / unselected counts against the config floors (BAND_MIN_SELECTED,
    BAND_PROMOTE_MIN_CLUSTERS, BAND_FWD_*, IMPROVE_MIN_POSITIONS, IMPROVE_PROMOTE_MIN_CLUSTERS,
    IMPROVE_FWD_*). The README's '~80 days / 6-9 months' are priors; this line is the estimate.
    A day counts toward the cluster floor only if it would be RETAINED by the lift
    (n_sel >= 1 and n_unsel >= max(BAND_MIN_UNSELECTED_PER_DAY_ABS, n_sel)).
  * the K5 kill condition (BAND_KILL_AFTER_DAYS: no band net of round-trip cost above zero ->
    'no entry signal') is re-evaluated on the matured rows with the day-clustered lower bound
    from selfimprove/evaluate.cluster_lb, and the champion's DSR at len(bands_ever_scored) via
    the vendored selfimprove/dsr.py (numpy only; NaN fails closed on any failure).

Reproducibility: time.time() is read ONCE in main() and threaded through as now_s; every
bootstrap uses np.random.default_rng(config.SEED + offset). Not financial advice.
"""
from __future__ import annotations

import glob
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config                                  # noqa: E402
import ledger                                  # noqa: E402  (module level: ledger_section spells it too,
                                               # and a function-local import binds a LOCAL name — the
                                               # NameError compose swallowed as "ledger: unavailable")

MAX_LINES = 30
_EMPTY = ("", "nan", "None", "NaN", "NA")
_DSR_SEED_OFFSET = 101


# ── small helpers ────────────────────────────────────────────────────────────────
def _num(v, default=None):
    try:
        if v is None or str(v) in _EMPTY:
            return default
        f = float(v)
        return default if f != f else f
    except (TypeError, ValueError):
        return default


def _day(ts: float) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(float(ts)))


def _pct(v) -> str:
    return "?" if v is None else f"{v * 100:+.0f}%"


def _json_lines(path: str) -> list:
    out = []
    if not path or not os.path.exists(path):
        return out
    with open(path) as f:
        for ln in f:
            ln = ln.strip()
            if not ln:
                continue
            try:
                out.append(json.loads(ln))
            except Exception:
                continue
    return out


def _read_json(path: str):
    if not path or not os.path.exists(path):
        return None
    with open(path) as f:
        return json.load(f)


def default_paths() -> dict:
    """Every file the summary reads, so a test can point all of them at a temp dir."""
    return {"run_log": config.RUN_LOG_PATH, "ledger": config.LEDGER_PATH,
            "verdicts": config.BAND_VERDICTS_PATH, "livebook": config.LIVEBOOK_SUMMARY_PATH,
            "champion": config.CHAMPION_PATH, "trials": config.TRIALS_PATH,
            "entry_history": config.ENTRY_LAB_HISTORY_PATH,
            "improve_history": config.IMPROVE_HISTORY_PATH,
            "proposals": config.PROPOSALS_DIR, "pause": config.PAUSE_PATH}


# ── sections (each returns a list of lines; each is wrapped by compose) ───────────
def runs_section(now_s: float, P: dict) -> list:
    rows = [r for r in _json_lines(P["run_log"])
            if _num(r.get("scan_ts")) is not None and 0 <= now_s - float(r["scan_ts"]) < 7 * 86400]
    if not rows:
        return [f"week ending {_day(now_s)} · runs (measured, trailing 7d): insufficient (n=0)"]
    days = {_day(r["scan_ts"]) for r in rows}
    dark = sum(1 for r in rows if r.get("dark"))
    per_day = len(rows) / max(1, len(days))
    secs = sorted(_num(r.get("run_seconds"), 0.0) for r in rows)
    med = secs[len(secs) // 2]
    return [f"week ending {_day(now_s)} · runs/day (measured, trailing 7d): {per_day:.1f} over "
            f"{len(days)} day(s), median {med:.0f} s · source-dark share {dark / len(rows):.0%}"]


def _ledger_frame(P: dict):
    import pandas as pd
    led = ledger.load(P["ledger"])
    if len(led) == 0:
        return led
    led = led.copy()
    led["_ts"] = pd.to_numeric(led["alert_ts"], errors="coerce")
    led = led[led["_ts"].notna()]
    led["_day"] = led["_ts"].apply(_day)
    led["_tier"] = led["tier"].astype(str).where(led["tier"].astype(str).isin(["A", "B"]), "B")
    led["_ret"] = pd.to_numeric(led[config.BAND_OUTCOME_METRIC], errors="coerce")
    led["_price"] = pd.to_numeric(led["entry_price"], errors="coerce").fillna(0.0)
    led["_seq"] = pd.to_numeric(led["event_seq"], errors="coerce")
    ok = (led["status"].astype(str) != "suspect") & (led["_price"] > config.LEDGER_MIN_ENTRY_PRICE)
    led["_matured"] = led["_ret"].notna() & ok
    return led


def ledger_section(led) -> list:
    if led is None or len(led) == 0:
        return ["ledger: insufficient (n=0) — no event rows yet",
                "A vs B (6h): insufficient (n=0)"]
    n_days = led["_day"].nunique()
    mat = led[led["_matured"]]
    n_sus = int((led["status"].astype(str) == "suspect").sum())
    promo_b = int(((led["_tier"] == "B") & ~ledger.empty_mask(led["promoted_ts"])).sum())
    n_a = int((led["_tier"] == "A").sum())
    lines = [f"ledger: {len(led)} events ({n_a} A) across {n_days} alert-day(s); matured "
             f"{config.BAND_OUTCOME_METRIC} rows {len(mat)}; suspect {n_sus}; promoted-B {promo_b} "
             f"(kept in B: intention-to-treat)"]
    parts = []
    for tier, label in (("A", "alerted"), ("B", "silent control")):
        r = mat[mat["_tier"] == tier]["_ret"]
        if len(r):
            parts.append(f"{tier} ({label}) median {_pct(float(r.median()))} dead {(r <= -0.99).mean():.0%} n={len(r)}")
        else:
            parts.append(f"{tier} ({label}) n=0")
    lines.append(f"{config.BAND_OUTCOME_METRIC}: " + " · ".join(parts))
    if len(mat) and mat["_day"].nunique() < config.MIN_BOOTSTRAP_CLUSTERS:
        lines[-1] += f" — {mat['_day'].nunique()} matured day(s) < {config.MIN_BOOTSTRAP_CLUSTERS}: not a bound"
    return lines


def _verdict_wide(P: dict):
    from selfimprove.entry_lab import store
    return store.pivot_verdicts(store.load_verdicts(P["verdicts"]))


def verdict_section(wide) -> list:
    if wide is None or len(wide) == 0:
        return ["band verdicts: insufficient (n=0)"]
    bands = [c for c in wide.columns if c not in ("token", "alert_ts")]
    if not bands:
        return [f"band verdicts: {len(wide)} events, no band columns"]
    cov = {b: float(wide[b].notna().mean()) for b in bands}
    low = min(cov, key=cov.get)
    below = [b for b, c in cov.items() if c < config.BAND_MIN_COVERAGE and b not in ("ctl_inverse_band",)]
    shown = ", ".join(below[:3]) + (f" +{len(below) - 3} more" if len(below) > 3 else "")
    return [f"band verdicts: {len(bands)} bands on {len(wide)} events; lowest coverage {low} "
            f"{cov[low]:.0%}; below {config.BAND_MIN_COVERAGE:.0%} floor: {shown or 'none'}"]


def _book(d: dict) -> dict:
    """The BOOK counts inside a livebook digest. improve.summary_json nests livebook.live_stats_dict
    under "book" and keeps only the gate's own tables at the top level, so reading n_done/n_suspect/
    n_gapped/n_no_route/entry_lag_median_s/n_missed from the top level printed 0 or '?'. A flat
    digest (an older file, or live_stats_dict written directly) still reads correctly."""
    b = d.get("book") if isinstance(d, dict) else None
    return b if isinstance(b, dict) else (d if isinstance(d, dict) else {})


def livebook_section(P: dict) -> list:
    d = _read_json(P["livebook"])
    book = _book(d)
    n_pos = book.get("n_positions", (d or {}).get("n_positions") if isinstance(d, dict) else None)
    if not isinstance(d, dict) or not n_pos:
        return ["livebook: insufficient (n=0) — no weekly summary JSON yet"]
    lag = _num(book.get("entry_lag_median_s"))
    lines = [f"livebook: {n_pos} positions, {book.get('n_done', 0)} done, "
             f"{book.get('n_suspect', 0)} suspect, {book.get('n_unpriced', 0)} unpriced, "
             f"{book.get('n_gapped', 0)} gapped, {book.get('n_no_route', 0)} no-route; entry lag median "
             f"{('%.0f s' % lag) if lag is not None else '?'}; refused {book.get('n_missed', 0)}"]
    per = {k: v for k, v in (d.get("per_policy") or {}).items()
           if isinstance(v, dict) and _num(v.get("n"), 0) > 0}
    if per:
        top = sorted(per.items(), key=lambda kv: -_num(kv[1].get("mean"), -9.0))[:3]
        lines.append("  top by mean (a lead is not evidence): " + " · ".join(
            f"{k} {_num(v.get('mean'), 0.0):+.3f} (n={int(_num(v.get('n'), 0))})" for k, v in top))
    ctl = {k: v for k, v in (d.get("controls") or {}).items()
           if isinstance(v, dict) and _num(v.get("n"), 0) > 0}
    if ctl:
        lines.append("  controls: " + " · ".join(
            f"{k} {_num(v.get('mean'), 0.0):+.3f} (n={int(_num(v.get('n'), 0))})" for k, v in ctl.items()))
    return lines


def champion_section(P: dict) -> list:
    from selfimprove import champion
    st = champion.state(P["champion"])
    ex, en = st["exit"], st["entry_band"]

    def _tag(arm: dict, default: str) -> str:
        name = arm.get("champion") or default
        if not arm.get("promoted_ts"):
            return f"{name} (default)"
        return f"{name} (promoted {_day(arm['promoted_ts'])})"

    def _nom(arm: dict) -> str:
        if arm.get("nominee"):
            return f"nominee {arm['nominee']} since {_day(arm['nominated_ts']) if arm.get('nominated_ts') else '?'}"
        if arm.get("failed_nominee"):
            return f"last failed nominee {arm['failed_nominee']}"
        return "no nominee"

    lines = [f"champions: exit={_tag(ex, config.IMPROVE_DEFAULT_EXIT_CHAMPION)}, {_nom(ex)} · "
             f"entry={_tag(en, config.DEFAULT_ENTRY_BAND)}, {_nom(en)}"]
    moves = [h for h in st.get("history", []) if isinstance(h, dict)
             and str(h.get("action", "")).lower() in ("promote", "promoted", "demote", "demoted",
                                                      "manual_set", "revert")]
    for h in moves[-2:]:
        lines.append(f"  {str(h.get('action')).upper()} {h.get('arm')}: {h.get('from')} -> {h.get('to')} "
                     f"({_day(h['ts']) if _num(h.get('ts')) else '?'})")
    return lines


def trials_section(P: dict) -> list:
    from selfimprove import trials
    t = trials.load(P["trials"])
    return [f"trials (DSR denominators, only grow): policies {len(t['policies_ever_scored'])} · "
            f"bands {len(t['bands_ever_scored'])} · nominations {len(t['nominations_ever'])} · "
            f"cumulative {t['cumulative_trials']}"]


def _blocking_lines(proposal_glob: str, limit: int = 2) -> list:
    files = sorted(glob.glob(proposal_glob), key=os.path.getmtime)
    if not files:
        return []
    out, hit = [], False
    with open(files[-1]) as f:
        for ln in f:
            s = ln.strip()
            if s.lower().startswith("blocking"):
                hit = True
                continue
            if hit and s.startswith("- "):
                out.append(s[2:].strip()[:110])
                if len(out) >= limit:
                    break
            elif hit and s.startswith("#"):
                break
    return out


def _last_verdict(hist: list) -> str:
    if not hist:
        return "no run yet"
    h = hist[-1]
    when = _day(h["ts"]) if _num(h.get("ts")) else "?"
    if h.get("gate_broken") or h.get("void"):
        return f"VOID on {when} (apparatus fault)"
    what = "PROMOTE" if h.get("promote") else ("DEMOTE" if h.get("demote") or h.get("demoted") else "NO CHANGE")
    win = h.get("winner") or h.get("best") or h.get("nominee") or h.get("band") or "none"
    extra = f", nominated {h['nominated']}" if h.get("nominated") else ""
    return f"{what} on {when}, best challenger {win}{extra}"


def gates_section(P: dict) -> list:
    lines = []
    for label, hist_path, pat in (("exit gate", P["improve_history"], "proposal-*.md"),
                                  ("entry gate", P["entry_history"], "entry-*.md")):
        try:
            lines.append(f"{label}: {_last_verdict(_json_lines(hist_path))}")
        except Exception as exc:
            lines.append(f"{label}: unavailable ({type(exc).__name__})")
        try:
            for b in _blocking_lines(os.path.join(P["proposals"], pat)):
                lines.append(f"  blocking: {b}")
        except Exception:
            pass
    return lines


def paper_gate_section(P: dict) -> list:
    """The paper gate's ONE line, relayed from data/livebook_summary.json (improve.paper_gate
    writes it there through summary_json; the verdict machinery lives in improve.py — this only
    repeats the line, never recomputes it). 'none registered' until the operator's window
    commit, or when the digest predates the gate."""
    d = _read_json(P["livebook"])
    pg = d.get("paper_gate") if isinstance(d, dict) else None
    if not isinstance(pg, dict) or not pg.get("line"):
        return ["paper gate: none registered"]
    return [f"paper gate: {str(pg['line']).strip()[:220]}"]


def _selected_mask(led, wide, champion_band: str):
    """Rows the champion band selected: verdict == 1 in the sidecar (joined on event_seq),
    falling back to tier A where the sidecar has no line for the event."""
    import numpy as np
    sel = (led["_tier"] == "A").to_numpy(copy=True)
    if wide is not None and len(wide) and champion_band in wide.columns:
        col = wide[champion_band]
        for i, (idx, seq) in enumerate(zip(led.index, led["_seq"])):
            if seq == seq and seq in col.index:
                v = col.loc[seq]
                if v == v:
                    sel[i] = bool(v == 1.0)
    return np.asarray(sel, dtype=bool)


def kill_section(led, wide, champion_band: str, now_s: float, P: dict) -> list:
    """K5: after BAND_KILL_AFTER_DAYS with no band net of cost above zero -> no entry signal."""
    import numpy as np
    from selfimprove import evaluate, policies, trials
    if led is None or len(led) == 0:
        return [f"K5 kill ({config.BAND_KILL_AFTER_DAYS} d): insufficient (n=0) — clock not started"]
    first_ts = float(led["_ts"].min())
    day_n = int((now_s - first_ts) // 86400)
    mat = led[led["_matured"]]
    cost = policies.round_trip_cost()
    above, best = [], None
    if len(mat) and wide is not None and len(wide):
        bands = [c for c in wide.columns if c not in ("token", "alert_ts", "ctl_inverse_band")]
        for b in bands:
            sel = _selected_mask(mat, wide, b)
            vals = mat["_ret"].to_numpy(dtype=float)[sel] - cost
            days = list(mat["_day"].to_numpy()[sel])
            if vals.size == 0:
                continue
            lb, ng = evaluate.cluster_lb(vals, days, seed=config.SEED + 7)
            if ng < config.MIN_BOOTSTRAP_CLUSTERS:
                continue                                   # not a bound — cannot clear K5
            if best is None or lb > best[1]:
                best = (b, lb, ng)
            if lb > 0:
                above.append(f"{b} {lb:+.3f}")
    verdict = ("bands net of cost above zero: " + ", ".join(above)) if above else \
        "no band net of cost above zero" + (f" (best {best[0]} LB {best[1]:+.3f} over {best[2]} days)"
                                            if best else " (no band has >= "
                                            f"{config.MIN_BOOTSTRAP_CLUSTERS} matured days: not a bound)")
    line = f"K5 kill ({config.BAND_KILL_AFTER_DAYS} d): day {day_n}/{config.BAND_KILL_AFTER_DAYS}; {verdict}"
    if day_n >= config.BAND_KILL_AFTER_DAYS and not above:
        line += " -> K5 VERDICT: no entry signal; stop registering bands"
    # the champion's DSR on its selected day means at len(bands_ever_scored); NaN fails closed
    dsr = float("nan")
    try:
        from selfimprove import dsr as DSR                 # the vendored Deflated Sharpe
        sel = _selected_mask(mat, wide, champion_band) if len(mat) else np.zeros(0, dtype=bool)
        dm: dict = {}
        for v, d in zip(mat["_ret"].to_numpy(dtype=float)[sel] - cost, mat["_day"].to_numpy()[sel]):
            dm.setdefault(d, []).append(v)
        n_tr = max(1, trials.family_count("bands", P["trials"]))
        if len(dm) >= config.MIN_BOOTSTRAP_CLUSTERS:
            dsr = float(DSR.deflated_sharpe_ratio([float(np.mean(v)) for v in dm.values()], n_tr))
    except Exception:
        dsr = float("nan")
    line += f"; champion DSR {'n/a' if dsr != dsr else '%.2f' % dsr} (gate {config.BAND_DSR_GATE})"
    return [line]


def promotion_eta_section(led, wide, champion_band: str, now_s: float, P: dict) -> list:
    """'earliest possible promotion' from the OBSERVED trailing-28-day per-day counts."""
    if led is None or len(led) == 0:
        return ["earliest possible promotion: not computable — 0 events in the trailing 28 days"]
    window_start = now_s - 28 * 86400
    first_ts = float(led["_ts"].min())
    window_days = max(1, min(28, int((now_s - max(window_start, first_ts)) // 86400) + 1))
    recent = led[led["_ts"] >= window_start]
    sel = _selected_mask(recent, wide, champion_band) if len(recent) else []
    per_day: dict = {}
    for d, s in zip(recent["_day"].to_numpy(), sel):
        n = per_day.setdefault(d, [0, 0])
        n[0 if s else 1] += 1
    n_sel = sum(v[0] for v in per_day.values())
    n_events = int(len(recent))
    retained = sum(1 for v in per_day.values()
                   if v[0] >= 1 and v[1] >= max(config.BAND_MIN_UNSELECTED_PER_DAY_ABS, v[0]))
    sel_rate = n_sel / window_days
    ev_rate = n_events / window_days
    ret_frac = retained / window_days
    head = (f"earliest possible promotion (from trailing-28d observed rates: {sel_rate:.2f} selected/day, "
            f"{ev_rate:.1f} events/day, {ret_frac:.0%} of days retained):")
    if n_sel == 0 or retained == 0:
        return [head + " not computable — 0 selected rows or 0 retained days in the window"]
    # have so far (whole ledger)
    mat = led[led["_matured"]]
    sel_all = _selected_mask(mat, wide, champion_band) if len(mat) else []
    have_sel = int(sum(sel_all))
    have_days = len(set(mat["_day"].to_numpy()[sel_all])) if have_sel else 0
    in_sample = max((config.BAND_MIN_SELECTED - have_sel) / sel_rate,
                    (config.BAND_PROMOTE_MIN_CLUSTERS - have_days) / ret_frac, 0.0)
    forward = max(config.BAND_FWD_MIN_SELECTED / sel_rate, config.BAND_FWD_MIN_DAYS / ret_frac)
    entry_days = in_sample + forward
    # exit gate: the livebook admits every event row (A always, B up to the cap)
    lb = _read_json(P["livebook"]) if os.path.exists(P["livebook"]) else None
    have_pos = int(_num(_book(lb).get("n_done"), 0) or 0) if isinstance(lb, dict) else 0
    day_frac = len(per_day) / window_days
    ex_in = max((config.IMPROVE_MIN_POSITIONS - have_pos) / ev_rate,
                (config.IMPROVE_PROMOTE_MIN_CLUSTERS - min(have_days, config.IMPROVE_PROMOTE_MIN_CLUSTERS)) / day_frac, 0.0)
    ex_fwd = max(config.IMPROVE_FWD_MIN_POSITIONS / ev_rate, config.IMPROVE_FWD_MIN_DAYS / day_frac)
    exit_days = ex_in + ex_fwd
    return [head,
            f"  entry band: >= {entry_days:.0f} days ({_day(now_s + entry_days * 86400)}) — "
            f"{have_sel}/{config.BAND_MIN_SELECTED} selected rows, {have_days}/{config.BAND_PROMOTE_MIN_CLUSTERS} days, "
            f"then {config.BAND_FWD_MIN_SELECTED} rows / {config.BAND_FWD_MIN_DAYS} forward days · "
            f"exit policy: >= {exit_days:.0f} days ({_day(now_s + exit_days * 86400)}) — "
            f"{have_pos}/{config.IMPROVE_MIN_POSITIONS} positions, then {config.IMPROVE_FWD_MIN_POSITIONS} positions / "
            f"{config.IMPROVE_FWD_MIN_DAYS} forward days"]


def paused_section(P: dict) -> list:
    from selfimprove import champion
    locked = bool(champion.state(P["champion"]).get("locked"))
    pause = os.path.exists(P["pause"])
    if pause or locked:
        why = " + ".join(w for w, on in (("PAUSE file", pause), ("champion.json locked", locked)) if on)
        return [f"PAUSED ({why}): both --apply gates evaluate and report only; nothing is written"]
    return []


# ── composition ──────────────────────────────────────────────────────────────────
def compose(now_s: float, research_line: str | None = None, paths: dict | None = None) -> list:
    """<= MAX_LINES lines. Every section is isolated: a failure prints 'unavailable' for that
    section and the rest still renders."""
    P = dict(default_paths(), **(paths or {}))
    lines: list = []

    def _add(label, fn, *args):
        try:
            got = fn(*args)
            lines.extend(got if isinstance(got, list) else [str(got)])
        except Exception as exc:
            lines.append(f"{label}: unavailable ({type(exc).__name__}: {str(exc)[:60]})")

    led = wide = None
    champion_band = config.DEFAULT_ENTRY_BAND
    try:
        from selfimprove import champion
        champion_band = champion.entry_band(P["champion"])
    except Exception:
        pass
    _add("runs", runs_section, now_s, P)
    try:
        led = _ledger_frame(P)
    except Exception as exc:
        lines.append(f"ledger: unavailable ({type(exc).__name__}: {str(exc)[:60]})")
    else:
        _add("ledger", ledger_section, led)
    try:
        wide = _verdict_wide(P)
    except Exception as exc:
        lines.append(f"band verdicts: unavailable ({type(exc).__name__})")
    else:
        _add("band verdicts", verdict_section, wide)
    _add("livebook", livebook_section, P)
    _add("champions", champion_section, P)
    _add("trials", trials_section, P)
    _add("gates", gates_section, P)
    _add("paper gate", paper_gate_section, P)
    _add("K5 kill", kill_section, led, wide, champion_band, now_s, P)
    _add("earliest possible promotion", promotion_eta_section, led, wide, champion_band, now_s, P)
    _add("paused", paused_section, P)
    if research_line:
        lines.append(f"research: {str(research_line).strip()[:200]}")
    if len(lines) > MAX_LINES:
        lines = lines[:MAX_LINES - 1] + [f"… ({len(lines) - MAX_LINES + 1} more line(s) cut at {MAX_LINES})"]
    return [str(x) for x in lines]


def _arg(argv: list, flag: str, default=None):
    if flag in argv:
        i = argv.index(flag)
        if i + 1 < len(argv):
            return argv[i + 1]
    return default


def main(argv: list) -> int:
    now_s = time.time()                                    # the ONE wall-clock read
    import alerts
    send = "--send" in argv
    lines = compose(now_s, _arg(argv, "--research"))
    title, body = alerts.format_weekly(lines)
    alerts.send_all(title, body, dry_run=not send)
    return 0


# ── offline self-test ────────────────────────────────────────────────────────────
def _fixture(d: str, now_s: float) -> dict:
    """A ~40-day synthetic repo state in a temp dir: ledger + sidecar + run log + livebook
    summary + champion/trials/history/proposals + PAUSE. Seeded; no wall-clock."""
    import numpy as np
    import pandas as pd
    import ledger
    from selfimprove import champion, trials
    from selfimprove.entry_lab import store
    rng = np.random.default_rng(config.SEED + 3)
    P = {"run_log": os.path.join(d, "run_log.jsonl"), "ledger": os.path.join(d, "ledger.csv"),
         "verdicts": os.path.join(d, "band_verdicts.csv"),
         "livebook": os.path.join(d, "livebook_summary.json"),
         "champion": os.path.join(d, "champion.json"), "trials": os.path.join(d, "trials.json"),
         "entry_history": os.path.join(d, "entry_lab_history.jsonl"),
         "improve_history": os.path.join(d, "improve_history.jsonl"),
         "proposals": os.path.join(d, "proposals"), "pause": os.path.join(d, "PAUSE")}
    os.makedirs(P["proposals"])
    rows, verd, seq = [], [], 1
    bands = ["band_a_strict", "band_no_age", "ctl_random_band", "ctl_inverse_band"]
    for day in range(40):
        for k in range(int(rng.integers(3, 7))):
            ts = now_s - (40 - day) * 86400 + k * 1800
            champ = bool(rng.uniform() < 0.3)
            tier = "A" if champ else "B"
            ret = float(rng.normal(0.15 if champ else -0.4, 0.5))
            matured = day < 39                             # today's rows have not matured
            rec = {c: "" for c in ledger.COLUMNS}
            rec.update({"token": f"0x{seq:040x}", "symbol": f"T{seq}", "tier": tier,
                        "band": "band_a_strict", "event_seq": seq,
                        "event_kind": "promotion" if champ else "first_sighting",
                        "alert_ts": ts, "entry_price": 0.001, "entry_mcap": 1e5, "entry_liq": 3e4,
                        "status": "open", "promoted_ts": "" if not (tier == "B" and k == 0) else ts + 60,
                        "ret_6h": max(-1.0, ret) if matured else ""})
            rows.append(rec)
            verd.append({"event_seq": seq, "token": rec["token"], "alert_ts": ts,
                         "verdicts": {"band_a_strict": champ, "band_no_age": bool(rng.uniform() < 0.5),
                                      "ctl_random_band": None if k == 0 else False,
                                      "ctl_inverse_band": not champ}})
            seq += 1
    pd.DataFrame(rows).reindex(columns=ledger.COLUMNS).to_csv(P["ledger"], index=False)
    store.append_verdicts(verd, P["verdicts"])
    with open(P["run_log"], "w") as f:
        for i in range(60):
            f.write(json.dumps({"scan_ts": now_s - i * 3000, "run_seconds": 90 + i % 7,
                                "dark": ["blockscout"] if i % 5 == 0 else []}) + "\n")
    with open(P["livebook"], "w") as f:
        # the shape improve.summary_json actually writes: the gate's own tables at the top level,
        # livebook.live_stats_dict nested under "book"
        json.dump({"ts": now_s, "champion": "sell_3h", "n_positions": 120, "n_days": 40,
                   "n_trials": 3,
                   "book": {"ts": now_s, "n_positions": 120, "n_done": 80, "n_open": 40, "n_suspect": 3,
                            "n_unpriced": 2, "n_gapped": 5, "n_no_route": 4,
                            "entry_lag_median_s": 310.0, "n_missed": 1,
                            "by_tier": {"A": 40, "B": 80}},
                   "per_policy": {"hold_to_end": {"n": 70, "mean": -0.61}, "sell_3h": {"n": 70, "mean": -0.05},
                                  "sell_1h": {"n": 70, "mean": -0.12}, "trail_30": {"n": 70, "mean": -0.2}},
                   "controls": {"ctl_exit_immediately": {"n": 70, "mean": -0.034},
                                "ctl_random_exit": {"n": 70, "mean": -0.45}},
                   "paper_gate": {"status": "open",
                                  "line": "band_no_age@sell_3h window 2026-09-21T00:00:00Z open (12 fills, "
                                          "3.0 of 7 days; n_days 3, a number, not a bound (floor 12))"}}, f)
    champion.write_state(exit={"champion": "sell_3h", "promoted_ts": now_s - 5 * 86400,
                               "previous": "cfg_ladder_stop", "nominee": None},
                         entry_band={"nominee": "band_no_age", "nominated_ts": now_s - 2 * 86400},
                         history_line={"ts": now_s - 5 * 86400, "arm": "exit", "action": "promote",
                                       "from": "cfg_ladder_stop", "to": "sell_3h"}, path=P["champion"])
    trials.bump("bands", bands[:2], P["trials"]); trials.bump("policies", ["sell_3h"], P["trials"])
    with open(P["entry_history"], "w") as f:
        f.write(json.dumps({"ts": now_s - 86400, "champion": "band_a_strict", "winner": "band_no_age",
                            "promote": False, "nominated": "band_no_age"}) + "\n")
    with open(P["improve_history"], "w") as f:
        f.write(json.dumps({"ts": now_s - 86400, "champion": "sell_3h", "winner": "sell_1h",
                            "promote": False, "gate_broken": False}) + "\n")
    with open(os.path.join(P["proposals"], "entry-20260910-000000.md"), "w") as f:
        f.write("# entry proposal\n\n## NO CHANGE\n\nBlocking:\n\n- only 39 retained days (needs 40)\n"
                "- forward-only prefix not yet matured\n- a third line that must not print\n")
    with open(os.path.join(P["proposals"], "proposal-20260910-000000.md"), "w") as f:
        f.write("## NO CHANGE\nBlocking:\n- only 80 completed positions (needs 300)\n")
    open(P["pause"], "w").close()
    return P


if __name__ == "__main__":
    import tempfile
    if len(sys.argv) > 1 and sys.argv[1] in ("--dry", "--send"):
        sys.exit(main(sys.argv[1:]))
    import alerts
    now = 1_789_300_000.0                                   # fixed: the self-test has no clock
    with tempfile.TemporaryDirectory() as d:
        P = _fixture(d, now)
        lines = compose(now, "research merged (verify green, 1 candidate registered)", P)
        body = "\n".join(lines)
        print(body)
        assert len(lines) <= MAX_LINES, len(lines)
        assert "runs/day (measured, trailing 7d): 20.0 over 3 day(s)" in body, lines[0]
        assert "ledger: " in body and "A (alerted) median" in body and "B (silent control) median" in body
        assert "promoted-B 28" in body, body
        assert "band verdicts: 4 bands" in body and "ctl_random_band" in body
        assert "livebook: 120 positions, 80 done, 3 suspect, 2 unpriced, 5 gapped" in body
        assert "top by mean" in body and "sell_3h -0.050" in body and "ctl_exit_immediately -0.034" in body
        assert "exit=sell_3h (promoted" in body and "nominee band_no_age" in body and "PROMOTE exit:" in body
        assert "trials (DSR denominators, only grow): policies 1 · bands 2" in body
        assert "blocking: only 39 retained days (needs 40)" in body and "third line" not in body
        assert "blocking: only 80 completed positions" in body
        assert "K5 kill (180 d): day 40/180" in body and "champion DSR" in body
        assert "paper gate: band_no_age@sell_3h window 2026-09-21T00:00:00Z open (12 fills" in body
        assert "earliest possible promotion (from trailing-28d observed rates:" in body
        assert "entry band: >=" in body and "exit policy: >=" in body
        assert "PAUSED (PAUSE file)" in body
        assert "research: research merged" in body
        eta = [l for l in lines if l.startswith("  entry band: >=")][0]
        # the ETA is derived from observed rates: halving the selection rate must move it
        P2 = dict(P)
        import pandas as pd
        led = pd.read_csv(P["ledger"], dtype=str)
        led.loc[led.index[::2], "tier"] = "B"
        import csv as _csv
        v = pd.read_csv(P["verdicts"], dtype=str)
        v.loc[(v["band"] == "band_a_strict") & (v["event_seq"].astype(int) % 2 == 0), "verdict"] = "0"
        P2["ledger"] = os.path.join(d, "ledger2.csv"); P2["verdicts"] = os.path.join(d, "bv2.csv")
        led.to_csv(P2["ledger"], index=False); v.to_csv(P2["verdicts"], index=False, quoting=_csv.QUOTE_MINIMAL)
        eta2 = [l for l in compose(now, None, P2) if l.startswith("  entry band: >=")][0]
        assert eta != eta2, "the promotion ETA must be recomputed from observed counts"
        # dry send path renders through alerts.format_weekly (never sends)
        t, b = alerts.format_weekly(lines)
        assert config.FOOTER in b and t.endswith("WEEKLY summary")
        alerts.send_all(t, b, dry_run=True)
        # the empty start: every section says insufficient / default, nothing raises
        e = os.path.join(d, "empty"); os.makedirs(e)
        PE = {k: os.path.join(e, os.path.basename(p)) for k, p in P.items()}
        el = compose(now, None, PE)
        eb = "\n".join(el)
        print("\n--- empty start ---\n" + eb)
        assert "insufficient (n=0)" in el[0] and "ledger: insufficient (n=0)" in eb
        assert "band verdicts: insufficient (n=0)" in eb and "livebook: insufficient (n=0)" in eb
        assert "not computable" in eb and "clock not started" in eb and "PAUSED" not in eb
        assert "paper gate: none registered" in eb
        assert f"exit={config.IMPROVE_DEFAULT_EXIT_CHAMPION} (default)" in eb
        assert len(el) <= MAX_LINES
    print("\nweekly_summary self-test OK (nothing sent)")
