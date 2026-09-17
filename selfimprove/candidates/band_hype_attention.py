"""
band_hype_attention — band_hype_early AND a floor on GMGN's viewer count. Written 2026-09-17,
NOT registered, and not registrable until its coverage has been MEASURED.

The logic is duplicated from band_hype_early rather than imported: a candidate module is a
standalone hypothesis the validator imports by path under `python3 -I -S`, and two bands that
share a body would silently move together when one of them is edited.
"""
from __future__ import annotations

# ── identity ──────────────────────────────────────────────────────────────────────
NAME = "band_hype_attention"
KIND = "band"

RATIONALE = (
    "HYPOTHESIS: band_hype_early, plus the requirement that GMGN's Trenches board shows the token "
    "has been LOOKED AT (visiting_count >= 3). MECHANISM: the proposed edge of an early entry is "
    "that other buyers arrive after you; the viewer count is the only direct observation of "
    "incoming attention this system has, everything else (volume, flow) being attention that has "
    "already been converted into trades. If attention leads flow, a viewer floor should select the "
    "same early, high-volume tokens band_hype_early selects while dropping those nobody is "
    "watching. COVERAGE IS THE PRECONDITION, NOT THE RESULT: visiting_count is only known for "
    "tokens GMGN's Trenches feed listed, and that feed applies a server-side launchpad allow-list "
    "which omits pons_v2 and bare V2/V3/V4 pools. On the one cached panel measured so far, "
    "visiting_count was non-zero on 20 of 60 Almost-bonded rows with a maximum of 5 (Migrated "
    "reaches 51), and NONE of the three runners of 2026-09-16 appears anywhere in this repo's GMGN "
    "cache (0 of 3). A band that is NA on most survivors cannot clear BAND_MIN_COVERAGE = 0.80 and "
    "would spend a trial for nothing, so this module is registered only once the measured coverage "
    "(latest_scan.json gmgn_coverage, the share of survivors with a known gmgn_visiting_count) "
    "supports it. The threshold 3 is chosen against that observed range, not fitted to any "
    "outcome. KILL CONDITION: the entry gate's verdict — no positive day-clustered selection lift "
    "over the same-day, same-age-bucket unselected pool net of cost against both controls — or "
    "coverage below BAND_MIN_COVERAGE, which retires it without a verdict at all; K5 kills it with "
    "the family. WHAT THIS IS NOT: not a result, and not a claim that anyone watched the runners: "
    "GMGN never saw them."
)
CONSUMED_DATA = ("docs/RETRO_2026-09-16_hype_runners.md", "docs/GMGN_TRENCHES.md", "data/ledger.csv",
                 "data/latest_scan.json", "selfimprove/candidates/band_hype_early.py")

# ── the fields the verdict must KNOW (a None in any of them is NA before verdict() runs) ──
# gmgn_visiting_count is REQUIRED on purpose: wherever GMGN did not see the token, this band is NA
# rather than False. An unknown is not evidence of absent attention.
REQUIRES = ("pair_age_min", "vol_h1", "liq_usd", "mcap", "buys_h1", "sells_h1", "gmgn_visiting_count")

# ── thresholds (module constants: a candidate may not edit config.py) ─────────────
MAX_AGE_MIN = 15.0
MIN_VOL_H1_USD = 50_000.0
MIN_LIQ_USD = 10_000.0
MAX_MCAP_USD = 2_000_000.0
MIN_BUY_SELL_RATIO = 1.0
MAX_TOP10_PCT = 20.0
MAX_ROUNDTRIP_PCT = 8.0
MIN_VISITING_COUNT = 3       # against an observed range of 0-5 on Almost-bonded, 0-51 on Migrated


def _top10(feat: dict):
    """(share, source): the EXACT Blockscout share when there is one, else GMGN's, else unknown."""
    if feat.get("holders_source") == "blockscout" and feat.get("top10_pct") is not None:
        return feat.get("top10_pct"), "blockscout"
    if feat.get("gmgn_top10_holder_pct") is not None:
        return feat.get("gmgn_top10_holder_pct"), "gmgn"
    return None, "unknown"


def _checks(feat: dict) -> dict:
    """Every clause as True / False / None — band_hype_early's clauses plus the viewer floor."""
    t10, _src = _top10(feat)
    rt = feat.get("roundtrip_loss_pct")
    return {
        "age": feat["pair_age_min"] <= MAX_AGE_MIN,
        "vol_h1": feat["vol_h1"] >= MIN_VOL_H1_USD,
        "liq": feat["liq_usd"] >= MIN_LIQ_USD,
        "mcap": feat["mcap"] <= MAX_MCAP_USD,
        "flow": feat["buys_h1"] >= MIN_BUY_SELL_RATIO * max(1.0, float(feat["sells_h1"])),
        "top10": None if t10 is None else t10 <= MAX_TOP10_PCT,
        "viewers": feat["gmgn_visiting_count"] >= MIN_VISITING_COUNT,
        "not_honeypot": feat.get("honeypot") is not True,
        "not_scam": feat.get("is_scam") is not True,
        "not_gmgn_honeypot": feat.get("gmgn_is_honeypot") is not True,
        "roundtrip": True if rt is None else rt <= MAX_ROUNDTRIP_PCT,
    }


def verdict(feat: dict):
    """True / False / None. Three-valued AND: a definite miss beats an unknown, and an unknown can
    never fire the band."""
    c = _checks(feat)
    if any(v is False for v in c.values()):
        return False
    if any(v is None for v in c.values()):
        return None
    return True


def explain(feat: dict) -> str:
    """Why the verdict was False or NA, for the B card's 'short of <band>' line."""
    c = _checks(feat)
    t10, src = _top10(feat)
    parts = []
    if c["age"] is False:
        parts.append("age above {:.0f}m (not early)".format(MAX_AGE_MIN))
    if c["vol_h1"] is False:
        parts.append("hour-1 volume below ${:,.0f} (the clone-swarm filter)".format(MIN_VOL_H1_USD))
    if c["liq"] is False:
        parts.append("liq below ${:,.0f}".format(MIN_LIQ_USD))
    if c["mcap"] is False:
        parts.append("mcap above ${:,.0f} (not early)".format(MAX_MCAP_USD))
    if c["flow"] is False:
        parts.append("sellers outnumber buyers in hour 1")
    if c["viewers"] is False:
        parts.append("fewer than {:d} gmgn viewers".format(MIN_VISITING_COUNT))
    if c["top10"] is False:
        parts.append("top-10 {:.1f}% above {:.0f}% (source: {})".format(float(t10), MAX_TOP10_PCT, src))
    elif c["top10"] is None:
        parts.append("unknown: top-10 share (no exact blockscout snapshot, no gmgn value)")
    if c["not_honeypot"] is False:
        parts.append("honeypot (router round trip)")
    if c["not_scam"] is False:
        parts.append("explorer is_scam")
    if c["not_gmgn_honeypot"] is False:
        parts.append("gmgn reports a honeypot")
    if c["roundtrip"] is False:
        parts.append("sell round trip above {:.0f}%".format(MAX_ROUNDTRIP_PCT))
    return "; ".join(parts)
