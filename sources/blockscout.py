"""
Blockscout v2 (robinhoodchain.blockscout.com) — the deployer-attribution backbone, plus the
chain-native facts no other free source carries: exact holder/transfer counters, a paged
holder list with `is_contract`/`is_scam` per holder, the creation transaction + listed
creator, EIP-1167 proxy resolution (`implementations[0].name` — the contract TEMPLATE, e.g.
"FlapTaxTokenV3"), the `is_scam`/`reputation` flags, and DECODED transaction logs.

CRITICAL SUBTLETY (verified on MIZUKARA): launchpad tokens are deployed BY a factory, so
Blockscout's `creator_address_hash` is the FACTORY (Flap router), not the dev. The real dev
wallet is the creation transaction's SENDER. `creator_of()` always attributes
deployer = tx_sender(creation_tx) and reports via="factory" when the two differ.

Failure semantics — every public function propagates http_client's three-way contract:
    dict/list  ok
    NOT_FOUND  the server answered 400/404: the token/address/tx does not exist ("absent")
    None       network / 429 / 5xx / Cloudflare challenge: unknown, retry later, NEVER score
None is never turned into NOT_FOUND here (conflating the two fabricated 472 dead-token rows
in a sibling project). A 200 whose body lacks the fields we need is also returned as None
(deferred) rather than parsed into zeros. Nothing raises on bad data.

Caching: immutable facts (creation tx, tx sender, tx logs) are cached forever; mutable
per-token facts (holders, counters, scam flags, proxy impl) for config.INFO_CACHE_MIN.
Cloudflare header handling lives in http_client (rotate-once-on-challenge) — nothing here.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config                                                   # noqa: E402
from http_client import NOT_FOUND, get_json, is_absent, is_deferred   # noqa: E402

BASE = config.BLOCKSCOUT_BASE
_FOREVER = config.FOREVER_CACHE_DAYS * 86400
_INFO = config.INFO_CACHE_MIN * 60
_BURN = {a.lower() for a in config.BURN_ADDRESSES}
# Decoded-log pages are 50 items; a launchpad creation tx emits ~10-20 events, so one page
# is the norm. The bound exists only so a pathological tx cannot spin the run.
_LOGS_MAX_PAGES = 4

# method-name heuristic for "this factory tx created a token" (vs swaps/airdrops);
# resolved creations are then confirmed via internal-transaction `create` entries.
_CREATE_HINTS = ("token", "create", "launch", "deploy", "mint")
_NOT_CREATE = ("swap", "airdrop", "claim", "collect", "buy", "sell")


# ── plumbing ─────────────────────────────────────────────────────────────────────
def _cache_path(key: str) -> str:
    return os.path.join(config.CACHE_DIR, f"bs_{key}.json")


def _legacy_addr_cache(addr: str) -> str:
    """The pre-2026-09-12 multi-chain client cached /addresses/{addr} forever under a
    host-tagged key (~21k files on the Mac). Creation facts in them are still valid."""
    return os.path.join(config.CACHE_DIR,
                        f"bs_robinhoodchain_blockscout_com_addr_{addr}.json")


def _read_cache(path: str, max_age: float | None):
    """Local JSON cache read (for records THIS module assembles, not raw responses)."""
    try:
        if not os.path.exists(path):
            return None
        if max_age is not None and time.time() - os.path.getmtime(path) > max_age:
            return None
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


def _write_cache(path: str, obj) -> None:
    try:
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w") as f:
            json.dump(obj, f)
        os.replace(tmp, path)
    except Exception:
        pass


def _get(path: str, cache_key: str | None = None, max_age: float | None = None,
         params: dict | None = None, cache_404: bool = False):
    """GET /api/v2/{path}. Returns parsed JSON | NOT_FOUND | None (http_client contract)."""
    url = f"{BASE}/api/v2/{path}"
    if params:
        # next_page_params echoes JSON values back as query params. Blockscout wants
        # JSON literals: None → "null", True/False → "true"/"false" (Python's default
        # "None"/"False" strings 422, and OMITTING the null cursor fields silently
        # resets pagination to page 1 — an infinite-loop trap).
        clean = {k: ("null" if v is None else str(v).lower() if isinstance(v, bool)
                     else v) for k, v in params.items()}
        url += "?" + urllib.parse.urlencode(clean)
    cache = _cache_path(cache_key) if cache_key else None
    return get_json(url, cache_path=cache, max_age_sec=max_age, cache_404=cache_404)


def _int(x, default=None):
    try:
        return int(float(x))
    except (TypeError, ValueError):
        return default


def _hash(obj) -> str | None:
    """Blockscout address objects are {hash, is_contract, ...}; older shapes were bare strings."""
    if isinstance(obj, dict):
        return obj.get("hash")
    return obj if isinstance(obj, str) else None


# ── token facts ───────────────────────────────────────────────────────────────────
def token_info(addr: str):
    """GET tokens/{addr} → {address, name, symbol, decimals, supply_raw:int, supply:float,
    holders:int|None, reputation, type} | NOT_FOUND | None.
    A 404 here is a stable "not a token" (negatively cached); the rest is mutable."""
    d = _get(f"tokens/{addr}", cache_key=f"tok_{addr.lower()}", max_age=_INFO, cache_404=True)
    if is_deferred(d) or is_absent(d):
        return d
    if not isinstance(d, dict) or "symbol" not in d:
        return None                      # a 200 without token fields: unusable, not "absent"
    decimals = _int(d.get("decimals"), 18)
    supply_raw = _int(d.get("total_supply"), 0)
    return {"address": addr, "name": d.get("name"), "symbol": d.get("symbol"),
            "decimals": decimals, "supply_raw": supply_raw,
            "supply": supply_raw / 10 ** decimals if supply_raw else 0.0,
            "holders": _int(d.get("holders_count", d.get("holders"))),
            "reputation": d.get("reputation"), "type": d.get("type")}


def token_counters(addr: str):
    """GET tokens/{addr}/counters → {transfers_count:int, holders_count:int} | NOT_FOUND | None.
    One call gives the exact tx/holder-rate gate (MIZUKARA: 132,631 / 1,962 = 67.6)."""
    d = _get(f"tokens/{addr}/counters", cache_key=f"cnt_{addr.lower()}", max_age=_INFO)
    if is_deferred(d) or is_absent(d):
        return d
    if not isinstance(d, dict):
        return None
    transfers = _int(d.get("transfers_count"))
    holders = _int(d.get("token_holders_count", d.get("holders_count")))
    if transfers is None or holders is None:
        return None
    return {"transfers_count": transfers, "holders_count": holders}


def top_holders(addr: str, supply_raw: int, n: int = 10):
    """Page 1 of GET tokens/{addr}/holders (50 rows, value-descending) →
    {top10_pct, top1_pct, lp_pct, n_contract_excluded, n_rows, source:"blockscout"}
    | NOT_FOUND | None.
    Contracts (the LP shows as is_contract:true, name "UniswapV2Pair"; also PoolManager,
    lockers, the launchpad curve) and burn addresses are excluded from the wallet
    concentration — DEXTools' danger line is for WALLETS. Their share is reported as
    lp_pct so the caller can cross-check against the on-chain LP burn. Sums are in raw
    units over supply_raw: the holder items carry no decimals on this instance."""
    if not supply_raw or supply_raw <= 0:
        return None                      # cannot normalise; caller passed no supply
    d = _get(f"tokens/{addr}/holders", cache_key=f"hold_{addr.lower()}", max_age=_INFO)
    if is_deferred(d) or is_absent(d):
        return d
    items = d.get("items") if isinstance(d, dict) else None
    if not isinstance(items, list):
        return None
    wallets: list[int] = []
    contract_raw = 0
    n_contract = 0
    for it in items:
        holder = it.get("address") or {}
        h = (_hash(holder) or "").lower()
        v = _int(it.get("value"), 0)
        if isinstance(holder, dict) and holder.get("is_contract"):
            n_contract += 1
            contract_raw += v
            continue
        if h in _BURN:
            continue
        wallets.append(v)
    # Blockscout returns page 1 value-descending; we keep that order (no re-sort needed,
    # but sorting is free insurance against a future ordering change).
    wallets.sort(reverse=True)
    top_n = wallets[:n]
    return {"top10_pct": 100.0 * sum(top_n) / supply_raw,
            "top1_pct": 100.0 * top_n[0] / supply_raw if top_n else 0.0,
            "lp_pct": 100.0 * contract_raw / supply_raw,
            "n_contract_excluded": n_contract, "n_rows": len(items),
            "source": "blockscout"}


# ── address facts ─────────────────────────────────────────────────────────────────
def _creation_record(d: dict) -> dict:
    return {"creation_tx": d.get("creation_transaction_hash") or d.get("creation_tx_hash"),
            "listed_creator": d.get("creator_address_hash"),
            "is_contract": bool(d.get("is_contract"))}


def address_info(addr: str):
    """GET addresses/{addr} → {address, is_contract, is_verified, is_scam, reputation,
    proxy_type, impl_name, impl_address, creation_tx, listed_creator} | NOT_FOUND | None.
    The whole response is cached INFO_CACHE_MIN (is_scam/reputation/impl are mutable); the
    immutable creation subset is ALSO written to a forever cache that creation_of() reads,
    so repeat attribution never re-spends the rate budget on a mutable-TTL fetch."""
    d = _get(f"addresses/{addr}", cache_key=f"addr_{addr.lower()}", max_age=_INFO)
    if is_deferred(d) or is_absent(d):
        return d
    if not isinstance(d, dict) or "is_contract" not in d:
        return None
    impls = d.get("implementations") or []
    impl = impls[0] if impls and isinstance(impls[0], dict) else {}
    rec = _creation_record(d)
    if rec["creation_tx"]:
        _write_cache(_cache_path(f"creation_{addr.lower()}"), rec)
    return {"address": addr, "is_contract": rec["is_contract"],
            "is_verified": bool(d.get("is_verified")), "is_scam": bool(d.get("is_scam")),
            "reputation": d.get("reputation"), "proxy_type": d.get("proxy_type"),
            "impl_name": impl.get("name"), "impl_address": impl.get("address_hash"),
            "creation_tx": rec["creation_tx"], "listed_creator": rec["listed_creator"]}


def creation_of(addr: str):
    """{creation_tx, listed_creator} | NOT_FOUND | None. `listed_creator` is Blockscout's
    creator field — for launchpad tokens that is the FACTORY. Immutable → forever cache
    (this module's record, then the legacy host-tagged file, then a live address_info).
    An existing address with no creation tx (an EOA) is NOT_FOUND — not cached, since a
    CREATE2 deployment could still land there."""
    rec = _read_cache(_cache_path(f"creation_{addr.lower()}"), _FOREVER)
    if not rec:
        for legacy in (_legacy_addr_cache(addr), _legacy_addr_cache(addr.lower())):
            old = _read_cache(legacy, None)
            if isinstance(old, dict) and old.get("creation_transaction_hash"):
                rec = _creation_record(old)
                _write_cache(_cache_path(f"creation_{addr.lower()}"), rec)
                break
    if not rec:
        info = address_info(addr)        # writes the forever record as a side effect
        if is_deferred(info) or is_absent(info):
            return info
        rec = {"creation_tx": info["creation_tx"], "listed_creator": info["listed_creator"]}
    if not rec.get("creation_tx"):
        return NOT_FOUND
    return {"creation_tx": rec["creation_tx"], "listed_creator": rec.get("listed_creator")}


# ── transactions ──────────────────────────────────────────────────────────────────
def tx_details(tx_hash: str):
    """GET transactions/{tx} → {hash, sender, to, method, block, timestamp, status}
    | NOT_FOUND | None. Immutable → forever cache (a 404 is not cached: a tx can be
    indexed seconds after it is mined)."""
    d = _get(f"transactions/{tx_hash}", cache_key=f"tx_{tx_hash.lower()}", max_age=_FOREVER)
    if is_deferred(d) or is_absent(d):
        return d
    if not isinstance(d, dict) or "from" not in d:
        return None
    return {"hash": tx_hash, "sender": _hash(d.get("from")), "to": _hash(d.get("to")),
            "method": d.get("method"), "block": d.get("block_number") or d.get("block"),
            "timestamp": d.get("timestamp"), "status": d.get("status")}


def tx_sender(tx_hash: str):
    """THE deployer-attribution primitive: the tx's `from`. str | NOT_FOUND | None."""
    d = tx_details(tx_hash)
    if is_deferred(d) or is_absent(d):
        return d
    return d.get("sender") or None


def creator_of(addr: str):
    """{deployer, creation_tx, via:"direct"|"factory", listed_creator} | NOT_FOUND | None.
    deployer = the creation transaction's SENDER, never the factory. Both inputs are
    forever-cached, so this is free after the first resolution."""
    c = creation_of(addr)
    if is_deferred(c) or is_absent(c):
        return c
    sender = tx_sender(c["creation_tx"])
    if is_deferred(sender) or is_absent(sender):
        return sender                    # tx not indexed (yet) / host dark — propagate
    if not sender:
        return None                      # a tx record with no `from`: unusable, retry
    listed = c.get("listed_creator")
    via = "direct" if (listed or "").lower() == sender.lower() else "factory"
    return {"deployer": sender, "creation_tx": c["creation_tx"], "via": via,
            "listed_creator": listed}


def tx_logs(tx_hash: str):
    """GET transactions/{tx}/logs → list[{address, topics, data, decoded_method, event,
    index}] | NOT_FOUND | None. `decoded_method` is Blockscout's full decoded signature
    (e.g. "TokenCreated(uint256 ts, address creator, ...)") when the ABI is known, `event`
    the bare name. Immutable → the assembled list is cached forever."""
    key = _cache_path(f"logs_{tx_hash.lower()}")
    cached = _read_cache(key, _FOREVER)
    if isinstance(cached, list):
        return cached
    out: list[dict] = []
    params: dict | None = None
    for _page in range(_LOGS_MAX_PAGES):
        d = _get(f"transactions/{tx_hash}/logs", cache_key=None, params=params)
        if is_deferred(d) or is_absent(d):
            return d if not out else None   # a lost later page = the whole list is unknown
        items = d.get("items") if isinstance(d, dict) else None
        if not isinstance(items, list):
            return None
        for it in items:
            dec = it.get("decoded") or {}
            mc = dec.get("method_call") if isinstance(dec, dict) else None
            out.append({"address": _hash(it.get("address")),
                        "topics": [t for t in (it.get("topics") or []) if t],
                        "data": it.get("data"), "decoded_method": mc,
                        "event": mc.split("(")[0] if mc else None,
                        "index": it.get("index")})
        params = d.get("next_page_params")
        if not params:
            break
    _write_cache(key, out)
    return out


def _tx_created_contracts(tx_hash: str) -> list[str]:
    """Contract addresses created inside a tx (internal `create`/`create2` entries)."""
    d = _get(f"transactions/{tx_hash}/internal-transactions",
             cache_key=f"itx_{tx_hash.lower()}", max_age=_FOREVER)
    out = []
    if not isinstance(d, dict):
        return out
    for it in d.get("items", []):
        if "create" in str(it.get("type", "")).lower():
            created = _hash(it.get("created_contract")) or _hash(it.get("to"))
            if created:
                out.append(created)
    return out


def address_tx_count(addr: str) -> int | None:
    """The wallet's transaction count (GET addresses/{addr}/counters → transactions_count), so a
    caller can know whether a 2-page walk of its outgoing txs was COMPLETE. None when deferred."""
    d = _get(f"addresses/{addr}/counters", cache_key=f"actr_{addr.lower()}",
             max_age=config.INFO_CACHE_MIN * 60)
    if d is None or is_absent(d) or not isinstance(d, dict):
        return None
    try:
        return int(d.get("transactions_count") or 0)
    except (TypeError, ValueError):
        return None


def wallet_created_tokens(wallet: str, max_pages: int = 10) -> list[dict]:
    """All token contracts this wallet created: direct deployments + launchpad-factory
    calls. Factory detection is METHOD-HINT based on ANY contract call (confirmed via
    internal `create` traces), so unlisted launchpads are still caught — the chain has
    more launchpads (Noxa, Arrow, ...) than anyone's config. Paginates the wallet's
    outgoing txs up to max_pages x 50. Not used by the runtime (the entry lab may use it);
    returns a list — an unavailable page simply ends the walk (partial, never fabricated)."""
    created: list[dict] = []
    params: dict = {"filter": "from"}
    for _page in range(max_pages):
        d = _get(f"addresses/{wallet}/transactions",
                 cache_key=None, params=params)  # tx lists grow — don't cache
        items = d.get("items", []) if isinstance(d, dict) else []
        if not items:
            break
        for tx in items:
            h = tx.get("hash")
            method = str(tx.get("method") or "").lower()
            ts = tx.get("timestamp")
            if tx.get("to") is None and tx.get("created_contract"):
                created.append({"token": _hash(tx["created_contract"]),
                                "via": "direct", "tx": h, "timestamp": ts})
            elif method:
                if any(k in method for k in _NOT_CREATE):
                    continue
                if not any(k in method for k in _CREATE_HINTS):
                    continue
                for c in _tx_created_contracts(h):
                    created.append({"token": c, "via": "factory", "tx": h,
                                    "timestamp": ts, "method": tx.get("method")})
        nxt = d.get("next_page_params")
        if not nxt:
            break
        params = {"filter": "from", **nxt}
    # dedupe preserving order (EIP-1167 clones can surface in multiple traces)
    seen: set[str] = set()
    out = []
    for c in created:
        if c["token"] and c["token"].lower() not in seen:
            seen.add(c["token"].lower())
            out.append(c)
    return out


# ── smoke test ────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    from http_client import health_report

    M = config.MIZUKARA
    t = token_info(M)
    print(f"token_info: {t}")
    assert isinstance(t, dict) and t["symbol"] == "MIZUKARA" and t["decimals"] == 18, t
    assert t["holders"] and t["holders"] > 1000, t
    assert t["supply_raw"] > 0 and abs(t["supply"] - t["supply_raw"] / 1e18) < 1e-6

    c = token_counters(M)
    print(f"token_counters: {c}")
    assert isinstance(c, dict) and c["transfers_count"] > c["holders_count"] > 0, c

    th = top_holders(M, t["supply_raw"])
    print(f"top_holders: {th}")
    assert isinstance(th, dict) and 0.0 < th["top10_pct"] < 100.0, th
    assert th["lp_pct"] > 0 and th["n_contract_excluded"] >= 1, th
    assert th["top1_pct"] <= th["top10_pct"], th

    a = address_info(M)
    print(f"address_info: {a}")
    assert isinstance(a, dict) and a["is_contract"] is True, a
    assert a["proxy_type"] == "eip1167" and a["impl_name"] == "FlapTaxTokenV3", a
    assert a["creation_tx"], a
    assert a["listed_creator"].lower() == config.FLAP_ROUTER.lower(), a["listed_creator"]

    cr = creator_of(M)
    print(f"creator_of: {cr}")
    assert isinstance(cr, dict) and cr["deployer"].lower() == config.MIZUKARA_DEV.lower(), cr
    assert cr["via"] == "factory" and cr["creation_tx"] == a["creation_tx"], cr

    logs = tx_logs(a["creation_tx"])
    events = [l["event"] for l in logs] if isinstance(logs, list) else logs
    print(f"tx_logs({a['creation_tx'][:12]}…): {len(logs) if isinstance(logs, list) else logs} "
          f"logs, events={events}")
    assert isinstance(logs, list) and "TokenCreated" in events and "TokenBought" in events, events
    flap = [l for l in logs if l["event"] == "TokenCreated"][0]
    assert flap["topics"][0] == config.TOPIC_FLAP_TOKEN_CREATED, flap["topics"]
    assert flap["address"].lower() == config.FLAP_ROUTER.lower(), flap["address"]

    bogus = token_info("0x0000000000000000000000000000000000000001")
    print(f"bogus token_info: {bogus!r}")
    assert is_absent(bogus), bogus

    print("\n--- source health ---")
    print(health_report())
    print("blockscout smoke: OK")
