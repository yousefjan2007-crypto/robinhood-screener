"""
Screening logic — PURE and deterministic. No network, no wall-clock, no RNG; identical
inputs always produce identical outputs (verify.py asserts this by walking the AST).

  • hard_gates(market, safety)  — reject a token outright on any positive rug finding.
  • soft_score(market, safety)  — rank the survivors 0-100 (ported verbatim from solana).
  • hc_checks(feat)             — THE one implementation of the A-tier ("band_a_strict")
                                  checks; a check is None when its input is unknown or came
                                  from a degraded source, never a silent False.
  • high_conviction(...)        — the human-readable misses derived from hc_checks.

Every threshold comes from config.py, so the entire screen re-tunes from one file.

THE PASS-THROUGH RULE (inherited from solana's GMGN contract): a hard gate fails closed only
on a POSITIVE finding from a source that answered (honeypot True, is_scam True, an owner that
is not renounced, LP known and below the floor, dev holding known and above the cap). An
absent or dark source passes through and is named in safety["sources_dark"]. The A-tier band
is stricter: it refuses to be A on unknown data (hc_checks → None → not A), because alerting on
unknown data during an outage is exactly how a B population gets contaminated with A-quality
tokens (measured on solana: 39 of 42 real alerts sat in the control arm).
"""
from __future__ import annotations

import config


def _known(x) -> bool:
    return x is not None


# ── hard gates ────────────────────────────────────────────────────────────────────
def hard_gates(market: dict, safety: dict) -> tuple[bool, dict]:
    """Return (passed, results). `results` records every individual check so the alert /
    ledger can show exactly WHY a token passed or was rejected. Keys whose value is None are
    informational (the solana-only gates with no analogue on this chain) and are never ANDed."""
    m, s = market or {}, safety or {}
    r: dict = {}
    r["has_market"] = bool(m) and _known(m.get("price_usd")) and (m.get("price_usd") or 0) > 0
    r["has_safety"] = bool(s)
    liq = m.get("liq_usd")
    vol = m.get("vol_h24")
    r["liq_ok"] = _known(liq) and liq >= config.LIQ_FLOOR_USD          # fails closed when unknown
    r["vol_ok"] = _known(vol) and vol >= config.MIN_VOL_H24_USD        # fails closed when unknown
    # chain truth (rpc) — positive findings only
    owner = s.get("owner_state")
    r["owner_ok"] = (not config.REQUIRE_OWNER_RENOUNCED) or owner != "owned"
    lp = s.get("lp_locked_pct")
    r["lp_ok"] = (not _known(lp)) or lp >= config.LP_LOCKED_MIN_PCT
    r["honeypot_ok"] = s.get("honeypot") is not True
    rt = s.get("roundtrip_loss_pct")
    r["sell_tax_ok"] = (not _known(rt)) or rt <= config.SELL_TAX_MAX_ROUNDTRIP_PCT
    # explorer flags
    r["not_scam"] = (not config.REJECT_IF_SCAM) or s.get("is_scam") is not True
    tmpl = s.get("template_name")
    r["template_ok"] = (not _known(tmpl)) or tmpl not in config.TEMPLATE_BLOCKLIST
    top10 = s.get("top10_pct")
    r["top10_ok"] = (not _known(top10)) or top10 <= config.TOP10_MAX_PCT
    dev = s.get("dev_pct")
    r["dev_ok"] = (not _known(dev)) or dev <= config.DEV_MAX_PCT
    prior = s.get("creator_prior_tokens")
    dead = s.get("creator_dead_frac")
    r["creator_ok"] = (not _known(prior)) or (
        prior <= config.CREATOR_MAX_PRIOR_TOKENS
        and not (prior >= 2 and _known(dead) and dead > config.CREATOR_MAX_DEAD_FRAC))
    snipes = s.get("sniper_swaps_first_blocks")
    r["sniper_ok"] = (not _known(snipes)) or snipes <= config.SNIPER_SWAPS_MAX
    # which sources answered (informational; the alert prints the dark ones)
    dark = set(s.get("sources_dark") or [])
    r["rpc_available"] = "rpc" not in dark
    r["blockscout_available"] = "blockscout" not in dark
    r["gt_available"] = "geckoterminal" not in dark
    # solana-only gates carried as None so the persisted gate dict stays comparable
    for k in ("mint_revoked", "freeze_revoked", "insider_ok", "insider_net_ok",
              "graph_insiders_ok", "risk_ok", "no_danger_risk"):
        r[k] = None

    passed = (
        r["has_market"] and r["has_safety"]
        and r["liq_ok"] and r["vol_ok"]
        and r["owner_ok"] and r["lp_ok"] and r["honeypot_ok"] and r["sell_tax_ok"]
        and r["not_scam"] and r["template_ok"] and r["top10_ok"] and r["dev_ok"]
        and r["creator_ok"] and r["sniper_ok"]
    )
    return bool(passed), r


_GATE_ORDER = ("has_market", "has_safety", "liq_ok", "vol_ok", "owner_ok", "lp_ok",
               "honeypot_ok", "sell_tax_ok", "not_scam", "template_ok", "top10_ok", "dev_ok",
               "creator_ok", "sniper_ok", "rpc_available", "blockscout_available",
               "gt_available")


def gates_bitmask(gates: dict) -> str:
    """Compact per-row record for the ledger: one char per gate in _GATE_ORDER
    ('1' pass, '0' fail, '-' informational/None). The full dict lives in latest_scan.json."""
    out = []
    for k in _GATE_ORDER:
        v = gates.get(k)
        out.append("-" if v is None else ("1" if v else "0"))
    return "".join(out)


# ── soft score (ported verbatim from solana) ──────────────────────────────────────
def _clip01(x: float) -> float:
    return 0.0 if x < 0 else (1.0 if x > 1 else x)


def _age_component(age_min) -> float:
    """Reward the age sweet-spot: penalize too-new (unreliable data) and old (decaying)."""
    if age_min is None or age_min != age_min:  # None / NaN
        return 0.3
    if age_min < config.AGE_MIN_MINUTES:
        return _clip01(age_min / config.AGE_MIN_MINUTES) * 0.5
    if age_min <= config.AGE_SWEET_MINUTES:
        return 1.0
    if age_min >= config.AGE_MAX_MINUTES:
        return 0.1
    span = config.AGE_MAX_MINUTES - config.AGE_SWEET_MINUTES
    return _clip01(1.0 - (age_min - config.AGE_SWEET_MINUTES) / span * 0.9)


def soft_score(market: dict, safety: dict) -> tuple[float, dict]:
    """Rank a survivor 0-100 from already-fetched fields (no extra API calls)."""
    m, s = market or {}, safety or {}
    mcap = m.get("mcap") or 0.0
    vol = m.get("vol_h24") or 0.0
    vmc = (vol / mcap) if mcap > 0 else 0.0
    c_vmc = _clip01(vmc / config.VMC_CAP)

    buys, sells = m.get("buys_h1") or 0, m.get("sells_h1") or 0
    tot = buys + sells
    c_bs = _clip01((buys / tot - 0.5) * 2.0) if tot > 0 else 0.0  # 0 at <=50% buys, 1 at 100%

    h = s.get("total_holders") or 0
    if h >= config.HOLDERS_SAFE:
        c_h = 1.0
    elif h <= config.HOLDERS_DANGER:
        c_h = 0.0
    else:
        c_h = (h - config.HOLDERS_DANGER) / (config.HOLDERS_SAFE - config.HOLDERS_DANGER)

    top10 = s.get("top10_pct")
    top10 = config.TOP10_MAX_PCT if top10 is None else top10
    c_conc = _clip01(1.0 - top10 / config.TOP10_MAX_PCT)

    c_age = _age_component(m.get("pair_age_min"))

    w = config.SOFT_WEIGHTS
    score = 100.0 * (w["vol_mcap"] * c_vmc + w["buy_sell"] * c_bs + w["holders"] * c_h
                     + w["concentration"] * c_conc + w["age"] * c_age)
    comps = {"vol_mcap": c_vmc, "buy_sell": c_bs, "holders": c_h,
             "concentration": c_conc, "age": c_age}
    return round(score, 1), comps


# ── the A-tier checks (band_a_strict) ─────────────────────────────────────────────
def hc_checks(feat: dict) -> dict:
    """Every A-tier check as {name: True | False | None} over the ONE flat feature dict
    (config.FEATURE_FIELDS). None means "unknown, or from a degraded source" — and an unknown
    can never be A. Applied only to tokens that already passed hard_gates.

    "A-tier" means best SURVIVAL odds — every quality metric in the safe band and every
    wash-trade rate organic-plausible — NOT predicted ROI. Insider presence predicts dumps,
    not pumps."""
    f = feat or {}
    dark = set(f.get("sources_dark") or [])
    c: dict = {}

    score = f.get("score")
    c["score"] = None if score is None else score >= config.HC_MIN_SCORE

    # exact holder facts only (Blockscout, contracts excluded); GT's snapshot can be 23 h stale
    holders_exact = f.get("holders_source") == "blockscout"
    top10 = f.get("top10_pct")
    c["top10"] = (top10 <= config.HC_TOP10_MAX_PCT) if (holders_exact and top10 is not None) else None
    holders = f.get("total_holders")
    c["holders"] = (holders >= config.HC_MIN_HOLDERS) if (holders_exact and holders is not None) else None

    liq = f.get("liq_usd")
    c["liq"] = None if liq is None else liq >= config.HC_MIN_LIQ_USD

    prior = f.get("creator_prior_tokens")
    c["creator_prior"] = None if prior is None else prior <= config.HC_CREATOR_MAX_PRIOR

    # $Cubrate post-mortem gates: metrics a bot farm inflates look IMPOSSIBLY GOOD on a young
    # coin; require organic-plausible rates.
    age = f.get("pair_age_min")
    c["age"] = None if age is None else age >= config.HC_MIN_AGE_MINUTES
    if holders_exact and holders is not None and age is not None and age > 0:
        c["holders_per_min"] = (holders / age) <= config.HC_MAX_HOLDERS_PER_MIN
    else:
        c["holders_per_min"] = None
    buys, sells = f.get("buys_h1"), f.get("sells_h1")
    if holders_exact and holders and buys is not None and sells is not None:
        c["tx_per_holder"] = ((buys + sells) / holders) <= config.HC_MAX_TX_PER_HOLDER_H1
    else:
        c["tx_per_holder"] = None

    rt = f.get("roundtrip_loss_pct")
    c["roundtrip"] = None if rt is None else rt <= config.HC_MAX_ROUNDTRIP_PCT

    if config.HC_REQUIRE_TEMPLATE_KNOWN:
        tmpl, verified = f.get("template_name"), f.get("verified_source")
        if "blockscout" in dark or (tmpl is None and verified is None):
            c["template"] = None
        else:
            c["template"] = (tmpl in config.TEMPLATE_WHITELIST) or (verified is True)
    else:
        c["template"] = True

    lp = f.get("lp_locked_pct")
    c["lp_known"] = None if (config.HC_REQUIRE_LP_KNOWN and lp is None) else True

    dev = f.get("dev_pct")
    c["dev_pct"] = None if dev is None else dev <= config.HC_MAX_DEV_PCT

    sniped = f.get("dev_sniped")
    c["not_sniped"] = None if sniped is None else (sniped is not True)
    return c


_MISS_TEXT = {
    "score": "score below {0}".format(config.HC_MIN_SCORE),
    "top10": "top10 above {0:.0f}% (or not exact)".format(config.HC_TOP10_MAX_PCT),
    "holders": "holders below {0} (or not exact)".format(config.HC_MIN_HOLDERS),
    "liq": "liq below ${0:,.0f}".format(config.HC_MIN_LIQ_USD),
    "creator_prior": "creator has more than {0} prior launch(es)".format(config.HC_CREATOR_MAX_PRIOR),
    "age": "age below {0}m (the <90m window is where rugs live)".format(config.HC_MIN_AGE_MINUTES),
    "holders_per_min": "holders/min above {0:.0f} (wallet-farm rate)".format(config.HC_MAX_HOLDERS_PER_MIN),
    "tx_per_holder": "tx/holder/hr above {0:.0f} (bot churn)".format(config.HC_MAX_TX_PER_HOLDER_H1),
    "roundtrip": "sell round-trip above {0:.0f}%".format(config.HC_MAX_ROUNDTRIP_PCT),
    "template": "contract template not whitelisted/verified",
    "lp_known": "LP status unknown",
    "dev_pct": "dev holds more than {0:.0f}%".format(config.HC_MAX_DEV_PCT),
    "not_sniped": "dev bought in the creation tx",
}


def high_conviction(feat: dict) -> tuple[bool, list[str]]:
    """(is_a_tier, misses). A miss that is unknown is reported as 'unknown: <check>' — an
    unknown is never A, but the alert/dashboard should say WHY it fell short."""
    checks = hc_checks(feat)
    misses = []
    for k, v in checks.items():
        if v is True:
            continue
        misses.append(("unknown: " if v is None else "") + _MISS_TEXT.get(k, k))
    return len(misses) == 0, misses


if __name__ == "__main__":
    # Fixture A — an obvious rug: owner not renounced, LP unburned, honeypot.
    rug_m = {"price_usd": 1e-6, "liq_usd": 500.0, "vol_h24": 100.0, "mcap": 20_000.0,
             "buys_h1": 0, "sells_h1": 5, "pair_age_min": 3.0}
    rug_s = {"owner_state": "owned", "lp_locked_pct": 0.0, "honeypot": True,
             "roundtrip_loss_pct": 60.0, "is_scam": False, "top10_pct": 85.0, "dev_pct": 40.0,
             "total_holders": 30, "sources_dark": []}
    ok, r = hard_gates(rug_m, rug_s)
    print(f"rug fixture passes hard gates? {ok}   (expect False)  failing: "
          f"{[k for k, v in r.items() if v is False]}")

    # Fixture B — a clean survivor with everything known and exact.
    clean_m = {"price_usd": 0.001, "liq_usd": 45_000.0, "vol_h24": 120_000.0, "mcap": 250_000.0,
               "fdv": 250_000.0, "buys_h1": 320, "sells_h1": 180, "pair_age_min": 180.0,
               "vol_h1": 9000.0, "vol_h6": 40000.0, "buys_h24": 3000, "sells_h24": 2500,
               "price_chg_h1": 4.0, "dex": "uniswap"}
    clean_s = {"owner_state": "renounced", "owner_renounced": True, "lp_locked_pct": 100.0,
               "lp_check_source": "rpc_v2", "honeypot": False, "roundtrip_loss_pct": 4.8,
               "is_scam": False, "template_name": "FlapTaxTokenV3", "is_proxy": True,
               "verified_source": True, "total_holders": 1400, "holders_source": "blockscout",
               "top10_pct": 18.0, "top10_pct_gt": 20.0, "lp_share_pct": 30.0, "dev_pct": 1.2,
               "deployer": "0xdev", "creator_prior_tokens": 0, "creator_dead_frac": 0.0,
               "creator_score": 80.0, "dev_sniped": False, "sniper_swaps_first_blocks": 4,
               "buys_per_buyer_m5": 1.1, "tx_per_holder_total": 5.0, "gt_score": 70.0,
               "gt_verified": True, "launchpad_graduation_pct": 100.0, "launchpad_completed": True,
               "launchpad_completed_age_s": 9000.0, "holders_updated_age_s": 600.0,
               "scanhood_verdict": "PASS", "scanhood_sellable": True, "sources_dark": []}
    ok2, _ = hard_gates(clean_m, clean_s)
    s, comps = soft_score(clean_m, clean_s)
    feat = dict(clean_m); feat.update(clean_s)
    feat.update({"score": s, "first_sighting": True, "sighting_age_s": 0.0})
    hc, misses = high_conviction(feat)
    print(f"clean fixture passes hard gates? {ok2}   (expect True)   score {s}/100")
    print("  components:", {k: round(v, 2) for k, v in comps.items()})
    print(f"clean fixture A-tier? {hc}   misses: {misses}")

    # Fixture C — the same token with Blockscout dark: hard gates pass through, A is UNKNOWN.
    dark_s = dict(clean_s, total_holders=1500, holders_source="gt", template_name=None,
                  verified_source=None, sources_dark=["blockscout"])
    ok3, r3 = hard_gates(clean_m, dark_s)
    feat3 = dict(feat); feat3.update(dark_s)
    checks3 = hc_checks(feat3)
    print(f"dark-blockscout fixture passes hard gates? {ok3}   (expect True)   "
          f"A checks None: {[k for k, v in checks3.items() if v is None]}   (expect holders/top10/"
          f"holders_per_min/tx_per_holder/template)")
    assert ok3 and checks3["holders"] is None and checks3["template"] is None

    # Fixture D — $Cubrate replay (literal at-alert numbers): must never be A-tier.
    cub_m = dict(clean_m, liq_usd=30_031.46, vol_h24=397_126.31, mcap=151_307.0,
                 buys_h1=3662, sells_h1=1987, pair_age_min=19.94)
    cub_s = dict(clean_s, top10_pct=4.4, total_holders=1344)
    cs, _ = soft_score(cub_m, cub_s)
    cf = dict(cub_m); cf.update(cub_s); cf.update({"score": cs, "first_sighting": True, "sighting_age_s": 0.0})
    cub_a, cub_misses = high_conviction(cf)
    print(f"Cubrate replay (score {cs}) A-tier? {cub_a}   (expect False)   misses: {cub_misses}")
    assert not cub_a and len(cub_misses) >= 2
    print("gates bitmask (clean):", gates_bitmask(hard_gates(clean_m, clean_s)[1]))
