"""
safety.py — THE ADAPTER BOUNDARY between the vendor modules and the pure screen.

Fuses rpc / blockscout / geckoterminal / scanhood / robinx (+ the archived legacy
token_creators.json index) into the ONE flat dict that screen.hard_gates / screen.hc_checks
consume. screen.py never sees http_client's NOT_FOUND sentinel or a deferred None marker —
every three-way status is mapped HERE, under one rule:

    A gate fails closed only on a POSITIVE finding from a source that ANSWERED. An absent
    or dark source passes through (the field stays None) and the source is named in
    sources_dark, so the alert can print a DEGRADED line and the A-tier band (hc_checks)
    can refuse to be A on unknown data.

Why the two passes: pass 1 is FREE and batched (one Multicall3 round trip covers owner /
LP burn / honeypot round trip for 50 tokens; ScanHood is 3 Hz; the legacy index is a local
file) and runs on every discovered token. Pass 2 is the budgeted, per-token, threaded pass
(GeckoTerminal is a hard 30/min per IP shared with three other jobs on the Mac; Blockscout
sits behind a Cloudflare challenge that darkened this project for 20 days, 2026-08-23 →
09-12) and runs only on tokens that survived the pass-1 market + chain gates.

Fallback ladders (first answer wins, a later source only fills a None unless stated):
  honeypot    rpc round trip  →  ScanHood sellable (False ⇒ True, True ⇒ False)  →  GT is_honeypot
  deployer    Blockscout creation-tx SENDER (overrides)  ←  discovery log creator  ←  ScanHood
              deployer  ←  GT developer_address
  prior       RobinX deployer.launched (overrides)  ←  legacy index count for that deployer
  template    Blockscout impl_name / is_verified (overrides)  ←  ScanHood contractTemplate/verified
  holders     Blockscout counters ('blockscout', exact)  ←  GT holders.count ('gt', 8 min..23 h
              stale — hc_checks treats non-blockscout holders as NA on purpose)

Verified facts this module leans on (2026-09-12): ScanHood's sell sim returns sellable
null on tokens it cannot simulate (MIZUKARA today) — null is UNKNOWN, never a honeypot;
RobinX does not attribute Flap launches (deployer null on MIZUKARA's dev) — a null RobinX
deployer never overrides our own attribution; MIZUKARA's creation tx carries TokenBought
next to TokenCreated (the dev-snipe signal) — dev_sniped is read from the creation-tx logs.

Never raises: one bad token never kills a run. No wall-clock: now_s comes from the caller
and is stamped as safety_ts (the watchlist refresh cadence reads it; no gate does).
"""
from __future__ import annotations

import json
import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config                                                    # noqa: E402
import http_client                                               # noqa: E402
from http_client import is_absent, is_deferred                   # noqa: E402
from concurrent.futures import ThreadPoolExecutor
from sources import blockscout, dexscreener, geckoterminal, kyber, robinx, rpc, scanhood   # noqa: E402

BLOCKSCOUT_HOST = "robinhoodchain.blockscout.com"
LEGACY_INVERSE_CACHE = os.path.join(config.CACHE_DIR, "legacy_creators_inverse.json")

# ── the contract ──────────────────────────────────────────────────────────────────
# Exactly the safety subset of config.FEATURE_FIELDS (asserted in __main__) …
SAFETY_FEATURE_KEYS = (
    "owner_state", "owner_renounced", "lp_locked_pct", "lp_check_source", "honeypot",
    "roundtrip_loss_pct", "is_scam", "template_name", "is_proxy", "verified_source",
    "total_holders", "holders_source", "top10_pct", "top10_pct_gt", "lp_share_pct", "dev_pct",
    "deployer", "creator_prior_tokens", "creator_dead_frac", "creator_score", "dev_sniped",
    "sniper_swaps_first_blocks", "buys_per_buyer_m5", "tx_per_holder_total", "gt_score",
    "gt_verified", "launchpad_graduation_pct", "launchpad_completed",
    "launchpad_completed_age_s", "holders_updated_age_s", "scanhood_verdict",
    "scanhood_sellable", "sources_dark",
)
# … plus the solana-only keys carried as None and never gated (persisted dicts stay
# comparable across the two screeners) …
SOLANA_ONLY_KEYS = ("mint_authority_active", "freeze_authority_active", "insider_pct",
                    "insider_networks_pct", "graph_insiders", "risk_score", "danger_risks",
                    "rugged")
# … plus bookkeeping.
BOOKKEEPING_KEYS = ("sources_used", "pass", "safety_ts")
SAFETY_KEYS = SAFETY_FEATURE_KEYS + SOLANA_ONLY_KEYS + BOOKKEEPING_KEYS
_LIST_KEYS = ("sources_dark", "sources_used", "danger_risks")
# private bookkeeping carried between the passes; build_feat reads FEATURE_FIELDS only
_PAIR_BLOCK_KEY = "_pair_block"


def empty_safety() -> dict:
    """Every key present and None (lists empty). The shape screen.hard_gates passes
    through on entirely — has_safety is True on any dict, and every gate is None-safe."""
    s = {k: None for k in SAFETY_KEYS}
    for k in _LIST_KEYS:
        s[k] = []
    return s


# ── small helpers ─────────────────────────────────────────────────────────────────
def _lower(a) -> str | None:
    if not isinstance(a, str) or not a:
        return None
    return a.lower()


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


def _mark(s: dict, source: str, dark: bool) -> None:
    """Record whether a source answered (sources_used) or went dark (sources_dark)."""
    key = "sources_dark" if dark else "sources_used"
    lst = s.get(key)
    if not isinstance(lst, list):
        lst = []
        s[key] = lst
    if source not in lst:
        lst.append(source)


def _copy(s: dict) -> dict:
    """A fresh dict with fresh lists — pass2 must never mutate the caller's s1."""
    out = dict(s)
    for k in _LIST_KEYS:
        out[k] = list(s.get(k) or [])
    return out


# ── the legacy creator index (archived token_creators.json) ─────────────────────
# {token_lower: {deployer, creation_tx, via, as_of}} — 21k entries / 5 MB, frozen 2026-07.
# Loaded ONCE per process into an inverse map {deployer_lower: n_tokens} and cached on disk
# so a cloud run pays ~50 ms, not a 5 MB parse per token. The count is the number of tokens
# that deployer has IN THE INDEX: for any token launched after the index froze (every token
# the live screener sees) that is exactly its prior-launch count; a token that is itself in
# the index (MIZUKARA) counts itself once. It is a FLOOR — RobinX overrides when it answers.
_legacy_lock = threading.Lock()
_legacy_inverse: dict | None = None


def _atomic_json(path: str, obj) -> None:
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f)
    os.replace(tmp, path)


def _build_legacy_inverse(src: str) -> dict:
    inv: dict = {}
    try:
        with open(src) as f:
            raw = json.load(f)
    except Exception as exc:
        print(f"  [safety] legacy creators index unreadable ({exc}); prior-token fallback off")
        return inv
    if not isinstance(raw, dict):
        return inv
    for _tok, rec in raw.items():
        dep = _lower(rec.get("deployer")) if isinstance(rec, dict) else None
        if dep:
            inv[dep] = inv.get(dep, 0) + 1
    return inv


def legacy_creator_counts() -> dict:
    """The inverse map, built lazily once and cached in memory + on disk. Thread-safe.
    Missing index (the runner without the archive) → {} and the fallback is simply off."""
    global _legacy_inverse
    if _legacy_inverse is not None:
        return _legacy_inverse
    with _legacy_lock:
        if _legacy_inverse is not None:
            return _legacy_inverse
        src = config.LEGACY_TOKEN_CREATORS_PATH
        inv: dict | None = None
        try:
            if (os.path.exists(LEGACY_INVERSE_CACHE) and os.path.exists(src)
                    and os.path.getmtime(LEGACY_INVERSE_CACHE) >= os.path.getmtime(src)):
                with open(LEGACY_INVERSE_CACHE) as f:
                    obj = json.load(f)
                if isinstance(obj, dict) and isinstance(obj.get("inverse"), dict):
                    inv = obj["inverse"]
        except Exception:
            inv = None
        if inv is None:
            inv = _build_legacy_inverse(src) if os.path.exists(src) else {}
            if inv:
                try:
                    _atomic_json(LEGACY_INVERSE_CACHE,
                                 {"inverse": inv, "source": os.path.basename(src)})
                except Exception:
                    pass
        _legacy_inverse = inv
        return inv


def _legacy_prior(deployer: str | None) -> int | None:
    if not deployer:
        return None
    n = legacy_creator_counts().get(deployer.lower())
    return int(n) if n else None


# ── pass 1: free / batched ────────────────────────────────────────────────────────
def _apply_chain_facts(s: dict, facts: dict | None) -> None:
    """rpc.chain_facts_many record → owner / LP / honeypot fields. A status of 'deferred'
    (nothing answered) or 'partial' (some Multicall legs deferred) names rpc dark: whatever
    stayed None did so because the node did not answer, not because it is unknowable."""
    if not isinstance(facts, dict):
        _mark(s, "rpc", dark=True)
        return
    st = facts.get("status")
    owner = facts.get("owner_state")
    s["owner_state"] = owner if owner in ("renounced", "owned", "no_owner_fn", "protocol") else None
    s["owner_renounced"] = (True if owner == "renounced" else
                            False if owner in ("owned", "protocol") else None)
    lp = _f(facts.get("lp_locked_pct"))
    s["lp_locked_pct"] = lp
    src = facts.get("lp_check_source")
    s["lp_check_source"] = src if (lp is not None and src and src != "unknown") else None
    hp = facts.get("honeypot")
    s["honeypot"] = hp if isinstance(hp, bool) else None
    s["roundtrip_loss_pct"] = _f(facts.get("roundtrip_loss_pct"))
    if st == "deferred":
        _mark(s, "rpc", dark=True)
    else:
        _mark(s, "rpc", dark=False)
        if st == "partial":
            _mark(s, "rpc", dark=True)


def _apply_scan(s: dict, scan: dict | None) -> None:
    """ScanHood verdict + sell-sim → scanhood_* and the honeypot fallback. `sellable` None
    means the sim did not run (measured on MIZUKARA today) — unknown, NEVER False."""
    if not isinstance(scan, dict):
        _mark(s, "scanhood", dark=True)
        return
    _mark(s, "scanhood", dark=False)
    s["scanhood_verdict"] = scan.get("verdict") or None
    sell = scan.get("sellable")
    s["scanhood_sellable"] = sell if isinstance(sell, bool) else None
    if s.get("honeypot") is None and isinstance(sell, bool):
        s["honeypot"] = (not sell)           # sellable False = a positive finding
    # weak template/verification fallback — Blockscout overrides in pass 2 when it answers
    if s.get("template_name") is None and scan.get("contract_template"):
        s["template_name"] = str(scan["contract_template"])
    if s.get("verified_source") is None and isinstance(scan.get("verified"), bool):
        s["verified_source"] = scan["verified"]
    dep = scan.get("deployer")
    if s.get("deployer") is None and isinstance(dep, dict) and _lower(dep.get("address")):
        s["deployer"] = _lower(dep["address"])


def _apply_disc(s: dict, d: dict | None) -> None:
    """The discovery record (rpc.discover_logs row, recheck carry-over, or a GT new_pools
    row): creator → deployer; a persisted dev_sniped verdict; GT m5 buys/buyers → the
    buys-per-buyer wash ratio; the V2 pair's creation block for the sniper count in pass 2."""
    if not isinstance(d, dict) or not d:
        return
    _mark(s, "disc", dark=False)
    creator = _lower(d.get("creator") or d.get("deployer"))
    if creator:
        s["deployer"] = creator                # overrides ScanHood's weak value
    # a launchpad pool under a trusted V4 hook: liquidity sits in the hook's custody by
    # construction (no LP token a dev could pull), so the LP question is answered by the
    # launchpad, not by a burn percentage. lp_locked_pct stays None (no % exists); the
    # source names the launchpad and hc_checks reads it as "LP known".
    hook = _lower(d.get("hook"))
    if hook and hook in config.TRUSTED_V4_HOOKS and s.get("lp_locked_pct") is None:
        s["lp_check_source"] = "v4_launchpad:" + config.TRUSTED_V4_HOOKS[hook]
    for k in ("dev_sniped", "sniped"):
        if isinstance(d.get(k), bool):
            s["dev_sniped"] = d[k]
            break
    buys, buyers = _i(d.get("buys_m5")), _i(d.get("buyers_m5"))
    if buys is not None and buyers:
        s["buys_per_buyer_m5"] = round(buys / buyers, 4)
    if d.get("kind") == "pair_v2" and _i(d.get("block")) and _lower(d.get("pair")):
        s[_PAIR_BLOCK_KEY] = {"pair": _lower(d["pair"]), "block": _i(d["block"])}


def _kyber_roundtrips(out: dict, facts: dict, markets: dict, budget: int | None) -> None:
    """The router legs are blind on tokens with no V2 pair (every V4 / launchpad token).
    Probe those through KyberSwap instead — buy KYBER_PROBE_WETH_WEI, quote selling it all
    back — for at most `budget` tokens per run, deepest liquidity first. Both legs routed ⇒
    honeypot False + the round-trip loss; an explicit no-route on the SELL leg alone is not
    proof of a honeypot on a minutes-old pool (unknown, named); a deferred leg names kyber dark."""
    budget = config.KYBER_ROUNDTRIP_BUDGET_PER_RUN if budget is None else budget
    cands = [t for t, s in out.items()
             if s.get("honeypot") is None and s.get("roundtrip_loss_pct") is None
             and isinstance(facts.get(t), dict) and facts[t].get("pair") is None
             and facts[t].get("status") in ("ok", "partial")]
    cands.sort(key=lambda t: -float((markets.get(t) or {}).get("liq_usd") or 0))
    for t in cands[:max(0, int(budget))]:
        s = out[t]
        try:
            buy = kyber.route(config.WETH, t, config.KYBER_PROBE_WETH_WEI)
            if is_deferred(buy):
                _mark(s, "kyber", dark=True)
                continue
            if is_absent(buy) or not isinstance(buy, dict) or not buy.get("amount_out"):
                _mark(s, "kyber", dark=False)          # answered: nothing to route (yet)
                continue
            sell = kyber.route(t, config.WETH, int(buy["amount_out"]))
            if is_deferred(sell):
                _mark(s, "kyber", dark=True)
                continue
            _mark(s, "kyber", dark=False)
            if is_absent(sell) or not isinstance(sell, dict) or not sell.get("amount_out"):
                continue                               # buyable, not (yet) sellable: unknown, never a verdict
            back = int(sell["amount_out"])
            s["roundtrip_loss_pct"] = round(100.0 * (1.0 - back / float(config.KYBER_PROBE_WETH_WEI)), 4)
            s["honeypot"] = False
        except Exception as exc:
            print(f"  [safety] {t[:10]}… kyber round trip failed: {exc}")
            _mark(s, "kyber", dark=True)


_SCAN_ERROR = object()


def _scanhood_many(toks: list, markets: dict, budget: int | None) -> dict:
    """ScanHood for at most `budget` tokens (deepest liquidity first), fetched concurrently: the
    per-host throttle still paces the STARTS at SCANHOOD_RATE_HZ, but a slow answer (up to 7 s
    measured) no longer blocks the next call. {token: scan | _SCAN_ERROR}; tokens beyond the
    budget are absent from the dict (not consulted, never marked dark)."""
    budget = config.SCANHOOD_BUDGET_PER_RUN if budget is None else budget
    order = sorted(toks, key=lambda t: -float((markets.get(t) or {}).get("liq_usd") or 0))[: max(0, int(budget))]
    if not order:
        return {}

    def one(t):
        try:
            return t, scanhood.scan(t)
        except Exception as exc:
            print(f"  [safety] {t[:10]}… scanhood failed: {exc}")
            return t, _SCAN_ERROR
    with ThreadPoolExecutor(max_workers=max(1, int(config.SCANHOOD_WORKERS))) as ex:
        return dict(ex.map(one, order))


def pass1_many(tokens: list, markets: dict, disc: dict, now_s: float,
               chain_cache: dict | None = None, kyber_budget: int | None = None,
               scanhood_budget: int | None = None) -> dict:
    """The FREE / batched pass for many tokens → {token_lower: safety dict, pass=1}.
    markets: {token_lower: dexscreener market} (a known pair skips the getPair leg);
    disc: {token_lower: discovery record} (may be absent for feed/watchlist tokens);
    chain_cache: optional {token_lower: rpc facts} — entries that answered are reused
    instead of re-probed (the watchlist's SAFETY_REFRESH_S cadence), fresh facts are
    written back into it. now_s is stamped as safety_ts, never used in a gate."""
    toks = list(dict.fromkeys(_lower(t) for t in (tokens or []) if _lower(t)))
    markets = markets or {}
    disc = disc or {}
    out = {t: empty_safety() for t in toks}
    if not toks:
        return out

    facts: dict = {}
    todo = []
    for t in toks:
        cached = (chain_cache or {}).get(t) if chain_cache else None
        if isinstance(cached, dict) and cached.get("status") in ("ok", "partial"):
            facts[t] = cached
        else:
            todo.append(t)
    if todo:
        pairs = {}
        for t in todo:
            m = markets.get(t) or {}
            pairs[t] = _lower(m.get("pair")) if isinstance(m, dict) else None
        try:
            fresh = rpc.chain_facts_many(todo, pairs=pairs) or {}
        except Exception as exc:                       # never let one batch kill the run
            print(f"  [safety] chain_facts_many failed: {exc}")
            fresh = {}
        for t in todo:
            f = fresh.get(t)
            if f is None:                              # tolerate a mixed-case key
                f = next((v for k, v in fresh.items() if _lower(k) == t), None)
            facts[t] = f
            if chain_cache is not None and isinstance(f, dict) and f.get("status") != "deferred":
                chain_cache[t] = f

    for t in toks:
        s = out[t]
        try:
            _apply_chain_facts(s, facts.get(t))
        except Exception as exc:
            print(f"  [safety] {t[:10]}… chain facts unreadable: {exc}")
            _mark(s, "rpc", dark=True)
    # the two slow pass-1 legs run side by side (each is throttled per host; neither waits for the other)
    with ThreadPoolExecutor(max_workers=1) as _bg:
        _scan_future = _bg.submit(_scanhood_many, toks, markets, scanhood_budget)
        _kyber_roundtrips(out, facts, markets, kyber_budget)
        scans = _scan_future.result()
    for t in toks:
        s = out[t]
        if t not in scans:
            continue                                   # beyond the budget: not consulted, not dark
        if scans[t] is _SCAN_ERROR:
            _mark(s, "scanhood", dark=True)
        else:
            _apply_scan(s, scans[t])
        try:
            _apply_disc(s, disc.get(t))
        except Exception as exc:
            print(f"  [safety] {t[:10]}… discovery record unreadable: {exc}")
        # legacy index: a floor on prior launches; RobinX overrides in pass 2
        prior = _legacy_prior(s.get("deployer"))
        if prior is not None:
            s["creator_prior_tokens"] = prior
            _mark(s, "legacy", dark=False)
        s["pass"] = 1
        s["safety_ts"] = now_s
    return out


# ── pass 2: budgeted, per token, thread-safe ──────────────────────────────────────
def _apply_gt(s: dict, now_s: float, token: str) -> None:
    info = geckoterminal.token_info(token)
    if is_deferred(info):
        _mark(s, "geckoterminal", dark=True)
        return
    _mark(s, "geckoterminal", dark=False)
    if is_absent(info) or not isinstance(info, dict):
        return                                   # GT answered: it does not index this token
    s["gt_score"] = _f(info.get("gt_score"))
    gv = info.get("gt_verified")
    s["gt_verified"] = gv if isinstance(gv, bool) else None
    s["top10_pct_gt"] = _f(info.get("top10_pct_gt"))
    ts = _f(info.get("holders_updated_ts"))
    s["holders_updated_age_s"] = max(0.0, now_s - ts) if ts is not None else None
    s["_gt_holders"] = _i(info.get("holders_count"))
    hp = info.get("is_honeypot_gt")
    if s.get("honeypot") is None and hp is True:
        s["honeypot"] = True                     # a positive finding; False alone is not proof
    if s.get("deployer") is None and _lower(info.get("developer_address")):
        s["deployer"] = _lower(info["developer_address"])
    if s.get("dev_pct") is None:
        s["dev_pct"] = _f(info.get("dev_holding_pct_gt"))
    lp = info.get("launchpad")
    if isinstance(lp, dict):
        s["launchpad_graduation_pct"] = _f(lp.get("graduation_pct"))
        comp = lp.get("completed")
        s["launchpad_completed"] = comp if isinstance(comp, bool) else None
        cts = _f(lp.get("completed_ts"))
        if comp is True and cts is not None:
            s["launchpad_completed_age_s"] = max(0.0, now_s - cts)


def _sniped_from_logs(logs: list) -> bool:
    want = config.TOPIC_FLAP_TOKEN_BOUGHT.lower()
    for lg in logs:
        if not isinstance(lg, dict):
            continue
        topics = lg.get("topics") or []
        if topics and str(topics[0]).lower() == want:
            return True
        if lg.get("event") == "TokenBought":
            return True
    return False


def _apply_blockscout(s: dict, token: str) -> None:
    """Holders / counters / top-10 / address flags / creation-tx sender / creation-tx logs.
    Any None (deferred) names blockscout dark; NOT_FOUND is an answer and does not."""
    answered = dark = False

    def note(x) -> bool:
        """Track the status; return True when x is a usable object."""
        nonlocal answered, dark
        if is_deferred(x):
            dark = True
            return False
        answered = True
        return not is_absent(x)

    ti = blockscout.token_info(token)
    supply_raw = None
    if note(ti) and isinstance(ti, dict):
        supply_raw = _i(ti.get("supply_raw"))
        h = _i(ti.get("holders"))
        if h is not None:
            s["total_holders"], s["holders_source"] = h, "blockscout"

    cnt = blockscout.token_counters(token)
    if note(cnt) and isinstance(cnt, dict):
        h, tx = _i(cnt.get("holders_count")), _i(cnt.get("transfers_count"))
        if h is not None:
            s["total_holders"], s["holders_source"] = h, "blockscout"
            if tx is not None and h > 0:
                s["tx_per_holder_total"] = round(tx / h, 4)

    if supply_raw and supply_raw > 0:
        th = blockscout.top_holders(token, supply_raw)
        if note(th) and isinstance(th, dict):
            s["top10_pct"] = _f(th.get("top10_pct"))
            s["lp_share_pct"] = _f(th.get("lp_pct"))

    creation_tx = None
    ai = blockscout.address_info(token)
    if note(ai) and isinstance(ai, dict):
        sc = ai.get("is_scam")
        s["is_scam"] = sc if isinstance(sc, bool) else None
        if ai.get("impl_name"):
            s["template_name"] = str(ai["impl_name"])
        s["is_proxy"] = bool(ai.get("proxy_type"))
        ver = ai.get("is_verified")
        if isinstance(ver, bool):
            s["verified_source"] = ver
        creation_tx = ai.get("creation_tx") or None

    co = blockscout.creator_of(token)
    if note(co) and isinstance(co, dict):
        if _lower(co.get("deployer")):
            s["deployer"] = _lower(co["deployer"])       # the creation-tx SENDER wins
        creation_tx = creation_tx or co.get("creation_tx")

    if creation_tx:
        logs = blockscout.tx_logs(creation_tx)
        if note(logs) and isinstance(logs, list):
            s["dev_sniped"] = _sniped_from_logs(logs)

    if answered:
        _mark(s, "blockscout", dark=False)
    if dark:
        _mark(s, "blockscout", dark=True)


def _apply_blockscout_fast(s: dict, token: str) -> None:
    """The two Blockscout calls that carry POSITIVE findings for the alert decision — address
    flags (is_scam, template, verified) and the holder/transfer counters — without the paged
    top-holders walk, the creation-tx sender or its logs (each a further 1–4 calls at 2 Hz)."""
    answered = dark = False

    def note(x) -> bool:
        nonlocal answered, dark
        if is_deferred(x):
            dark = True
            return False
        answered = True
        return not is_absent(x)

    cnt = blockscout.token_counters(token)
    if note(cnt) and isinstance(cnt, dict):
        h, tx = _i(cnt.get("holders_count")), _i(cnt.get("transfers_count"))
        if h is not None:
            s["total_holders"], s["holders_source"] = h, "blockscout"
            if tx is not None and h > 0:
                s["tx_per_holder_total"] = round(tx / h, 4)
    ai = blockscout.address_info(token)
    if note(ai) and isinstance(ai, dict):
        sc = ai.get("is_scam")
        s["is_scam"] = sc if isinstance(sc, bool) else None
        if ai.get("impl_name"):
            s["template_name"] = str(ai["impl_name"])
        s["is_proxy"] = bool(ai.get("proxy_type"))
        ver = ai.get("is_verified")
        if isinstance(ver, bool):
            s["verified_source"] = ver
    if answered:
        _mark(s, "blockscout", dark=False)
    if dark:
        _mark(s, "blockscout", dark=True)


def _apply_robinx(s: dict) -> None:
    dep = s.get("deployer")
    if not dep:
        return
    w = robinx.wallet(dep)
    if is_deferred(w):
        _mark(s, "robinx", dark=True)
        return
    _mark(s, "robinx", dark=False)
    if is_absent(w) or not isinstance(w, dict):
        return                                   # never seen this wallet: an answer
    rec = w.get("deployer")
    if not isinstance(rec, dict):
        return                                   # no record (RobinX skips Flap launches)
    launched = _i(rec.get("launched"))
    dead = _i(rec.get("dead"))
    s["creator_score"] = _f(rec.get("score"))
    if launched is not None:
        s["creator_prior_tokens"] = launched     # overrides the legacy floor
        if launched > 0 and dead is not None:
            s["creator_dead_frac"] = round(min(dead, launched) / launched, 4)


def pass2(token: str, market: dict, s1: dict, now_s: float, disc: dict | None = None,
          fast: bool = False) -> dict:
    """The budgeted pass for ONE token, building on its pass-1 dict (never mutated).
    GeckoTerminal /info → Blockscout (skipped entirely while the host is marked blocked)
    → RobinX wallet of the attributed deployer → the V2 sniper count when the pair's
    creation block is known (from `disc`, or carried over from pass 1). Safe to call
    concurrently for different tokens: no shared mutable state beyond http_client's
    throttle and the lazily-built legacy map (lock-guarded)."""
    t = _lower(token) or ""
    s = _copy(s1 if isinstance(s1, dict) else empty_safety())
    for k in SAFETY_KEYS:
        s.setdefault(k, [] if k in _LIST_KEYS else None)
    if disc:
        try:
            _apply_disc(s, disc)
        except Exception:
            pass

    try:
        _apply_gt(s, now_s, t)
    except Exception as exc:
        print(f"  [safety] {t[:10]}… geckoterminal failed: {exc}")
        _mark(s, "geckoterminal", dark=True)

    if http_client.is_blocked(BLOCKSCOUT_HOST):
        _mark(s, "blockscout", dark=True)
    else:
        try:
            if fast:
                _apply_blockscout_fast(s, t)
            else:
                _apply_blockscout(s, t)
        except Exception as exc:
            print(f"  [safety] {t[:10]}… blockscout failed: {exc}")
            _mark(s, "blockscout", dark=True)

    if fast:
        # the alert-path variant: GT + the two Blockscout flag calls only (~6–8 s instead of ~50 s);
        # RobinX, the launch history, the launcher balance and the sniper count are refined by the
        # watchlist refresh — an alert is decided on positive findings, and those are all here
        gt_holders = s.pop("_gt_holders", None)
        if s.get("total_holders") is None and gt_holders is not None:
            s["total_holders"], s["holders_source"] = gt_holders, "gt"
        s.pop(_PAIR_BLOCK_KEY, None)
        _mark(s, "fast_pass2", dark=False)
        s["pass"] = 2
        s["safety_ts"] = now_s
        return s

    gt_holders = s.pop("_gt_holders", None)
    if s.get("total_holders") is None and gt_holders is not None:
        s["total_holders"], s["holders_source"] = gt_holders, "gt"

    if s.get("deployer") and s.get("creator_prior_tokens") is None:
        prior = _legacy_prior(s["deployer"])     # a deployer first learned in pass 2
        if prior is not None:
            s["creator_prior_tokens"] = prior
            _mark(s, "legacy", dark=False)
    try:
        _apply_robinx(s)
    except Exception as exc:
        print(f"  [safety] {t[:10]}… robinx failed: {exc}")
        _mark(s, "robinx", dark=True)

    # Launchpad tokens: the launcher's whole-life LongLaunch history in ONE indexed log query
    # (exact), judged by the dead fraction of its PRIOR launches (Dexscreener, one batched call).
    # An agent/service wallet (>= LAUNCH_SERVICE_MIN_CREATES launches) launches for many users:
    # its history is not this dev's, so both fields stay unknown and the gate passes through.
    launch_service = False
    if (str(s.get("lp_check_source") or "").startswith("v4_launchpad") and s.get("deployer")
            and s.get("creator_prior_tokens") is None):
        try:
            launches = rpc.creator_launches(s["deployer"])
        except Exception as exc:
            print(f"  [safety] {t[:10]}… launch history failed: {exc}")
            launches = None
        if launches is None:
            _mark(s, "rpc", dark=True)
        else:
            prior = [x for x in launches if x != t]
            if len(prior) >= config.LAUNCH_SERVICE_MIN_CREATES:
                launch_service = True
                _mark(s, "launch_service", dark=False)
            else:
                s["creator_prior_tokens"] = len(prior)
                _mark(s, "launchpad", dark=False)
                if prior:
                    try:
                        r = dexscreener.enrich_many(prior[-30:], now_s)
                        ok = {str(k).lower(): v for k, v in (r.get("ok") or {}).items()}
                        absent = {str(x).lower() for x in (r.get("absent") or set())}
                        judged = [x for x in prior[-30:] if x in ok or x in absent]
                        dead = sum(1 for x in judged if x in absent
                                   or float((ok.get(x) or {}).get("mcap") or 0) < config.CREATOR_DEAD_MCAP_USD)
                        if judged:
                            s["creator_dead_frac"] = round(dead / len(judged), 4)
                    except Exception as exc:
                        print(f"  [safety] {t[:10]}… prior-launch outcomes failed: {exc}")

    # dev holding for a launchpad token: the launcher's own balance (exact, two eth_calls) —
    # meaningful only when the launcher is a person, not an app/agent wallet launching for many
    if (str(s.get("lp_check_source") or "").startswith("v4_launchpad") and s.get("deployer")
            and s.get("dev_pct") is None and not launch_service
            and (s.get("creator_prior_tokens") is None
                 or s["creator_prior_tokens"] < config.LAUNCH_SERVICE_MIN_CREATES)):
        try:
            bal, sup = rpc.balance_of(t, s["deployer"]), rpc.total_supply(t)
            if bal is not None and sup:
                s["dev_pct"] = round(100.0 * bal / sup, 4)
        except Exception as exc:
            print(f"  [safety] {t[:10]}… launcher balance failed: {exc}")

    # Prior-launch count from the explorer when neither RobinX nor the legacy index knows the
    # wallet (RobinX does not attribute Flap launches; the index froze in July). Two tx pages
    # (100 txs) is exact for a fresh deployer; a heavier wallet stays UNKNOWN unless the walk
    # already found more launches than the hard gate allows.
    if s.get("deployer") and s.get("creator_prior_tokens") is None and not launch_service \
            and not http_client.is_blocked(BLOCKSCOUT_HOST):
        try:
            created = blockscout.wallet_created_tokens(s["deployer"], max_pages=2)
            prior = sum(1 for c in created if str(c.get("token", "")).lower() != t.lower())
            n_tx = blockscout.address_tx_count(s["deployer"])
            if prior > config.CREATOR_MAX_PRIOR_TOKENS or (n_tx is not None and n_tx <= 100):
                s["creator_prior_tokens"] = prior
                _mark(s, "blockscout", dark=False)
        except Exception as exc:
            print(f"  [safety] {t[:10]}… creator history failed: {exc}")

    pb = s.pop(_PAIR_BLOCK_KEY, None)
    if isinstance(pb, dict) and pb.get("pair") and pb.get("block"):
        try:
            s["sniper_swaps_first_blocks"] = rpc.swaps_in_first_blocks(
                pb["pair"], int(pb["block"]), config.SNIPER_FIRST_BLOCKS)
        except Exception:
            s["sniper_swaps_first_blocks"] = None
        if s["sniper_swaps_first_blocks"] is None:
            _mark(s, "rpc", dark=True)

    s["pass"] = 2
    s["safety_ts"] = now_s
    return s


# ── the DEGRADED line ─────────────────────────────────────────────────────────────
# (source, field, label): a gate is listed only when its source was dark AND the field is
# still unknown — a value filled by another source is not degraded.
_DEGRADED_MAP = (
    ("rpc", "owner_state", "owner"),
    ("rpc", "lp_locked_pct", "LP burn"),
    ("rpc", "honeypot", "honeypot"),
    ("rpc", "roundtrip_loss_pct", "sell round-trip"),
    ("rpc", "sniper_swaps_first_blocks", "sniper swaps"),
    ("blockscout", "total_holders", "holders"),
    ("blockscout", "top10_pct", "top10"),
    ("blockscout", "template_name", "template"),
    ("blockscout", "is_scam", "is_scam"),
    ("blockscout", "deployer", "deployer"),
    ("blockscout", "dev_sniped", "sniped-at-create"),
    ("geckoterminal", "gt_score", "GT score"),
    ("geckoterminal", "top10_pct_gt", "GT top10"),
    ("geckoterminal", "dev_pct", "dev holding"),
    ("scanhood", "scanhood_verdict", "ScanHood verdict"),
    ("robinx", "creator_score", "creator score"),
    ("robinx", "creator_dead_frac", "creator dead launches"),
)


def degraded_fields(s: dict) -> list:
    """Human-readable gates that passed through because their source was dark, for the
    alert's DEGRADED line. Holders and template carry their own rule: holders are 'exact'
    only from Blockscout (a GT count is stale, never A), template needs either an impl name
    or a verification flag."""
    s = s or {}
    dark = set(s.get("sources_dark") or [])
    out = []
    for src, field, label in _DEGRADED_MAP:
        if src not in dark:
            continue
        if field == "total_holders":
            unknown = s.get("holders_source") != "blockscout"
        elif field == "template_name":
            unknown = s.get("template_name") is None and s.get("verified_source") is None
        else:
            unknown = s.get(field) is None
        if unknown:
            out.append(f"{label} ({src} dark)")
    return out


# ── smoke test ────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import time
    import screen
    from sources import dexscreener

    print("robinhood_screener sources/safety")
    # 0. the contract: exactly the safety subset of FEATURE_FIELDS
    runtime_keys = {"token", "score", "first_sighting", "sighting_age_s"}
    expect = set(config.FEATURE_FIELDS) - set(dexscreener.MARKET_KEYS) - runtime_keys
    assert set(SAFETY_FEATURE_KEYS) == expect, (set(SAFETY_FEATURE_KEYS) ^ expect)
    e = empty_safety()
    assert set(e) == set(SAFETY_KEYS) and e["sources_dark"] == [] and e["honeypot"] is None
    print(f"  SAFETY_KEYS: {len(SAFETY_KEYS)} ({len(SAFETY_FEATURE_KEYS)} feature + "
          f"{len(SOLANA_ONLY_KEYS)} solana-only + {len(BOOKKEEPING_KEYS)} bookkeeping)  ok")
    ok_gates, _ = screen.hard_gates({"price_usd": 1.0, "liq_usd": 1e5, "vol_h24": 1e5}, e)
    assert ok_gates, "an all-None safety dict must pass through every gate"
    print(f"  legacy inverse map: {len(legacy_creator_counts()):,} deployers")

    now_s = time.time()
    miz = config.MIZUKARA.lower()
    market = dexscreener.enrich_many([config.MIZUKARA], now_s)["ok"][miz]

    # 1. pass 1
    s1 = pass1_many([config.MIZUKARA], {miz: market}, {}, now_s)[miz]
    known = {k: v for k, v in s1.items() if v not in (None, []) and not k.startswith("_")}
    print("  pass-1 known:", known)
    assert s1["pass"] == 1 and s1["safety_ts"] == now_s
    assert s1["owner_state"] == "renounced" and s1["owner_renounced"] is True, s1["owner_state"]
    assert s1["lp_locked_pct"] == 100.0 and s1["lp_check_source"] == "rpc_v2"
    assert s1["honeypot"] is False, s1["honeypot"]
    assert s1["scanhood_sellable"] is not False or s1["honeypot"] is True

    # 2. pass 2
    s2 = pass2(miz, market, s1, now_s)
    print("  pass-2 known:", {k: v for k, v in s2.items() if v not in (None, [])})
    assert set(s2) >= set(SAFETY_KEYS) and s2["pass"] == 2
    assert s1["pass"] == 1, "pass2 mutated s1"
    assert s2["holders_source"] == "blockscout" and s2["total_holders"] > 1000, \
        (s2["holders_source"], s2["total_holders"])
    assert s2["template_name"] == "FlapTaxTokenV3", s2["template_name"]
    assert s2["is_scam"] is False and s2["is_proxy"] is True
    assert s2["deployer"] == config.MIZUKARA_DEV.lower(), s2["deployer"]
    assert s2["dev_sniped"] is True, s2["dev_sniped"]
    assert isinstance(s2["creator_prior_tokens"], int) and s2["creator_prior_tokens"] >= 1
    assert s2["top10_pct"] is not None and s2["top10_pct"] < config.TOP10_MAX_PCT
    assert s2["tx_per_holder_total"] is not None and s2["tx_per_holder_total"] > 1
    passed, gates = screen.hard_gates(market, s2)
    print(f"  hard_gates(MIZUKARA): passed={passed}  failing={[k for k, v in gates.items() if v is False]}"
          f"  (liq ${market['liq_usd']:,.0f} vol24 ${market['vol_h24']:,.0f} → vol_ok False expected)")
    print("  degraded:", degraded_fields(s2))

    # 3. a dark Blockscout: hard gates pass through, holders come from GT, A is unknowable
    http_client.mark_blocked(BLOCKSCOUT_HOST, "test")
    s3 = pass2(miz, market, _copy(s1), now_s)
    health = http_client.health_report()
    http_client.reset_health()                   # never leave a test block behind
    deg = degraded_fields(s3)
    print(f"  dark blockscout: holders_source={s3['holders_source']} total_holders={s3['total_holders']}"
          f" template={s3['template_name']} dark={s3['sources_dark']}")
    print("  degraded:", deg)
    assert s3["holders_source"] != "blockscout" and "blockscout" in s3["sources_dark"]
    assert s3["template_name"] is None or s3["template_name"] == s1.get("template_name")
    for name in ("holders", "top10", "template"):
        assert any(d.startswith(name) for d in deg), (name, deg)
    passed3, gates3 = screen.hard_gates(market, s3)
    assert gates3["blockscout_available"] is False and gates3["not_scam"] and gates3["top10_ok"]
    feat = dict(market); feat.update(s3); feat["score"] = 50.0
    hc = screen.hc_checks(feat)
    assert hc["holders"] is None and hc["template"] is None and hc["top10"] is None
    print("  hc_checks under dark blockscout → None:", [k for k, v in hc.items() if v is None])
    print("\n--- source health (captured before the test block was reset) ---")
    print(health)
    print("safety smoke: OK")
