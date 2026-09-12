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


def simulate(entry: float, bars: list, policy: dict, pessimistic: bool = True) -> float:
    """Net return per $1 for one policy over one path. Never look-ahead.

    A policy is a dict of optional keys, all read as multiples/fractions OF THE ENTRY PRICE:
      ladder      [(multiple, fraction_of_original)] take-profit rungs, filled at the rung price
      stop        fraction below entry that closes the REMAINDER (0.5 = -50%)
      trail       fraction below the running high-water mark that closes the remainder
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
        """Protective exit price given a high-water mark: the HIGHER of stop and trail."""
        tp = pk * (1.0 - trail_frac) if trail_frac is not None else None
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
_POLICY_KEYS = {"ladder", "stop", "trail", "max_hold_s"}


def validate_policy(name: str, pol: dict) -> str | None:
    """Return a reason string when a candidate policy is malformed, else None. Shape rules: name
    ^[a-z][a-z0-9_]{2,40}$, not a built-in or control, keys ⊆ {ladder, stop, trail, max_hold_s},
    no random_exit (controls are apparatus, never candidates), ladder fractions in (0,1] summing
    <= 1 with strictly increasing multiples > 1, stop/trail in (0,1), max_hold_s int >= 0."""
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
    print("candidate validation ok; policies loaded:", len(POLICIES), "controls:", len(CONTROLS))
