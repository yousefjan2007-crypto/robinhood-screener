"""
quotes.py — the EVM replacement for solana_screener's Jupiter quote, used ONLY by
paper_exec.py and selfimprove/livebook.py (paper fills and the live paper book). The pass-1
honeypot / sell-tax gate is rpc.honeypot_roundtrip, never this module.

Why a separate module with a baked-in status: on solana, 16 of the livebook's 51 "≥3x
winners" were quote artifacts and a sibling project fabricated 472 of 1,400 ledger rows as
dead tokens by reading a rate limit as "no route". So every quote here is a dict that can
never be None and always carries a THREE-WAY status the caller must branch on:

    ok        — a real, sized fill at a named source
    absent    — the coin is DEAD for execution purposes; a $0 fill is honest
    deferred  — unknown this tick (rate limit, node down, aggregator dark): retry, never fill

"Dead" is decided ON-CHAIN FIRST (plan table "Quote layer / honeypot", binding). The commonest
death on this chain is a drained UniswapV2 pool, and that verdict must never depend on an
aggregator answering:

    absent ⇔ (V2 pair exists AND getReserves == (0,0) AND router ∈ {revert, zero})
          OR (no V2 pair AND kyber == no_route AND scanhood == no_route)

Aggregators (KyberSwap, then ScanHood) are consulted only when there is NO V2 pair (V3/V4/
pons pools — the V2 Router02 reverts on those). Any probe the active branch needs that went
unanswered ⇒ deferred. When the router reverts on a pool that still has reserves, the
constant-product fallback (config.UNIV2_FEE_BPS) prices the fill from the reserves so a
transient router quirk is not read as death either.

Resolution order for a sell (buy mirrors it with path [WETH, token]):
    (0) WETH/USD (Dexscreener WETH/USDG → GeckoTerminal fallback); unknown ⇒ deferred
    (1) rpc.v2_pair(token): deferred ⇒ deferred. Pair exists ⇒ router getAmountsOut
        ok ⇒ 'router' (or 'multicall' in the batched path); revert/zero ⇒ getReserves:
        (0,0) ⇒ ABSENT (drained), > 0 ⇒ 'reserves' constant-product, unanswered ⇒ deferred
    (2) no pair ⇒ kyber.route: ok ⇒ 'kyber'; no_route ⇒ scanhood.quote: ok ⇒ 'scanhood';
        no_route ⇒ ABSENT; any unanswered ⇒ deferred

Reserves are also fetched on the router-ok path (one extra eth_call, or free inside the
Multicall3 batch) because the livebook's quote-integrity gate R3 refuses a WETH amount_out
larger than the pool's WETH reserve, and impact_pct needs the spot ratio. A missing reserves
probe never downgrades an ok router quote — it is informational there.

Raw units end-to-end: amount_in_raw / amount_out_raw are wei for WETH and raw token units for
the token; `usd` is the WETH leg in USD (spent on a buy, received on a sell). Quotes are never
cached (a stale quote is a fabricated fill). No wall-clock in any compute path: `now_s` comes
from the caller and is echoed as `ts`. Nothing here raises on bad data — one bad token never
kills a run.

Measured 2026-09-12 on MIZUKARA (V2 pool ≈ 2.3 WETH): $10 round trip ≈ 0.6% fees + ~0.3%
impact; the 0.01 WETH honeypot probe round trip is 1.44%.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config                                                   # noqa: E402
from http_client import NOT_FOUND, is_absent, is_deferred        # noqa: E402
from sources import dexscreener, geckoterminal, kyber, rpc, scanhood  # noqa: E402

WETH_DECIMALS = 18                     # WETH is 18-decimal on every chain; not a tunable
_SOURCES = ("router", "multicall", "kyber", "scanhood", "reserves")
_STATUSES = ("ok", "absent", "deferred")


# ── quote dict construction ─────────────────────────────────────────────────────
def _probes() -> dict:
    return {"router": "skipped", "kyber": "skipped", "scanhood": "skipped",
            "pair": "skipped", "reserves": None}


def _new(side: str, token: str, amount_in_raw: int, now_s: float, weth_px) -> dict:
    """A deferred skeleton with every key present, in the documented order. Every resolver
    fills it in place; a resolver that bails early leaves an honest 'deferred'."""
    return {"status": "deferred", "side": side, "token": token,
            "amount_in_raw": int(amount_in_raw), "amount_out_raw": None, "usd": None,
            "weth_px_usd": weth_px, "source": None, "impact_pct": None,
            "gas_usd": float(config.PAPER_GAS_USD_PER_SWAP), "ts": now_s, "probes": _probes()}


def _note(q: dict, text: str) -> None:
    prev = q["probes"].get("note")
    q["probes"]["note"] = (prev + "; " + text) if prev else text


def _weth_usd(wei, weth_px) -> float | None:
    try:
        return None if (wei is None or weth_px is None) else wei / 10 ** WETH_DECIMALS * weth_px
    except (TypeError, ValueError, OverflowError):
        return None


def _finish_ok(q: dict, amount_out: int, source: str, weth_px, *, impact=None, usd=None) -> dict:
    q["status"] = "ok"
    q["amount_out_raw"] = int(amount_out)
    q["source"] = source
    q["impact_pct"] = impact
    weth_leg = amount_out if q["side"] == "sell" else q["amount_in_raw"]
    q["usd"] = usd if usd is not None else _weth_usd(weth_leg, weth_px)
    return q


def _finish_absent(q: dict) -> dict:
    q["status"] = "absent"
    q["amount_out_raw"] = None
    q["usd"] = None
    q["source"] = None
    q["impact_pct"] = None
    return q


# ── WETH/USD ────────────────────────────────────────────────────────────────────
def weth_price_usd(now_s: float) -> tuple:
    """('ok', px) | ('deferred', None). Dexscreener's WETH/USDG pair (~$29.7M, cached
    WETH_PX_CACHE_S inside dexscreener) first, GeckoTerminal's token attrs second. NEVER 0.0:
    a zero price would value every WETH-quoted position at nothing."""
    try:
        px = dexscreener.weth_price_usd(now_s)
        if px and px > 0:
            return "ok", float(px)
    except Exception:
        pass
    try:
        a = geckoterminal.token_attrs(config.WETH)
        if isinstance(a, dict) and not is_absent(a):
            px = a.get("price_usd")
            if px and px > 0:
                return "ok", float(px)
    except Exception:
        pass
    return "deferred", None


def _resolve_weth_px(now_s: float, weth_px):
    """The caller's price when given and sane; otherwise fetch. None ⇒ deferred."""
    try:
        if weth_px is not None and float(weth_px) > 0:
            return float(weth_px)
    except (TypeError, ValueError):
        pass
    return weth_price_usd(now_s)[1]


# ── ABI decoding for the batched path (rpc.dec_uint is the only primitive used) ───
def _amounts_from(success: bool, data) -> tuple:
    """getAmountsOut return data → (last amount|None, 'ok'|'zero'|'revert'). Empty or short
    return data on a successful call is the router's "no route" — deterministic."""
    if not success or not data or len(str(data)) < 2 + 128:
        return None, "revert"
    try:
        off = rpc.dec_uint(data, 0) // 32
        n = rpc.dec_uint(data, off)
        if n < 1:
            return None, "revert"
        last = rpc.dec_uint(data, off + n)
    except (ValueError, TypeError):
        return None, "revert"
    return (last, "ok") if last > 0 else (0, "zero")


def _reserves_from(success: bool, data, token: str):
    """getReserves return data → (reserve_token, reserve_weth) | None (unusable: re-ask)."""
    if not success or not data:
        return None
    try:
        r0, r1 = rpc.dec_uint(data, 0), rpc.dec_uint(data, 1)
        token_is_0 = int(token, 16) < int(config.WETH, 16)
    except (ValueError, TypeError):
        return None
    return (r0, r1) if token_is_0 else (r1, r0)


# ── constant product + impact ───────────────────────────────────────────────────
def _cp_out(amount_in: int, r_in: int, r_out: int) -> int:
    """UniswapV2 getAmountOut with config.UNIV2_FEE_BPS, integer math end to end."""
    fee = 10000 - int(config.UNIV2_FEE_BPS)
    a = int(amount_in) * fee
    denom = int(r_in) * 10000 + a
    return a * int(r_out) // denom if denom > 0 else 0


def _impact_from_reserves(side: str, amount_in, amount_out, res) -> float | None:
    """(1 - realized/spot)*100 with spot = Rt/Rw for a buy (tokens per WETH) and Rw/Rt for a
    sell; includes the pool fee, which is what a fill actually pays."""
    try:
        if not res or amount_in is None or amount_out is None or amount_in <= 0:
            return None
        rt, rw = res
        if rt <= 0 or rw <= 0:
            return None
        spot = (rt / rw) if side == "buy" else (rw / rt)
        if spot <= 0:
            return None
        return round((1.0 - (amount_out / amount_in) / spot) * 100.0, 4)
    except (TypeError, ValueError, ZeroDivisionError, OverflowError):
        return None


def _impact_from_kyber(kq: dict) -> float | None:
    """(amount_out_usd / amount_in_usd - 1) * -100 when Kyber priced both legs."""
    try:
        i, o = kq.get("amount_in_usd"), kq.get("amount_out_usd")
        if i is None or o is None or i <= 0:
            return None
        return round((o / i - 1.0) * -100.0, 4)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


# ── decimals / whole-unit strings for ScanHood ──────────────────────────────────
def _decimals_ex(token: str) -> tuple:
    """(decimals|None, deferred). rpc distinguishes 'the contract has no decimals()' (a fact)
    from 'the node did not answer' (deferred) in _decimals_ex; fall back conservatively."""
    fn = getattr(rpc, "_decimals_ex", None)
    try:
        if callable(fn):
            d, deferred = fn(token)
            return d, bool(deferred)
        d = rpc.decimals(token)
        return d, d is None
    except Exception:
        return None, True


def _whole_str(raw: int, decimals: int) -> str:
    """Exact decimal string of raw / 10**decimals ("1000000", "0.01") — never through float."""
    raw = int(raw)
    if decimals <= 0:
        return str(raw)
    q, r = divmod(raw, 10 ** decimals)
    if r == 0:
        return str(q)
    frac = str(r).rjust(decimals, "0").rstrip("0")
    return "%d.%s" % (q, frac)


# ── branch 1: a V2 pair exists ──────────────────────────────────────────────────
def _resolve_with_pair(q: dict, token: str, pair: str, weth_px, router_res, reserves=None,
                       reserves_known: bool = False, source_ok: str = "router") -> dict:
    """Finish a quote for a token with a V2 pair. router_res = (amount|None, status).
    `reserves` is a pre-fetched (rt, rw) (batched path) or None; when `reserves_known` is
    False and they are needed, one getReserves eth_call is made."""
    side, amount_in = q["side"], q["amount_in_raw"]
    q["probes"]["pair"] = pair
    amt, rst = router_res
    q["probes"]["router"] = rst if rst in ("ok", "revert", "zero", "deferred") else "deferred"
    if q["probes"]["router"] == "deferred":
        return q                                            # the skeleton is already deferred

    res_status = "ok" if reserves is not None else None
    if reserves is None and not reserves_known:
        try:
            res_status, reserves = rpc.v2_reserves(pair, token)
        except Exception:
            res_status, reserves = "deferred", None
    if res_status == "ok" and reserves is not None:
        q["probes"]["reserves"] = [int(reserves[0]), int(reserves[1])]

    if rst == "ok":
        return _finish_ok(q, amt, source_ok, weth_px,
                          impact=_impact_from_reserves(side, amount_in, amt, reserves))

    # router revert / zero: the pool decides, never an aggregator
    if res_status != "ok" or reserves is None:
        _note(q, "router %s; reserves unanswered (%s)" % (rst, res_status or "unknown"))
        return q                                            # deferred
    rt, rw = int(reserves[0]), int(reserves[1])
    if rt == 0 or rw == 0:
        _note(q, "drained V2 pool")
        return _finish_absent(q)
    if side == "sell":
        out = _cp_out(amount_in, rt, rw)
    else:
        out = _cp_out(amount_in, rw, rt)
    _note(q, "constant-product from reserves (router %s)" % rst)
    return _finish_ok(q, out, "reserves", weth_px,
                      impact=_impact_from_reserves(side, amount_in, out, (rt, rw)))


# ── branch 2: no V2 pair → aggregators ──────────────────────────────────────────
def _scanhood_probe(q: dict, token: str, weth_px) -> dict:
    """ScanHood /api/quote as the last resort. Its amounts are WHOLE units: a sell needs the
    token's decimals; a buy is sized in ETH (18 dp) and needs none."""
    side, amount_in = q["side"], q["amount_in_raw"]
    existence_only = False
    if side == "sell":
        dec, deferred = _decimals_ex(token)
        if dec is None and deferred:
            q["probes"]["scanhood"] = "deferred"
            _note(q, "token decimals() unanswered; sell cannot be sized")
            return q
        if dec is None:
            # A contract with no decimals(): nothing can size the sell, but whether a route
            # exists at all is still a fair question — ask for a nominal amount only.
            existence_only = True
            amt_str = "1"
        else:
            amt_str = _whole_str(amount_in, int(dec))
    else:
        amt_str = _whole_str(amount_in, WETH_DECIMALS)
    try:
        sq = scanhood.quote(token, side, amt_str)
    except Exception:
        sq = None
    if is_deferred(sq):
        q["probes"]["scanhood"] = "deferred"
        return q
    if is_absent(sq) or not isinstance(sq, dict):
        q["probes"]["scanhood"] = "no_route"
        return _finish_absent(q) if q["probes"]["kyber"] == "no_route" else q
    q["probes"]["scanhood"] = "ok"
    if existence_only:
        _note(q, "scanhood has a route but the token has no decimals(); unsizable")
        return q                                            # deferred, honestly
    out = sq.get("amount_out")
    echoed = sq.get("amount_in")
    if out is None or out <= 0:
        q["probes"]["scanhood"] = "no_route"
        return _finish_absent(q) if q["probes"]["kyber"] == "no_route" else q
    if echoed is not None and int(echoed) != int(amount_in):
        _note(q, "scanhood echoed amount_in %s != requested %s" % (echoed, amount_in))
        q["probes"]["scanhood"] = "deferred"
        return q
    return _finish_ok(q, out, "scanhood", weth_px)


def _resolve_no_pair(q: dict, token: str, weth_px) -> dict:
    side, amount_in = q["side"], q["amount_in_raw"]
    q["probes"]["pair"] = "none"
    t_in, t_out = (token, config.WETH) if side == "sell" else (config.WETH, token)
    try:
        kq = kyber.route(t_in, t_out, amount_in)
    except Exception:
        kq = None
    if is_deferred(kq):
        q["probes"]["kyber"] = "deferred"
        return _scanhood_probe(q, token, weth_px)         # may still fill; never absent
    if is_absent(kq) or not isinstance(kq, dict) or not kq.get("amount_out"):
        q["probes"]["kyber"] = "no_route"
        return _scanhood_probe(q, token, weth_px)
    q["probes"]["kyber"] = "ok"
    usd = kq.get("amount_out_usd") if side == "sell" else kq.get("amount_in_usd")
    return _finish_ok(q, kq["amount_out"], "kyber", weth_px,
                      impact=_impact_from_kyber(kq), usd=usd if usd and usd > 0 else None)


# ── the single-token path (the correctness reference for the batch) ─────────────
def _quote_one(side: str, token: str, amount_in: int, now_s: float, weth_px) -> dict:
    q = _new(side, token, amount_in, now_s, None)
    try:
        px = _resolve_weth_px(now_s, weth_px)
        if px is None:
            _note(q, "WETH/USD unavailable")
            return q
        q["weth_px_usd"] = px
        if q["amount_in_raw"] <= 0:
            _note(q, "non-positive amount_in: nothing to quote")
            return _finish_ok(q, 0, None, px)
        try:
            pst, pair = rpc.v2_pair(token)
        except Exception:
            pst, pair = "deferred", None
        if pst == "deferred":
            q["probes"]["pair"] = "deferred"
            return q
        if pst == "ok" and pair:
            path = [token, config.WETH] if side == "sell" else [config.WETH, token]
            try:
                router_res = rpc.get_amounts_out(q["amount_in_raw"], path)
            except Exception:
                router_res = (None, "deferred")
            return _resolve_with_pair(q, token, pair, px, router_res)
        return _resolve_no_pair(q, token, px)
    except Exception as exc:                                # never raise: deferred, named
        _note(q, "internal error: %s" % str(exc)[:120])
        q["status"] = "deferred"
        return q


def quote_sell(token: str, tokens_raw: int, now_s: float, *, weth_px=None) -> dict:
    """Sell `tokens_raw` raw units of `token` for WETH. Never None; branch on ['status']."""
    try:
        tokens_raw = int(tokens_raw)
    except (TypeError, ValueError):
        tokens_raw = 0
    return _quote_one("sell", token, tokens_raw, now_s, weth_px)


def quote_buy(token: str, usd: float, now_s: float, *, weth_px=None) -> dict:
    """Buy `usd` worth of `token` with WETH: amount_in_wei = usd / weth_px * 1e18. Never None."""
    px = _resolve_weth_px(now_s, weth_px)
    if px is None:
        q = _new("buy", token, 0, now_s, None)
        _note(q, "WETH/USD unavailable")
        return q
    try:
        wei = int(float(usd) / px * 10 ** WETH_DECIMALS)
    except (TypeError, ValueError, OverflowError):
        wei = 0
    return _quote_one("buy", token, wei, now_s, px)


# ── the batched path (Multicall3; per-token fallback) ────────────────────────────
def quote_sell_many(items: list, now_s: float, *, weth_px=None) -> dict:
    """{token: Quote} for [(token, tokens_raw), ...]. With config.USE_MULTICALL3 the router
    and reserves calls for every token that has a V2 pair go into ONE rpc.multicall3
    (allowFailure); a per-item router failure proceeds through the single-token revert branch
    with the batched reserves; a batch that returns None or fails to decode falls back to
    quote_sell for EVERY item this tick — never a silent deferral of the whole book.
    A token listed twice keeps the last entry (the dict is keyed by token)."""
    out: dict = {}
    items = [(t, a) for (t, a) in (items or [])]
    if not items:
        return out
    px = _resolve_weth_px(now_s, weth_px)
    if not config.USE_MULTICALL3 or px is None:
        for t, a in items:
            out[t] = quote_sell(t, a, now_s, weth_px=px)
        return out

    # pairs first (rpc.v2_pair caches a non-zero pair forever, so open positions are free)
    pair_of: dict = {}
    batch: list = []                                        # (token, amount, pair)
    for t, a in items:
        try:
            a = int(a)
        except (TypeError, ValueError):
            a = 0
        if a <= 0:
            pair_of[t] = ("skip", None)
            continue
        try:
            pst, pair = rpc.v2_pair(t)
        except Exception:
            pst, pair = "deferred", None
        pair_of[t] = (pst, pair)
        if pst == "ok" and pair:
            batch.append((t, a, pair))

    decoded: dict = {}
    if batch:
        calls = []
        for t, a, pair in batch:
            calls.append((config.UNIV2_ROUTER,
                          rpc.encode_call(config.SEL_GET_AMOUNTS_OUT, ("uint", a),
                                          ("addr[]", [t, config.WETH]))))
            calls.append((pair, config.SEL_GET_RESERVES))
        try:
            res = rpc.multicall3(calls)
        except Exception:
            res = None
        if res is None or len(res) != len(calls):
            for t, a in items:                              # the whole tick, per token
                out[t] = quote_sell(t, a, now_s, weth_px=px)
            return out
        for i, (t, a, pair) in enumerate(batch):
            ok_r, data_r = res[2 * i]
            ok_s, data_s = res[2 * i + 1]
            decoded[t] = (_amounts_from(ok_r, data_r), _reserves_from(ok_s, data_s, t))

    for t, a in items:
        pst, pair = pair_of.get(t, ("deferred", None))
        if pst == "skip":
            out[t] = quote_sell(t, a, now_s, weth_px=px)
            continue
        q = _new("sell", t, int(a), now_s, px)
        if pst == "deferred":
            q["probes"]["pair"] = "deferred"
            out[t] = q
        elif pst == "ok" and pair:
            router_res, reserves = decoded.get(t, ((None, "deferred"), None))
            out[t] = _resolve_with_pair(q, t, pair, px, router_res, reserves=reserves,
                                        reserves_known=False, source_ok="multicall")
        else:
            out[t] = _resolve_no_pair(q, t, px)
    return out


# ── one-liner for fills / notes ─────────────────────────────────────────────────
def _fmt_raw(x) -> str:
    try:
        return "%.3g" % float(x)
    except (TypeError, ValueError):
        return str(x)


def describe(q: dict) -> str:
    """'sell 1.2e24 raw → 0.00321 WETH ($8.13) via router' / 'ABSENT: router zero, reserves
    (0,0) — drained V2 pool' / 'DEFERRED: pair deferred'."""
    try:
        p = q.get("probes") or {}
        st, side, src = q.get("status"), q.get("side"), q.get("source")
        usd = q.get("usd")
        usd_s = ("$%.2f" % usd) if isinstance(usd, (int, float)) else "$?"
        if st == "ok":
            ain, aout = q.get("amount_in_raw"), q.get("amount_out_raw")
            if side == "sell":
                s = "sell %s raw → %.6g WETH (%s) via %s" % (
                    _fmt_raw(ain), (aout or 0) / 10 ** WETH_DECIMALS, usd_s, src)
            else:
                s = "buy %.6g WETH (%s) → %s raw via %s" % (
                    (ain or 0) / 10 ** WETH_DECIMALS, usd_s, _fmt_raw(aout), src)
            if q.get("impact_pct") is not None:
                s += " impact %.2f%%" % q["impact_pct"]
        else:
            parts = []
            pair = p.get("pair")
            if pair == "none":
                parts.append("no V2 pair")
            elif pair == "deferred":
                parts.append("pair deferred")
            elif pair and pair != "skipped":
                parts.append("router %s" % p.get("router"))
                res = p.get("reserves")
                if res is not None:
                    parts.append("reserves (%s,%s)" % (_fmt_raw(res[0]), _fmt_raw(res[1])))
                elif p.get("router") in ("revert", "zero"):
                    parts.append("reserves unanswered")
            for k in ("kyber", "scanhood"):
                if p.get(k, "skipped") != "skipped":
                    parts.append("%s %s" % (k, p[k]))
            s = "%s: %s" % (st.upper() if st else "?", ", ".join(parts) or "no probes")
        if p.get("note"):
            s += " — " + p["note"]
        return s
    except Exception:
        return "quote %r" % (q,)


# ── smoke test ───────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import time
    now_s = time.time()                                     # the only wall-clock read
    MIZ, POOL = config.MIZUKARA, config.MIZUKARA_POOL
    BOGUS = "0x" + "0" * 39 + "1"
    print("robinhood_screener quotes")

    st, px = weth_price_usd(now_s)
    print("  WETH/USD: %s %s" % (st, px))
    assert st == "ok" and px is not None and 500 <= px <= 20000, "WETH price out of range"

    # live round trip on MIZUKARA ($10)
    b = quote_buy(MIZ, 10.0, now_s, weth_px=px)
    print("  buy : " + describe(b))
    assert b["status"] == "ok" and b["source"] in ("router", "multicall"), b
    assert b["amount_out_raw"] > 0 and b["amount_in_raw"] > 0
    assert set(b) == set(_new("buy", MIZ, 1, now_s, px)), "quote keys drifted"
    s = quote_sell(MIZ, b["amount_out_raw"], now_s, weth_px=px)
    print("  sell: " + describe(s))
    assert s["status"] == "ok" and s["source"] == "router", s
    cost = (1.0 - s["amount_out_raw"] / b["amount_in_raw"]) * 100.0
    fee_floor = 2 * config.UNIV2_FEE_BPS / 100.0
    print("  round-trip cost: %.3f%%  (fees %.2f%% + impact on a ~2.3 WETH pool; "
          "1.44%% at the 0.01 WETH probe)" % (cost, fee_floor))
    assert fee_floor <= cost < config.SELL_TAX_MAX_ROUNDTRIP_PCT, cost
    assert s["probes"]["reserves"] and s["probes"]["reserves"][1] > 0, "reserves missing"
    assert s["amount_out_raw"] < s["probes"]["reserves"][1], "livebook R3: out > reserve_weth"

    many = quote_sell_many([(MIZ, b["amount_out_raw"])], now_s, weth_px=px)
    m = many[MIZ]
    print("  many: " + describe(m))
    assert m["status"] == "ok" and m["source"] == "multicall", m
    rel = abs(m["amount_out_raw"] - s["amount_out_raw"]) / s["amount_out_raw"]
    assert rel < 0.001, "multicall vs single differ by %.4f%%" % (rel * 100)
    assert m["probes"]["reserves"] is not None and m["impact_pct"] is not None

    # bogus token: no pair → kyber no_route → scanhood (no_route ⇒ absent | deferred ⇒ deferred)
    bq = quote_sell(BOGUS, 10 ** 18, now_s, weth_px=px)
    print("  bogus: " + describe(bq))
    assert bq["probes"]["pair"] == "none" and bq["probes"]["kyber"] == "no_route", bq["probes"]
    assert bq["probes"]["scanhood"] in ("no_route", "deferred"), bq["probes"]
    expect = "absent" if bq["probes"]["scanhood"] == "no_route" else "deferred"
    print("    scanhood said %s ⇒ overall must be %s: %s" % (
        bq["probes"]["scanhood"], expect, bq["status"]))
    assert bq["status"] == expect

    # ── offline fixtures: monkeypatch the sources ──
    calls = {"kyber": 0}
    orig = {"v2_pair": rpc.v2_pair, "gao": rpc.get_amounts_out, "res": rpc.v2_reserves,
            "kyber": kyber.route, "sh": scanhood.quote, "mc": rpc.multicall3,
            "dec": getattr(rpc, "_decimals_ex", None), "decimals": rpc.decimals}

    def _kyber_counting(ret):
        def f(*a, **k):
            calls["kyber"] += 1
            return ret
        return f

    def _patch(pair=("ok", POOL.lower()), gao=(None, "revert"), res=("ok", (0, 0)),
               kyb=None, sh=None, dec=(18, False), mc=None):
        rpc.v2_pair = lambda t: pair
        rpc.get_amounts_out = lambda a, p: gao
        rpc.v2_reserves = lambda p, t: res
        kyber.route = _kyber_counting(kyb)
        scanhood.quote = lambda a, s, amt: sh
        rpc._decimals_ex = lambda t: dec
        rpc.decimals = lambda t: dec[0]
        rpc.multicall3 = mc if mc is not None else orig["mc"]

    try:
        # (a) router zero + reserves (0,0) + kyber deferred ⇒ ABSENT, kyber never consulted
        _patch(gao=(0, "zero"), res=("ok", (0, 0)), kyb=None)
        qa = quote_sell("0xfixture", 10 ** 24, now_s, weth_px=2500.0)
        print("  (a) " + describe(qa))
        assert qa["status"] == "absent" and calls["kyber"] == 0, qa
        assert qa["probes"]["router"] == "zero" and qa["probes"]["reserves"] == [0, 0]

        # (b) router revert + reserves (1e24, 2e18) ⇒ ok, source reserves, constant product
        _patch(gao=(None, "revert"), res=("ok", (10 ** 24, 2 * 10 ** 18)))
        amt_in = 10 ** 22
        qb = quote_sell("0xfixture", amt_in, now_s, weth_px=2500.0)
        print("  (b) " + describe(qb))
        exp = _cp_out(amt_in, 10 ** 24, 2 * 10 ** 18)
        assert qb["status"] == "ok" and qb["source"] == "reserves", qb
        assert qb["amount_out_raw"] == exp, (qb["amount_out_raw"], exp)
        assert abs(qb["usd"] - exp / 1e18 * 2500.0) < 1e-9 and qb["impact_pct"] > 0
        qb2 = quote_buy("0xfixture", 10.0, now_s, weth_px=2500.0)
        assert qb2["status"] == "ok" and qb2["source"] == "reserves" and qb2["impact_pct"] > 0
        assert qb2["amount_out_raw"] == _cp_out(qb2["amount_in_raw"], 2 * 10 ** 18, 10 ** 24)
        print("  (b') buy mirror: " + describe(qb2))

        # (c) no pair + kyber NOT_FOUND + scanhood None ⇒ DEFERRED
        _patch(pair=("absent", None), kyb=NOT_FOUND, sh=None)
        qc = quote_sell("0xfixture", 10 ** 24, now_s, weth_px=2500.0)
        print("  (c) " + describe(qc))
        assert qc["status"] == "deferred" and qc["probes"]["scanhood"] == "deferred", qc
        assert qc["probes"]["kyber"] == "no_route" and qc["probes"]["pair"] == "none"

        # (d) no pair + kyber NOT_FOUND + scanhood NOT_FOUND ⇒ ABSENT
        _patch(pair=("absent", None), kyb=NOT_FOUND, sh=NOT_FOUND)
        qd = quote_sell("0xfixture", 10 ** 24, now_s, weth_px=2500.0)
        print("  (d) " + describe(qd))
        assert qd["status"] == "absent" and qd["probes"]["scanhood"] == "no_route", qd

        # (d') no pair + kyber deferred + scanhood NOT_FOUND ⇒ DEFERRED (absent needs both)
        _patch(pair=("absent", None), kyb=None, sh=NOT_FOUND)
        qd2 = quote_sell("0xfixture", 10 ** 24, now_s, weth_px=2500.0)
        print("  (d') " + describe(qd2))
        assert qd2["status"] == "deferred" and qd2["probes"]["kyber"] == "deferred", qd2

        # (d'') no pair + kyber deferred + scanhood ok ⇒ ok via scanhood
        _patch(pair=("absent", None), kyb=None,
               sh={"amount_out": 3 * 10 ** 15, "amount_in": 10 ** 24, "decimals": 18})
        qd3 = quote_sell("0xfixture", 10 ** 24, now_s, weth_px=2500.0)
        print("  (d'') " + describe(qd3))
        assert qd3["status"] == "ok" and qd3["source"] == "scanhood", qd3
        assert abs(qd3["usd"] - 3e15 / 1e18 * 2500.0) < 1e-9

        # (e) pair deferred ⇒ DEFERRED, and no aggregator is asked
        _patch(pair=("deferred", None))
        before = calls["kyber"]
        qe = quote_sell("0xfixture", 10 ** 24, now_s, weth_px=2500.0)
        print("  (e) " + describe(qe))
        assert qe["status"] == "deferred" and qe["probes"]["pair"] == "deferred", qe
        assert qe["probes"]["router"] == "skipped" and calls["kyber"] == before

        # (f) multicall3 None ⇒ per-token quote_sell for every item
        _patch(gao=(123456, "ok"), res=("ok", (10 ** 24, 2 * 10 ** 18)), mc=lambda c: None)
        qf = quote_sell_many([("0xfixture", 10 ** 22), ("0xfixture2", 10 ** 22)], now_s,
                             weth_px=2500.0)
        print("  (f) " + describe(qf["0xfixture"]))
        assert all(v["status"] == "ok" and v["source"] == "router" for v in qf.values()), qf

        # (g) multicall3 answers: item 1 router ok, item 2 router failed ⇒ reserves branch
        # Fake tokens numerically < WETH are token0, so (reserve0, reserve1) == (Rt, Rw). Note
        # MIZUKARA (0x4074…) is numerically ABOVE WETH (0x0Bd7…) and is token1 — the decoder
        # swaps by address order exactly as rpc.v2_reserves does; a fixture must encode that.
        fake_tok, fake_tok2 = "0x" + "0" * 39 + "2", "0x" + "0" * 39 + "3"
        enc_amounts = "0x" + rpc.enc_uint(0x20) + rpc.enc_uint(2) + rpc.enc_uint(10 ** 22) \
            + rpc.enc_uint(19_000_000_000_000_000)
        enc_res = "0x" + rpc.enc_uint(10 ** 24) + rpc.enc_uint(2 * 10 ** 18) + rpc.enc_uint(now_s)
        _patch(pair=("ok", POOL.lower()), gao=(None, "deferred"),
               res=("deferred", None),           # the single-call reserves must NOT be needed
               mc=lambda c: [(True, enc_amounts), (True, enc_res), (False, "0x"), (True, enc_res)])
        qg = quote_sell_many([(fake_tok, 10 ** 22), (fake_tok2, 10 ** 22)], now_s, weth_px=2500.0)
        g1, g2 = qg[fake_tok], qg[fake_tok2]
        print("  (g1) " + describe(g1))
        print("  (g2) " + describe(g2))
        assert g1["status"] == "ok" and g1["source"] == "multicall" \
            and g1["amount_out_raw"] == 19_000_000_000_000_000, g1
        assert g1["probes"]["reserves"] == [10 ** 24, 2 * 10 ** 18] and g1["impact_pct"] is not None
        assert g2["status"] == "ok" and g2["source"] == "reserves" \
            and g2["amount_out_raw"] == _cp_out(10 ** 22, 10 ** 24, 2 * 10 ** 18), g2

        # (h) decoding of an ABI-encoded zero amount and a drained pool inside the batch
        enc_zero = "0x" + rpc.enc_uint(0x20) + rpc.enc_uint(2) + rpc.enc_uint(10 ** 22) + rpc.enc_uint(0)
        enc_empty = "0x" + rpc.enc_uint(0) + rpc.enc_uint(0) + rpc.enc_uint(0)
        _patch(pair=("ok", POOL.lower()), mc=lambda c: [(True, enc_zero), (True, enc_empty)])
        qh = quote_sell_many([(fake_tok, 10 ** 22)], now_s, weth_px=2500.0)[fake_tok]
        print("  (h) " + describe(qh))
        assert qh["status"] == "absent" and qh["probes"]["router"] == "zero", qh

        # (i) the skeleton every resolver starts from is deferred with every probe skipped
        # (the live WETH fetch is not re-exercised here; step (0) returns exactly this dict)
        qi = _new("sell", "0xfixture", 10 ** 24, now_s, None)
        assert qi["status"] == "deferred" and all(
            v == "skipped" for k, v in qi["probes"].items() if k != "reserves")
        print("  (i) skeleton is deferred with every probe skipped")
        print("  whole-unit strings:", _whole_str(10 ** 24, 18), _whole_str(10 ** 16, 18),
              _whole_str(123456789, 6))
        assert _whole_str(10 ** 24, 18) == "1000000" and _whole_str(10 ** 16, 18) == "0.01"
        assert _whole_str(123456789, 6) == "123.456789"
    finally:
        rpc.v2_pair, rpc.get_amounts_out, rpc.v2_reserves = orig["v2_pair"], orig["gao"], orig["res"]
        kyber.route, scanhood.quote, rpc.multicall3 = orig["kyber"], orig["sh"], orig["mc"]
        rpc.decimals = orig["decimals"]
        if orig["dec"] is not None:
            rpc._decimals_ex = orig["dec"]
    print("  OK")
