"""
band_hype_early — an entry hypothesis, written 2026-09-17, NOT registered.

Registering a band is a counted trial (selfimprove/trials.json only grows and deflates every
later Deflated-Sharpe gate), so this file ships validated and unregistered; registration is the
operator's step. Nothing here is evidence, and nothing here may be read as a result.

A candidate may not edit config.py, so every threshold below is a MODULE CONSTANT. They are the
champion band's own numbers with one clause changed, deliberately: the point of the experiment is
that one clause, and a band that moved five thresholds at once would tell us nothing about any of
them.
"""
from __future__ import annotations

# ── identity ──────────────────────────────────────────────────────────────────────
NAME = "band_hype_early"
KIND = "band"

RATIONALE = (
    "HYPOTHESIS: the coins worth entering on this chain are EARLY (pool age <= 15 min, mcap <= "
    "$2M), already trading heavily (hour-1 volume >= $50k on >= $10k of liquidity), not "
    "seller-dominated, not concentrated (top-10 <= 20 %) and carrying no POSITIVE rug finding. "
    "MECHANISM: the champion band band_volume_early asks for buys >= 2x sells, and that clause is "
    "exactly what rejected FOMOPAD — the one runner of 2026-09-16 the screener actually sighted "
    "(at pair_age_min 4.07 through the gt_new_pools feed, ledgered as event_seq 621, tier B, "
    "champion_reason 'buys below 2x sells in hour 1': 1,273 buys against 1,137 sells = 1.12x). "
    "The proposed mechanism is that in the first minutes of a token that is moving, the flow is "
    "two-sided by construction — snipers are already selling into the same candles that are "
    "lifting it — so a 2x buy dominance selects calm launches rather than hot ones, while the "
    "hour-1 VOLUME floor is what actually separates a runner from the noise. This band therefore "
    "keeps every other clause of band_volume_early, tightens age 30 -> 15 min, adds a "
    "concentration ceiling and a no-positive-rug-finding clause, and relaxes the flow clause to "
    "buys >= sells. WHY THE VOLUME FLOOR STAYS: the clone swarms are the reason. The same three "
    "names carry 13, 6 and 11 distinct same-name tokens at roughly $30k liquidity and $25-38k "
    "LIFETIME volume with buys/sells 1.4-1.5x; they clear every hard gate (LIQ_FLOOR_USD $10k, "
    "MIN_VOL_H24_USD $20k) and they would sail through a relaxed flow clause. What they never do "
    "is $50k of volume in hour one at an age <= 15 min, so at this age the volume floor IS the "
    "clone filter. TOP-10 SOURCE: the exact Blockscout share when there is one, else GMGN's, else "
    "NA — never GeckoTerminal's, which read 39.2 % on FOMOPAD 22 h after Blockscout read 14.9 % "
    "at sighting; a concentration number is a property of the source and the instant. "
    "KILL CONDITION: the entry gate's own verdict — no positive day-clustered selection lift over "
    "the same-day, same-age-bucket unselected pool (net of cost, at the BY-corrected level, "
    "against ctl_random_band and ctl_inverse_band), or K5: BAND_KILL_AFTER_DAYS with no band above "
    "zero means 'no entry signal' and this one dies with the family. "
    "WHAT THIS IS NOT: not a result. The three runners of 2026-09-16 were chosen BECAUSE they ran, "
    "n = 3 on one afternoon is one day-cluster against a floor of MIN_BOOTSTRAP_CLUSTERS = 12, and "
    "the retrospective that describes them refuses to state a hit rate, a lift or a threshold. "
    "The honest prior is still that this class of trade is -EV and that this band fails."
)
# Every file read while forming the hypothesis (so the trial is auditable).
CONSUMED_DATA = ("docs/RETRO_2026-09-16_hype_runners.md", "data/ledger.csv", "data/latest_scan.json",
                 "selfimprove/entry_lab/bands.py (band_volume_early)")

# ── the fields the verdict must KNOW (a None in any of them is NA before verdict() runs) ──
REQUIRES = ("pair_age_min", "vol_h1", "liq_usd", "mcap", "buys_h1", "sells_h1")

# ── thresholds (module constants: a candidate may not edit config.py) ─────────────
MAX_AGE_MIN = 15.0           # band_volume_early's 30 min, halved: the three runners peaked +8/+17/+29 min
MIN_VOL_H1_USD = 50_000.0    # BAND_VE_MIN_VOL_H1_USD, unchanged — the clone filter
MIN_LIQ_USD = 10_000.0       # BAND_VE_MIN_LIQ_USD, unchanged
MAX_MCAP_USD = 2_000_000.0   # BAND_VE_MAX_MCAP_USD, unchanged
MIN_BUY_SELL_RATIO = 1.0     # BAND_VE_BUY_SELL_RATIO 2.0 -> 1.0: THE clause under test
MAX_TOP10_PCT = 20.0         # the A-band's own concentration ceiling (HC_TOP10_MAX_PCT)
MAX_ROUNDTRIP_PCT = 8.0      # HC_MAX_ROUNDTRIP_PCT: a known round trip worse than this is a finding


def _top10(feat: dict):
    """(share, source): the EXACT Blockscout share when there is one, else GMGN's Trenches value,
    else (None, 'unknown'). GeckoTerminal's is deliberately not consulted — it is a later snapshot
    of a different instant (39.2 % vs Blockscout's 14.9 % on the same token, 22 h apart)."""
    if feat.get("holders_source") == "blockscout" and feat.get("top10_pct") is not None:
        return feat.get("top10_pct"), "blockscout"
    if feat.get("gmgn_top10_holder_pct") is not None:
        return feat.get("gmgn_top10_holder_pct"), "gmgn"
    return None, "unknown"


def _checks(feat: dict) -> dict:
    """Every clause as True / False / None (None = the input is unknown). A rug clause is False
    only on a POSITIVE finding: an unknown honeypot or a missing round trip is not a finding."""
    t10, _src = _top10(feat)
    rt = feat.get("roundtrip_loss_pct")
    return {
        "age": feat["pair_age_min"] <= MAX_AGE_MIN,
        "vol_h1": feat["vol_h1"] >= MIN_VOL_H1_USD,
        "liq": feat["liq_usd"] >= MIN_LIQ_USD,
        "mcap": feat["mcap"] <= MAX_MCAP_USD,
        "flow": feat["buys_h1"] >= MIN_BUY_SELL_RATIO * max(1.0, float(feat["sells_h1"])),
        "top10": None if t10 is None else t10 <= MAX_TOP10_PCT,
        "not_honeypot": feat.get("honeypot") is not True,
        "not_scam": feat.get("is_scam") is not True,
        "not_gmgn_honeypot": feat.get("gmgn_is_honeypot") is not True,
        "roundtrip": True if rt is None else rt <= MAX_ROUNDTRIP_PCT,
    }


def verdict(feat: dict):
    """True / False / None. Three-valued AND: a definite miss beats an unknown, and an unknown can
    never fire the band (an unknown top-10 share is NA, never a pass)."""
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
