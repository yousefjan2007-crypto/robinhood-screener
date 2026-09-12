"""
ScanHood (https://scanhood.xyz/docs, /openapi.json) — a free, keyless safety + quote + launch
layer purpose-built for Robinhood Chain (4663). Free tier ≈ 5 req/s per IP; config pins 3 Hz.

What this source UNIQUELY provides (nothing else on this chain does all of it, verified
2026-09-12 in the endpoint survey, explore_agent6.md §2b):

  scan(addr)             GET /api/scan?token=      composite verdict PASS|CAUTION|DANGER from a
                         REAL callStatic buy+sell honeypot simulation (roundTripLossPct), LP-lock
                         status, contract verification, deployer reputation, RWA-impostor check.
  quote(addr, side, amt) GET /api/quote            best-route (V2/V3) swap quote vs WETH, exact
                         amounts, names the venue/pool/fee — a paper-fill source that lives on the
                         same host as the verdict.
  launch_feed()          GET /api/robinhood-noxa-launches.json — new/trending/graduating/graduated
                         rows across the locked-LP launchpads (Pons/LaunchHood/ScanHood) with a
                         per-row safety verdict and the factory→platform map.
  stock_tokens()         GET /api/robinhood-stocks.json — the 194 OFFICIAL tokenized stocks/ETFs.
                         Infrastructure, not launches: they must never mark a deployer "proven".
  deployer_reputation()  GET /api/deployer-reputation.json — serial-rugger / spam-launcher overlay.

FAILURE SEMANTICS (the http_client three-way contract, honoured everywhere here):
  * A verdict we do not recognise, a 400/404, or a dark host all make scan() return None and the
    safety gate PASSES THROUGH, stated as "unavailable" in the alert — the solana GMGN contract.
    We never fail a token on this source's silence, only on a positive DANGER it actually said.
  * "N/A degrades to pass-through": on a minutes-old token the live verdict is CAUTION with
    sellable=None and the flag 'could not simulate a sell (no pool / new token)'. That is NOT a
    honeypot finding — it is the simulator saying "no pool yet" — and callers must treat
    sellable=None as unknown, not as False. (Observed on MIZUKARA itself on 2026-09-12 when the
    market snapshot fell back to `_source: "onchain"`; the same host's /api/quote sold fine.)
  * quote(): {"error": ...} in the body means "no route" → NOT_FOUND (an explicit absence, not a
    fill of zero). None means deferred: a paper trade must be retried, never filled at a guess.
    Quotes are NEVER cached — a stale quote is a fabricated fill.
  * launch_feed() returns {} when deferred/absent (a feed with no rows is "nothing new", a missing
    feed is "unknown" — the caller must not confuse the two, so {} carries no 'updated_at').
  * stock_tokens() returns an empty set when the feed is unavailable; the caller's symbol backstop
    (config.EXCLUDE_SYMBOLS) still applies.
Nothing here raises on bad data: one bad token never kills a run.
"""
from __future__ import annotations

import os
import sys
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config                                                        # noqa: E402
from http_client import NOT_FOUND, get_json, is_absent, is_deferred  # noqa: E402

# Only the quote URL lives in config today; the host is derived from it so a future
# SCANHOOD_BASE constant wins automatically (see 'deviations' in the build report).
_QUOTE_URL = config.SCANHOOD_QUOTE_URL
SCANHOOD_BASE = getattr(config, "SCANHOOD_BASE",
                        "{0.scheme}://{0.netloc}".format(urllib.parse.urlparse(_QUOTE_URL)))
SCAN_URL = f"{SCANHOOD_BASE}/api/scan"
LAUNCH_FEED_URL = f"{SCANHOOD_BASE}/api/robinhood-noxa-launches.json"
STOCKS_URL = f"{SCANHOOD_BASE}/api/robinhood-stocks.json"
DEPLOYER_REP_URL = f"{SCANHOOD_BASE}/api/deployer-reputation.json"
OHLCV_URL = f"{SCANHOOD_BASE}/api/ohlcv"

# Cache TTLs — module fallbacks until config grows named constants for them.
FEED_CACHE_S = getattr(config, "SCANHOOD_FEED_CACHE_S", 120)         # cron-refreshed feed
STOCKS_CACHE_S = getattr(config, "SCANHOOD_STOCKS_CACHE_S", 24 * 3600)  # 194 issues; changes rarely
REP_CACHE_S = getattr(config, "SCANHOOD_REP_CACHE_S", 6 * 3600)      # 13.5 MB static dump

VERDICTS = ("PASS", "CAUTION", "DANGER")
FEED_SECTIONS = ("new", "trending", "graduating", "graduated")
SIDES = ("buy", "sell")


# ── helpers ──────────────────────────────────────────────────────────────────────
def _norm_addr(addr) -> str:
    """Lower-case the address: the old cache held two identical MIZUKARA files differing only
    in checksum case, each costing its own network call."""
    return str(addr or "").strip().lower()


def _f(x) -> float | None:
    try:
        if x is None or isinstance(x, bool):
            return None
        return float(x)
    except (TypeError, ValueError):
        return None


def _i(x) -> int | None:
    try:
        if x is None or isinstance(x, bool):
            return None
        return int(x)
    except (TypeError, ValueError):
        return None


def _b(x) -> bool | None:
    return x if isinstance(x, bool) else None


def _s(x) -> str | None:
    return str(x) if isinstance(x, str) and x else None


def _lower_or_none(x) -> str | None:
    return _norm_addr(x) or None


# ── 1. safety scan ───────────────────────────────────────────────────────────────
def scan(addr: str) -> dict | None:
    """Composite safety scan, or None when unavailable (→ the gate passes through).

    Returns a flat dict:
      verdict              'PASS' | 'CAUTION' | 'DANGER'  (anything else → None, not a verdict)
      sellable             bool | None   None = the sell sim could not run ("no pool / new token")
      round_trip_loss_pct  float | None  buy→sell loss from the callStatic simulation
      lp                   {type, status, safe, detail} | None
      launchpad            str | None
      deployer             {address, launched, risk, label} | None  (address lower-cased)
      flags                list[str]   the flag messages; levels stay in raw['flags']
      verified             bool | None  source verified on Blockscout (per ScanHood)
      contract_template    str | None
      rwa                  bool | None  RWA / tokenized-stock impostor check
      market               dict | None  ScanHood's own market snapshot (pool, dex, liq, priceUsd…)
      raw                  the full response
    Cached INFO_CACHE_MIN (the sim result and LP status are mutable per-token facts).
    """
    a = _norm_addr(addr)
    if not a:
        return None
    cache = os.path.join(config.CACHE_DIR, f"sh_{a}.json")
    d = get_json(f"{SCAN_URL}?token={a}", cache_path=cache,
                 max_age_sec=config.INFO_CACHE_MIN * 60)
    if is_deferred(d) or is_absent(d) or not isinstance(d, dict):
        return None
    verdict = str(d.get("verdict") or "").upper()
    if verdict not in VERDICTS:
        return None                      # unknown verdict = no verdict: pass through, never gate

    lp = d.get("lp")
    lp_out = None
    if isinstance(lp, dict):
        lp_out = {"type": _s(lp.get("type")), "status": _s(lp.get("status")),
                  "safe": _b(lp.get("safe")), "detail": _s(lp.get("detail"))}
    dep = d.get("deployer")
    dep_out = None
    if isinstance(dep, dict):
        dep_out = {"address": _lower_or_none(dep.get("address")),
                   "launched": _i(dep.get("launched")),
                   "risk": dep.get("risk") if isinstance(dep.get("risk"), (str, int, float)) else None,
                   "label": _s(dep.get("label"))}
    flags: list = []
    for fl in d.get("flags") or []:
        if isinstance(fl, dict):
            msg = fl.get("msg") or fl.get("message")
            if msg:
                flags.append(str(msg))
        elif isinstance(fl, str) and fl:
            flags.append(fl)
    market = d.get("market") if isinstance(d.get("market"), dict) else None
    return {"verdict": verdict,
            "sellable": _b(d.get("sellable")),
            "round_trip_loss_pct": _f(d.get("roundTripLossPct")),
            "lp": lp_out,
            "launchpad": _s(d.get("launchpad")),
            "deployer": dep_out,
            "flags": flags,
            "verified": _b(d.get("verified")),
            "contract_template": _s(d.get("contractTemplate")),
            "rwa": _b(d.get("rwa")),
            "market": market,
            "raw": d}


# ── 2. swap quote (paper fills) ──────────────────────────────────────────────────
def quote(addr: str, side: str, amount_whole: str):
    """Read-only best-route swap quote vs WETH. NEVER cached.

    `amount_whole` is in WHOLE units of the token being sold/spent (a decimal string — "1000000"
    for a sell of 1M tokens, "0.01" for a buy with 0.01 WETH): verified 2026-09-12, a sell of
    "1000000" on an 18-decimal token echoed amountIn = 1e24.

    Returns {amount_out:int (raw units — WETH wei for a sell, token raw units for a buy),
             amount_in:int, decimals:int|None, venue, pool (lower), fee_bps:int|None, exact:bool,
             symbol, side, raw}
          | NOT_FOUND  — the server said no route ({"error": ...}: "no pool with liquidity")
          | None       — deferred (network/429/5xx); the caller must retry, never fill at a guess.
    Note: ScanHood signals "no route" with HTTP 422, which http_client retries and returns as
    None (only 400/404 are terminal there) — so today an explicit no-route usually surfaces as
    deferred; a 200 body carrying "error" is mapped to NOT_FOUND here.
    """
    a = _norm_addr(addr)
    side = str(side or "").lower()
    if not a or side not in SIDES:
        return NOT_FOUND                 # malformed request: no amount of retrying makes a route
    q = urllib.parse.urlencode({"token": a, "side": side, "amount": str(amount_whole)})
    d = get_json(f"{_QUOTE_URL}?{q}")    # no cache_path on purpose
    if is_deferred(d):
        return None
    if is_absent(d):
        return NOT_FOUND
    if not isinstance(d, dict):
        return None
    if d.get("error"):
        return NOT_FOUND
    out = _i(d.get("amountOut"))
    if out is None:
        return None                      # a 200 without an amount is not a quote
    return {"amount_out": out,
            "amount_in": _i(d.get("amountIn")),
            "decimals": _i(d.get("decimals")),
            "venue": _s(d.get("venue")),
            "pool": _lower_or_none(d.get("pool")),
            "fee_bps": _i(d.get("feeBps")),
            "exact": bool(d.get("exact")),
            "symbol": _s(d.get("symbol")),
            "side": side,
            "raw": d}


# ── 3. launch feed ───────────────────────────────────────────────────────────────
def _feed_row(r: dict) -> dict | None:
    """Normalise one feed row. Field names verified live 2026-09-12 (survey §2b + this module's
    smoke): token/symbol/pool/price/mcap/liquidity/vol24/vol1/buys24/sells24/launchTime/ageS/
    factory/platform/quoteSymbol/curve and an optional safety:{verdict,loss,unsafe}. `age_s` is
    the feed's own age AS OF the feed's updated_at (no wall-clock here); recompute from
    launch_time with the caller's now_s when freshness matters."""
    tok = _lower_or_none(r.get("token"))
    if not tok:
        return None
    safety = r.get("safety") if isinstance(r.get("safety"), dict) else {}
    return {"token": tok,
            "symbol": _s(r.get("symbol")),
            "pool": _lower_or_none(r.get("pool")),
            "price": _f(r.get("price")),
            "mcap": _f(r.get("mcap")),
            "liquidity": _f(r.get("liquidity")),
            "vol24": _f(r.get("vol24")),
            "vol1": _f(r.get("vol1")),
            "buys24": _i(r.get("buys24")),
            "sells24": _i(r.get("sells24")),
            "launch_time": _i(r.get("launchTime")),
            "age_s": _i(r.get("ageS")),
            "factory": _lower_or_none(r.get("factory")),
            "platform": _s(r.get("platform")),
            "quote_symbol": _s(r.get("quoteSymbol")),
            "safety_verdict": (str(safety.get("verdict")).upper()
                               if safety.get("verdict") else None),
            "safety_loss": _f(safety.get("loss")),
            "curve": _b(r.get("curve"))}


def launch_feed() -> dict:
    """The locked-LP launchpad feed (Pons / LaunchHood / ScanHood factories).

    Returns {"updated_at": int|None, "new": [...], "trending": [...], "graduating": [...],
             "graduated": [...], "factories": {factory_lower: platform}}
    — each section a list of normalised rows (see _feed_row) — or {} when deferred/absent so a
    missing feed can never read as "no launches". Cached 120 s: the feed is cron-refreshed and
    the 2-minute job cadence makes a shorter TTL pure waste of the 5 r/s budget.
    """
    cache = os.path.join(config.CACHE_DIR, "sh_noxa_launches.json")
    d = get_json(LAUNCH_FEED_URL, cache_path=cache, max_age_sec=FEED_CACHE_S)
    if is_deferred(d) or is_absent(d) or not isinstance(d, dict):
        return {}
    sections = d.get("sections") if isinstance(d.get("sections"), dict) else d
    out: dict = {"updated_at": _i(d.get("updated_at"))}
    for sec in FEED_SECTIONS:
        rows = []
        for r in sections.get(sec) or []:
            if isinstance(r, dict):
                n = _feed_row(r)
                if n:
                    rows.append(n)
        out[sec] = rows
    facts = d.get("factories") if isinstance(d.get("factories"), dict) else {}
    out["factories"] = {_norm_addr(k): str(v) for k, v in facts.items() if _norm_addr(k)}
    return out


# ── 4. static feeds ──────────────────────────────────────────────────────────────
def stock_tokens() -> set:
    """Addresses (lower) of the OFFICIAL tokenized stocks / ETFs (Robinhood's own issues, 194
    on 2026-09-12 under the `tokens` key). These are infrastructure, not dev launches, and must
    never mark a deployer as 'proven'. The feed's `impostors` list is deliberately NOT merged in:
    an impostor is a scam launch, exactly what the screener must keep scoring. Empty set when
    the feed is unavailable (the EXCLUDE_SYMBOLS backstop still applies)."""
    cache = os.path.join(config.CACHE_DIR, "sh_stocks.json")
    d = get_json(STOCKS_URL, cache_path=cache, max_age_sec=STOCKS_CACHE_S)
    out: set = set()
    if is_deferred(d) or is_absent(d):
        return out
    items = d if isinstance(d, list) else \
        ((d or {}).get("tokens") or (d or {}).get("stocks") or (d or {}).get("data") or [])
    for it in items if isinstance(items, list) else []:
        if isinstance(it, dict):
            a = it.get("token") or it.get("address") or it.get("contract")
            if a:
                out.add(_norm_addr(a))
    return out


def deployer_reputation() -> dict | None:
    """Static serial-rugger / spam-launcher feed → {wallet(lower): entry}, or None.

    This file is ~13.5 MB (measured 2026-09-12; the July cache on disk is 7.9 MB and growing),
    so the 6 h TTL is the floor, not a target: it is a NEGATIVE overlay (a 'proven' dev who is
    also a flagged serial rugger gets a warning line), never a per-run dependency. Call it from
    the attribution path at most once per run."""
    cache = os.path.join(config.CACHE_DIR, "sh_deployer_rep.json")
    d = get_json(DEPLOYER_REP_URL, cache_path=cache, max_age_sec=REP_CACHE_S)
    if is_deferred(d) or is_absent(d):
        return None
    if isinstance(d, dict):
        items = d.get("deployers") or d.get("data") or d
        if isinstance(items, list):
            return {_norm_addr(e.get("address")): e for e in items
                    if isinstance(e, dict) and e.get("address")}
        if isinstance(items, dict):
            return {_norm_addr(k): v for k, v in items.items()}
    if isinstance(d, list):
        return {_norm_addr(e.get("address")): e for e in d
                if isinstance(e, dict) and e.get("address")}
    return None


def ohlcv(pool: str, tf: str = "1d") -> list | None:
    """Fallback OHLCV bars for a pool GeckoTerminal has not indexed; None when unavailable.
    Closed bars are immutable but the last bar is not, hence the mutable-fact TTL."""
    p = _norm_addr(pool)
    if not p:
        return None
    cache = os.path.join(config.CACHE_DIR, f"sh_ohlcv_{p}_{tf}.json")
    d = get_json(f"{OHLCV_URL}?pool={p}&tf={tf}", cache_path=cache,
                 max_age_sec=config.INFO_CACHE_MIN * 60)
    if is_deferred(d) or is_absent(d):
        return None
    if isinstance(d, dict):
        d = d.get("candles") or d.get("ohlcv") or d.get("data")
    return d if isinstance(d, list) else None


# ── smoke test ───────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("robinhood_screener sources/scanhood")
    print(f"  base {SCANHOOD_BASE}  rate {config.HOST_RATE_HZ.get('scanhood.xyz')} Hz")

    # 1. scan
    s = scan(config.MIZUKARA)
    assert s is not None, "scan(MIZUKARA) unavailable — cannot smoke-test the verdict path"
    assert s["verdict"] in ("PASS", "CAUTION"), s["verdict"]
    print(f"  scan MIZUKARA: verdict={s['verdict']} sellable={s['sellable']} "
          f"round_trip_loss={s['round_trip_loss_pct']}% lp={s['lp']} "
          f"launchpad={s['launchpad']} verified={s['verified']} rwa={s['rwa']}")
    print(f"    contract_template={s['contract_template']!r}  deployer={s['deployer']}")
    print(f"    flags={s['flags']}")
    if s["sellable"] is None:
        # the N/A-degrades-to-pass-through rule: the sim did not run, which is not a finding
        assert any("could not simulate a sell" in f for f in s["flags"]), s["flags"]
        assert s["round_trip_loss_pct"] is None
        print("    NOTE: sell sim did not run (no pool / new token) — sellable None is UNKNOWN, "
              "not a honeypot; gate passes through")
    else:
        assert s["sellable"] is True, s["sellable"]
        assert s["round_trip_loss_pct"] is not None and 0 <= s["round_trip_loss_pct"] <= 10, \
            s["round_trip_loss_pct"]

    # 2. quote — sell 1,000,000 whole MIZUKARA for WETH
    q = quote(config.MIZUKARA, "sell", "1000000")
    assert isinstance(q, dict) and not is_absent(q), f"sell quote: {q!r}"
    assert q["amount_out"] > 0 and q["venue"], q
    print(f"  quote sell 1e6 MIZUKARA: amount_out={q['amount_out']} wei "
          f"({q['amount_out'] / 1e18:.9f} WETH) venue={q['venue']} pool={q['pool']} "
          f"fee_bps={q['fee_bps']} exact={q['exact']} decimals={q['decimals']}")
    if q["decimals"] is not None and q["amount_in"] is not None:
        assert q["amount_in"] == 10 ** 6 * 10 ** q["decimals"], q["amount_in"]
        print("    amount is WHOLE tokens: amountIn echoed as 1e6 * 10**decimals  ok")
    assert q["pool"] == config.MIZUKARA_POOL.lower(), q["pool"]
    bogus = quote("0x0000000000000000000000000000000000000001", "sell", "1")
    assert bogus is None or is_absent(bogus), bogus
    print(f"  quote bogus token → {'NOT_FOUND (absent: explicit no route)' if is_absent(bogus) else 'None (deferred: HTTP 422 is retried by http_client)'}")

    # 3. launch feed
    feed = launch_feed()
    assert feed, "launch_feed deferred"
    print(f"  launch_feed updated_at={feed['updated_at']} sections: " +
          " ".join(f"{k}={len(feed[k])}" for k in FEED_SECTIONS) +
          f" factories={len(feed['factories'])}")
    assert config.FACTORIES["pons"].lower() in feed["factories"], feed["factories"]
    unknown = {a: p for a, p in feed["factories"].items()
               if a not in {v.lower() for v in config.FACTORIES.values()}}
    print(f"    factories not in config.FACTORIES: {unknown or 'none'}")
    for sec in FEED_SECTIONS:
        if feed[sec]:
            r = feed[sec][0]
            print(f"    {sec}[0]: {r['symbol']} {r['token'][:10]}… platform={r['platform']} "
                  f"liq={r['liquidity']} age_s={r['age_s']} safety={r['safety_verdict']}/"
                  f"{r['safety_loss']} curve={r['curve']}")
            assert set(r) == {"token", "symbol", "pool", "price", "mcap", "liquidity", "vol24",
                              "vol1", "buys24", "sells24", "launch_time", "age_s", "factory",
                              "platform", "quote_symbol", "safety_verdict", "safety_loss",
                              "curve"}, sorted(r)

    # 4. stocks (24 h cache) — deployer_reputation() is 13.5 MB and not exercised here
    st = stock_tokens()
    assert len(st) > 100, len(st)
    print(f"  stock_tokens: {len(st)} official tokenized stocks  "
          f"(MIZUKARA excluded? {config.MIZUKARA.lower() not in st})")
    print("smoke ok")
