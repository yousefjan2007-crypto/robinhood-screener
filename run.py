"""
One-shot orchestrator — the cloud scan (GitHub Actions; the Mac dispatches it every 5 min).

    python3 run.py             # dry run: print, write nothing, send nothing
    python3 run.py --commit    # write ledger / state / scan, send nothing
    python3 run.py --send      # write AND push alerts (what the workflow runs)

Per run: advance the block cursor over factory/launchpad logs (exact discovery at ANY cadence —
GitHub's cron fired 13.7×/day against a nominal 288 on this account, measured 2026-09-12) ∪ due
rechecks ∪ the 24h watchlist ∪ the GT new-pools hedge → batched Dexscreener enrichment → pass 1
(market gates + ONE Multicall3 batch of chain facts + ScanHood) → pass 2 under a GT budget
(GT /info, Blockscout, RobinX; threaded) → soft score → ONE flat feature dict → every registered
entry band → the CHAMPION band alone sets tier A and alerts → events (first sighting / band fire /
promotion) become ledger rows with a monotonic event_seq; every band's verdict is recorded per
event → batched forward-return update → exit signals → paper fills → state + latest_scan.json
(the point-in-time feature store) → run_log.jsonl.

Reproducibility: time.time() is captured ONCE below and threaded through everything; the only
other clock read is time.monotonic() for the run-time budget and run_seconds.
Honesty: nothing is marked 'seen' until it had a market snapshot; a source that did not answer
is 'deferred' (retried), never 'absent'; the champion band refuses to be A on unknown data.
"""
from __future__ import annotations

import json
import math
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import config
import http_client
import ledger
import screen
from alerts import format_alert, format_degraded_notice, format_exit_alert, send_all
from selfimprove import champion as CHAMP
from selfimprove.entry_lab import bands as BANDS
from selfimprove.entry_lab import runtime as LAB
from selfimprove.entry_lab import store as STORE
from sources import dexscreener as dex
from sources import geckoterminal as gt
from sources import rpc
from sources import safety as SAFE

BLOCKSCOUT_HOST = "robinhoodchain.blockscout.com"


# ── small atomic JSON helpers ─────────────────────────────────────────────────────
def _clean(o):
    """NaN/inf → None recursively so json.dump(allow_nan=False) never trips."""
    if isinstance(o, float):
        return None if (math.isnan(o) or math.isinf(o)) else o
    if isinstance(o, dict):
        return {k: _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple, set)):
        return [_clean(v) for v in o]
    return o


def _atomic_json(path: str, obj, indent=None) -> None:
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w") as f:
        json.dump(_clean(obj), f, indent=indent, allow_nan=False)
    os.replace(tmp, path)


def _load_json(path: str, default):
    if not os.path.exists(path):
        return default
    try:
        return json.load(open(path))
    except Exception:
        return default


def _round(v, sig: int = 6):
    if isinstance(v, float) and v == v and not math.isinf(v) and v != 0.0:
        return float(f"{v:.{sig}g}")
    return v


class _Budget:
    def __init__(self, seconds: float):
        self.t0 = time.monotonic()
        self.limit = seconds
        self.cuts: dict = {}

    def left(self) -> float:
        return self.limit - (time.monotonic() - self.t0)

    def ok(self, stage: str) -> bool:
        if self.left() > 0:
            return True
        self.cuts[stage] = self.cuts.get(stage, 0) + 1
        return False

    def elapsed(self) -> float:
        return time.monotonic() - self.t0


# ── discovery ────────────────────────────────────────────────────────────────────
def discover_from_logs(cursor: dict, head: int | None, budget: _Budget) -> tuple:
    """Exact discovery: (disc dict token→record, new_cursor dict, gap_blocks). The cursor
    advances only to the block of the last log actually processed; log tokens are never
    truncated silently — the per-run cap moves the cursor, not the token."""
    if not head:
        return {}, cursor, 0
    last = int(cursor.get("last_block") or 0)
    if last <= 0:
        start = max(0, head - config.DISCOVERY_BACKFILL_BLOCKS)
        gap = 0
    else:
        start = last + 1
        gap = 0
        floor = head - config.DISCOVERY_MAX_CATCHUP_BLOCKS
        if start < floor:
            gap = floor - start
            start = floor
    if start > head:
        return {}, cursor, 0
    addrs = list(config.DISCOVERY_LOG_SOURCES)
    topics = [[t for _k, t in config.DISCOVERY_LOG_SOURCES.values()]]
    disc: dict = {}
    processed_block = start - 1
    capped = False
    win = config.LOG_WINDOW_BLOCKS
    s = start
    while s <= head and not capped and budget.ok("discovery"):
        w = win
        while True:
            e = min(s + w - 1, head)
            logs, err = rpc.get_logs(addrs, topics, s, e)
            if logs is not None:
                break
            if w > config.MIN_LOG_WINDOW:
                w //= 2
                continue
            print(f"  [discover] window {s}-{e} failed ({err}); cursor stays at {processed_block}")
            new = {"last_block": processed_block, "updated_ts": cursor.get("updated_ts")}
            return disc, new, gap
        for rec in rpc.decode_discovery(logs):
            if len(disc) >= config.DISCOVERY_MAX_LOG_TOKENS_PER_RUN:
                capped = True
                break
            t = rec["token"].lower()
            if t not in disc:
                disc[t] = rec
            processed_block = rec["block"]
        if not capped:
            processed_block = e
        s = e + 1
    if capped:
        processed_block = max(start - 1, processed_block - 1)   # re-scan the cut block next run
    new = {"last_block": processed_block, "updated_ts": cursor.get("updated_ts")}
    return disc, new, gap


def feed_tokens(seen: dict, known: set, budget: _Budget) -> set:
    out: set = set()
    if "gt_new_pools" in config.DISCOVERY_FEEDS:
        for page in range(1, config.GT_NEW_POOLS_PAGES + 1):
            if not budget.ok("feeds"):
                break
            for p in gt.new_pools(page):
                t = (p.get("token") or "").lower()
                if t and t not in seen and t not in known:
                    out.add(t)
    return out


# ── the screen for one token ──────────────────────────────────────────────────────
def _excluded_symbol(symbol) -> bool:
    """Quote assets, tokenized-stock legs and their leveraged variants (OPENAIx1L, NVDAx3L) are
    LongLaunch numeraires that the GT new-pools feed cannot tell from launches."""
    sym = str(symbol or "")
    if sym.upper() in config.EXCLUDE_SYMBOLS:
        return True
    return any(re.search(pat, sym, re.IGNORECASE) for pat in config.EXCLUDE_SYMBOL_PATTERNS)


def _size_gates_only(market: dict) -> bool:
    """True when the ONLY failing market facts are size (liq/vol): a young token to re-check."""
    liq = market.get("liq_usd") or 0.0
    vol = market.get("vol_h24") or 0.0
    return (market.get("price_usd") or 0) > 0 and (liq < config.LIQ_FLOOR_USD or vol < config.MIN_VOL_H24_USD)


def run(dry_run: bool = True, send: bool = False) -> list:
    now_s = time.time()                       # THE single wall-clock capture
    budget = _Budget(config.RUN_TIME_BUDGET_S)
    http_client.reset_health()
    trigger = os.environ.get("TRIGGER") or ("schedule" if config.IS_CI else "manual")

    # ── state ────────────────────────────────────────────────────────────────────
    state = _load_json(config.STATE_PATH, {})
    state.setdefault("alerted", {})
    if state.get("blockscout_backoff_until", 0) > now_s:
        http_client.mark_blocked(BLOCKSCOUT_HOST, "cross-run backoff")
    if config.IS_CI and not config.BLOCKSCOUT_ENABLED_ON_RUNNER:
        http_client.mark_blocked(BLOCKSCOUT_HOST, "disabled on the runner (pre-flight)")
    champ_state = CHAMP.state()
    exit_plan = CHAMP.exit_plan()
    reg = BANDS.load_registry()
    champion = champ_state["entry_band"]["champion"]
    if champion not in reg.names():
        print(f"  [run] champion band {champion!r} is not registered; using {config.DEFAULT_ENTRY_BAND}")
        champion = config.DEFAULT_ENTRY_BAND
    led = ledger.load()
    ledger_index = ledger.index(led)
    watch = LAB.load_watchlist()
    recheck = _load_json(config.RECHECK_PATH, {})
    seen = {t: ts for t, ts in _load_json(config.SEEN_PATH, {}).items()
            if now_s - float(ts) < config.SEEN_TTL_S}
    cursor = _load_json(config.CURSOR_PATH, {})

    # ── discovery ────────────────────────────────────────────────────────────────
    head = rpc.block_number()
    disc, new_cursor, gap = discover_from_logs(cursor, head, budget)
    log_tokens = list(disc)
    watch_tokens = [t for t in LAB.watch_due(watch, ledger_index, now_s)][: config.DISCOVER_QUOTA["watchlist"] + 100]
    due_re = sorted((t for t, r in recheck.items() if float(r.get("next_check", 0)) <= now_s),
                    key=lambda t: float(recheck[t].get("next_check", 0)))[: config.RECHECK_PER_RUN]
    known = set(ledger_index) | set(watch) | set(recheck) | set(log_tokens)
    feeds = feed_tokens(seen, known, budget)
    # quotas: logs → watchlist → rechecks → feeds, unused quota spills forward; watchlist exempt
    # from MAX_DISCOVER (it is a batched re-enrich, not discovery)
    order: list = []
    q = dict(config.DISCOVER_QUOTA)
    spill = 0
    for name, toks in (("logs", log_tokens), ("watchlist", watch_tokens),
                       ("rechecks", due_re), ("feeds", sorted(feeds))):
        allow = q[name] + spill
        take = toks[:allow]
        spill = max(0, allow - len(take))
        order += [(name, t) for t in take]
    by_source = {n: sum(1 for s_, _t in order if s_ == n) for n in q}
    quota_cuts = {"logs": max(0, len(log_tokens) - by_source["logs"]),
                  "watchlist": max(0, len(watch_tokens) - by_source["watchlist"]),
                  "rechecks": max(0, len(due_re) - by_source["rechecks"]),
                  "feeds": max(0, len(feeds) - by_source["feeds"])}
    enrich_set = [t for s_, t in order if s_ == "watchlist"] + \
                 [t for s_, t in order if s_ != "watchlist"][: config.MAX_DISCOVER]
    src_of = {t: s_ for s_, t in order}
    for t in disc:
        recheck.get(t, {}).setdefault("disc", disc[t])   # log record persists with a recheck
    print(f"discover: logs {len(log_tokens)} (gap {gap} blocks) · watchlist {len(watch_tokens)} · "
          f"rechecks {len(due_re)} · feeds {len(feeds)} → enrich {len(enrich_set)}  "
          f"[trigger {trigger}, head {head}]")

    # ── enrichment ───────────────────────────────────────────────────────────────
    res = dex.enrich_many(enrich_set, now_s) if enrich_set else {"ok": {}, "absent": set(), "deferred": set()}
    markets = res.get("ok", {})
    absent = set(res.get("absent", set()))
    deferred = set(res.get("deferred", set()))
    deferred_by_stage = {"enrich": len(deferred)}
    rejected: list = []           # tokens evaluated with a market snapshot and rejected → seen
    for t in absent:
        src = src_of.get(t)
        r = recheck.get(t) or {"n_checks": 0, "first_seen": now_s}
        if src == "logs" or (src == "rechecks" and r.get("disc")):
            n = int(r.get("n_checks", 0))
            if n < len(config.RECHECK_SCHEDULE_S):
                r.update({"next_check": now_s + config.RECHECK_SCHEDULE_S[n], "n_checks": n + 1,
                          "disc": r.get("disc") or disc.get(t)})
                recheck[t] = r
            else:
                recheck.pop(t, None); seen[t] = now_s
        elif src == "watchlist":
            rejected.append(t)
        else:
            recheck.pop(t, None); seen[t] = now_s

    # ── pass 1: market gates, then ONE chain-facts batch ─────────────────────────
    chain_cache: dict = {}
    for t, w in watch.items():
        if w.get("chain_facts") and now_s - float(w.get("safety_ts") or 0) < config.SAFETY_REFRESH_S:
            chain_cache[t] = w["chain_facts"]
    p1_tokens: list = []
    pass1_rejects: dict = {}
    try:
        from sources import scanhood
        stocks = scanhood.stock_tokens()      # tokenized stocks are RWA, not launches (24 h cache)
    except Exception:
        stocks = set()
    for t, m in markets.items():
        if not budget.ok("pass1"):
            deferred.add(t); continue
        if t in stocks or _excluded_symbol(m.get("symbol")):
            pass1_rejects["infra_or_stock"] = pass1_rejects.get("infra_or_stock", 0) + 1
            rejected.append(t); continue
        if (m.get("price_usd") or 0) <= 0:
            pass1_rejects["has_market"] = pass1_rejects.get("has_market", 0) + 1
            rejected.append(t); continue
        if _size_gates_only(m):
            key = "liq_ok" if (m.get("liq_usd") or 0) < config.LIQ_FLOOR_USD else "vol_ok"
            pass1_rejects[key] = pass1_rejects.get(key, 0) + 1
            if src_of.get(t) == "watchlist":
                rejected.append(t)
            else:
                r = recheck.get(t) or {"n_checks": 0, "first_seen": now_s}
                n = int(r.get("n_checks", 0))
                if n < len(config.RECHECK_SCHEDULE_S) and (m.get("pair_age_min") or 0) < config.AGE_MAX_MINUTES:
                    r.update({"next_check": now_s + config.RECHECK_SCHEDULE_S[n], "n_checks": n + 1,
                              "disc": r.get("disc") or disc.get(t)})
                    recheck[t] = r
                else:
                    recheck.pop(t, None); seen[t] = now_s
            continue
        p1_tokens.append(t)
    dmap = {t: (disc.get(t) or (recheck.get(t) or {}).get("disc") or (watch.get(t) or {}).get("disc") or {})
            for t in p1_tokens}
    s1 = SAFE.pass1_many(p1_tokens, markets, dmap, now_s, chain_cache=chain_cache) if p1_tokens else {}
    survivors1: list = []
    for t in p1_tokens:
        s = s1.get(t) or SAFE.empty_safety()
        ok, gates = screen.hard_gates(markets[t], s)
        if not ok:
            for k, v in gates.items():
                if v is False:
                    pass1_rejects[k] = pass1_rejects.get(k, 0) + 1
            rejected.append(t)
            continue
        survivors1.append(t)

    # ── pass 2: budgeted + threaded (fresh tokens by liq desc, then watch refreshes) ──
    fresh = sorted((t for t in survivors1 if src_of.get(t) != "watchlist"),
                   key=lambda t: -(markets[t].get("liq_usd") or 0))
    watched = [t for t in survivors1 if src_of.get(t) == "watchlist"]
    refresh_due = [t for t in watched
                   if now_s - float((watch.get(t) or {}).get("refreshed_ts") or 0) >= config.GT_INFO_REFRESH_S]
    refresh_due = refresh_due[: config.WATCH_REFRESH_PER_RUN]
    p2_budget = config.GT_INFO_BUDGET_PER_RUN
    p2_list = fresh[:p2_budget] + refresh_due[: max(0, p2_budget - min(len(fresh), p2_budget)) + config.WATCH_REFRESH_PER_RUN]
    p2_list = p2_list[: p2_budget + config.WATCH_REFRESH_PER_RUN]
    deferred_by_stage["pass2_overflow"] = len([t for t in fresh if t not in p2_list])
    safety: dict = {}

    def _p2(t):
        if not budget.ok("pass2"):
            return t, None
        try:
            return t, SAFE.pass2(t, markets[t], s1[t], now_s, disc=dmap.get(t))
        except Exception as exc:          # one bad token never kills the run
            print(f"  ! pass2({t[:10]}…) failed: {exc}")
            return t, None
    with ThreadPoolExecutor(max_workers=config.PASS2_WORKERS) as ex:
        for t, s2 in ex.map(_p2, p2_list):
            if s2 is not None:
                safety[t] = s2
    for t in survivors1:
        if t in safety:
            continue
        w = watch.get(t) or {}
        if src_of.get(t) == "watchlist" and w.get("last_safety"):
            merged = dict(w["last_safety"]); merged.update({k: v for k, v in (s1.get(t) or {}).items() if v is not None})
            safety[t] = merged
        elif src_of.get(t) != "watchlist" and t in fresh:
            deferred.add(t)                # pass-2 overflow: retried next run, never seen
        else:
            safety[t] = s1.get(t) or SAFE.empty_safety()

    # ── score, bands, tier ───────────────────────────────────────────────────────
    survivors: list = []
    for t in survivors1:
        if t not in safety:
            continue
        m, s = markets[t], safety[t]
        ok, gates = screen.hard_gates(m, s)  # pass-2 facts can only add positive findings
        if not ok:
            for k, v in gates.items():
                if v is False:
                    pass1_rejects[k] = pass1_rejects.get(k, 0) + 1
            rejected.append(t); continue
        score, comps = screen.soft_score(m, s)
        first = t not in ledger_index
        fs_ts = (ledger_index.get(t) or {}).get("first_sighting_ts") or (watch.get(t) or {}).get("first_sighting_ts")
        age_s = 0.0 if first or not fs_ts else max(0.0, now_s - float(fs_ts))
        feat = LAB.build_feat(t, m, s, score, first, age_s)
        verdicts = LAB.evaluate_bands(feat, reg, champion)
        tier = LAB.tier_for(True, verdicts, champion)
        reason = LAB.champion_reason(feat, reg, champion, verdicts)
        _, misses = screen.high_conviction(feat)
        row = dict(feat)
        row.update({"symbol": m.get("symbol", "?"), "url": m.get("url", ""), "gates_ok": True,
                    "verdicts": verdicts, "feat": feat, "market": m, "score": score, "gates": gates,
                    "sources_dark": list(s.get("sources_dark") or []), "deployer": s.get("deployer"),
                    "tier": tier, "band": champion, "hc_misses": misses, "champion_reason": reason,
                    "misses": len(misses), "safety_ts": s.get("safety_ts"), "comps": comps,
                    "source": src_of.get(t), "bands": {k: (None if v is None else int(bool(v))) for k, v in verdicts.items()}})
        survivors.append(row)
    survivors.sort(key=lambda r: -r["score"])
    prior_true = {t: set((watch.get(t) or {}).get("bands_true") or []) for t in watch}
    events = LAB.decide_events(survivors, ledger_index, now_s, champion, reg, prior_true=prior_true)
    ev_by_token = {e["token"]: e for e in events}
    for r in survivors:
        e = ev_by_token.get(r["token"])
        r["event_kind"] = e["event_kind"] if e else None
        r["fired_band"] = e["fired_band"] if e else None
        if e:
            r["tier"] = e["tier"]
    n_na = sum(1 for r in survivors if r["verdicts"].get(champion) is None)
    champion_na_frac = (n_na / len(survivors)) if survivors else 0.0
    a_tier = [r for r in survivors if r["tier"] == "A" and r["event_kind"] == "promotion"
              or (r["tier"] == "A" and r["event_kind"] == "first_sighting")]
    print(f"screen: {len(markets)} enriched · {len(survivors1)} passed pass 1 · {len(survivors)} survivors · "
          f"{len(a_tier)} A · events {len(events)} · champion NA {champion_na_frac:.0%}")
    for r in survivors[: max(config.ALERT_TOP_N, len(a_tier))]:
        print(f"  {r['tier']} {r['symbol']:12s} score {r['score']:5.1f} liq ${r.get('liq_usd') or 0:,.0f} "
              f"age {r.get('pair_age_min') or 0:.0f}m [{r.get('event_kind') or '-'}]"
              + ("" if r["tier"] == "A" else f"  short of {champion}: {r['champion_reason'][:80]}"))

    # ── alerts ───────────────────────────────────────────────────────────────────
    cooldown = config.ALERT_COOLDOWN_HOURS * 3600
    fresh_alerts = [r for r in a_tier[: config.ALERT_TOP_N]
                    if now_s - float(state["alerted"].get(r["token"], 0)) >= cooldown]
    dark = sorted(http_client.degraded_hosts())
    for r in fresh_alerts:
        r["plan_line"] = CHAMP.describe_plan(exit_plan, r.get("price_usd") or 0.0, promoted=champ_state["exit"])
    if fresh_alerts:
        title, body = format_alert(fresh_alerts, degraded=dark, band=champion)
        send_all(title, body, dry_run=not send)
    else:
        print("no A-tier this run" + (" (sources dark: " + ", ".join(dark) + ")" if dark else ""))
    if dark and (now_s - float(state.get("degraded_alert_ts", 0)) >= config.DEGRADED_ALERT_COOLDOWN_HOURS * 3600):
        fields = sorted({f for r in survivors for f in SAFE.degraded_fields(r["feat"])})
        t2, b2 = format_degraded_notice(http_client.health_report(), dark, fields)
        send_all(t2, b2, dry_run=not send)
        if not dry_run:
            state["degraded_alert_ts"] = now_s
    # Blockscout cross-run backoff
    if http_client.is_blocked(BLOCKSCOUT_HOST) and not state.get("blockscout_backoff_until", 0) > now_s:
        n = int(state.get("blockscout_challenged_runs", 0)) + 1
        state["blockscout_challenged_runs"] = n
        if n >= 2:
            state["blockscout_backoff_until"] = now_s + config.BLOCKSCOUT_BACKOFF_S
            state["blockscout_challenged_runs"] = 0
    elif not http_client.is_blocked(BLOCKSCOUT_HOST):
        state["blockscout_challenged_runs"] = 0
    if champion_na_frac > 0.5 and len(survivors) >= 5 and \
            now_s - float(state.get("na_alert_ts", 0)) >= config.DEGRADED_ALERT_COOLDOWN_HOURS * 3600:
        from alerts import format_event
        t3, b3 = format_event("APPARATUS FAULT", [f"champion band {champion} was NA on {champion_na_frac:.0%} of "
                                                  f"{len(survivors)} survivors this run", "dark: " + ", ".join(dark)])
        send_all(t3, b3, dry_run=not send)
        if not dry_run:
            state["na_alert_ts"] = now_s

    # ── persist ──────────────────────────────────────────────────────────────────
    filled, exits, paper_stats = 0, [], {}
    seqs: dict = {}
    if not dry_run:
        ledger.ensure_exists()
        seqs = ledger.record_rows(events, alert_ts=now_s, plan_name=exit_plan["name"])
        for r in survivors:
            r["event_seq"] = seqs.get(r["token"])
        STORE.append_verdicts([{"event_seq": seqs[e["token"]], "token": e["token"], "alert_ts": now_s,
                                "verdicts": e["verdicts"]} for e in events if e["token"] in seqs])
        filled, exits = ledger.update_forward(now_s, lambda toks: dex.forward_snapshot_many(toks, now_s))
        if exits:
            t4, b4 = format_exit_alert(exits)
            send_all(t4, b4, dry_run=not send)
        if config.PAPER_EXEC:
            try:
                import paper_exec               # lazy: the book must never break alerting
                paper_stats = paper_exec.retry_pending(now_s) or {}
                for e in exits:
                    paper_exec.execute_exit(e, now_s)
                for r in fresh_alerts:
                    if r.get("event_seq") is not None:
                        paper_exec.open_position(r["token"], r["symbol"], "A", r["event_seq"], now_s,
                                                 plan=exit_plan, alert_ts=now_s)
            except Exception as exc:
                print(f"  ! paper_exec failed: {exc}")
        for r in fresh_alerts:
            state["alerted"][r["token"]] = now_s
        state["alerted"] = {t: ts for t, ts in state["alerted"].items() if now_s - float(ts) < 7 * 86400}
        # watchlist: survivors in, rejected counted, evictions
        watch = LAB.watch_update(watch, survivors, rejected, ledger_index, now_s)
        for r in survivors:
            w = watch.get(r["token"])
            if w is not None:
                w["last_safety"] = {k: v for k, v in safety.get(r["token"], {}).items() if not k.startswith("_")}
                cf = (s1.get(r["token"]) or {})
                if cf.get("pass") == 1 or r["token"] in chain_cache:
                    w["chain_facts"] = chain_cache.get(r["token"]) or w.get("chain_facts")
                if r["token"] in safety and safety[r["token"]].get("pass") == 2:
                    w["refreshed_ts"] = now_s
                w["disc"] = dmap.get(r["token"]) or w.get("disc")
        LAB.save_watchlist(watch)
        for t in rejected:
            if t not in watch:
                seen[t] = now_s
        for t in list(recheck):
            if t in ledger_index or t in seqs:
                recheck.pop(t, None)
        if len(recheck) > config.RECHECK_MAX:
            worst = sorted(recheck, key=lambda t: (-int(recheck[t].get("n_checks", 0)), float(recheck[t].get("next_check", 0))))
            for t in worst[: len(recheck) - config.RECHECK_MAX]:
                recheck.pop(t, None)
        _atomic_json(config.SEEN_PATH, seen)
        _atomic_json(config.RECHECK_PATH, recheck)
        _atomic_json(config.CURSOR_PATH, dict(new_cursor, updated_ts=now_s))
        _atomic_json(config.STATE_PATH, state, indent=1)
        n_moved = ledger.rotate(now_s)
        if n_moved:
            print(f"ledger: rotated {n_moved} resolved row(s) older than {config.LEDGER_ROTATE_AFTER_DAYS} d")
        print(f"ledger: +{len(seqs)} event row(s), {filled} horizon cell(s) filled, {len(exits)} exit(s), paper {paper_stats}")
    else:
        print(f"(dry run — would ledger {len(events)} event(s); nothing written)")

    # ── the point-in-time feature store + run log ─────────────────────────────────
    scan = {"scan_ts": now_s, "trigger": trigger, "band": champion, "bands_hash": STORE.bands_code_hash(),
            "champion": {"exit": exit_plan["name"], "entry_band": champion},
            "champion_na_frac": _round(champion_na_frac), "head": head, "cursor": new_cursor, "gap_blocks": gap,
            "discovered": len(order), "by_source": by_source, "quota_cuts": quota_cuts,
            "enriched": len(markets), "absent": len(absent), "deferred": len(deferred),
            "deferred_by_stage": dict(deferred_by_stage, **{f"budget_{k}": v for k, v in budget.cuts.items()}),
            "pass1_rejects_by_gate": pass1_rejects,
            "survivors": [{k: _round(v) if isinstance(v, float) else v for k, v in r.items()
                           if k not in ("feat", "market", "verdicts", "comps")} for r in survivors],
            "plan": exit_plan, "health": http_client.health_snapshot(), "dark": dark,
            "run_seconds": round(budget.elapsed(), 1)}
    if not dry_run:
        _atomic_json(config.SCAN_PATH, scan)
        keep = [l for l in (_load_json_lines(config.RUN_LOG_PATH)) if now_s - float(l.get("scan_ts", 0)) < config.RUN_LOG_KEEP_S]
        keep.append({"scan_ts": now_s, "trigger": trigger, "run_seconds": scan["run_seconds"],
                     "discovered": len(order), "survivors": len(survivors), "n_a": len(fresh_alerts),
                     "events": len(events), "deferred": bool(budget.cuts), "dark": dark})
        tmp = f"{config.RUN_LOG_PATH}.{os.getpid()}.tmp"
        with open(tmp, "w") as f:
            for l in keep:
                f.write(json.dumps(_clean(l)) + "\n")
        os.replace(tmp, config.RUN_LOG_PATH)
    print(f"run_seconds {scan['run_seconds']}  budget cuts {budget.cuts or 'none'}")
    print("--- source health ---\n" + http_client.health_report())
    return survivors


def _load_json_lines(path: str) -> list:
    out = []
    if os.path.exists(path):
        for line in open(path):
            try:
                out.append(json.loads(line))
            except Exception:
                continue
    return out


if __name__ == "__main__":
    args = set(sys.argv[1:])
    run(dry_run=not ("--send" in args or "--commit" in args), send="--send" in args)
