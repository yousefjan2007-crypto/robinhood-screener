"""
Entry-band statistics — every number the entry gate decides on, all day-clustered, all seeded.

WHAT. Given the ledger (one row per entry event with write-once forward returns) and the
band-verdict sidecar (every registered band's 1/0/NA verdict recorded at the SAME instant as the
row's entry price), this module builds one outcome series and scores every band on it:

  own_lb              the band's selected rows on their own: day-clustered BOOTSTRAP_ALPHA
                      quantile of the mean (evaluate.cluster_lb — the (sum, count) resampler)
  selection_lift_lb   the band's picks against the SAME-DAY, SAME-AGE-BUCKET unselected pool —
                      the lower bound on "did selecting help", stratified so a band cannot win
                      by firing on a good afternoon or by firing late
  day_paired_lift_lb  lift(band) − lift(champion), resampled JOINTLY over days on the rows both
                      bands judged — the only statistic the champion may be replaced on
  shuffled_fp_rate    the within-stratum permutation calibration: permute the band's verdicts
                      among the rows of each (day, age-bucket) stratum, preserving how many it
                      selected there, and count how often the lift bound still clears zero. On a
                      null this must be ≈ BOOTSTRAP_ALPHA; if the champion's is > BAND_SHUFFLE_MAX_FP
                      the resampler is miscalibrated on this data and the run is void
  boot_p_one_sided    the paired-lift resample's share ≤ 0 (a bootstrap p-value, one-sided)
  by_reject           Benjamini–Yekutieli over the family's p-values (valid under ARBITRARY
                      dependence — the bands are correlated restatements of each other)
  dsr_day_means       Deflated Sharpe (entry_bot/stats.py) on the per-DAY mean of selected returns
                      at the family's cumulative trial count; NaN (fails closed) when entry_bot is
                      absent or there are < 8 days
  inert / coverage / median_age   apparatus checks and the timing column

WHY STRATIFIED BY DAY AND AGE BUCKET (the measured incidents this shape answers). Alerts arrive in
bursts and share one market regime per day (on solana 43 of 60 live-path survivors landed in a
five-day window), so the day is the resampling unit, never the row — `~/entry_bot/CLAUDE.md`
documents a rule whose mean was +0.577 and whose clustered lower bound was −0.228 at n=10. And a
band that fires late in the 24 h watch window is not comparing like with like: on solana
promotions arrived a median 8.9 h after first sighting and the early rug wave was already over,
so an unstratified "selected vs everything" lift rewards WAITING rather than choosing. The pool
is therefore the unselected rows of the same day AND the same sighting-age bucket
(config.BAND_AGE_BUCKETS_S), a stratum is dropped when its unselected side is thinner than
max(BAND_MIN_UNSELECTED_PER_DAY_ABS, n_sel), and every scorecard row prints the median
sighting age of the band's picks so a timing artefact is visible on the table.

WHY THE INVERSE CONTROL IS RECOMPUTED HERE. ctl_inverse_band is (champion verdict == 0) with NA
where the champion is NA, computed from the champion column over the scoring window — never read
from the sidecar. The sidecar's copy was written against whichever band was champion at the time;
after a promotion it would be the complement of the wrong band.

Exclusions (both arms, counted and printed): unmatured metric cells, status 'suspect' (quote
integrity), entry_price < LEDGER_MIN_ENTRY_PRICE (Racoon's 5.6e-36 entry poisoned every mean),
and — via exclude_dark — rows whose sources_dark intersects the champion's required sources (a
row tiered on partial facts is not evidence about the band). Promoted-B rows are KEPT in the B
arm (intention-to-treat); nothing here filters on promoted_ts.

No wall-clock: the CLI captures time.time() once for the markdown header only. Randomness only via
np.random.default_rng(config.SEED + offset). Never raises on bad data.
"""
from __future__ import annotations

import hashlib
import os
import sys
import time

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import config                                        # noqa: E402
from selfimprove import evaluate as EV               # noqa: E402
from selfimprove.entry_lab import bands as B         # noqa: E402
from selfimprove.entry_lab import store              # noqa: E402

INVERSE = "ctl_inverse_band"
ENTRY_BOT_DIR = os.path.join(config.HOME, "entry_bot")     # stats.py, sys.path APPEND (our config wins)
_EMPTY = ("", "nan", "None", "NaN", "NA")
DSR_MIN_DAYS = 8                                           # entry_bot's own floor; below it, NaN
SHUFFLE_INNER_REPS = 400                                   # cheap inner bound inside the permutation loop

# REQUIRES field -> the source that answers it (substring rules, first match wins). A row whose
# sources_dark names one of the champion's sources was tiered on partial facts.
FIELD_SOURCE_RULES = (
    (("gmgn_",), "gmgn"),                 # first: gmgn_holders / gmgn_sniper_* must not match the rules below
    (("gt_", "launchpad_", "holders_updated", "top10_pct_gt"), "geckoterminal"),
    (("holders", "top10", "template", "is_scam", "deployer", "verified_source", "is_proxy",
      "creator_prior", "creator_dead"), "blockscout"),
    (("owner", "lp_", "roundtrip", "honeypot", "dev_pct", "dev_sniped", "sniper"), "rpc"),
    (("creator_score",), "robinx"),
)


# ── helpers ────────────────────────────────────────────────────────────────────────
def _num(v, default=float("nan")) -> float:
    try:
        if str(v) in _EMPTY:
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def day_of(ts: float) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(float(ts)))


def age_bucket(age_s: float, buckets=None) -> int:
    """Index into config.BAND_AGE_BUCKETS_S of the largest boundary <= age (0 for age <= 0)."""
    b = np.asarray(buckets if buckets is not None else config.BAND_AGE_BUCKETS_S, dtype=float)
    try:
        a = float(age_s)
    except (TypeError, ValueError):
        return 0
    if not np.isfinite(a) or a <= 0:
        return 0
    return int(max(0, np.searchsorted(b, a, side="right") - 1))


def _as_sel(x) -> np.ndarray:
    """A selection mask as float 1.0 / 0.0 / NaN (NaN = the band was NA on that row)."""
    a = np.asarray(x, dtype=float)
    out = np.full(a.shape, np.nan)
    ok = np.isfinite(a)
    out[ok] = (a[ok] != 0).astype(float)
    return out


def _codes(values) -> tuple:
    """Integer codes for a sequence of hashables (stable order of first appearance)."""
    idx: dict = {}
    codes = np.empty(len(values), dtype=int)
    for i, v in enumerate(values):
        codes[i] = idx.setdefault(v, len(idx))
    return codes, len(idx)


# ── the outcome series ─────────────────────────────────────────────────────────────
def outcome_series(led: pd.DataFrame, verdicts_wide: pd.DataFrame,
                   metric: str | None = None) -> tuple:
    """(series, excluded). series columns: event_seq, token, alert_ts, day, sighting_age_s,
    age_bucket, r, tier, event_kind, sources_dark (list), plus one float column per band in
    verdicts_wide (1/0/NaN, joined on event_seq; NaN where the sidecar has no line).
    excluded: {unmatured, suspect, bad_entry, bad_row} counts. Promoted-B rows are KEPT."""
    metric = metric or config.BAND_OUTCOME_METRIC
    excluded = {"unmatured": 0, "suspect": 0, "bad_entry": 0, "bad_row": 0}
    band_cols = [c for c in (verdicts_wide.columns if verdicts_wide is not None else [])
                 if c not in ("token", "alert_ts")]
    base_cols = ["event_seq", "token", "alert_ts", "day", "sighting_age_s", "age_bucket", "r",
                 "tier", "event_kind", "sources_dark"]
    if led is None or len(led) == 0:
        return pd.DataFrame(columns=base_cols + band_cols), excluded

    # first-sighting instant per token (the earliest first_sighting row; else the earliest row)
    first_ts: dict = {}                      # token -> (ts, from_a_first_sighting_row)
    for tok, kind, ts in zip(led["token"], led["event_kind"], led["alert_ts"]):
        t = str(tok).lower()
        tsv = _num(ts)
        if not np.isfinite(tsv):
            continue
        is_first = str(kind) == "first_sighting"
        cur = first_ts.get(t)
        if cur is None or (is_first and not cur[1]):
            first_ts[t] = (tsv, is_first)
        elif is_first == cur[1]:
            first_ts[t] = (min(cur[0], tsv), cur[1])

    rows = []
    for _, rrow in led.iterrows():
        try:
            seq = int(float(rrow["event_seq"]))
            tok = str(rrow["token"]).lower()
            ts = _num(rrow["alert_ts"])
            if not np.isfinite(ts):
                excluded["bad_row"] += 1
                continue
        except (TypeError, ValueError):
            excluded["bad_row"] += 1
            continue
        if str(rrow.get("status")) == "suspect":
            excluded["suspect"] += 1
            continue
        entry = _num(rrow.get("entry_price"), 0.0)
        if not (entry > config.LEDGER_MIN_ENTRY_PRICE):
            excluded["bad_entry"] += 1
            continue
        r = _num(rrow.get(metric))
        if not np.isfinite(r):
            excluded["unmatured"] += 1
            continue
        kind = str(rrow.get("event_kind"))
        if kind == "first_sighting":
            age = 0.0
        else:
            f = first_ts.get(tok)
            age = max(0.0, ts - f[0]) if f else 0.0
        dark_raw = rrow.get("sources_dark")
        dark = [] if str(dark_raw) in _EMPTY else [s.strip() for s in str(dark_raw).split(",") if s.strip()]
        rows.append({"event_seq": seq, "token": tok, "alert_ts": ts, "day": day_of(ts),
                     "sighting_age_s": age, "age_bucket": int(age_bucket(age)), "r": r,
                     "tier": str(rrow.get("tier")), "event_kind": kind, "sources_dark": dark})
    ser = pd.DataFrame(rows, columns=base_cols)
    if len(ser) and band_cols:
        wide = verdicts_wide[band_cols].copy()
        wide.index = pd.to_numeric(wide.index, errors="coerce")
        wide = wide[wide.index.notna()]
        wide.index = wide.index.astype(int)
        wide = wide[~wide.index.duplicated(keep="last")]
        ser = ser.join(wide, on="event_seq")
    else:
        for c in band_cols:
            ser[c] = np.nan
    ser = ser.sort_values("event_seq").reset_index(drop=True)
    return ser, excluded


def champion_sources(reg, champion: str) -> set:
    """Source names the champion's REQUIRES fields depend on (FIELD_SOURCE_RULES)."""
    spec = reg.get(champion) if reg is not None else None
    if spec is None:
        spec = B.BUILTINS.get(champion)
    if spec is None:
        return set()
    out = set()
    for field in spec.REQUIRES:
        f = str(field)
        for needles, src in FIELD_SOURCE_RULES:
            if any(n in f for n in needles):
                out.add(src)
                break
    return out


def exclude_dark(series: pd.DataFrame, sources) -> tuple:
    """Drop rows (BOTH arms) whose sources_dark intersects `sources`. Returns (series, n_dropped)."""
    srcs = set(sources or ())
    if series is None or len(series) == 0 or not srcs:
        return series, 0
    keep = np.array([not (set(d or []) & srcs) for d in series["sources_dark"]], dtype=bool)
    return series[keep].reset_index(drop=True), int((~keep).sum())


# ── selection masks ────────────────────────────────────────────────────────────────
def sel(series: pd.DataFrame, band: str, champion: str | None = None) -> np.ndarray:
    """1.0 / 0.0 / NaN per row. ctl_inverse_band is computed ON THE FLY as (champion == 0),
    NaN where the champion is NaN — never read from the sidecar."""
    if band == INVERSE:
        if not champion or champion not in series.columns:
            return np.full(len(series), np.nan)
        c = _as_sel(series[champion].values)
        out = np.full(len(series), np.nan)
        ok = np.isfinite(c)
        out[ok] = (c[ok] == 0).astype(float)
        return out
    if band not in series.columns:
        return np.full(len(series), np.nan)
    return _as_sel(series[band].values)


def coverage(s) -> float:
    a = np.asarray(s, dtype=float)
    return float(np.isfinite(a).mean()) if a.size else 0.0


def inert(sel_a, sel_b) -> bool:
    """True when the two masks agree on EVERY shared non-NA row (an alias cannot detect anything).
    False when there is no shared row."""
    a, b = _as_sel(sel_a), _as_sel(sel_b)
    if a.size != b.size:
        return False
    both = np.isfinite(a) & np.isfinite(b)
    if not both.any():
        return False
    return bool(np.all(a[both] == b[both]))


def median_age(s, ages) -> float:
    a, g = _as_sel(s), np.asarray(ages, dtype=float)
    m = (a == 1) & np.isfinite(g)
    return float(np.median(g[m])) if m.any() else float("nan")


# ── bounds ─────────────────────────────────────────────────────────────────────────
def own_lb(s, r, days, reps: int | None = None, seed: int | None = None) -> tuple:
    """(lb, n_days) on the band's selected rows alone via evaluate.cluster_lb."""
    a, rr = _as_sel(s), np.asarray(r, dtype=float)
    m = (a == 1) & np.isfinite(rr)
    if not m.any():
        return float("nan"), 0
    d = [days[i] for i in np.flatnonzero(m)]
    return EV.cluster_lb(rr[m], d, reps=reps, seed=seed)


def _resample_ratio(sums: np.ndarray, cnts: np.ndarray, reps: int, seed: int) -> np.ndarray:
    """sum(picked sums)/sum(picked cnts) over `reps` day-resamples (evaluate.cluster_lb's
    (sum, count) shape, batched 250 at a time), returned as the full distribution."""
    ng = sums.size
    rng = np.random.default_rng(seed)
    parts = []
    for start in range(0, reps, 250):
        k = min(250, reps - start)
        pick = rng.integers(0, ng, size=(k, ng))
        num = sums[pick].sum(axis=1)
        den = cnts[pick].sum(axis=1)
        with np.errstate(divide="ignore", invalid="ignore"):
            parts.append(np.where(den > 0, num / den, np.nan))
    return np.concatenate(parts)


def _lift_parts(s: np.ndarray, r: np.ndarray, strata: np.ndarray, n_strata: int,
                stratum_day: np.ndarray, n_days: int) -> tuple:
    """Per-DAY (D, W) for the stratified lift, over the rows where `s` is non-NA.
    Per stratum: S_sel, n_sel, S_unsel, n_unsel; the stratum is dropped when
    n_unsel < max(BAND_MIN_UNSELECTED_PER_DAY_ABS, n_sel) or n_sel == 0. Its contribution is
    D = S_sel − n_sel·mean_unsel (the n_sel-weighted lift) with weight W = n_sel, summed to
    the day; ΣD/ΣW over a resample of days is the pooled lift. Returns (D, W, day_kept mask)."""
    ok = np.isfinite(s) & np.isfinite(r)
    sel1 = ok & (s == 1)
    sel0 = ok & (s == 0)
    n_sel = np.bincount(strata[sel1], minlength=n_strata).astype(float)
    s_sel = np.bincount(strata[sel1], weights=r[sel1], minlength=n_strata)
    n_uns = np.bincount(strata[sel0], minlength=n_strata).astype(float)
    s_uns = np.bincount(strata[sel0], weights=r[sel0], minlength=n_strata)
    floor = np.maximum(float(config.BAND_MIN_UNSELECTED_PER_DAY_ABS), n_sel)
    keep = (n_sel > 0) & (n_uns >= floor)
    with np.errstate(divide="ignore", invalid="ignore"):
        mean_uns = np.where(n_uns > 0, s_uns / n_uns, 0.0)
    d_str = np.where(keep, s_sel - n_sel * mean_uns, 0.0)
    w_str = np.where(keep, n_sel, 0.0)
    D = np.bincount(stratum_day, weights=d_str, minlength=n_days)
    W = np.bincount(stratum_day, weights=w_str, minlength=n_days)
    return D, W, W > 0


def _strata(days, buckets) -> tuple:
    """(strata codes, n_strata, stratum_day codes, day codes, n_days)."""
    dcodes, nd = _codes([str(x) for x in days])
    keys = list(zip(dcodes.tolist(), [int(b) for b in buckets]))
    scodes, ns = _codes(keys)
    stratum_day = np.zeros(ns, dtype=int)
    stratum_day[scodes] = dcodes
    return scodes, ns, stratum_day, dcodes, nd


def _lift_dist(s, r, days, buckets, reps: int, seed: int) -> tuple:
    """(distribution over resamples, point estimate, n_days retained) for one band's lift."""
    s, r = _as_sel(s), np.asarray(r, dtype=float)
    if s.size == 0:
        return np.array([]), float("nan"), 0
    scodes, ns, sday, dcodes, nd = _strata(days, buckets)
    D, W, kept = _lift_parts(s, r, scodes, ns, sday, nd)
    if not kept.any():
        return np.array([]), float("nan"), 0
    Dk, Wk = D[kept], W[kept]
    point = float(Dk.sum() / Wk.sum())
    return _resample_ratio(Dk, Wk, reps, seed), point, int(kept.sum())


def selection_lift_lb(s, r, days, buckets, reps: int | None = None,
                      seed: int | None = None) -> tuple:
    """(lb, mean, n_days): the band's picks vs the same-day, same-age-bucket unselected pool,
    days resampled with replacement. lb = the BOOTSTRAP_ALPHA quantile."""
    reps = reps or config.BOOTSTRAP_REPS
    dist, point, nd = _lift_dist(s, r, days, buckets, reps,
                                 config.SEED if seed is None else seed)
    dist = dist[np.isfinite(dist)]
    if dist.size == 0:
        return float("nan"), point, nd
    return float(np.quantile(dist, config.BOOTSTRAP_ALPHA)), point, nd


def _paired_dist(sel_b, sel_champ, r, days, buckets, reps: int, seed: int) -> tuple:
    """(distribution of lift_b − lift_champ over joint day resamples, point, n_days) on the
    intersection of rows both bands judged."""
    a, c, rr = _as_sel(sel_b), _as_sel(sel_champ), np.asarray(r, dtype=float)
    if a.size == 0 or a.size != c.size:
        return np.array([]), float("nan"), 0
    both = np.isfinite(a) & np.isfinite(c) & np.isfinite(rr)
    if not both.any():
        return np.array([]), float("nan"), 0
    idx = np.flatnonzero(both)
    dd = [days[i] for i in idx]
    bb = [buckets[i] for i in idx]
    scodes, ns, sday, dcodes, nd = _strata(dd, bb)
    Da, Wa, ka = _lift_parts(a[idx], rr[idx], scodes, ns, sday, nd)
    Dc, Wc, kc = _lift_parts(c[idx], rr[idx], scodes, ns, sday, nd)
    kept = ka | kc
    if not (ka.any() and kc.any()):
        return np.array([]), float("nan"), int(kept.sum())
    Da, Wa, Dc, Wc = Da[kept], Wa[kept], Dc[kept], Wc[kept]
    point = float(Da.sum() / Wa.sum() - Dc.sum() / Wc.sum())
    ng = Da.size
    rng = np.random.default_rng(seed)
    parts = []
    for start in range(0, reps, 250):
        k = min(250, reps - start)
        pick = rng.integers(0, ng, size=(k, ng))
        with np.errstate(divide="ignore", invalid="ignore"):
            la = Da[pick].sum(axis=1) / Wa[pick].sum(axis=1)
            lc = Dc[pick].sum(axis=1) / Wc[pick].sum(axis=1)
        parts.append(la - lc)
    return np.concatenate(parts), point, int(kept.sum())


def day_paired_lift_lb(sel_b, sel_champ, r, days, buckets, reps: int | None = None,
                       seed: int | None = None) -> tuple:
    """(lb, mean, n_days) for lift(band) − lift(champion), resampled jointly over days."""
    reps = reps or config.BOOTSTRAP_REPS
    dist, point, nd = _paired_dist(sel_b, sel_champ, r, days, buckets, reps,
                                   config.SEED + 17 if seed is None else seed)
    dist = dist[np.isfinite(dist)]
    if dist.size == 0:
        return float("nan"), point, nd
    return float(np.quantile(dist, config.BOOTSTRAP_ALPHA)), point, nd


def boot_p_one_sided(sel_b, sel_champ, r, days, buckets, reps: int | None = None,
                     seed: int | None = None) -> float:
    """Share of the paired-lift resample distribution that is <= 0. NaN when undefined."""
    reps = reps or config.BOOTSTRAP_REPS
    dist, _, _ = _paired_dist(sel_b, sel_champ, r, days, buckets, reps,
                              config.SEED + 17 if seed is None else seed)
    dist = dist[np.isfinite(dist)]
    if dist.size == 0:
        return float("nan")
    return float(np.mean(dist <= 0))


def shuffled_fp_rate(s, r, days, buckets, reps: int | None = None,
                     inner_reps: int = SHUFFLE_INNER_REPS) -> float:
    """Permute the band's verdicts WITHIN each (day, age-bucket) stratum among its non-NA rows
    (per-stratum selection counts preserved), rep k seeded default_rng(SEED + 1000 + k), and
    return the share of permutations whose selection_lift_lb (inner_reps) is > 0."""
    reps = config.BAND_SHUFFLE_REPS if reps is None else int(reps)
    s, rr = _as_sel(s), np.asarray(r, dtype=float)
    ok = np.isfinite(s) & np.isfinite(rr)
    if ok.sum() < 2 or reps <= 0:
        return float("nan")
    idx = np.flatnonzero(ok)
    dd = [days[i] for i in idx]
    bb = [buckets[i] for i in idx]
    scodes, ns, sday, dcodes, nd = _strata(dd, bb)
    s_ok = s[idx]
    r_ok = rr[idx]
    order0 = np.lexsort((np.arange(idx.size), scodes))     # rows grouped by stratum, original order
    hits = 0
    n = 0
    for k in range(reps):
        rng = np.random.default_rng(config.SEED + 1000 + k)
        order1 = np.lexsort((rng.random(idx.size), scodes))   # same grouping, random order inside
        perm = np.empty_like(s_ok)
        perm[order1] = s_ok[order0]
        D, W, kept = _lift_parts(perm, r_ok, scodes, ns, sday, nd)
        if not kept.any():
            continue
        dist = _resample_ratio(D[kept], W[kept], inner_reps, config.SEED + 1000 + k)
        dist = dist[np.isfinite(dist)]
        if dist.size == 0:
            continue
        n += 1
        if float(np.quantile(dist, config.BOOTSTRAP_ALPHA)) > 0:
            hits += 1
    return float(hits / n) if n else float("nan")


def by_reject(pvals, q: float | None = None) -> list:
    """Benjamini–Yekutieli (BH with the harmonic correction, valid under arbitrary dependence).
    NaN p-values are never rejected and still count toward m."""
    q = config.FDR_Q if q is None else float(q)
    p = np.asarray([float(x) if x == x else 1.0 for x in (pvals or [])], dtype=float)
    m = p.size
    if m == 0:
        return []
    c_m = float(np.sum(1.0 / np.arange(1, m + 1)))
    order = np.argsort(p)
    ps = p[order]
    thresh = (np.arange(1, m + 1) / (m * c_m)) * q
    below = np.flatnonzero(ps <= thresh)
    k = int(below.max()) + 1 if below.size else 0
    out = np.zeros(m, dtype=bool)
    out[order[:k]] = True
    return [bool(x) for x in out]


def _dsr_fn():
    """entry_bot/stats.deflated_sharpe_ratio, or None when entry_bot is absent (fails closed).
    sys.path APPEND so THIS repo's config wins inside stats.py."""
    if not os.path.isfile(os.path.join(ENTRY_BOT_DIR, "stats.py")):
        return None
    if ENTRY_BOT_DIR not in sys.path:
        sys.path.append(ENTRY_BOT_DIR)
    try:
        import stats as ST                      # noqa: F401  (entry_bot/stats.py)
        fn = getattr(ST, "deflated_sharpe_ratio", None)
        if fn is None or not callable(fn):
            return None
        return fn
    except Exception:
        return None


def dsr_day_means(s, r, days, n_trials: int) -> float:
    """Deflated Sharpe on the per-DAY mean of the selected rows' returns (entry_bot/stats.py
    scales by sqrt(n−1): feeding rows where the honest cluster count is days inflates the
    statistic ~4x — measured 17% false pass IID vs 0% over day means on the exit book).
    NaN when entry_bot is absent or fewer than DSR_MIN_DAYS days."""
    fn = _dsr_fn()
    if fn is None:
        return float("nan")
    a, rr = _as_sel(s), np.asarray(r, dtype=float)
    m = (a == 1) & np.isfinite(rr)
    dm: dict = {}
    for i in np.flatnonzero(m):
        dm.setdefault(str(days[i]), []).append(rr[i])
    means = [float(np.mean(v)) for v in dm.values()]
    if len(means) < DSR_MIN_DAYS:
        return float("nan")
    try:
        return float(fn(means, max(int(n_trials), 1)))
    except Exception:
        return float("nan")


# ── the table ──────────────────────────────────────────────────────────────────────
def band_row(series: pd.DataFrame, band: str, champion: str, status: str, n_trials: int,
             reps: int | None = None, with_p: bool = True) -> dict:
    """One scorecard row for `band` on `series`. NaN where undefined; never raises."""
    r = series["r"].values.astype(float)
    days = [str(d) for d in series["day"].values]
    buckets = [int(b) for b in series["age_bucket"].values]
    ages = series["sighting_age_s"].values.astype(float)
    s = sel(series, band, champion)
    c = sel(series, champion, champion)
    row = {"band": band, "status": status, "n": int(np.nansum(s == 1)), "days": 0,
           "coverage": coverage(s), "mean": float("nan"), "own_lb": float("nan"),
           "lift_lb": float("nan"), "lift_mean": float("nan"), "lift_days": 0,
           "paired_lb": float("nan"), "paired_mean": float("nan"), "paired_days": 0,
           "dsr": float("nan"), "p": float("nan"), "by_keep": None,
           "median_age_s": median_age(s, ages),
           "inert_vs_champion": (band != champion) and inert(s, c),
           "na_share": float(1.0 - coverage(s)) if s.size else 1.0}
    try:
        m = (s == 1) & np.isfinite(r)
        if m.any():
            row["mean"] = float(np.mean(r[m]))
            row["own_lb"], row["days"] = own_lb(s, r, days, reps=reps)
        lb, mean, nd = selection_lift_lb(s, r, days, buckets, reps=reps)
        row["lift_lb"], row["lift_mean"], row["lift_days"] = lb, mean, nd
        if band != champion:
            plb, pmean, pnd = day_paired_lift_lb(s, c, r, days, buckets, reps=reps)
            row["paired_lb"], row["paired_mean"], row["paired_days"] = plb, pmean, pnd
            if with_p:
                row["p"] = boot_p_one_sided(s, c, r, days, buckets, reps=reps)
        row["dsr"] = dsr_day_means(s, r, days, n_trials)
    except Exception as exc:                     # a bad column must not kill the table
        row["error"] = f"{type(exc).__name__}: {exc}"
    return row


def table(series: pd.DataFrame, reg, champion: str, n_trials: int,
          reps: int | None = None) -> tuple:
    """(rows, controls): rows for every non-control registered band (champion first), controls
    for the two negative controls. BY (q=FDR_Q) is applied over the non-champion candidates'
    one-sided bootstrap p-values and written to each row's by_keep."""
    names = reg.names() if reg is not None else list(B.BUILTINS)
    if champion not in names:
        names = [champion] + names
    rows, controls = [], []
    for name in names:
        is_ctl = (reg.is_control(name) if reg is not None else name in B.CONTROL_NAMES)
        status = "control" if is_ctl else ("champion" if name == champion else
                                          (reg.status(name) if reg is not None else "candidate") or "candidate")
        row = band_row(series, name, champion, status, n_trials, reps=reps, with_p=not is_ctl)
        (controls if is_ctl else rows).append(row)
    rows.sort(key=lambda x: (x["band"] != champion, -(x["paired_lb"] if x["paired_lb"] == x["paired_lb"] else -1e9)))
    cands = [x for x in rows if x["band"] != champion]
    keep = by_reject([x["p"] for x in cands])
    for x, k in zip(cands, keep):
        x["by_keep"] = bool(k)
    return rows, controls


def _fmt(v, spec: str = "+.3f") -> str:
    try:
        if v is None or v != v:
            return "·"
        return format(float(v), spec)
    except (TypeError, ValueError):
        return "?"


def markdown(rows: list, controls: list, meta: dict | None = None) -> str:
    """The scorecard as markdown. meta: {champion, n_events, n_days, n_excluded, metric,
    n_trials, fp_champion, stamp}. An empty table renders 'insufficient (n=0)'."""
    meta = meta or {}
    lines = []
    champ = meta.get("champion", "?")
    lines.append(f"### entry-band scorecard — metric `{meta.get('metric', config.BAND_OUTCOME_METRIC)}`"
                 + (f" — {meta['stamp']} UTC" if meta.get("stamp") else ""))
    lines.append("")
    n_ev = int(meta.get("n_events", 0) or 0)
    if n_ev == 0 or not rows:
        lines.append("insufficient (n=0): no matured event rows on the ledger yet.")
        return "\n".join(lines) + "\n"
    lines.append(f"- champion: `{champ}`; matured events: **{n_ev}** across **{meta.get('n_days', '?')}** "
                 f"alert-days; excluded: {meta.get('n_excluded', {})}; cumulative band trials: "
                 f"**{meta.get('n_trials', '?')}**")
    if meta.get("fp_champion") is not None:
        lines.append(f"- champion shuffled false-positive rate: **{_fmt(meta['fp_champion'], '.3f')}** "
                     f"(void above {config.BAND_SHUFFLE_MAX_FP})")
    lines.append("")
    hdr = ("| band | status | n | days | cov | mean | own LB | lift LB | paired LB | DSR | p | BY | "
           "median age | inert |")
    lines += [hdr, "|" + "---|" * 14]
    for x in rows + controls:
        age = x.get("median_age_s")
        age_s = "·" if age is None or age != age else f"{age / 3600:.1f} h"
        by = "·" if x.get("by_keep") is None else ("keep" if x["by_keep"] else "drop")
        lines.append(
            f"| `{x['band']}` | {x['status']} | {x['n']} | {x['days']} | {_fmt(x['coverage'], '.2f')} | "
            f"{_fmt(x['mean'])} | {_fmt(x['own_lb'])} | {_fmt(x['lift_lb'])} | {_fmt(x['paired_lb'])} | "
            f"{_fmt(x['dsr'], '.3f')} | {_fmt(x['p'], '.3f')} | {by} | {age_s} | "
            f"{'YES' if x.get('inert_vs_champion') else 'no'} |")
    lines.append("")
    lines.append(f"Bounds are the {config.BOOTSTRAP_ALPHA:.1%} quantile of a DAY-clustered bootstrap "
                 f"({config.BOOTSTRAP_REPS} reps); lift is vs the same-day, same-age-bucket unselected "
                 f"pool; paired = lift(band) − lift(champion) on shared rows. A row with fewer than "
                 f"{config.MIN_BOOTSTRAP_CLUSTERS} days is not a bound. Means are context, never the "
                 f"criterion. Not financial advice.")
    return "\n".join(lines) + "\n"


def load_real(ledger_path: str | None = None, verdicts_path: str | None = None) -> tuple:
    """(series, excluded, verdicts_wide) from the committed files. Never raises."""
    try:
        import ledger as LED
        led = LED.load(ledger_path)
    except Exception as exc:
        print(f"  [scorecard] ledger unreadable ({exc}); treating as empty")
        led = pd.DataFrame(columns=["token", "event_kind", "alert_ts", "event_seq"])
    try:
        wide = store.pivot_verdicts(store.load_verdicts(verdicts_path))
    except Exception as exc:
        print(f"  [scorecard] verdict sidecar unreadable ({exc}); treating as empty")
        wide = pd.DataFrame(columns=["token", "alert_ts"])
    ser, excl = outcome_series(led, wide)
    return ser, excl, wide


# ── offline self-test ──────────────────────────────────────────────────────────────
def synthetic_series(n_days: int = 60, per_day: int = 12, planted: str = "band_score60",
                     champion: str = "band_a_strict", seed: int | None = None,
                     start_ts: float = 1_780_012_800.0, planted_edge: bool = True) -> pd.DataFrame:
    """A synthetic outcome series: per day `per_day` events (8 first sightings at age 0, the
    rest band fires at ~2 h), skewed −EV returns (most −0.9, a few +3). `planted` selects 3
    rows per day and, when planted_edge, those rows carry the top returns. Every other band is
    an independent random selection (15%; ctl_random_band 10%); the champion picks 2 rows per
    day at random (no edge). start_ts defaults to a UTC midnight so a day's events share a day."""
    base_seed = config.SEED if seed is None else seed
    rng = np.random.default_rng(base_seed)
    names = [n for n in B.BUILTINS if n != INVERSE]
    # every "other band" draws from ITS OWN stream (seeded by name) so that registering one more
    # built-in never re-rolls the returns, the champion's picks or the planted picks of the
    # fixture (the 2026-09-12 launchpad band moved the shared stream and a null control cleared
    # the paired bound by chance)
    band_rng = {n: np.random.default_rng(base_seed + 1000 + int(hashlib.sha256(n.encode()).hexdigest()[:8], 16) % 100_000)
                for n in names}
    rows = []
    seq = 0
    for d in range(n_days):
        day_ts = start_ts + d * 86400
        n_first = min(8, per_day)
        for j in range(per_day):
            seq += 1
            first = j < n_first
            age = 0.0 if first else 7200.0 + 60 * j
            r = -0.9 if rng.random() < 0.90 else 3.0
            row = {"event_seq": seq, "token": f"0x{seq:040x}", "alert_ts": day_ts + j * 600,
                   "day": day_of(day_ts + j * 600), "sighting_age_s": age, "age_bucket": int(age_bucket(age)),
                   "r": r, "tier": "B", "event_kind": "first_sighting" if first else "band_fire",
                   "sources_dark": []}
            for n in names:
                row[n] = 0.0
            rows.append(row)
        # the day's rows
        drows = rows[-per_day:]
        for n in names:
            if n in (planted, champion):
                continue
            rate = config.BAND_CTL_RANDOM_RATE if n == "ctl_random_band" else 0.15
            rn = band_rng[n]
            for x in drows:
                x[n] = 1.0 if rn.random() < rate else 0.0
        for k in rng.choice(per_day, size=2, replace=False):
            drows[int(k)][champion] = 1.0
        picks = rng.choice(n_first, size=3, replace=False)       # in bucket 0: 5 unselected remain
        for k in picks:
            x = drows[int(k)]
            x[planted] = 1.0
            if planted_edge:
                x["r"] = float(rng.choice([1.5, 0.8, 0.4, -0.9], p=[0.3, 0.3, 0.3, 0.10]))
    return pd.DataFrame(rows)


if __name__ == "__main__":
    import tempfile
    t_start = time.time()
    champ = config.DEFAULT_ENTRY_BAND
    planted = "band_score60"
    ser = synthetic_series(planted=planted, champion=champ)
    r = ser["r"].values.astype(float)
    days = list(ser["day"].values)
    buckets = list(ser["age_bucket"].values)
    print(f"synthetic series: {len(ser)} rows over {ser['day'].nunique()} days; mean r {r.mean():+.3f} "
          f"(skewed −EV), buckets {sorted(set(buckets))}")
    assert r.mean() < 0

    # 1. the planted band clears zero on lift AND on its own bound; a random band does not
    s_p = sel(ser, planted, champ)
    s_rand = sel(ser, "band_holders500", champ)
    lb_p, mean_p, nd_p = selection_lift_lb(s_p, r, days, buckets)
    lb_r, mean_r, nd_r = selection_lift_lb(s_rand, r, days, buckets)
    olb_p, ond_p = own_lb(s_p, r, days)
    olb_r, ond_r = own_lb(s_rand, r, days)
    print(f"  planted  lift LB {lb_p:+.3f} (mean {mean_p:+.3f}, {nd_p} days)  own LB {olb_p:+.3f} ({ond_p} days)")
    print(f"  random   lift LB {lb_r:+.3f} (mean {mean_r:+.3f}, {nd_r} days)  own LB {olb_r:+.3f} ({ond_r} days)")
    assert lb_p > 0 and olb_p > 0 and nd_p == 60
    assert not (lb_r > 0) and not (olb_r > 0)
    # determinism
    assert selection_lift_lb(s_p, r, days, buckets)[0] == lb_p

    # 2. paired vs the champion, and the one-sided bootstrap p
    s_c = sel(ser, champ, champ)
    plb, pmean, pnd = day_paired_lift_lb(s_p, s_c, r, days, buckets)
    p_p = boot_p_one_sided(s_p, s_c, r, days, buckets)
    plb_r, _, _ = day_paired_lift_lb(s_rand, s_c, r, days, buckets)
    p_r = boot_p_one_sided(s_rand, s_c, r, days, buckets)
    print(f"  paired vs champion: planted LB {plb:+.3f} p={p_p:.4f}; random LB {plb_r:+.3f} p={p_r:.3f}")
    assert plb > 0 and p_p < 0.01 and not (plb_r > 0) and p_r > 0.05

    # 3. the shuffled control destroys the planted edge (default reps)
    fp = shuffled_fp_rate(s_p, r, days, buckets)
    print(f"  shuffled fp rate on the planted band: {fp:.3f} ({config.BAND_SHUFFLE_REPS} reps, "
          f"cap {config.BAND_SHUFFLE_MAX_FP})")
    assert fp <= config.BAND_SHUFFLE_MAX_FP

    # 4. inverse control computed on the fly: (champion == 0), NaN where the champion is NaN
    ser_na = ser.copy()
    ser_na.loc[0, champ] = np.nan
    s_inv = sel(ser_na, INVERSE, champ)
    assert s_inv[0] != s_inv[0] and s_inv[1] == (1.0 - float(ser_na.loc[1, champ]))
    assert coverage(s_inv) == coverage(sel(ser_na, champ, champ))
    # the champion NA share matters: the inverse must NOT be read from a sidecar column
    ser_na[INVERSE] = 1.0
    assert (sel(ser_na, INVERSE, champ)[1:] == 1.0 - _as_sel(ser_na[champ].values[1:])).all()

    # 5. BY matches a hand-computed answer (and statsmodels when importable)
    pv = [0.001, 0.004, 0.02, 0.03, 0.2, 0.5, 0.8, 0.9, float("nan")]
    m = len(pv)
    c_m = sum(1.0 / i for i in range(1, m + 1))
    hand = []
    ps = sorted((p if p == p else 1.0) for p in pv)
    kmax = max([k + 1 for k in range(m) if ps[k] <= (k + 1) / (m * c_m) * config.FDR_Q] or [0])
    cut = ps[kmax - 1] if kmax else -1.0
    hand = [bool(p == p and p <= cut) for p in pv]
    got = by_reject(pv)
    print(f"  BY q={config.FDR_Q}: reject {got} (hand: {hand})")
    assert got == hand and got[0] and got[1] and not got[4] and not got[-1]
    assert by_reject([]) == []
    try:
        from statsmodels.stats.multitest import multipletests
        sm = [bool(x) for x in multipletests([p if p == p else 1.0 for p in pv], alpha=config.FDR_Q,
                                             method="fdr_by")[0]]
        assert sm == got, (sm, got)
        print("  BY agrees with statsmodels fdr_by")
    except ImportError:
        print("  statsmodels not importable — BY checked against the hand computation only")

    # 6. DSR on day means: planted high, and NaN when entry_bot is missing (fails closed)
    n_tr = len(B.BUILTINS)
    dsr_p = dsr_day_means(s_p, r, days, n_tr)
    dsr_r = dsr_day_means(s_rand, r, days, n_tr)
    print(f"  DSR at {n_tr} trials: planted {dsr_p:.3f}, random {dsr_r:.3f}")
    assert dsr_p >= config.BAND_DSR_GATE and dsr_r < config.BAND_DSR_GATE
    assert dsr_day_means(s_p[:24], r[:24], days[:24], n_tr) != dsr_day_means(s_p[:24], r[:24], days[:24], n_tr), \
        "fewer than 8 days must be NaN"
    _saved_dir, _saved_path = ENTRY_BOT_DIR, list(sys.path)
    ENTRY_BOT_DIR = os.path.join(tempfile.gettempdir(), "no_such_entry_bot_dir")
    sys.path = [p for p in sys.path if not p.endswith("entry_bot")]
    sys.modules.pop("stats", None)
    v = dsr_day_means(s_p, r, days, n_tr)
    ENTRY_BOT_DIR, sys.path = _saved_dir, _saved_path
    assert v != v, "entry_bot absent must yield NaN"
    print("  DSR with entry_bot absent: NaN (fails closed)")

    # 7. inert detection; median age for the timing column
    assert inert(s_p, s_p.copy()) and not inert(s_p, s_c)
    twin = s_p.copy(); twin[np.isnan(twin)] = np.nan
    twin_na = s_p.copy(); twin_na[:5] = np.nan
    assert inert(twin_na, s_p), "NA rows are not shared rows"
    assert not inert(np.full(len(ser), np.nan), s_p), "no shared row ⇒ not inert"
    ages = ser["sighting_age_s"].values
    print(f"  median sighting age: planted {median_age(s_p, ages):.0f}s (bucket-0 picks), "
          f"random {median_age(s_rand, ages):.0f}s")
    assert median_age(s_p, ages) == 0.0

    # 8. strata dropping when n_unsel < max(2, n_sel): a band selecting 7 of 8 bucket-0 rows
    #    leaves 1 unselected → every bucket-0 stratum dropped; bucket-2 (4 rows, none selected)
    #    contributes nothing → NaN lift, 0 days
    s_greedy = np.zeros(len(ser))
    for d, g in ser.groupby("day"):
        idx = g.index[g["age_bucket"] == 0][:7]
        s_greedy[idx] = 1.0
    lb_g, _, nd_g = selection_lift_lb(s_greedy, r, days, buckets)
    assert nd_g == 0 and lb_g != lb_g, (lb_g, nd_g)
    # 5 selected of 8 leaves 3 unselected < 5 → dropped; 3 of 8 leaves 5 ≥ 3 → kept
    s_five = np.zeros(len(ser))
    for d, g in ser.groupby("day"):
        s_five[g.index[g["age_bucket"] == 0][:5]] = 1.0
    assert selection_lift_lb(s_five, r, days, buckets)[2] == 0
    assert selection_lift_lb(s_p, r, days, buckets)[2] == 60
    print("  strata dropping: 7/8 and 5/8 selected → dropped (n_unsel < max(2, n_sel)); 3/8 kept")

    # 9. outcome_series from a real-shaped ledger + sidecar in a temp dir, exclusions counted
    import ledger as LED
    with tempfile.TemporaryDirectory() as d:
        lp, vp = os.path.join(d, "ledger.csv"), os.path.join(d, "band_verdicts.csv")
        t0 = 1_780_000_000.0
        LED.record_rows([{"token": "0xaaa", "symbol": "A", "tier": "B", "band": champ,
                          "event_kind": "first_sighting", "market": {"price_usd": 1.0, "mcap": 1e6, "liq_usd": 5e4},
                          "score": 50.0, "sources_dark": []},
                         {"token": "0xbbb", "symbol": "B", "tier": "B", "band": champ,
                          "event_kind": "first_sighting", "market": {"price_usd": 1e-35, "mcap": 1e6, "liq_usd": 5e4},
                          "score": 50.0, "sources_dark": ["blockscout"]}], alert_ts=t0, path=lp)
        LED.record_rows([{"token": "0xaaa", "symbol": "A", "tier": "A", "band": champ, "fired_band": champ,
                          "event_kind": "promotion", "market": {"price_usd": 2.0, "mcap": 2e6, "liq_usd": 6e4},
                          "score": 75.0, "sources_dark": ["robinx"]}], alert_ts=t0 + 7200, path=lp)
        led = LED.load(lp)
        led.loc[0, "ret_6h"] = 0.5
        led.loc[1, "ret_6h"] = 0.1
        led.loc[2, "ret_6h"] = -0.2
        led.loc[2, "status"] = "suspect"
        LED.save(led, lp)
        store.append_verdicts([{"event_seq": 1, "token": "0xaaa", "alert_ts": t0,
                                "verdicts": {champ: False, planted: True, "ctl_random_band": None}},
                               {"event_seq": 3, "token": "0xaaa", "alert_ts": t0 + 7200,
                                "verdicts": {champ: True, planted: True, "ctl_random_band": False}}], vp)
        ser2, excl, wide = load_real(lp, vp)
        print(f"  outcome_series: kept {len(ser2)} of 3 rows; excluded {excl}")
        assert len(ser2) == 1 and excl["suspect"] == 1 and excl["bad_entry"] == 1
        assert ser2.at[0, "event_seq"] == 1 and ser2.at[0, planted] == 1.0 and ser2.at[0, champ] == 0.0
        assert ser2.at[0, "ctl_random_band"] != ser2.at[0, "ctl_random_band"]
        # the promotion row's age would have been 7200 s (bucket 2) had it matured
        led.loc[2, "status"] = "open"
        LED.save(led, lp)
        ser3, _, _ = load_real(lp, vp)
        assert len(ser3) == 2 and ser3.at[1, "sighting_age_s"] == 7200.0 and ser3.at[1, "age_bucket"] == 2
        assert ser3.at[1, "sources_dark"] == ["robinx"]
        # exclude_dark drops the row whose dark source the champion needs
        reg = B.load_registry()
        srcs = champion_sources(reg, champ)
        print(f"  champion sources for {champ}: {sorted(srcs)}")
        assert srcs == {"blockscout", "rpc"}
        assert champion_sources(reg, "band_dev_score_ge70") == {"robinx"}
        assert champion_sources(reg, "band_graduated_only") == {"geckoterminal"}
        kept, n_drop = exclude_dark(ser3, {"robinx"})
        assert n_drop == 1 and len(kept) == 1
        assert exclude_dark(ser3, srcs)[1] == 0
        # an empty ledger renders 'insufficient (n=0)'
        ser0, excl0 = outcome_series(LED.load(os.path.join(d, "missing.csv")), wide)
        assert len(ser0) == 0
        assert "insufficient (n=0)" in markdown([], [], {"n_events": 0})

    # 10. the table + markdown on the synthetic series
    reg = B.load_registry()
    rows, ctls = table(ser, reg, champ, n_trials=n_tr)
    md = markdown(rows, ctls, {"champion": champ, "n_events": len(ser), "n_days": 60,
                               "n_excluded": {}, "n_trials": n_tr, "fp_champion": 0.02})
    print(md)
    top = [x for x in rows if x["band"] == planted][0]
    assert rows[0]["band"] == champ and top["by_keep"] is True and top["lift_lb"] > 0
    assert all(x["by_keep"] is None for x in ctls) and len(ctls) == 2
    assert not any(x["inert_vs_champion"] for x in rows + ctls)
    assert "median age" in md and f"`{planted}`" in md
    print(f"OK — scorecard.py assertions hold ({time.time() - t_start:.1f}s).")

    if "--markdown" in sys.argv[1:]:
        print("\n=== live scorecard ===")
        ser_live, excl_live, _ = load_real()
        try:
            from selfimprove import champion as CH
            from selfimprove import trials as TR
            champ_live = CH.entry_band()
            n_tr_live = TR.family_count("bands")
        except Exception:
            champ_live, n_tr_live = config.DEFAULT_ENTRY_BAND, len(B.BUILTINS)
        reg_live = B.load_registry()
        ser_live, n_dark = exclude_dark(ser_live, champion_sources(reg_live, champ_live))
        excl_live["dark"] = n_dark
        if len(ser_live):
            rows, ctls = table(ser_live, reg_live, champ_live, n_tr_live)
        else:
            rows, ctls = [], []
        print(markdown(rows, ctls, {"champion": champ_live, "n_events": len(ser_live),
                                    "n_days": int(ser_live["day"].nunique()) if len(ser_live) else 0,
                                    "n_excluded": excl_live, "n_trials": n_tr_live,
                                    "stamp": time.strftime("%Y-%m-%d %H:%M", time.gmtime(time.time()))}))
