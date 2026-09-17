"""
paper_exec.py — the A book: paper fills at REAL quotes for every ALERTED entry and every exit
signal the ledger fires. Runs in the cloud (run.py); its two state files are committed back with
data/ so the book survives the runner.

What it measures is the one thing the honest ledger cannot: EXECUTION COST. The ledger's forward
returns are frictionless Dexscreener mid-prices. Real memecoin fills are not — a $10 round trip
on MIZUKARA's ~2.3 WETH V2 pool measured ~0.6% fees + ~0.3% impact (quotes.py, 2026-09-12), and
on this L2 every swap also pays a FIXED gas term (config.PAPER_GAS_USD_PER_SWAP, KyberSwap's
$0.067-0.075 at 289k gas) that at $10 positions is ~1.4% of a round trip on its own. The
scorecard here is therefore the go/no-go for any future automation: if THIS number is not
repeatedly positive, an execution layer would just be automating losses faster.

  entry alert  → quote WETH→token for the plan's size, record the fill, freeze the plan
  tp signal    → quote token→WETH for the ladder fraction OF THE ORIGINAL size, record proceeds
  stop / trail / time_exit → quote token→WETH for everything left; ABSENT = $0 proceeds, honest

Ported from solana_screener/paper_exec.py (accounting kept: raw token units end-to-end so P&L
is pure USD cash flow and decimals never enter the math; tp fractions of the original size so
the moonbag survives a full ladder; a dead coin at a protective exit is a $0 fill, never
imagined away) with the changes the plan's incident list demands:

  • EVM quotes via quotes.py, and its THREE-WAY status is branched on everywhere: 'ok' = a sized
    fill at a named source; 'absent' = dead for execution ($0 is the truth); 'deferred' = unknown
    this run — retried by retry_pending(), NEVER filled. On solana a rate limit read as "no
    route" fabricated 472 of 1,400 dead-token rows in a sibling project and 16 of 51 livebook
    "winners" were quote artifacts; a deferred quote here is queued, not decided.
  • Positions keyed (token, event_seq): a promotion opens a NEW position even when the token
    already has one (plan table "Livebook identity"). The ledger's B control never enters the
    book — an exit event for an unknown key is simply ignored.
  • The exit plan is FROZEN per position at open (champion_at_open + the full plan dict) and
    every exit is validated against THAT plan, never against the current champion: a champion
    switch cannot retroactively change what an open position does.
  • State writes are atomic (tmp + os.replace, allow_nan=False after a NaN→None pass). solana
    rewrote positions.json in place — a crash between truncate and write lost the whole book
    (plan bug (c)). Every JSON write here goes through _save_state; the CSV is append-only.
  • A fixed gas term per swap: usd_flow is NET of gas — -(usd + gas) on a buy, (usd - gas) on
    a sell, and 0.0 with NO gas on a $0 dead-route sell (you would not send that transaction).
  • No S book (that was a solana-local experiment).

Reproducibility contract: now_s is always passed IN by the caller (never wall-clock in here);
quotes are live I/O by nature and are recorded once, never recomputed. Nothing here raises on
bad data — one bad token or one unreadable file never kills a cloud run. NO KEYS, NO FUNDS, NO
TRANSACTIONS — quotes only. Not financial advice.
"""
from __future__ import annotations

import csv
import json
import math
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config    # noqa: E402
import quotes    # noqa: E402

COLUMNS = ["ts", "token", "event_seq", "symbol", "tier", "side", "plan_name", "usd_flow",
           "tokens_raw_delta", "px_usd", "weth_px_usd", "impact_pct", "gas_usd", "quote_source",
           "quote_status", "gap_s", "note"]
PROTECTIVE_KINDS = ("stop", "trail", "time_exit")   # sell the whole remainder; absent ⇒ $0 close
EXIT_KINDS = ("tp",) + PROTECTIVE_KINDS
FRAC_SCALE = 10 ** 6            # ladder fractions applied in integer math (1e24 * 0.5 is not
                                # exact in a float; 1e24 * 500000 // 1000000 is)
SUMMARY_SENTENCE = ("Real automated trading is justified ONLY if this number (not the "
                    "frictionless ledger's) is repeatedly positive. The gap between the two IS "
                    "your execution cost.")


# ── small helpers ────────────────────────────────────────────────────────────────
def _size_usd() -> float:
    return float(config.STACK_USD) * float(config.POSITION_PCT)


def _gas() -> float:
    return float(config.PAPER_GAS_USD_PER_SWAP)


def _key(token, event_seq) -> str:
    try:
        seq = int(event_seq)
    except (TypeError, ValueError):
        seq = event_seq
    return "%s:%s" % (str(token).lower(), seq)


def _f6(x):
    """Floats rounded to 6 significant digits on the way into the CSV (plan "Repo growth");
    ints (raw token amounts) are never touched."""
    if isinstance(x, bool) or x is None:
        return x
    if isinstance(x, int):
        return x
    try:
        v = float(x)
    except (TypeError, ValueError):
        return x
    if math.isnan(v) or math.isinf(v):
        return None
    return float("%.6g" % v)


def _clean(obj):
    """NaN/inf → None recursively so allow_nan=False can never raise on a state write."""
    if isinstance(obj, float):
        return None if (math.isnan(obj) or math.isinf(obj)) else obj
    if isinstance(obj, dict):
        return {str(k): _clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_clean(v) for v in obj]
    return obj


def _empty_state() -> dict:
    return {"positions": {}, "pending_buys": []}


def _load_state(path: str | None = None) -> dict:
    """Tolerant of a missing file (fresh checkout). A corrupt file is copied aside as
    <path>.corrupt and an empty book is returned with a printed warning — never a raise, and
    never a silent overwrite of the only copy."""
    path = path or config.PAPER_POSITIONS_PATH
    st = _empty_state()
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return st
    try:
        raw = json.load(open(path))
    except Exception as exc:
        print("  [paper] %s unreadable (%s); copied to %s.corrupt, starting an empty book"
              % (path, exc, path))
        try:
            shutil.copyfile(path, path + ".corrupt")
        except OSError:
            pass
        return st
    if not isinstance(raw, dict):
        return st
    pos = raw.get("positions")
    if isinstance(pos, dict):
        for k, p in pos.items():
            if isinstance(p, dict):
                p.setdefault("pending_exits", [])
                p.setdefault("realized_usd", 0.0)
                p.setdefault("gas_usd", 0.0)
                p.setdefault("closed", False)
                st["positions"][k] = p
    pb = raw.get("pending_buys")
    if isinstance(pb, list):
        st["pending_buys"] = [b for b in pb if isinstance(b, dict)]
    return st


def _save_state(state: dict, path: str | None = None) -> bool:
    """tmp + os.replace: the live file is either the old book or the new one, never a
    truncated file. Returns False (and prints) instead of raising when the write fails; the
    tmp file is removed so a failed write leaves nothing behind but the intact original."""
    path = path or config.PAPER_POSITIONS_PATH
    tmp = "%s.%d.tmp" % (path, os.getpid())
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(tmp, "w") as f:
            json.dump(_clean(state), f, indent=1, sort_keys=True, allow_nan=False)
        os.replace(tmp, path)
        return True
    except Exception as exc:
        print("  [paper] STATE WRITE FAILED for %s (%s); the book on disk is unchanged" % (path, exc))
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        return False


def _append_rows(rows: list, path: str | None = None) -> None:
    """CSV append via the csv module; header written on the first append."""
    if not rows:
        return
    path = path or config.PAPER_LEDGER_PATH
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        need_header = not os.path.exists(path) or os.path.getsize(path) == 0
        with open(path, "a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=COLUMNS, extrasaction="ignore")
            if need_header:
                w.writeheader()
            for r in rows:
                out = {}
                for c in COLUMNS:
                    v = _f6(r.get(c))
                    out[c] = "" if v is None else v
                w.writerow(out)
    except Exception as exc:
        print("  [paper] LEDGER APPEND FAILED for %s (%s)" % (path, exc))


def _read_rows(path: str | None = None) -> list:
    path = path or config.PAPER_LEDGER_PATH
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return []
    try:
        with open(path, newline="") as f:
            return list(csv.DictReader(f))
    except Exception:
        return []


def _row(ts, token, event_seq, symbol, tier, side, plan_name, *, usd_flow=0.0,
         tokens_raw_delta=0, px_usd=None, weth_px_usd=None, impact_pct=None, gas_usd=0.0,
         quote_source=None, quote_status=None, gap_s=None, note="") -> dict:
    return {"ts": ts, "token": token, "event_seq": event_seq, "symbol": symbol, "tier": tier,
            "side": side, "plan_name": plan_name, "usd_flow": usd_flow,
            "tokens_raw_delta": int(tokens_raw_delta), "px_usd": px_usd,
            "weth_px_usd": weth_px_usd, "impact_pct": impact_pct, "gas_usd": gas_usd,
            "quote_source": quote_source or "none", "quote_status": quote_status or "?",
            "gap_s": gap_s, "note": note}


def _q_usd(q: dict):
    try:
        u = q.get("usd")
        return None if u is None else float(u)
    except (TypeError, ValueError):
        return None


def _frozen_plan(plan) -> dict:
    """The plan dict as stored on a position: {name, ladder:[[m,f]], stop, trail, trail_arm,
    flow, max_hold_s}. Garbage becomes the default champion plan (never a crash in the alert
    path). `trail_arm` and `flow` are carried for the AUDIT TRAIL — what the alert promised — not
    because this book executes them: the arm is applied upstream in ledger.update_forward, which
    decides whether a trail event is emitted at all, and `flow_exit` is not an EXIT_KIND here (no
    5-minute feed runs beside the scan, so the paper book alone scores that leg)."""
    if not isinstance(plan, dict) or not plan.get("name"):
        return {"name": config.IMPROVE_DEFAULT_EXIT_CHAMPION,
                "ladder": [[float(m), float(f)] for m, f in config.TP_LADDER],
                "stop": float(config.HARD_STOP_PCT), "trail": None, "trail_arm": None,
                "flow": None, "max_hold_s": None}
    ladder = []
    for rung in (plan.get("ladder") or []):
        try:
            m, f = rung
            ladder.append([float(m), float(f)])
        except (TypeError, ValueError):
            continue
    flow = plan.get("flow")
    return {"name": str(plan.get("name")), "ladder": ladder, "stop": plan.get("stop"),
            "trail": plan.get("trail"), "trail_arm": plan.get("trail_arm"),
            "flow": dict(flow) if isinstance(flow, dict) else None,
            "max_hold_s": plan.get("max_hold_s")}


# ── the accounting primitives (shared by the alert path and the retry path) ──────
def _fill_buy(state: dict, spec: dict, q: dict, now_s: float, *, note: str = "") -> dict:
    """Open the position `spec` describes from an 'ok' buy quote. Mutates state; returns the
    buy row. `spec` = {token, event_seq, symbol, tier, alert_ts, plan}."""
    usd = _size_usd()
    gas = _gas()
    tokens_raw = int(q.get("amount_out_raw") or 0)
    plan = _frozen_plan(spec.get("plan"))
    key = _key(spec["token"], spec["event_seq"])
    p = {"token": str(spec["token"]), "event_seq": spec["event_seq"], "symbol": spec.get("symbol"),
         "tier": spec.get("tier"), "opened_ts": now_s, "alert_ts": spec.get("alert_ts"),
         "cost_usd": usd, "tokens_raw": tokens_raw, "tokens_remaining_raw": tokens_raw,
         "entry_px_usd": (usd / tokens_raw) if tokens_raw > 0 else None,
         "weth_px_usd_open": q.get("weth_px_usd"), "impact_pct_open": q.get("impact_pct"),
         "quote_source_open": q.get("source"), "realized_usd": 0.0, "gas_usd": gas,
         "closed": False, "closed_ts": None, "close_reason": None,
         "champion_at_open": plan["name"], "plan": plan, "pending_exits": []}
    state["positions"][key] = p
    text = quotes.describe(q)
    if note:
        text = note + " — " + text
    try:
        lag = float(now_s) - float(spec.get("alert_ts"))
    except (TypeError, ValueError):
        lag = None
    return _row(now_s, p["token"], spec["event_seq"], p["symbol"], p["tier"], "buy", plan["name"],
                usd_flow=-(usd + gas), tokens_raw_delta=tokens_raw, px_usd=p["entry_px_usd"],
                weth_px_usd=q.get("weth_px_usd"), impact_pct=q.get("impact_pct"), gas_usd=gas,
                quote_source=q.get("source"), quote_status=q.get("status"), gap_s=lag, note=text)


def _buy_failed_row(spec: dict, q: dict, now_s: float, note: str) -> dict:
    plan = _frozen_plan(spec.get("plan"))
    return _row(now_s, str(spec["token"]), spec["event_seq"], spec.get("symbol"), spec.get("tier"),
                "buy_failed", plan["name"], quote_source=q.get("source"),
                quote_status=q.get("status"), note=note + " — " + quotes.describe(q))


def _plan_allows(plan: dict, kind: str) -> bool:
    """An exit kind is executed only if the position's FROZEN plan contains that leg."""
    if kind == "tp":
        return bool(plan.get("ladder"))
    if kind == "stop":
        return bool(plan.get("stop"))
    if kind == "trail":
        return bool(plan.get("trail"))
    if kind == "time_exit":
        return plan.get("max_hold_s") is not None
    return False


def _size_exit(p: dict, event: dict) -> tuple:
    """(sell_raw, side). tp: the ladder fraction OF THE ORIGINAL size, each rung matched against
    the frozen ladder by multiple (the frozen fraction is used; a rung the frozen plan does not
    have is skipped). Protective kinds: everything left."""
    kind = event.get("kind")
    tokens_raw = int(p.get("tokens_raw") or 0)
    remaining = int(p.get("tokens_remaining_raw") or 0)
    if kind == "tp":
        frozen = [(float(m), float(f)) for m, f in (p.get("plan") or {}).get("ladder") or []]
        frac_scaled, mults = 0, []
        for rung in (event.get("levels") or []):
            try:
                m = float(rung[0])
            except (TypeError, ValueError, IndexError):
                continue
            match = [f for fm, f in frozen if abs(fm - m) < 1e-9]
            if not match:
                continue
            frac_scaled += int(round(match[0] * FRAC_SCALE))
            mults.append(m)
        if not mults:
            return 0, "tp"
        sell_raw = min(tokens_raw * frac_scaled // FRAC_SCALE, remaining)
        return sell_raw, "tp_" + "+".join("%gx" % m for m in mults)
    if kind in PROTECTIVE_KINDS:
        return remaining, kind
    return 0, str(kind)


def _apply_exit(state: dict, key: str, p: dict, event: dict, q: dict, sell_raw: int,
                side: str, now_s: float, *, gap_s=None, note_prefix: str = "") -> tuple:
    """Apply an 'ok' or 'absent' sell quote to position p. Returns (outcome, row) with outcome
    in {'filled', 'failed'}. Deferred quotes never reach here."""
    kind = event.get("kind")
    gas = _gas()
    status = q.get("status")
    desc = quotes.describe(q)
    base = dict(ts=now_s, token=p["token"], event_seq=p["event_seq"], symbol=p.get("symbol"),
                tier=p.get("tier"), plan_name=(p.get("plan") or {}).get("name"))
    if status == "ok":
        usd = _q_usd(q) or 0.0
        row = _row(side=side, usd_flow=usd - gas, tokens_raw_delta=-sell_raw,
                   px_usd=(usd / sell_raw) if sell_raw > 0 else None,
                   weth_px_usd=q.get("weth_px_usd"), impact_pct=q.get("impact_pct"), gas_usd=gas,
                   quote_source=q.get("source"), quote_status="ok", gap_s=gap_s,
                   note=(note_prefix + desc), **base)
        p["tokens_remaining_raw"] = int(p["tokens_remaining_raw"]) - sell_raw
        p["realized_usd"] = float(p.get("realized_usd") or 0.0) + usd - gas
        p["gas_usd"] = float(p.get("gas_usd") or 0.0) + gas
        if kind in PROTECTIVE_KINDS or p["tokens_remaining_raw"] <= 0:
            p["closed"], p["closed_ts"] = True, now_s
            p["close_reason"] = kind if kind in PROTECTIVE_KINDS else "ladder_complete"
            p["pending_exits"] = []
        return "filled", row
    # absent
    if kind in PROTECTIVE_KINDS:
        row = _row(side="no_route", usd_flow=0.0, tokens_raw_delta=-sell_raw, px_usd=0.0,
                   weth_px_usd=q.get("weth_px_usd"), gas_usd=0.0, quote_source=q.get("source"),
                   quote_status="absent", gap_s=gap_s,
                   note=note_prefix + "NO ROUTE (on-chain first: " + desc + ") — dead coin, proceeds $0",
                   **base)
        p["tokens_remaining_raw"] = int(p["tokens_remaining_raw"]) - sell_raw
        p["closed"], p["closed_ts"], p["close_reason"] = True, now_s, "no_route"
        p["pending_exits"] = []
        return "filled", row
    # a token the ledger prices at 2x but nobody can route is suspicious, not dead
    row = _row(side="tp_failed", usd_flow=0.0, tokens_raw_delta=0, gas_usd=0.0,
               weth_px_usd=q.get("weth_px_usd"), quote_source=q.get("source"),
               quote_status="absent", gap_s=gap_s,
               note=note_prefix + "no route at TP (%s) — tokens kept: %s" % (side, desc), **base)
    return "failed", row


def _queue_exit(p: dict, event: dict, now_s: float) -> None:
    """Append the deferred exit, or bump tries on an identical one already queued."""
    pend = p.setdefault("pending_exits", [])
    for pe in pend:
        ev = pe.get("event") or {}
        if ev.get("kind") == event.get("kind") and _clean(ev.get("levels")) == _clean(event.get("levels")):
            pe["tries"] = int(pe.get("tries") or 0) + 1
            return
    pend.append({"event": _clean(dict(event)), "first_seen_ts": now_s, "tries": 0})


# ── public API ───────────────────────────────────────────────────────────────────
def open_position(token: str, symbol: str, tier: str, event_seq, now_s: float, *, plan: dict,
                  alert_ts: float, quote_buy_fn=quotes.quote_buy, weth_px=None,
                  positions_path: str | None = None, ledger_path: str | None = None) -> dict | None:
    """Simulate the entry the alert recommends (POSITION_PCT of STACK_USD, via WETH).
    Idempotent per (token, event_seq): a re-run can never double-buy, and a key already in
    pending_buys is left to retry_pending (not re-quoted here). Returns the buy row on an 'ok'
    quote; None for buy_failed ('absent'), buy_deferred ('deferred') and no-ops."""
    state = _load_state(positions_path)
    key = _key(token, event_seq)
    if key in state["positions"]:
        return None
    if any(_key(b.get("token"), b.get("event_seq")) == key for b in state["pending_buys"]):
        return None
    spec = {"token": str(token), "event_seq": event_seq, "symbol": symbol, "tier": tier,
            "alert_ts": alert_ts, "plan": _frozen_plan(plan)}
    usd = _size_usd()
    try:
        q = quote_buy_fn(token, usd, now_s, weth_px=weth_px)
    except Exception as exc:
        q = quotes._new("buy", str(token), 0, now_s, weth_px)
        quotes._note(q, "quote_buy raised: %s" % str(exc)[:120])
    if not isinstance(q, dict):
        q = quotes._new("buy", str(token), 0, now_s, weth_px)
    status = q.get("status")
    if status == "ok" and int(q.get("amount_out_raw") or 0) <= 0:
        status = "absent"
        quotes._note(q, "ok quote with zero tokens out")
    if status == "ok" and _q_usd(q) is None:
        status = "deferred"                                   # a fill nobody can value
        quotes._note(q, "ok quote with no USD valuation; treated as deferred")

    if status == "absent":
        rows = [_buy_failed_row(spec, q, now_s, "no route — position not opened")]
        _append_rows(rows, ledger_path)
        return None
    if status != "ok":                                        # deferred (or garbage)
        state["pending_buys"].append(dict(spec, first_seen_ts=now_s, tries=0))
        if not _save_state(state, positions_path):
            return None
        _append_rows([_row(now_s, spec["token"], event_seq, symbol, tier, "buy_deferred",
                           spec["plan"]["name"], quote_source=q.get("source"),
                           quote_status=q.get("status"),
                           note="quote deferred — queued for retry_pending: " + quotes.describe(q))],
                     ledger_path)
        return None
    row = _fill_buy(state, spec, q, now_s)
    if not _save_state(state, positions_path):
        return None
    _append_rows([row], ledger_path)
    return row


def execute_exit(event: dict, now_s: float, *, quote_sell_fn=quotes.quote_sell, weth_px=None,
                 positions_path: str | None = None, ledger_path: str | None = None) -> dict | None:
    """Simulate the sell an exit signal recommends. `event` comes straight from
    ledger.update_forward: {kind: tp|stop|trail|time_exit, token, symbol, event_seq, price,
    ret, mult, levels?, due_ts?, gap_s}. An unknown or closed key (the B control, a position
    never opened) is ignored. The exit is validated against the position's FROZEN plan — a
    kind the stored plan has no leg for is ignored, whatever the current champion says.
    Returns the fill row; None for tp_failed, deferred (queued) and no-ops."""
    if not isinstance(event, dict):
        return None
    state = _load_state(positions_path)
    key = _key(event.get("token"), event.get("event_seq"))
    p = state["positions"].get(key)
    if p is None or p.get("closed"):
        return None
    kind = event.get("kind")
    if kind not in EXIT_KINDS or not _plan_allows(p.get("plan") or {}, kind):
        return None
    sell_raw, side = _size_exit(p, event)
    if sell_raw <= 0:
        return None
    try:
        q = quote_sell_fn(p["token"], sell_raw, now_s, weth_px=weth_px)
    except Exception as exc:
        q = quotes._new("sell", p["token"], sell_raw, now_s, weth_px)
        quotes._note(q, "quote_sell raised: %s" % str(exc)[:120])
    if not isinstance(q, dict):
        q = quotes._new("sell", p["token"], sell_raw, now_s, weth_px)
    status = q.get("status")
    if status == "ok" and _q_usd(q) is None:
        status = "deferred"
    if status not in ("ok", "absent"):
        _queue_exit(p, event, now_s)
        _save_state(state, positions_path)
        return None
    outcome, row = _apply_exit(state, key, p, event, q, sell_raw, side, now_s,
                               gap_s=event.get("gap_s"))
    if not _save_state(state, positions_path):
        return None
    _append_rows([row], ledger_path)
    return row if outcome == "filled" else None


def retry_pending(now_s: float, *, quote_buy_fn=quotes.quote_buy, quote_sell_fn=quotes.quote_sell,
                  weth_px=None, positions_path: str | None = None,
                  ledger_path: str | None = None) -> dict:
    """Re-quote every pending buy and pending exit once. 'ok' fills exactly as the alert path
    would (a pending buy records entry_lag_s in its note); 'absent' resolves as the alert path
    would; 'deferred' bumps tries, and after config.PAPER_PENDING_MAX_RUNS tries the item is
    dropped with a buy_failed / <kind>_failed row (tokens kept for an exit)."""
    state = _load_state(positions_path)
    counts = {"buys_filled": 0, "buys_failed": 0, "exits_filled": 0, "exits_failed": 0,
              "still_pending": 0}
    rows: list = []
    max_runs = int(config.PAPER_PENDING_MAX_RUNS)
    usd = _size_usd()

    keep_buys = []
    for pb in state["pending_buys"]:
        key = _key(pb.get("token"), pb.get("event_seq"))
        if key in state["positions"]:
            continue                                          # filled elsewhere; drop
        try:
            q = quote_buy_fn(pb["token"], usd, now_s, weth_px=weth_px)
        except Exception as exc:
            q = quotes._new("buy", str(pb.get("token")), 0, now_s, weth_px)
            quotes._note(q, "quote_buy raised: %s" % str(exc)[:120])
        if not isinstance(q, dict):
            q = quotes._new("buy", str(pb.get("token")), 0, now_s, weth_px)
        status = q.get("status")
        if status == "ok" and int(q.get("amount_out_raw") or 0) <= 0:
            status = "absent"
        if status == "ok" and _q_usd(q) is None:
            status = "deferred"
        tries = int(pb.get("tries") or 0)
        if status == "ok":
            try:
                lag = float(now_s) - float(pb.get("alert_ts"))
            except (TypeError, ValueError):
                lag = float("nan")
            rows.append(_fill_buy(state, pb, q, now_s,
                                  note="pending buy filled on retry %d, entry_lag_s=%.0f" % (tries + 1, lag)))
            counts["buys_filled"] += 1
        elif status == "absent":
            rows.append(_buy_failed_row(pb, q, now_s, "no route on retry %d — position not opened" % (tries + 1)))
            counts["buys_failed"] += 1
        else:
            tries += 1
            pb["tries"] = tries
            if tries >= max_runs:
                rows.append(_buy_failed_row(pb, q, now_s, "quote deferred %d run(s) — given up" % tries))
                counts["buys_failed"] += 1
            else:
                keep_buys.append(pb)
                counts["still_pending"] += 1
    state["pending_buys"] = keep_buys

    for key, p in state["positions"].items():
        pend = p.get("pending_exits") or []
        if not pend:
            continue
        if p.get("closed"):
            p["pending_exits"] = []
            continue
        keep = []
        for pe in pend:
            if p.get("closed"):
                break                                         # an earlier retry closed it
            event = pe.get("event") or {}
            kind = event.get("kind")
            tries = int(pe.get("tries") or 0)
            if kind not in EXIT_KINDS or not _plan_allows(p.get("plan") or {}, kind):
                continue
            sell_raw, side = _size_exit(p, event)
            if sell_raw <= 0:
                continue
            try:
                q = quote_sell_fn(p["token"], sell_raw, now_s, weth_px=weth_px)
            except Exception as exc:
                q = quotes._new("sell", p["token"], sell_raw, now_s, weth_px)
                quotes._note(q, "quote_sell raised: %s" % str(exc)[:120])
            if not isinstance(q, dict):
                q = quotes._new("sell", p["token"], sell_raw, now_s, weth_px)
            status = q.get("status")
            if status == "ok" and _q_usd(q) is None:
                status = "deferred"
            try:
                waited = float(now_s) - float(pe.get("first_seen_ts"))
            except (TypeError, ValueError):
                waited = 0.0
            try:
                gap = float(event.get("gap_s")) + waited
            except (TypeError, ValueError):
                gap = waited
            if status in ("ok", "absent"):
                outcome, row = _apply_exit(state, key, p, event, q, sell_raw, side, now_s, gap_s=gap,
                                           note_prefix="retry %d after %.0fs deferred — " % (tries + 1, waited))
                rows.append(row)
                counts["exits_filled" if outcome == "filled" else "exits_failed"] += 1
                continue
            tries += 1
            pe["tries"] = tries
            if tries >= max_runs:
                rows.append(_row(now_s, p["token"], p["event_seq"], p.get("symbol"), p.get("tier"),
                                 "%s_failed" % kind, (p.get("plan") or {}).get("name"),
                                 quote_source=q.get("source"), quote_status=q.get("status"), gap_s=gap,
                                 note="quote deferred %d run(s) — given up, tokens kept: %s"
                                      % (tries, quotes.describe(q))))
                counts["exits_failed"] += 1
            else:
                keep.append(pe)
                counts["still_pending"] += 1
        p["pending_exits"] = [] if p.get("closed") else keep

    if not _save_state(state, positions_path):
        return counts
    _append_rows(rows, ledger_path)
    return counts


def summary(*, quote_sell_many_fn=quotes.quote_sell_many, mark_open: bool = True, now_s=None,
            positions_path: str | None = None, ledger_path: str | None = None) -> dict:
    """The paper scorecard: realized cash + a live mark of open remainders (ONE quote_sell_many
    over the open book when mark_open and now_s are given) vs cost, per position and TOTAL, a
    per-plan_name breakdown, counts, and the mean entry impact. A remainder is marked at
    max(proceeds - gas, 0): a crumb worth less than its gas is worth $0, because you would not
    send that transaction. A deferred mark counts $0 in the total and is reported as unmarked.
    Prints, and returns the numbers."""
    state = _load_state(positions_path)
    pos = state["positions"]
    gas = _gas()
    out = {"n_positions": len(pos), "n_open": 0, "n_closed": 0,
           "n_pending_buys": len(state["pending_buys"]), "n_pending_exits": 0,
           "invested_usd": 0.0, "realized_usd": 0.0, "open_value_usd": 0.0, "gas_usd": 0.0,
           "net_usd": 0.0, "net_pct": None, "n_unmarked": 0, "mean_entry_impact_pct": None,
           "by_plan": {}, "positions": {}}
    print("paper execution (A book): %d position(s), %d pending buy(s)" % (len(pos), len(state["pending_buys"])))
    # mean entry impact from the buy rows
    imp = []
    for r in _read_rows(ledger_path):
        if r.get("side") == "buy":
            try:
                imp.append(float(r.get("impact_pct")))
            except (TypeError, ValueError):
                pass
    if imp:
        out["mean_entry_impact_pct"] = sum(imp) / len(imp)
    if not pos:
        print("  (no paper fills yet — a position opens at the next A-tier alert)")
        print("  " + SUMMARY_SENTENCE)
        return out

    # one batched mark over the open book, aggregated per token, pro-rated per position
    marks: dict = {}
    remaining_by_token: dict = {}
    for p in pos.values():
        if not p.get("closed") and int(p.get("tokens_remaining_raw") or 0) > 0:
            t = str(p["token"]).lower()
            remaining_by_token[t] = remaining_by_token.get(t, 0) + int(p["tokens_remaining_raw"])
    if mark_open and now_s is not None and remaining_by_token:
        try:
            marks = quote_sell_many_fn(list(remaining_by_token.items()), now_s) or {}
        except Exception as exc:
            print("  [paper] mark failed (%s); open remainders unmarked" % exc)
            marks = {}
        marks = {str(k).lower(): v for k, v in marks.items()}

    print("  %-2s%-12s %5s %7s %9s %9s %8s  %s" % ("", "symbol", "seq", "cost", "realized", "open val", "P&L", "status"))
    for key in sorted(pos, key=lambda k: float(pos[k].get("opened_ts") or 0)):
        p = pos[key]
        cost = float(p.get("cost_usd") or 0.0)
        real = float(p.get("realized_usd") or 0.0)
        rem = int(p.get("tokens_remaining_raw") or 0)
        cur, marked = 0.0, True
        if not p.get("closed") and rem > 0:
            out["n_open"] += 1
            q = marks.get(str(p["token"]).lower())
            if q is None or not isinstance(q, dict):
                marked = not (mark_open and now_s is not None)
            elif q.get("status") == "ok" and _q_usd(q) is not None:
                share = rem / float(remaining_by_token[str(p["token"]).lower()])
                cur = max(_q_usd(q) * share - gas, 0.0)
            elif q.get("status") == "absent":
                cur = 0.0
            else:
                marked = False
        else:
            out["n_closed"] += 1
        if not marked:
            out["n_unmarked"] += 1
        out["n_pending_exits"] += len(p.get("pending_exits") or [])
        pnl = real + cur - cost
        out["invested_usd"] += cost
        out["realized_usd"] += real
        out["open_value_usd"] += cur
        out["gas_usd"] += float(p.get("gas_usd") or 0.0)
        pname = (p.get("plan") or {}).get("name") or "?"
        bp = out["by_plan"].setdefault(pname, {"n": 0, "cost": 0.0, "realized": 0.0, "open": 0.0, "pnl": 0.0})
        bp["n"] += 1; bp["cost"] += cost; bp["realized"] += real; bp["open"] += cur; bp["pnl"] += pnl
        st = ("closed:" + str(p.get("close_reason"))) if p.get("closed") else "open"
        if p.get("pending_exits"):
            st += " (%d pending exit)" % len(p["pending_exits"])
        if not marked:
            st += " [unmarked]"
        out["positions"][key] = {"cost": cost, "realized": real, "open": cur, "pnl": pnl,
                                 "closed": bool(p.get("closed")), "marked": marked}
        print("  %-2s%-12s %5s %7.2f %9.2f %9.2f %+8.2f  %s" % (
            str(p.get("tier") or "")[:2], str(p.get("symbol"))[:12], str(p.get("event_seq"))[:5],
            cost, real, cur, pnl, st))
    inv, real, opn = out["invested_usd"], out["realized_usd"], out["open_value_usd"]
    out["net_usd"] = real + opn - inv
    out["net_pct"] = ((real + opn) / inv - 1.0) if inv > 0 else None
    print("  TOTAL invested $%.2f → realized $%.2f + open $%.2f = net %+.2f (%s); gas paid $%.2f"
          % (inv, real, opn, out["net_usd"],
             ("%+.1f%%" % (out["net_pct"] * 100)) if out["net_pct"] is not None else "n/a",
             out["gas_usd"]))
    for pname, bp in sorted(out["by_plan"].items()):
        print("    plan %-24s n=%-3d cost %7.2f realized %8.2f open %8.2f pnl %+8.2f"
              % (pname, bp["n"], bp["cost"], bp["realized"], bp["open"], bp["pnl"]))
    print("  open %d / closed %d / pending buys %d / pending exits %d%s" % (
        out["n_open"], out["n_closed"], out["n_pending_buys"], out["n_pending_exits"],
        (" / unmarked %d" % out["n_unmarked"]) if out["n_unmarked"] else ""))
    if out["mean_entry_impact_pct"] is not None:
        print("  mean entry impact %.2f%% over %d buy(s)" % (out["mean_entry_impact_pct"], len(imp)))
    print("  " + SUMMARY_SENTENCE)
    return out


# ── smoke test (OFFLINE by default; --live for one real round trip) ──────────────
if __name__ == "__main__":
    import tempfile
    from selfimprove import champion

    T0 = 1_700_000_000.0                                      # no wall-clock offline
    WPX = 2500.0
    TOK = "0x" + "a" * 40
    POOL = "0x" + "b" * 40
    USD = _size_usd()
    GAS = _gas()

    def _q(side, token, amount_in, now_s, status, out=None, source="router", impact=0.31):
        q = quotes._new(side, token, amount_in, now_s, WPX)
        if status == "ok":
            quotes._finish_ok(q, out, source, WPX, impact=impact)
        elif status == "absent":
            q["probes"]["pair"], q["probes"]["router"], q["probes"]["reserves"] = POOL, "zero", [0, 0]
            quotes._note(q, "drained V2 pool")
            quotes._finish_absent(q)
        else:
            q["probes"]["pair"] = "deferred"
        return q

    def make_buy(script):
        """script: list of statuses consumed in order (the last repeats)."""
        calls = {"n": 0}

        def f(token, usd, now_s, *, weth_px=None):
            st = script[min(calls["n"], len(script) - 1)]; calls["n"] += 1
            wei = int(usd / WPX * 10 ** 18)
            return _q("buy", token, wei, now_s, st, out=10 ** 24)
        f.calls = calls
        return f

    def make_sell(script):
        """script: list of (status, usd_out) consumed in order (the last repeats)."""
        calls = {"n": 0}

        def f(token, tokens_raw, now_s, *, weth_px=None):
            st, usd_out = script[min(calls["n"], len(script) - 1)]; calls["n"] += 1
            wei = int(usd_out / WPX * 10 ** 18) if usd_out else 0
            return _q("sell", token, tokens_raw, now_s, st, out=wei)
        f.calls = calls
        return f

    def rows(path):
        return _read_rows(path)

    with tempfile.TemporaryDirectory() as d:
        PP, LP = os.path.join(d, "paper_positions.json"), os.path.join(d, "paper_ledger.csv")
        kw = dict(positions_path=PP, ledger_path=LP)
        plan = champion.exit_plan("cfg_ladder_stop")
        assert plan["ladder"] and plan["stop"] and plan["max_hold_s"] is None

        # P1 buy opens once; re-open is a no-op
        r = open_position(TOK, "DEMO", "A", 7, T0, plan=plan, alert_ts=T0 - 240,
                          quote_buy_fn=make_buy(["ok"]), **kw)
        assert r and r["side"] == "buy" and r["tokens_raw_delta"] == 10 ** 24, r
        assert abs(r["usd_flow"] + (USD + GAS)) < 1e-9 and r["plan_name"] == "cfg_ladder_stop"
        assert abs(r["gap_s"] - 240) < 1e-9, "buy gap_s = entry lag"
        r2 = open_position(TOK, "DEMO", "A", 7, T0 + 1, plan=plan, alert_ts=T0 - 240,
                           quote_buy_fn=make_buy(["ok"]), **kw)
        st = _load_state(PP)
        assert r2 is None and len(st["positions"]) == 1 and len(rows(LP)) == 1
        p = st["positions"][_key(TOK, 7)]
        assert p["plan"]["name"] == "cfg_ladder_stop" and p["champion_at_open"] == "cfg_ladder_stop"
        assert p["tokens_remaining_raw"] == 10 ** 24 and abs(p["cost_usd"] - USD) < 1e-9
        assert abs(p["entry_px_usd"] - USD / 10 ** 24) < 1e-40 and p["gas_usd"] == GAS
        print("P1  buy opens once, re-open no-op                         ok")

        # P2 tp sells the ladder fraction of the ORIGINAL size; usd_flow = usd - gas
        ev = {"kind": "tp", "token": TOK, "symbol": "DEMO", "event_seq": 7, "price": 2e-23,
              "ret": 1.0, "mult": 2.0, "levels": [(2.0, 0.5)], "gap_s": 120.0}
        r = execute_exit(ev, T0 + 3600, quote_sell_fn=make_sell([("ok", 10.0)]), **kw)
        assert r and r["side"] == "tp_2x" and r["tokens_raw_delta"] == -5 * 10 ** 23, r
        assert abs(r["usd_flow"] - (10.0 - GAS)) < 1e-9 and r["gap_s"] == 120.0
        assert r["quote_source"] == "router" and r["quote_status"] == "ok"
        p = _load_state(PP)["positions"][_key(TOK, 7)]
        assert p["tokens_remaining_raw"] == 5 * 10 ** 23 and abs(p["realized_usd"] - (10.0 - GAS)) < 1e-9
        assert not p["closed"]
        ev2 = dict(ev, levels=[(5.0, 0.25), (10.0, 0.15)], mult=10.0)
        r = execute_exit(ev2, T0 + 7200, quote_sell_fn=make_sell([("ok", 30.0)]), **kw)
        assert r["side"] == "tp_5x+10x" and r["tokens_raw_delta"] == -4 * 10 ** 23, r
        p = _load_state(PP)["positions"][_key(TOK, 7)]
        assert p["tokens_remaining_raw"] == 10 ** 23 and not p["closed"], "moonbag survives the ladder"
        assert abs(p["realized_usd"] - (40.0 - 2 * GAS)) < 1e-9
        print("P2  tp = ladder fraction of ORIGINAL size, usd_flow = usd - gas  ok")

        # P3 stop with an 'absent' quote liquidates at $0, charges no gas, closes no_route
        ev3 = {"kind": "stop", "token": TOK, "symbol": "DEMO", "event_seq": 7, "price": 0.0,
               "ret": -1.0, "mult": 0.0, "gap_s": 300.0}
        r = execute_exit(ev3, T0 + 9000, quote_sell_fn=make_sell([("absent", 0)]), **kw)
        assert r and r["side"] == "no_route" and r["usd_flow"] == 0.0 and r["gas_usd"] == 0.0, r
        assert r["tokens_raw_delta"] == -10 ** 23 and r["quote_status"] == "absent"
        assert r["note"].startswith("NO ROUTE (on-chain first: ABSENT") and r["note"].endswith("dead coin, proceeds $0")
        p = _load_state(PP)["positions"][_key(TOK, 7)]
        assert p["closed"] and p["close_reason"] == "no_route" and p["tokens_remaining_raw"] == 0
        assert abs(p["realized_usd"] - (40.0 - 2 * GAS)) < 1e-9 and abs(p["gas_usd"] - 3 * GAS) < 1e-9
        assert execute_exit(ev3, T0 + 9001, quote_sell_fn=make_sell([("ok", 5.0)]), **kw) is None, "closed: ignored"
        print("P3  stop + absent ⇒ $0 no_route close, no gas             ok")

        # P4 stop with a 'deferred' quote does NOT fill and is queued; retry_pending fills it
        open_position(TOK, "DEMO", "A", 8, T0, plan=plan, alert_ts=T0, quote_buy_fn=make_buy(["ok"]), **kw)
        ev4 = dict(ev3, event_seq=8, gap_s=60.0)
        n_before = len(rows(LP))
        r = execute_exit(ev4, T0 + 100, quote_sell_fn=make_sell([("deferred", 0)]), **kw)
        p = _load_state(PP)["positions"][_key(TOK, 8)]
        assert r is None and len(p["pending_exits"]) == 1 and p["tokens_remaining_raw"] == 10 ** 24
        assert not p["closed"] and len(rows(LP)) == n_before, "a deferred exit writes no row"
        r = execute_exit(ev4, T0 + 101, quote_sell_fn=make_sell([("deferred", 0)]), **kw)
        p = _load_state(PP)["positions"][_key(TOK, 8)]
        assert len(p["pending_exits"]) == 1 and p["pending_exits"][0]["tries"] == 1, "identical exit bumps, not dupes"
        c = retry_pending(T0 + 400, quote_buy_fn=make_buy(["ok"]), quote_sell_fn=make_sell([("ok", 4.0)]), **kw)
        assert c["exits_filled"] == 1 and c["still_pending"] == 0, c
        p = _load_state(PP)["positions"][_key(TOK, 8)]
        assert p["closed"] and p["close_reason"] == "stop" and p["pending_exits"] == []
        last = rows(LP)[-1]
        assert last["side"] == "stop" and abs(float(last["usd_flow"]) - (4.0 - GAS)) < 1e-6
        assert abs(float(last["gap_s"]) - (60.0 + 300.0)) < 1e-6, "gap = signal gap + time deferred"
        assert "retry 3" in last["note"] or "retry" in last["note"]
        print("P4  deferred stop queued, filled by retry_pending         ok")

        # P5 a deferred exit is logged <kind>_failed after PAPER_PENDING_MAX_RUNS retries, tokens kept
        open_position(TOK, "DEMO", "A", 9, T0, plan=plan, alert_ts=T0, quote_buy_fn=make_buy(["ok"]), **kw)
        ev5 = dict(ev3, event_seq=9)
        assert execute_exit(ev5, T0 + 100, quote_sell_fn=make_sell([("deferred", 0)]), **kw) is None
        for i in range(config.PAPER_PENDING_MAX_RUNS):
            c = retry_pending(T0 + 200 + i, quote_buy_fn=make_buy(["deferred"]),
                              quote_sell_fn=make_sell([("deferred", 0)]), **kw)
            if i < config.PAPER_PENDING_MAX_RUNS - 1:
                assert c["still_pending"] == 1 and c["exits_failed"] == 0, (i, c)
            else:
                assert c["exits_failed"] == 1 and c["still_pending"] == 0, (i, c)
        p = _load_state(PP)["positions"][_key(TOK, 9)]
        assert not p["closed"] and p["tokens_remaining_raw"] == 10 ** 24 and p["pending_exits"] == []
        last = rows(LP)[-1]
        assert last["side"] == "stop_failed" and last["tokens_raw_delta"] == "0" and last["quote_status"] == "deferred"
        print("P5  deferred exit ⇒ stop_failed after %d retries, tokens kept   ok" % config.PAPER_PENDING_MAX_RUNS)

        # P6 tp with 'absent' writes tp_failed and keeps the tokens
        ev6 = dict(ev, event_seq=9)
        r = execute_exit(ev6, T0 + 5000, quote_sell_fn=make_sell([("absent", 0)]), **kw)
        p = _load_state(PP)["positions"][_key(TOK, 9)]
        last = rows(LP)[-1]
        assert r is None and last["side"] == "tp_failed" and last["tokens_raw_delta"] == "0"
        assert not p["closed"] and p["tokens_remaining_raw"] == 10 ** 24 and p["realized_usd"] == 0.0
        print("P6  tp + absent ⇒ tp_failed, tokens kept                  ok")

        # P7 an exit for an unknown key is ignored (the B control never enters the book)
        n_before = len(rows(LP))
        assert execute_exit(dict(ev3, event_seq=999), T0 + 5000, quote_sell_fn=make_sell([("ok", 9.0)]), **kw) is None
        assert execute_exit(dict(ev3, token="0x" + "c" * 40), T0 + 5000, quote_sell_fn=make_sell([("ok", 9.0)]), **kw) is None
        assert len(rows(LP)) == n_before and len(_load_state(PP)["positions"]) == 3
        print("P7  unknown key ignored                                   ok")

        # P8 atomic write: os.replace raises after the tmp is written; the original is intact
        before = open(PP).read()
        json.loads(before)
        orig_replace = os.replace

        def boom(src, dst):
            assert os.path.exists(src) and os.path.getsize(src) > 0, "tmp must be fully written first"
            raise OSError("simulated crash between tmp write and replace")
        os.replace = boom
        try:
            st = _load_state(PP)
            st["positions"]["garbage:1"] = {"token": "garbage", "nan": float("nan")}
            ok = _save_state(st, PP)
            r = open_position(TOK, "DEMO", "A", 10, T0, plan=plan, alert_ts=T0, quote_buy_fn=make_buy(["ok"]), **kw)
        finally:
            os.replace = orig_replace
        assert ok is False and r is None
        assert open(PP).read() == before and json.loads(before)
        assert "garbage:1" not in _load_state(PP)["positions"] and _key(TOK, 10) not in _load_state(PP)["positions"]
        assert not [f for f in os.listdir(d) if f.endswith(".tmp")], "no tmp left behind"
        st = _load_state(PP); st["x"] = float("nan"); st["positions"]["k"] = {"v": float("inf"), "pending_exits": []}
        assert _save_state(st, PP) and json.load(open(PP))["positions"]["k"]["v"] is None
        st = _load_state(PP); st["positions"].pop("k"); _save_state(st, PP)
        print("P8  atomic write: original intact after a failed replace  ok")

        # P9 the plan is frozen: a champion switch does not change what an open position does
        p9 = os.path.join(d, "champion.json")
        saved_path = config.CHAMPION_PATH
        config.CHAMPION_PATH = p9
        try:
            champion.write_state(exit={"champion": "sell_15m", "promoted_ts": T0}, path=p9)
            assert champion.exit_plan()["max_hold_s"] == 900, "champion switched to a pure time exit"
            ev9 = {"kind": "time_exit", "token": TOK, "symbol": "DEMO", "event_seq": 9, "price": 1e-23,
                   "ret": 0.0, "mult": 1.0, "due_ts": T0 + 900, "gap_s": 30.0}
            n_before = len(rows(LP))
            sell = make_sell([("ok", 9.0)])
            assert execute_exit(ev9, T0 + 1000, quote_sell_fn=sell, **kw) is None
            assert sell.calls["n"] == 0 and len(rows(LP)) == n_before, "no quote, no row: stored plan has no max_hold_s"
            p = _load_state(PP)["positions"][_key(TOK, 9)]
            assert not p["closed"] and p["plan"]["name"] == "cfg_ladder_stop"
            # a rung the frozen ladder does not have is skipped too
            assert execute_exit(dict(ev6, levels=[(3.0, 0.5)]), T0 + 1000, quote_sell_fn=sell, **kw) is None
            assert sell.calls["n"] == 0
            # ...whereas a position OPENED under the new champion honours the time exit
            p15 = champion.exit_plan()
            open_position(TOK, "DEMO", "A", 11, T0, plan=p15, alert_ts=T0, quote_buy_fn=make_buy(["ok"]), **kw)
            r = execute_exit(dict(ev9, event_seq=11), T0 + 1000, quote_sell_fn=make_sell([("ok", 9.5)]), **kw)
            assert r and r["side"] == "time_exit" and r["plan_name"] == "sell_15m"
            p = _load_state(PP)["positions"][_key(TOK, 11)]
            assert p["closed"] and p["close_reason"] == "time_exit" and p["champion_at_open"] == "sell_15m"
        finally:
            config.CHAMPION_PATH = saved_path
        print("P9  plan frozen per position (champion switch ignored)    ok")

        # P10 a deferred BUY is queued and filled by retry_pending later (entry_lag_s in the note)
        r = open_position(TOK, "DEMO", "A", 12, T0 + 10, plan=plan, alert_ts=T0, quote_buy_fn=make_buy(["deferred"]), **kw)
        st = _load_state(PP)
        assert r is None and len(st["pending_buys"]) == 1 and _key(TOK, 12) not in st["positions"]
        assert rows(LP)[-1]["side"] == "buy_deferred"
        r = open_position(TOK, "DEMO", "A", 12, T0 + 11, plan=plan, alert_ts=T0, quote_buy_fn=make_buy(["ok"]), **kw)
        assert r is None and len(_load_state(PP)["pending_buys"]) == 1, "pending key is not re-quoted by the alert path"
        c = retry_pending(T0 + 700, quote_buy_fn=make_buy(["deferred"]), quote_sell_fn=make_sell([("ok", 1.0)]), **kw)
        assert c["still_pending"] == 1 and _load_state(PP)["pending_buys"][0]["tries"] == 1
        c = retry_pending(T0 + 900, quote_buy_fn=make_buy(["ok"]), quote_sell_fn=make_sell([("ok", 1.0)]), **kw)
        assert c["buys_filled"] == 1 and c["still_pending"] == 0, c
        st = _load_state(PP)
        assert st["pending_buys"] == [] and _key(TOK, 12) in st["positions"]
        last = rows(LP)[-1]
        assert last["side"] == "buy" and "entry_lag_s=900" in last["note"] and abs(float(last["gap_s"]) - 900) < 1e-6
        # a pending buy that goes absent, and one given up after PAPER_PENDING_MAX_RUNS
        open_position(TOK, "DEMO", "A", 13, T0, plan=plan, alert_ts=T0, quote_buy_fn=make_buy(["deferred"]), **kw)
        c = retry_pending(T0 + 950, quote_buy_fn=make_buy(["absent"]), quote_sell_fn=make_sell([("ok", 1.0)]), **kw)
        assert c["buys_failed"] == 1 and rows(LP)[-1]["side"] == "buy_failed" and _load_state(PP)["pending_buys"] == []
        open_position(TOK, "DEMO", "A", 14, T0, plan=plan, alert_ts=T0, quote_buy_fn=make_buy(["deferred"]), **kw)
        for i in range(config.PAPER_PENDING_MAX_RUNS):
            c = retry_pending(T0 + 1000 + i, quote_buy_fn=make_buy(["deferred"]), quote_sell_fn=make_sell([("ok", 1.0)]), **kw)
        assert c["buys_failed"] == 1 and _load_state(PP)["pending_buys"] == []
        assert rows(LP)[-1]["side"] == "buy_failed" and "given up" in rows(LP)[-1]["note"]
        print("P10 deferred buy queued, filled by retry_pending          ok")

        # P11 every fill row carries quote_source and quote_status
        all_rows = rows(LP)
        assert all_rows and list(all_rows[0].keys()) == COLUMNS
        fills = [r for r in all_rows if r["side"] in ("buy", "stop", "trail", "time_exit", "no_route") or r["side"].startswith("tp_")]
        assert len(fills) >= 8
        for r in all_rows:
            assert r["quote_status"] in ("ok", "absent", "deferred"), r
            assert r["quote_source"], r
            if r["quote_status"] == "ok":
                assert r["quote_source"] in quotes._SOURCES, r
        for r in fills:
            if r["side"] != "no_route" and not r["side"].endswith("_failed"):
                assert r["quote_status"] == "ok" and r["quote_source"] in quotes._SOURCES, r
        assert sorted({r["side"] for r in all_rows}) == sorted({"buy", "buy_deferred", "buy_failed", "tp_2x", "tp_5x+10x",
                                                                "no_route", "stop", "stop_failed", "tp_failed", "time_exit"})
        print("P11 every row carries quote_source + quote_status         ok")

        # summary with one batched mark (two open positions share TOK: one quote, pro-rated)
        def many(items, now_s, *, weth_px=None):
            return {t: _q("sell", t, a, now_s, "ok", out=int(6.0 / WPX * 10 ** 18), source="multicall") for t, a in items}
        print()
        s = summary(quote_sell_many_fn=many, now_s=T0 + 2000, **kw)
        # seqs 7, 8, 9, 11, 12 opened; 10 (failed atomic write), 13 (absent) and 14 (given up) never did
        assert s["n_positions"] == 5 and s["n_open"] == 2 and s["n_closed"] == 3, s
        assert abs(s["open_value_usd"] - (6.0 - 2 * GAS)) < 1e-9, "one $6 mark pro-rated over two remainders, gas each"
        assert abs(s["invested_usd"] - 5 * USD) < 1e-9 and s["mean_entry_impact_pct"] is not None
        assert abs(s["gas_usd"] - 9 * GAS) < 1e-9, "9 swaps paid gas; the no_route close paid none"
        assert set(s["by_plan"]) == {"cfg_ladder_stop", "sell_15m"} and s["by_plan"]["sell_15m"]["n"] == 1
        s2 = summary(mark_open=False, **kw)
        assert s2["open_value_usd"] == 0.0 and s2["n_unmarked"] == 0
        print("summary ok")
    print("\nOK (offline)")

    if "--live" in sys.argv:
        import time
        now_s = time.time()                                   # the only wall-clock read
        MIZ = config.MIZUKARA
        print("\n--live: $%.2f MIZUKARA round trip at real quotes (nothing written outside a temp dir)" % USD)
        st, px = quotes.weth_price_usd(now_s)
        print("  WETH/USD: %s %s" % (st, px))
        b = quotes.quote_buy(MIZ, USD, now_s, weth_px=px)
        print("  buy : " + quotes.describe(b))
        if b["status"] == "ok":
            s = quotes.quote_sell(MIZ, b["amount_out_raw"], now_s, weth_px=px)
            print("  sell: " + quotes.describe(s))
            if s["status"] == "ok":
                back = float(s["usd"])
                print("  round trip: $%.2f → $%.4f  quote cost %.3f%%  + gas 2×$%.2f  = net %.3f%% "
                      "(that is the execution cost the ledger cannot see)"
                      % (USD, back, (1 - back / USD) * 100, GAS, (1 - (back - 2 * GAS) / USD) * 100))
        with tempfile.TemporaryDirectory() as d:
            r = open_position(MIZ, "MIZUKARA", "A", 0, now_s, plan=champion.exit_plan(), alert_ts=now_s,
                              weth_px=px, positions_path=os.path.join(d, "p.json"),
                              ledger_path=os.path.join(d, "l.csv"))
            print("  paper buy row: %s" % ({k: r[k] for k in ("side", "usd_flow", "px_usd", "impact_pct",
                                                              "quote_source", "quote_status")} if r else "not filled"))
        print("OK (live)")
