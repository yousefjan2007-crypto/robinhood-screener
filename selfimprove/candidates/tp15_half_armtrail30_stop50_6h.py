"""
The PRICE-ONLY benchmark for the adaptive exit: bank half at 1.5x, hard -50% stop until the
high-water mark reaches 1.5x, then a 30% trail on the remainder, exit all at 6 h.

WRITTEN, NOT REGISTERED. Registering this module (register.py --scan) is a permanent counted
trial: the name enters selfimprove/trials.json forever and deflates every later Deflated-Sharpe
gate, including the entry lab's. That is the operator's move, scheduled no earlier than
2026-09-20, and it is deliberately not a side effect of writing the file.

WHY IT EXISTS. It is the control for `tp15_half_flowtrail_stop50_6h`, which is the same plan with
a live-flow take-profit bolted on. Without a price-only twin, any advantage the adaptive policy
shows would be confounded with the arm, the half-rung and the 6 h stop all at once; with it, the
paired comparison isolates exactly one thing — whether reading the 5-minute flow adds anything
over the identical price rules. That is also why the two modules share every number they can.
"""
from __future__ import annotations

# ── identity ──────────────────────────────────────────────────────────────────────
NAME = "tp15_half_armtrail30_stop50_6h"
KIND = "policy"

RATIONALE = (
    "HYPOTHESIS: on this chain the measured shape is a large early move followed by a total "
    "loss (A-tier median +34% at 6h, -99% at 24h), so the money is in banking part of the move "
    "early and protecting the rest without letting the protection pre-empt the -50% stop. "
    "MECHANISM: sell half at 1.5x as a pre-committed LIMIT — the first multiple the ledger's "
    "winners reliably touch — and keep a hard -50% stop on the whole position until the "
    "high-water mark reaches 1.5x, at which point a 30% trail takes over the remainder at "
    "1.05x entry or better. The arm is the point: an unarmed 30% trail sits at 0.70x entry from "
    "the first tick, above the stop, so the stop could never fire and the -50% floor the "
    "operator asked for would be decorative. Whatever survives is sold at 6 h, the horizon past "
    "which the ledger's forward returns are uniformly negative. "
    "ROLE: this is the price-only benchmark against which the flow variant is judged; it is a "
    "candidate in its own right, but its main job is to make that comparison clean. "
    "KILL CONDITION: retire it when the paired day-clustered lower bound against the exit "
    "champion is <= 0 after KILL_MIN_ALERT_DAYS alert-days of forward evidence, or when its own "
    "day-clustered lower bound net of policies.round_trip_cost() is <= the best negative "
    "control's. The honest prior is that this class of trade is -EV and that the correct "
    "outcome is NO CHANGE. "
    "NOT A REPEAT OF: the proven-dev look-ahead (this reads no creator history), the seasonality "
    "leak (no whole-sample statistic), or any in-sample threshold search — every number here is "
    "pre-declared and none was fitted to a Robinhood row."
)

# Every file read when forming the hypothesis, so the trial is auditable.
CONSUMED_DATA = ("data/ledger.csv", "data/livebook_summary.json", "data/proposals/",
                 "selfimprove/policies.py")

# Thresholds live here as module constants because a candidate may not edit config.py. They
# mirror config.IMPROVE_PROMOTE_MIN_CLUSTERS / MIN_BOOTSTRAP_CLUSTERS as of 2026-09-17; the gate
# reads config, not these — they are the written-down kill condition, not a second source of truth.
KILL_MIN_ALERT_DAYS = 40
KILL_MIN_CLUSTERS = 12

# ── policy shape (KIND == "policy") ──────────────────────────────────────────────
# Keys ⊆ {ladder, stop, trail, trail_arm, flow, max_hold_s}; the ladder is a list of lists so the
# dict round-trips through registry.json unchanged (policies.load_candidates retuples the rungs).
POLICY = {
    "ladder": [[1.5, 0.5]],     # bank half at 1.5x, filled AT the rung level (a limit, not a mark)
    "stop": 0.50,               # hard -50% from entry, live from the first tick and never removed
    "trail": 0.30,              # 30% off the high-water mark...
    "trail_arm": 1.5,           # ...but only once that mark has reached 1.5x entry
    "max_hold_s": 21600,        # 6 h
}
