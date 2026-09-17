"""
Dexscreener source (free / no-auth) for Robinhood Chain (chain slug config.DEX_CHAIN).

What it uniquely provides: the cheap MARKET pass — one GET for up to 30 tokens returns, per
indexed pair, priceUsd / liquidity.usd / marketCap / fdv / volume.{h1,h6,h24} /
txns.{h1,h24}.{buys,sells} / priceChange / pairCreatedAt / labels, plus the token's
info.websites + info.socials (the input for the P3 social layer). It is also the WETH/USD
reference price (WETH's deepest pair is WETH/USDG v3, ~$29.7M liq, verified 2026-09-12) and
two discovery hedges (token-profiles / token-boosts "latest" feeds, filtered to this chain).
We always pick the deepest-liquidity pair where the token is the BASE leg — the pool you'd
actually exit through.

Failure semantics (the http_client three-way contract, honoured per ADDRESS):
    ok        — a normalised market dict (see _normalize for the exact keys)
    absent    — NOT_FOUND: the API answered and no pair has this token as base. tokens/v1
                never 404s on an unknown address; it simply omits it from the list
                (verified live: [MIZUKARA, 0x…01] → a 1-element list). So "absent" here is
                "answered without it", never an HTTP status.
    deferred  — None: network / 429 / 5xx. A deferred chunk defers EVERY address in it;
                nothing in a deferred chunk is ever scored, dead-marked or ledgered.
No function raises on bad data: one malformed pair never kills a run. No wall clock in
any compute path — pair age is computed from the caller's now_s (the same now_s that
stamps the ledger row, so age and timestamp agree).
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config                                                    # noqa: E402
from http_client import NOT_FOUND, get_json, is_absent, is_deferred  # noqa: E402

BASE = "https://api.dexscreener.com"
CHUNK = 30                       # tokens/v1 accepts up to 30 comma-separated addresses

# Keys every market dict carries — FEATURE_FIELDS' market block is a subset of these. The three
# m5 keys are the per-tick FLOW features the keeper's paper book reads (policies.FLOW_FEATURES);
# they are not in FEATURE_FIELDS and no band or gate reads them.
MARKET_KEYS = ("symbol", "name", "price_usd", "liq_usd", "mcap", "fdv", "vol_h1", "vol_h6",
               "vol_h24", "buys_h1", "sells_h1", "buys_h24", "sells_h24", "vol_m5", "buys_m5",
               "sells_m5", "price_chg_h1", "pair", "pair_created_ms", "pair_age_min", "dex",
               "pair_labels", "url", "socials")


# ── helpers ──────────────────────────────────────────────────────────────────────
def _f(x, default=None):
    """float() that never raises. `default` is the ABSENCE value: 0.0 for counts/volumes
    (no trades is a real zero), None for prices/caps/liquidity (unknown is not zero — a
    0.0 price would ledger as a -100% rug)."""
    try:
        if x is None or x == "":
            return default
        return float(x)
    except (TypeError, ValueError):
        return default


def _i(x) -> int:
    v = _f(x, 0.0)
    try:
        return int(v)
    except (TypeError, ValueError, OverflowError):
        return 0


def _addr_of(tok) -> str:
    return str((tok or {}).get("address") or "").lower()


def _pick_pair(pairs, addr: str):
    """Deepest-liquidity pair where `addr` is the BASE token (the pool you'd exit through).
    A pair where the token is the quote leg is somebody else's market, not this token's."""
    best, best_liq = None, -1.0
    a = addr.lower()
    for p in pairs or []:
        if not isinstance(p, dict) or _addr_of(p.get("baseToken")) != a:
            continue
        liq = _f((p.get("liquidity") or {}).get("usd"), 0.0)
        if liq > best_liq:
            best, best_liq = p, liq
    return best


def _socials(p: dict) -> dict:
    info = p.get("info") or {}
    out = {"website": None, "twitter": None, "telegram": None}
    for w in info.get("websites") or []:
        if isinstance(w, dict) and w.get("url") and not out["website"]:
            out["website"] = w["url"]
    for s in info.get("socials") or []:
        if not isinstance(s, dict):
            continue
        t, u = str(s.get("type", "")).lower(), s.get("url")
        if t in out and u and not out[t]:
            out[t] = u
    return out


def _normalize(p: dict, addr: str, now_s: float) -> dict:
    """Flatten one Dexscreener pair into the market dict. Field names per the 2026-09-12
    survey (explore_agent6.md §2): priceUsd, liquidity.usd, marketCap, fdv,
    volume.{h1,h6,h24}, txns.{h1,h24}.{buys,sells}, priceChange.h1 (often ABSENT — the
    live MIZUKARA pair carried only priceChange.h24), pairCreatedAt (ms), labels, dexId.
    pair_age_min is None when pairCreatedAt is missing, else derived from now_s only.
    The m5 window (volume.m5, txns.m5 — present on 45/45 cached pairs, 2026-09-17) is the
    flow feature set: absent is None on all three, NEVER 0.0, because a flow rule that read
    "unknown" as "no flow" would exit on every dark tick."""
    liq = p.get("liquidity") or {}
    vol = p.get("volume") or {}
    txns = p.get("txns") or {}
    h1, h24 = txns.get("h1") or {}, txns.get("h24") or {}
    m5 = txns.get("m5")
    chg = p.get("priceChange") or {}
    base = p.get("baseToken") or {}
    created_ms = p.get("pairCreatedAt")
    created_ms = int(created_ms) if isinstance(created_ms, (int, float)) and created_ms > 0 else None
    age_min = ((now_s * 1000.0 - created_ms) / 60000.0) if created_ms else None
    labels = p.get("labels")
    return {
        "symbol": str(base.get("symbol") or "?"),
        "name": str(base.get("name") or ""),
        "price_usd": _f(p.get("priceUsd")),
        "liq_usd": _f(liq.get("usd")),
        "mcap": _f(p.get("marketCap")),
        "fdv": _f(p.get("fdv")),
        "vol_h1": _f(vol.get("h1"), 0.0),
        "vol_h6": _f(vol.get("h6"), 0.0),
        "vol_h24": _f(vol.get("h24"), 0.0),
        "buys_h1": _i(h1.get("buys")),
        "sells_h1": _i(h1.get("sells")),
        "buys_h24": _i(h24.get("buys")),
        "sells_h24": _i(h24.get("sells")),
        "vol_m5": _f(vol.get("m5")),
        "buys_m5": _i(m5.get("buys")) if isinstance(m5, dict) else None,
        "sells_m5": _i(m5.get("sells")) if isinstance(m5, dict) else None,
        "price_chg_h1": _f(chg.get("h1")),
        "pair": p.get("pairAddress"),
        "pair_created_ms": created_ms,
        "pair_age_min": age_min,
        "dex": str(p.get("dexId") or ""),
        "pair_labels": [str(x) for x in labels] if isinstance(labels, list) else [],
        "url": str(p.get("url") or ""),
        "socials": _socials(p),
    }


# ── per-address cache (shared format with get_json's cache) ──────────────────────
# tokens/v1 is one URL for 30 addresses, so the client's URL-keyed cache would key a whole
# chunk; the ledger/rechecks want per-token reuse. Each address gets cache/dex_{addr}.json
# holding ITS pairs (a list) or the {"__absent__": true} marker — the same format get_json
# writes for the single-address URL, so token_pairs and enrich_many share files.
def _cache_path(addr: str) -> str:
    return os.path.join(config.CACHE_DIR, f"dex_{addr.lower()}.json")


def _cache_get(addr: str, now_s: float, max_age_sec: float):
    """(pairs | NOT_FOUND, hit). Freshness is judged against the caller's now_s, not the
    wall clock, so a re-run with a pinned now_s sees the same cache decisions."""
    path = _cache_path(addr)
    try:
        if max_age_sec <= 0 or not os.path.exists(path):
            return None, False
        if now_s - os.path.getmtime(path) > max_age_sec:
            return None, False
        with open(path) as f:
            obj = json.load(f)
    except Exception:
        return None, False
    if isinstance(obj, dict) and obj.get("__absent__") is True:
        return NOT_FOUND, True
    if isinstance(obj, list):
        return obj, True
    return None, False


def _cache_put(addr: str, obj) -> None:
    path = _cache_path(addr)
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(obj, f)
        os.replace(tmp, path)
    except Exception:
        try:
            os.remove(tmp)
        except Exception:
            pass


# ── public API ───────────────────────────────────────────────────────────────────
def enrich_many(addrs, now_s: float, max_age_sec: float | None = None) -> dict:
    """Batch market pass → {"ok": {addr: market}, "absent": set, "deferred": set}, keys
    lowercased. GET tokens/v1/{chain}/{a1,...,a30}: a chunk that answers makes every
    address with a base pair ok and the rest absent; a chunk that fails (None) defers
    every address in it — the sibling project fabricated 472 dead rows by calling a
    failed batch "no pair". max_age_sec None → config.ENRICH_CACHE_MIN*60; 0 → always
    fresh (forward snapshots must never read a cached tick)."""
    if max_age_sec is None:
        max_age_sec = config.ENRICH_CACHE_MIN * 60
    out = {"ok": {}, "absent": set(), "deferred": set()}
    todo = []
    for a in dict.fromkeys(str(x).lower() for x in (addrs or []) if x):
        cached, hit = _cache_get(a, now_s, max_age_sec)
        if not hit:
            todo.append(a)
        elif is_absent(cached):
            out["absent"].add(a)
        else:
            pair = _pick_pair(cached, a)
            if pair:
                out["ok"][a] = _normalize(pair, a, now_s)
            else:
                out["absent"].add(a)
    for i in range(0, len(todo), CHUNK):
        chunk = todo[i:i + CHUNK]
        data = get_json(f"{BASE}/tokens/v1/{config.DEX_CHAIN}/{','.join(chunk)}")
        if is_deferred(data) or not isinstance(data, list):
            # NOT_FOUND on a batch URL would mean a malformed request, not 30 missing
            # tokens — treat anything that is not a pair list as unknown.
            out["deferred"].update(chunk)
            print(f"  [dexscreener] deferred chunk of {len(chunk)} (network/429/5xx) — "
                  f"not scored, retried next run")
            continue
        by_addr: dict = {a: [] for a in chunk}
        for p in data:
            if not isinstance(p, dict):
                continue
            for a in (_addr_of(p.get("baseToken")), _addr_of(p.get("quoteToken"))):
                if a in by_addr:
                    by_addr[a].append(p)
        for a in chunk:
            pair = _pick_pair(by_addr[a], a)
            if pair:
                out["ok"][a] = _normalize(pair, a, now_s)
                _cache_put(a, by_addr[a])
            else:
                out["absent"].add(a)
                _cache_put(a, {"__absent__": True})   # a 2-min negative; a new pair shows next run
    return out


def token_pairs(addr: str, now_s: float, max_age_sec: float | None = None):
    """One token → market dict | NOT_FOUND (answered, no base pair) | None (deferred)."""
    try:
        res = enrich_many([addr], now_s, max_age_sec=max_age_sec)
    except Exception as exc:                          # belt and braces: never raise
        print(f"  [dexscreener] token_pairs({addr}) failed: {exc!r}")
        return None
    a = str(addr).lower()
    if a in res["ok"]:
        return res["ok"][a]
    if a in res["absent"]:
        return NOT_FOUND
    return None


def forward_snapshot(addr: str, now_s: float):
    """Ledger forward-fill tick for one token: always fresh — a cached tick would stamp a
    2-minute-old price as this horizon's observation."""
    return token_pairs(addr, now_s, max_age_sec=0)


def forward_snapshot_many(addrs, now_s: float) -> dict:
    """Batch forward-fill ticks, same shape as enrich_many, always fresh (max_age_sec=0).
    Absent here means the pair dropped out of the index this poll — the ledger needs
    config.DEAD_CONFIRM_TICKS consecutive absences before it calls that -100%."""
    return enrich_many(addrs, now_s, max_age_sec=0)


def _discover(feed: str, cache_name: str) -> set:
    """Shared body for the two 'latest' feeds: a list of {chainId, tokenAddress, ...}
    across every chain (verified: 16/30 profiles and 14/30 boosts were robinhood on
    2026-09-12). Filtered on chainId == config.DEX_CHAIN; lowercase addresses. Empty set
    when deferred — discovery hedges fail silent, the log cursor is the exact source."""
    data = get_json(f"{BASE}/{feed}",
                    cache_path=os.path.join(config.CACHE_DIR, cache_name),
                    max_age_sec=config.ENRICH_CACHE_MIN * 60)
    if is_deferred(data) or is_absent(data) or not isinstance(data, list):
        return set()
    out = set()
    for row in data:
        if not isinstance(row, dict) or row.get("chainId") != config.DEX_CHAIN:
            continue
        a = str(row.get("tokenAddress") or "").lower()
        if a.startswith("0x") and len(a) == 42:
            out.add(a)
    return out


def discover_profiles() -> set:
    """Tokens whose dev paid Dexscreener for a profile (the 'latest' feed, this chain)."""
    return _discover("token-profiles/latest/v1", "dex_profiles.json")


def discover_boosts() -> set:
    """Tokens with a paid Dexscreener boost (the 'latest' feed, this chain)."""
    return _discover("token-boosts/latest/v1", "dex_boosts.json")


def weth_price_usd(now_s: float):
    """WETH/USD from the deepest pair where WETH is the BASE token (WETH/USDG v3,
    ~$29.7M liquidity, verified 2026-09-12). Cached config.WETH_PX_CACHE_S. Returns None
    when deferred, absent, or unparseable — NEVER 0.0, which would price every WETH-quoted
    position at zero."""
    data = get_json(f"{BASE}/tokens/v1/{config.DEX_CHAIN}/{config.WETH}",
                    cache_path=os.path.join(config.CACHE_DIR, "weth_px.json"),
                    max_age_sec=config.WETH_PX_CACHE_S)
    if is_deferred(data) or is_absent(data) or not isinstance(data, list):
        return None
    pair = _pick_pair(data, config.WETH)
    if not pair:
        return None
    px = _f(pair.get("priceUsd"))
    return px if px and px > 0 else None


if __name__ == "__main__":
    import time
    now_s = time.time()          # the ONLY wall-clock read in this module
    bogus = "0x0000000000000000000000000000000000000001"
    res = enrich_many([config.MIZUKARA, bogus], now_s=now_s)
    miz = config.MIZUKARA.lower()
    print(f"enrich_many: ok={sorted(res['ok'])} absent={sorted(res['absent'])} "
          f"deferred={sorted(res['deferred'])}")
    assert miz in res["ok"], "MIZUKARA should be indexed on dexscreener/robinhood"
    assert bogus in res["absent"], "a bogus address must be ABSENT (answered, no pair), not deferred"
    m = res["ok"][miz]
    assert tuple(m.keys()) == MARKET_KEYS, f"market keys drifted: {list(m)}"
    assert m["liq_usd"] is not None and m["liq_usd"] > 1000, m["liq_usd"]
    assert (m["pair"] or "").lower() == config.MIZUKARA_POOL.lower(), m["pair"]
    assert m["pair_age_min"] is not None and m["pair_age_min"] > 0
    print(f"{m['symbol']}: price=${m['price_usd']:.6g}  mcap=${m['mcap']:,.0f}  "
          f"liq=${m['liq_usd']:,.0f}  vol24=${m['vol_h24']:,.0f}  buys/sells h24="
          f"{m['buys_h24']}/{m['sells_h24']}  dex={m['dex']} {m['pair_labels']}")
    print(f"pair: {m['pair']}  age: {m['pair_age_min']/1440:.1f} days  chg_h1={m['price_chg_h1']}")
    print(f"socials: {m['socials']}")
    assert any(m["socials"].values()), "expected MIZUKARA socials on dexscreener"
    # second call is a cache hit (same file get_json would use) → identical, no network
    m2 = token_pairs(config.MIZUKARA, now_s=now_s)
    assert m2 == m, "cached re-read must reproduce the market dict"
    assert token_pairs(bogus, now_s=now_s) is NOT_FOUND
    # absence-typed fields: a pair with nothing but an address yields None prices, 0.0 flow
    empty = _normalize({"baseToken": {"address": miz}}, miz, now_s)
    assert empty["price_usd"] is None and empty["liq_usd"] is None and empty["mcap"] is None
    assert empty["vol_h24"] == 0.0 and empty["buys_h1"] == 0 and empty["pair_age_min"] is None
    # the m5 flow window is None when absent — never 0.0 (unknown flow is not zero flow)
    assert empty["vol_m5"] is None and empty["buys_m5"] is None and empty["sells_m5"] is None
    assert m["vol_m5"] is None or m["vol_m5"] >= 0.0
    px = weth_price_usd(now_s)
    print(f"WETH/USD: {px}")
    assert px is not None and 500 <= px <= 20000, px
    prof, boosts = discover_profiles(), discover_boosts()
    assert isinstance(prof, set) and isinstance(boosts, set)
    print(f"discovery: {len(prof)} profiled + {len(boosts)} boosted tokens on {config.DEX_CHAIN}")
    print("dexscreener smoke OK")
