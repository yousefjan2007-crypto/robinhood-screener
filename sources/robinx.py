"""
RobinX (https://robinx.io, API at api.robinx.io) — deployer-reputation index for Robinhood
Chain: genesis-to-present launch history per wallet (launched / real / dead / score), insider
funding-graph flags, and a 10-minute-delayed new-launch feed with the deployer ALREADY
resolved. Nothing else on this chain ships a scored deployer record; Blockscout gives us the
raw creation list and devindex.py scores it ourselves, so RobinX is the independent
CROSS-CHECK on our own attribution (and the only insider signal we have at all).

ACCESS — FREE TIER ONLY. The endpoints used here (/wallet/{address}, /feed/new?tier=free,
/report/{token}?tier=free) are free, need no auth header and no signature; verified 200 with
real data 2026-09-12 (explore_agent6.md §2d). RobinX also sells (a) an x402 pay-per-call
tier (USDC on Base — each call is paid by SIGNING A TRANSACTION) and (b) an "Agent" key tier
($29/mo, `x-robinx-key` header) with realtime, undelayed feeds. NEITHER IS USED: this project
is alert-only and never holds keys, signs anything, or moves funds. The free feed's 10-minute
delay is the price of that line, and it is already inside the screener's own age gate.

Failure semantics (the http_client three-way contract, honoured verbatim):
  dict       — normalised facts (documented per function)
  NOT_FOUND  — the server ANSWERED 400/404: no such wallet/token in their index ("absent")
  None       — network / 429 / 5xx / challenge: DEFERRED — retry later, never score
A wallet that RobinX knows but that never deployed comes back with `deployer: null`; that is
a real answer (deployer=None inside a dict), not an error. Measured 2026-09-12: the MIZUKARA
dev (a Flap launch) is `deployer: null` and /report says "deployer not yet attributed for this
launch path" — RobinX does NOT attribute launchpad (factory-routed) launches to the creation-tx
sender the way devindex.py does, so a null here must never override our own attribution.
Their free-tier rate caps are unpublished ("rate-capped"), so config.ROBINX_RATE_HZ is
deliberately conservative.
Their own caveat, kept here so nobody over-reads the numbers: venue coverage was 76.2% of
ETH-side volume as of 2026-07-30, so any activity total is "a floor, never a ceiling".
"""
from __future__ import annotations

import os
import sys
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config                                              # noqa: E402
from http_client import NOT_FOUND, get_json, is_absent     # noqa: E402

BASE = "https://api.robinx.io"          # host is rate-pinned in config.HOST_RATE_HZ
FREE_TIER = "free"                      # the only tier this module ever asks for
FEED_DEFAULT_LIMIT = 50


# ── helpers ───────────────────────────────────────────────────────────────────────
def _addr(a) -> str:
    """Lowercase 0x-address for cache keys and feed items; EVM addresses are case-insensitive
    and RobinX mixes cases across endpoints, so normalise before anything compares them."""
    return str(a or "").strip().lower()


def _int(x):
    try:
        return int(x) if x is not None else None
    except (TypeError, ValueError):
        return None


def _float(x):
    try:
        return float(x) if x is not None else None
    except (TypeError, ValueError):
        return None


def _unwrap(d):
    """Every RobinX response is an envelope: {data:{...}, signal, confidence, sources,
    model_used, latency_ms, timestamp} (observed live 2026-09-12 on /wallet, /feed/new and
    /report; explore_agent6.md §2d recorded only the inner shape). The facts live in `data`;
    the outer signal/confidence duplicate data.verdict. Tolerates an unwrapped body too."""
    if isinstance(d, dict) and isinstance(d.get("data"), dict):
        return d["data"]
    return d


def _verdict(v) -> dict | None:
    """{signal, confidence, reasons} or None. RobinX verdicts carry free-text reasons; keep
    them as a list of strings so an alert can print them verbatim."""
    if not isinstance(v, dict):
        return None
    reasons = v.get("reasons")
    if not isinstance(reasons, list):
        reasons = [reasons] if reasons else []
    return {"signal": v.get("signal"),
            "confidence": _float(v.get("confidence")),
            "reasons": [str(r) for r in reasons]}


# ── public API ────────────────────────────────────────────────────────────────────
def available() -> bool:
    """The free tier needs nothing — no key, no wallet — so the source is always wired."""
    return True


def wallet(addr: str):
    """GET /wallet/{address} — the deployer record + insider flags for one wallet.

    Returns:
      {deployer: {launched:int, real:int, dead:int, score:float|None} | None,
       insider:  dict | None,          # their insider-flow block, passed through
       verdict:  {signal, confidence, reasons:list} | None,
       raw:      dict}                 # the full response, for the archive
      NOT_FOUND — RobinX has never seen this address (400/404; negatively cached: an
                  unknown address stays unknown for the cache window)
      None      — deferred
    `deployer` is None when RobinX knows the wallet but it has launched nothing — a real
    answer, and NOT a reason to skip the token (our own Blockscout attribution still runs).
    Mutable (their index re-scores as launches resolve) → INFO_CACHE_MIN.
    """
    a = _addr(addr)
    if not a.startswith("0x") or len(a) != 42:
        return NOT_FOUND
    cache = os.path.join(config.CACHE_DIR, f"robinx_wallet_{a}.json")
    d = get_json(f"{BASE}/wallet/{a}", cache_path=cache,
                 max_age_sec=config.INFO_CACHE_MIN * 60, cache_404=True)
    if d is None or is_absent(d):
        return d
    raw, d = d, _unwrap(d)
    if not isinstance(d, dict):
        return None                     # a non-object body is a server-side oddity: defer
    dep = d.get("deployer")
    deployer = None
    if isinstance(dep, dict):
        deployer = {"launched": _int(dep.get("launched")),
                    "real": _int(dep.get("real")),
                    "dead": _int(dep.get("dead")),
                    "score": _float(dep.get("score"))}
    ins = d.get("insider")
    return {"deployer": deployer,
            "insider": ins if isinstance(ins, dict) else None,
            "verdict": _verdict(d.get("verdict")),
            "raw": raw}


def feed_new(since=None, limit: int = FEED_DEFAULT_LIMIT):
    """GET /feed/new?tier=free&limit=N[&since=cursor] — the 10-minute-delayed new-launch
    feed with deployer attribution resolved. Never cached: a feed page is a moving window
    and the cursor protocol (`since` drains oldest-first) is the dedup.

    Returns {items: [{token, deployer, launched_at, block, deployer_score,
                      deployer_launched, signal, confidence}],   # addresses lowercased
             cursor, has_more, delayed_minutes}  |  None (deferred).
    Only enabled as a discovery source when "robinx_feed_new" is in config.DISCOVERY_FEEDS
    — that switch lives at the caller, not here.
    """
    try:
        n = max(1, int(limit))
    except (TypeError, ValueError):
        n = FEED_DEFAULT_LIMIT
    url = f"{BASE}/feed/new?tier={FREE_TIER}&limit={n}"
    if since:
        # the cursor is "<ISO seen_at>|<token>" — its '+' would decode to a space unencoded
        url += "&since=" + urllib.parse.quote(str(since), safe="")
    d = _unwrap(get_json(url))
    if d is None or is_absent(d) or not isinstance(d, dict):
        return None                     # a 4xx on the feed itself is a client bug, not
                                        # "no launches": surface it as deferred, never empty
    items = []
    for it in d.get("items") or []:
        if not isinstance(it, dict):
            continue
        tok = _addr(it.get("token"))
        if not tok:
            continue
        items.append({"token": tok,
                      "deployer": _addr(it.get("deployer")) or None,
                      "launched_at": it.get("launched_at"),
                      "block": _int(it.get("block")),
                      "deployer_score": _float(it.get("deployer_score")),
                      "deployer_launched": _int(it.get("deployer_launched")),
                      "signal": it.get("signal"),
                      "confidence": _float(it.get("confidence"))})
    return {"items": items,
            "cursor": d.get("cursor"),
            "has_more": bool(d.get("has_more")),
            "delayed_minutes": _int(d.get("delayed_minutes"))}


def report(token: str):
    """GET /report/{token}?tier=free — RobinX's own read on one token. Optional colour for
    an alert, never a gate: their activity totals are a floor (76.2% venue coverage).

    Returns {identity, verdict:{signal,confidence,reasons}|None,
             activity:{swaps:int|None, weth_vol:float|None, traders:int|None, is_real:bool|None},
             market: dict|None, contract_template, raw}  |  NOT_FOUND  |  None.
    Mutable → INFO_CACHE_MIN; a 404 (not indexed yet) is NOT negatively cached — a token
    that is 3 minutes old is absent now and present on the next run.
    """
    t = _addr(token)
    if not t.startswith("0x") or len(t) != 42:
        return NOT_FOUND
    cache = os.path.join(config.CACHE_DIR, f"robinx_report_{t}.json")
    d = get_json(f"{BASE}/report/{t}?tier={FREE_TIER}", cache_path=cache,
                 max_age_sec=config.INFO_CACHE_MIN * 60)
    if d is None or is_absent(d):
        return d
    raw, d = d, _unwrap(d)
    if not isinstance(d, dict):
        return None
    act = d.get("activity") if isinstance(d.get("activity"), dict) else {}
    is_real = act.get("is_real")
    return {"identity": d.get("identity"),
            "verdict": _verdict(d.get("verdict")),
            "activity": {"swaps": _int(act.get("swaps")),
                         "weth_vol": _float(act.get("weth_vol")),
                         "traders": _int(act.get("traders")),
                         "is_real": bool(is_real) if is_real is not None else None},
            "market": d.get("market") if isinstance(d.get("market"), dict) else None,
            "contract_template": d.get("contract_template"),
            "raw": raw}


if __name__ == "__main__":
    # Frugal: 3 live calls at config.ROBINX_RATE_HZ (caches may make it fewer).
    print("robinhood_screener sources/robinx (FREE tier; no key, no signing)")
    print(f"  available: {available()}")

    w = wallet(config.MIZUKARA_DEV)
    assert w is not None, "wallet(MIZUKARA_DEV) deferred — RobinX unreachable or rate-capped"
    assert isinstance(w, dict) and not is_absent(w), f"MIZUKARA dev should be indexed, got {w!r}"
    dep = w["deployer"]
    print(f"  wallet {config.MIZUKARA_DEV[:10]}…: deployer={dep}  "
          f"verdict={w['verdict']['signal'] if w['verdict'] else None}  "
          f"insider={'yes' if w['insider'] else 'none'}")
    assert dep is None or isinstance(dep["launched"], int), "deployer.launched must be int"
    if dep is not None:
        assert dep["launched"] >= 1, "the MIZUKARA dev launched MIZUKARA, so launched >= 1"

    f = feed_new(limit=5)
    if f is None:
        print("  feed_new: deferred (None)")
    else:
        print(f"  feed_new: delayed_minutes={f['delayed_minutes']}  n={len(f['items'])}  "
              f"has_more={f['has_more']}  cursor={str(f['cursor'])[:24]}")
        assert isinstance(f["items"], list)
        if f["items"]:
            first = f["items"][0]
            print(f"    first: {first}")
            assert first["token"].startswith("0x") and first["token"] == first["token"].lower()

    r = report(config.MIZUKARA)
    if r is None:
        print("  report: deferred (None)")
    elif is_absent(r):
        print("  report: NOT_FOUND (MIZUKARA not in their report index)")
    else:
        print(f"  report MIZUKARA: traders={r['activity']['traders']}  swaps={r['activity']['swaps']}  "
              f"is_real={r['activity']['is_real']}  template={r['contract_template']}  "
              f"verdict={r['verdict']['signal'] if r['verdict'] else None}")
        assert r["activity"]["traders"] is None or r["activity"]["traders"] >= 0

    # a malformed address never reaches the network and is 'absent', not an exception
    assert is_absent(wallet("not-an-address"))
    print("  ok")
