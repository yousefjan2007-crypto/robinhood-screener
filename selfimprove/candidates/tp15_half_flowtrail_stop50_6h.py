"""
The ADAPTIVE exit: `tp15_half_armtrail30_stop50_6h` plus a take-profit whose timing reads live
5-minute flow once the position has reached 1.5x.

WRITTEN, NOT REGISTERED. Registering it (register.py --scan) is a permanent counted trial that
deflates every later Deflated-Sharpe gate; that is the operator's move, no earlier than
2026-09-20, and deliberately not a side effect of writing the file.

NOT BACK-TESTABLE, AND THAT IS STATED IN THE CODE. An OHLCV bar carries no buy/sell split and no
5-minute window, so `policies.simulate` returns NaN for any policy carrying a `flow` block and
`evaluate.score` drops the row: this candidate scores n = 0 in every bar backtest, for ever. Its
only evidence is the live paper book, paired tick-for-tick against the price-only benchmark above,
which is the honest position — a number invented from bars would be worse than no number.
"""
from __future__ import annotations

# ── identity ──────────────────────────────────────────────────────────────────────
NAME = "tp15_half_flowtrail_stop50_6h"
KIND = "policy"

RATIONALE = (
    "HYPOTHESIS: on a memecoin the give-back the ledger measures between +34% at 6h and -99% at "
    "24h is not a price event first — it is a FLOW event. Sellers take over the 5-minute window "
    "and volume collapses before the price finishes falling, so a rule that reads flow can leave "
    "earlier than a 30% trail, which by construction cannot act until 30% of the move is already "
    "gone. "
    "MECHANISM: identical price rules to tp15_half_armtrail30_stop50_6h (half at 1.5x, hard -50% "
    "stop always, 30% trail armed at 1.5x, 6 h time stop) plus, AFTER the arm only, two exits: "
    "(i) the 5-minute buy share (buys / (buys + sells)) below 0.45 for 2 consecutive 60 s ticks — "
    "seller dominance sustained across two adjacent observations, not one noisy print; (ii) "
    "5-minute volume below 0.20 x its post-arm peak — an 80% collapse of the flow that was "
    "carrying the move. The share rule needs at least 5 trades in the window; below that the "
    "share is noise and the tick counts as neither weak nor strong. Nothing reads flow before "
    "1.5x: below the arm the regime is stop-only, exactly as the benchmark. "
    "THREE-VALUED SEMANTICS: a dark feature tick (the read was deferred, or the pair carries no "
    "m5 window) can NEVER trigger an exit — it resets the streak and is counted in "
    "flow_dark_ticks. With the features dark throughout, this policy is the benchmark tick for "
    "tick, byte-identical fills. Deferred is never scored, here as everywhere. "
    "NOT BACK-TESTABLE: policies.simulate returns NaN for it and evaluate.score gives n = 0; the "
    "live paper book is the only evidence, and the comparison that matters is the paired one "
    "against the benchmark, which differs from it in exactly this rule and nothing else. "
    "KILL CONDITION: retire it when the paired day-clustered lower bound against "
    "tp15_half_armtrail30_stop50_6h is <= 0 over the forward window (KILL_MIN_ALERT_DAYS "
    "alert-days, at least KILL_MIN_CLUSTERS clusters — a bound from fewer clusters is not a "
    "bound), or when n_flow_dark exceeds KILL_MAX_FLOW_DARK_SHARE of its closes, at which point "
    "the rule is mostly not running and any difference measured is an artefact of the feature "
    "feed rather than of the hypothesis. "
    "NOT A REPEAT OF: the FOMO/Bull500 quote artefacts (the book's integrity gate still applies "
    "to every tick this rule reads), the proven-dev look-ahead (no creator history), or any "
    "in-sample threshold search — 0.45, 2, 0.20 and 5 are the operator's pre-declared numbers, "
    "fitted to no Robinhood row."
)

CONSUMED_DATA = ("data/ledger.csv", "data/livebook_summary.json", "data/proposals/",
                 "selfimprove/policies.py", "selfimprove/livebook.py")

# Kill-condition thresholds as module constants: a candidate may not edit config.py. The first
# two mirror config.IMPROVE_PROMOTE_MIN_CLUSTERS / MIN_BOOTSTRAP_CLUSTERS as of 2026-09-17; the
# gate reads config, not these. The third has no config twin — it is this candidate's own.
KILL_MIN_ALERT_DAYS = 40
KILL_MIN_CLUSTERS = 12
KILL_MAX_FLOW_DARK_SHARE = 0.50

# ── policy shape (KIND == "policy") ──────────────────────────────────────────────
# The price legs are byte-identical to tp15_half_armtrail30_stop50_6h on purpose: the paired
# comparison must isolate the flow rule and nothing else.
POLICY = {
    "ladder": [[1.5, 0.5]],     # bank half at 1.5x, filled AT the rung level (a limit, not a mark)
    "stop": 0.50,               # hard -50% from entry, live from the first tick and never removed
    "trail": 0.30,              # 30% off the high-water mark...
    "trail_arm": 1.5,           # ...but only once that mark has reached 1.5x entry
    "max_hold_s": 21600,        # 6 h
    "flow": {                   # post-arm only; livebook-scored, never back-tested
        "buy_share_max": 0.45,  # 5-min buy share below this is a weak tick
        "weak_ticks": 2,        # ...and two CONSECUTIVE weak ticks exit (a gap breaks the streak)
        "vol_floor_frac": 0.20, # 5-min volume below 20% of its post-arm peak exits
        "min_txns_m5": 5,       # fewer trades than this in the window: not enough flow to judge
    },
}
