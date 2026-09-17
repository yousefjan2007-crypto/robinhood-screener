"""
Entry bands — the pre-declared family of A-tier hypotheses, the two negative controls, the
candidate registry loader and the static source check that guards it.

WHAT. A band is a pure function verdict(feat) -> True | False | None over the ONE flat feature
dict (config.FEATURE_FIELDS; None when unknown). None means NA: a REQUIRES field is unknown, or
the band raised. The champion band (selfimprove.champion.entry_band(), default band_a_strict)
alone sets tier A and alerts; every registered band's verdict is recorded per ledger event in
the sidecar (entry_lab/store.py) so bands are compared on IDENTICAL forward returns.

WHY A FAMILY, WHY CONTROLS. On solana the strict band was the best of 14 bands tested and every
relaxation was worse (n=16 A rows — hold it loosely). Each band registered here is a counted
trial (selfimprove/trials.json only grows) and deflates every later Deflated-Sharpe gate. The
controls exist so that failure is VISIBLE: ctl_random_band fires AT EVALUATION for a sha256-chosen
10% of tokens, and ctl_inverse_band is the champion's complement. If a control clears the gate, the
apparatus is measuring itself and the run is void. (It once also waited a per-token deterministic
delay spread over the 24 h watch window, to mimic a maturation band's firing instants. That made it
inert: run.py records first-sighting verdicts at sighting_age_s = 0 and controls may not open a
band_fire row, so the delayed control returned 0 on 672 of 672 committed verdict rows. Changed
2026-09-17 — config.BAND_CTL_RANDOM_CHANGED_ON; verdicts recorded before that date carry no
evidence about it, and the scorecard says so rather than the history being rewritten.)

WHY THE STATIC CHECK. A research candidate is Python written by a headless model on a branch.
static_ok() walks the AST: only an import allowlist, no wall-clock / RNG / network / subprocess /
environment attribute chains, no open()/exec()/eval()/__import__(). Identifiers inside strings
and comments are never matched (a band named ctl_random_band does not trip 'random'). A module
that fails is skipped with a printed warning and its verdicts are NA; a module absent from the
registry is never imported at all. Every verdict call is wrapped: exception -> NA. A candidate
bug can never break alerting.

No wall-clock here: sighting_age_s is computed by run.py from its single time.time().
"""
from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import os
import sys
from dataclasses import dataclass, field
from typing import Callable, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import config   # noqa: E402
import screen   # noqa: E402

BUILTIN_MODULE = "entry_lab.bands"
CANDIDATE_MODULE_PREFIX = "candidates."
ALLOWED_IMPORTS = {"math", "json", "config", "clf_runtime", "selfimprove", "typing",
                   "__future__", "hashlib"}
FORBIDDEN_ATTR_SEGMENTS = {"time", "datetime", "random", "subprocess", "urllib", "http_client",
                           "requests", "socket", "pathlib"}
FORBIDDEN_CALLS = {"open", "__import__", "exec", "eval"}
ACTIVE_STATUSES = ("champion", "candidate", "control")


# ── the band contract ──────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class BandSpec:
    """One entry band. verdict() applies the NA rule (any REQUIRES field None -> None), then
    calls the body inside try/except (exception -> None) and coerces the result to a bool.
    explain() says why the verdict was False or NA, for the B-card 'short of <champion>' line."""
    NAME: str
    RATIONALE: str
    REQUIRES: tuple
    CONSUMED_DATA: tuple
    KIND: str                                   # 'candidate' | 'control'
    body: Callable = field(repr=False, compare=False)
    explainer: Optional[Callable] = field(default=None, repr=False, compare=False)
    # Three-valued bands (the built-in hc family) run their body even when a REQUIRES field is
    # None: screen.hc_checks already yields None per unknown check, and _all_or_none is a Kleene
    # AND, so a definite miss (age 60 min) beside an unknown (LP status on a V3 pool) is False,
    # not NA. Found on the first cloud run: 4/4 survivors were "NA" although every one failed
    # the age check outright. Candidates keep the simple contract (any REQUIRES None -> None).
    # In both cases an unknown can never make a band FIRE (True with a missing field -> None).
    THREE_VALUED: bool = False

    def missing(self, feat: dict) -> list:
        f = feat or {}
        return [k for k in self.REQUIRES if f.get(k) is None]

    def verdict(self, feat: dict):
        f = feat or {}
        miss = self.missing(f)
        if miss and not self.THREE_VALUED:
            return None
        try:
            r = self.body(f)
        except Exception:
            return None
        if r is None:
            return None
        if r is not True and r is not False:
            try:
                r = bool(r)
            except Exception:
                return None
        if r and miss:
            return None                    # an unknown input can never fire a band
        return r

    def explain(self, feat: dict) -> str:
        f = feat or {}
        miss = self.missing(f)
        # a three-valued band with a definite miss explains the miss (the explainer lists it
        # beside the unknowns); only a verdict of None is described as NA
        if miss and (not self.THREE_VALUED or self.verdict(f) is None):
            return "NA: " + ", ".join(miss) + " unknown"
        if self.explainer is not None:
            try:
                return str(self.explainer(f))
            except Exception as exc:
                return f"{self.NAME}: explain failed ({type(exc).__name__})"
        v = self.verdict(f)
        if v is None:
            return "NA: a check had unknown input"
        return "" if v else f"short of {self.NAME}"


# ── helpers over screen.hc_checks (the ONE implementation of the HC checks) ─────────
# Fields whose absence turns an hc check into None (template is a two-field rule and lp_known
# is config-dependent; both are handled by the body, never by the NA pre-pass).
_HC_REQUIRES = ("score", "liq_usd", "pair_age_min", "creator_prior_tokens", "roundtrip_loss_pct",
                "dev_pct", "dev_sniped", "total_holders", "top10_pct", "holders_source",
                "buys_h1", "sells_h1")
# lp_locked_pct is deliberately NOT required: hc_checks answers "LP known" from the % OR from a
# trusted launchpad hook (lp_check_source v4_launchpad:*), so the check itself carries the NA.
_HC_DATA = ("data/ledger.csv (solana A-vs-B scorecard, 16 A rows)", "$Cubrate post-mortem",
            "sources/safety.py degraded-source rule")


def _all_or_none(checks: dict):
    """Three-valued AND over {True, False, None}: False if any check is False (a definite miss
    is definite whatever else is unknown), None if nothing is False and something is unknown,
    True only when every check is True."""
    vals = list(checks.values())
    if any(v is False for v in vals):
        return False
    if any(v is None for v in vals):
        return None
    return True


def _hc_explain(feat: dict, drop: tuple = ()) -> str:
    checks = screen.hc_checks(feat)
    for k in drop:
        checks.pop(k, None)
    misses = [("unknown: " if v is None else "") + screen._MISS_TEXT.get(k, k)
              for k, v in checks.items() if v is not True]
    return "; ".join(misses)


def _band_a_strict(f):
    return _all_or_none(screen.hc_checks(f))


def _explain_a_strict(f):
    ok, misses = screen.high_conviction(f)
    return "" if ok else "; ".join(misses)


def _band_no_age(f):
    c = screen.hc_checks(f)
    # Both age-keyed $Cubrate gates go: with HC_MIN_HOLDERS=1000 and HC_MAX_HOLDERS_PER_MIN=8 the
    # holders/min check already implies age >= 125 min, so dropping only 'age' is inert vs the
    # champion (measured at build time). tx_per_holder (bot churn) stays.
    c.pop("age", None)
    c.pop("holders_per_min", None)
    return _all_or_none(c)


def _band_score60(f):
    c = screen.hc_checks(f)
    c["score"] = None if f.get("score") is None else f["score"] >= config.BAND_SCORE60_MIN
    return _all_or_none(c)


def _band_holders500(f):
    c = screen.hc_checks(f)
    # exact holder facts only; GT snapshots are up to 23 h stale -> unknown, never a number
    if f.get("holders_source") != "blockscout" or f.get("total_holders") is None:
        c["holders"] = None
    else:
        c["holders"] = f["total_holders"] >= config.BAND_HOLDERS500_MIN
    return _all_or_none(c)


def _band_dev_score_ge70(f):
    return f["creator_score"] >= config.BAND_DEV_SCORE_MIN


def _band_graduated_only(f):
    return (f["launchpad_completed"] is True
            and f["launchpad_completed_age_s"] <= config.BAND_GRADUATED_MAX_AGE_S)


def _top10_for_band(f):
    """Exact Blockscout top-10 when available; GT's snapshot only while fresh; else None."""
    if f.get("holders_source") == "blockscout":
        return f.get("top10_pct")
    age = f.get("holders_updated_age_s")
    if age is not None and age <= config.BAND_HOLDERS_MAX_STALE_S:
        return f.get("top10_pct_gt")
    return None


def _band_top10_le15(f):
    c = screen.hc_checks(f)
    c.pop("top10", None)
    t = _top10_for_band(f)
    c["top10_le15"] = None if t is None else t <= config.BAND_TOP10_LE15_MAX
    return _all_or_none(c)


def _explain_top10_le15(f):
    t = _top10_for_band(f)
    parts = []
    if t is None:
        parts.append("unknown: top10 (no exact or fresh holder snapshot)")
    elif t > config.BAND_TOP10_LE15_MAX:
        parts.append(f"top10 above {config.BAND_TOP10_LE15_MAX:.0f}%")
    rest = _hc_explain(f, drop=("top10",))
    if rest:
        parts.append(rest)
    return "; ".join(parts)


def _band_lp_burned_renounced(f):
    c = {
        "owner": None if f.get("owner_renounced") is None else f["owner_renounced"] is True,
        "lp": None if f.get("lp_locked_pct") is None
              else f["lp_locked_pct"] >= config.BAND_LP_BURNED_MIN_PCT,
        "roundtrip": None if f.get("roundtrip_loss_pct") is None
                     else f["roundtrip_loss_pct"] <= config.BAND_ROUND_TRIP_LOSS_MAX_PCT,
    }
    return _all_or_none(c)


def _explain_lp_burned_renounced(f):
    parts = []
    if f["owner_renounced"] is not True:
        parts.append("owner not renounced")
    if f["lp_locked_pct"] < config.BAND_LP_BURNED_MIN_PCT:
        parts.append(f"LP burned below {config.BAND_LP_BURNED_MIN_PCT:.0f}%")
    if f["roundtrip_loss_pct"] > config.BAND_ROUND_TRIP_LOSS_MAX_PCT:
        parts.append(f"round trip above {config.BAND_ROUND_TRIP_LOSS_MAX_PCT:.0f}%")
    return "; ".join(parts)


# ── controls ───────────────────────────────────────────────────────────────────────
def random_control_params(token: str) -> tuple:
    """(selected, delay_s) for a token — sha256 of the lowercased address, never the salted
    builtin hash(). Stable across processes and machines, so the control cannot be re-rolled."""
    h = hashlib.sha256(str(token).lower().encode("utf-8")).hexdigest()
    selected = (int(h[:8], 16) % 10_000) < int(round(config.BAND_CTL_RANDOM_RATE * 10_000))
    delay_s = int(h[8:16], 16) / 0xFFFFFFFF * config.BAND_WATCH_WINDOW_S
    return selected, delay_s


def _ctl_random_band(f):
    """Fires AT EVALUATION for the sha256-selected share — the delay from random_control_params is
    deliberately not applied. run.py records first-sighting verdicts at sighting_age_s = 0 and
    controls are barred from opening a band_fire row (runtime.decide_events), so a delayed control
    returned 0 on 672 of 672 committed verdict rows. An inert control cannot make failure visible,
    which is the only job it has. random_control_params is unchanged and still pinned by verify."""
    return bool(random_control_params(f["token"])[0])


def _ctl_inverse_band(f):
    """Never computed here. runtime.evaluate_bands fills it as
    (None if champion verdict is None else not champion verdict); the scorecard recomputes it on
    the fly from the champion column over the scoring window and never reads the sidecar value.
    Audit column only."""
    return None


BUILTINS: dict = {}


def _register(spec: BandSpec) -> BandSpec:
    BUILTINS[spec.NAME] = spec
    return spec


band_a_strict = _register(BandSpec(
    "band_a_strict",
    "The default champion: every screen.hc_checks check True (score, exact top-10 and holder "
    "count from Blockscout, liquidity, creator prior launches, age >= 90 min, organic "
    "holders/min and tx/holder rates, sell round-trip, whitelisted/verified template, LP known, "
    "dev holding, not sniped at creation). On solana this strict band was the best of 14 tested "
    "and every relaxation was worse; an unknown input is NA, never a silent False.",
    _HC_REQUIRES, _HC_DATA, "candidate", _band_a_strict, _explain_a_strict, THREE_VALUED=True))

band_no_age = _register(BandSpec(
    "band_no_age",
    "band_a_strict without the two age-keyed $Cubrate gates (age >= 90 min, holders/min <= 8; "
    "tx/holder churn kept). Tests whether the age floor buys survival or only delays entry past "
    "the early peak: solana winners peak ~4 h after entry, so an earlier entry could capture more "
    "of the move — or more of the rugs. Dropping 'age' alone is inert (holders/min implies it).",
    tuple(k for k in _HC_REQUIRES), _HC_DATA, "candidate", _band_no_age,
    lambda f: _hc_explain(f, drop=("age", "holders_per_min")), THREE_VALUED=True))

band_score60 = _register(BandSpec(
    "band_score60",
    "band_a_strict with the soft-score floor lowered from HC_MIN_SCORE to BAND_SCORE60_MIN. "
    "The soft score was anti-predictive for upside on solana; this measures whether the score "
    "floor is doing any work in the strict band or merely shrinking its n.",
    _HC_REQUIRES, _HC_DATA, "candidate", _band_score60,
    lambda f: ("score below %.0f; " % config.BAND_SCORE60_MIN
               if f["score"] < config.BAND_SCORE60_MIN else "") + _hc_explain(f, drop=("score",)), THREE_VALUED=True))

band_holders500 = _register(BandSpec(
    "band_holders500",
    "band_a_strict with the exact holder floor lowered to BAND_HOLDERS500_MIN (Blockscout "
    "count only, contracts excluded). Robinhood Chain is younger and thinner than solana; a "
    "1000-holder floor may be unreachable inside the 24 h window for most survivors.",
    _HC_REQUIRES, _HC_DATA, "candidate", _band_holders500,
    lambda f: ("holders below %d; " % config.BAND_HOLDERS500_MIN
               if f["total_holders"] < config.BAND_HOLDERS500_MIN else "")
    + _hc_explain(f, drop=("holders",)), THREE_VALUED=True))

band_dev_score_ge70 = _register(BandSpec(
    "band_dev_score_ge70",
    "RobinX's free deployer score >= BAND_DEV_SCORE_MIN (their own webhook threshold). A pure "
    "deployer-reputation band, kept because the proven-dev hypothesis was retired on evidence "
    "(P1 was a look-ahead bug; 27 of 27 $10M deployers were distinct) and this is the cheapest "
    "honest way to keep testing 'the dev matters' against a gate-matched control.",
    ("creator_score",), ("sources/robinx.py wallet record", "data/archive/proven_dev/"),
    "candidate", _band_dev_score_ge70,
    lambda f: f"deployer score {f['creator_score']:.0f} below {config.BAND_DEV_SCORE_MIN}"))

band_graduated_only = _register(BandSpec(
    "band_graduated_only",
    "Launchpad graduation completed within BAND_GRADUATED_MAX_AGE_S (GT launchpad_details / "
    "Flap progress). The pump.fun-migration analogue: on solana the migration instant was the "
    "one structural event every winner shared, and 60% of rugs collapsed < 20 min after it.",
    ("launchpad_completed", "launchpad_completed_age_s"),
    ("sources/geckoterminal.py token_info.launchpad", "$Cubrate post-mortem"),
    "candidate", _band_graduated_only,
    lambda f: ("not graduated" if f["launchpad_completed"] is not True
               else f"graduated {f['launchpad_completed_age_s'] / 60:.0f} min ago, "
                    f"past {config.BAND_GRADUATED_MAX_AGE_S // 60} min")))

band_top10_le15 = _register(BandSpec(
    "band_top10_le15",
    "band_a_strict with a tighter top-10 concentration cap (BAND_TOP10_LE15_MAX). Exact "
    "Blockscout top-10 when available; GT's snapshot only while younger than "
    "BAND_HOLDERS_MAX_STALE_S (GT holder snapshots run 8 min..23 h stale); otherwise NA.",
    tuple(k for k in _HC_REQUIRES if k != "top10_pct"), _HC_DATA + ("GT /tokens/{a}/info",),
    "candidate", _band_top10_le15, _explain_top10_le15, THREE_VALUED=True))

band_lp_burned_renounced = _register(BandSpec(
    "band_lp_burned_renounced",
    "Pure chain-truth band: owner renounced, LP burned >= BAND_LP_BURNED_MIN_PCT, sell "
    "round-trip <= BAND_ROUND_TRIP_LOSS_MAX_PCT — the three facts read directly by eth_call "
    "and never from an aggregator (GoPlus and ScanHood both got MIZUKARA's LP wrong). Tests "
    "whether survival is mostly contract hygiene rather than market shape.",
    ("owner_renounced", "lp_locked_pct", "roundtrip_loss_pct"),
    ("sources/rpc.py chain_facts_many", "MIZUKARA anchor"),
    "candidate", _band_lp_burned_renounced, _explain_lp_burned_renounced, THREE_VALUED=True))

def _band_launchpad_lenient(f):
    """The strict band minus the three checks a launchpad launch cannot answer for an app- or
    agent-launched token (dev holding, launcher prior count, creation-tx snipe); identical to
    band_a_strict off the launchpad, so coverage is the champion's."""
    c = screen.hc_checks(f)
    if str(f.get("lp_check_source") or "").startswith("v4_launchpad"):
        for k in ("dev_pct", "creator_prior", "not_sniped"):
            c.pop(k, None)
    return _all_or_none(c)


def _explain_launchpad_lenient(f):
    drop = ("dev_pct", "creator_prior", "not_sniped") if str(f.get("lp_check_source") or "").startswith("v4_launchpad") else ()
    return _hc_explain(f, drop=drop)


band_launchpad_lenient = _register(BandSpec(
    "band_launchpad_lenient",
    "Pre-declared 2026-09-12 from the two reference winners (CATGPT, ANTHROPIG: Bankr/Doppler "
    "launches on Uniswap V4). On the launchpad the dev holding, the launcher's prior count and "
    "the creation-tx snipe are unanswerable for app/agent-launched tokens, so this band drops "
    "exactly those three hc checks THERE and equals band_a_strict everywhere else. Tests whether "
    "the strict band's dev/creator checks cost recall on the chain's dominant launchpad.",
    tuple(k for k in _HC_REQUIRES if k not in ("dev_pct", "dev_sniped", "creator_prior_tokens")),
    ("CATGPT/ANTHROPIG launch paths (GT OHLCV 2026-09-12)", "LongLaunch Create logs", "sources/safety.py pass 2"),
    "candidate", _band_launchpad_lenient, _explain_launchpad_lenient, THREE_VALUED=True))

def _band_volume_early(f):
    return (f["pair_age_min"] <= config.BAND_VE_MAX_AGE_MIN
            and f["vol_h1"] >= config.BAND_VE_MIN_VOL_H1_USD
            and f["liq_usd"] >= config.BAND_VE_MIN_LIQ_USD
            and f["mcap"] <= config.BAND_VE_MAX_MCAP_USD
            and f["buys_h1"] >= config.BAND_VE_BUY_SELL_RATIO * max(1.0, float(f["sells_h1"])))


def _explain_volume_early(f):
    parts = []
    if f["pair_age_min"] > config.BAND_VE_MAX_AGE_MIN:
        parts.append(f"age above {config.BAND_VE_MAX_AGE_MIN:.0f}m (not early)")
    if f["vol_h1"] < config.BAND_VE_MIN_VOL_H1_USD:
        parts.append(f"hour-1 volume below ${config.BAND_VE_MIN_VOL_H1_USD:,.0f}")
    if f["liq_usd"] < config.BAND_VE_MIN_LIQ_USD:
        parts.append(f"liq below ${config.BAND_VE_MIN_LIQ_USD:,.0f}")
    if f["mcap"] > config.BAND_VE_MAX_MCAP_USD:
        parts.append(f"mcap above ${config.BAND_VE_MAX_MCAP_USD:,.0f} (not early)")
    if f["buys_h1"] < config.BAND_VE_BUY_SELL_RATIO * max(1.0, float(f["sells_h1"])):
        parts.append(f"buys below {config.BAND_VE_BUY_SELL_RATIO:.0f}x sells in hour 1")
    return "; ".join(parts)


band_volume_early = _register(BandSpec(
    "band_volume_early",
    "The operator's thesis, pre-declared 2026-09-12: get in EARLY (pool age <= 30 min, mcap <= "
    "$2M) on a coin that already trades heavily (hour-1 volume >= $50k, liq >= $10k) with buyers "
    "dominating (buys >= 2x sells). Measured at sighting on the day's 40 survivors: FRONTIER (23x), "
    "ROBOJENSEN (3.7x), DEGENFLY (3.3x) all had buys >= 2x sells while every balanced-flow "
    "sighting went flat or to zero (n=9, in-sample). A hypothesis the ledger will judge — it is "
    "the alerted band by manual --set, and the demotion test reverts it to band_a_strict if the "
    "selection lift over the same-day, same-age pool is not positive.",
    ("pair_age_min", "vol_h1", "liq_usd", "buys_h1", "sells_h1", "mcap"),
    ("latest_scan.json history 2026-09-12 (40 survivors, first sighting vs mcap 2.5 h later)",),
    "candidate", _band_volume_early, _explain_volume_early))

# ── the GMGN bands (pre-declared 2026-09-13) ──────────────────────────────────────
_GMGN_REQUIRES = ("gmgn_bundler_ratio", "gmgn_is_wash_trading")


def _band_gmgn_clean(f):
    """The strict band AND organic GMGN wallet tags: bundler wallets / holders at or below the
    A-band max (organic 0.00-0.01; the $Cubrate wallet farm 1.42) and no wash-trading flag."""
    c = screen.hc_checks(f)
    br, wash = f.get("gmgn_bundler_ratio"), f.get("gmgn_is_wash_trading")
    c["gmgn_bundler"] = None if br is None else br <= config.HC_GMGN_BUNDLER_RATIO_MAX
    c["gmgn_wash"] = None if wash is None else (wash is False)
    return _all_or_none(c)


def _explain_gmgn_clean(f):
    parts = []
    br, wash = f.get("gmgn_bundler_ratio"), f.get("gmgn_is_wash_trading")
    if br is None:
        parts.append("unknown: GMGN bundler ratio")
    elif br > config.HC_GMGN_BUNDLER_RATIO_MAX:
        parts.append(f"GMGN bundler ratio {br:.2f} above {config.HC_GMGN_BUNDLER_RATIO_MAX:.2f} (organic ~0.01)")
    if wash is None:
        parts.append("unknown: GMGN wash-trading flag")
    elif wash:
        parts.append("GMGN flags wash trading")
    rest = _hc_explain(f)
    if rest:
        parts.append(rest)
    return "; ".join(parts)


band_gmgn_clean = _register(BandSpec(
    "band_gmgn_clean",
    "Pre-declared 2026-09-13: band_a_strict plus GMGN's behavioural wallet tags — bundler wallets "
    "/ holders <= HC_GMGN_BUNDLER_RATIO_MAX and no wash-trading flag. On solana the bundler RATIO "
    "was the one signal that separated the $Cubrate wallet farm (1.42, RugCheck insider "
    "networks 0 before and after the rug) from organic coins (WIF 0.002, trumplet 0.01). Tests "
    "whether that second opinion buys survival here; NA when GMGN is dark, never a silent False.",
    _HC_REQUIRES + _GMGN_REQUIRES,
    ("solana $Cubrate post-mortem (2026-07-05)", "GMGN /v1/token/info wallet_tags_stat"),
    "candidate", _band_gmgn_clean, _explain_gmgn_clean, THREE_VALUED=True))


def _band_new_creation(f):
    return (f["pair_age_min"] <= config.BAND_NC_MAX_AGE_MIN
            and f["liq_usd"] >= config.LIQ_FLOOR_USD
            and f["roundtrip_loss_pct"] <= config.HC_MAX_ROUNDTRIP_PCT)


def _explain_new_creation(f):
    parts = []
    if f["pair_age_min"] > config.BAND_NC_MAX_AGE_MIN:
        parts.append(f"age above {config.BAND_NC_MAX_AGE_MIN:.0f}m (not a new creation)")
    if f["liq_usd"] < config.LIQ_FLOOR_USD:
        parts.append(f"liq below ${config.LIQ_FLOOR_USD:,.0f}")
    if f["roundtrip_loss_pct"] > config.HC_MAX_ROUNDTRIP_PCT:
        parts.append(f"round trip above {config.HC_MAX_ROUNDTRIP_PCT:.0f}%")
    return "; ".join(parts)


band_new_creation = _register(BandSpec(
    "band_new_creation",
    "Pre-declared 2026-09-13: GMGN Trenches' NEW column as a band — a priced pair no older than "
    "BAND_NC_MAX_AGE_MIN with the liquidity floor and a KNOWN sell round trip, and nothing else "
    "(band_volume_early adds the volume/flow condition; this is the unconditioned early entry). "
    "The base rate is the hypothesis under test: 0.2-2 % of curve launches ever graduate and "
    "~69 % never trade past their creation day.",
    ("pair_age_min", "liq_usd", "roundtrip_loss_pct"),
    ("GMGN Trenches new_creation column (2026-09-13)", "arXiv 2607.02823 graduation rates"),
    "candidate", _band_new_creation, _explain_new_creation))


def _band_almost_bonded(f):
    return (f["gmgn_progress"] >= config.BAND_AB_MIN_PROGRESS and f["gmgn_progress"] < 1.0
            and f.get("launchpad_completed") is not True
            and f["liq_usd"] >= config.LIQ_FLOOR_USD
            and f["roundtrip_loss_pct"] <= config.HC_MAX_ROUNDTRIP_PCT)


def _explain_almost_bonded(f):
    parts = []
    if f.get("launchpad_completed") is True or f["gmgn_progress"] >= 1.0:
        parts.append("curve already completed (migrated)")
    elif f["gmgn_progress"] < config.BAND_AB_MIN_PROGRESS:
        parts.append(f"curve progress {100 * f['gmgn_progress']:.0f}% below {100 * config.BAND_AB_MIN_PROGRESS:.0f}%")
    if f["liq_usd"] < config.LIQ_FLOOR_USD:
        parts.append(f"liq below ${config.LIQ_FLOOR_USD:,.0f}")
    if f["roundtrip_loss_pct"] > config.HC_MAX_ROUNDTRIP_PCT:
        parts.append(f"round trip above {config.HC_MAX_ROUNDTRIP_PCT:.0f}%")
    return "; ".join(parts)


band_almost_bonded = _register(BandSpec(
    "band_almost_bonded",
    "Pre-declared 2026-09-13: GMGN Trenches' ALMOST-BONDED column as a band — GMGN curve progress "
    ">= BAND_AB_MIN_PROGRESS and below 1.0 on a launch not yet completed, priced, with the "
    "liquidity floor and a known sell round trip. Tests the 'buy the curve before migration' "
    "entry GMGN's own guide recommends against buying above ~$45k on the curve; NA without a "
    "GMGN progress reading.",
    ("gmgn_progress", "liq_usd", "roundtrip_loss_pct"),
    ("GMGN Trenches near_completion column (2026-09-13)", "GeckoTerminal launchpad_details"),
    "candidate", _band_almost_bonded, _explain_almost_bonded))

ctl_random_band = _register(BandSpec(
    "ctl_random_band",
    "NEGATIVE CONTROL. A sha256-chosen BAND_CTL_RANDOM_RATE share of tokens is selected AT "
    "EVALUATION (random_control_params's selected flag) — no per-token timing, no staggered "
    "instants; see _ctl_random_band's own docstring for why. Its only job is to fail every gate; "
    "if it does not, the apparatus is measuring itself.",
    ("token",), (), "control", _ctl_random_band,
    lambda f: "selected (sha256 10%)" if random_control_params(f["token"])[0] else "not selected"))

ctl_inverse_band = _register(BandSpec(
    "ctl_inverse_band",
    "NEGATIVE CONTROL, audit column only: the champion's complement, computed by "
    "runtime.evaluate_bands as (None if champion verdict is None else not champion verdict) and "
    "recomputed on the fly by the scorecard. If the inverse beats the champion, the champion is "
    "anti-selecting.",
    (), (), "control", _ctl_inverse_band, lambda f: "inverse of the champion"))

CONTROL_NAMES = tuple(n for n, s in BUILTINS.items() if s.KIND == "control")
FAMILY_NAMES = tuple(n for n, s in BUILTINS.items() if s.KIND == "candidate")


# ── static source check for research candidates ───────────────────────────────────
def _attr_chain(node) -> list:
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return list(reversed(parts))
    return []


def static_ok(path: str) -> tuple:
    """(ok, reason). AST-based: import allowlist, forbidden attribute chains, forbidden calls.
    Strings and comments are never inspected."""
    try:
        with open(path, encoding="utf-8") as f:
            src = f.read()
        tree = ast.parse(src, filename=path)
    except Exception as exc:
        return False, f"unparseable: {type(exc).__name__}: {exc}"
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                if top not in ALLOWED_IMPORTS:
                    return False, f"import {alias.name!r} not in the allowlist"
        elif isinstance(node, ast.ImportFrom):
            if node.level or not node.module:
                return False, "relative imports are not allowed"
            top = node.module.split(".")[0]
            if top not in ALLOWED_IMPORTS:
                return False, f"from {node.module!r} import ... not in the allowlist"
        elif isinstance(node, ast.Attribute):
            chain = _attr_chain(node)
            if not chain:
                continue
            for a, b in zip(chain, chain[1:]):
                if a == "os" and b == "environ":
                    return False, "os.environ is forbidden"
                if a in ("numpy", "np") and b == "random":
                    return False, "numpy.random is forbidden"
            bad = [seg for seg in chain if seg in FORBIDDEN_ATTR_SEGMENTS]
            if bad:
                return False, f"attribute chain {'.'.join(chain)!r} touches {bad[0]!r}"
        elif isinstance(node, ast.Call):
            fn = node.func
            if isinstance(fn, ast.Name) and fn.id in FORBIDDEN_CALLS:
                return False, f"call to {fn.id}() is forbidden"
    return True, "ok"


# ── registry ───────────────────────────────────────────────────────────────────────
def spec_from_module(mod, kind: str = "candidate") -> BandSpec:
    """Wrap a candidate module (NAME, RATIONALE, REQUIRES, CONSUMED_DATA, verdict, explain?) into
    a BandSpec. Raises on a malformed module — the loader catches and skips."""
    name = str(getattr(mod, "NAME"))
    requires = tuple(str(k) for k in getattr(mod, "REQUIRES"))
    unknown = [k for k in requires if k not in config.FEATURE_FIELDS]
    if unknown:
        raise ValueError(f"REQUIRES not in FEATURE_FIELDS: {unknown}")
    verdict = getattr(mod, "verdict")
    if not callable(verdict):
        raise TypeError("verdict is not callable")
    explain = getattr(mod, "explain", None)
    return BandSpec(name, str(getattr(mod, "RATIONALE")), requires,
                    tuple(str(x) for x in (getattr(mod, "CONSUMED_DATA", ()) or ())),
                    kind, verdict, explain if callable(explain) else None)


def load_candidate_module(path: str, name: str):
    """Import selfimprove/candidates/<name>.py from its path (NOT via sys.modules, so a
    re-validation sees fresh code). Callers must run static_ok first."""
    spec = importlib.util.spec_from_file_location(f"selfimprove.candidates.{name}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Registry:
    """The registered bands: built-ins from this module, research candidates imported from
    selfimprove/candidates/<name>.py only if listed AND static_ok passes."""

    def __init__(self, entries: list, specs: dict, skipped: dict):
        self.entries = entries          # raw registry entries with kind == 'band'
        self._specs = specs             # name -> BandSpec (loaded)
        self.skipped = skipped          # name -> reason (listed but not loaded; verdicts NA)
        self._status = {e["name"]: e.get("status", "candidate") for e in entries}

    def names(self) -> list:
        return sorted(n for n, s in self._status.items() if s in ACTIVE_STATUSES)

    def candidates(self) -> list:
        return sorted(n for n, s in self._status.items() if s in ("champion", "candidate"))

    def controls(self) -> list:
        return sorted(n for n, s in self._status.items() if s == "control")

    def get(self, name: str):
        return self._specs.get(name)

    def status(self, name: str):
        return self._status.get(name)

    def is_control(self, name: str) -> bool:
        return self._status.get(name) == "control"

    def entry(self, name: str):
        for e in self.entries:
            if e.get("name") == name:
                return e
        return None


def load_registry(path: str | None = None) -> Registry:
    path = path or config.REGISTRY_PATH
    entries: list = []
    try:
        with open(path) as f:
            raw = json.load(f)
        entries = [e for e in (raw.get("candidates") or []) if e.get("kind") == "band"]
    except Exception as exc:
        print(f"  [bands] registry {path} unreadable ({exc}); built-ins only")
        entries = [{"name": n, "kind": "band", "module": BUILTIN_MODULE,
                    "status": ("control" if s.KIND == "control" else
                               "champion" if n == config.DEFAULT_ENTRY_BAND else "candidate")}
                   for n, s in BUILTINS.items()]
    cand_dir = os.path.dirname(os.path.abspath(path))
    specs: dict = {}
    skipped: dict = {}
    for e in entries:
        name, module = e.get("name"), str(e.get("module") or "")
        if e.get("status") not in ACTIVE_STATUSES:
            continue
        if module == BUILTIN_MODULE:
            if name in BUILTINS:
                specs[name] = BUILTINS[name]
            else:
                skipped[name] = "no such built-in band"
        elif module.startswith(CANDIDATE_MODULE_PREFIX):
            mod_name = module[len(CANDIDATE_MODULE_PREFIX):]
            mpath = os.path.join(cand_dir, mod_name + ".py")
            if not os.path.exists(mpath):
                skipped[name] = f"module file missing: {mpath}"
            else:
                ok, why = static_ok(mpath)
                if not ok:
                    skipped[name] = f"static check failed: {why}"
                else:
                    try:
                        mod = load_candidate_module(mpath, mod_name)
                        sp = spec_from_module(mod, "control" if e.get("status") == "control"
                                              else "candidate")
                        if sp.NAME != name:
                            raise ValueError(f"module NAME {sp.NAME!r} != registry name {name!r}")
                        specs[name] = sp
                    except Exception as exc:
                        skipped[name] = f"import failed: {type(exc).__name__}: {exc}"
        else:
            skipped[name] = f"unknown module {module!r}"
    for name, why in skipped.items():
        print(f"  [bands] WARNING band {name!r} skipped ({why}); its verdicts are NA")
    return Registry(entries, specs, skipped)


def tier_for(gates_ok: bool, verdicts: dict, champion: str) -> str:
    """'A' iff the hard gates passed AND the champion band's verdict is True. A band laxer than
    the hard gates is impossible by construction; an NA champion is B."""
    return "A" if (gates_ok and (verdicts or {}).get(champion) is True) else "B"


# ── smoke test (offline) ───────────────────────────────────────────────────────────
def clean_fixture() -> dict:
    """A clean survivor with every FEATURE_FIELDS key known (screen.py's clean fixture)."""
    f = {k: None for k in config.FEATURE_FIELDS}
    f.update({
        "price_usd": 0.001, "liq_usd": 45_000.0, "mcap": 250_000.0, "fdv": 250_000.0,
        "vol_h1": 9000.0, "vol_h6": 40000.0, "vol_h24": 750_000.0, "buys_h1": 450,
        "sells_h1": 50, "buys_h24": 3000, "sells_h24": 2500, "price_chg_h1": 4.0,
        "pair_age_min": 240.0, "dex": "uniswap",
        "owner_state": "renounced", "owner_renounced": True, "lp_locked_pct": 100.0,
        "lp_check_source": "rpc_v2", "honeypot": False, "roundtrip_loss_pct": 4.8,
        "is_scam": False, "template_name": "FlapTaxTokenV3", "is_proxy": True,
        "verified_source": True, "total_holders": 1400, "holders_source": "blockscout",
        "top10_pct": 18.0, "top10_pct_gt": 20.0, "lp_share_pct": 30.0, "dev_pct": 1.2,
        "deployer": "0xdev", "creator_prior_tokens": 0, "creator_dead_frac": 0.0,
        "creator_score": 80.0, "dev_sniped": False, "sniper_swaps_first_blocks": 4,
        "buys_per_buyer_m5": 1.1, "tx_per_holder_total": 5.0, "gt_score": 70.0,
        "gt_verified": True, "launchpad_graduation_pct": 100.0, "launchpad_completed": True,
        "launchpad_completed_age_s": 9000.0, "holders_updated_age_s": 600.0,
        "scanhood_verdict": "PASS", "scanhood_sellable": True, "sources_dark": [],
        "gmgn_launchpad_platform": "flap", "gmgn_progress": 1.0, "gmgn_bundler_ratio": 0.01,
        "gmgn_sniper_hold_pct": 0.5, "gmgn_insider_hold_pct": 0.0, "gmgn_fresh_wallet_pct": 5.0,
        "gmgn_rat_vol_pct": 0.0, "gmgn_smart_degen_count": 2, "gmgn_is_wash_trading": False,
        "gmgn_holders": 1400,
        "token": "0x" + "ab" * 20, "first_sighting": True, "sighting_age_s": 0.0,
    })
    f["score"] = screen.soft_score(f, f)[0]
    return f


if __name__ == "__main__":
    import tempfile

    base = clean_fixture()
    assert set(base) == set(config.FEATURE_FIELDS)
    print(f"built-ins: {list(BUILTINS)}")

    # 1. band_a_strict == screen.high_conviction on 20 variations (incl. the $Cubrate replay)
    variations = [
        {}, {"score": 50.0}, {"score": 70.0}, {"score": 69.9}, {"top10_pct": 25.0},
        {"top10_pct": 20.0}, {"total_holders": 999}, {"total_holders": 1000},
        {"pair_age_min": 89.0}, {"pair_age_min": 90.0}, {"pair_age_min": 30.0, "total_holders": 1300},
        {"liq_usd": 24_999.0}, {"creator_prior_tokens": 2}, {"roundtrip_loss_pct": 8.5},
        {"dev_pct": 2.5}, {"dev_sniped": True}, {"template_name": "Unknown", "verified_source": False},
        {"buys_h1": 3000, "sells_h1": 3000},
        # $Cubrate replay: literal at-alert numbers — never A
        {"liq_usd": 30_031.46, "vol_h24": 397_126.31, "mcap": 151_307.0, "buys_h1": 3662,
         "sells_h1": 1987, "pair_age_min": 19.94, "top10_pct": 4.4, "total_holders": 1344},
        {"liq_usd": 30_031.46, "vol_h24": 397_126.31, "mcap": 151_307.0, "buys_h1": 3662,
         "sells_h1": 1987, "pair_age_min": 95.0, "top10_pct": 4.4, "total_holders": 1344},
    ]
    n_true = 0
    for i, var in enumerate(variations):
        f = dict(base); f.update(var)
        f["score"] = screen.soft_score(f, f)[0]
        v = band_a_strict.verdict(f)
        hc_ok, misses = screen.high_conviction(f)
        assert v is not None and v == hc_ok, (i, var, v, hc_ok, misses)
        n_true += bool(v)
        if i == 0:
            assert v is True, f"the clean fixture must be A-tier: {misses}"
        if "19.94" in str(var.get("pair_age_min")):
            assert v is False, "Cubrate replay must never be A"
    print(f"  band_a_strict == high_conviction on {len(variations)} variations ({n_true} True)")
    print(f"  explain(clean w/ score 50): {band_a_strict.explain(dict(base, score=50.0))!r}")

    # every band on the clean fixture: a verdict, never NA (ctl_inverse excepted)
    for n, s in BUILTINS.items():
        v = s.verdict(base)
        print(f"  {n:26s} clean -> {v!s:5s}  requires={len(s.REQUIRES)}")
        if n != "ctl_inverse_band":
            assert v is not None, n
    assert band_no_age.verdict(dict(base, pair_age_min=30.0, total_holders=200, score=80.0)) is False  # holders floor
    assert band_no_age.verdict(dict(base, pair_age_min=60.0, score=80.0)) is True, "age-keyed gates dropped: 60m is fine"
    f_noage = dict(base, pair_age_min=80.0)
    assert band_a_strict.verdict(f_noage) is False and band_no_age.verdict(f_noage) is True, \
        "band_no_age must NOT be inert vs the champion (it was, before holders/min was dropped)"
    # NOTE: with HC_MIN_HOLDERS=1000 and HC_MAX_HOLDERS_PER_MIN=8, holders/min <= 8 already
    # implies age >= 125 min, so band_no_age cannot fire where band_a_strict does not — the
    # scorecard's inert check will say so; the band stays registered as the pre-declared test.
    print("  band_no_age: inert vs band_a_strict under current config (holders/min implies age >= "
          f"{config.HC_MIN_HOLDERS / config.HC_MAX_HOLDERS_PER_MIN:.0f} min)")
    assert band_holders500.verdict(dict(base, total_holders=600)) is True
    assert band_holders500.verdict(dict(base, holders_source="gt")) is None
    assert band_dev_score_ge70.verdict(dict(base, creator_score=69.0)) is False
    assert band_graduated_only.verdict(dict(base, launchpad_completed_age_s=3600.0)) is True
    assert band_graduated_only.verdict(dict(base, launchpad_completed_age_s=3601.0)) is False
    assert band_top10_le15.verdict(base) is False and band_top10_le15.verdict(dict(base, top10_pct=15.0)) is True
    assert band_lp_burned_renounced.verdict(dict(base, roundtrip_loss_pct=6.1)) is False

    # 2. a dark REQUIRES field yields None for every band; explain names it. Three-valued bands
    #    are tested on a fixture they FIRE on (a definite miss would rightly beat the unknown).
    _fires = {"band_top10_le15": {"top10_pct": 15.0, "top10_pct_gt": 15.0},
              "band_graduated_only": {"launchpad_completed_age_s": 3600.0}}
    for n, s in BUILTINS.items():
        fx = dict(base, **_fires.get(n, {}))
        if s.THREE_VALUED:
            assert s.verdict(fx) is True, (n, "fixture must fire")
        for k in s.REQUIRES:
            dark = dict(fx); dark[k] = None
            assert s.verdict(dark) is None, (n, k)
            assert s.explain(dark).startswith("NA: ") and k in s.explain(dark), (n, k)
    # 2b. three-valued: a definite miss beside an unknown is False, never masked as NA
    assert band_a_strict.verdict(dict(base, pair_age_min=60.0, lp_locked_pct=None)) is False
    assert band_a_strict.verdict(dict(base, lp_locked_pct=None)) is None
    # a raising body is NA, never an exception
    boom = BandSpec("boom", "", ("score",), (), "candidate", lambda f: 1 / 0)
    assert boom.verdict(base) is None and boom.explain(base)
    print("  dark REQUIRES -> NA for every band; raising body -> NA")

    # 3. ctl_random_band: rate within ±2 pp on 10,000 synthetic addresses, process-stable, delayed
    n_sel = 0
    fired_at_zero = 0
    for i in range(10_000):
        tok = "0x" + hashlib.sha256(f"synthetic-{i}".encode()).hexdigest()[:40]
        f = dict(base, token=tok, sighting_age_s=float(config.BAND_WATCH_WINDOW_S))
        v = ctl_random_band.verdict(f)
        n_sel += bool(v)
        if ctl_random_band.verdict(dict(f, sighting_age_s=0.0)):
            fired_at_zero += 1
    rate = n_sel / 10_000
    print(f"  ctl_random_band: {rate:.4f} selected at window end (target {config.BAND_CTL_RANDOM_RATE}); "
          f"{fired_at_zero} fired at age 0 (the age is not read — changed {config.BAND_CTL_RANDOM_CHANGED_ON})")
    assert abs(rate - config.BAND_CTL_RANDOM_RATE) <= 0.02
    # the control fires at EVALUATION: run.py records first-sighting verdicts at age 0, so a control
    # that waited for its delay was inert (0 of 672 committed rows)
    assert fired_at_zero == n_sel, (fired_at_zero, n_sel)
    sel, delay = random_control_params("0x" + "AB" * 20)
    assert random_control_params("0x" + "ab" * 20) == (sel, delay), "case-insensitive, stable"
    # pinned value: any change here means the control was re-rolled
    assert random_control_params("0x" + "ab" * 20) == (False, 62357.833739732814), \
        random_control_params("0x" + "ab" * 20)
    assert ctl_random_band.verdict(dict(base, token=None)) is None
    assert ctl_inverse_band.verdict(base) is None

    # 4. static_ok
    with tempfile.TemporaryDirectory() as d:
        cases = {
            "t_time.py": "import time\nNAME='x'\n",
            "t_open.py": "import config\ndef verdict(f):\n    return open('/etc/passwd')\n",
            "t_sklearn.py": "from sklearn.linear_model import LogisticRegression\n",
            "t_env.py": "import config\nX = config.os.environ.get('HOME')\n",
            "t_np.py": "import math\ndef verdict(f):\n    return np.random.default_rng(1)\n",
            "t_str.py": "import config\nNAME = 'ctl_random_band'\n# import time, open()\n"
                        "def verdict(f):\n    return f.get('time') is None\n",
        }
        for fn, src in cases.items():
            with open(os.path.join(d, fn), "w") as fh:
                fh.write(src)
        res = {fn: static_ok(os.path.join(d, fn)) for fn in cases}
        for fn, (ok, why) in res.items():
            print(f"  static_ok {fn:14s} -> {ok!s:5s} {why}")
        assert not res["t_time.py"][0] and not res["t_open.py"][0] and not res["t_sklearn.py"][0]
        assert not res["t_env.py"][0] and not res["t_np.py"][0]
        assert res["t_str.py"][0], "identifiers in strings/comments must not match"
    tmpl = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "candidates", "_template.py")
    if os.path.exists(tmpl):
        ok, why = static_ok(tmpl)
        print(f"  static_ok _template.py -> {ok} {why}")
        assert ok
    else:
        print("  _template.py not present yet — skipped")

    # 5. registry + tier_for
    reg = load_registry()
    print(f"  registry: {len(reg.names())} names, candidates={reg.candidates()}, controls={reg.controls()}")
    assert set(reg.controls()) == set(CONTROL_NAMES) and config.DEFAULT_ENTRY_BAND in reg.candidates()
    assert reg.status(config.DEFAULT_ENTRY_BAND) == "champion"
    with tempfile.TemporaryDirectory() as d:
        # an unregistered module under candidates/ is never evaluated; a listed bad one is skipped
        with open(os.path.join(d, "band_bad.py"), "w") as fh:
            fh.write("import time\nNAME='band_bad'\nREQUIRES=()\nRATIONALE=''\n"
                     "def verdict(f):\n    return True\n")
        with open(os.path.join(d, "band_never.py"), "w") as fh:
            fh.write("NAME='band_never'\nREQUIRES=()\nRATIONALE=''\ndef verdict(f):\n    return True\n")
        rp = os.path.join(d, "registry.json")
        with open(rp, "w") as fh:
            json.dump({"schema": 2, "candidates": [
                {"name": "band_a_strict", "kind": "band", "module": BUILTIN_MODULE, "status": "champion"},
                {"name": "band_bad", "kind": "band", "module": "candidates.band_bad", "status": "candidate"},
                {"name": "sell_45m", "kind": "policy", "module": "candidates.sell_45m", "status": "candidate"},
            ]}, fh)
        r2 = load_registry(rp)
        assert r2.names() == ["band_a_strict", "band_bad"] and r2.get("band_bad") is None
        assert "band_bad" in r2.skipped and r2.get("band_never") is None
    champ = config.DEFAULT_ENTRY_BAND
    assert tier_for(True, {champ: True}, champ) == "A"
    assert tier_for(False, {champ: True}, champ) == "B"
    assert tier_for(True, {champ: None}, champ) == "B"
    assert tier_for(True, {champ: False}, champ) == "B"
    assert tier_for(True, {}, champ) == "B"
    print("  tier_for ok")
    print("OK — bands.py assertions hold.")
