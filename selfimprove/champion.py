"""
The champion state — what the runtime executes — and its ONLY writer.

selfimprove/champion.json (tracked in git; the Actions runner reads it on every scan):
{
  "schema": 2, "locked": false,
  "exit":       {"champion": str, "promoted_ts", "promoted_at_alert_seq", "promoted_at_ts",
                 "previous", "evidence": {...}, "nominee"?, "nominated_at_alert_seq"?,
                 "nominated_ts"?, "failed_nominee"?, "failed_ts"?, "demotion_judged"?},
  "entry_band": {"champion": str, "promoted_ts", "promoted_at_event_seq", "previous",
                 "evidence": {...}, "nominee"?, "nominated_at_event_seq"?, "nominated_ts"?,
                 "metric", "failed_nominee"?, "failed_ts"?, "demotion_judged"?},
  "history":    [ {ts, arm, action, from, to, reason|evidence_summary}, ... ]
}

Defaults on absence / corruption: exit = config.IMPROVE_DEFAULT_EXIT_CHAMPION (the plan alerts
print and paper_exec executes, so the paired baseline is what the system actually does), entry
band = config.DEFAULT_ENTRY_BAND.

Two independent counters live here on purpose: alert_seq (exit arm) is minted by the Mac-local
livebook in gitignored data/livebook.json; event_seq (entry arm) by the cloud runner in the
committed ledger. If the livebook is ever rebuilt, alert_seq restarts at 1 — which is why the
exit nominee's forward test also requires opened_ts > nominated_ts.

write_state() is the sole writer (verify greps that no other module opens CHAMPION_PATH for
writing): read-modify-write, tmp + os.replace, never partial. `locked` (or the PAUSE file)
makes every --apply run evaluate-and-report only. The CLI `--set` is the human rollback path:
it goes through write_state with evidence {manual: true, reason}, so an audit line exists.
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config                                  # noqa: E402
from selfimprove import policies as POL        # noqa: E402

SCHEMA = 2


def _default_state() -> dict:
    return {"schema": SCHEMA, "locked": False,
            "exit": {"champion": config.IMPROVE_DEFAULT_EXIT_CHAMPION, "promoted_ts": None,
                     "promoted_at_alert_seq": None, "promoted_at_ts": None, "previous": None,
                     "evidence": None},
            "entry_band": {"champion": config.DEFAULT_ENTRY_BAND, "promoted_ts": None,
                           "promoted_at_event_seq": None, "previous": None, "evidence": None,
                           "metric": config.BAND_OUTCOME_METRIC},
            "history": []}


def state(path: str | None = None) -> dict:
    """The champion state, with defaults filled in. Prints (never raises) when the file is
    missing or corrupt — the alert path must keep working."""
    path = path or config.CHAMPION_PATH
    st = _default_state()
    if not os.path.exists(path):
        return st
    try:
        raw = json.load(open(path))
    except Exception as exc:
        print(f"  [champion] {path} unreadable ({exc}); using defaults")
        return st
    if not isinstance(raw, dict):
        return st
    for arm in ("exit", "entry_band"):
        if isinstance(raw.get(arm), dict):
            st[arm].update(raw[arm])
        if not st[arm].get("champion"):
            st[arm]["champion"] = _default_state()[arm]["champion"]
    st["locked"] = bool(raw.get("locked", False))
    st["history"] = list(raw.get("history") or [])
    return st


def paused(path: str | None = None) -> bool:
    return os.path.exists(config.PAUSE_PATH) or bool(state(path).get("locked"))


def exit_champion(path: str | None = None) -> str:
    name = state(path)["exit"]["champion"]
    if name not in POL.POLICIES:
        print(f"  [champion] exit champion {name!r} is not a registered policy; using default")
        return config.IMPROVE_DEFAULT_EXIT_CHAMPION
    return name


def entry_band(path: str | None = None) -> str:
    return state(path)["entry_band"]["champion"]


def write_state(exit: dict | None = None, entry_band: dict | None = None,
                history_line: dict | None = None, locked: bool | None = None,
                path: str | None = None) -> dict:
    """THE sole writer. Merges the given arm dict(s) into the current state and writes
    atomically. Returns the new state."""
    path = path or config.CHAMPION_PATH
    st = state(path)
    if exit:
        st["exit"].update(exit)
    if entry_band:
        st["entry_band"].update(entry_band)
    if locked is not None:
        st["locked"] = bool(locked)
    if history_line:
        st["history"].append(history_line)
    st["schema"] = SCHEMA
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w") as f:
        json.dump(st, f, indent=1, sort_keys=True, allow_nan=False)
    os.replace(tmp, path)
    return st


# ── what the exit champion controls at runtime ───────────────────────────────────
def exit_plan(name: str | None = None) -> dict:
    """{name, ladder:[[m,f],..], stop, trail, max_hold_s} for a policy name. A control name,
    a deleted candidate or garbage falls back to the default plan with a printed warning —
    never a crash in the alert path."""
    name = name or exit_champion()
    pol = POL.POLICIES.get(name)
    if pol is None or name in POL.CONTROLS or "random_exit" in (pol or {}):
        if name != config.IMPROVE_DEFAULT_EXIT_CHAMPION:
            print(f"  [champion] no executable plan for {name!r}; using "
                  f"{config.IMPROVE_DEFAULT_EXIT_CHAMPION}")
        name = config.IMPROVE_DEFAULT_EXIT_CHAMPION
        pol = POL.POLICIES.get(name) or {"ladder": list(config.TP_LADDER), "stop": config.HARD_STOP_PCT}
    return {"name": name,
            "ladder": [[float(m), float(f)] for m, f in (pol.get("ladder") or [])],
            "stop": pol.get("stop"), "trail": pol.get("trail"),
            "max_hold_s": pol.get("max_hold_s")}


def _fmt_hold(s: int) -> str:
    if s == 0:
        return "immediately"
    if s % 3600 == 0:
        return f"+{s // 3600} h"
    return f"+{s // 60} min"


def describe_plan(plan: dict, entry_price: float, size_usd: float | None = None,
                  promoted: dict | None = None) -> str:
    """One honest line for alerts / README. A pure time-exit champion renders as exactly what
    was measured ('exit ALL at +15 min · no hard stop · no TP ladder') — nothing is added."""
    size = size_usd if size_usd is not None else min(
        config.STACK_USD * config.POSITION_PCT, config.STACK_USD / config.MAX_CONCURRENT)
    name = plan.get("name", "?")
    if name == config.IMPROVE_DEFAULT_EXIT_CHAMPION and not (promoted or {}).get("promoted_ts"):
        tag = f"champion {name} — default, no policy has cleared the gate"
    elif promoted and promoted.get("promoted_ts"):
        import time
        when = time.strftime("%Y-%m-%d", time.gmtime(float(promoted["promoted_ts"])))
        tag = f"champion {name}, promoted {when} seq {promoted.get('promoted_at_alert_seq')}"
    else:
        tag = f"champion {name}"
    parts = [f"buy ~${size:.0f}"]
    if plan.get("stop"):
        parts.append(f"hard-stop ${entry_price * (1 - plan['stop']):.6g} (-{plan['stop'] * 100:.0f}%)")
    else:
        parts.append("no hard stop")
    if plan.get("trail"):
        parts.append(f"trail -{plan['trail'] * 100:.0f}% off the high-water mark")
    if plan.get("ladder"):
        parts.append("TP " + ", ".join(f"{m:g}x→sell {f * 100:.0f}%" for m, f in plan["ladder"]))
    else:
        parts.append("no TP ladder")
    if plan.get("max_hold_s") is not None:
        parts.append(f"exit ALL at {_fmt_hold(int(plan['max_hold_s']))}")
    return f"PLAN [{tag}]: " + " · ".join(parts)


def _cli(argv: list) -> int:
    import time
    if "--set" not in argv:
        st = state()
        print(json.dumps({k: v for k, v in st.items() if k != "history"}, indent=1, sort_keys=True))
        print(f"paused: {paused()}   history lines: {len(st['history'])}")
        print(describe_plan(exit_plan(), 1.0, promoted=st["exit"]))
        return 0
    i = argv.index("--set")
    try:
        arm, name = argv[i + 1].split("=", 1)
    except Exception:
        print("usage: champion.py --set exit=<policy>|entry_band=<band> --reason '...' [--publish]")
        return 2
    reason = argv[argv.index("--reason") + 1] if "--reason" in argv else ""
    if not reason:
        print("a --reason is required: this is the audit line for a manual change")
        return 2
    if arm not in ("exit", "entry_band"):
        print("arm must be exit or entry_band"); return 2
    if arm == "exit" and (name not in POL.POLICIES or name in POL.CONTROLS):
        print(f"{name!r} is not an executable registered policy"); return 2
    prev = state()[arm]["champion"]
    now = time.time()
    seq_key = "promoted_at_alert_seq" if arm == "exit" else "promoted_at_event_seq"
    new = write_state(**{arm: {"champion": name, "previous": prev, "promoted_ts": now, seq_key: None,
                               "evidence": {"manual": True, "reason": reason, "previous": prev},
                               "nominee": None, "demotion_judged": None}},
                      history_line={"ts": now, "arm": arm, "action": "manual_set", "from": prev,
                                    "to": name, "reason": reason})
    print(f"{arm}: {prev} → {name}  (manual; reason recorded)")
    if "--publish" in argv:
        from selfimprove import publish
        ok = publish.publish_files([config.CHAMPION_PATH], f"champion: manual {arm} → {name}")
        print("published to origin/main" if ok else "PUBLISH FAILED — run again or push by hand")
    return 0


if __name__ == "__main__":
    import tempfile
    # offline: defaults, a promotion write, exit_plan fallbacks, describe_plan wording
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "champion.json")
        st = state(p)
        assert st["exit"]["champion"] == config.IMPROVE_DEFAULT_EXIT_CHAMPION
        assert st["entry_band"]["champion"] == config.DEFAULT_ENTRY_BAND
        write_state(exit={"champion": "sell_15m", "promoted_ts": 1.7e9, "promoted_at_alert_seq": 388,
                          "previous": "cfg_ladder_stop"},
                    history_line={"ts": 1.7e9, "arm": "exit", "action": "promote"}, path=p)
        st2 = state(p)
        assert st2["exit"]["champion"] == "sell_15m" and st2["entry_band"]["champion"] == config.DEFAULT_ENTRY_BAND
        print("write_state ok; exit champion now", st2["exit"]["champion"])
        with open(p, "w") as f:
            f.write("{not json")
        assert state(p)["exit"]["champion"] == config.IMPROVE_DEFAULT_EXIT_CHAMPION, "corrupt file → defaults"
    print(describe_plan(exit_plan("cfg_ladder_stop"), 0.0012))
    print(describe_plan(exit_plan("sell_15m"), 0.0012, promoted={"promoted_ts": 1.7e9, "promoted_at_alert_seq": 388}))
    p15 = exit_plan("sell_15m")
    assert p15["ladder"] == [] and p15["stop"] is None and p15["max_hold_s"] == 900
    assert exit_plan("ctl_random_exit")["name"] == config.IMPROVE_DEFAULT_EXIT_CHAMPION
    assert exit_plan("no_such_policy")["name"] == config.IMPROVE_DEFAULT_EXIT_CHAMPION
    sys.exit(_cli(sys.argv[1:]) if len(sys.argv) > 1 else 0)
