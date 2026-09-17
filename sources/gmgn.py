"""
GMGN OpenAPI (https://openapi.gmgn.ai) — the Trenches feed and the wallet-tag second opinion,
for Robinhood Chain. GMGN lists the chain first-class under the slug `robinhood` (verified live
2026-09-12/13: /v1/trenches returned 60 rows per column, /v1/token/info answers).

Two calls, both keyed (X-APIKEY header + timestamp/client_id query, GMGN's "exist" auth):

  trenches()               POST /v1/trenches?chain=robinhood — the three Trenches columns in ONE
                           request: new_creation (New), near_completion (Almost bonded, returned by
                           GMGN under the key `pump`), completed (Migrated). Each row carries the
                           launchpad platform, the bonding-curve progress, holder count, the
                           sniper / insider / fresh-wallet / rat-trader hold rates, smart-money
                           count and the wash-trading flag. Used as a DISCOVERY HEDGE (run.py
                           feed_tokens) and as pass-2 features (safety._apply_gmgn).
  token_info(token)        GET /v1/token/info — stat + wallet_tags_stat → the bundler RATIO
                           (bundler wallets / holders: organic 0.00-0.01, the $Cubrate wallet
                           farm 1.42), the same hold rates, holders, smart-money count, and
                           progress (the info payload's key is `launchpad_progress`, NOT
                           `progress`). The wash-trading flag is NOT in this payload at all —
                           it comes ONLY from the Trenches row (features_from_row).

THE BODY SHAPE IS LOAD-BEARING. GMGN's own client (OpenApiClient.ts buildTrenchesBody) sends
{"version": "v2", "<column>": {"filters": [...], "launchpad_platform_v2": true, "limit": 80,
"quote_address_type": [...robinhood set...], ...min_*/max_*}}. Without `version` and
`quote_address_type` the server answers code 0 with EMPTY columns — a trap that reads exactly
like "nothing new" (one research agent fell into it, 2026-09-12). build_trenches_body is the
single place that shape lives.

Failure semantics — the http_client three-way contract, honoured verbatim:
  dict       normalised rows / features
  NOT_FOUND  the server ANSWERED 404 (token_info on a token GMGN does not index): absent, a fact
  None       deferred — network / 5xx / a 429 (which is TERMINAL for the run on this host, see
             config.HOST_429_TERMINAL) / a non-zero GMGN code / no key configured. A deferred
             GMGN is named in sources_dark by safety._apply_gmgn and NEVER widens a gate: no hard
             gate reads a gmgn_* field (unlike solana's two-way `{}`, where a 429 silently
             loosened four gates). Unknown is never A.

Coverage caveat (measured 2026-09-12): GMGN's Trenches applies a fixed server-side launchpad
allow-list for robinhood that omits pons_v2 (67 of the top-100 rank rows) and bare V2/V3/V4
pools (tagged ""), so the feed complements the log cursor; it cannot replace it.

The ONE wall-clock in this module is the auth timestamp inside _query(): GMGN rejects a stale
one (AUTH_TIMESTAMP_EXPIRED — measured when run.py's single now_s, captured at the start of the
run, reached pass 2 a minute later). It is transport, like http_client's cache clock; nothing
scored reads it, and verify pins it to that one function.
Python's default User-Agent is rejected by Cloudflare (403 error 1010); http_client sends
config.USER_AGENT, which passes.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time
import urllib.parse
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config                                                        # noqa: E402
import http_client                                                   # noqa: E402
from http_client import NOT_FOUND, get_json, is_absent, post_json    # noqa: E402

HOST = "openapi.gmgn.ai"
BASE = config.GMGN_BASE
GMGN_FEATURE_KEYS = (
    "gmgn_launchpad_platform", "gmgn_progress", "gmgn_bundler_ratio", "gmgn_sniper_hold_pct",
    "gmgn_insider_hold_pct", "gmgn_fresh_wallet_pct", "gmgn_rat_vol_pct", "gmgn_smart_degen_count",
    "gmgn_is_wash_trading", "gmgn_holders",
)
_COLUMN_ALIASES = {"pump": "near_completion"}     # GMGN returns Almost-bonded under `pump`


# ── helpers ────────────────────────────────────────────────────────────────────────
_key_memo: list = []      # [key | None], resolved once per process (pass 2 calls this from 3 threads)


def _api_key():
    if not _key_memo:
        _key_memo.append(config.load_credentials().get("gmgn_api_key") or None)
    return _key_memo[0]


def _cached(cache, ttl, fetch):
    """Read-through cache that stores ONLY a code-0 answer (or a 404 as the absent marker):
    an error envelope is deferred and must be retried, never replayed from disk."""
    if cache and ttl:
        obj, hit = http_client._cache_read(cache, ttl)
        if hit:
            return obj
    d = fetch()
    if cache and ttl:
        try:
            if isinstance(d, dict) and not is_absent(d) and d.get("code") == 0:
                http_client._atomic_write_json(cache, d)
            elif is_absent(d):
                http_client._atomic_write_json(cache, {"__absent__": True})
        except Exception:
            pass
    return d


def _f(x):
    try:
        if x is None or isinstance(x, bool):
            return None
        v = float(x)
        return v if v == v and v not in (float("inf"), float("-inf")) else None
    except (TypeError, ValueError):
        return None


def _i(x):
    try:
        if x is None or isinstance(x, bool):
            return None
        return int(x)
    except (TypeError, ValueError):
        return None


def _b(x):
    return x if isinstance(x, bool) else None


def _pct(x):
    v = _f(x)
    return None if v is None else v * 100.0


def _str(x):
    return x if isinstance(x, str) and x else None


def _query() -> str:
    """The auth query: GMGN wants a FRESH unix timestamp per request (the one wall-clock here)."""
    return urllib.parse.urlencode({"chain": config.GMGN_CHAIN, "timestamp": int(time.time()),
                                   "client_id": str(uuid.uuid4())})


def _headers(key: str) -> dict:
    return {"X-APIKEY": key, "Content-Type": "application/json"}


def empty_features() -> dict:
    return {k: None for k in GMGN_FEATURE_KEYS}


def token_url(token: str) -> str:
    return config.GMGN_TOKEN_URL.format(chain=config.GMGN_CHAIN, token=str(token).lower())


# ── the Trenches feed ──────────────────────────────────────────────────────────────
def build_trenches_body(columns=None, filters: dict | None = None, limit: int | None = None) -> dict:
    """GMGN's own client shape (buildTrenchesBody): version v2 + one section per column."""
    cols = tuple(columns) if columns else tuple(config.GMGN_TRENCHES_COLUMNS)
    section = {"filters": list(config.GMGN_TRENCHES_FILTERS), "launchpad_platform_v2": True,
               "limit": int(limit or config.GMGN_TRENCHES_LIMIT),
               "quote_address_type": list(config.GMGN_QUOTE_ADDRESS_TYPES)}
    if filters:
        section.update({str(k): v for k, v in filters.items()})
    body: dict = {"version": "v2"}
    for c in cols:
        body[c] = dict(section)
    return body


def _normalize_columns(data, columns) -> dict:
    out = {c: [] for c in columns}
    if not isinstance(data, dict):
        return out
    for k, rows in data.items():
        c = _COLUMN_ALIASES.get(k, k)
        if c in out:
            out[c] = [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []
    return out


def trenches(columns=None, filters: dict | None = None, cache_s=None):
    """{column: [rows]} for the requested columns, or None (deferred / dark / no key)."""
    key = _api_key()
    if not key:
        return None
    cols = tuple(columns) if columns else tuple(config.GMGN_TRENCHES_COLUMNS)
    body = build_trenches_body(cols, filters)
    ttl = config.GMGN_TRENCHES_CACHE_S if cache_s is None else cache_s
    cache = None
    if ttl:
        h = hashlib.sha256(json.dumps({"cols": cols, "filters": filters or {}}, sort_keys=True).encode()).hexdigest()[:12]
        cache = os.path.join(config.CACHE_DIR, f"gmgn_trenches_{h}.json")
    d = _cached(cache, ttl, lambda: post_json(f"{BASE}/v1/trenches?{_query()}", body, headers=_headers(key)))
    if d is None or is_absent(d) or not isinstance(d, dict) or d.get("code") != 0:
        return None                          # a malformed request or a GMGN error is not "nothing new"
    return _normalize_columns(d.get("data"), cols)


def features_from_row(row: dict) -> dict:
    """A Trenches row → EXACTLY the gmgn_* feature keys (None when the row lacks a field).
    The bundler RATIO needs /v1/token/info's wallet tags, so it stays None here."""
    r = row if isinstance(row, dict) else {}
    return {
        "gmgn_launchpad_platform": _str(r.get("launchpad_platform")),
        "gmgn_progress": _f(r.get("progress")),
        "gmgn_bundler_ratio": None,
        "gmgn_sniper_hold_pct": _pct(r.get("top70_sniper_hold_rate")),
        "gmgn_insider_hold_pct": _pct(r.get("suspected_insider_hold_rate")),
        "gmgn_fresh_wallet_pct": _pct(r.get("fresh_wallet_rate")),
        "gmgn_rat_vol_pct": _pct(r.get("rat_trader_amount_rate")),
        "gmgn_smart_degen_count": _i(r.get("smart_degen_count")),
        "gmgn_is_wash_trading": _b(r.get("is_wash_trading")),
        "gmgn_holders": _i(r.get("holder_count")),
    }


# ── the wallet-tag second opinion ──────────────────────────────────────────────────
def token_info(token: str, cache_s=None):
    """gmgn_* features from /v1/token/info, NOT_FOUND (absent), or None (deferred / no key)."""
    key = _api_key()
    if not key:
        return None
    t = str(token or "").lower()
    ttl = config.INFO_CACHE_MIN * 60 if cache_s is None else cache_s
    cache = os.path.join(config.CACHE_DIR, f"gmgn_info_{t}.json") if ttl else None
    d = _cached(cache, ttl, lambda: get_json(f"{BASE}/v1/token/info?{_query()}&address={t}", headers=_headers(key)))
    if d is None:
        return None
    if is_absent(d):
        return NOT_FOUND
    if not isinstance(d, dict) or d.get("code") != 0:
        return None
    data = d.get("data") or {}
    stat = data.get("stat") or {}
    tags = data.get("wallet_tags_stat") or {}
    if not stat and not tags:
        return None                          # 200 with no record = unparseable, deferred, not a fact
    holders = _i(stat.get("holder_count"))
    bundlers = _i(tags.get("bundler_wallets"))
    out = empty_features()
    out.update({
        "gmgn_launchpad_platform": _str(data.get("launchpad_platform")),
        "gmgn_progress": _f(data.get("launchpad_progress")),
        "gmgn_bundler_ratio": (None if bundlers is None or holders is None
                               else round(bundlers / max(holders, 1), 6)),
        "gmgn_sniper_hold_pct": _pct(stat.get("top70_sniper_hold_rate")),
        "gmgn_insider_hold_pct": _pct(stat.get("suspected_insider_hold_rate")),
        "gmgn_fresh_wallet_pct": _pct(stat.get("fresh_wallet_rate")),
        "gmgn_rat_vol_pct": _pct(stat.get("top_rat_trader_percentage")),
        "gmgn_smart_degen_count": _i(tags.get("smart_wallets")),
        "gmgn_holders": holders,
    })
    return out


if __name__ == "__main__":
    from collections import Counter

    print("gmgn smoke test — one live /v1/trenches call for chain", config.GMGN_CHAIN)
    if not _api_key():
        print("  no GMGN key configured (env GMGN_API_KEY / config.local.json / the solana sibling) — "
              "the source degrades to dark; nothing else to show")
        raise SystemExit(0)
    cols = trenches(cache_s=0)
    if cols is None:
        print("  deferred / dark (see http_client.health_report())")
        raise SystemExit(0)
    first = None
    for c in config.GMGN_TRENCHES_COLUMNS:
        rows = cols.get(c) or []
        plat = Counter(str(r.get("launchpad_platform") or "") for r in rows).most_common(4)
        print(f"  {c:16s} {len(rows):3d} rows   platforms {plat}")
        for r in rows[:2]:
            f = features_from_row(r)
            print(f"     {str(r.get('symbol')):10.10s} progress {f['gmgn_progress']}  holders {f['gmgn_holders']}  "
                  f"snipers {f['gmgn_sniper_hold_pct']}%  smart {f['gmgn_smart_degen_count']}  {token_url(r.get('address'))}")
        if c == "completed" and rows and first is None:
            first = rows[0].get("address")
    if first:
        ti = token_info(first, cache_s=0)
        print(f"  token_info({first[:10]}…): "
              + (repr(ti) if ti is None or is_absent(ti) else
                 f"bundler ratio {ti['gmgn_bundler_ratio']}  holders {ti['gmgn_holders']}  smart {ti['gmgn_smart_degen_count']}"))
