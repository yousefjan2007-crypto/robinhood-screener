"""
Live multi-policy paper book — every exit policy trading the same alerts, simultaneously,
on Robinhood Chain. Runs INSIDE the scan keeper on GitHub Actions (.github/keeper.sh,
KEEPER_BOOK=1: one `--tick` per LIVEBOOK_TICK_INTERVAL_S under the scan's lock). Four state
files — data/livebook.json, livebook_fills.csv, livebook_feed.json, livebook_missed.jsonl —
are COMMITTED with the scan (`git add data/`), so a successor keeper restores the book from
its checkout; data/livebook_ticks.jsonl (~3.5 MB/day) stays gitignored and rides the run's
artifact. The Mac ran this under launchd until the cloud-book cutover and must never tick
again (tracked state). Ported from solana_screener/selfimprove/livebook.py (2026-09-12).

WHY THIS EXISTS, AND WHY IT IS THE RIGHT NEXT STEP RATHER THAN MORE BACKTESTING.
The bar backtest (selfimprove/evaluate.py over selfimprove/paths.py bars) has three weaknesses
a live book removes outright:

 1. WITHIN-BAR AMBIGUITY. An OHLC bar gives four numbers and no path, so a path-dependent exit
    (a trailing stop) has to be bracketed by assuming an ordering. That bracketing is where the
    worst bug of 2026-08-12 lived: the simulator ratcheted the high-water mark to the bar's HIGH
    before testing the same bar's LOW, which is look-ahead inside the bar. It moved the headline
    policy from mean +0.142 / LB +0.006 (appeared to clear the bar) to mean -0.164 / LB -0.184.
    A tick stream has ONE price per observation. There is no ordering to assume.
 2. COARSE BARS. 347 of 639 backfilled solana rows priced on 1-HOUR bars. An hour bar can contain
    an entire pump and dump. Live ticks are seconds old by construction.
 3. SEARCH CONTAMINATION. Every backtest number was found by looking at history. Live rows did
    not exist when the policies were written, which is the only real cure.

...and one it removes that nothing else can: EXECUTION COST IS MEASURED, NOT ASSUMED.
`policies.round_trip_cost()` charges a flat figure. Measured on live quotes 2026-08-12 (solana),
buying $10 and immediately selling: Papoi -2.8%, TIGER -3.6%, ETG **-27.6%**. A flat cost is
therefore optimistic, badly so on illiquid names, and only real quotes can tell you which is which.

=============================== HOW THE COMPARISON STAYS FAIR ===============================
ONE shared entry per alert. A single quotes.quote_buy is taken once, and all N policies and
both controls inherit that identical fill — same token, same moment, same cost basis. So any
difference between two policies is caused by the exit and nothing else. Then ONE batched
quotes.quote_sell_many (one Multicall3) per tick prices every due position, and ONE WETH/USD
read per tick values them, so the cost is O(open positions), not O(positions x policies).
=============================================================================================

TWO FILL CONVENTIONS, AND THEY ARE DELIBERATELY ASYMMETRIC:
  * A ladder rung is a pre-committed LIMIT: it fills at the rung level (entry_px x multiple),
    never at the observed tick. If price gaps from 1.5x to 5x between ticks, a real limit at 2x
    fills near 2x; marking it at 5x would be the same class of leak as reading an eventual peak
    as a fill price.
  * A stop / trail / time exit is a MARKET exit and fills at the observed tick price. Between
    ticks price may have been worse, so this leg is mildly OPTIMISTIC. `gap_s` is recorded on
    every tick and every fill so the size of that optimism stays auditable.
  * When a rung and a protective exit are both satisfied by the same tick, the move happened
    BETWEEN ticks and the protective exit wins: the remainder closes at the observed price.

PARTIAL SELLS ARE PRO-RATED FROM A FULL-SIZE QUOTE, WHICH IS CONSERVATIVE. Quoting each
policy's partial sell separately would be N quotes per position per tick. One full-size sell
quote is taken and partial proceeds are pro-rated from it. Verified convex on live quotes
2026-08-12 — a smaller sell always gets a BETTER per-token price — so pro-rating understates.

WHAT THIS PORT CHANGES, AND THE INCIDENTS BEHIND EACH CHANGE.
  * IDENTITY: positions are keyed "<token>:<event_seq>", not per token. solana keyed per mint
    and so held 438 of 439 positions as tier B — the ledger froze the tier at first sighting one
    layer up, and the book could never open the promotion as its own position. Here the feed
    opens a NEW position for a promotion or band_fire row even when the token is open as B.
  * QUOTE-INTEGRITY GATE (solana had none; 16 of its 51 ">=3x winners" were quote artifacts).
    Before any policy steps, an UPWARD tick must pass R1 (mult <= LIVEBOOK_MAX_PLAUSIBLE_MULT),
    R3 (WETH out <= the pool's WETH reserve when known) and R2 (a jump above
    LIVEBOOK_TICK_JUMP_MAX vs the last honest price is corroborated by ONE batched fresh
    Dexscreener read within LIVEBOOK_XSOURCE_TOL). A failed tick is logged 'suspect', steps
    NOTHING and fills NOTHING; LIVEBOOK_SUSPECT_TICKS_MAX consecutive suspects retire the
    position as 'suspect' (excluded from every statistic). DOWNWARD moves are never gated —
    a gate that could hide a collapse would flatter every exit rule.
  * THREE-WAY QUOTES: 'deferred' (rate limit, node down) is never a fill — LIVEBOOK_DEFERRED_
    TICKS_MAX consecutive deferrals retire the position as 'unpriced', NEVER as -100%. 'absent'
    (the on-chain-first death rule in quotes.py) closes every open policy at $0 — 'no_route'.
    A sibling project once fabricated 472 dead rows by reading a rate limit as "no route".
  * GAPS: every policy state records close_gap_s and `gapped`. A market exit (stop / trail /
    time_exit / random_exit) whose closing tick gap exceeds LIVEBOOK_MAX_SCORABLE_GAP_S, or that
    follows any gap above it inside the decision horizon, is NOT what the policy would have done
    (Mac asleep) — improve.py scores it NaN. 22.5% of solana's time-exit fills carried a gap
    over 5 min and its scorer never excluded them. A terminal mark (hold_to_end / the tracking
    window end) and a limit rung fill are not decisions, so they are never `gapped`.
  * DURABILITY: the book is saved atomically FIRST, then fills are flushed. On 2026-08-15 a
    KeyError mid-loop meant the state write never ran, so every policy re-fired on the next tick
    and appended again — `sell_30m` logged 199 fills from ONE position.
  * ENTRY LAG: the ledger of record is read in the feed's mode (config.LIVEBOOK_FEED_SOURCE).
    `worktree` — the keeper's: this checkout's data/ledger.csv, which run.py writes in ONE
    tmp + os.replace and the tick reads under the same lock, so the lag is the scan's wall
    time after alert_ts plus at most one tick. `origin` — the Mac's, retired: origin/main via
    git fetch + show, never pull (dispatch + run + commit + fetch: 3–8 min). Either way a row
    older than MAX_ENTRY_LAG_S is refused and logged with its lag, so the book judges "exit
    policy given a late entry" and says so (entry_lag_s on every position).
  * HANDOFFS: a successor keeper's first tick follows the predecessor's last by about one
    push/poll cycle; last_tick_ts, max_gap_s and `gapped` make that gap visible (a market-
    decision close after a gap > LIVEBOOK_MAX_SCORABLE_GAP_S scores NaN). That is a scoring
    parameter, never raised to hide a slow handoff — the cutover is gated on the measured gap.
  * THE BAND UNDER TEST: every new row is stamped `sidecar_true` (the bands whose verdict at
    that event_seq is 1, from data/band_verdicts.csv); when config.LIVEBOOK_BAND_UNDER_TEST
    names a registered non-control band, its picks are admitted at the cap under their own
    sub-cap, so a candidate's rows reach the book beside the champion's. None = inert.

Reproducibility: `now_s` is always passed in by the caller (time.time() ONCE in main()); the
only other clock is time.monotonic() around the tick for the tick_wall_s log line. No keys, no
funds, no transactions — quotes only. Nothing here raises on bad data. Not financial advice.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config                                   # noqa: E402
import quotes                                   # noqa: E402
from selfimprove import policies as POL         # noqa: E402
from selfimprove import publish                 # noqa: E402
from sources import dexscreener, rpc            # noqa: E402

# ── files (four committed with the scan; the ticks log gitignored, the run's artifact carries
#    it; the weekly summary in config.LIVEBOOK_SUMMARY_PATH is not ours) ──
BOOK_PATH = os.path.join(config.DATA_DIR, "livebook.json")
FILLS_PATH = os.path.join(config.DATA_DIR, "livebook_fills.csv")
TICKS_PATH = os.path.join(config.DATA_DIR, "livebook_ticks.jsonl")
FEED_STATE_PATH = os.path.join(config.DATA_DIR, "livebook_feed.json")
MISSED_PATH = os.path.join(config.DATA_DIR, "livebook_missed.jsonl")
LEDGER_REL = "data/ledger.csv"                  # the ledger of record (origin mode: origin/main:)
VERDICTS_REL = "data/band_verdicts.csv"         # the sidecar the band-under-test stamp reads

FILL_COLUMNS = ["ts", "token", "event_seq", "symbol", "tier", "policy", "side",
                "frac_of_original", "px", "usd_proceeds", "gap_s", "note"]

# A position is dropped once every policy has closed it, or after MAX_TRACK_S. The longest
# max_hold_s in the family is 6h, so past the decision horizon no policy has a decision left
# and the only reason to keep quoting is to give hold_to_end and the no-stop ladder an honest
# terminal mark. That does not need minute resolution, so the cadence drops.
TICK_INTERVAL_S = float(config.LIVEBOOK_TICK_INTERVAL_S)
TICK_INTERVAL_LATE_S = float(config.LIVEBOOK_TICK_INTERVAL_LATE_S)
DECISION_HORIZON_S = float(config.LIVEBOOK_DECISION_HORIZON_S)
MAX_TRACK_S = float(config.LIVEBOOK_MAX_TRACK_S)
MARKET_CLOSES = ("stop", "trail", "time_exit", "random_exit")   # the closes `gapped` applies to


# ── atomic state, because a crash mid-write must not corrupt the book ───────────────
def _clean(obj):
    """NaN/inf → None recursively so every write can use allow_nan=False (a NaN in the book
    is unparseable by a strict reader and silently poisons every mean downstream)."""
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {str(k): _clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_clean(v) for v in obj]
    if isinstance(obj, set):
        return sorted(_clean(v) for v in obj)
    return obj


def _load(path: str, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path) as fh:
            obj = json.load(fh)
        return obj if isinstance(obj, type(default)) else default
    except Exception:
        return default


def _save_atomic(obj, path: str) -> None:
    """tmp + os.replace. solana's paper_exec rewrote its positions JSON in place, so a crash
    between truncate and write lost the whole book; os.replace is atomic on POSIX."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = "%s.%d.tmp" % (path, os.getpid())
    with open(tmp, "w") as fh:
        json.dump(_clean(obj), fh, indent=1, allow_nan=False)
    os.replace(tmp, path)


def _append_fill(row: dict) -> None:
    new = not os.path.exists(FILLS_PATH) or os.path.getsize(FILLS_PATH) == 0
    with open(FILLS_PATH, "a", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FILL_COLUMNS, extrasaction="ignore")
        if new:
            w.writeheader()
        w.writerow({c: row.get(c, "") for c in FILL_COLUMNS})


def _append_jsonl(path: str, obj: dict) -> None:
    with open(path, "a") as fh:
        fh.write(json.dumps(_clean(obj), allow_nan=False) + "\n")


def _num(v, default=None):
    try:
        if v is None or str(v).strip() in ("", "nan", "None", "NaN"):
            return default
        f = float(v)
        return f if math.isfinite(f) else default
    except (TypeError, ValueError):
        return default


def _pos_key(token: str, event_seq) -> str:
    return "%s:%d" % (str(token).lower(), int(event_seq))


def _names() -> list:
    return list(POL.POLICIES) + list(getattr(POL, "CONTROLS", {}))


def _new_policy_state() -> dict:
    return {"remaining": 1.0, "peak_px": 0.0, "rungs_left": None, "realized_usd": 0.0,
            "closed": False, "closed_ts": None, "close_reason": "",
            "close_gap_s": None, "gapped": False}


def _finish_state(pos: dict, st: dict, reason: str, now_s: float, gap_s: float) -> None:
    """Close bookkeeping shared by every close path; sets close_gap_s and `gapped`."""
    st["closed"] = True
    st["closed_ts"] = now_s
    st["close_reason"] = reason
    st["close_gap_s"] = round(float(gap_s), 1)
    g = float(config.LIVEBOOK_MAX_SCORABLE_GAP_S)
    st["gapped"] = bool(reason in MARKET_CLOSES and
                        (gap_s > g or float(pos.get("max_gap_s") or 0.0) > g))


def _fill_row(pos: dict, policy: str, side: str, frac: float, px: float, usd: float,
              gap_s: float, now_s: float, note: str = "") -> dict:
    return {"ts": now_s, "token": pos["token"], "event_seq": pos["event_seq"],
            "symbol": pos["symbol"], "tier": pos["tier"], "policy": policy, "side": side,
            "frac_of_original": round(float(frac), 6), "px": px,
            "usd_proceeds": round(float(usd), 6), "gap_s": round(float(gap_s), 1), "note": note}


# ── open: the ONE shared entry fill ──────────────────────────────────────────────
def open_alert(row: dict, now_s: float, *, quote_buy_fn=quotes.quote_buy, weth_px=None,
               decimals_fn=None) -> tuple:
    """Take the ONE shared entry fill for a ledger row and seed a state machine per policy
    and control. Returns (status, pos): 'opened' | 'already' | 'deferred' (retry — nothing
    written) | 'absent' (no route at alert — not opened, a buy_failed fill row) | 'invalid'.

    Idempotent per (token, event_seq): the feed can surface the same row twice; re-entering
    would double-count it in every policy at once. A promotion or band_fire row carries its own
    event_seq and so opens its own position even when the token is already open as B.
    """
    try:
        token = str(row.get("token") or "").lower()
        seq = int(float(row.get("event_seq")))
    except (TypeError, ValueError):
        return "invalid", None
    if not token or seq < 0:
        return "invalid", None
    key = _pos_key(token, seq)
    book = _load(BOOK_PATH, {})
    if key in book:
        return "already", None
    symbol = str(row.get("symbol") or "?")
    tier = str(row.get("tier") or "B")
    usd = float(config.STACK_USD * config.POSITION_PCT)
    try:
        q = quote_buy_fn(token, usd, now_s, weth_px=weth_px)
    except Exception as exc:
        print("  [livebook] buy quote raised for %s: %s" % (token, str(exc)[:120]))
        q = None
    if not isinstance(q, dict) or q.get("status") == "deferred":
        return "deferred", None
    if q.get("status") == "absent":
        _append_fill({"ts": now_s, "token": token, "event_seq": seq, "symbol": symbol,
                      "tier": tier, "policy": "*", "side": "buy_failed",
                      "frac_of_original": 0.0, "px": 0.0, "usd_proceeds": 0.0, "gap_s": 0,
                      "note": "no route at alert — not opened; " + quotes.describe(q)})
        return "absent", None
    try:
        tokens = int(q.get("amount_out_raw") or 0)
    except (TypeError, ValueError):
        tokens = 0
    if tokens <= 0:
        return "invalid", None
    cost = _num(q.get("usd"), usd) or usd
    entry_px = cost / tokens                    # USD per RAW token — the basis for every multiple
    dec = None
    try:
        dec = (decimals_fn or rpc.decimals)(token)
        dec = int(dec) if dec is not None else None
    except Exception:
        dec = None
    # ALERT_SEQ — a strictly increasing integer, never reused, never renumbered. It is what makes
    # "this policy was nominated BEFORE these rows existed" a checkable fact rather than a claim.
    # improve.py's forward test reads only rows with alert_seq > nominated_at_alert_seq (and
    # opened_ts > nominated_ts, since a rebuilt book restarts this counter at 1).
    aseq = 1 + max([int(p.get("alert_seq", 0) or 0) for p in book.values()] or [0])
    alert_ts = _num(row.get("alert_ts"), now_s)
    prior = row.get("prior_event_seq")
    try:
        prior = int(float(prior)) if prior not in (None, "", "nan", "None") else None
    except (TypeError, ValueError):
        prior = None
    reserves = (q.get("probes") or {}).get("reserves")
    pos = {"token": token, "event_seq": seq, "event_kind": str(row.get("event_kind") or ""),
           "prior_event_seq": prior, "symbol": symbol, "tier": tier, "ledger_tier": tier,
           "ledger_plan_name": str(row.get("plan_name") or ""),
           "fired_band": str(row.get("fired_band") or ""),
           "sidecar_true": sorted({str(b) for b in (row.get("sidecar_true") or [])}),
           "alert_ts": alert_ts, "entry_lag_s": round(now_s - alert_ts, 1),
           "opened_ts": now_s, "alert_seq": aseq,
           "cost_usd": cost, "tokens_raw": tokens, "entry_px": entry_px, "decimals": dec,
           "weth_px_usd_open": _num(q.get("weth_px_usd")), "quote_source_open": q.get("source"),
           "entry_impact_pct": _num(q.get("impact_pct"), 0.0),
           "last_tick_ts": now_s, "n_ticks": 0, "done": False,
           "last_honest_px": entry_px, "suspect_ticks": 0, "deferred_ticks": 0,
           "suspect": False, "unpriced": False, "max_gap_s": 0.0,
           "policies": {name: _new_policy_state() for name in _names()}}
    for st in pos["policies"].values():
        st["peak_px"] = entry_px
    book[key] = pos
    _save_atomic(book, BOOK_PATH)               # durable FIRST...
    _append_fill(_fill_row(pos, "*", "buy", 1.0, entry_px, -cost, 0.0, now_s,
                           note="shared entry via %s, impact %.4f, reserve_weth %s; fired_band %s; "
                                "sidecar_true %s; %s" % (
                                    q.get("source"), pos["entry_impact_pct"],
                                    reserves[1] if reserves else "?", pos["fired_band"] or "-",
                                    ",".join(pos["sidecar_true"]) or "-", quotes.describe(q))))
    return "opened", pos


# ── the state machine ────────────────────────────────────────────────────────────
def random_exit_hold_s(pos: dict) -> float:
    """The ctl_random_exit hold: uniform over the decision horizon, seeded from a sha256 digest
    of the position key (NOT builtin hash(), which PYTHONHASHSEED salts per process) so it can
    never be re-rolled until it looks bad. Two positions on one token get different holds."""
    import numpy as _np
    key = _pos_key(pos["token"], pos["event_seq"])
    seed = config.SEED + int(hashlib.sha256(key.encode()).hexdigest()[:8], 16)
    return float(_np.random.default_rng(seed).uniform(0.0, DECISION_HORIZON_S))


def _step_policy(pos: dict, name: str, st: dict, px: float, now_s: float,
                 usd_full: float, gap_s: float, flow=None) -> list:
    """Advance ONE policy by one observed price. Returns the fills it produced. `flow` is the
    tick's POL.flow_from_market() dict (or None when dark / not read) — carried, not yet read:
    no policy in the family has a flow schema, so nothing here consumes it.

    A tick is a single price, so unlike the bar simulator there is no ordering to assume. The
    one genuine ambiguity left is that both a rung and a protective exit can be satisfied by the
    same tick (peak 100x, price now 5x: the 2x rung is below us AND the 30% trail is broken).
    That means the move happened BETWEEN ticks and we never observed the intervening prices, so
    the protective exit wins and the remainder closes at the observed price. Crediting the rung
    as well would be claiming a fill at a price we never saw.
    """
    # Explicit membership test, NOT `POLICIES.get(name) or CONTROLS[name]`: hold_to_end's policy
    # is the empty dict, which is falsy, so `or` fell through and raised KeyError on the one
    # policy that matters most as the baseline.
    if name in POL.POLICIES:
        pol = POL.POLICIES[name]
    elif name in getattr(POL, "CONTROLS", {}):
        pol = POL.CONTROLS[name]
    else:
        return []                               # a retired candidate: left for the terminal mark
    if st["closed"]:
        return []
    if st["rungs_left"] is None:
        st["rungs_left"] = [list(x) for x in (pol.get("ladder") or [])]
    entry_px = pos["entry_px"]
    fills: list = []
    if px > st["peak_px"]:
        st["peak_px"] = px

    stop_frac, trail_frac = pol.get("stop"), pol.get("trail")
    levels = []
    if stop_frac is not None:
        levels.append(entry_px * (1.0 - stop_frac))
    if trail_frac is not None:
        levels.append(st["peak_px"] * (1.0 - trail_frac))
    exit_level = max(levels) if levels else None

    def _close(reason, fill_px):
        frac = st["remaining"]
        if frac <= 1e-9:
            _finish_state(pos, st, reason, now_s, gap_s)
            return
        usd = usd_full * frac * (fill_px / px if px > 0 else 0.0)
        st["realized_usd"] += usd
        st["remaining"] = 0.0
        _finish_state(pos, st, reason, now_s, gap_s)
        fills.append(_fill_row(pos, name, reason, frac, fill_px, usd, gap_s, now_s))

    # random_exit control (2026-09 solana audit: this key was read only by the bar simulator,
    # so the live control was a bit-identical alias of hold_to_end on 294/294 positions — a
    # control that cannot detect anything). Streaming equivalent of "uniform random bar".
    if pol.get("random_exit"):
        if (now_s - pos["opened_ts"]) >= random_exit_hold_s(pos):
            _close("random_exit", px)
        return fills

    if exit_level is not None and px <= exit_level:
        is_trail = trail_frac is not None and \
            st["peak_px"] * (1.0 - trail_frac) >= (levels[0] if stop_frac is not None else -1.0)
        _close("trail" if is_trail else "stop", px)
        return fills

    # ladder rungs: pre-committed LIMIT levels, filled AT the level, never at the observed tick
    still = []
    for lv, fr in st["rungs_left"]:
        if px >= entry_px * lv and st["remaining"] > 1e-9:
            frac = min(fr, st["remaining"])
            fill_px = entry_px * lv
            usd = usd_full * frac * (fill_px / px if px > 0 else 0.0)
            st["realized_usd"] += usd
            st["remaining"] -= frac
            fills.append(_fill_row(pos, name, "tp_%gx" % lv, frac, fill_px, usd, gap_s, now_s,
                                   note="limit fill at rung level"))
        else:
            still.append([lv, fr])
    st["rungs_left"] = still
    if st["remaining"] <= 1e-9:
        st["remaining"] = 0.0
        _finish_state(pos, st, "ladder_complete", now_s, gap_s)
        return fills

    mh = pol.get("max_hold_s")
    if mh is not None and (now_s - pos["opened_ts"]) >= mh:
        _close("time_exit", px)
    return fills


def _close_all_unfilled(pos: dict, reason: str, now_s: float, gap_s: float) -> int:
    """suspect / unpriced retirement: every open policy closes with NO fill and its unsold
    fraction left in place — the scorer excludes these positions, never scores them -100%."""
    n = 0
    for st in pos["policies"].values():
        if not st.get("closed"):
            _finish_state(pos, st, reason, now_s, gap_s)
            n += 1
    pos["done"] = True
    return n


# ── the feed: the ledger of record, by config.LIVEBOOK_FEED_SOURCE ───────────────
def _cloud_ledger_rows() -> list:
    """The ledger of record, in the feed's mode (config.LIVEBOOK_FEED_SOURCE):
      worktree — the keeper's mode: data/ledger.csv in THIS checkout, csv.DictReader, no
                 subprocess. run.py saves the ledger in one tmp + os.replace and the tick holds
                 the keeper's lock, so a reader sees the old or the new file, never a partial one.
      origin   — the Mac's mode: `git fetch` (incremental) then publish.origin_blob (`git show
                 origin/main:data/ledger.csv`, exit 128 ⇒ None ⇒ []). Deliberately NOT `git
                 pull`: a data-collection job must never be able to create a merge conflict or
                 move the user's HEAD.
    A feed outage must never kill the tick loop: every failure is []."""
    if config.LIVEBOOK_FEED_SOURCE == "worktree":
        try:
            with open(config.LEDGER_PATH, newline="") as fh:
                return [dict(r) for r in csv.DictReader(fh)]
        except Exception:
            return []
    root = config.ROOT
    try:
        subprocess.run(["git", "fetch", "-q", publish.REMOTE], cwd=root, timeout=60,
                       capture_output=True, check=False)
    except Exception:
        pass                                    # stale origin/main is still a valid read
    try:
        text = publish.origin_blob(LEDGER_REL, root)
    except Exception:
        text = None
    if not text:
        return []
    try:
        return [dict(r) for r in csv.DictReader(io.StringIO(text))]
    except Exception:
        return []


def _sidecar_true(seqs: set) -> dict:
    """{event_seq: sorted band names whose sidecar verdict is "1"} for the given event_seqs,
    from data/band_verdicts.csv in the feed's mode: the checkout's file (worktree) or
    publish.origin_blob(VERDICTS_REL) (origin — no second fetch: the sidecar rides the same
    scan commit as the ledger row, so the ledger's fetch already brought it). {} on ANY
    failure, including an unreadable or missing sidecar: fail closed — a row is never
    always-admitted on a stamp the feed could not read. Verdicts 0 / NA are not stamps."""
    if not seqs:
        return {}
    try:
        if config.LIVEBOOK_FEED_SOURCE == "worktree":
            with open(config.BAND_VERDICTS_PATH, newline="") as fh:
                text = fh.read()
        else:
            text = publish.origin_blob(VERDICTS_REL, config.ROOT)
        if not text:
            return {}
        want = {int(q) for q in seqs}
        out: dict = {}
        for r in csv.DictReader(io.StringIO(text)):
            if str(r.get("verdict")).strip() != "1":
                continue
            try:
                seq = int(float(r.get("event_seq")))
            except (TypeError, ValueError):
                continue
            if seq in want:
                out.setdefault(seq, set()).add(str(r.get("band")))
        return {k: sorted(v) for k, v in out.items()}
    except Exception:
        return {}


def _row_item(r: dict, now_s: float) -> dict | None:
    """A ledger row → the feed's pending-item shape (None when unparseable). `sidecar_true`
    starts empty; feed_from_ledger stamps it from the sidecar once per batch of new rows."""
    try:
        token = str(r.get("token") or "").lower()
        ats = float(r.get("alert_ts"))
        seq = int(float(r.get("event_seq")))
    except (TypeError, ValueError):
        return None
    if not token or not math.isfinite(ats):
        return None
    prior = r.get("prior_event_seq")
    try:
        prior = int(float(prior)) if prior not in (None, "", "nan", "None") else None
    except (TypeError, ValueError):
        prior = None
    return {"token": token, "event_seq": seq, "symbol": str(r.get("symbol") or "?"),
            "tier": str(r.get("tier") or "B"), "event_kind": str(r.get("event_kind") or ""),
            "prior_event_seq": prior, "plan_name": str(r.get("plan_name") or ""),
            "fired_band": str(r.get("fired_band") or ""), "sidecar_true": [],
            "alert_ts": ats, "first_seen_ts": now_s}


def feed_from_ledger(now_s: float, *, quote_buy_fn=quotes.quote_buy, rows_fn=_cloud_ledger_rows,
                     weth_px=None, decimals_fn=None, verbose: bool = True) -> dict:
    """Open a book position for each new ledger event row. Returns counts.

    A WATERMARK on alert_ts, not a scan of the whole file: the first run sets it to `now` and
    opens nothing, so a historical backlog is skipped rather than opened at prices that are
    hours stale (and rather than re-logging every old row as a miss on every tick, forever).
    Per new row: already in the book ⇒ skip; tier outside LIVEBOOK_FEED_TIERS ⇒ missed; entry
    lag over MAX_ENTRY_LAG_S ⇒ missed with the lag; a B row when LIVEBOOK_MAX_OPEN positions are
    open ⇒ missed 'book_full' (A and promotion rows are always admitted); a deferred buy quote
    ⇒ the row waits in feed.pending and is retried every tick until the lag cap.

    THE BAND UNDER TEST. Every batch of new rows is stamped `sidecar_true` (the bands whose
    sidecar verdict at that event_seq is 1 — one sidecar read per batch, none when there are
    no new rows). When config.LIVEBOOK_BAND_UNDER_TEST names a band, a row it selected is
    admitted at the cap too, under LIVEBOOK_BAND_UNDER_TEST_MAX_OPEN open such positions;
    past that sub-cap it is refused 'band_under_test_full'. A pending (deferred-buy) row keeps
    its stamp. Unreadable sidecar ⇒ empty stamps ⇒ plain B rules (fail closed).
    """
    book = _load(BOOK_PATH, {})
    state = _load(FEED_STATE_PATH, {})
    pending = [p for p in (state.get("pending") or []) if isinstance(p, dict)]
    try:
        rows = list(rows_fn() or [])
    except Exception as exc:
        print("  [livebook] feed rows_fn failed: %s" % str(exc)[:120])
        rows = []
    stats = {"seen": len(rows), "opened": 0, "missed": 0, "pending": 0, "already": 0,
             "invalid": 0}
    mark = _num(state.get("watermark_alert_ts"))
    if mark is None:                            # first run — start from now, skip the backlog
        _save_atomic({"watermark_alert_ts": now_s, "pending": []}, FEED_STATE_PATH)
        if verbose:
            print("feed: watermark initialised at %.0f; %d historical row(s) skipped"
                  % (now_s, len(rows)))
        return stats
    open_count = sum(1 for p in book.values() if isinstance(p, dict) and not p.get("done"))
    but = config.LIVEBOOK_BAND_UNDER_TEST or None
    under_test_open = sum(1 for p in book.values() if isinstance(p, dict) and not p.get("done")
                          and but is not None and but in (p.get("sidecar_true") or []))

    def _miss(item, lag, reason):
        stats["missed"] += 1
        _append_jsonl(MISSED_PATH, {"ts": now_s, "token": item["token"],
                                    "event_seq": item["event_seq"], "symbol": item["symbol"],
                                    "tier": item["tier"], "alert_ts": item["alert_ts"],
                                    "entry_lag_s": round(lag, 1), "reason": reason})

    def _try_open(item) -> bool:
        """True when the item is settled (opened / missed / already); False = still pending."""
        nonlocal open_count, under_test_open
        key = _pos_key(item["token"], item["event_seq"])
        if key in book:
            stats["already"] += 1
            return True
        lag = now_s - item["alert_ts"]
        if lag > config.MAX_ENTRY_LAG_S:
            _miss(item, lag, "entry lag exceeds MAX_ENTRY_LAG_S")
            return True
        under_test = but is not None and but in (item.get("sidecar_true") or [])
        always = item["tier"] == "A" or item["event_kind"] == "promotion" or (
            under_test and under_test_open < config.LIVEBOOK_BAND_UNDER_TEST_MAX_OPEN)
        if not always and open_count >= config.LIVEBOOK_MAX_OPEN:
            _miss(item, lag, "band_under_test_full" if under_test else "book_full")
            return True
        status, pos = open_alert(item, now_s, quote_buy_fn=quote_buy_fn, weth_px=weth_px,
                                 decimals_fn=decimals_fn)
        if status == "opened":
            stats["opened"] += 1
            open_count += 1
            if under_test:
                under_test_open += 1
            book[key] = pos
            return True
        if status == "deferred":
            return False
        if status == "already":
            stats["already"] += 1
            return True
        _miss(item, lag, "no route at alert (absent)" if status == "absent"
              else "unusable buy quote (%s)" % status)
        return True

    still: list = []
    for item in pending:                        # older rows get their slot first
        if not _try_open(item):
            still.append(item)
    pend_keys = {_pos_key(p["token"], p["event_seq"]) for p in still}

    newest = mark
    parsed = []
    for r in rows:
        item = _row_item(r, now_s)
        if item is None:
            stats["invalid"] += 1
            continue
        if item["alert_ts"] <= mark:
            continue
        parsed.append(item)
    parsed.sort(key=lambda it: (it["alert_ts"], it["event_seq"]))
    if parsed:                                  # ONE sidecar read per batch of new rows, none otherwise
        stamps = _sidecar_true({it["event_seq"] for it in parsed})
        for item in parsed:
            item["sidecar_true"] = list(stamps.get(item["event_seq"], []))
    for item in parsed:
        newest = max(newest, item["alert_ts"])
        key = _pos_key(item["token"], item["event_seq"])
        if key in book or key in pend_keys:
            stats["already"] += 1
            continue
        if item["tier"] not in config.LIVEBOOK_FEED_TIERS:
            _miss(item, now_s - item["alert_ts"], "tier not in LIVEBOOK_FEED_TIERS")
            continue
        if not _try_open(item):
            still.append(item)
            pend_keys.add(key)
    stats["pending"] = len(still)
    _save_atomic({"watermark_alert_ts": newest, "pending": still}, FEED_STATE_PATH)
    if verbose and (stats["opened"] or stats["missed"] or stats["pending"]):
        print("feed: %d row(s) seen, opened %d, missed %d, pending %d (deferred buys), "
              "already %d" % (stats["seen"], stats["opened"], stats["missed"],
                              stats["pending"], stats["already"]))
    return stats


# ── the tick ─────────────────────────────────────────────────────────────────────
def _default_dex(tokens, now_s: float) -> dict:
    return dexscreener.enrich_many(sorted(tokens), now_s, max_age_sec=0)


def _quote_rounds(due: list) -> list:
    """Partition the due positions into rounds of UNIQUE tokens: quote_sell_many keys its result
    by token, and two positions on one token (B sighting + promotion) hold different fills."""
    rounds: list = []
    for entry in due:
        for r in rounds:
            if entry[1]["token"] not in r["tokens"]:
                r["items"].append(entry)
                r["tokens"].add(entry[1]["token"])
                break
        else:
            rounds.append({"items": [entry], "tokens": {entry[1]["token"]}})
    return rounds


def tick(now_s: float, *, quote_many_fn=quotes.quote_sell_many, weth_px_fn=quotes.weth_price_usd,
         dex_fn=None, flow_fn=None, weth_px=None, verbose: bool = True) -> dict:
    """One cycle: ONE WETH/USD read, ONE batched sell quote for every due position, the
    quote-integrity gate, at most ONE batched flow read (the Dexscreener m5 window, only for
    honest due positions holding an open state under a flow policy — none today, so never),
    then every policy advances; the book is saved atomically FIRST and the fill / tick logs are
    flushed after it (the 2026-08-15 199-fills incident).

    Cadence: a position is due when its gap since the last tick is >= 0.9 x the wanted interval
    (0.9 so launchd jitter never skips a whole cycle): TICK_INTERVAL_S while the PREVIOUS tick
    was inside the decision horizon (so the first tick past the horizon — where sell_6h fires —
    still arrives a minute later, not 15), TICK_INTERVAL_LATE_S after. No sleep, ever.
    """
    t_mono = time.monotonic()                   # duration logging only, never a compute input
    book = _load(BOOK_PATH, {})
    stats = {"open": 0, "due": 0, "skipped": 0, "quoted": 0, "deferred": 0, "suspect": 0,
             "no_route": 0, "fills": 0, "retired": 0, "weth_deferred": False, "tick_wall_s": 0.0}
    due: list = []                              # (key, pos, gap_s)
    for key, pos in book.items():
        if not isinstance(pos, dict) or pos.get("done"):
            continue
        stats["open"] += 1
        last = _num(pos.get("last_tick_ts"), pos["opened_ts"])
        gap_s = now_s - last
        want = TICK_INTERVAL_S if (last - pos["opened_ts"]) < DECISION_HORIZON_S \
            else TICK_INTERVAL_LATE_S
        if gap_s < want * 0.9:
            stats["skipped"] += 1
            continue
        due.append((key, pos, gap_s))
    stats["due"] = len(due)
    if not due:
        stats["tick_wall_s"] = round(time.monotonic() - t_mono, 3)
        if verbose:
            print("tick: %d open, 0 due, %d not-yet-due, tick_wall_s=%.3f"
                  % (stats["open"], stats["skipped"], stats["tick_wall_s"]))
        return stats

    # (0) ONE WETH/USD per tick; unknown ⇒ nothing can be valued ⇒ nothing is stepped
    px_weth = _num(weth_px)
    if px_weth is None or px_weth <= 0:
        try:
            wst, px_weth = weth_px_fn(now_s)
        except Exception:
            wst, px_weth = "deferred", None
        if wst != "ok" or not px_weth or px_weth <= 0:
            stats["weth_deferred"] = True
            stats["tick_wall_s"] = round(time.monotonic() - t_mono, 3)
            print("tick: WETH/USD deferred — tick SKIPPED, %d due position(s) untouched, "
                  "tick_wall_s=%.3f" % (len(due), stats["tick_wall_s"]))
            return stats

    # (1) quotes: one batched call per round of unique tokens (normally exactly one)
    qmap: dict = {}
    for r in _quote_rounds(due):
        try:
            res = quote_many_fn([(p["token"], p["tokens_raw"]) for _k, p, _g in r["items"]],
                                now_s, weth_px=px_weth) or {}
        except Exception as exc:
            print("  [livebook] quote_sell_many raised: %s" % str(exc)[:120])
            res = {}
        for key, pos, _g in r["items"]:
            qmap[key] = res.get(pos["token"])

    # (2) classify every due position; collect the R2 corroboration set
    plan: list = []                             # dicts: key,pos,gap_s,q,verdict,reason,px,usd_full
    need_dex: set = set()
    for key, pos, gap_s in due:
        q = qmap.get(key)
        item = {"key": key, "pos": pos, "gap_s": gap_s, "q": q, "verdict": "ok",
                "reason": None, "px": 0.0, "usd_full": 0.0, "dex_px": None}
        plan.append(item)
        if not isinstance(q, dict) or q.get("status") == "deferred":
            item["verdict"] = "deferred"
            item["reason"] = quotes.describe(q) if isinstance(q, dict) else "no quote returned"
            continue
        if q.get("status") == "absent":
            item["verdict"] = "absent"
            item["reason"] = "absent: " + quotes.describe(q)
            continue
        usd_full = _num(q.get("usd"), 0.0) or 0.0
        tokens_raw = int(pos.get("tokens_raw") or 0)
        px = usd_full / tokens_raw if tokens_raw > 0 else 0.0
        item["px"], item["usd_full"] = px, usd_full
        last_honest = _num(pos.get("last_honest_px"), pos["entry_px"]) or pos["entry_px"]
        if px > last_honest:                    # UPWARD moves only — downside is never gated
            mult = px / pos["entry_px"] if pos["entry_px"] > 0 else float("inf")
            reserves = (q.get("probes") or {}).get("reserves")
            rw = None
            try:
                rw = int(reserves[1]) if reserves and reserves[1] is not None else None
            except (TypeError, ValueError, IndexError):
                rw = None
            # `slack`: a ratio one ulp above the threshold is not evidence (10x after a float
            # round trip of a 1e-23 entry price lands at 10.000000000000002).
            slack = 1.0 + 1e-9
            if mult > config.LIVEBOOK_MAX_PLAUSIBLE_MULT * slack:
                item["verdict"], item["reason"] = "suspect", "mult>%gx" % config.LIVEBOOK_MAX_PLAUSIBLE_MULT
            elif rw is not None and int(q.get("amount_out_raw") or 0) > rw:
                item["verdict"], item["reason"] = "suspect", "out exceeds pool reserve"
            elif last_honest > 0 and px / last_honest > config.LIVEBOOK_TICK_JUMP_MAX * slack:
                item["verdict"] = "corroborate"
                need_dex.add(pos["token"])

    # (3) ONE batched fresh Dexscreener read over every token that needs corroboration
    dex = {"ok": {}, "absent": set(), "deferred": set()}
    if need_dex:
        try:
            got = (dex_fn or _default_dex)(sorted(need_dex), now_s)
            if isinstance(got, dict):
                dex = got
        except Exception as exc:
            print("  [livebook] dexscreener corroboration raised: %s" % str(exc)[:120])
    dex_ok = {str(k).lower(): v for k, v in (dex.get("ok") or {}).items()}
    tol = float(config.LIVEBOOK_XSOURCE_TOL)
    for item in plan:
        if item["verdict"] != "corroborate":
            continue
        pos, px = item["pos"], item["px"]
        m = dex_ok.get(pos["token"])
        dec = pos.get("decimals")
        p_usd = _num(m.get("price_usd")) if isinstance(m, dict) else None
        if not isinstance(m, dict) or dec is None or not p_usd or p_usd <= 0 or px <= 0:
            item["verdict"], item["reason"] = "suspect", "jump uncorroborated"
            continue
        dex_px = p_usd / 10 ** int(dec)         # USD per RAW token, same units as px
        item["dex_px"] = dex_px
        ratio = dex_px / px
        if 1.0 / tol <= ratio <= tol:
            item["verdict"], item["reason"] = "ok", "jump corroborated by dexscreener"
        else:
            item["verdict"], item["reason"] = "suspect", "jump contradicted by dexscreener"

    # (3b) ONE batched flow read — a SEPARATE call from the R2 corroboration (its call counts
    # are pinned) — over the honest due tokens whose position holds an OPEN state under a flow
    # policy (POL.flow_policy_names(): [] today ⇒ this never runs). A suspect / deferred /
    # absent tick steps nothing, so it reads nothing. `flow_status`: ok | dark (asked, not
    # answered — deferred, absent or an unanswered m5 window) | n/a (not asked).
    flow_names = POL.flow_policy_names()
    need_flow: set = set()
    for item in plan:
        item["flow"], item["flow_status"], item["need_flow"] = None, "n/a", False
        if not flow_names or item["verdict"] not in ("ok", "corroborate"):
            continue
        pols = item["pos"].get("policies") or {}
        if any(isinstance(pols.get(n), dict) and not pols[n].get("closed") for n in flow_names):
            item["need_flow"] = True
            need_flow.add(item["pos"]["token"])
    flow = {"ok": {}, "absent": set(), "deferred": set()}
    if need_flow:
        try:
            got = (flow_fn or _default_dex)(sorted(need_flow), now_s)
            if isinstance(got, dict):
                flow = got
        except Exception as exc:
            print("  [livebook] flow read raised: %s" % str(exc)[:120])
    flow_ok = {str(k).lower(): v for k, v in (flow.get("ok") or {}).items()}
    for item in plan:
        if not item["need_flow"]:
            continue
        item["flow"] = POL.flow_from_market(flow_ok.get(item["pos"]["token"]))
        item["flow_status"] = "ok" if item["flow"] is not None else "dark"

    # (4) apply
    pending_fills: list = []
    pending_ticks: list = []
    names = _names()
    for item in plan:
        key, pos, gap_s, q = item["key"], item["pos"], item["gap_s"], item["q"]
        px, usd_full, verdict = item["px"], item["usd_full"], item["verdict"]
        line = {"ts": now_s, "token": pos["token"], "event_seq": pos["event_seq"], "px": px,
                "usd_full": usd_full,
                "mult": (px / pos["entry_px"]) if pos["entry_px"] > 0 else 0.0,
                "gap_s": round(gap_s, 1), "weth_px": px_weth,
                "source": q.get("source") if isinstance(q, dict) else None, "status": "ok",
                "flow": item["flow"], "flow_status": item["flow_status"]}
        if item["reason"]:
            line["reason"] = item["reason"]
        if item["dex_px"] is not None:
            line["dex_px"] = item["dex_px"]

        if verdict == "deferred":
            stats["deferred"] += 1
            pos["deferred_ticks"] = int(pos.get("deferred_ticks") or 0) + 1
            line["status"] = "deferred"
            if pos["deferred_ticks"] >= config.LIVEBOOK_DEFERRED_TICKS_MAX:
                _close_all_unfilled(pos, "unpriced", now_s, gap_s)
                pos["unpriced"] = True
                stats["retired"] += 1
                line["reason"] = "%s; retired UNPRICED after %d consecutive deferred ticks" % (
                    line.get("reason"), pos["deferred_ticks"])
            pending_ticks.append(line)
            continue                            # last_tick_ts untouched: the gap stays honest

        if verdict == "suspect":
            stats["suspect"] += 1
            pos["suspect_ticks"] = int(pos.get("suspect_ticks") or 0) + 1
            pos["last_tick_ts"] = now_s
            line["status"] = "suspect"
            if pos["suspect_ticks"] >= config.LIVEBOOK_SUSPECT_TICKS_MAX:
                _close_all_unfilled(pos, "suspect", now_s, gap_s)
                pos["suspect"] = True
                stats["retired"] += 1
                line["reason"] = "%s; retired SUSPECT after %d consecutive suspect ticks" % (
                    line["reason"], pos["suspect_ticks"])
            pending_ticks.append(line)
            continue

        if verdict == "absent":
            # No route on a memecoin means the pool is gone (quotes.py decided this on-chain
            # first). That is a real outcome, not an error: every still-open policy closes at
            # zero, which is exactly "could not exit a dead coin".
            stats["no_route"] += 1
            pos["n_ticks"] = int(pos.get("n_ticks") or 0) + 1
            pos["suspect_ticks"] = 0
            pos["deferred_ticks"] = 0
            pos["last_tick_ts"] = now_s
            for name, st in pos["policies"].items():
                if not st.get("closed"):
                    st["remaining"] = 0.0
                    _finish_state(pos, st, "no_route", now_s, gap_s)
                    pending_fills.append(_fill_row(pos, name, "no_route", 0.0, 0.0, 0.0, gap_s,
                                                   now_s, note="NO ROUTE — dead coin, proceeds $0"))
                    stats["fills"] += 1
            pos["done"] = True
            stats["retired"] += 1
            pending_ticks.append(line)
            continue

        # honest tick
        stats["quoted"] += 1
        pos["suspect_ticks"] = 0
        pos["deferred_ticks"] = 0
        pos["last_honest_px"] = px
        pos["n_ticks"] = int(pos.get("n_ticks") or 0) + 1
        age = now_s - pos["opened_ts"]
        if age <= DECISION_HORIZON_S:
            pos["max_gap_s"] = max(float(pos.get("max_gap_s") or 0.0), float(gap_s))
        pending_ticks.append(line)
        # A position opened before a policy existed has no state for it. Backfill rather than
        # KeyError — but a state born MID-FLIGHT records fabricated economics (a sell_3h
        # backfilled onto a 5h-old position "exits at 3h" at the deploy-time price), so it is
        # stamped and the scorer excludes stamped states from every statistic.
        for name in names:
            if name not in pos["policies"]:
                st_new = _new_policy_state()
                st_new["backfilled_ts"] = now_s
                st_new["peak_px"] = pos["entry_px"]
                pos["policies"][name] = st_new
        for name in names:
            for f in _step_policy(pos, name, pos["policies"][name], px, now_s, usd_full, gap_s,
                                  flow=item["flow"]):
                pending_fills.append(f)
                stats["fills"] += 1
        pos["last_tick_ts"] = now_s
        if all(s.get("closed") for s in pos["policies"].values()) or age > MAX_TRACK_S:
            # mark anything still open at the LAST OBSERVED price (never zero, never the peak)
            for name, st in pos["policies"].items():
                if not st.get("closed"):
                    frac = float(st.get("remaining") or 0.0)
                    usd = usd_full * frac
                    st["realized_usd"] += usd
                    st["remaining"] = 0.0
                    _finish_state(pos, st, "tracking_window_end", now_s, gap_s)
                    pending_fills.append(_fill_row(pos, name, "tracking_window_end", frac, px, usd,
                                                   gap_s, now_s,
                                                   note="terminal mark at the last observed price"))
                    stats["fills"] += 1
            pos["done"] = True
            stats["retired"] += 1

    _save_atomic(book, BOOK_PATH)               # durable FIRST...
    for f in pending_fills:                     # ...then the logs, so a crash cannot duplicate
        _append_fill(f)
    for ln in pending_ticks:
        _append_jsonl(TICKS_PATH, ln)
    stats["tick_wall_s"] = round(time.monotonic() - t_mono, 3)
    if verbose:
        print("tick: %d open, %d due, %d not-yet-due, %d quoted, %d deferred, %d suspect, "
              "%d no-route, %d fills, %d retired, weth $%.0f, tick_wall_s=%.3f"
              % (stats["open"], stats["due"], stats["skipped"], stats["quoted"],
                 stats["deferred"], stats["suspect"], stats["no_route"], stats["fills"],
                 stats["retired"], px_weth, stats["tick_wall_s"]))
    return stats


# ── scorecard ────────────────────────────────────────────────────────────────────
def _stat_row(rets: list) -> dict:
    if not rets:
        return {"n": 0, "mean": None, "median": None, "win_rate": None}
    s = sorted(rets)
    n = len(s)
    med = s[n // 2] if n % 2 else 0.5 * (s[n // 2 - 1] + s[n // 2])
    return {"n": n, "mean": sum(s) / n, "median": med,
            "win_rate": sum(1 for r in s if r > 0) / n}


def live_stats_dict(now_s=None) -> dict:
    """The per-policy realized table as a dict for the weekly summary JSON. Excludes suspect
    and unpriced positions entirely, and per policy the backfilled and `gapped` states — the
    same exclusions improve.live_returns applies. `ts` is the caller's now_s or, absent one,
    the latest tick in the book (never the wall clock)."""
    book = _load(BOOK_PATH, {})
    positions = [p for p in book.values() if isinstance(p, dict)]
    done = [p for p in positions if p.get("done")]
    scorable = [p for p in done if not p.get("suspect") and not p.get("unpriced")
                and (_num(p.get("cost_usd"), 0.0) or 0.0) > 0]

    def _table(names):
        out = {}
        for name in names:
            rets, n_g, n_bf = [], 0, 0
            for p in scorable:
                st = (p.get("policies") or {}).get(name)
                if not isinstance(st, dict) or not st.get("closed"):
                    continue
                if st.get("backfilled_ts") is not None:
                    n_bf += 1
                    continue
                if st.get("gapped"):
                    n_g += 1
                    continue
                rets.append(float(st.get("realized_usd") or 0.0) / float(p["cost_usd"]) - 1.0)
            row = _stat_row(rets)
            row["n_gapped"] = n_g
            row["n_backfilled"] = n_bf
            out[name] = row
        return out

    lags = sorted(float(p["entry_lag_s"]) for p in positions
                  if isinstance(p.get("entry_lag_s"), (int, float)))
    n_missed, n_refused_full = 0, 0
    if os.path.exists(MISSED_PATH):
        try:
            with open(MISSED_PATH) as fh:
                for ln in fh:
                    if not ln.strip():
                        continue
                    n_missed += 1
                    try:
                        if json.loads(ln).get("reason") == "band_under_test_full":
                            n_refused_full += 1
                    except Exception:
                        pass
        except Exception:
            n_missed, n_refused_full = 0, 0
    # the stamp: "stamped" = the sidecar named at least one true band for the row. An empty
    # list is either "no band true" or "sidecar unreadable" — the two are indistinguishable by
    # design (fail closed), so a falling stamped share is the signal that the sidecar is dark.
    n_stamped = sum(1 for p in positions if p.get("sidecar_true"))
    but = config.LIVEBOOK_BAND_UNDER_TEST or None
    under_test = None
    if but is not None:
        under_test = {"name": but,
                      "n_open": sum(1 for p in positions if not p.get("done")
                                    and but in (p.get("sidecar_true") or [])),
                      "n_done": sum(1 for p in done if but in (p.get("sidecar_true") or [])),
                      "n_refused_full": n_refused_full}
    ticks = [_num(p.get("last_tick_ts")) for p in positions]
    ticks = [t for t in ticks if t is not None]
    ts = now_s if now_s is not None else (max(ticks) if ticks else None)
    return {"ts": ts, "n_positions": len(positions), "n_done": len(done),
            "n_open": len(positions) - len(done),
            "n_suspect": sum(1 for p in done if p.get("suspect")),
            "n_unpriced": sum(1 for p in done if p.get("unpriced")),
            "n_gapped": sum(1 for p in done if any(
                isinstance(s, dict) and s.get("gapped") for s in (p.get("policies") or {}).values())),
            "n_no_route": sum(1 for p in done if any(
                isinstance(s, dict) and s.get("close_reason") == "no_route"
                for s in (p.get("policies") or {}).values())),
            "entry_lag_median_s": (lags[len(lags) // 2] if lags else None),
            "n_missed": n_missed,
            "sidecar_coverage": {"n_stamped": n_stamped, "n_unstamped": len(positions) - n_stamped},
            "band_under_test": under_test,
            "by_tier": {t: sum(1 for p in positions if p.get("tier") == t) for t in ("A", "B")},
            "per_policy": _table(list(POL.POLICIES)),
            "controls": _table(list(getattr(POL, "CONTROLS", {})))}


def scorecard(now_s=None) -> None:
    """Per-policy realised P&L on the live book. This is the number that matters — it is net of
    REAL quoted execution cost, out of sample, and free of any within-bar assumption."""
    d = live_stats_dict(now_s)
    if d["n_positions"] == 0:
        print("livebook: empty — no alerts tracked yet")
        return
    print("livebook: %d position(s), %d complete, %d still tracking; %d suspect, %d unpriced, "
          "%d with a gapped exit, %d no-route; entry lag median %s s; %d refused"
          % (d["n_positions"], d["n_done"], d["n_open"], d["n_suspect"], d["n_unpriced"],
             d["n_gapped"], d["n_no_route"],
             ("%.0f" % d["entry_lag_median_s"]) if d["entry_lag_median_s"] is not None else "?",
             d["n_missed"]))
    sc, bt = d["sidecar_coverage"], d.get("band_under_test")
    print("  sidecar: %d stamped / %d unstamped; band under test: %s"
          % (sc["n_stamped"], sc["n_unstamped"],
             ("%s — %d open, %d done, %d refused at the sub-cap" % (
                 bt["name"], bt["n_open"], bt["n_done"], bt["n_refused_full"])) if bt else "none"))
    rows = [(v["mean"], k, v) for k, v in d["per_policy"].items() if v["n"]]
    if not rows:
        print("  (no scorable completed positions yet — policies are still running)")
        return
    base = d["per_policy"].get("hold_to_end", {}).get("mean")
    print("\n  %-24s %4s %9s %8s %6s %8s %6s" % ("policy", "n", "mean ret", "median", "win%",
                                                 "vs hold", "gapped"))
    for m, name, v in sorted(rows, key=lambda r: r[0], reverse=True):
        vs = ("%+8.3f" % (m - base)) if base is not None else "       -"
        print("  %-24s %4d %+9.3f %+8.3f %5.1f%% %s %6d" % (name, v["n"], m, v["median"],
                                                        v["win_rate"] * 100, vs, v["n_gapped"]))
    for name, v in d["controls"].items():
        if v["n"]:
            print("  %-24s %4d %+9.3f %+8.3f %5.1f%%   (control)" % (
                name, v["n"], v["mean"], v["median"], v["win_rate"] * 100))
    print("\n  net of REAL quoted execution cost, suspect/unpriced positions and gapped exits")
    print("  excluded. The backtest charges a flat %.1f%% round trip; live round trips measured"
          % (POL.round_trip_cost() * 100))
    print("  -2.8% to -27.6% on solana depending on liquidity, so treat backtest P&L as")
    print("  optimistic until this table has enough rows to replace it.")


# ── entry points ─────────────────────────────────────────────────────────────────
def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    now_s = time.time()                         # captured ONCE at the entry point, then threaded
    if "--scorecard" in argv:
        scorecard(now_s)
        return 0
    if "--tick" in argv:
        try:
            wst, px = quotes.weth_price_usd(now_s)   # ONE WETH/USD read shared by feed + tick
        except Exception:
            wst, px = "deferred", None
        px = px if wst == "ok" else None
        try:
            feed_from_ledger(now_s, weth_px=px)     # pull new alerts from origin/main first...
        except Exception as exc:
            print("  [livebook] feed failed: %s" % str(exc)[:200])
        try:
            tick(now_s, weth_px=px)                  # ...then advance every open position
        except Exception as exc:
            print("  [livebook] tick failed BEFORE the book save — nothing flushed: %s"
                  % str(exc)[:200])
        return 0
    print(__doc__.strip().split("\n")[0])
    print("\nusage:")
    print("  python3 selfimprove/livebook.py --tick        # one feed + tick cycle (the keeper's book loop, 60 s)")
    print("  python3 selfimprove/livebook.py --scorecard   # per-policy live P&L")
    print("  python3 selfimprove/livebook.py               # offline smoke test, then the scorecard")
    print("  python3 selfimprove/livebook.py --live        # one real feed + tick against origin/main")
    scorecard(now_s)
    return 0


def _live() -> int:
    """One real feed + tick against origin/main (which may be empty today)."""
    now_s = time.time()
    rows = _cloud_ledger_rows()
    if not rows:
        print("no rows in origin/main:%s (nothing to feed)" % LEDGER_REL)
    else:
        print("origin/main:%s: %d row(s)" % (LEDGER_REL, len(rows)))
    wst, px = quotes.weth_price_usd(now_s)
    print("WETH/USD: %s %s" % (wst, px))
    px = px if wst == "ok" else None
    st = feed_from_ledger(now_s, rows_fn=lambda: rows, weth_px=px)
    print("feed:", st)
    st2 = tick(now_s, weth_px=px)
    print("tick:", st2)
    return 0


# ── offline smoke test ───────────────────────────────────────────────────────────
def _smoke() -> None:
    import tempfile
    g = globals()
    saved = {k: g[k] for k in ("BOOK_PATH", "FILLS_PATH", "TICKS_PATH", "FEED_STATE_PATH",
                               "MISSED_PATH")}
    saved_max_open = config.LIVEBOOK_MAX_OPEN
    saved_cfg = (config.LIVEBOOK_BAND_UNDER_TEST, config.LIVEBOOK_BAND_UNDER_TEST_MAX_OPEN,
                 config.LIVEBOOK_FEED_SOURCE, config.BAND_VERDICTS_PATH)
    WETH = 2500.0
    TOK_RAW = 10 ** 24                          # the $10 buy fills 1e24 raw (1e6 whole tokens)
    USD = config.STACK_USD * config.POSITION_PCT
    t0 = 1_800_000_000.0
    G = float(config.LIVEBOOK_MAX_SCORABLE_GAP_S)

    def _skel(side, token, status, now_s):
        return {"status": status, "side": side, "token": token, "amount_in_raw": 0,
                "amount_out_raw": None, "usd": None, "weth_px_usd": WETH, "source": None,
                "impact_pct": None, "gas_usd": 0.07, "ts": now_s,
                "probes": {"router": "skipped", "kyber": "skipped", "scanhood": "skipped",
                           "pair": "skipped", "reserves": None}}

    def buy_ok(token, usd, now_s, *, weth_px=None):
        q = _skel("buy", token, "ok", now_s)
        q.update(amount_in_raw=int(usd / WETH * 1e18), amount_out_raw=TOK_RAW, usd=usd,
                 source="router", impact_pct=0.3)
        q["probes"].update(router="ok", pair="0xpair", reserves=[10 ** 27, 2 * 10 ** 18])
        return q

    def buy_with(status):
        return lambda token, usd, now_s, weth_px=None: _skel("buy", token, status, now_s)

    # the quote script: token -> {"status", "mult", "reserve_weth"}; the injected batch reads it
    script: dict = {}

    def sell_many(items, now_s, *, weth_px=None):
        out = {}
        for token, raw in items:
            s = script.get(token, {"status": "ok", "mult": 1.0})
            q = _skel("sell", token, s.get("status", "ok"), now_s)
            q["amount_in_raw"] = int(raw)
            if q["status"] == "ok":
                usd = USD * s["mult"] * int(raw) / TOK_RAW
                q.update(amount_out_raw=int(usd / WETH * 1e18), usd=usd, source="multicall",
                         impact_pct=0.3)
                rw = s.get("reserve_weth")
                q["probes"].update(router="ok", pair="0xpair",
                                   reserves=[10 ** 27, rw] if rw is not None else None)
            out[token] = q
        return out

    dex_calls = {"n": 0}
    dex_mult = {}                               # token -> multiple the "other source" reports

    def dex_fn(tokens, now_s):
        dex_calls["n"] += 1
        ok = {}
        for t in tokens:
            m = dex_mult.get(t)
            if m is None:
                continue
            entry_whole = USD / TOK_RAW * 10 ** 18   # USD per WHOLE token at entry
            ok[t] = {"price_usd": entry_whole * m, "symbol": "X"}
        return {"ok": ok, "absent": set(), "deferred": set(tokens) - set(ok)}

    def row(token, seq, tier="A", kind="promotion", ats=None, prior=None):
        return {"token": token, "event_seq": seq, "symbol": "T%d" % seq, "tier": tier,
                "event_kind": kind, "prior_event_seq": prior, "plan_name": "cfg_ladder_stop",
                "alert_ts": t0 - 30 if ats is None else ats}

    def opn(r, now_s=t0, buy=buy_ok):
        return open_alert(r, now_s, quote_buy_fn=buy, weth_px=WETH, decimals_fn=lambda t: 18)

    def flow_fn(tokens, now_s):                 # the flow read, deferred: never called today
        return {"ok": {}, "absent": set(), "deferred": set(tokens)}

    def tk(now_s, **kw):
        return tick(now_s, quote_many_fn=sell_many, dex_fn=dex_fn, flow_fn=flow_fn, weth_px=WETH,
                    verbose=False, **kw)

    def book():
        return _load(BOOK_PATH, {})

    def fills():
        if not os.path.exists(FILLS_PATH):
            return []
        with open(FILLS_PATH, newline="") as fh:
            return list(csv.DictReader(fh))

    def ticks():
        if not os.path.exists(TICKS_PATH):
            return []
        with open(TICKS_PATH) as fh:
            return [json.loads(ln) for ln in fh if ln.strip()]

    def reset(d):
        for name, base in (("BOOK_PATH", "livebook.json"), ("FILLS_PATH", "livebook_fills.csv"),
                           ("TICKS_PATH", "livebook_ticks.jsonl"),
                           ("FEED_STATE_PATH", "livebook_feed.json"),
                           ("MISSED_PATH", "livebook_missed.jsonl")):
            p = os.path.join(d, base)
            g[name] = p
            if os.path.exists(p):
                os.remove(p)
        script.clear()
        dex_mult.clear()

    ladder_names = [n for n, p in POL.POLICIES.items() if p.get("ladder")]
    assert "cfg_ladder" in ladder_names and "hold_to_end" in POL.POLICIES and "trail_30" in POL.POLICIES
    try:
        with tempfile.TemporaryDirectory() as d:
            reset(d)
            # L1 every policy resolves and the position retires on 1x → 10x → 0.2x
            T1 = "0x" + "a" * 39 + "1"
            st, pos = opn(row(T1, 1))
            assert st == "opened" and pos["entry_px"] == USD / TOK_RAW and pos["alert_seq"] == 1
            assert pos["entry_lag_s"] == 30.0 and pos["decimals"] == 18 and pos["ledger_tier"] == "A"
            script[T1] = {"status": "ok", "mult": 1.0}
            tk(t0 + 60)
            script[T1] = {"status": "ok", "mult": 10.0}
            tk(t0 + 120)
            script[T1] = {"status": "ok", "mult": 0.2}
            tk(t0 + MAX_TRACK_S + 120)
            p = book()[_pos_key(T1, 1)]
            assert p["done"] and not p["suspect"] and not p["unpriced"]
            for name, s in p["policies"].items():
                assert s["closed"] and abs(s["remaining"]) < 1e-9, (name, s)
            by_pol: dict = {}
            for f in fills():
                if f["token"] == T1 and f["policy"] != "*":
                    by_pol[f["policy"]] = by_pol.get(f["policy"], 0.0) + float(f["frac_of_original"])
            for name in _names():
                assert abs(by_pol.get(name, 0.0) - 1.0) < 1e-6, (name, by_pol.get(name))
            sides = {f["side"] for f in fills() if f["token"] == T1 and f["policy"] == "cfg_ladder"}
            assert sides == {"tp_2x", "tp_5x", "tp_10x", "tracking_window_end"}, sides  # 10x was honest
            print("  L1 ok: 1x→10x→0.2x — every policy closed, fills sum to 1.0 each, retired")

            # L2/L3/L4 peak-then-die: trail beats hold; hold marks at the final price; rungs are limits
            T2 = "0x" + "b" * 39 + "2"
            opn(row(T2, 2))
            for ts_, m in ((60, 1.0), (120, 3.0), (180, 1.5), (MAX_TRACK_S + 180, 0.1)):
                script[T2] = {"status": "ok", "mult": m}
                tk(t0 + ts_)
            p = book()[_pos_key(T2, 2)]
            tr, hold = p["policies"]["trail_30"], p["policies"]["hold_to_end"]
            assert tr["close_reason"] == "trail" and abs(tr["realized_usd"] - USD * 1.5) < 1e-9, tr
            assert tr["realized_usd"] > hold["realized_usd"]
            assert tr["gapped"] is False and tr["close_gap_s"] == 60.0
            print("  L2 ok: trail_30 exits at 1.5x off the 3x high-water mark (%.2f) > hold_to_end (%.2f)"
                  % (tr["realized_usd"], hold["realized_usd"]))
            assert hold["close_reason"] == "tracking_window_end" and abs(hold["realized_usd"] - USD * 0.1) < 1e-9
            assert hold["gapped"] is False
            print("  L3 ok: hold_to_end marked at the final observed 0.1x ($%.2f)" % hold["realized_usd"])
            rung = [f for f in fills() if f["token"] == T2 and f["policy"] == "cfg_ladder" and f["side"] == "tp_2x"]
            assert len(rung) == 1 and float(rung[0]["px"]) == 2.0 * p["entry_px"], rung
            assert rung[0]["note"] == "limit fill at rung level"
            assert abs(float(rung[0]["usd_proceeds"]) - USD * 3.0 * 0.5 * (2.0 / 3.0)) < 1e-6
            print("  L4 ok: tp_2x filled at 2x entry_px (observed 3x), pro-rated from the full quote")

            # L5 exactly one 'buy' fill row per position
            buys: dict = {}
            for f in fills():
                if f["side"] == "buy":
                    k = (f["token"], f["event_seq"])
                    buys[k] = buys.get(k, 0) + 1
            assert len(buys) == 2 and all(v == 1 for v in buys.values()), buys
            print("  L5 ok: exactly one shared 'buy' row per position")

            # L6 an absent sell closes every policy at exactly -1.00 with no_route
            T3 = "0x" + "c" * 39 + "3"
            opn(row(T3, 3))
            script[T3] = {"status": "absent"}
            tk(t0 + 60)
            p = book()[_pos_key(T3, 3)]
            assert p["done"]
            for name, s in p["policies"].items():
                assert s["close_reason"] == "no_route" and s["realized_usd"] == 0.0
                assert s["realized_usd"] / p["cost_usd"] - 1.0 == -1.0
            nr = [f for f in fills() if f["token"] == T3 and f["side"] == "no_route"]
            assert len(nr) == len(_names())
            print("  L6 ok: absent quote → every policy -1.00, close_reason no_route")

            # L7 identity: same (token, event_seq) opens once; a promotion row opens a SECOND position
            T4 = "0x" + "d" * 39 + "4"
            n0 = len(book())
            assert opn(row(T4, 10, tier="B", kind="first_sighting"))[0] == "opened"
            assert opn(row(T4, 10, tier="B", kind="first_sighting"))[0] == "already"
            assert opn(row(T4, 11, tier="A", kind="promotion", prior=10))[0] == "opened"
            b = book()
            assert len(b) == n0 + 2 and _pos_key(T4, 10) in b and _pos_key(T4, 11) in b
            assert b[_pos_key(T4, 11)]["prior_event_seq"] == 10
            assert b[_pos_key(T4, 11)]["alert_seq"] == b[_pos_key(T4, 10)]["alert_seq"] + 1
            script[T4] = {"status": "ok", "mult": 2.5}
            stt = tk(t0 + 60)
            assert stt["quoted"] >= 2            # both positions on one token priced (two rounds)
            assert all(b2["policies"]["cfg_ladder"]["realized_usd"] > 0
                       for b2 in (book()[_pos_key(T4, 10)], book()[_pos_key(T4, 11)]))
            print("  L7 ok: (token, event_seq) opens once; the promotion row opened a second position")

            # L8 a fabricated 5000x tick is suspect: steps nothing, fills nothing
            T5 = "0x" + "e" * 39 + "5"
            opn(row(T5, 5))
            script[T5] = {"status": "ok", "mult": 1.0}
            tk(t0 + 60)
            dex_mult[T5] = 1.0
            script[T5] = {"status": "ok", "mult": 5000.0}
            nf = len(fills())
            tk(t0 + 120)
            p = book()[_pos_key(T5, 5)]
            last = [t for t in ticks() if t["token"] == T5][-1]
            assert last["status"] == "suspect" and last["reason"].startswith("mult>"), last
            assert p["suspect_ticks"] == 1 and p["n_ticks"] == 1 and len(fills()) == nf
            assert p["policies"]["cfg_ladder"]["realized_usd"] == 0.0 and not p["policies"]["cfg_ladder"]["closed"]
            print("  L8 ok: 5000x tick → suspect (%s), no step, no fill, ladder untouched" % last["reason"])
            # L8b a 12x jump that Dexscreener contradicts (says 1.0x) is suspect
            script[T5] = {"status": "ok", "mult": 12.0}
            tk(t0 + 180)
            last = [t for t in ticks() if t["token"] == T5][-1]
            assert last["status"] == "suspect" and last["reason"] == "jump contradicted by dexscreener", last
            assert last["dex_px"] == p["entry_px"] * 1.0 and book()[_pos_key(T5, 5)]["suspect_ticks"] == 2
            # L8c a 12x jump with Dexscreener deferred is 'jump uncorroborated'
            del dex_mult[T5]
            tk(t0 + 240)
            last = [t for t in ticks() if t["token"] == T5][-1]
            assert last["status"] == "suspect" and last["reason"] == "jump uncorroborated", last
            assert book()[_pos_key(T5, 5)]["suspect_ticks"] == 3
            print("  L8b/c ok: 12x contradicted by dexscreener → suspect; dexscreener dark → 'jump uncorroborated'")
            # L8d out > reserve_weth is suspect even at a modest multiple
            script[T5] = {"status": "ok", "mult": 1.5, "reserve_weth": 1}
            tk(t0 + 300)
            last = [t for t in ticks() if t["token"] == T5][-1]
            assert last["status"] == "suspect" and last["reason"] == "out exceeds pool reserve", last
            print("  L8d ok: WETH out above the pool reserve → suspect")

            # L9 a >10x jump corroborated within 3x is accepted and the rungs fill
            T6 = "0x" + "f" * 39 + "6"
            opn(row(T6, 6))
            script[T6] = {"status": "ok", "mult": 1.0}
            tk(t0 + 60)
            dex_mult[T6] = 12.0 * 2.0            # within LIVEBOOK_XSOURCE_TOL of the quote
            script[T6] = {"status": "ok", "mult": 12.0}
            n_dex = dex_calls["n"]
            tk(t0 + 120)
            p = book()[_pos_key(T6, 6)]
            last = [t for t in ticks() if t["token"] == T6][-1]
            assert last["status"] == "ok" and last["reason"] == "jump corroborated by dexscreener"
            assert dex_calls["n"] == n_dex + 1
            sides = {f["side"] for f in fills() if f["token"] == T6 and f["policy"] == "cfg_ladder"}
            assert sides == {"tp_2x", "tp_5x", "tp_10x"}, sides
            assert p["policies"]["cfg_ladder"]["close_reason"] == "" and abs(p["policies"]["cfg_ladder"]["remaining"] - 0.1) < 1e-9
            print("  L9 ok: 12x jump corroborated (dex 24x, within 3x) → accepted, three rungs filled at their levels")

            # L10 a drop to 0.01x in one tick is never suspect (and Dexscreener is never asked)
            T7 = "0x" + "0" * 38 + "a7"
            opn(row(T7, 7))
            script[T7] = {"status": "ok", "mult": 1.0}
            tk(t0 + 60)
            script[T7] = {"status": "ok", "mult": 0.01, "reserve_weth": 1}   # even with R3 bait
            n_dex = dex_calls["n"]
            tk(t0 + 120)
            p = book()[_pos_key(T7, 7)]
            last = [t for t in ticks() if t["token"] == T7][-1]
            assert last["status"] == "ok" and dex_calls["n"] == n_dex
            assert p["policies"]["stop_50"]["close_reason"] == "stop" and p["suspect_ticks"] == 0
            print("  L10 ok: 0.01x collapse is never gated; stop_50 fired at the observed price")

            # L11 LIVEBOOK_SUSPECT_TICKS_MAX consecutive suspects retire the position 'suspect'
            T8 = "0x" + "0" * 38 + "b8"
            opn(row(T8, 8))
            script[T8] = {"status": "ok", "mult": 5000.0}
            for i in range(config.LIVEBOOK_SUSPECT_TICKS_MAX):
                assert not book()[_pos_key(T8, 8)]["done"]
                tk(t0 + 60 * (i + 1))
            p = book()[_pos_key(T8, 8)]
            assert p["done"] and p["suspect"] and p["suspect_ticks"] == config.LIVEBOOK_SUSPECT_TICKS_MAX
            assert all(s["close_reason"] == "suspect" and s["remaining"] == 1.0 for s in p["policies"].values())
            assert not [f for f in fills() if f["token"] == T8 and f["policy"] != "*"]
            print("  L11 ok: %d consecutive suspect ticks → retired 'suspect', no fills"
                  % config.LIVEBOOK_SUSPECT_TICKS_MAX)

            # L12 an honest tick after a suspect resets the counter
            T9 = "0x" + "0" * 38 + "c9"
            opn(row(T9, 9))
            script[T9] = {"status": "ok", "mult": 5000.0}
            tk(t0 + 60)
            assert book()[_pos_key(T9, 9)]["suspect_ticks"] == 1
            script[T9] = {"status": "ok", "mult": 1.1}
            tk(t0 + 120)
            p = book()[_pos_key(T9, 9)]
            assert p["suspect_ticks"] == 0 and p["n_ticks"] == 1
            assert abs(p["last_honest_px"] / p["entry_px"] - 1.1) < 1e-9
            print("  L12 ok: an honest tick after a suspect resets suspect_ticks to 0")

            # L13 LIVEBOOK_DEFERRED_TICKS_MAX deferred quotes retire as 'unpriced' (never -100%)
            T10 = "0x" + "0" * 37 + "d10"
            opn(row(T10, 12))
            script[T10] = {"status": "deferred"}
            for i in range(config.LIVEBOOK_DEFERRED_TICKS_MAX):
                assert not book()[_pos_key(T10, 12)]["done"]
                tk(t0 + 60 * (i + 1))
            p = book()[_pos_key(T10, 12)]
            assert p["done"] and p["unpriced"] and not p["suspect"]
            assert p["deferred_ticks"] == config.LIVEBOOK_DEFERRED_TICKS_MAX and p["n_ticks"] == 0
            assert all(s["close_reason"] == "unpriced" and s["remaining"] == 1.0 for s in p["policies"].values())
            d_ = live_stats_dict(t0)
            assert d_["n_unpriced"] == 1 and d_["n_suspect"] >= 1   # T5 also retires during these ticks
            assert d_["per_policy"]["hold_to_end"]["n"] == sum(
                1 for q_ in book().values() if q_["done"] and not q_["suspect"] and not q_["unpriced"])
            json.dumps(d_, allow_nan=False)
            print("  L13 ok: %d deferred ticks → retired 'unpriced'; excluded from the scorecard, never -100%%"
                  % config.LIVEBOOK_DEFERRED_TICKS_MAX)

            # L14 the feed: watermark, lag refusal, one open, no re-open, empty rows
            reset(d)
            tf = t0 + 10_000
            st = feed_from_ledger(tf, quote_buy_fn=buy_ok, rows_fn=lambda: [row("0x" + "1" * 40, 1, ats=tf - 100)],
                                  weth_px=WETH, decimals_fn=lambda t: 18, verbose=False)
            fs = _load(FEED_STATE_PATH, {})
            assert fs["watermark_alert_ts"] == tf and st["opened"] == 0 and not book()
            late_ts = tf + 10
            fresh_ts_now = late_ts + config.MAX_ENTRY_LAG_S + 1
            rows_ = [row("0x" + "1" * 40, 1, ats=late_ts),                         # past the lag cap
                     row("0x" + "2" * 40, 2, ats=fresh_ts_now - 30),               # fresh
                     row("0x" + "3" * 40, 3, tier="C", ats=fresh_ts_now - 20)]     # tier not fed
            st = feed_from_ledger(fresh_ts_now, quote_buy_fn=buy_ok, rows_fn=lambda: rows_,
                                  weth_px=WETH, decimals_fn=lambda t: 18, verbose=False)
            assert st["opened"] == 1 and st["missed"] == 2, st
            missed = [json.loads(ln) for ln in open(MISSED_PATH) if ln.strip()]
            m1 = [m for m in missed if m["event_seq"] == 1][0]
            assert m1["reason"].startswith("entry lag") and m1["entry_lag_s"] == config.MAX_ENTRY_LAG_S + 1
            assert [m for m in missed if m["event_seq"] == 3][0]["reason"] == "tier not in LIVEBOOK_FEED_TIERS"
            p = book()[_pos_key("0x" + "2" * 40, 2)]
            assert p["entry_lag_s"] == 30.0 and p["alert_ts"] == fresh_ts_now - 30
            st = feed_from_ledger(fresh_ts_now + 60, quote_buy_fn=buy_ok, rows_fn=lambda: rows_,
                                  weth_px=WETH, decimals_fn=lambda t: 18, verbose=False)
            assert st["opened"] == 0 and st["missed"] == 0 and len(book()) == 1, st
            st = feed_from_ledger(fresh_ts_now + 120, quote_buy_fn=buy_ok, rows_fn=lambda: [],
                                  weth_px=WETH, verbose=False)
            assert st["seen"] == 0 and st["opened"] == 0
            st = feed_from_ledger(fresh_ts_now + 180, quote_buy_fn=buy_ok,
                                  rows_fn=lambda: [{"token": "x", "alert_ts": "garbage"}], weth_px=WETH, verbose=False)
            assert st["invalid"] == 1 and st["opened"] == 0
            print("  L14 ok: first run set the watermark and opened nothing; late row refused with lag %.0f s; "
                  "fresh row opened once (entry_lag_s=30); watermark blocks re-opening; [] / garbage rows do not raise"
                  % m1["entry_lag_s"])

            # L15 a deferred buy stays in feed.pending and opens on a later tick
            calls = {"n": 0}

            def buy_defer_first(token, usd, now_s, *, weth_px=None):
                calls["n"] += 1
                return _skel("buy", token, "deferred", now_s) if calls["n"] == 1 else buy_ok(token, usd, now_s, weth_px=weth_px)
            tp = fresh_ts_now + 300
            st = feed_from_ledger(tp, quote_buy_fn=buy_defer_first, rows_fn=lambda: [row("0x" + "4" * 40, 4, ats=tp - 10)],
                                  weth_px=WETH, decimals_fn=lambda t: 18, verbose=False)
            fs = _load(FEED_STATE_PATH, {})
            assert st["pending"] == 1 and len(fs["pending"]) == 1 and fs["pending"][0]["event_seq"] == 4
            assert _pos_key("0x" + "4" * 40, 4) not in book()
            st = feed_from_ledger(tp + 60, quote_buy_fn=buy_defer_first, rows_fn=lambda: [],
                                  weth_px=WETH, decimals_fn=lambda t: 18, verbose=False)
            assert st["opened"] == 1 and st["pending"] == 0 and _load(FEED_STATE_PATH, {})["pending"] == []
            assert book()[_pos_key("0x" + "4" * 40, 4)]["entry_lag_s"] == 70.0
            print("  L15 ok: deferred buy parked in feed.pending, opened on the next tick (lag stamped 70 s)")

            # L16 a B row is refused 'book_full' at the cap while an A row is admitted
            config.LIVEBOOK_MAX_OPEN = len([p for p in book().values() if not p.get("done")])
            tb = tp + 600
            rows_ = [row("0x" + "5" * 40, 5, tier="B", kind="first_sighting", ats=tb - 10),
                     row("0x" + "6" * 40, 6, tier="A", kind="promotion", ats=tb - 10)]
            st = feed_from_ledger(tb, quote_buy_fn=buy_ok, rows_fn=lambda: rows_, weth_px=WETH,
                                  decimals_fn=lambda t: 18, verbose=False)
            assert st["opened"] == 1 and st["missed"] == 1
            missed = [json.loads(ln) for ln in open(MISSED_PATH) if ln.strip()]
            assert missed[-1]["event_seq"] == 5 and missed[-1]["reason"] == "book_full"
            assert _pos_key("0x" + "6" * 40, 6) in book() and _pos_key("0x" + "5" * 40, 5) not in book()
            config.LIVEBOOK_MAX_OPEN = saved_max_open
            print("  L16 ok: B row refused 'book_full' at LIVEBOOK_MAX_OPEN; A row admitted")

            # L17 fills are flushed only AFTER the atomic book save
            reset(d)
            T11 = "0x" + "0" * 37 + "e11"
            opn(row(T11, 21))
            script[T11] = {"status": "ok", "mult": 1.0}
            tk(t0 + 60)
            script[T11] = {"status": "ok", "mult": 10.0}
            nf, nt = len(fills()), len(ticks())
            orig_save = g["_save_atomic"]

            def boom(obj, path):
                raise OSError("disk full (simulated)")
            g["_save_atomic"] = boom
            raised = False
            try:
                tk(t0 + 120)
            except OSError:
                raised = True
            finally:
                g["_save_atomic"] = orig_save
            assert raised and len(fills()) == nf and len(ticks()) == nt
            assert book()[_pos_key(T11, 21)]["n_ticks"] == 1        # the on-disk book is the pre-tick one
            tk(t0 + 120)                                             # the retry produces the fills once
            assert len(fills()) > nf
            print("  L17 ok: a failing book save flushed no fill and no tick line; the retry filled once")

            # L18 random_exit fires at its sha256-derived hold (recomputed independently here)
            import numpy as _np
            T12 = "0x" + "0" * 37 + "f12"
            opn(row(T12, 22))
            key = "%s:%d" % (T12, 22)
            hold = float(_np.random.default_rng(
                config.SEED + int(hashlib.sha256(key.encode()).hexdigest()[:8], 16)
            ).uniform(0.0, config.LIVEBOOK_DECISION_HORIZON_S))
            assert abs(hold - random_exit_hold_s(book()[_pos_key(T12, 22)])) < 1e-12
            script[T12] = {"status": "ok", "mult": 1.2}
            tick_times = [t0 + 60, t0 + max(hold + 1.0, 60 + 54.0)]
            expected = [t for t in tick_times if t - t0 >= hold][0]
            for tt in tick_times:
                tk(tt)
                s = book()[_pos_key(T12, 22)]["policies"]["ctl_random_exit"]
                if tt < expected:
                    assert not s["closed"]
            assert s["closed"] and s["close_reason"] == "random_exit" and s["closed_ts"] == expected
            print("  L18 ok: ctl_random_exit closed at the first tick past its sha256 hold (%.0f s)" % hold)

            # L19 a closing tick gap above LIVEBOOK_MAX_SCORABLE_GAP_S marks the market exit gapped
            T13 = "0x" + "0" * 36 + "1a13"
            opn(row(T13, 23))
            script[T13] = {"status": "ok", "mult": 1.0}
            tk(t0 + 60)
            script[T13] = {"status": "ok", "mult": 0.3}
            tk(t0 + 60 + 3600)                   # the Mac slept for an hour
            p = book()[_pos_key(T13, 23)]
            s = p["policies"]["stop_50"]
            assert s["close_reason"] == "stop" and s["close_gap_s"] == 3600.0 and s["gapped"] is True
            assert p["max_gap_s"] == 3600.0 and not p["policies"]["hold_to_end"]["closed"]
            # ...and a later 60 s-gap market exit is still gapped (the gap happened inside the horizon)
            script[T13] = {"status": "ok", "mult": 0.25}
            tk(t0 + 60 + 3600 + 60)
            p = book()[_pos_key(T13, 23)]
            assert p["policies"]["sell_1h"]["close_reason"] == "time_exit" and p["policies"]["sell_1h"]["gapped"] is True
            # ...while a limit rung after a gap is a limit fill, not a decision: never gapped
            script[T13] = {"status": "ok", "mult": 1.0}
            tk(t0 + MAX_TRACK_S + 3800)
            p = book()[_pos_key(T13, 23)]
            assert p["done"] and p["policies"]["hold_to_end"]["gapped"] is False
            d_ = live_stats_dict(t0)
            assert d_["per_policy"]["stop_50"]["n_gapped"] >= 1 and d_["n_gapped"] >= 1
            assert d_["per_policy"]["hold_to_end"]["n"] >= 1
            print("  L19 ok: stop after a 3600 s gap → gapped (NaN for the scorer); hold_to_end's terminal mark is not")
            json.dumps(live_stats_dict(t0), allow_nan=False)

            # L20 the band under test: a B row the sidecar says band_x selected is admitted at the
            # cap; verdict 0 is book_full; None makes the rule inert; the stamp is always recorded
            reset(d)
            config.LIVEBOOK_FEED_SOURCE = "worktree"
            config.BAND_VERDICTS_PATH = os.path.join(d, "band_verdicts.csv")
            config.LIVEBOOK_BAND_UNDER_TEST = "band_x"
            tu = t0 + 20_000

            def side(lines):
                with open(config.BAND_VERDICTS_PATH, "w", newline="") as fh:
                    fh.write("event_seq,token,alert_ts,band,verdict\n" + "".join("%s\n" % ln for ln in lines))

            def feed(rows_, ts_):
                return feed_from_ledger(ts_, quote_buy_fn=buy_ok, rows_fn=lambda: rows_, weth_px=WETH,
                                        decimals_fn=lambda t: 18, verbose=False)
            side([])
            feed([], tu)                                                   # the watermark
            feed([row("0x" + "7" * 40, 71, ats=tu + 10)], tu + 20)         # one A row; then the cap sits here
            config.LIVEBOOK_MAX_OPEN = 1
            TU = "0x" + "8" * 40
            side(["81,%s,%r,band_x,1" % (TU, tu + 30), "81,%s,%r,band_a_strict,0" % (TU, tu + 30)])
            st = feed([row(TU, 81, tier="B", kind="first_sighting", ats=tu + 30)], tu + 40)
            p = book()[_pos_key(TU, 81)]
            assert st["opened"] == 1 and p["sidecar_true"] == ["band_x"], (st, p["sidecar_true"])
            assert [f for f in fills() if f["side"] == "buy" and f["token"] == TU][0]["note"].count("sidecar_true band_x") == 1
            TV = "0x" + "9" * 40
            side(["82,%s,%r,band_x,0" % (TV, tu + 50)])
            st = feed([row(TV, 82, tier="B", kind="first_sighting", ats=tu + 50)], tu + 60)
            missed = [json.loads(ln) for ln in open(MISSED_PATH) if ln.strip()]
            assert st["missed"] == 1 and missed[-1]["reason"] == "book_full"
            config.LIVEBOOK_BAND_UNDER_TEST = None
            TW = "0x" + "0" * 39 + "a"
            side(["83,%s,%r,band_x,1" % (TW, tu + 70)])
            st = feed([row(TW, 83, tier="B", kind="first_sighting", ats=tu + 70)], tu + 80)
            missed = [json.loads(ln) for ln in open(MISSED_PATH) if ln.strip()]
            assert st["missed"] == 1 and missed[-1]["reason"] == "book_full"
            d_ = live_stats_dict(tu + 90)
            assert d_["sidecar_coverage"] == {"n_stamped": 1, "n_unstamped": 1} and d_["band_under_test"] is None
            config.LIVEBOOK_MAX_OPEN = saved_max_open
            print("  L20 ok: band_x,1 in the sidecar admitted a B row at the cap (stamped ['band_x']); verdict 0 → book_full; "
                  "None → inert")
            scorecard(t0)
    finally:
        for k, v in saved.items():
            g[k] = v
        config.LIVEBOOK_MAX_OPEN = saved_max_open
        (config.LIVEBOOK_BAND_UNDER_TEST, config.LIVEBOOK_BAND_UNDER_TEST_MAX_OPEN,
         config.LIVEBOOK_FEED_SOURCE, config.BAND_VERDICTS_PATH) = saved_cfg
    print("livebook smoke test OK (offline, temp dir)")


if __name__ == "__main__":
    if "--tick" in sys.argv or "--scorecard" in sys.argv:
        sys.exit(main())
    if "--live" in sys.argv:
        sys.exit(_live())
    _smoke()
    print()
    sys.exit(main([]))
