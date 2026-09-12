"""
GeckoTerminal v2 source for network config.GT_NETWORK ("robinhood").

What this source UNIQUELY provides on Robinhood Chain (survey explore_agent6.md §2a):
  • new_pools(page)  — the newest-pools discovery feed with DISTINCT buyer/seller counts
                       (transactions.{m5,h1,...}.{buys,sells,buyers,sellers}) and the dex id
                       (bonding-curve pools show up as 'pons-v2' BEFORE graduation).
  • token_info(addr) — the richest free rug-gate record on this chain: holder count + top-10
                       concentration (with its own staleness stamp, measured 8 min .. 23 h),
                       is_honeypot, developer address + holding %, launchpad graduation
                       progress (the pump.fun-migration analogue), gt_score, gt_verified.
  • token_attrs / token_pools — supply, price, fdv and the pool list with the is_base flag.
  • pool_ohlcv       — minute/hour/day bars for the price-path lab.

Failure semantics (the http_client three-way contract, honoured on every call):
  parsed dict → ok;  NOT_FOUND → the server answered 400/404 ('GT does not index this');
  None → deferred (network / 429 / 5xx) — the caller must retry later and NEVER score it.
  List-returning functions return [] on deferred (a discovery feed that is dark simply
  contributes nothing this run; http_client already printed the failure).

Rate limit: the free tier is a hard ~30 req/min PER IP and, once exceeded, 429s for the rest
of the window (a 10-call burst got 429 on every call). http_client throttles this host at
config.GECKOTERMINAL_RATE_HZ and backs a 429 off by 20 s × attempt — so a call here may block
for a while; it never fails loudly. Every function here never raises on bad data: one bad
token must never kill a run. No wall-clock in any compute path: timestamps are PARSED to
epoch floats, ages are for the caller to compute from its own now_s.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config                                                    # noqa: E402
from http_client import get_json, NOT_FOUND, is_absent, is_deferred  # noqa: E402

from datetime import datetime, timezone                          # noqa: E402

BASE = "https://api.geckoterminal.com/api/v2"
_FOREVER = config.FOREVER_CACHE_DAYS * 86400
# new_pools turns over ~582 pools/hour (survey §5: pages 1+2 spanned 4.1 min), so a page is
# stale after a minute; 60 s also lets the discovery stage and a re-run within the same
# minute share one call against the shared 30/min budget. config has no constant for it yet.
NEW_POOLS_CACHE_S = 60
_ID_PREFIX = f"{config.GT_NETWORK}_"     # relationship ids look like "robinhood_0xabc..."
# GT quote legs on this chain are NOT only WETH: page 1 on 2026-09-12 showed pons-v2 pools
# quoted in native ETH as 0xeeee…eeee (the ERC-7528 sentinel), a uniswap-v4 pool quoted as
# 0x0000…0000, plus USDG / RDDT / NVDA / djt stock legs. config.QUOTE_TOKENS holds only WETH,
# so the two native sentinels are added here until config carries them; a stock-quoted pool
# still resolves correctly because its BASE is the launch token.
_NATIVE_SENTINELS = {"0x0000000000000000000000000000000000000000",
                     "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"}
_QUOTE_LIKE = {q.lower() for q in config.QUOTE_TOKENS} | _NATIVE_SENTINELS


# ── tiny parsers (never raise) ───────────────────────────────────────────────────
def _f(x, default=None):
    """float or `default` — GT ships numbers as strings ('81.0971'); '' / None / junk → default."""
    try:
        return default if x is None or x == "" else float(x)
    except (TypeError, ValueError):
        return default


def _i(x, default=None):
    try:
        return default if x is None or x == "" else int(x)
    except (TypeError, ValueError):
        return default


def _b(x):
    """GT's tri-state booleans: false / true / 'unknown' → False / True / None."""
    if isinstance(x, bool):
        return x
    if isinstance(x, str):
        s = x.strip().lower()
        if s in ("true", "yes", "1"):
            return True
        if s in ("false", "no", "0"):
            return False
    return None


def _iso_ts(s) -> float | None:
    """ISO-8601 → epoch seconds, or None. Pure parsing (no wall clock): 3.9's fromisoformat
    rejects the trailing 'Z' GT emits ('2026-09-11T02:47:31Z'), hence the replace; a naive
    stamp is taken as UTC, which is what GT means."""
    if not s or not isinstance(s, str):
        return None
    try:
        dt = datetime.fromisoformat(s.strip().replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except (ValueError, TypeError, OverflowError):
        return None


def _addr_from_id(gt_id) -> str | None:
    """'robinhood_0xAbC…' → '0xabc…' (lowercase); anything else → None."""
    if not isinstance(gt_id, str) or "_" not in gt_id:
        return None
    return gt_id.split("_", 1)[1].lower() or None


def _rel_id(item: dict, name: str):
    return (((item.get("relationships") or {}).get(name) or {}).get("data") or {}).get("id")


def _attrs(d):
    """data.attributes of a single-resource response, or {} (None/NOT_FOUND/malformed)."""
    if not d or is_absent(d) or not isinstance(d, dict):
        return {}
    data = d.get("data")
    return (data.get("attributes") or {}) if isinstance(data, dict) else {}


# ── public API ───────────────────────────────────────────────────────────────────
def new_pools(page: int = 1, network: str = config.GT_NETWORK) -> list[dict]:
    """Newest pools first (the discovery feed). Each row:
      {pool, token, symbol, name, created_at (ISO str|None), created_ts (epoch float|None),
       reserve_usd, fdv_usd, mcap_usd, dex, is_base, buys_m5, sells_m5, buyers_m5, sellers_m5,
       buys_h1, sells_h1, price_chg_h1}
    `token` is the BASE token (lowercase) unless the base leg is a quote token
    (config.QUOTE_TOKENS), in which case the quote leg is the candidate and is_base=False.
    `name` is the pool name GT reports ('SYM / WETH') — new_pools carries no token name.
    Counts are int|None (None = GT did not report the window). [] on deferred."""
    cache = os.path.join(config.CACHE_DIR, f"gt_new_pools_{network}_p{int(page)}.json")
    d = get_json(f"{BASE}/networks/{network}/new_pools?page={int(page)}",
                 cache_path=cache, max_age_sec=NEW_POOLS_CACHE_S)
    if is_deferred(d) or is_absent(d) or not isinstance(d, dict):
        return []
    out = []
    for it in (d.get("data") or []):
        try:
            a = it.get("attributes") or {}
            base = _addr_from_id(_rel_id(it, "base_token"))
            quote = _addr_from_id(_rel_id(it, "quote_token"))
            is_base = not (base and base in _QUOTE_LIKE)
            token = base if is_base else quote
            # pool name is 'SYM / QUOTE' (V3 appends a fee tier: 'Dojo / WETH 0.01%')
            parts = [p.strip().split(" ")[0] for p in str(a.get("name") or "").split(" / ")]
            symbol = (parts[0] if is_base else (parts[1] if len(parts) > 1 else parts[0])) or "?"
            tx = a.get("transactions") or {}
            m5, h1 = tx.get("m5") or {}, tx.get("h1") or {}
            created = a.get("pool_created_at")
            out.append({
                "pool": (a.get("address") or "").lower() or None,
                "token": token, "symbol": symbol, "name": a.get("name"),
                "created_at": created, "created_ts": _iso_ts(created),
                "reserve_usd": _f(a.get("reserve_in_usd"), 0.0),
                "fdv_usd": _f(a.get("fdv_usd")),
                "mcap_usd": _f(a.get("market_cap_usd")),
                "dex": _rel_id(it, "dex"), "is_base": is_base,
                "buys_m5": _i(m5.get("buys")), "sells_m5": _i(m5.get("sells")),
                "buyers_m5": _i(m5.get("buyers")), "sellers_m5": _i(m5.get("sellers")),
                "buys_h1": _i(h1.get("buys")), "sells_h1": _i(h1.get("sells")),
                "price_chg_h1": _f((a.get("price_change_percentage") or {}).get("h1")),
            })
        except Exception:
            continue      # one malformed row never drops the page
    return out


def token_info(addr: str, network: str = config.GT_NETWORK):
    """GET /tokens/{addr}/info → the pass-2 rug-gate record (survey §2a), or NOT_FOUND
    (GT does not index the token — negatively cached, it is a stable fact for a run) or
    None (deferred). Keys:
      holders_count:int|None, top10_pct_gt:float|None, holders_updated_at:str|None,
      holders_updated_ts:float|None, is_honeypot_gt:bool|None (false/true/'unknown'→None),
      developer_address:str|None (lowercase), dev_holding_pct_gt:float|None,
      launchpad:{graduation_pct, completed:bool|None, completed_at, completed_ts,
                 migrated_pool}|None,
      gt_score:float|None, gt_score_details:dict|None, gt_verified:bool|None,
      twitter, telegram, websites:list
    holders_updated_* is shipped because the snapshot is 8 min .. 23 h stale (measured on
    six tokens) — the caller decides freshness from ITS now_s. mint_authority /
    freeze_authority are Solana fields, always null on EVM, deliberately ignored."""
    addr = (addr or "").lower()
    cache = os.path.join(config.CACHE_DIR, f"gt_info_{network}_{addr}.json")
    d = get_json(f"{BASE}/networks/{network}/tokens/{addr}/info",
                 cache_path=cache, max_age_sec=config.INFO_CACHE_MIN * 60, cache_404=True)
    if is_deferred(d):
        return None
    if is_absent(d):
        return NOT_FOUND
    a = _attrs(d)
    if not a:
        return None      # 200 with no attributes = unparseable, treat as deferred, not absent
    try:
        h = a.get("holders") or {}
        dist = h.get("distribution_percentage") or {}
        lp = a.get("launchpad_details")
        launchpad = None
        if isinstance(lp, dict):
            launchpad = {"graduation_pct": _f(lp.get("graduation_percentage")),
                         "completed": _b(lp.get("completed")),
                         "completed_at": lp.get("completed_at"),
                         "completed_ts": _iso_ts(lp.get("completed_at")),
                         "migrated_pool": (lp.get("migrated_destination_pool_address") or None)}
            if launchpad["migrated_pool"]:
                launchpad["migrated_pool"] = str(launchpad["migrated_pool"]).lower()
        dev = a.get("developer_address")
        details = a.get("gt_score_details")
        return {
            "holders_count": _i(h.get("count")),
            "top10_pct_gt": _f(dist.get("top_10")),
            "holders_updated_at": h.get("last_updated"),
            "holders_updated_ts": _iso_ts(h.get("last_updated")),
            "is_honeypot_gt": _b(a.get("is_honeypot")),
            "developer_address": str(dev).lower() if dev else None,
            "dev_holding_pct_gt": _f(a.get("developer_holding_percentage")),
            "launchpad": launchpad,
            "gt_score": _f(a.get("gt_score")),
            "gt_score_details": details if isinstance(details, dict) else None,
            "gt_verified": _b(a.get("gt_verified")),
            "twitter": a.get("twitter_handle") or None,
            "telegram": a.get("telegram_handle") or None,
            "websites": [w for w in (a.get("websites") or []) if isinstance(w, str)],
        }
    except Exception:
        return None


def token_attrs(addr: str, network: str = config.GT_NETWORK):
    """GET /tokens/{addr} → {symbol, name, decimals:int|None, total_supply (HUMAN units, from
    normalized_total_supply — GT's raw `total_supply` is wei-scaled), price_usd, fdv_usd,
    market_cap_usd} (floats or None), NOT_FOUND, or None. Market snapshot → ENRICH cache."""
    addr = (addr or "").lower()
    cache = os.path.join(config.CACHE_DIR, f"gt_tok_{network}_{addr}.json")
    d = get_json(f"{BASE}/networks/{network}/tokens/{addr}",
                 cache_path=cache, max_age_sec=config.ENRICH_CACHE_MIN * 60)
    if is_deferred(d):
        return None
    if is_absent(d):
        return NOT_FOUND
    a = _attrs(d)
    if not a:
        return None
    return {"symbol": a.get("symbol"), "name": a.get("name"),
            "decimals": _i(a.get("decimals")),
            "total_supply": _f(a.get("normalized_total_supply")),
            "price_usd": _f(a.get("price_usd")),
            "fdv_usd": _f(a.get("fdv_usd")),
            "market_cap_usd": _f(a.get("market_cap_usd"))}


def token_pools(addr: str, network: str = config.GT_NETWORK) -> list[dict]:
    """Pools trading a token: [{pool, dex, reserve_usd, is_base}]. `is_base` matters for
    OHLCV: a pool's price series is quoted for its BASE token, so pools where our token
    is the quote leg would report the OTHER token's price (a $1,888 'high' regression on
    MIZUKARA came from exactly this). Addresses lowercase. [] on deferred/absent."""
    addr = (addr or "").lower()
    cache = os.path.join(config.CACHE_DIR, f"gt_pools_{network}_{addr}.json")
    d = get_json(f"{BASE}/networks/{network}/tokens/{addr}/pools",
                 cache_path=cache, max_age_sec=config.INFO_CACHE_MIN * 60)
    if is_deferred(d) or is_absent(d) or not isinstance(d, dict):
        return []
    out = []
    for it in (d.get("data") or []):
        try:
            a = it.get("attributes") or {}
            base = _addr_from_id(_rel_id(it, "base_token")) or ""
            out.append({"pool": (a.get("address") or "").lower() or None,
                        "dex": _rel_id(it, "dex"),
                        "reserve_usd": _f(a.get("reserve_in_usd"), 0.0),
                        "is_base": base == addr})
        except Exception:
            continue
    return out


def pool_ohlcv(pool: str, timeframe: str = "day", aggregate: int = 1,
               limit: int = config.PATHS_LIMIT, network: str = config.GT_NETWORK,
               cache_path: str | None = None, max_age_sec: float | None = None):
    """GET /pools/{pool}/ohlcv/{timeframe}?aggregate=N&limit=L&currency=usd →
    [{ts:int, o, h, l, c, v}] sorted OLDEST FIRST with duplicate timestamps collapsed (GT
    ships newest-first, omits no-trade bars, and has repeated a bar), or NOT_FOUND / None.
    `v` is None when the element is absent: a bar whose volume is UNREPORTED must never be
    conflated with a real 0.0 (the peak-mcap era treated both as zero and rejected real
    wicks). Uncached by default — the price-path lab caches under cache/paths/ with its own
    7-day policy; pass cache_path/max_age_sec to opt in."""
    timeframe = timeframe if timeframe in ("minute", "hour", "day") else "day"
    url = (f"{BASE}/networks/{network}/pools/{(pool or '').lower()}/ohlcv/{timeframe}"
           f"?aggregate={int(aggregate)}&limit={int(limit)}&currency=usd")
    d = get_json(url, cache_path=cache_path, max_age_sec=max_age_sec)
    if is_deferred(d):
        return None
    if is_absent(d):
        return NOT_FOUND
    rows = _attrs(d).get("ohlcv_list")
    if not isinstance(rows, list):
        return None
    # GT returns NEWEST first, skips bars with no trades, and can repeat a timestamp: the
    # MIZUKARA daily series carried the 1788739200 bar TWICE, byte-identical (2026-09-12).
    # A repeated bar would double-count volume in the path lab, so collapse on ts, keeping
    # the row with the larger reported volume (a partial vs a completed bar).
    by_ts: dict = {}
    for r in rows:
        try:
            if not isinstance(r, (list, tuple)) or len(r) < 5:
                continue
            ts = _i(r[0])
            o, h, l, c = (_f(r[1]), _f(r[2]), _f(r[3]), _f(r[4]))
            if ts is None or None in (o, h, l, c):
                continue
            bar = {"ts": ts, "o": o, "h": h, "l": l, "c": c,
                   "v": _f(r[5]) if len(r) > 5 else None}
            prev = by_ts.get(ts)
            if prev is None or (bar["v"] or 0.0) > (prev["v"] or 0.0):
                by_ts[ts] = bar
        except Exception:
            continue
    return [by_ts[k] for k in sorted(by_ts)]


if __name__ == "__main__":
    # ≤ 6 GT calls at 0.25 Hz ≈ 25 s. Cold caches → 5 network calls.
    print("geckoterminal smoke (network=%s)" % config.GT_NETWORK)

    np_ = new_pools(1)
    print(f"  new_pools(1): {len(np_)} rows")
    for r in np_[:3]:
        print(f"    {r['token']}  {r['symbol']!r:<14} dex={r['dex']!r:<12} buyers_m5={r['buyers_m5']} "
              f"created_ts={r['created_ts']}")
    if np_:
        assert 1 <= len(np_) <= 20, len(np_)
        assert all(r["token"] and r["token"].startswith("0x") for r in np_), "token addr missing"
        assert all(r["token"] not in _QUOTE_LIKE for r in np_), "candidate is a quote token"
        assert all(r["created_ts"] is None or r["created_ts"] > 1.7e9 for r in np_), "bad created_ts"
    else:
        print("  (new_pools deferred/empty — GT dark this run; feed contract holds: [])")

    info = token_info(config.MIZUKARA)
    print(f"  token_info(MIZUKARA): {'NOT_FOUND' if is_absent(info) else 'deferred' if info is None else 'ok'}")
    if info and not is_absent(info):
        print(f"    holders={info['holders_count']} top10={info['top10_pct_gt']}% "
              f"updated={info['holders_updated_at']} (ts={info['holders_updated_ts']}) "
              f"honeypot={info['is_honeypot_gt']} dev={info['developer_address']} "
              f"dev%={info['dev_holding_pct_gt']} gt_score={info['gt_score']} "
              f"verified={info['gt_verified']} launchpad={info['launchpad']}")
        assert info["holders_count"] and info["holders_count"] > 1000, info["holders_count"]
        assert info["top10_pct_gt"] is not None and 0 < info["top10_pct_gt"] < 100, info["top10_pct_gt"]
        assert info["gt_verified"] is True, info["gt_verified"]
        assert info["holders_updated_ts"] is None or info["holders_updated_ts"] > 1.7e9
    else:
        assert info is None, "MIZUKARA must be indexed by GT"   # deferred is tolerable, absent is not

    a = token_attrs(config.MIZUKARA)
    if a and not is_absent(a):
        print(f"    attrs: {a['symbol']} decimals={a['decimals']} supply={a['total_supply']:,.0f} "
              f"price=${a['price_usd']:.6g} fdv=${a['fdv_usd']:,.0f}")
        assert a["symbol"] == "MIZUKARA" and a["decimals"] == 18
        assert 9e8 < a["total_supply"] < 1.1e9, a["total_supply"]   # 1B, human units
    else:
        print(f"    attrs: {a!r}")

    pools = token_pools(config.MIZUKARA)
    print(f"  token_pools(MIZUKARA): {len(pools)} pools, {sum(1 for p in pools if p['is_base'])} base-side")
    if pools:
        hit = [p for p in pools if p["pool"] == config.MIZUKARA_POOL.lower()]
        assert hit and hit[0]["is_base"], f"MIZUKARA_POOL missing or not base-side: {hit}"
        print(f"    MIZUKARA_POOL dex={hit[0]['dex']} reserve=${hit[0]['reserve_usd']:,.0f} is_base={hit[0]['is_base']}")

    bars = pool_ohlcv(config.MIZUKARA_POOL, "day", 1, 5)
    if bars and not is_absent(bars):
        print(f"  pool_ohlcv(MIZUKARA_POOL, day, 1, 5): {len(bars)} bars, "
              f"ts {bars[0]['ts']} → {bars[-1]['ts']}; last={bars[-1]}")
        assert all(bars[i]["ts"] < bars[i + 1]["ts"] for i in range(len(bars) - 1)), "not ascending"
        assert all(b["h"] >= b["l"] for b in bars)
    else:
        print(f"  pool_ohlcv: {bars!r}")
    print("ok")
