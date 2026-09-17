"""
Exit policies, and an honest simulator for them.

PORTED BYTE-FOR-BYTE from solana_screener/selfimprove/policies.py (2026-09-12). The family is kept
IDENTICAL on purpose: the same 16 names mean the same thing on both books, and on this chain the
family is genuinely pre-registered (no Robinhood rows were looked at when it was written).
Chain-specific variants enter only through the candidate registry as counted trials.
The one numeric change is round_trip_cost(), which adds the fixed L2 gas term solana lacks.

THE MEASURED PROBLEM THIS FILE EXISTS TO SOLVE. On the cloud ledger (903 rows, 2026-07-03 to
2026-08-12) the A-tier band's median return is -6% at 1h, **+34% at 6h**, then -99% at 24h and
-99% at 7d; the silent B control is -34% / -82% / -94% / -97%. So the screen's *selection* is
doing real work and the system has no exit, which converts a large early edge into a total loss
on every single row. `config.TP_LADDER` and `config.HARD_STOP_PCT` exist but only ever produced
*alerts*, never a simulated fill, and nothing has ever compared them against alternatives.

THE POLICY FAMILY IS PRE-DECLARED, and that is a statistical requirement rather than tidiness.
`~/entry_bot/CLAUDE.md` measures what happens otherwise: deflated Sharpe fell from 0.962 to 0.886
purely from counting the search honestly, and a greedy filter stack over ~70 uncorrected
comparisons produced a rule that died on the first robustness check. Every policy scored here is
in POLICIES below, the count is written into the proposal, and it accumulates across runs in
`selfimprove/trials.json` — because a loop that proposes changes is itself a trial generator, and
`signal_lab/registry.py` hardcodes `n_trials = 50` and so cannot see its own searching.

WITHIN-BAR ORDER IS UNKNOWABLE, so every policy is reported as a BRACKET. A bar records only
(open, high, low, close): if it touches both the stop and the take-profit, which came first is not
in the data. `pessimistic=True` resolves it against us (stop first), `False` for us. Report both;
never quote the optimistic number alone. This is the same convention as
`entry_bot/config.PATH_ASSUMPTIONS`, and it matters far more here than there because
`paths.py` may have to fall back to 1h bars for older alerts — an hour is long enough to contain
an entire pump and dump, so a coarse-resolution bracket can be almost uninformative. The
evaluator therefore stratifies on `res` and reports the mix.

COSTS ARE CHARGED, because a memecoin round trip is not free and an uncosted ladder is fiction.
`config.PAPER_SLIPPAGE_BPS` (100 = 1%) is applied per side, measured on a real ~$80k-liquidity
pump.fun pool in 2026-07. That is deliberately the same figure `paper_exec.py` uses, so a policy
that wins here and a paper fill that wins there mean the same thing.
"""
from __future__ import annotations

import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config   # noqa: E402


def round_trip_cost() -> float:
    """Cost charged across a full round trip (both sides): the proportional slippage assumption
    plus the FIXED gas term an L2 adds (config.PAPER_GAS_USD_PER_SWAP per swap, expressed as a
    fraction of the plan's position size). At $10 positions gas is ~1.4% of a round trip — a
    proportional-only model would miss it entirely."""
    position_usd = config.STACK_USD * config.POSITION_PCT
    return (2.0 * (config.PAPER_SLIPPAGE_BPS / 10_000.0)
            + 2.0 * config.PAPER_GAS_USD_PER_SWAP / position_usd)


# ── the adaptive exit: an ARMED trail, and a flow rule no bar can score ─────────────────────
# The exit the operator asked for on 2026-09-17: a hard -50% stop ALWAYS, plus a take-profit
# whose TIMING adapts to live flow — but only once the position has actually worked. Two keys:
#   trail_arm  the multiple of entry the HIGH-WATER MARK must reach before `trail` is live at
#              all. Without it a 30% trail sits at 0.70 x entry from the first tick, i.e. ABOVE
#              the 0.50 x stop, so the stop could never fire and "the stop is always live" would
#              be false by construction. Armed at 1.5x the trail's first possible level is
#              1.5 x 0.7 = 1.05 x entry, and the stop keeps the whole pre-arm regime.
#   flow       {buy_share_max, weak_ticks, vol_floor_frac, min_txns_m5} — a POST-ARM rule read
#              from the 5-minute Dexscreener window on every live tick. It has no bar equivalent
#              (an OHLCV bar carries no buy/sell split at any resolution), so `simulate` returns
#              NaN for any policy carrying it and ONLY the live paper book ever scores it.
FLOW_KEYS = ("buy_share_max", "weak_ticks", "vol_floor_frac", "min_txns_m5")
FLOW_FEATURES = ("vol_m5", "buys_m5", "sells_m5", "vol_h1", "buys_h1", "sells_h1", "liq_usd")


def _numf(v):
    """float(v), or None when it is missing / NaN / infinite / unreadable. Unknown stays unknown:
    a flow field coerced to 0.0 would read as a volume collapse and fire the rule on bad data."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def trail_active(pol: dict, peak_px: float, entry_px: float) -> bool:
    """Is this policy's trailing stop live yet? A trail with no `trail_arm` is live from entry
    (every pre-declared policy in POLICIES); an armed one only once the HIGH-WATER MARK has
    reached entry x trail_arm. The bar simulator, the live book and the cloud ledger leg all read
    the same predicate, so the three can never disagree about whether a trail is armed."""
    if pol.get("trail") is None:
        return False
    arm = pol.get("trail_arm")
    return arm is None or float(peak_px) >= float(entry_px) * float(arm)


def flow_policy_names() -> list:
    """The policies whose exit reads the 5-minute flow window. Empty until one is registered,
    which is what lets the live book skip the feature read entirely."""
    return [n for n, p in POLICIES.items() if isinstance(p.get("flow"), dict)]


def flow_from_market(m) -> dict | None:
    """FLOW_FEATURES copied out of ONE Dexscreener market dict, or None when the three fields the
    rule actually needs are not all present. None means DARK, never zero: a fabricated zero would
    read as a collapse and exit the position on an outage."""
    if not isinstance(m, dict):
        return None
    if any(m.get(k) is None for k in ("vol_m5", "buys_m5", "sells_m5")):
        return None
    return {k: m.get(k) for k in FLOW_FEATURES}


def flow_state_init(st: dict) -> None:
    """Seed the flow fields on a live-book policy state. setdefault, never assignment: a position
    already mid-flight must keep the streak and the volume peak it has accumulated."""
    st.setdefault("armed", False)
    st.setdefault("armed_ts", None)
    st.setdefault("peak_vol_m5", None)
    st.setdefault("weak_ticks", 0)
    st.setdefault("flow_ticks", 0)
    st.setdefault("flow_dark_ticks", 0)


def flow_step(st: dict, pol: dict, flow, px: float, entry_px: float, now_s: float,
              gap_s: float, max_gap_s: float) -> str | None:
    """One tick of the post-arm flow rule: 'weak_flow' | 'vol_dry' | None. Pure — every input is
    passed in, and `now_s` (the caller's single wall-clock capture) is only ever STORED as
    armed_ts, never compared against a clock read here.

    The rule is PRE-DECLARED (operator numbers, 2026-09-17; no Robinhood row was looked at):
      * BEFORE the arm — the high-water mark below entry x trail_arm — nothing happens, and both
        the weak streak and the volume peak are RESET. The pre-arm regime is stop-only by
        construction, and a streak accumulated at 1.1x must not carry into the post-arm rule.
      * A gap longer than max_gap_s resets the streak: "two CONSECUTIVE ticks" is a claim about
        two adjacent observations, and the book was not watching across the gap.
      * Dark features (the read was deferred, or the pair carries no m5 window) reset the streak
        and count a dark tick. A dark tick can never exit — deferred is never scored.
      * (ii) volume floor: 5-minute volume below vol_floor_frac x its POST-ARM peak ⇒ 'vol_dry'.
        The peak is ratcheted BEFORE the test, so on the arming tick peak == this tick's own
        volume and `v < frac * v` is impossible for frac in (0,1): the rule structurally cannot
        fire on the tick that arms it, whatever the position's earlier volume was.
      * (i) buy share: only when the window holds at least min_txns_m5 trades — below that the
        share is noise and the tick counts as neither weak nor strong, so it can neither start
        nor extend the streak. Share below buy_share_max ⇒ weak_ticks += 1, otherwise the streak
        resets; weak_ticks at the configured length ⇒ 'weak_flow'.
    """
    cfg = pol.get("flow") or {}
    arm = pol.get("trail_arm")
    peak_px = max(float(st.get("peak_px") or 0.0), float(px))   # == st["peak_px"] inside _step_policy
    if arm is not None and peak_px < float(entry_px) * float(arm):
        st["weak_ticks"] = 0
        st["peak_vol_m5"] = None
        return None
    if not st.get("armed"):
        st["armed"] = True
        st["armed_ts"] = now_s
    if float(gap_s) > float(max_gap_s):
        st["weak_ticks"] = 0
    vals = [_numf((flow or {}).get(k)) for k in ("vol_m5", "buys_m5", "sells_m5")]
    if any(v is None for v in vals):
        st["weak_ticks"] = 0
        st["flow_dark_ticks"] = int(st.get("flow_dark_ticks") or 0) + 1
        return None
    st["flow_ticks"] = int(st.get("flow_ticks") or 0) + 1
    vol, buys, sells = max(0.0, vals[0]), max(0.0, vals[1]), max(0.0, vals[2])
    peak_vol = st.get("peak_vol_m5")
    peak_vol = vol if peak_vol is None else max(float(peak_vol), vol)
    st["peak_vol_m5"] = peak_vol                                # ratcheted BEFORE the floor test
    if peak_vol > 0.0 and vol < float(cfg["vol_floor_frac"]) * peak_vol:
        return "vol_dry"
    n_txns = buys + sells
    if n_txns >= float(cfg["min_txns_m5"]) and n_txns > 0:
        if buys / n_txns < float(cfg["buy_share_max"]):
            st["weak_ticks"] = int(st.get("weak_ticks") or 0) + 1
            if st["weak_ticks"] >= int(cfg["weak_ticks"]):
                return "weak_flow"
        else:
            st["weak_ticks"] = 0
    return None


def simulate(entry: float, bars: list, policy: dict, pessimistic: bool = True) -> float:
    """Net return per $1 for one policy over one path. Never look-ahead.

    A policy is a dict of optional keys, all read as multiples/fractions OF THE ENTRY PRICE:
      ladder      [(multiple, fraction_of_original)] take-profit rungs, filled at the rung price
      stop        fraction below entry that closes the REMAINDER (0.5 = -50%)
      trail       fraction below the running high-water mark that closes the remainder
      trail_arm   multiple of entry the high-water mark must reach before `trail` is live at all
      flow        the live-flow take-profit — NOT back-testable; the whole path scores NaN
      max_hold_s  seconds after entry at which whatever remains is sold at that bar's close
    Anything unsold at the end of the path is marked at the final close — not at zero, and not at
    the peak. Marking at zero would flatter every exit rule by construction; marking at the peak
    is the look-ahead `entry_bot/labels.py` documents as worth a spurious +0.70.

    The rung price is used as the fill, which is legitimate here ONLY because a rung is a
    pre-committed limit level: we sell BECAUSE price reached it. Multiplying by an eventual peak
    we did not pre-commit to would be the leak.
    """
    if entry <= 0 or not bars:
        return float("nan")
    if policy.get("flow"):
        # A bar carries OHLCV only: no buy/sell split, no 5-minute window, so the flow leg cannot
        # be replayed on bars at any resolution. Returning a number here would be inventing one.
        # evaluate.score drops NaN rows, so a flow policy simply scores n = 0 in every bar
        # backtest and is judged by the live paper book alone.
        return float("nan")
    if policy.get("random_exit"):
        # Deterministic per path (seeded off the path itself, never the wall clock), so the
        # control is reproducible and cannot be re-rolled until it looks bad.
        import numpy as _np
        _r = _np.random.default_rng(config.SEED + int(abs(bars[0]["ts"])) % 1_000_003)
        b = bars[int(_r.integers(0, len(bars)))]
        return (b["c"] / entry) * (1.0 - round_trip_cost()) - 1.0
    ladder = list(policy.get("ladder") or [])
    stop_frac = policy.get("stop")
    trail_frac = policy.get("trail")
    max_hold = policy.get("max_hold_s")
    t0 = bars[0]["ts"]

    sold, proceeds, peak = 0.0, 0.0, entry

    def _exit_level(pk):
        """Protective exit price given a high-water mark: the HIGHER of stop and trail. An ARMED
        trail counts only once `pk` has reached entry x trail_arm — and `pk` is whatever the
        caller's within-bar reading anchors on, so the pessimistic reading arms off the PREVIOUS
        bar's peak exactly as it trails off it, never off a high that has not happened yet."""
        tp = pk * (1.0 - trail_frac) if trail_active(policy, pk, entry) else None
        cands = [p for p in (entry * (1.0 - stop_frac) if stop_frac is not None else None, tp)
                 if p is not None]
        return max(cands) if cands else None

    def _fill(px, b):
        """Achievable fill for a protective exit. If the bar OPENS below the level, price gapped
        through it and you get the open, not the level — assuming otherwise invents liquidity at
        a price that never traded."""
        return min(px, b["o"]) if b.get("o", 0) > 0 else px

    for b in bars:
        # WITHIN-BAR ORDERING. A bar gives O/H/L/C with no path, so the two readings must differ
        # in the ORDER the high and the low are applied — including what the trail is anchored on.
        # The original version ratcheted `peak` to this bar's high BEFORE testing the low in both
        # readings, which anchors the "pessimistic" trail on a high that has not happened yet.
        # Measured 2026-08-12: that let 27 of 639 rows score HIGHER pessimistically than
        # optimistically (worst gap +51.2 on one row) — definitionally impossible, and it silently
        # inflated every trail policy because both readings shared the optimistic anchor.
        if pessimistic:
            ex = _exit_level(peak)                 # anchored on the PREVIOUS high-water mark
            if ex is not None and b["l"] > 0 and b["l"] <= ex:
                proceeds += max(0.0, 1.0 - sold) * (_fill(ex, b) / entry)
                sold = 1.0
                break
            if b["h"] > peak:                      # ...only now does the high happen
                peak = b["h"]
            for lv, fr in [(lv, fr) for lv, fr in ladder if b["h"] >= entry * lv]:
                proceeds += fr * lv
                sold += fr
                ladder.remove((lv, fr))
        else:
            if b["h"] > peak:                      # high first: peak ratchets, rungs fill
                peak = b["h"]
            for lv, fr in [(lv, fr) for lv, fr in ladder if b["h"] >= entry * lv]:
                proceeds += fr * lv
                sold += fr
                ladder.remove((lv, fr))
            ex = _exit_level(peak)                 # ...then the low tests the RAISED trail
            if ex is not None and b["l"] > 0 and b["l"] <= ex:
                proceeds += max(0.0, 1.0 - sold) * (_fill(ex, b) / entry)
                sold = 1.0
                break
        if max_hold is not None and (b["ts"] - t0) >= max_hold:
            proceeds += max(0.0, 1.0 - sold) * (b["c"] / entry)
            sold = 1.0
            break
        if sold >= 0.9999:
            break

    if sold < 0.9999:                      # path ran out — mark the remainder at the last close
        proceeds += (1.0 - sold) * (bars[-1]["c"] / entry)
    return proceeds * (1.0 - round_trip_cost()) - 1.0


# ── NEGATIVE CONTROLS — permanently registered, and they must FAIL every gate ──
# Measured 2026-08-15: recentring the paired advantage matrix to exactly zero mean (keeping the
# real cross-policy correlation of 0.561, the day structure and the skew) and then taking the MAX
# over 14 policies of the day-clustered 2.5% lower bound crosses zero 33.3% of the time at 12
# alert-days and 20.0% at 41. A gate that fires a third of the time on noise is not a gate.
# These exist so that failure is VISIBLE rather than inferred: if any control clears the
# promotion gate, improve.py refuses to emit a proposal at all. A control that passes is the
# cheapest possible proof that the apparatus is measuring itself.
CONTROLS = {
    "ctl_exit_immediately": {"max_hold_s": 0},        # buy and sell at once: pure round-trip cost
    "ctl_random_exit":      {"random_exit": True},    # uniform random bar, rng seeded per mint
}

# ── the pre-declared family. Adding a row here is adding a TRIAL; say so in the proposal. ──
H = 3600
POLICIES: dict[str, dict] = {
    # what the system implicitly does today: alert and never exit
    "hold_to_end":            {},
    # pure time exits — the cheapest possible fix, and the +34%@6h number says look here first
    "sell_15m":               {"max_hold_s": 15 * 60},
    "sell_30m":               {"max_hold_s": 30 * 60},
    "sell_1h":                {"max_hold_s": 1 * H},
    "sell_2h":                {"max_hold_s": 2 * H},
    # 2026-09 audit: median time-to-peak among 2x winners is ~3.6h — the 2h..6h gap was
    # exactly where the lifecycle data points. One new trial (DSR deflates accordingly).
    "sell_3h":                {"max_hold_s": 3 * H},
    "sell_6h":                {"max_hold_s": 6 * H},
    # stop only
    "stop_30":                {"stop": 0.30},
    "stop_50":                {"stop": 0.50},
    # the shipped ladder, as-is and with the shipped stop
    "cfg_ladder":             {"ladder": list(config.TP_LADDER)},
    "cfg_ladder_stop":        {"ladder": list(config.TP_LADDER), "stop": config.HARD_STOP_PCT},
    # trailing stops — the natural answer to "big early move, total loss later"
    "trail_30":               {"trail": 0.30},
    "trail_50":               {"trail": 0.50},
    # hybrids: bank half early, trail the rest
    "tp2_half_trail30":       {"ladder": [(2.0, 0.50)], "trail": 0.30},
    "tp2_half_stop50_6h":     {"ladder": [(2.0, 0.50)], "stop": 0.50, "max_hold_s": 6 * H},
    "cfg_ladder_trail30_6h":  {"ladder": list(config.TP_LADDER), "trail": 0.30,
                               "max_hold_s": 6 * H},
}


# ── the candidate pool (selfimprove/candidates/registry.json, kind == "policy") ─────────────
_POLICY_KEYS = {"ladder", "stop", "trail", "trail_arm", "flow", "max_hold_s"}


def validate_policy(name: str, pol: dict) -> str | None:
    """Return a reason string when a candidate policy is malformed, else None. Shape rules: name
    ^[a-z][a-z0-9_]{2,40}$, not a built-in or control, keys ⊆ {ladder, stop, trail, trail_arm,
    flow, max_hold_s}, no random_exit (controls are apparatus, never candidates), ladder fractions
    in (0,1] summing <= 1 with strictly increasing multiples > 1, stop/trail in (0,1),
    max_hold_s int >= 0, trail_arm a number > 1 that requires a trail to arm, and flow a dict
    carrying EXACTLY FLOW_KEYS that requires trail_arm."""
    import re
    if not re.match(r"^[a-z][a-z0-9_]{2,40}$", str(name)):
        return "bad name"
    if name in POLICIES or name in CONTROLS:
        return "name collides with a built-in policy or control"
    if not isinstance(pol, dict) or not pol:
        return "policy must be a non-empty dict"
    if set(pol) - _POLICY_KEYS:
        return f"unknown keys {sorted(set(pol) - _POLICY_KEYS)}"
    lad = pol.get("ladder")
    if lad is not None:
        try:
            rungs = [(float(m), float(f)) for m, f in lad]
        except Exception:
            return "ladder must be [(multiple, fraction), ...]"
        if not rungs or any(f <= 0 or f > 1 for _m, f in rungs) or sum(f for _m, f in rungs) > 1.0 + 1e-9:
            return "ladder fractions must be in (0,1] and sum <= 1"
        mults = [m for m, _f in rungs]
        if mults[0] <= 1 or any(b <= a for a, b in zip(mults, mults[1:])):
            return "ladder multiples must be > 1 and strictly increasing"
    for k in ("stop", "trail"):
        v = pol.get(k)
        if v is not None and not (isinstance(v, (int, float)) and 0 < v < 1):
            return f"{k} must be in (0,1)"
    mh = pol.get("max_hold_s")
    if mh is not None and not (isinstance(mh, int) and mh >= 0):
        return "max_hold_s must be a non-negative int"
    arm = pol.get("trail_arm")
    if arm is not None:
        if not (isinstance(arm, (int, float)) and arm > 1):
            return "trail_arm must be a number > 1"
        if pol.get("trail") is None:
            return "trail_arm without trail has nothing to arm"
    fl = pol.get("flow")
    if fl is not None:
        if not isinstance(fl, dict) or set(fl) != set(FLOW_KEYS):
            return f"flow must be a dict with exactly the keys {sorted(FLOW_KEYS)}"
        for k in ("buy_share_max", "vol_floor_frac"):
            v = fl.get(k)
            if not (isinstance(v, (int, float)) and 0 < v < 1):
                return f"flow.{k} must be in (0,1)"
        if not (isinstance(fl.get("weak_ticks"), int) and fl["weak_ticks"] >= 1):
            return "flow.weak_ticks must be an int >= 1"
        if not (isinstance(fl.get("min_txns_m5"), int) and fl["min_txns_m5"] >= 0):
            return "flow.min_txns_m5 must be a non-negative int"
        if arm is None:
            # A flow rule from entry would be LAXER than the stop-only regime the operator asked
            # for before 1.5x: it could take a small loss on quiet flow that the -50% stop would
            # have ridden. The arm is what makes "stop always, adapt only once it works" true.
            return "flow requires trail_arm (a flow rule from entry is laxer than the pre-arm stop-only regime)"
    return None


def load_candidates(path: str | None = None) -> dict:
    """Validated {name: policy} from the registry (kind == 'policy', status != 'retired').
    A missing or unreadable registry yields {} with a printed note — never a crash: the
    runtime imports this module inside the alert path."""
    import json
    import os as _os
    path = path or config.REGISTRY_PATH
    if not _os.path.exists(path):
        return {}
    try:
        reg = json.load(open(path))
    except Exception as exc:
        print(f"  [policies] registry unreadable ({exc}); no candidates loaded")
        return {}
    out = {}
    for c in reg.get("candidates", []):
        if c.get("kind") != "policy" or c.get("status") == "retired":
            continue
        name, pol = c.get("name"), c.get("policy")
        why = validate_policy(name, pol)
        if why:
            print(f"  [policies] candidate {name!r} skipped: {why}")
            continue
        if pol.get("ladder") is not None:
            pol = dict(pol, ladder=[(float(m), float(f)) for m, f in pol["ladder"]])
        out[name] = pol
    return out


POLICIES.update(load_candidates())


if __name__ == "__main__":
    # A synthetic path, so the bracket and the no-look-ahead property are visible without network.
    def bar(ts, o, h, l, c):
        return {"ts": ts, "o": o, "h": h, "l": l, "c": c, "v": 1.0}

    print(f"round-trip cost charged: {round_trip_cost():.2%}\n")
    # doubles in the first 30 min, then dies to ~zero — the ledger's characteristic shape
    up_then_dead = [bar(0, 1.0, 1.6, 0.95, 1.5), bar(1800, 1.5, 2.4, 1.4, 2.2),
                    bar(3600, 2.2, 2.3, 0.4, 0.5), bar(7200, 0.5, 0.5, 0.01, 0.02)]
    print(f"  {'policy':<24s} {'pessimistic':>12s} {'optimistic':>12s}")
    for name, pol in POLICIES.items():
        p = simulate(1.0, up_then_dead, pol, pessimistic=True)
        o = simulate(1.0, up_then_dead, pol, pessimistic=False)
        print(f"  {name:<24s} {p:>+12.3f} {o:>+12.3f}")
    print("\n  sanity: hold_to_end must be ~total loss on this path, and any exit must beat it.")
    hold = simulate(1.0, up_then_dead, POLICIES["hold_to_end"])
    assert hold < -0.9, hold
    assert simulate(1.0, up_then_dead, POLICIES["sell_30m"]) > hold
    assert simulate(1.0, up_then_dead, POLICIES["trail_30"]) > hold
    # a rung must never be filled from a high the path never reached
    flat = [bar(0, 1.0, 1.01, 0.99, 1.0), bar(600, 1.0, 1.01, 0.99, 1.0)]
    assert abs(simulate(1.0, flat, {"ladder": [(10.0, 1.0)]}) - (-round_trip_cost())) < 1e-9

    # THE BRACKET MUST ACTUALLY DIVERGE. One bar that touches both the 2x rung and the -50% stop
    # is unresolvable, so pessimistic must come out strictly worse than optimistic. The fixture
    # above never produces such a bar, so without this the bracketing could be silently dead code
    # and every policy would be quoted at a single fake-precise number.
    amb = [bar(0, 1.0, 2.5, 0.4, 0.45)]
    pol = {"ladder": [(2.0, 0.50)], "stop": 0.50}
    pess = simulate(1.0, amb, pol, pessimistic=True)
    opt = simulate(1.0, amb, pol, pessimistic=False)
    assert pess < opt, f"bracket did not diverge: {pess} vs {opt}"
    print(f"  ambiguous bar (touches 2x rung AND -50% stop): "
          f"pessimistic {pess:+.3f} < optimistic {opt:+.3f}")
    # marking the unsold remainder must use the LAST CLOSE, never zero and never the peak
    rise = [bar(0, 1.0, 3.0, 1.0, 2.0)]
    assert abs(simulate(1.0, rise, {}) - (2.0 * (1 - round_trip_cost()) - 1.0)) < 1e-9, \
        "hold_to_end must mark at the final close"
    print("  OK — assertions hold.")
    # candidate validation: a malformed policy is refused, a valid one is accepted
    assert validate_policy("sell_45m", {"max_hold_s": 2700}) is None
    assert validate_policy("hold_to_end", {"max_hold_s": 1}) is not None
    assert validate_policy("bad", {"ladder": [(0.5, 0.5)]}) is not None
    assert validate_policy("ctl_x", {"random_exit": True}) is not None
    # the flow plumbing is inert until a policy carries a `flow` schema
    assert flow_policy_names() == [] and flow_from_market({"vol_m5": None, "buys_m5": 1, "sells_m5": 1}) is None
    assert set(flow_from_market({k: 1 for k in FLOW_FEATURES})) == set(FLOW_FEATURES)
    print("candidate validation ok; policies loaded:", len(POLICIES), "controls:", len(CONTROLS),
          "flow policies:", flow_policy_names())

    # ── the adaptive exit: the arm makes the stop dominant, the flow leg refuses to be scored ──
    ARM = {"stop": 0.50, "trail": 0.30, "trail_arm": 1.5}
    assert validate_policy("armed_x", ARM) is None
    assert validate_policy("armed_x", {"trail_arm": 1.5, "stop": 0.5}) is not None      # nothing to arm
    FLOW = {"buy_share_max": 0.45, "weak_ticks": 2, "vol_floor_frac": 0.20, "min_txns_m5": 5}
    assert validate_policy("flow_x", dict(ARM, flow=FLOW)) is None
    assert validate_policy("flow_x", {"trail": 0.3, "flow": FLOW}) is not None          # flow needs the arm
    assert validate_policy("flow_x", dict(ARM, flow=dict(FLOW, extra=1))) is not None   # exact key set
    assert not trail_active(ARM, 1.2, 1.0) and trail_active(ARM, 1.5, 1.0)
    # below the arm a 1.2x -> 0.45x path must exit at the -50% STOP, not at the 0.84 trail
    low = [bar(0, 1.0, 1.2, 1.0, 1.2), bar(60, 1.2, 1.2, 0.45, 0.45)]
    assert abs(simulate(1.0, low, ARM) - simulate(1.0, low, {"stop": 0.50})) < 1e-12
    assert simulate(1.0, low, ARM) < simulate(1.0, low, {"stop": 0.50, "trail": 0.30})
    high = [bar(0, 1.0, 1.6, 1.0, 1.6), bar(60, 1.6, 1.6, 1.1, 1.1)]
    assert abs(simulate(1.0, high, ARM) - simulate(1.0, high, {"stop": 0.50, "trail": 0.30})) < 1e-12
    print("  armed trail: below 1.5x the exit is the stop (%.3f); above it the trail fills at 1.12 (%.3f)"
          % (simulate(1.0, low, ARM), simulate(1.0, high, ARM)))
    assert simulate(1.0, high, dict(ARM, flow=FLOW)) != simulate(1.0, high, dict(ARM, flow=FLOW))  # NaN
    # one flow script: arm, one strong tick, two weak ticks -> weak_flow on the second
    stf = {"peak_px": 1.6, "closed": False}
    flow_state_init(stf)
    F = lambda b, s: {"vol_m5": 1000.0, "buys_m5": b, "sells_m5": s}        # noqa: E731
    reasons = [flow_step(stf, dict(ARM, flow=FLOW), f, 1.6, 1.0, 1_800_000_000.0 + 60 * i, 60.0, 180.0)
               for i, f in enumerate((F(9, 1), F(1, 9), F(1, 9)))]
    assert reasons == [None, None, "weak_flow"], reasons
    assert flow_step(stf, dict(ARM, flow=FLOW), None, 1.6, 1.0, 0.0, 60.0, 180.0) is None   # dark never exits
    print("  flow rule: %s  (dark ticks: %d, never an exit)" % (reasons, stf["flow_dark_ticks"]))
    print("  flow policies registered:", flow_policy_names(), "— bar-scored:", not flow_policy_names())
