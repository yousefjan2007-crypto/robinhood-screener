"""
Honest track-record ledger — the anti-over-claiming centerpiece of this project.

One row per ENTRY EVENT, keyed (token, alert_ts). An event is opened when a token first passes
the hard gates ("first_sighting"), when a registered entry band flips True on the watchlist
("band_fire"), or when the CHAMPION band flips True ("promotion" — the only kind that alerts,
tier A). Every row gets the same write-once forward returns at 1h/6h/24h/7d from batched
Dexscreener snapshots, so the A arm is exactly "what was alerted, at the price it was alerted"
and B is "passed hard gates, failed the champion band at that instant" — a gate-matched control
by construction. (solana_screener froze the tier at first sighting and 39 of 42 real alerts sat
in its control arm; this schema exists to make that impossible.)

`event_seq` is a strictly increasing integer minted here, never reused, never renumbered — the
forward-only key the self-improvement gates test against.

Rules that keep the numbers honest:
  • entry snapshots are immutable; horizon cells are write-once and time-gated;
  • a token absent from the index is written as -100% only on the DEAD_CONFIRM_TICKS-th
    consecutive absence (an EVM pair can drop out of the index for one poll);
  • a quote whose implied supply drifts > SUPPLY_DRIFT_MAX vs entry — or whose multiple vs entry
    exceeds LEDGER_MAX_PLAUSIBLE_MULT, which the supply test cannot see when the quote leg itself
    is mispriced (EDDICE, event_seq 498: ret_6h = 11,026,957 at a constant implied supply) —
    writes NOTHING that run, and the row goes terminally 'suspect' after SUSPECT_TICKS_MAX runs;
  • every horizon cell is stamped with lag_{h} = how late the run grid sampled it, write-once with
    the cell and never backfilled: the scan grid IS the sampling grid, so a cell filled hours late
    (Mac asleep, 2026-09-14..17) is a spot sample of a different instant and the scorecards exclude
    it above LEDGER_MAX_CELL_LAG_S; rows written before the stamp existed are 'lag_unknown';
  • exit signals fire only for alerted rows (tier != B), only from the row's OWN frozen plan,
    and each kind/rung exactly once;
  • promoted B rows stay in the B arm in every statistic (intention-to-treat);
  • nothing is stamped per row per run: max/min ratchet only on a new extreme and
    last_snapshot_ts only on a fill or an exit event (the repo commits this file every run).

Reproducibility: now_s / alert_ts are passed IN by the caller and used only for horizon
bookkeeping — never in any scoring path.
"""
from __future__ import annotations

import csv
import json
import os
import time

import pandas as pd

import config

HORIZ = config.LEDGER_HORIZONS  # {"1h": 3600, "6h": 21600, "24h": 86400, "7d": 604800}

BASE_COLS = ["token", "symbol", "tier", "band", "fired_band", "event_seq", "event_kind",
             "prior_event_seq", "alert_ts", "entry_price", "entry_mcap", "entry_liq",
             "entry_score", "gates_mask", "plan_name", "sources_dark", "deployer"]
FWD_COLS: list = []
for _h in HORIZ:
    FWD_COLS += [f"price_{_h}", f"mcap_{_h}", f"ret_{_h}"]
TAIL_COLS = ["max_ret_seen", "min_ret_seen", "rugged_after", "status", "tp_alerted",
             "stop_alerted", "trail_alerted", "time_exited", "suspect_ticks", "absent_ticks",
             "promoted_ts", "last_snapshot_ts"]
# lag_{h} is APPENDED LAST (never interleaved with the ret_/price_/mcap_ block): the sibling voice
# assistant reads this CSV by header, and load() reindexes an older file so the four read as empty.
TAIL_COLS += [f"lag_{_h}" for _h in HORIZ]
COLUMNS = BASE_COLS + FWD_COLS + TAIL_COLS
_EMPTY = ("", "nan", "None", "NaN")
_LAST_H = list(HORIZ)[-1]


def empty_mask(series: pd.Series) -> pd.Series:
    """True where a cell is missing or an empty-string marker. Vectorised .astype(str) is NOT
    enough on its own: pandas >= 3 keeps a missing cell as NA instead of rendering it "nan",
    so an isin(_EMPTY) test alone silently misses every NaN (found by verify on the runner)."""
    return series.isna() | series.astype(str).str.strip().isin(_EMPTY)


def _num(v, default=0.0) -> float:
    try:
        if str(v) in _EMPTY:
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def _is_true(v) -> bool:
    return str(v).lower() == "true"


def load(path: str | None = None) -> pd.DataFrame:
    path = path or config.LEDGER_PATH
    if os.path.exists(path) and os.path.getsize(path) > 0:
        # astype(object): pandas >=3 makes dtype=str a STRICT string dtype that raises on the
        # float/bool cell writes update_forward() does. reindex: a CSV written before a schema
        # addition must load with the missing columns present (the pre-migration incident path).
        return pd.read_csv(path, dtype=str).astype(object).reindex(columns=COLUMNS)
    return pd.DataFrame(columns=COLUMNS)


def save(df: pd.DataFrame, path: str | None = None) -> None:
    path = path or config.LEDGER_PATH
    tmp = f"{path}.{os.getpid()}.tmp"
    df.reindex(columns=COLUMNS).to_csv(tmp, index=False)
    os.replace(tmp, path)


def ensure_exists(path: str | None = None) -> None:
    """Write the header if the file is missing (a fresh repo / a fresh cloud checkout)."""
    path = path or config.LEDGER_PATH
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        save(pd.DataFrame(columns=COLUMNS), path)


def next_event_seq(led: pd.DataFrame) -> int:
    """1 + max(event_seq); 1 on an empty or all-NaN column (pandas' max of an empty object
    column is NaN, which would mint 'nan' forever)."""
    if len(led) == 0:
        return 1
    s = pd.to_numeric(led["event_seq"], errors="coerce").dropna()
    return int(s.max()) + 1 if len(s) else 1


def origin_max_event_seq(rel: str = "data/ledger.csv") -> int | None:
    """max(event_seq) in `origin/main`'s ledger, or None when it cannot be read — for ANY
    reason (no git, no remote ref, no such blob, a malformed column).

    PRINT-ONLY, and deliberately narrow. DESIGN.md's rule is that every Mac reader of committed
    data reads the ff-merged worktree and only the 60 s livebook reads `git show origin/main:`;
    this does not soften that. The worktree stays the source of every row scored. This one read
    exists because the offline labs (backfill, evaluate) are run by hand against a tree the
    cloud keeper has usually moved past by a few events, and a result is easier to trust when
    the run says out loud how far behind its input was. It is never an input to a decision, so
    it never fails a run: unreadable is None and the caller stays quiet.
    """
    try:
        from selfimprove.publish import origin_blob
        txt = origin_blob(rel, config.ROOT)
        if not txt:
            return None
        rows = list(csv.DictReader(txt.splitlines()))
        seqs = []
        for r in rows:
            try:
                seqs.append(int(float(r.get("event_seq"))))
            except (TypeError, ValueError):
                continue
        return max(seqs) if seqs else None
    except Exception:
        return None


def warn_if_behind_origin(led: pd.DataFrame, where: str) -> int | None:
    """Print a WARNING when origin/main's ledger carries events this copy does not. Returns
    origin's max event_seq (None when unknown). The ONE staleness line both offline labs use,
    so they cannot drift apart in what they claim about their own input."""
    local = next_event_seq(led) - 1
    remote = origin_max_event_seq()
    if remote is not None and remote > local:
        print(f"  [{where}] WARNING: origin/main's ledger is at event_seq {remote}, this worktree "
              f"at {local} — {remote - local} newer event(s) are NOT read here. `git merge "
              f"--ff-only origin/main` first if you want them; nothing below sees them.")
    return remote


def index(led: pd.DataFrame) -> dict:
    """Per-token view for run.py's event decisions:
    {token: {"n": rows, "has_a": bool, "first_sighting_ts": float|None, "fired": set[band],
             "last_alert_ts": float}}"""
    out: dict = {}
    if len(led) == 0:
        return out
    for _, r in led.iterrows():
        t = str(r["token"]).lower()
        d = out.setdefault(t, {"n": 0, "has_a": False, "first_sighting_ts": None,
                               "fired": set(), "last_alert_ts": 0.0})
        d["n"] += 1
        if str(r["tier"]) == "A":
            d["has_a"] = True
        ts = _num(r["alert_ts"])
        if str(r["event_kind"]) == "first_sighting" or d["first_sighting_ts"] is None:
            d["first_sighting_ts"] = ts if d["first_sighting_ts"] is None else min(d["first_sighting_ts"], ts)
        fb = str(r["fired_band"])
        if fb not in _EMPTY:
            d["fired"].add(fb)
        d["last_alert_ts"] = max(d["last_alert_ts"], ts)
    return out


def plan_dict(plan_name: str) -> dict:
    """The exit plan a row was recorded with, as {ladder, stop, trail, trail_arm, flow,
    max_hold_s}. A missing, control or unknown name falls back to the default champion (never a
    crash in the exit path)."""
    try:
        from selfimprove import champion as _ch
        return _ch.exit_plan(plan_name)
    except Exception:
        return {"name": config.IMPROVE_DEFAULT_EXIT_CHAMPION,
                "ladder": [list(x) for x in config.TP_LADDER], "stop": config.HARD_STOP_PCT,
                "trail": None, "trail_arm": None, "flow": None, "max_hold_s": None}


def record_rows(events: list, alert_ts: float, plan_name: str | None = None,
                path: str | None = None) -> dict:
    """events: survivor dicts with keys token, symbol, tier, band, event_kind (first_sighting |
    band_fire | promotion), fired_band (None for first sighting), prior_event_seq (optional),
    market {price_usd, mcap, liq_usd}, score, gates (dict), sources_dark (list), deployer.
    Mints event_seq, writes the immutable entry snapshot, stamps promoted_ts on the token's
    earlier rows when the event is a promotion. Returns {token: event_seq} for the rows written.
    A token may get at most one row per alert_ts and at most BAND_MAX_EVENTS_PER_TOKEN rows."""
    led = load(path)
    plan_name = plan_name or config.IMPROVE_DEFAULT_EXIT_CHAMPION
    idx = index(led)
    seq = next_event_seq(led)
    written: dict = {}
    new = []
    for ev in events:
        token = str(ev["token"]).lower()
        if token in written:
            continue
        info = idx.get(token, {"n": 0, "has_a": False, "fired": set()})
        is_promo = ev.get("event_kind") == "promotion"
        if is_promo and info["has_a"]:
            continue                      # a token is alerted at most once
        if not is_promo and info["n"] >= config.BAND_MAX_EVENTS_PER_TOKEN:
            continue                      # the cap binds band fires, never the champion's fire
        m = ev.get("market") or {}
        gates = ev.get("gates") or {}
        try:
            from screen import gates_bitmask
            mask = gates_bitmask(gates)
        except Exception:
            mask = ""
        rec = {c: "" for c in COLUMNS}
        rec.update({
            "token": token, "symbol": ev.get("symbol", "?"), "tier": ev.get("tier", "B"),
            "band": ev.get("band", config.DEFAULT_ENTRY_BAND),
            "fired_band": ev.get("fired_band") or "",
            "event_seq": seq, "event_kind": ev.get("event_kind", "first_sighting"),
            "prior_event_seq": ev.get("prior_event_seq") if ev.get("prior_event_seq") is not None else "",
            "alert_ts": alert_ts,
            "entry_price": m.get("price_usd") if m.get("price_usd") is not None else ev.get("price", 0.0),
            "entry_mcap": m.get("mcap") if m.get("mcap") is not None else ev.get("mcap", 0.0),
            "entry_liq": m.get("liq_usd") if m.get("liq_usd") is not None else ev.get("liq", 0.0),
            "entry_score": ev.get("score", 0.0), "gates_mask": mask, "plan_name": plan_name,
            "sources_dark": ",".join(ev.get("sources_dark") or []),
            "deployer": ev.get("deployer") or "",
            "max_ret_seen": 0.0, "min_ret_seen": 0.0, "rugged_after": False, "status": "open",
            "tp_alerted": 0.0, "stop_alerted": False, "trail_alerted": False,
            "time_exited": False, "suspect_ticks": 0, "absent_ticks": 0,
            "promoted_ts": "", "last_snapshot_ts": "",
        })
        new.append(rec)
        written[token] = seq
        seq += 1
        if ev.get("event_kind") == "promotion" and len(led):
            prior = (led["token"].astype(str).str.lower() == token) & \
                    empty_mask(led["promoted_ts"])
            led.loc[prior, "promoted_ts"] = alert_ts
    if new:
        add = pd.DataFrame(new)
        led = add if len(led) == 0 else pd.concat([led, add], ignore_index=True)
        save(led, path)
    elif len(led):
        save(led, path)
    return written


def update_forward(now_s: float, snapshot_many_fn, path: str | None = None) -> tuple:
    """For every open row, one BATCHED snapshot per run: snapshot_many_fn(tokens) ->
    {"ok": {token: market}, "absent": set, "deferred": set}. Returns (cells filled, exit events).

    deferred → nothing is written for that row (last_snapshot_ts untouched; retried next run).
    absent   → absent_ticks += 1 (capped); the row is treated as dead (price 0, return -100%)
               only from the DEAD_CONFIRM_TICKS-th consecutive absence.
    ok       → quote-integrity gate, then ratchet / exit signals / horizon fills.

    Exit events are the alert's own discipline plan, read from the row's frozen plan_name:
    {kind: tp|stop|trail|time_exit, token, symbol, event_seq, price, ret, mult, levels?, due_ts?,
    gap_s}. Each kind (and each ladder rung) fires exactly once. B rows never emit."""
    led = load(path)
    if len(led) == 0:
        return 0, []
    # rows to poll: open, priced, newest first, capped
    open_mask = ~led["status"].astype(str).isin(["resolved", "suspect"])
    cand = led[open_mask].copy()
    cand["_ts"] = pd.to_numeric(cand["alert_ts"], errors="coerce").fillna(0.0)
    cand = cand.sort_values("_ts", ascending=False).head(config.LEDGER_MAX_POLL_ROWS)
    rows = [i for i in cand.index if _num(led.at[i, "entry_price"]) > config.LEDGER_MIN_ENTRY_PRICE]
    tokens = sorted({str(led.at[i, "token"]).lower() for i in rows})
    if not tokens:
        return 0, []
    res = snapshot_many_fn(tokens) or {}
    ok = {str(k).lower(): v for k, v in (res.get("ok") or {}).items()}
    absent = {str(t).lower() for t in (res.get("absent") or set())}

    filled = 0
    events: list = []
    _plans: dict = {}
    for i in rows:
        token = str(led.at[i, "token"]).lower()
        entry = _num(led.at[i, "entry_price"])
        alert_ts = _num(led.at[i, "alert_ts"])
        snap = ok.get(token)
        if snap is None and token not in absent:
            continue                                   # deferred: write nothing this run
        touched = False
        if snap is None:
            n_abs = min(int(_num(led.at[i, "absent_ticks"])) + 1, config.DEAD_CONFIRM_TICKS)
            if n_abs != int(_num(led.at[i, "absent_ticks"])):
                led.at[i, "absent_ticks"] = n_abs
            if n_abs < config.DEAD_CONFIRM_TICKS:
                continue                               # one index gap is not death
            cur_price, cur_mcap, cur_liq = 0.0, 0.0, 0.0
        else:
            if int(_num(led.at[i, "absent_ticks"])) != 0:
                led.at[i, "absent_ticks"] = 0
            cur_price = _num(snap.get("price_usd"))
            cur_mcap = _num(snap.get("mcap")) or _num(snap.get("fdv"))
            cur_liq = _num(snap.get("liq_usd"))
            # Quote-integrity gate (FOMO regression). Implied supply (mcap/price) is invariant
            # across honest quotes; a pair-switch or broken tick changes the price scale.
            entry_mcap = _num(led.at[i, "entry_mcap"])
            if cur_price > 0 and cur_mcap > 0 and entry_mcap > 0:
                ratio = (cur_mcap / cur_price) / (entry_mcap / entry)
                if not (1.0 / config.SUPPLY_DRIFT_MAX <= ratio <= config.SUPPLY_DRIFT_MAX):
                    n_sus = int(_num(led.at[i, "suspect_ticks"])) + 1
                    led.at[i, "suspect_ticks"] = n_sus
                    if n_sus >= config.SUSPECT_TICKS_MAX:
                        led.at[i, "status"] = "suspect"
                    continue                           # nothing else is written this run
            # Plausibility (EDDICE, event_seq 498: ret_6h = 11,026,957 at mcap 4.3e11 with a
            # CONSTANT implied supply — the quote leg itself was mispriced, so the gate above
            # cannot see it). Same counter, same terminal state: no second bookkeeping.
            if cur_price > 0 and entry > 0 and cur_price / entry > config.LEDGER_MAX_PLAUSIBLE_MULT:
                n_sus = int(_num(led.at[i, "suspect_ticks"])) + 1
                led.at[i, "suspect_ticks"] = n_sus
                if n_sus >= config.SUSPECT_TICKS_MAX:
                    led.at[i, "status"] = "suspect"
                continue                               # nothing else is written this run
            if int(_num(led.at[i, "suspect_ticks"])) != 0:
                led.at[i, "suspect_ticks"] = 0

        cur_ret = (cur_price / entry - 1.0) if cur_price > 0 else -1.0
        mx, mn = _num(led.at[i, "max_ret_seen"]), _num(led.at[i, "min_ret_seen"])
        if cur_ret > mx:
            led.at[i, "max_ret_seen"] = cur_ret; mx = cur_ret; touched = True
        if cur_ret < mn:
            led.at[i, "min_ret_seen"] = cur_ret; touched = True

        # exit signals — alerted rows only, from the row's OWN frozen plan
        sym = str(led.at[i, "symbol"])
        if str(led.at[i, "tier"]) != "B":
            pname = str(led.at[i, "plan_name"]) or config.IMPROVE_DEFAULT_EXIT_CHAMPION
            plan = _plans.get(pname) or _plans.setdefault(pname, plan_dict(pname))
            last_seen = _num(led.at[i, "last_snapshot_ts"]) or alert_ts
            base = {"token": token, "symbol": sym, "event_seq": int(_num(led.at[i, "event_seq"])),
                    "price": cur_price, "ret": cur_ret,
                    "mult": (cur_price / entry) if cur_price > 0 else 0.0,
                    "gap_s": now_s - last_seen}
            stop = plan.get("stop")
            if stop and not _is_true(led.at[i, "stop_alerted"]) and cur_ret <= -stop:
                led.at[i, "stop_alerted"] = True; touched = True
                events.append(dict(base, kind="stop"))
            trail = plan.get("trail")
            if trail and not _is_true(led.at[i, "trail_alerted"]):
                peak_px = entry * (1.0 + max(mx, 0.0))       # high-water mark, run cadence
                # An ARMED trail is not live until the high-water mark reaches entry x trail_arm.
                # Without that test a 30% trail sits at 0.70 x entry from the first snapshot,
                # ABOVE the -50% stop, so the stop could never fire — the opposite of the "stop
                # always" the plan promises. `flow` is ignored here by design: the cloud leg has
                # no 5-minute feed, so a flow policy runs its PRICE legs here and its flow leg is
                # scored only by the paper book.
                arm = plan.get("trail_arm")
                armed = arm is None or 1.0 + max(mx, 0.0) >= float(arm)
                if armed and cur_price <= peak_px * (1.0 - trail):
                    led.at[i, "trail_alerted"] = True; touched = True
                    events.append(dict(base, kind="trail"))
            ladder = plan.get("ladder") or []
            if ladder and cur_price > 0:
                cur_mult = cur_price / entry
                tp_done = _num(led.at[i, "tp_alerted"])
                crossed = [(float(m), float(f)) for m, f in ladder if cur_mult >= m and m > tp_done]
                if crossed:
                    led.at[i, "tp_alerted"] = max(m for m, _f in crossed); touched = True
                    events.append(dict(base, kind="tp", levels=crossed))
            mh = plan.get("max_hold_s")
            if mh is not None and not _is_true(led.at[i, "time_exited"]) and now_s - alert_ts >= mh:
                led.at[i, "time_exited"] = True; touched = True
                events.append(dict(base, kind="time_exit", due_ts=alert_ts + mh))

        for hname, hsec in HORIZ.items():
            col = f"ret_{hname}"
            if str(led.at[i, col]) not in _EMPTY:
                continue                               # write-once
            if now_s - alert_ts < hsec:
                continue                               # time-gated
            led.at[i, f"price_{hname}"] = cur_price if cur_price > 0 else 0.0
            led.at[i, f"mcap_{hname}"] = cur_mcap if cur_price > 0 else 0.0
            led.at[i, col] = cur_ret
            # how late the run grid actually sampled this horizon; write-once with the cell it
            # describes, never backfilled (the scorecards refuse a cell later than
            # LEDGER_MAX_CELL_LAG_S — FOMOPAD's 4.6x peak is recorded as 1.035x from a late tick)
            led.at[i, f"lag_{hname}"] = max(0.0, now_s - (alert_ts + hsec))
            filled += 1; touched = True

        if cur_price > 0 and cur_liq < config.RUG_LIQ_USD and not _is_true(led.at[i, "rugged_after"]):
            led.at[i, "rugged_after"] = True; touched = True
        if touched:
            led.at[i, "last_snapshot_ts"] = now_s
        if str(led.at[i, f"ret_{_LAST_H}"]) not in _EMPTY:
            led.at[i, "status"] = "resolved"

    save(led, path)
    return filled, events


def rotate(now_s: float, path: str | None = None) -> int:
    """Move resolved rows older than LEDGER_ROTATE_AFTER_DAYS to data/resolved_YYYY.csv (no
    'ledger' in the name) so the every-run commit stays small. Returns rows moved."""
    led = load(path)
    if len(led) == 0:
        return 0
    ts = pd.to_numeric(led["alert_ts"], errors="coerce").fillna(0.0)
    old = (led["status"].astype(str) == "resolved") & \
          (ts < now_s - config.LEDGER_ROTATE_AFTER_DAYS * 86400)
    if not old.any():
        return 0
    moved = led[old]
    for year, chunk in moved.groupby(ts[old].apply(lambda t: time.strftime("%Y", time.gmtime(t)))):
        out = os.path.join(os.path.dirname(path or config.LEDGER_PATH), f"resolved_{year}.csv")
        chunk.reindex(columns=COLUMNS).to_csv(out, mode="a", index=False,
                                              header=not os.path.exists(out))
    save(led[~old].reset_index(drop=True), path)
    return int(old.sum())


def _fmt_ret(v) -> str:
    if str(v) in _EMPTY:
        return "    ·"
    return f"{float(v) * 100:+5.0f}%"


TIER_LABEL = {"A": "alerted", "B": "silent control"}


def summary(path: str | None = None) -> None:
    led = load(path)
    n = len(led)
    print(f"ledger: {n} event row(s)")
    if n == 0:
        print("  (nothing logged yet — the cloud job writes rows every run once deployed)")
        return
    led = led.copy()
    led["_mx"] = pd.to_numeric(led["max_ret_seen"], errors="coerce").fillna(-9.0)
    led["_tier"] = led["tier"].astype(str).where(led["tier"].astype(str).isin(["A", "B"]), "B")
    led["_day"] = pd.to_numeric(led["alert_ts"], errors="coerce").fillna(0.0).apply(
        lambda t: time.strftime("%Y-%m-%d", time.gmtime(t)))
    top = led.sort_values("_mx", ascending=False).head(25)
    print(f"  {'':2s}{'symbol':12s} {'kind':14s} {'1h':>6s} {'6h':>6s} {'24h':>6s} {'7d':>6s}  {'best':>6s}  status")
    for _, r in top.iterrows():
        flag = "  RUGGED" if _is_true(r["rugged_after"]) else ""
        print(f"  {r['_tier']:2s}{str(r['symbol'])[:12]:12s} {str(r['event_kind'])[:14]:14s} "
              f"{_fmt_ret(r['ret_1h'])} {_fmt_ret(r['ret_6h'])} {_fmt_ret(r['ret_24h'])} "
              f"{_fmt_ret(r['ret_7d'])}  {_num(r['max_ret_seen'])*100:+5.0f}%  {r['status']}{flag}")
    print()
    for tier in ("A", "B"):
        sub = led[led["_tier"] == tier]
        if len(sub) == 0:
            continue
        days = sub["_day"].nunique()
        promoted = int((~empty_mask(sub["promoted_ts"])).sum())
        extra = f", promoted-B n={promoted}" if tier == "B" else ""
        print(f"  tier {tier} ({TIER_LABEL[tier]}), n={len(sub)} across {days} alert-day(s){extra}:")
        for bname, bsub in sub.groupby(sub["band"].astype(str)):
            if sub["band"].nunique() > 1:
                print(f"    band {bname}: n={len(bsub)}")
        for h in HORIZ:
            r = pd.to_numeric(sub[f"ret_{h}"], errors="coerce").dropna()
            if len(r):
                print(f"    {h:>3s}: n={len(r):4d}  hit-rate={(r > 0).mean()*100:4.0f}%  "
                      f"median={r.median()*100:+6.1f}%  dead={(r <= -0.99).mean()*100:3.0f}%  "
                      f"best={r.max()*100:+.0f}%  worst={r.min()*100:+.0f}%")
        ra = sub["rugged_after"].astype(str).str.lower().eq("true").mean()
        print(f"    rugged-after-passing-gates: {ra*100:.0f}%")
        if days < config.MIN_BOOTSTRAP_CLUSTERS:
            print(f"    n_{tier} = {len(sub)} across {days} alert-days — below "
                  f"MIN_BOOTSTRAP_CLUSTERS={config.MIN_BOOTSTRAP_CLUSTERS}, this is not a bound")
    print(f"  forward-cell sampling lag (write-once, stamped with the cell; a cell filled later than "
          f"LEDGER_MAX_CELL_LAG_S={config.LEDGER_MAX_CELL_LAG_S:.0f}s is a spot sample of a different "
          f"instant — the entry-band scorecard (scorecard.outcome_series) and the paper gate exclude "
          f"these cells; summary() and the dashboard print raw cells):")
    for h in HORIZ:
        lg = pd.to_numeric(led[f"lag_{h}"], errors="coerce").dropna()
        n_late = int((lg > config.LEDGER_MAX_CELL_LAG_S).sum())
        print(f"    {h:>3s}  late cells (lag > LEDGER_MAX_CELL_LAG_S): {n_late}/{len(lg)}"
              + ("   (rows written before the stamp existed carry no lag and are excluded as lag_unknown)"
                 if len(lg) == 0 else ""))
    imp = pd.Series(False, index=led.index)
    for h in HORIZ:
        imp |= pd.to_numeric(led[f"ret_{h}"], errors="coerce").fillna(-9.0) > config.LEDGER_MAX_PLAUSIBLE_MULT - 1.0
    imp |= pd.to_numeric(led["max_ret_seen"], errors="coerce").fillna(-9.0) > config.LEDGER_MAX_PLAUSIBLE_MULT - 1.0
    print(f"  implausible-mult suspects: {int(imp.sum())} row(s) carrying a recorded multiple above "
          f"LEDGER_MAX_PLAUSIBLE_MULT={config.LEDGER_MAX_PLAUSIBLE_MULT:.0f}x (the EDDICE 11e6x row) — new "
          f"ticks above it are refused into the suspect path; the entry-band scorecard and the paper "
          f"gate exclude these cells, summary() and the dashboard print raw cells")
    n_sus = led["status"].astype(str).eq("suspect").sum()
    if n_sus:
        print(f"  quote-integrity: {n_sus} row(s) terminally SUSPECT (implied supply moved "
              f">{config.SUPPLY_DRIFT_MAX}x vs entry) — excluded from every rate above, "
              f"NOT 'still maturing'.")
    print("  If tier A does not clearly beat tier B here, the A-tier band is NOT adding "
          "signal — loosen/rethink it rather than trusting the label.")
    print("  Reminder: negative expectancy is the base rate. A losing scorecard here is "
          "the screen doing its job — telling you not to scale.")


if __name__ == "__main__":
    import tempfile
    # offline demo in a temp dir: first sighting (B) → promotion (A) → batched forward update
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "ledger.csv")
        ensure_exists(p)
        t0 = 1_000_000.0
        w1 = record_rows([{"token": "0xAbC", "symbol": "DEMO", "tier": "B", "band": "band_a_strict",
                           "event_kind": "first_sighting", "market": {"price_usd": 1.0, "mcap": 1e6,
                           "liq_usd": 5e4}, "score": 55.0, "gates": {}, "sources_dark": []}],
                         alert_ts=t0, path=p)
        w2 = record_rows([{"token": "0xabc", "symbol": "DEMO", "tier": "A", "band": "band_a_strict",
                           "fired_band": "band_a_strict", "event_kind": "promotion",
                           "prior_event_seq": w1["0xabc"], "market": {"price_usd": 2.0, "mcap": 2e6,
                           "liq_usd": 6e4}, "score": 75.0, "gates": {}, "sources_dark": []}],
                         alert_ts=t0 + 7200, path=p)
        led = load(p)
        print(f"rows: {len(led)}  seqs: {w1} {w2}  promoted_ts on B row: {led.at[0, 'promoted_ts']}")
        assert len(led) == 2 and w2["0xabc"] == w1["0xabc"] + 1
        # 6h later: price 5.0 → the A row crosses the 2x rung; the B row emits nothing
        snap = lambda toks: {"ok": {t: {"price_usd": 5.0, "mcap": 5e6, "liq_usd": 5e4} for t in toks},
                             "absent": set(), "deferred": set()}
        filled, ev = update_forward(t0 + 7200 + 21600, snap, path=p)
        print(f"filled {filled} cell(s); events: {[(e['kind'], e.get('levels')) for e in ev]}")
        assert [e["kind"] for e in ev] == ["tp"] and ev[0]["levels"] == [(2.0, 0.5)]
        filled2, ev2 = update_forward(t0 + 7200 + 21660, snap, path=p)
        assert not ev2, "the rung must not re-fire"
        # dead token: first absence writes nothing, second writes -100%
        dead = lambda toks: {"ok": {}, "absent": set(toks), "deferred": set()}
        f3, ev3 = update_forward(t0 + 7200 + 86400, dead, path=p)
        f4, ev4 = update_forward(t0 + 7200 + 86400 + 300, dead, path=p)
        led = load(p)
        print(f"dead path: first absence filled {f3}, second filled {f4}; A ret_24h = {led.at[1, 'ret_24h']}; "
              f"events: {[e['kind'] for e in ev4]}")
        assert f3 == 0 and _num(led.at[1, "ret_24h"]) == -1.0
        summary(p)
    print("\n=== live ledger ===")
    summary()
