"""
The keeper watchdog's alert half. The `*/5` cron job (.github/workflows/keeper-watchdog.yml)
restarts a dead keeper in bash (via `keeper.sh --keeper-alive` / `--ensure-keeper`) and then
runs THIS to say so when the scan is stale.

    python3 watchdog.py            # offline fixtures, then a DRY assessment of data/latest_scan.json
    python3 watchdog.py --send     # the workflow's path: alert SCAN STALE when the scan is stale

Pure core: assess(scan, now_s, action) -> lines. No scan file → one line; a scan older than
config.KEEPER_STALE_S → one line with the age, the trigger and what the watchdog did
(WATCHDOG_ACTION ∈ restarted | none | circuit-open); a scan whose keys are missing or malformed
is NOT a finding — a broken file is a different failure from a silent keeper, and the dashboard
already shows it. The wall clock is captured EXACTLY once, in main(). stdlib + config + alerts
only — no subprocess, no http_client, no sources: this module can never scan.
"""
from __future__ import annotations

import json
import os
import sys
import time

import config
import alerts

ACTION_WORDS = {"restarted": "keeper restarted: yes",
                "none": "keeper restarted: no",
                "circuit-open": "circuit open (3+ keeper failures in 2 h; not restarting — needs a human)"}
ALERT_ENV = ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "NTFY_TOPIC")


def _read_json(path: str):
    """A dict/list from a JSON file, or None when missing or unreadable (dashboard._read_json's
    contract, repeated here because dashboard imports pandas and this module is stdlib-only)."""
    try:
        if path and os.path.exists(path) and os.path.getsize(path) > 0:
            with open(path) as f:
                return json.load(f)
    except Exception as exc:
        print(f"  [watchdog] {path} unreadable ({exc})")
    return None


def assess(scan, now_s: float, action: str) -> list:
    """The findings (0 or 1 line). Unknown / missing / malformed keys are never a finding."""
    if scan is None:
        return ["no latest_scan.json"]
    if not isinstance(scan, dict):
        return []
    try:
        scan_ts = float(scan.get("scan_ts"))
    except (TypeError, ValueError):
        return []
    if scan_ts != scan_ts:                                   # NaN
        return []
    age_s = now_s - scan_ts
    if age_s <= config.KEEPER_STALE_S:
        return []
    trigger = scan.get("trigger") or "?"
    what = ACTION_WORDS.get(str(action), f"watchdog action: {action}")
    return [f"last scan {int(age_s // 60)} min ago (trigger {trigger}); {what}"]


def main(argv) -> int:
    send = "--send" in argv
    now_s = time.time()                                      # THE single wall-clock capture
    action = os.environ.get("WATCHDOG_ACTION") or "none"
    if send:
        missing = [k for k in ALERT_ENV if not os.environ.get(k)]
        if missing:
            print(f"watchdog: --send needs {', '.join(missing)} (alerts.send_all is a silent no-op without them)")
            return 2
    lines = assess(_read_json(config.SCAN_PATH), now_s, action)
    if not lines:
        print(f"watchdog: scan fresh (action={action}); nothing to say")
        return 0
    title, body = alerts.format_event("SCAN STALE", lines)
    alerts.send_all(title, body, dry_run=not send)
    return 0


if __name__ == "__main__":
    # Offline fixtures first (a fixed clock, never time.time() here), then the real file, DRY
    # unless --send was given — the only path that sends.
    _now = 1_800_000_000.0
    for _label, _scan, _act in (("fresh", {"scan_ts": _now - 120, "trigger": "keeper"}, "none"),
                                ("45 min, restarted", {"scan_ts": _now - 45 * 60, "trigger": "keeper"}, "restarted"),
                                ("60 min, circuit open", {"scan_ts": _now - 3600, "trigger": "watchdog"}, "circuit-open"),
                                ("no file", None, "none"),
                                ("unknown keys", {"foo": 1}, "none"),
                                ("garbage ts", {"scan_ts": "x"}, "restarted")):
        print(f"  fixture {_label:<22} -> {assess(_scan, _now, _act)}")
    sys.exit(main(sys.argv[1:]))
