"""
Fetch a price path for every ledger row. Resumable, cache-backed, safe to re-run.

PORTED from solana_screener/selfimprove/backfill.py (2026-09-12): the ledger is read through
`ledger.load()` (token/symbol/tier/alert_ts/event_seq columns — `token`, never `mint`), the
clock is read ONCE in main and threaded into `paths.fetch_path(token, alert_ts, now_s)`.

RUN IT BY HAND, REPEATEDLY, NEVER FROM A SCHEDULER. GeckoTerminal's free tier is a hard 30
calls/min PER IP and on the Mac that IP is shared with solana-listener / -livebook / the solana
backfill and this project's own jobs, so http_client holds this host at
config.GECKOTERMINAL_RATE_HZ_MAC (0.25 Hz). Budget: ~1 pools call + up to config.PATHS_MAX_PAGES
OHLCV calls per resolution tried, at 4 s each ≈ 12–20 s/row — this is a background job, not
something to wait on, and a job that ran unattended would starve every other GT reader on this
machine. A single pass will still leave rows `deferred` (429s from the other jobs' bursts).
Deferred rows are NOT written as failures — they are simply absent from the output, so the next
pass retries exactly them and nothing else. Two or three passes converge. Never interpret a
missing row as a dead token; see paths.fetch_path.

`--since=<days>` restricts the pass to rows alerted inside the window (newest first, as always).
The recent cohort is where the fine resolutions still exist: paging reaches ~2.8 days at 1m, so
a 5-day pass is the set for which an ORDERED answer is even possible, and spending the budget
there first is the difference between 1m bars and 1h bars on the rows that matter.

Output: cache/paths.jsonl (regenerable, gitignored via cache/), one record per priced row:
    {token, symbol, tier, event_seq, alert_ts, res, pages, entry, n_bars, bars:[{ts,o,h,l,c,v}]}
`res` is the bar resolution actually used (1m / 15m / 1h) and MUST be carried into any analysis —
an hour-long bar cannot resolve a token that peaks four minutes after the alert. `pages` is how
many OHLCV pages that resolution needed; a row reached only on page 4 of a backward walk rests
on a different call budget than one the newest page covered.

A ledger token can carry several rows (first_sighting → band_fire → promotion, each at its own
alert_ts and entry), so the resume key is `event_seq` — the ledger's forward-only key — falling
back to the token address for a row that has none.
"""
from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config                                  # noqa: E402
import ledger as LED                           # noqa: E402
from selfimprove import paths as P             # noqa: E402

OUT = os.path.join(config.CACHE_DIR, "paths.jsonl")   # regenerable cache, not committed state
_EMPTY = ("", "nan", "None", "NaN")


def _key(token, event_seq) -> str:
    """Resume key: event_seq when the row has one (several events per token), else the token."""
    s = str(event_seq)
    if s in _EMPTY:
        return str(token).lower()
    try:
        return f"seq:{int(float(s))}"
    except (TypeError, ValueError):
        return str(token).lower()


def _done() -> set:
    if not os.path.exists(OUT):
        return set()
    got = set()
    with open(OUT) as fh:
        for line in fh:
            try:
                r = json.loads(line)
                got.add(_key(r["token"], r.get("event_seq")))
            except Exception:
                continue
    return got


def _rows(led, now_s: float, since_days: float | None = None) -> list:
    """Ledger rows as plain dicts with a numeric alert_ts, newest first (finest resolution
    still available). A row with no usable alert_ts is data, not a crash: skipped.
    `since_days` keeps only rows alerted at or after now_s - since_days*86400."""
    floor = None if not since_days else float(now_s) - float(since_days) * 86400.0
    rows = []
    for _, r in led.iterrows():
        try:
            a = float(r["alert_ts"])
        except (TypeError, ValueError):
            continue
        if not (a > 0) or str(r["alert_ts"]) in _EMPTY:
            continue
        if floor is not None and a < floor:
            continue
        rows.append({"token": str(r["token"]).lower(), "symbol": str(r.get("symbol") or ""),
                     "tier": str(r.get("tier") or ""), "event_seq": r.get("event_seq"),
                     "alert_ts": a})
    rows.sort(key=lambda r: -r["alert_ts"])          # newest first
    return rows


def run(now_s: float, ledger_path: str | None = None, limit: int | None = None,
        since_days: float | None = None) -> dict:
    """One pass over the ledger. `now_s` is captured by the caller (main) exactly once."""
    ledger_path = ledger_path or config.LEDGER_PATH
    led = LED.load(ledger_path)
    # WHICH LEDGER THIS IS. Every number downstream is a statement about this file at this
    # moment, and the cloud keeper commits new events every few minutes, so name the file, its
    # size and its high-water event_seq before spending a single call — and say so when
    # origin/main has already moved past it (print-only; the worktree stays the source).
    print(f"ledger {ledger_path}: {len(led)} row(s), max event_seq "
          f"{LED.next_event_seq(led) - 1}", flush=True)
    LED.warn_if_behind_origin(led, "backfill")
    rows = _rows(led, now_s, since_days)
    if since_days:
        print(f"  --since={since_days:g}d: {len(rows)} row(s) alerted inside the window", flush=True)
    done = _done()
    todo = [r for r in rows if _key(r["token"], r["event_seq"]) not in done]
    if limit:
        todo = todo[:limit]
    print(f"ledger {len(rows)} rows; already priced {len(done)}; attempting {len(todo)}",
          flush=True)
    stats = {"ok": 0, "absent": 0, "no_cover": 0, "deferred": 0}
    if not todo:
        print("nothing to backfill", flush=True)
        return stats
    by_res: dict = {}
    t0 = time.time()
    with open(OUT, "a") as fh:
        for i, r in enumerate(todo, 1):
            a = r["alert_ts"]
            try:
                p = P.fetch_path(r["token"], a, now_s)
            except Exception as e:                      # a bad row is data, not a crash
                print(f"  !! {r['token'][:12]}: {type(e).__name__}: {e}", flush=True)
                stats["deferred"] += 1
                continue
            stats[p["status"]] = stats.get(p["status"], 0) + 1
            if p["status"] != "ok":
                continue                                # deferred rows stay absent -> retried
            by_res[p["res"]] = by_res.get(p["res"], 0) + 1
            seq = r["event_seq"]
            try:
                seq = int(float(seq)) if str(seq) not in _EMPTY else None
            except (TypeError, ValueError):
                seq = None
            fh.write(json.dumps({
                "token": r["token"], "symbol": r["symbol"], "tier": r["tier"],
                "event_seq": seq, "alert_ts": a, "res": p["res"],
                "pages": int(p.get("pages") or 0), "entry": p["entry"],
                "n_bars": len(p["bars"]), "bars": p["bars"]}) + "\n")
            fh.flush()
            if i % 25 == 0:
                print(f"  {i}/{len(todo)}  {time.time()-t0:.0f}s  {stats}  res={by_res}",
                      flush=True)
    print(f"done in {time.time()-t0:.0f}s  {stats}  resolutions={by_res}", flush=True)
    print(f"  -> {OUT} now holds {len(_done())} priced rows", flush=True)
    if stats.get("deferred"):
        print(f"  {stats['deferred']} rows were RATE-LIMITED, not absent. Re-run to retry them.",
              flush=True)
    return stats


def main(argv: list | None = None) -> int:
    lim = None
    src = None
    since = None
    for a in (sys.argv[1:] if argv is None else argv):
        if a.startswith("--limit="):
            lim = int(a.split("=", 1)[1])
        if a.startswith("--ledger="):
            src = a.split("=", 1)[1]
        if a.startswith("--since="):
            since = float(a.split("=", 1)[1])
    now_s = time.time()            # the ONE clock read; everything below is a parameter
    run(now_s, src, lim, since)
    return 0


if __name__ == "__main__":
    sys.exit(main())
