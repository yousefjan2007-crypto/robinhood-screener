"""
KyberSwap aggregator route quotes on Robinhood Chain (chain slug 'robinhood'), keyless.

What this source uniquely provides: a venue-agnostic *aggregator* fill quote with USD on
BOTH legs (amountInUsd / amountOutUsd) and a gas estimate (gasUsd) — the structural analogue
of the Jupiter quote solana_screener uses for paper fills. It routes across every venue Kyber
indexes on 4663, so it is the ONLY paper-fill quote source for tokens that have no UniswapV2
pair (the V2 Router02 getAmountsOut path reverts on V3/V4 pools — explore_agent6.md §4).
Verified 2026-09-12: WETH→MIZUKARA 0.1 ETH routed through the MIZUKARA pool with
amountInUsd ≈ $251, gasUsd ≈ $0.075 at 289,533 gas.

Role: paper-fill QUOTE SOURCE ONLY. Never a gate — an aggregator's failure to route says
nothing about a token's safety, and its `amountOut` includes Kyber's own fee/route choice
(29.10M vs the router's 29.40M on MIZUKARA), so it is "what a real aggregator fill would be",
not "what the pool would give".

Failure semantics (the three-way contract of http_client, honoured exactly):
    dict       — a real route: routeSummary.amountOut > 0.
    NOT_FOUND  — an EXPLICIT no-route: HTTP 400/404 (live: 400 {"code":4011,"message":"token
                 not found"}), or HTTP 200 with code != 0, amountOut == 0, or no routeSummary.
                 The server answered; asking again will not help. The paper book may then try
                 its other quote sources or mark the fill unquotable.
    None       — deferred: network/429/5xx/challenge. Unknown — retry next run, never score.
Conflating the last two fabricates fills; the distinction is the whole point of this module.

Quotes are NEVER cached: a stale quote is a fabricated fill (http_client.post_json docstring).
No wall-clock is read here; nothing in this module depends on time. Never raises on bad data.
"""
from __future__ import annotations

import os
import sys
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config                                                    # noqa: E402
from http_client import NOT_FOUND, get_json, is_absent, is_deferred  # noqa: E402

# Kyber's own success code (explore_agent6.md §2: {"code":0,"message":"successfully",...}).
# Any other code on a 200 body (4011 = "token not found") is an explicit refusal, not a fault.
_KYBER_OK_CODE = 0


def _int(x, default=None):
    """Kyber serialises every wei amount as a decimal STRING ("29101597882311178396368896");
    ints above 2**53 must never pass through float."""
    try:
        if x is None or x == "":
            return default
        return int(str(x).strip())
    except (TypeError, ValueError):
        return default


def _float(x, default=None):
    try:
        if x is None or x == "":
            return default
        return float(x)
    except (TypeError, ValueError):
        return default


def _route_pools(route) -> list:
    """Flatten routeSummary.route ([[hop, hop, ...], [hop, ...]]: one list per split path)
    into the ordered list of pool addresses, lowercased. Unknown shapes yield []."""
    pools: list = []
    try:
        for path in route or []:
            for hop in path or []:
                if isinstance(hop, dict):
                    p = hop.get("pool")
                    if isinstance(p, str) and p:
                        pools.append(p.lower())
    except Exception:
        return []
    return pools


def route(token_in: str, token_out: str, amount_in_wei: int):
    """Aggregator route quote for exactly `amount_in_wei` of `token_in` into `token_out`.

    Returns a normalised flat dict | NOT_FOUND (explicit no-route) | None (deferred):
        amount_out      int         wei of token_out the route would deliver (> 0)
        amount_in       int         wei of token_in actually quoted (echoed by Kyber)
        amount_in_usd   float|None  USD value of the input leg
        amount_out_usd  float|None  USD value of the output leg (slippage in USD = in - out)
        gas             int|None    estimated gas units
        gas_usd         float|None  estimated gas cost in USD (L2 execution)
        l1_fee_usd      float|None  L1 data fee in USD ("0" observed on 4663)
        route_pools     list[str]   pool addresses along the route, lowercased, in hop order
        raw             dict        the untouched routeSummary, for the ledger's audit trail
    """
    try:
        amount_in_wei = int(amount_in_wei)
        if amount_in_wei <= 0:
            return NOT_FOUND                     # nothing to route; the server would 400 anyway
        qs = urllib.parse.urlencode({"tokenIn": str(token_in), "tokenOut": str(token_out),
                                     "amountIn": str(amount_in_wei)})
        resp = get_json(f"{config.KYBER_ROUTES_URL}?{qs}")   # no cache_path: never cache a quote
    except Exception:
        return None                              # a client-side fault is unknown, not "no route"
    if is_deferred(resp):
        return None
    if is_absent(resp):
        return NOT_FOUND                         # live: HTTP 400 code 4011 "token not found"
    try:
        if not isinstance(resp, dict) or _int(resp.get("code"), _KYBER_OK_CODE) != _KYBER_OK_CODE:
            return NOT_FOUND                     # 200 body carrying an application-level refusal
        rs = (resp.get("data") or {}).get("routeSummary")
        if not isinstance(rs, dict):
            return NOT_FOUND
        amount_out = _int(rs.get("amountOut"), 0)
        if amount_out <= 0:
            return NOT_FOUND
        return {
            "amount_out": amount_out,
            "amount_in": _int(rs.get("amountIn"), amount_in_wei),
            "amount_in_usd": _float(rs.get("amountInUsd")),
            "amount_out_usd": _float(rs.get("amountOutUsd")),
            "gas": _int(rs.get("gas")),
            "gas_usd": _float(rs.get("gasUsd")),
            "l1_fee_usd": _float(rs.get("l1FeeUsd")),
            "route_pools": _route_pools(rs.get("route")),
            "raw": rs,
        }
    except Exception:
        return NOT_FOUND                         # the server answered with something unusable


def usd_legs(q):
    """(amount_in_usd, amount_out_usd) from a route() result; (None, None) for NOT_FOUND/None
    or a malformed dict, so callers can subtract without a status check first."""
    if not isinstance(q, dict) or is_absent(q):
        return None, None
    return q.get("amount_in_usd"), q.get("amount_out_usd")


if __name__ == "__main__":
    print("robinhood_screener sources/kyber")
    probe = 10 ** 17                                          # 0.1 WETH, the survey's exact call
    q = route(config.WETH, config.MIZUKARA, probe)
    assert isinstance(q, dict) and not is_absent(q), f"MIZUKARA route: expected ok, got {q!r}"
    assert q["amount_out"] > 0
    in_usd, out_usd = usd_legs(q)
    print(f"  WETH -> MIZUKARA 0.1 ETH: amount_out={q['amount_out']} "
          f"({q['amount_out'] / 1e18:,.3f} tokens @18dp)")
    print(f"  amount_in_usd={in_usd}  amount_out_usd={out_usd}  "
          f"slippage_usd={(in_usd - out_usd) if (in_usd is not None and out_usd is not None) else None}")
    print(f"  gas={q['gas']}  gas_usd={q['gas_usd']}  l1_fee_usd={q['l1_fee_usd']}  "
          f"(config.PAPER_GAS_USD_PER_SWAP={config.PAPER_GAS_USD_PER_SWAP})")
    print(f"  route_pools={q['route_pools']}")
    assert q["route_pools"], "expected at least one pool on the route"
    assert q["route_pools"][0] == config.MIZUKARA_POOL.lower(), \
        f"first pool {q['route_pools'][0]} != MIZUKARA_POOL"
    assert in_usd is not None and in_usd > 0, "amountInUsd missing"
    print(f"  usd_legs on NOT_FOUND -> {usd_legs(NOT_FOUND)}   on None -> {usd_legs(None)}")

    # The load-bearing distinction: a token Kyber does not know is an EXPLICIT no-route
    # (NOT_FOUND), never a deferred None. Live 2026-09-12: HTTP 400 {"code":4011,...}.
    nf = route(config.WETH, "0x0000000000000000000000000000000000000001", probe)
    print(f"  WETH -> 0x…0001: {nf!r}")
    assert nf is not None, "no-route came back deferred (None) — that would be a fabricated retry loop"
    assert is_absent(nf), f"expected NOT_FOUND, got {nf!r}"
    assert route(config.WETH, config.MIZUKARA, 0) is NOT_FOUND
    print("  ok: route dict, no-route is NOT_FOUND (not None), zero amount is NOT_FOUND")
