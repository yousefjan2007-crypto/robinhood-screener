"""
Price PATHS for ledger rows — the data the ledger has never had.

PORTED from solana_screener/selfimprove/paths.py (2026-09-12). Same shape, same three-way status
contract, same call-budget optimisation; the network calls now go through sources/geckoterminal.py
(network config.GT_NETWORK), the token column is `token` (an EVM address), and `now_s` is a
PARAMETER — the solana original called time.time() inside fetch_path, which is a compute path.

WHY THIS EXISTS. `ledger.py` samples four horizons (1h/6h/24h/7d) plus a `max_ret_seen`
high-water mark taken on the scan grid. That is enough to say what a position was worth at four
moments and enough to say a level was touched at *some* point, but it cannot answer the
question this project actually needs answered:

    the A-tier band's median return is +34% at 6h and -99% at 24h (solana cloud ledger, n=13).
    The selection works. There is no exit. WHICH exit would have kept the +34%?

Answering that requires an ordered path, because "did price fall below the stop BEFORE it rose
through the take-profit" is a statement about order, and no number of horizon snapshots contains
it. `max_ret_seen` and `min_ret_seen` are both realised for almost every row (median max +13%,
median min -97%), so without ordering every policy is simultaneously a winner and a loser.

WHY GECKOTERMINAL. On solana the free pump.fun swap-api returned the most recent 500 bars **in
which trades occurred**, not a contiguous window, so an alert three weeks old had scrolled out.
GeckoTerminal's pool OHLCV does reach back (a solana alert from 2026-07-03 was covered by its 1h
series), and on Robinhood Chain it is the only free OHLCV source at all (survey explore_agent6.md
§2a). Its `ohlcv_list` timestamps are SECONDS; this module keeps everything in seconds and says so.

    NOTE for anyone porting `entry_bot/candles.py`: its `ts` is WRONG BY 1000x. swap-api returns
    `timestamp` in MILLISECONDS and candles.py:66 reads it as seconds, so `path_from`'s
    `b["ts"] >= entry_ts` compares seconds against milliseconds and can never exclude a bar.
    entry_bot's verify.py misses it because its fixture uses ts=100/200 with entry=150, which is
    internally consistent.

RESOLUTION IS A DATA-QUALITY FIELD, NOT AN IMPLEMENTATION DETAIL. Coverage depth trades off
against granularity: 1m reaches back hours, 15m days, 1h weeks. We take the FINEST series that
covers the alert and record which one it was, because a 1h bar's high and low have no order
across a whole hour — a token that peaks four minutes after the alert is unresolvable at 1h.
Any analysis that pools resolutions without reporting the mix is lying about its precision, so
`fetch_path` returns `res` and the evaluator stratifies on it.
"""
from __future__ import annotations

import os
import sys

# selfimprove/ is a subpackage but its siblings live one level up and import each other flat
# (`import config`), so the repo root must be importable however this file is invoked. Same
# bootstrap idiom as signal_lab/deepvalue/.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config                                                        # noqa: E402
from http_client import get_json, is_absent, is_deferred             # noqa: E402
from sources import geckoterminal as GT                              # noqa: E402

# Finest first. (timeframe, aggregate, label, approx_span_sec per PAGE at limit=config.PATHS_LIMIT
# — `fetch_path` multiplies by config.PATHS_MAX_PAGES for the reach, since `_ohlcv` pages back.)
RESOLUTIONS = (
    ("minute", 1, "1m", config.PATHS_LIMIT * 60),
    ("minute", 15, "15m", config.PATHS_LIMIT * 15 * 60),
    ("hour", 1, "1h", config.PATHS_LIMIT * 3600),
)
LIMIT = config.PATHS_LIMIT
CACHE_MAX_AGE_SEC = config.PATHS_CACHE_MAX_AGE_S   # a dead token's history is immutable; a live one still moves


def _cache(name: str) -> str:
    d = os.path.join(config.CACHE_DIR, "paths")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, name)


DEFERRED = "deferred"    # we could not get an answer (429 / network) — retry later
ABSENT = "absent"        # GeckoTerminal answered and knows no such pool — a real, final answer


def top_pool(token: str) -> tuple[str, str | None]:
    """(status, deepest-reserve base-leg pool address). status is "ok" | ABSENT | DEFERRED.

    THE THREE-WAY SPLIT IS LOAD-BEARING, and the solana version of this function got it wrong on
    its first build. A bare None for BOTH "HTTP 429" and "no such token" silently converts a rate
    limit into a fabricated outcome: on the very first probe run that produced 4 bogus
    `no_pool`/`no_cover` rows out of 6, purely because GeckoTerminal was throttling — the same
    mistake `corpus/enrich.py` documents as having fabricated 472 of 1,400 rows as dead tokens.
    A deferred row must be retried, never scored.

    This is why the pools call goes through `http_client.get_json` against the same endpoint
    `sources/geckoterminal.token_pools` uses, rather than through `token_pools` itself: that
    helper returns [] for deferred AND absent (fine for a discovery feed, fatal here). The
    OHLCV leg uses `geckoterminal.pool_ohlcv`, which preserves bars / NOT_FOUND / None.

    BASE LEG ONLY. A pool where the token is the QUOTE token reports the other asset's price —
    this project measured a $1,888 MIZUKARA from exactly this, and `entry_bot/corpus/peaks.py`
    ports the same filter. GT relationship ids are 'robinhood_0x…' lowercase, so the check is
    base_id.lower().endswith(token.lower()).
    """
    token = (token or "").lower()
    d = get_json(f"{GT.BASE}/networks/{config.GT_NETWORK}/tokens/{token}/pools",
                 cache_path=_cache(f"pools_{token}.json"), max_age_sec=CACHE_MAX_AGE_SEC)
    if is_deferred(d):
        return DEFERRED, None
    if is_absent(d) or not isinstance(d, dict):
        return ABSENT, None
    pools = d.get("data")
    if not pools:
        return ABSENT, None
    best, best_res = None, -1.0
    for p in pools:
        if not isinstance(p, dict):
            continue
        a = p.get("attributes") or {}
        rel = ((p.get("relationships") or {}).get("base_token") or {}).get("data") or {}
        base_id = str(rel.get("id") or "").lower()
        if base_id and not base_id.endswith(token):
            continue                      # token is the quote leg — its price is not this series
        try:
            res = float(a.get("reserve_in_usd") or 0.0)
        except (TypeError, ValueError):
            res = 0.0
        addr = (a.get("address") or "").lower() or None
        if addr and res > best_res:
            best, best_res = addr, res
    return ("ok", best) if best else (ABSENT, None)


def _ohlcv(pool: str, timeframe: str, aggregate: int, alert_ts: float) -> tuple[str, list, int]:
    """(status, one OHLCV series, pages fetched) oldest-first, seconds. Same three-way split.

    Bars come from `geckoterminal.pool_ohlcv`, which already sorts oldest-first, collapses GT's
    repeated timestamps and keeps `v` as None when the feed omits volume — distinct from a real
    0.0: the evaluator's sanitizer drops v==0 bars as phantom prints, and a bar whose volume is
    merely UNREPORTED must not be conflated in.

    PAGING BACKWARD. One page is LIMIT bars and GT emits a bar per traded minute, so on a busy
    pool the newest minute/1 page begins only ~16 h ago: FOMOPAD's unpaged page started at
    18:34Z for a 14:51:50Z alert and the row read `no_cover` while the token was in fact up
    4.6x inside it. So while the oldest bar we hold is still NEWER than the alert, the page came
    back full (a short page means GT has nothing older), and we are under config.PATHS_MAX_PAGES,
    ask again for the window ENDING at that oldest bar. Pages are merged on ts, keeping the
    larger reported volume exactly as pool_ohlcv does within a page — the window boundary repeats
    a bar, and one bar counted twice would double its volume in the evaluator's sanitizer.

    A PAGE WE COULD NOT FETCH POISONS THE WHOLE RESOLUTION. Returning the pages we did get would
    quietly hand the caller a series that starts after the alert, which `fetch_path` would read
    as "does not reach back" and — with no deferral recorded — as `no_cover`, i.e. as the claim
    that this token had no price. That is exactly the conflation `corpus/enrich.py` documents as
    having fabricated 472 of 1,400 rows as dead tokens. Deferred here, retried next pass.
    """
    by_ts: dict = {}
    before = None
    pages = 0
    max_pages = max(1, int(config.PATHS_MAX_PAGES))
    while True:
        name = (f"ohlcv_{pool}_{timeframe}{aggregate}.json" if before is None
                else f"ohlcv_{pool}_{timeframe}{aggregate}_b{int(before)}.json")
        bars = GT.pool_ohlcv(pool, timeframe, aggregate, LIMIT,
                             cache_path=_cache(name), max_age_sec=CACHE_MAX_AGE_SEC,
                             before_timestamp=before)
        if is_deferred(bars):
            return DEFERRED, [], pages
        pages += 1
        if is_absent(bars) or not isinstance(bars, list):
            break                              # GT knows no bars in this window: a real answer
        n_raw = len(bars)
        oldest = None
        for b in bars:
            try:
                rec = {"ts": float(b["ts"]), "o": float(b["o"]), "h": float(b["h"]),
                       "l": float(b["l"]), "c": float(b["c"]),
                       "v": None if b.get("v") is None else float(b["v"])}
            except (TypeError, ValueError, KeyError):
                continue
            prev = by_ts.get(rec["ts"])
            if prev is None or (rec["v"] or 0.0) > (prev["v"] or 0.0):
                by_ts[rec["ts"]] = rec
            oldest = rec["ts"] if oldest is None else min(oldest, rec["ts"])
        if oldest is None or oldest <= alert_ts or n_raw < LIMIT or pages >= max_pages:
            break
        before = int(oldest)
    out = [by_ts[k] for k in sorted(by_ts)]
    return ("ok" if out else ABSENT), out, pages


def fetch_path(token: str, alert_ts: float, now_s: float) -> dict:
    """Bars at or after `alert_ts` for `token`, at the finest resolution that covers the alert.

    `now_s` is passed IN (the caller captures time.time() once) — it only steers which
    resolutions are worth a call; it never enters a price.

    Returns {"status", "res", "pool", "entry", "bars", "n_before", "pages"}.

    `pages` is the number of OHLCV pages actually fetched — for the resolution that answered on
    an `ok`, across every resolution tried otherwise. It belongs beside `res` in the record: a
    row covered only by page 4 of a backward walk rests on a different call budget than one the
    newest page covered, and a coverage change between passes is visible nowhere else.

    `status` is one of:
      ok        — a covering series was found and there is at least one bar at/after the alert
      absent    — GeckoTerminal answered and knows no base-leg pool for this token
      no_cover  — pools exist and every series answered, but none reaches back to the alert
                  (the active bars around it have scrolled out of the LIMIT-bar window)
      deferred  — we could not get an answer (429 / network). RETRY; never score.

    `deferred` is separated from the two real answers for the reason `corpus/enrich.py` documents
    at length: conflating "we were rate limited" with "this token has no price" fabricated 472 of
    1,400 rows as dead tokens on an earlier build of the sibling corpus. A row that cannot be
    priced must be dropped from a policy comparison and COUNTED, never treated as a loss.

    `entry` is the OPEN of the first bar at or after the alert — the first price actually
    transactable then. Using the bar's close would be later information.
    """
    st, pool = top_pool(token)
    if st != "ok":
        return {"status": st, "res": None, "pool": None, "entry": 0.0,
                "bars": [], "n_before": 0, "pages": 0}
    # SKIP RESOLUTIONS THAT CANNOT REACH BACK. A 1000-bar 1m series spans ~17 hours, so asking
    # for it on a three-week-old alert spends a call to learn something arithmetic. Start at the
    # finest resolution whose span could plausibly cover the alert's age, and keep one finer as a
    # safety margin (bars are emitted only when trades occur, so a quiet token's series reaches
    # back further in wall-clock than its nominal span). Measured 2026-08-12 on solana: this was
    # ~2 wasted calls per row out of ~3, against a hard 30/min budget shared by every job on
    # this IP.
    # A resolution's reach is its span TIMES the page cap, now that _ohlcv walks backward: 1m
    # reaches ~2.8 days, not ~17 hours. Picking off the single-page span would send a two-day-old
    # alert straight to 15m bars and throw away the ordering the whole lab exists to recover.
    age = max(0.0, float(now_s) - float(alert_ts))
    reach = max(1, int(config.PATHS_MAX_PAGES))
    start = 0
    for i, (_tf, _ag, _lb, span) in enumerate(RESOLUTIONS):
        if span * reach >= age:
            start = i
            break
    else:
        start = len(RESOLUTIONS) - 1
    start = max(0, start - 1)              # one finer than needed, as the margin
    deferred_any = False
    pages_total = 0
    for timeframe, agg, label, _span in RESOLUTIONS[start:]:
        bst, bars, pg = _ohlcv(pool, timeframe, agg, alert_ts)
        pages_total += pg
        if bst == DEFERRED:
            deferred_any = True
            continue
        if not bars or bars[0]["ts"] > alert_ts:
            continue                       # does not reach back far enough — try coarser
        after = [b for b in bars if b["ts"] >= alert_ts]
        if not after:
            continue
        return {"status": "ok", "res": label, "pool": pool, "entry": after[0]["o"],
                "bars": after, "n_before": len(bars) - len(after), "pages": pg}
    # every resolution either failed to answer or failed to cover. If ANY was unanswered we
    # cannot claim "no_cover" — that would be the fabrication above.
    return {"status": DEFERRED if deferred_any else "no_cover", "res": None, "pool": pool,
            "entry": 0.0, "bars": [], "n_before": 0, "pages": pages_total}


if __name__ == "__main__":
    import time
    # One live fetch on the canonical anchor: 1 pools call + up to PATHS_MAX_PAGES OHLCV calls
    # per resolution tried, at 0.25 Hz.
    # The clock is read ONCE here, at the entry point, and threaded through.
    now_s = time.time()
    alert_ts = now_s - 3 * 86400
    print(f"paths smoke (network={config.GT_NETWORK})  token={config.MIZUKARA}  "
          f"alert_ts={time.strftime('%Y-%m-%d %H:%M', time.gmtime(alert_ts))} UTC (now-3d)")
    try:
        p = fetch_path(config.MIZUKARA, alert_ts, now_s)
    except Exception as e:                       # GT dark / malformed: degrade, don't assert
        print(f"  fetch_path raised {type(e).__name__}: {e}")
        p = {"status": DEFERRED, "res": None, "pool": None, "entry": 0.0, "bars": [],
             "n_before": 0, "pages": 0}
    print(f"  status={p['status']}  res={p['res']}  pages={p['pages']}  pool={p['pool']}  "
          f"n_bars={len(p['bars'])}  n_before={p['n_before']}  entry={p['entry']:.4g}")
    if p["status"] == "ok":
        b0, bN = p["bars"][0], p["bars"][-1]
        print(f"  first bar ts={b0['ts']:.0f} (>= alert_ts: {b0['ts'] >= alert_ts})  "
              f"last bar ts={bN['ts']:.0f}  entry==first open: {p['entry'] == b0['o']}")
        if p["pool"] != config.MIZUKARA_POOL.lower():
            print(f"  note: deepest base-leg pool is {p['pool']}, not config.MIZUKARA_POOL")
    elif p["status"] == DEFERRED:
        print("  GT deferred (rate-limited / dark) — retry later; this is NOT 'no price'.")
    else:
        print(f"  {p['status']}: GT answered; no series covers this alert at any resolution.")
