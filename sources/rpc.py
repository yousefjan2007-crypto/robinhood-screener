"""
Robinhood Chain JSON-RPC primitives — the chain's OWN answers, over http_client.post_json.

What this source uniquely provides: facts no third-party indexer can get wrong or take away —
owner()/decimals() straight from the token, the V2 pair from the factory, LP burn measured as
pair-token balances at the burn addresses (GoPlus reported MIZUKARA's LP as 1 holder of dust and
ScanHood said "unknown"; balanceOf(0xdead) on the pair says 100% — the eth_call is the only
trustworthy version), the honeypot / sell-tax round-trip simulated on the real router
(getAmountsOut both ways, no keys, no funds), first-block discovery via factory/launchpad logs,
and the FALLBACK deployer-attribution path (find_creation_tx / creation_of) for when the
explorer goes dark — it did, on 2026-08-23, behind a Cloudflare challenge.

Failure semantics (the three-way contract, everywhere):
    ok        — a decoded value
    absent    — a deterministic "does not exist": zero address from getPair, a router revert
                ("no V2 route" is not evidence of a trap), owner() returning nothing
    deferred  — None: network / 429 / 5xx / an undecodable answer. Retry later, NEVER score.
eth_call_ex() is where the distinction is made: the node's explicit revert is
{"code":3,"message":"execution reverted","data":"0x"} (measured 2026-09-12), and a revert is
an ANSWER, not a failure. Nothing here raises on bad data; one bad token never kills a run.

Throughput comes from Multicall3 (aggregate3, allowFailure=true), not from request rate: the
public RPC 429s above ~5 req/s from one IP (config pins 2 Hz), so PASS-1 packs ≤50 calls per
eth_call and falls back to per-token single calls only when a whole batch fails.

Constraint worth stating: the public RPC is NOT an archive node (historical *state* is pruned
to roughly the last 20k blocks, so an eth_getCode binary search for the creation block silently
returns garbage — measured, do not reintroduce it). Historical *logs* are served all the way
back to genesis, which is why attribution goes through eth_getLogs, walking backward in windows
bounded by CREATION_MAX_LOOKBACK_BLOCKS. eth_getLogs hard-errors at 10,000 matches (-32000),
so every range scan halves its window on error.
"""
from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config                                                        # noqa: E402
from http_client import post_json, NOT_FOUND, is_absent, is_deferred  # noqa: E402

# Creation-scan geometry (config.py dropped RPC_CREATION_* when discovery moved to the block
# cursor; the attribution fallback still needs them). 500k blocks ≈ 14 h at 0.101 s/block —
# one call resolves anything the recurring scanner actually evaluates; 5M blocks ≈ 5.8 days
# bounds the walk so an old token costs ten calls, then is left to the explorer/cache.
CREATION_WINDOW_BLOCKS = 500_000
CREATION_MAX_LOOKBACK_BLOCKS = 5_000_000
MULTICALL_CHUNK = 50               # calls per aggregate3 eth_call; the node's eth_call gas cap is
                                   # generous but return data for 50 quotes stays < 20 KB
_ZERO_ADDR = "0x" + "0" * 40
_DEAD_ADDR = config.BURN_ADDRESSES[0]
_MIN_LOG_WINDOW = 512

_id = [0]


# ── JSON-RPC transport ───────────────────────────────────────────────────────────
def _call(method: str, params: list):
    """One JSON-RPC call. Returns (result, None) | (None, error message). A NOT_FOUND from the
    client (HTTP 400/404 from an RPC host) is a transport problem, not a chain answer."""
    _id[0] += 1
    resp = post_json(config.RPC_URL, {"jsonrpc": "2.0", "id": _id[0],
                                      "method": method, "params": params})
    if is_deferred(resp) or is_absent(resp) or not isinstance(resp, dict):
        return None, "network failure"
    if "error" in resp:
        err = resp["error"]
        msg = err.get("message", err) if isinstance(err, dict) else err
        return None, str(msg)
    return resp.get("result"), None


def block_number() -> int | None:
    r, _ = _call("eth_blockNumber", [])
    try:
        return int(r, 16) if r else None
    except (TypeError, ValueError):
        return None


def eth_call(to: str, data: str, block: str = "latest") -> str | None:
    r, _ = _call("eth_call", [{"to": to, "data": data}, block])
    return r


def eth_call_ex(to: str, data: str, block: str = "latest") -> tuple:
    """eth_call with the reason attached: (hex result, None) on success;
    (None, "execution reverted") when the node returned a JSON-RPC error mentioning a revert
    (the router's explicit {"code":3,"message":"execution reverted","data":"0x"});
    (None, "network failure") when the transport failed; (None, <message>) otherwise.
    Load-bearing: a revert is a deterministic ANSWER (absent), a network failure is deferred."""
    r, err = _call("eth_call", [{"to": to, "data": data}, block])
    if err is None:
        return (r if isinstance(r, str) else "0x"), None
    if err == "network failure":
        return None, "network failure"
    if "revert" in err.lower():
        return None, "execution reverted"
    return None, err


def get_logs(address, topics: list, from_block: int, to_block: int):
    """One eth_getLogs call. `address` may be a string, a list of addresses (the node accepts
    an address array — one call covers every discovery source) or None.
    Returns (logs list | None, error string | None)."""
    flt = {"fromBlock": hex(from_block), "toBlock": hex(to_block), "topics": topics}
    if address:
        flt["address"] = address
    return _call("eth_getLogs", [flt])


def tx_details(tx_hash: str) -> dict | None:
    """{sender, to, block} for a transaction, straight from the node."""
    r, _ = _call("eth_getTransactionByHash", [tx_hash])
    if not isinstance(r, dict) or not r.get("from"):
        return None
    blk = r.get("blockNumber")
    try:
        blk = int(blk, 16) if isinstance(blk, str) else blk
    except ValueError:
        blk = None
    return {"sender": r["from"], "to": r.get("to"), "block": blk}


def tx_sender(tx_hash: str) -> str | None:
    """The deployer-attribution primitive, RPC edition: the creation tx's sender."""
    d = tx_details(tx_hash)
    return d["sender"] if d else None


# ── ABI helpers (hand-rolled: no web3/eth_abi on system python, and none wanted) ──
def _strip(h) -> str:
    h = h or ""
    return h[2:] if h.startswith("0x") else h


def enc_uint(n: int) -> str:
    """uint256 as 64 hex chars."""
    return "%064x" % int(n)


def enc_addr(a: str) -> str:
    """address as a left-padded 32-byte word (64 hex chars)."""
    return _strip(a).lower().rjust(64, "0")


def dec_uint(hexdata: str, word: int) -> int:
    """Word `word` (0-based) of ABI-encoded data as int. Raises ValueError when the data is
    shorter than that word — every public caller catches and maps to deferred/absent."""
    h = _strip(hexdata)
    s = h[word * 64:(word + 1) * 64]
    if len(s) < 64:
        raise ValueError("abi data shorter than word %d" % word)
    return int(s, 16)


def dec_addr(hexdata: str, word: int) -> str:
    """Word `word` as a 0x-prefixed lowercase address (the last 20 bytes of the word)."""
    h = _strip(hexdata)
    s = h[word * 64:(word + 1) * 64]
    if len(s) < 64:
        raise ValueError("abi data shorter than word %d" % word)
    return "0x" + s[24:].lower()


def encode_call(selector: str, *args) -> str:
    """selector + ABI head/tail encoding. Each arg is ('uint', n) | ('addr', a) |
    ('addr[]', [..]) | ('uint[]', [..]). Dynamic arrays are encoded as a head offset (relative
    to the start of the argument area) plus a tail of length + items, so
    getAmountsOut(uint256,address[]) = selector + uint + 0x40 + len + addrs."""
    head, tail = [], []
    head_size = 32 * len(args)
    for kind, val in args:
        if kind == "uint":
            head.append(enc_uint(val))
        elif kind == "addr":
            head.append(enc_addr(val))
        elif kind in ("addr[]", "uint[]"):
            enc = enc_addr if kind == "addr[]" else enc_uint
            body = enc_uint(len(val)) + "".join(enc(v) for v in val)
            head.append(enc_uint(head_size + sum(len(t) // 2 for t in tail)))
            tail.append(body)
        else:
            raise ValueError("unknown abi kind %r" % kind)
    sel = selector if selector.startswith("0x") else "0x" + selector
    return sel + "".join(head) + "".join(tail)


def _dec_uint_array(hexdata: str) -> list:
    """uint256[] return value → list of ints (offset word, length word, items)."""
    off = dec_uint(hexdata, 0) // 32
    n = dec_uint(hexdata, off)
    return [dec_uint(hexdata, off + 1 + i) for i in range(n)]


# ── Multicall3 ───────────────────────────────────────────────────────────────────
def _enc_aggregate3(calls: list) -> str:
    """aggregate3((address target, bool allowFailure, bytes callData)[]) with allowFailure=true.
    Layout: selector, outer offset 0x20, N, N tuple offsets relative to the start of the
    array's element area (right after N), then each tuple =
    [target, allowFailure, 0x60 (offset of callData inside the tuple), len, callData padded]."""
    tuples = []
    for target, data in calls:
        d = _strip(data).lower()
        padded = d + "0" * ((64 - len(d) % 64) % 64)
        tuples.append(enc_addr(target) + enc_uint(1) + enc_uint(0x60)
                      + enc_uint(len(d) // 2) + padded)
    offsets, off = [], 32 * len(tuples)
    for t in tuples:
        offsets.append(off)
        off += len(t) // 2
    return (config.SEL_AGGREGATE3 + enc_uint(0x20) + enc_uint(len(tuples))
            + "".join(enc_uint(o) for o in offsets) + "".join(tuples))


def _dec_aggregate3(hexdata: str, expected: int) -> list | None:
    """Result[] = [0x20, N, N offsets, each Result = (success, 0x40, len, bytes padded)] →
    [(success, "0x..."), ...]. None when the shape is wrong (callers fall back)."""
    try:
        h = _strip(hexdata)
        base = dec_uint(h, 0) * 2
        n = int(h[base:base + 64], 16)
        if n != expected:
            return None
        elems = base + 64
        out = []
        for i in range(n):
            off = int(h[elems + i * 64:elems + (i + 1) * 64], 16)
            p = elems + off * 2
            success = int(h[p:p + 64], 16) != 0
            boff = int(h[p + 64:p + 128], 16)
            q = p + boff * 2
            blen = int(h[q:q + 64], 16)
            data = h[q + 64:q + 64 + blen * 2]
            if len(data) != blen * 2:
                return None
            out.append((success, "0x" + data))
        return out
    except (ValueError, IndexError, TypeError):
        return None


def multicall3(calls: list, block: str = "latest") -> list | None:
    """Multicall3.aggregate3 over [(target, callData), ...], allowFailure=true for every call.
    Returns [(success, returndata hex), ...] in order, chunked at MULTICALL_CHUNK per eth_call;
    None on network failure or an undecodable answer (callers fall back to per-token calls).
    A per-call revert is (False, revert data), NOT a batch failure."""
    if not calls:
        return []
    out = []
    for i in range(0, len(calls), MULTICALL_CHUNK):
        chunk = calls[i:i + MULTICALL_CHUNK]
        try:
            data = _enc_aggregate3(chunk)
        except (ValueError, TypeError, AttributeError):
            return None
        r, err = eth_call_ex(config.MULTICALL3, data, block)
        if r is None:
            return None
        dec = _dec_aggregate3(r, len(chunk))
        if dec is None:
            return None
        out.extend(dec)
    return out


# ── small fact cache (decoded values, never raw responses) ───────────────────────
# post_json's cache would freeze whatever came back, error envelope included; caching the
# DECODED fact means a transient error is never remembered as a permanent one.
def _cache_path(name: str) -> str:
    return os.path.join(config.CACHE_DIR, "rpc_%s.json" % name)


def _cache_get(name: str, max_age_sec: float | None):
    p = _cache_path(name)
    try:
        if not os.path.exists(p):
            return None, False
        if max_age_sec is not None and time.time() - os.path.getmtime(p) > max_age_sec:
            return None, False
        with open(p) as f:
            return json.load(f).get("value"), True
    except Exception:
        return None, False


def _cache_put(name: str, value) -> None:
    p = _cache_path(name)
    try:
        tmp = "%s.%d.tmp" % (p, os.getpid())
        with open(tmp, "w") as f:
            json.dump({"value": value}, f)
        os.replace(tmp, p)
    except Exception:
        pass


# ── per-token single calls (the reference semantics PASS-1 must reproduce) ───────
def _owner_from(success: bool, data: str | None) -> str | None:
    """Map an owner() answer: zero → renounced, other → owned, revert/empty → no_owner_fn."""
    if not success or not data or len(_strip(data)) < 64:
        return "no_owner_fn"
    try:
        addr = dec_addr(data, 0)
    except ValueError:
        return "no_owner_fn"
    if addr == _ZERO_ADDR:
        return "renounced"
    if addr.lower() in config.PROTOCOL_OWNERS:
        return "protocol"                 # a launchpad's shared contract owns every clone; no dev holds the key
    return "owned"


def owner(token: str) -> str | None:
    """"renounced" | "owned" | "no_owner_fn" | None (deferred). An owner can mint/pause/
    blacklist, which is why REQUIRE_OWNER_RENOUNCED is a hard gate; a contract with no
    owner() at all (returns 0x, or reverts) cannot have one either."""
    r, err = eth_call_ex(token, config.SEL_OWNER)
    if r is None:
        if err == "execution reverted":
            return "no_owner_fn"
        return None
    return _owner_from(True, r)


def _uint_from(success: bool, data: str | None) -> int | None:
    if not success or not data:
        return None
    try:
        return dec_uint(data, 0)
    except ValueError:
        return None


def balance_of(token: str, holder: str) -> int | None:
    r, err = eth_call_ex(token, encode_call(config.SEL_BALANCE_OF, ("addr", holder)))
    return _uint_from(r is not None, r)


def total_supply(token: str) -> int | None:
    r, err = eth_call_ex(token, config.SEL_TOTAL_SUPPLY)
    return _uint_from(r is not None, r)


def _decimals_ex(token: str) -> tuple:
    """(decimals|None, deferred). None+False = the contract answered nothing / reverted (a
    codeless address, a non-ERC-20) — a FACT, not a retry; None+True = the node did not answer."""
    key = "dec_%s" % token.lower()
    v, hit = _cache_get(key, None)
    if hit and isinstance(v, int):
        return v, False
    r, err = eth_call_ex(token, config.SEL_DECIMALS)
    if r is None:
        return None, err != "execution reverted"
    d = _uint_from(True, r)
    if d is not None:
        _cache_put(key, d)
    return d, False


def decimals(token: str) -> int | None:
    """decimals() — immutable, cached forever once known. None when unknown (see _decimals_ex
    for whether that is a fact or a deferral)."""
    return _decimals_ex(token)[0]


def _pair_from(success: bool, data: str | None) -> tuple:
    """getPair answer → (status, pair|None). Zero address = absent (no V2 pair yet)."""
    if not success:
        return "absent", None
    try:
        p = dec_addr(data, 0)
    except ValueError:
        return "deferred", None
    return ("absent", None) if p == _ZERO_ADDR else ("ok", p)


def _get_pair_data(token: str) -> str:
    a, b = token.lower(), config.WETH.lower()
    lo, hi = (a, b) if int(a, 16) < int(b, 16) else (b, a)
    return encode_call(config.SEL_GET_PAIR, ("addr", lo), ("addr", hi))


def v2_pair(token: str) -> tuple:
    """(status ok|absent|deferred, pair address lowercase|None) from
    factory.getPair(min(token,WETH), max(token,WETH)). A non-zero answer is cached forever;
    a zero answer only INFO_CACHE_MIN minutes — a pair can appear later (bonding-curve
    graduation), so 'absent' must be re-asked."""
    key = "pair_%s" % token.lower()
    v, hit = _cache_get(key, None)
    if hit and isinstance(v, str) and v.startswith("0x") and v != _ZERO_ADDR:
        return "ok", v
    v, hit = _cache_get(key, config.INFO_CACHE_MIN * 60)
    if hit and v == _ZERO_ADDR:
        return "absent", None
    r, err = eth_call_ex(config.UNIV2_FACTORY, _get_pair_data(token))
    if r is None:
        return ("absent", None) if err == "execution reverted" else ("deferred", None)
    st, pair = _pair_from(True, r)
    if st == "ok":
        _cache_put(key, pair)
    elif st == "absent":
        _cache_put(key, _ZERO_ADDR)
    return st, pair


def v2_reserves(pair: str, token: str) -> tuple:
    """(status, (reserve_token, reserve_weth)|None) via getReserves(). V2's token0 is the
    numerically smaller address, which decides which reserve is the token's."""
    r, err = eth_call_ex(pair, config.SEL_GET_RESERVES)
    if r is None:
        return ("absent", None) if err == "execution reverted" else ("deferred", None)
    try:
        r0, r1 = dec_uint(r, 0), dec_uint(r, 1)
    except ValueError:
        return "deferred", None
    token_is_0 = int(token, 16) < int(config.WETH, 16)
    return "ok", ((r0, r1) if token_is_0 else (r1, r0))


def _lp_from_raw(total: int | None, burned: list, locked: list) -> dict:
    """Assemble the LP-burn fact from raw balances. pct None when total==0 or any leg deferred."""
    if total is None or any(b is None for b in burned) or any(b is None for b in locked):
        return {"status": "deferred", "pct": None, "burned_raw": None, "locker_raw": None,
                "total_raw": total, "source": "rpc"}
    b, l = sum(burned), sum(locked)
    pct = None if total == 0 else round(100.0 * (b + l) / total, 4)
    return {"status": "ok", "pct": pct, "burned_raw": b, "locker_raw": l,
            "total_raw": total, "source": "rpc"}


def lp_burned_pct(pair: str) -> dict:
    """{status, pct, burned_raw, locker_raw, total_raw, source:"rpc"} — LP tokens at
    0xdead + 0x0 + every config.LP_LOCKER_ADDRESSES, over the PAIR's totalSupply.
    Verified: MIZUKARA_POOL balanceOf(0xdead) == totalSupply → 100.0 (GoPlus and ScanHood
    both missed it)."""
    total = total_supply(pair)
    burned = [balance_of(pair, a) for a in config.BURN_ADDRESSES]
    locked = [balance_of(pair, a) for a in config.LP_LOCKER_ADDRESSES]
    return _lp_from_raw(total, burned, locked)


def _amounts_from(success: bool, data: str | None) -> tuple:
    """getAmountsOut answer → (last amount|None, status ok|revert|zero)."""
    if not success or not data or len(_strip(data)) < 128:
        return None, "revert"                 # empty return = no route, deterministic
    try:
        amts = _dec_uint_array(data)
    except ValueError:
        return None, "revert"
    if not amts:
        return None, "revert"
    return (amts[-1], "ok") if amts[-1] > 0 else (0, "zero")


def get_amounts_out(amount_in: int, path: list) -> tuple:
    """(last amount|None, status ok|revert|zero|deferred) from Router02.getAmountsOut."""
    data = encode_call(config.SEL_GET_AMOUNTS_OUT, ("uint", amount_in), ("addr[]", path))
    r, err = eth_call_ex(config.UNIV2_ROUTER, data)
    if r is None:
        return (None, "revert") if err == "execution reverted" else (None, "deferred")
    return _amounts_from(True, r)


def _honeypot_from_legs(buy_status: str, buy_out, sell_status: str | None, sell_back) -> dict:
    """Combine the two probe legs into the honeypot fact. 'No V2 route' on the buy leg is a
    pass-through (route none, honeypot None): the absence of a route is not evidence of a
    trap. A sell leg that reverts or returns zero on tokens we could buy IS."""
    out = {"route": "none", "buy_out_raw": None, "sell_back_wei": None,
           "roundtrip_loss_pct": None, "honeypot": None, "status": "ok"}
    if buy_status == "deferred":
        out["status"] = "deferred"
        return out
    if buy_status in ("revert", "zero"):
        return out
    out.update(route="v2", buy_out_raw=buy_out)
    if sell_status is None or sell_status == "deferred":
        out["status"] = "deferred"
        return out
    if sell_status in ("revert", "zero"):
        out.update(honeypot=True, sell_back_wei=0 if sell_status == "zero" else None)
        return out
    probe = config.HONEYPOT_PROBE_WETH_WEI
    out.update(sell_back_wei=sell_back, honeypot=False,
               roundtrip_loss_pct=round((1.0 - sell_back / probe) * 100.0, 4))
    return out


def honeypot_roundtrip(token: str, pair: str | None = None) -> dict:
    """{route "v2"|"none", buy_out_raw, sell_back_wei, roundtrip_loss_pct, honeypot bool|None,
    status ok|deferred}. Buy HONEYPOT_PROBE_WETH_WEI of the token, then quote selling ALL of
    it back; loss = fees + impact both ways (~4.8% measured on MIZUKARA). `pair` is accepted
    for interface symmetry with the batch path; the router resolves the pool itself."""
    buy_out, bst = get_amounts_out(config.HONEYPOT_PROBE_WETH_WEI, [config.WETH, token])
    if bst != "ok":
        return _honeypot_from_legs(bst, buy_out, None, None)
    sell_back, sst = get_amounts_out(buy_out, [token, config.WETH])
    return _honeypot_from_legs(bst, buy_out, sst, sell_back)


# ── PASS-1 chain facts: single-call reference and the Multicall3 batch ───────────
_FACT_KEYS = ("owner_state", "decimals", "pair", "lp_locked_pct", "lp_check_source",
              "burned_raw", "lp_total_raw", "roundtrip_loss_pct", "honeypot", "route",
              "status", "sources_used")


def _empty_facts() -> dict:
    return {"owner_state": None, "decimals": None, "pair": None, "lp_locked_pct": None,
            "lp_check_source": "unknown", "burned_raw": None, "lp_total_raw": None,
            "roundtrip_loss_pct": None, "honeypot": None, "route": "none",
            "status": "deferred", "sources_used": ["rpc"]}


def _finish_facts(f: dict, deferred_flags: list) -> dict:
    """status: ok when nothing was deferred, deferred when everything was, else partial."""
    if not any(deferred_flags):
        f["status"] = "ok"
    elif all(deferred_flags):
        f["status"] = "deferred"
    else:
        f["status"] = "partial"
    return f


def _apply_lp(f: dict, lp: dict) -> bool:
    """Write the LP fact into facts; returns True when it was deferred."""
    f["burned_raw"] = lp.get("burned_raw")
    f["lp_total_raw"] = lp.get("total_raw")
    f["lp_locked_pct"] = lp.get("pct")
    f["lp_check_source"] = "rpc_v2" if lp.get("pct") is not None else "unknown"
    return lp.get("status") == "deferred"


def _apply_hp(f: dict, hp: dict) -> bool:
    f["route"] = hp["route"]
    f["honeypot"] = hp["honeypot"]
    f["roundtrip_loss_pct"] = hp["roundtrip_loss_pct"]
    return hp["status"] == "deferred"


def chain_facts_one(token: str, pair: str | None = None) -> dict:
    """The single-call reference for one token (what chain_facts_many must reproduce)."""
    f = _empty_facts()
    dfl = []
    f["owner_state"] = owner(token)
    dfl.append(f["owner_state"] is None)
    f["decimals"], dec_deferred = _decimals_ex(token)
    dfl.append(dec_deferred)
    if pair:
        pst, p = "ok", pair.lower()
    else:
        pst, p = v2_pair(token)
    f["pair"] = p
    dfl.append(pst == "deferred")
    if pst == "ok":
        dfl.append(_apply_lp(f, lp_burned_pct(p)))
        dfl.append(_apply_hp(f, honeypot_roundtrip(token, p)))
    elif pst == "deferred":
        dfl.append(True)
    return _finish_facts(f, dfl)


def chain_facts_many(tokens: list, pairs: dict | None = None) -> dict:
    """PASS-1 batch: {token: facts} with facts = {owner_state, decimals, pair, lp_locked_pct,
    lp_check_source ("rpc_v2"|"unknown"), burned_raw, lp_total_raw, roundtrip_loss_pct,
    honeypot, route, status ("ok"|"deferred"|"partial"), sources_used:["rpc"]}.
    Three Multicall3 rounds: (1) owner, decimals, getPair for tokens with no known pair;
    (2) per pair: totalSupply, balanceOf(dead), balanceOf(zero), balanceOf(each locker) and
    the buy-leg quote; (3) the sell legs with the buy outputs. Per-call failures carry the
    single-call semantics; a whole round returning None falls back to the per-token
    functions for that round, so the output is identical either way."""
    # Only a 20-byte ADDRESS can be a V2 pair. Dexscreener reports a 32-byte pool ID for
    # Uniswap V4 pools (and V3 pools are not V2 pairs either); querying those as a pair made
    # every LP/route leg fail and the token read as "rpc dark". Anything else falls back to
    # factory.getPair, which answers 0x0 for a token with no V2 pair — a fact, not a failure.
    pairs = {k.lower(): v for k, v in (pairs or {}).items()
             if isinstance(v, str) and v.startswith("0x") and len(v) == 42}
    facts = {t: _empty_facts() for t in tokens}
    dfl = {t: [] for t in tokens}
    known_pair = {t: (pairs.get(t.lower()) or None) for t in tokens}
    pair_state = {}                                   # token → (status, pair)

    # ── round 1: owner, decimals (cache-aware), getPair (cache-aware) ──
    dec_cached, pair_cached = {}, {}
    for t in tokens:
        v, hit = _cache_get("dec_%s" % t.lower(), None)
        if hit and isinstance(v, int):
            dec_cached[t] = v
        if known_pair[t]:
            pair_state[t] = ("ok", known_pair[t].lower())
            continue
        v, hit = _cache_get("pair_%s" % t.lower(), None)
        if hit and isinstance(v, str) and v.startswith("0x") and v != _ZERO_ADDR:
            pair_state[t] = ("ok", v)
            continue
        v, hit = _cache_get("pair_%s" % t.lower(), config.INFO_CACHE_MIN * 60)
        if hit and v == _ZERO_ADDR:
            pair_state[t] = ("absent", None)
    tags, calls = [], []
    for t in tokens:
        tags.append(("owner", t)); calls.append((t, config.SEL_OWNER))
        if t not in dec_cached:
            tags.append(("dec", t)); calls.append((t, config.SEL_DECIMALS))
        if t not in pair_state:
            tags.append(("pair", t)); calls.append((config.UNIV2_FACTORY, _get_pair_data(t)))
    res = multicall3(calls) if calls else []
    dec_deferred = {t: False for t in tokens}
    if res is None:
        for t in tokens:
            facts[t]["owner_state"] = owner(t)
            facts[t]["decimals"], dec_deferred[t] = _decimals_ex(t)
            if t not in pair_state:
                pair_state[t] = v2_pair(t)
    else:
        for (kind, t), (ok, data) in zip(tags, res):
            if kind == "owner":
                facts[t]["owner_state"] = _owner_from(ok, data)
            elif kind == "dec":
                d = _uint_from(ok, data)
                facts[t]["decimals"] = d
                if d is not None:
                    _cache_put("dec_%s" % t.lower(), d)
            else:
                st, p = _pair_from(ok, data)
                pair_state[t] = (st, p)
                if st == "ok":
                    _cache_put("pair_%s" % t.lower(), p)
                elif st == "absent":
                    _cache_put("pair_%s" % t.lower(), _ZERO_ADDR)
        for t, d in dec_cached.items():
            facts[t]["decimals"] = d
    for t in tokens:
        st, p = pair_state.get(t, ("deferred", None))
        facts[t]["pair"] = p
        dfl[t].append(facts[t]["owner_state"] is None)
        dfl[t].append(dec_deferred[t])          # an inner-call revert inside a multicall that
        dfl[t].append(st == "deferred")         # answered is a fact, never a deferral
        if st == "deferred":
            dfl[t].append(True)

    # ── round 2: LP balances + buy leg for every token with a pair ──
    with_pair = [t for t in tokens if pair_state.get(t, ("x", None))[0] == "ok"]
    probe = config.HONEYPOT_PROBE_WETH_WEI
    lockers = list(config.LP_LOCKER_ADDRESSES)
    tags, calls = [], []
    for t in with_pair:
        p = pair_state[t][1]
        tags.append(("total", t)); calls.append((p, config.SEL_TOTAL_SUPPLY))
        for a in config.BURN_ADDRESSES:
            tags.append(("burn", t)); calls.append((p, encode_call(config.SEL_BALANCE_OF, ("addr", a))))
        for a in lockers:
            tags.append(("lock", t)); calls.append((p, encode_call(config.SEL_BALANCE_OF, ("addr", a))))
        tags.append(("buy", t))
        calls.append((config.UNIV2_ROUTER, encode_call(config.SEL_GET_AMOUNTS_OUT, ("uint", probe),
                                                       ("addr[]", [config.WETH, t]))))
    res = multicall3(calls) if calls else []
    buy_legs = {}                                     # token → (status, amount)
    if res is None:
        for t in with_pair:
            dfl[t].append(_apply_lp(facts[t], lp_burned_pct(pair_state[t][1])))
            buy_legs[t] = get_amounts_out(probe, [config.WETH, t])[::-1]
    else:
        raw = {t: {"total": None, "burn": [], "lock": []} for t in with_pair}
        for (kind, t), (ok, data) in zip(tags, res):
            if kind == "total":
                raw[t]["total"] = _uint_from(ok, data)
            elif kind in ("burn", "lock"):
                raw[t][kind].append(_uint_from(ok, data))
            else:
                amt, st = _amounts_from(ok, data)
                buy_legs[t] = (st, amt)
        for t in with_pair:
            dfl[t].append(_apply_lp(facts[t], _lp_from_raw(raw[t]["total"], raw[t]["burn"],
                                                           raw[t]["lock"])))

    # ── round 3: sell legs for every token that bought ──
    sellers = [t for t in with_pair if buy_legs.get(t, ("deferred",))[0] == "ok"]
    calls = [(config.UNIV2_ROUTER, encode_call(config.SEL_GET_AMOUNTS_OUT, ("uint", buy_legs[t][1]),
                                              ("addr[]", [t, config.WETH]))) for t in sellers]
    res = multicall3(calls) if calls else []
    sell_legs = {}
    if res is None:
        for t in sellers:
            sell_legs[t] = get_amounts_out(buy_legs[t][1], [t, config.WETH])[::-1]
    else:
        for t, (ok, data) in zip(sellers, res):
            amt, st = _amounts_from(ok, data)
            sell_legs[t] = (st, amt)
    for t in with_pair:
        bst, bamt = buy_legs.get(t, ("deferred", None))
        sst, samt = sell_legs.get(t, (None, None))
        dfl[t].append(_apply_hp(facts[t], _honeypot_from_legs(bst, bamt, sst, samt)))

    for t in tokens:
        _finish_facts(facts[t], dfl[t])
    return facts


# ── discovery: factory / launchpad logs ──────────────────────────────────────────
def _sources_lc() -> dict:
    return {addr.lower(): (kind, topic.lower()) for addr, (kind, topic)
            in config.DISCOVERY_LOG_SOURCES.items()}


def _candidate_leg(t0: str, t1: str) -> str | None:
    """The non-quote leg of a pair; None when both or neither legs are quote tokens."""
    q0, q1 = t0 in config.QUOTE_TOKENS, t1 in config.QUOTE_TOKENS
    if q0 == q1:
        return None
    return t1 if q0 else t0


def _decode_discovery(logs: list) -> list:
    """Decode raw eth_getLogs entries from the discovery sources into
    [{kind, token, pair, creator, block, tx, log_index}] sorted by (block, log_index).
    PairCreated: token0/token1 in topics[1..2], pair = data word 0. PoolCreated: same legs,
    fee in topics[3], pool = the LAST data word. Flap TokenCreated: every arg is non-indexed
    — data words [ts, creator, nonce, token, ...strings] — so creator = word 1, token = word 3."""
    srcs = _sources_lc()
    out = []
    for lg in logs or []:
        try:
            addr = str(lg.get("address", "")).lower()
            topics = [str(x).lower() for x in (lg.get("topics") or [])]
            if addr not in srcs or not topics or topics[0] != srcs[addr][1]:
                continue
            kind = srcs[addr][0]
            data = lg.get("data") or "0x"
            creator = pair = token = None
            extra: dict = {}
            if kind == "flap_create":
                creator = dec_addr(data, 1)
                token = dec_addr(data, 3)
            elif kind == "longlaunch_create":
                if len(topics) < 3:
                    continue
                token = "0x" + topics[1][-40:]
                creator = "0x" + topics[2][-40:]
                nwords = len(_strip(data)) // 64
                extra = {"numeraire": dec_addr(data, 1) if nwords > 1 else None,
                         "hook": dec_addr(data, 3) if nwords > 3 else None,
                         "launchpad": "bankr"}
            elif kind == "pons_create":
                if len(topics) < 4:
                    continue
                token = "0x" + topics[1][-40:]
                creator = "0x" + topics[3][-40:]
                extra = {"pool_id": topics[2], "launchpad": "pons"}
            elif kind == "pool_v4":
                if len(topics) < 4:
                    continue
                nwords = len(_strip(data)) // 64
                hook = dec_addr(data, 2) if nwords > 2 else None
                if config.V4_DISCOVERY_HOOKS_ONLY and (hook or "").lower() not in config.TRUSTED_V4_HOOKS:
                    continue
                # both legs are kept; the numeraire is resolved over the whole window below
                extra = {"legs": ("0x" + topics[2][-40:], "0x" + topics[3][-40:]),
                         "hook": hook, "pool_id": topics[1],
                         "launchpad": config.TRUSTED_V4_HOOKS.get((hook or "").lower())}
                token = extra["legs"][0]              # placeholder until resolved
            else:
                if len(topics) < 3:
                    continue
                t0, t1 = "0x" + topics[1][-40:], "0x" + topics[2][-40:]
                token = _candidate_leg(t0, t1)
                if token is None:
                    continue
                nwords = len(_strip(data)) // 64
                pair = dec_addr(data, 0 if kind == "pair_v2" else nwords - 1)
            rec = {"kind": kind, "token": token, "pair": pair, "creator": creator,
                   "block": int(lg.get("blockNumber", "0x0"), 16),
                   "tx": lg.get("transactionHash"),
                   "log_index": int(lg.get("logIndex") or "0x0", 16)}
            rec.update(extra)
            out.append(rec)
        except (ValueError, TypeError, AttributeError):
            continue                                  # one malformed log never kills a run
    out = _resolve_v4_legs(out)
    out.sort(key=lambda d: (d["block"], d["log_index"]))
    return out


def _resolve_v4_legs(recs: list) -> list:
    """pool_v4 rows carry both legs; the numeraire side is whichever leg is a quote token, a
    numeraire declared by a longlaunch_create row in the same window, or a currency seen on
    >= NUMERAIRE_MIN_POOLS_IN_WINDOW pools in the window (numeraires repeat, launches do not).
    Both or neither ⇒ the row is dropped. A token seen by both a longlaunch_create and a
    pool_v4 row keeps ONE record: the create row's creator/numeraire plus the pool's id/hook."""
    from collections import Counter
    declared = {str(r.get("numeraire") or "").lower() for r in recs if r["kind"] == "longlaunch_create"}
    freq = Counter()
    for r in recs:
        if r["kind"] == "pool_v4":
            for leg in r["legs"]:
                freq[leg.lower()] += 1
    def is_num(a: str) -> bool:
        a = a.lower()
        return a in config.QUOTE_TOKENS or a in declared or freq[a] >= config.NUMERAIRE_MIN_POOLS_IN_WINDOW
    resolved = []
    for r in recs:
        if r["kind"] != "pool_v4":
            resolved.append(r)
            continue
        a, b = r.pop("legs")
        na, nb = is_num(a), is_num(b)
        if na == nb:
            continue
        r["token"], r["numeraire"] = (b, a) if na else (a, b)
        resolved.append(r)
    merged: dict = {}
    for r in resolved:
        t = r["token"].lower()
        cur = merged.get(t)
        if cur is None:
            merged[t] = r
            continue
        keep, other = (cur, r) if cur["kind"] == "longlaunch_create" else ((r, cur) if r["kind"] == "longlaunch_create" else (cur, r))
        for k in ("pool_id", "hook", "numeraire", "creator", "launchpad"):
            if keep.get(k) in (None, "") and other.get(k) not in (None, ""):
                keep[k] = other[k]
        merged[t] = keep
    return list(merged.values())


def creator_launches(launcher: str, head: int | None = None) -> list | None:
    """Every token `launcher` created through LongLaunch over the factory's whole life, in
    block order — ONE indexed eth_getLogs (0.5 s measured). None when the node did not answer.
    A count at or above LAUNCH_SERVICE_MIN_CREATES marks an agent/service wallet that launches
    for many users: its history is not this token's dev history (pass-through, not a reject)."""
    if not launcher:
        return None
    topic = "0x" + "0" * 24 + launcher.lower()[2:]
    logs, _err = get_logs(config.LONGLAUNCH_FACTORY, [config.TOPIC_LONGLAUNCH_CREATE, None, topic],
                          0, head or block_number() or 0)
    if logs is None:
        return None
    out = []
    for lg in logs:
        try:
            tps = lg.get("topics") or []
            out.append(("0x" + str(tps[1])[-40:].lower(), int(lg.get("blockNumber", "0x0"), 16)))
        except (IndexError, ValueError, TypeError):
            continue
    out.sort(key=lambda x: x[1])
    return [t for t, _b in out]


def creator_launch_count(launcher: str, head: int | None = None) -> int | None:
    l = creator_launches(launcher, head)
    return None if l is None else len(l)


decode_discovery = _decode_discovery   # public alias for callers that own the window loop


def discover_logs(from_block: int, to_block: int) -> list:
    """ONE eth_getLogs over every config.DISCOVERY_LOG_SOURCES address (address array) with
    topics=[[each topic0]], decoded. Returns [] on failure — callers keep their cursor where
    it was, because an empty window and a failed window must not look alike; use
    discover_logs_raw_status when the distinction matters."""
    logs, err = _discover_raw(from_block, to_block)
    return _decode_discovery(logs) if logs is not None else []


def _discover_raw(from_block: int, to_block: int):
    addrs = list(config.DISCOVERY_LOG_SOURCES)
    topics = [[topic for (_kind, topic) in config.DISCOVERY_LOG_SOURCES.values()]]
    return get_logs(addrs, topics, from_block, to_block)


def discover_logs_windowed(from_block: int, to_block: int, window: int | None = None):
    """Yield (window_end_block, decoded_list) walking config.LOG_WINDOW_BLOCKS windows with
    get_logs_windowed semantics (halving on the 10k-log error)."""
    addrs = list(config.DISCOVERY_LOG_SOURCES)
    topics = [[topic for (_kind, topic) in config.DISCOVERY_LOG_SOURCES.values()]]
    for end, logs in get_logs_windowed(addrs, topics, from_block, to_block, window=window):
        yield end, _decode_discovery(logs)


def swaps_in_first_blocks(pair: str, from_block: int, n: int) -> int | None:
    """Count of V2 Swap logs on the pair in [from_block, from_block+n-1] — the sniper gate's
    raw material. None when the node did not answer."""
    logs, err = get_logs(pair, [config.TOPIC_SWAP_V2], from_block, from_block + max(n, 1) - 1)
    return len(logs) if isinstance(logs, list) else None


# ── attribution fallback (creation tx via earliest log) ──────────────────────────
def find_creation_tx(token: str, head: int | None = None) -> dict | None:
    """The token's FIRST-EVER log → the transaction that created it.

    An ERC-20 emits its mint Transfer inside the creation transaction, so the earliest
    log an address ever emits identifies that transaction. Walks backward from head in
    windows and stops one window PAST the earliest log found — an empty earlier window
    is the proof that nothing precedes it. Returns {creation_tx, block} or None when the
    token is older than the lookback bound (or the node errored), which the caller must
    treat as "unknown", never as "no deployer".
    """
    head = head if head is not None else block_number()
    if not head:
        return None
    window = CREATION_WINDOW_BLOCKS
    budget = CREATION_MAX_LOOKBACK_BLOCKS
    earliest = None
    hi = head
    scanned = 0
    while hi >= 0 and scanned < budget:
        lo = max(0, hi - window + 1)
        logs, err = get_logs(token, [], lo, hi)
        if logs is None:
            if window > _MIN_LOG_WINDOW:  # range too wide for the node — narrow and retry
                window //= 2
                continue
            print(f"  [rpc] creation scan gave up on {token[:10]}…: {err}")
            return None
        if logs:
            for lg in logs:
                try:
                    key = (int(lg.get("blockNumber", "0x0"), 16),
                           int(lg.get("logIndex", "0x0") or "0x0", 16))
                except (TypeError, ValueError):
                    continue
                if earliest is None or key < earliest[0]:
                    earliest = (key, lg)
        elif earliest is not None:
            break                       # a clean gap below the earliest log → that is it
        scanned += hi - lo + 1
        hi = lo - 1
    if earliest is None:
        return None
    lg = earliest[1]
    return {"creation_tx": lg.get("transactionHash"), "block": earliest[0][0],
            "exhaustive": True}


def creation_of(token: str) -> dict | None:
    """RPC fallback for blockscout.creation_of + tx_sender in one call chain:
    {deployer, creation_tx, via}. `via` is 'direct' when the creation tx deployed the
    contract itself (no `to`), 'factory' when it called a launchpad."""
    c = find_creation_tx(token)
    if not c or not c.get("creation_tx"):
        return None
    d = tx_details(c["creation_tx"])
    if not d or not d.get("sender"):
        return None
    return {"deployer": d["sender"], "creation_tx": c["creation_tx"],
            "via": "direct" if not d.get("to") else "factory",
            "block": c.get("block")}


def get_logs_windowed(address, topics: list, from_block: int, to_block: int,
                      window: int | None = None, on_progress=None):
    """Yield (window_end_block, logs) for each scanned window from_block→to_block.
    Halves the window on range-too-large errors (down to 512 blocks minimum);
    a persistent failure at minimum window is skipped with a warning rather than
    aborting the whole sweep."""
    window = window or config.LOG_WINDOW_BLOCKS
    start = from_block
    while start <= to_block:
        w = window
        while True:
            end = min(start + w - 1, to_block)
            logs, err = get_logs(address, topics, start, end)
            if logs is not None:
                yield end, logs
                break
            if w > _MIN_LOG_WINDOW:
                w //= 2
                continue
            print(f"  [rpc] giving up on blocks {start}-{end}: {err}")
            yield end, []
            break
        if on_progress:
            on_progress(end)
        start = end + 1


# ── smoke test ───────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    MIZ, POOL = config.MIZUKARA, config.MIZUKARA_POOL
    BOGUS = "0x" + "0" * 39 + "1"
    print("robinhood_screener sources/rpc")

    head = block_number()
    print(f"  head block: {head}")
    assert head and head > 17_000_000, "unexpected head"

    # ABI round-trip on a known encoding: getAmountsOut(uint256,address[]) layout
    enc = encode_call(config.SEL_GET_AMOUNTS_OUT, ("uint", 5), ("addr[]", [config.WETH, MIZ]))
    assert enc[:10] == config.SEL_GET_AMOUNTS_OUT and len(enc) == 10 + 5 * 64
    assert dec_uint(enc[10:], 1) == 0x40 and dec_uint(enc[10:], 2) == 2
    assert dec_addr(enc[10:], 4) == MIZ.lower()
    print("  abi: getAmountsOut encoding = selector + uint + 0x40 + len + addrs  ok")

    ex = eth_call_ex(config.UNIV2_ROUTER, encode_call(config.SEL_GET_AMOUNTS_OUT, ("uint", 10 ** 16),
                                                      ("addr[]", [config.WETH, BOGUS])))
    print(f"  eth_call_ex bogus route: {ex}")
    assert ex == (None, "execution reverted"), "router revert must be classified as a revert"

    o = owner(MIZ)
    d = decimals(MIZ)
    print(f"  MIZUKARA owner()={o}  decimals()={d}")
    assert o == "renounced" and d == 18

    st, pair = v2_pair(MIZ)
    print(f"  v2_pair: {st} {pair}")
    assert st == "ok" and pair == POOL.lower()
    rs, res = v2_reserves(pair, MIZ)
    print(f"  reserves: {rs} token={res[0] if res else None} weth={res[1] if res else None}")
    assert rs == "ok" and res[1] > 0

    lp = lp_burned_pct(POOL)
    print(f"  lp_burned_pct: {lp}")
    assert lp["status"] == "ok" and lp["pct"] == 100.0, "MIZUKARA LP is 100% burned to 0xdead"

    hp = honeypot_roundtrip(MIZ, pair)
    print(f"  honeypot_roundtrip: {hp}")
    assert hp["route"] == "v2" and hp["honeypot"] is False and hp["status"] == "ok"
    # Floor = two V2 fees; ceiling = the tax-trap gate. (The survey's "4.8%" was a 0.1 WETH buy
    # against a 1,000,000-token sell — mismatched legs; at the 0.01 WETH config probe the true
    # round trip on MIZUKARA is ~1.4%: 0.6% fees + ~0.85% impact on a 2.3 WETH reserve.)
    fee_floor = 2 * config.UNIV2_FEE_BPS / 100.0
    assert fee_floor <= hp["roundtrip_loss_pct"] < config.SELL_TAX_MAX_ROUNDTRIP_PCT, \
        f"expected {fee_floor}%..{config.SELL_TAX_MAX_ROUNDTRIP_PCT}% on MIZUKARA"

    # Multicall3 raw: owner + decimals in one eth_call
    mc = multicall3([(MIZ, config.SEL_OWNER), (MIZ, config.SEL_DECIMALS), (BOGUS, config.SEL_OWNER)])
    print(f"  multicall3 raw: {mc}")
    assert mc and mc[0][0] and dec_addr(mc[0][1], 0) == _ZERO_ADDR and dec_uint(mc[1][1], 0) == 18
    assert mc[2] == (True, "0x"), "a codeless target succeeds with empty data"

    many = chain_facts_many([MIZ, BOGUS])
    one = chain_facts_one(MIZ)
    print(f"  chain_facts_many[MIZ]: {many[MIZ]}")
    print(f"  chain_facts_one [MIZ]: {one}")
    for k in _FACT_KEYS:
        if k == "roundtrip_loss_pct":
            assert abs(many[MIZ][k] - one[k]) < 0.5, k   # reserves may move between calls
        else:
            assert many[MIZ][k] == one[k], f"batch/single mismatch on {k}"
    assert many[MIZ]["status"] == "ok" and many[MIZ]["lp_check_source"] == "rpc_v2"
    print(f"  chain_facts_many[bogus]: {many[BOGUS]}")
    assert many[BOGUS]["owner_state"] in ("no_owner_fn", None) and many[BOGUS]["route"] == "none"
    assert many[BOGUS]["pair"] is None and many[BOGUS]["honeypot"] is None
    assert many[BOGUS]["status"] == "ok", "every answer about a codeless address is a fact"

    ob = owner(BOGUS)
    hb = honeypot_roundtrip(BOGUS)
    print(f"  bogus single: owner={ob} honeypot={hb}")
    assert ob in ("no_owner_fn", None) and hb["route"] == "none" and hb["honeypot"] is None

    found = discover_logs(head - config.DISCOVERY_BACKFILL_BLOCKS, head)
    counts = {}
    for e in found:
        counts[e["kind"]] = counts.get(e["kind"], 0) + 1
    print(f"  discover_logs last {config.DISCOVERY_BACKFILL_BLOCKS} blocks: {len(found)} → {counts}")
    if found:
        print(f"    first: {found[0]}")
        assert all(e["token"].startswith("0x") and len(e["token"]) == 42 for e in found)
        assert found == sorted(found, key=lambda e: (e["block"], e["log_index"]))
    assert counts.get("pair_v2", 0) + counts.get("flap_create", 0) > 0, "expected launches in ~5 min"
    v2 = [e for e in found if e["kind"] == "pair_v2"]
    if v2:
        n = swaps_in_first_blocks(v2[0]["pair"], v2[0]["block"], config.SNIPER_FIRST_BLOCKS)
        print(f"  swaps_in_first_blocks({v2[0]['pair'][:10]}…, {config.SNIPER_FIRST_BLOCKS}) = {n}")
        assert n is None or n >= 0
    print("  OK")
