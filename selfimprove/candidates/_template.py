"""
TEMPLATE for a research candidate — copy to selfimprove/candidates/<NAME>.py and edit.
The file name MUST equal NAME. Registering it (register.py --scan) is a COUNTED TRIAL: the name
goes into selfimprove/trials.json forever and deflates every later Deflated-Sharpe gate, so a
candidate is a hypothesis you are willing to pay for, not a tweak.

A candidate module is ONE of two kinds:

  KIND = "band"    — an entry hypothesis: verdict(feat) -> True | False | None over the flat
                     feature dict (config.FEATURE_FIELDS; None when unknown). None = NA.
  KIND = "policy"  — an exit hypothesis: POLICY = {ladder?, stop?, trail?, trail_arm?, flow?,
                     max_hold_s?} (selfimprove/policies.py:_POLICY_KEYS is the authority and
                     validate_policy is the shape check).

FORBIDDEN (bands.static_ok rejects the module and it is never imported):
  * any import whose top-level module is not in
        math, json, config, clf_runtime, selfimprove, typing, __future__, hashlib
    (so: no os, sys, time, datetime, random, numpy, pandas, sklearn, urllib, requests, ...)
    `clf_runtime` is RESERVED for a later plan and does not exist in this repo yet: importing
    it passes the static check and is then refused by register.py's `python3 -I -S` validator,
    so it fails closed — but do not write one.
  * any attribute chain touching os.environ, time, datetime, random, numpy.random,
    subprocess, urllib, http_client, requests, socket, pathlib
  * any call to open(), __import__(), exec(), eval()
  * (by policy, not by the checker) any wall-clock, RNG or network — a band must be a pure
    function of feat; sighting_age_s is the only clock it may read.

verdict() is wrapped by the runtime: an exception is NA, never an outage. The NA rule is
applied BEFORE your body: if any REQUIRES field is None the verdict is None without calling
verdict(). Thresholds belong in config.py — but a candidate may not edit config.py, so hold
your threshold as a module constant and say so in RATIONALE.
"""
from __future__ import annotations

import config

# ── identity ──────────────────────────────────────────────────────────────────────
NAME = "band_template_example"          # == this file's basename; ^[a-z][a-z0-9_]{2,40}$
KIND = "band"                           # "band" | "policy"

RATIONALE = (
    "One paragraph: the hypothesis as a hypothesis, the mechanism you think produces the "
    "edge, the kill condition (what result would make you retire it), and which dead-end "
    "items from research_prompt.md it does NOT repeat. This example fires on survivors whose "
    "liquidity is at least HC_MIN_LIQ_USD and whose sell round-trip is under the HC cap."
)
# Every file / report you read when forming the hypothesis (so the trial is auditable).
CONSUMED_DATA = ("data/ledger.csv", "data/proposals/", "selfimprove/entry_lab/reports/")

# ── band shape (KIND == "band") ──────────────────────────────────────────────────
# Fields the body reads. Must be a subset of config.FEATURE_FIELDS and non-empty. If any of
# them is None the runtime returns NA without calling verdict().
REQUIRES = ("liq_usd", "roundtrip_loss_pct")


def verdict(feat: dict):
    """True / False / None. Pure: same feat -> same verdict, always."""
    return (feat["liq_usd"] >= config.HC_MIN_LIQ_USD
            and feat["roundtrip_loss_pct"] <= config.HC_MAX_ROUNDTRIP_PCT)


def explain(feat: dict) -> str:
    """Why the verdict was False (shown on B cards as 'short of <band>: ...'). Optional."""
    parts = []
    if feat["liq_usd"] < config.HC_MIN_LIQ_USD:
        parts.append("liq below ${:,.0f}".format(config.HC_MIN_LIQ_USD))
    if feat["roundtrip_loss_pct"] > config.HC_MAX_ROUNDTRIP_PCT:
        parts.append("round trip above {:.0f}%".format(config.HC_MAX_ROUNDTRIP_PCT))
    return "; ".join(parts)


# ── policy shape (KIND == "policy") — replace the band block above with this ─────
# NAME = "sell_45m"
# KIND = "policy"
# RATIONALE = "..."
# CONSUMED_DATA = ("data/livebook_summary.json", "data/proposals/")
# POLICY = {"max_hold_s": 45 * 60}            # keys ⊆ {ladder?, stop?, trail?, trail_arm?,
#                                             #          flow?, max_hold_s?}
#                                             # ladder = [[multiple, fraction], ...]
#
# trail_arm: a number > 1 — the multiple of entry the HIGH-WATER MARK must reach before `trail`
#   is live at all. It requires `trail` (there is nothing to arm without one). Below the arm the
#   position runs on `stop` alone, which is the point: an unarmed 30% trail sits above a -50%
#   stop from entry and the stop could never fire.
# flow: a dict with EXACTLY policies.FLOW_KEYS — {"buy_share_max": 0<x<1, "weak_ticks": int >= 1,
#   "vol_floor_frac": 0<x<1, "min_txns_m5": int >= 0} — the post-arm 5-minute flow rule. It
#   requires `trail_arm` (a flow rule from entry is LAXER than the pre-arm stop-only regime).
#   LIVE BOOK ONLY: policies.simulate has no 5-minute feed and returns NaN for a flow policy, so
#   a flow candidate is scored by the live book and the paper gate, never by the bar simulator.
