"""
The run.py hook for the entry lab: one normalized feature dict per survivor, every registered
band's verdict, the tier, the event decision and the 24 h watchlist.

WHAT run.py does with this module, in order (after the second hard_gates pass + soft_score):
    feat      = build_feat(token, market, safety, score, first_sighting, sighting_age_s)
    verdicts  = evaluate_bands(feat, registry, champion)
    tier      = tier_for(gates_ok, verdicts, champion)
    reason    = champion_reason(feat, registry, champion, verdicts)     # B cards / band_na_reason
    events    = decide_events(survivors, ledger.index(led), now_s, champion, registry)
    seqs      = ledger.record_rows(events, alert_ts=now_s, plan_name=...)
    store.append_verdicts([{event_seq, token, alert_ts, verdicts} for the event rows])
    state     = watch_update(load_watchlist(), survivors, rejected, ledger.index(led), now_s)

WHY EVENTS. solana_screener froze the tier at first sighting and 39 of 42 real A-tier alerts sat
in its control arm. Here every band flip inside BAND_WATCH_WINDOW_S opens its OWN ledger row at
its OWN entry price (first_sighting -> band_fire -> promotion), so "what was alerted, at the
price it was alerted" and "passed hard gates, failed the champion at that instant" are exact by
construction, and every band is compared on identical forward returns from the sidecar.
Promoted B rows stay B (intention-to-treat); nothing here filters on promoted_ts.

WHY THE WATCHLIST. solana promotions arrived a median 8.9 h (max 26.2 h) after first sighting;
a survivor must be re-screened inside the window or maturation bands can never fire. The
watchlist is the cost-bounded list of tokens to re-enrich; eviction is by window, by an A row,
or by WATCH_MAX_GATE_FAILS consecutive hard-gate failures.

No wall-clock: now_s comes from run.py's single time.time(). No randomness. Never raises on bad
data in the runtime path.
"""
from __future__ import annotations

import json
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import config                                   # noqa: E402
import screen                                   # noqa: E402
from selfimprove.entry_lab import bands as B    # noqa: E402

tier_for = B.tier_for
INVERSE = "ctl_inverse_band"
EVENT_KINDS = ("first_sighting", "band_fire", "promotion")


# ── feature normalisation ──────────────────────────────────────────────────────────
def _clean_scalar(v):
    """NaN / inf / pd.NA / numpy scalars -> plain Python or None. Never raises."""
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return None if (v != v or v in (math.inf, -math.inf)) else v
    if isinstance(v, str):
        return v
    # numpy scalar / pandas NA / anything with .item()
    try:
        import pandas as pd
        if v is pd.NA or v is pd.NaT:
            return None
    except Exception:
        pass
    item = getattr(v, "item", None)
    if callable(item):
        try:
            return _clean_scalar(item())
        except Exception:
            return None
    try:
        if v != v:            # a NaN-like object
            return None
    except Exception:
        pass
    return v


def _clean(v):
    if isinstance(v, dict):
        return {str(k): _clean(x) for k, x in v.items()}
    if isinstance(v, (list, tuple, set)):
        return [_clean(x) for x in v]
    return _clean_scalar(v)


def build_feat(token: str, market: dict, safety: dict, score, first_sighting: bool,
               sighting_age_s) -> dict:
    """EXACTLY config.FEATURE_FIELDS keys, None when unknown. market and safety are flat
    dicts (sources/dexscreener.py, sources/safety.py); runtime keys are set here."""
    m, s = market or {}, safety or {}
    feat = {k: None for k in config.FEATURE_FIELDS}
    for src in (m, s):
        for k, v in src.items():
            if k in feat:
                feat[k] = _clean(v)
    feat["token"] = str(token).lower() if token is not None else None
    feat["score"] = _clean_scalar(score)
    feat["first_sighting"] = bool(first_sighting)
    feat["sighting_age_s"] = _clean_scalar(sighting_age_s)
    if feat["sources_dark"] is None:
        feat["sources_dark"] = []
    return feat


# ── verdicts ───────────────────────────────────────────────────────────────────────
def evaluate_bands(feat: dict, registry, champion: str) -> dict:
    """{name: True | False | None} for EVERY registry name. A listed-but-unloaded band is None.
    ctl_inverse_band is the champion's complement (None when the champion is None)."""
    out: dict = {}
    names = registry.names() if registry is not None else []
    for name in names:
        if name == INVERSE:
            continue
        spec = registry.get(name)
        out[name] = spec.verdict(feat) if spec is not None else None
    if INVERSE in names:
        cv = out.get(champion)
        out[INVERSE] = None if cv is None else (not cv)
    return out


def champion_reason(feat: dict, registry, champion: str, verdicts: dict) -> str:
    """Why the champion did not fire: '' when it did; 'NA: <fields> unknown' when None; the
    band's explain() otherwise. Shown on B cards as 'short of <champion>: ...'."""
    v = (verdicts or {}).get(champion)
    if v is True:
        return ""
    spec = registry.get(champion) if registry is not None else None
    if spec is None:
        return f"NA: {champion} is not a registered band"
    try:
        if v is None:
            miss = spec.missing(feat)
            if miss:
                return "NA: " + ", ".join(miss) + " unknown"
            text = spec.explain(feat)
            return text if text.startswith("NA") else "NA: " + (text or "a check had unknown input")
        return spec.explain(feat) or f"short of {champion}"
    except Exception as exc:
        return f"short of {champion} (explain failed: {type(exc).__name__})"


# ── events ─────────────────────────────────────────────────────────────────────────
def _inside_window(info: dict, now_s: float) -> bool:
    ts = info.get("first_sighting_ts")
    if ts is None:
        return False
    return 0.0 <= (now_s - float(ts)) <= config.BAND_WATCH_WINDOW_S


def decide_events(survivors: list, ledger_index: dict, now_s: float, champion: str,
                  registry, prior_true: dict | None = None) -> list:
    """Return the survivor dicts that open a ledger row this run, augmented with event_kind,
    fired_band, prior_event_seq (None; the ledger fills it), tier and band (= champion).

      token not in the ledger                      -> first_sighting (tier from tier_for)
      champion True, no A row, inside the window   -> promotion (tier A, fired_band=champion)
      a non-control band newly True, not fired yet,
        n < BAND_MAX_EVENTS_PER_TOKEN, in window   -> band_fire (tier B, alphabetically first)
      else                                          -> nothing

    A capped token gets nothing more EXCEPT a promotion while it has no A row. The champion
    only ever opens a promotion, never a band_fire. `prior_true` ({token: set(bands)}, optional)
    lets run.py exclude bands that were already True at an earlier event (from the watchlist's
    bands_true) so a band that was True at first sighting is not re-fired."""
    ledger_index = ledger_index or {}
    prior_true = prior_true or {}
    controls = set(registry.controls()) if registry is not None else set()
    out = []
    for s in survivors or []:
        try:
            token = str(s["token"]).lower()
            verdicts = s.get("verdicts") or {}
            gates_ok = bool(s.get("gates_ok", True))
            info = ledger_index.get(token)
            ev = dict(s)
            ev["token"] = token
            ev["band"] = champion
            ev["prior_event_seq"] = None
            if info is None:
                ev["event_kind"] = "first_sighting"
                ev["fired_band"] = None
                ev["tier"] = tier_for(gates_ok, verdicts, champion)
                out.append(ev)
                continue
            if not gates_ok:
                continue
            n = int(info.get("n") or 0)
            has_a = bool(info.get("has_a"))
            fired = set(info.get("fired") or ()) | set(prior_true.get(token) or ())
            inside = _inside_window(info, now_s)
            if verdicts.get(champion) is True and not has_a and inside:
                ev["event_kind"] = "promotion"
                ev["fired_band"] = champion
                ev["tier"] = "A"
                out.append(ev)
                continue
            if n >= config.BAND_MAX_EVENTS_PER_TOKEN or not inside:
                continue
            newly = sorted(b for b, v in verdicts.items()
                           if v is True and b != champion and b not in controls and b not in fired)
            if newly:
                ev["event_kind"] = "band_fire"
                ev["fired_band"] = newly[0]
                ev["tier"] = "B"
                out.append(ev)
        except Exception as exc:
            print(f"  [runtime] decide_events skipped {s.get('token')!r}: {type(exc).__name__}: {exc}")
    return out


# ── watchlist ──────────────────────────────────────────────────────────────────────
def _misses_of(s: dict) -> int:
    if s.get("misses") is not None:
        try:
            return int(s["misses"])
        except Exception:
            pass
    try:
        return sum(1 for v in screen.hc_checks(s.get("feat") or {}).values() if v is not True)
    except Exception:
        return 99


def watch_update(state: dict, survivors: list, rejected_tokens, ledger_index: dict,
                 now_s: float) -> dict:
    """Mutates and returns the watchlist {token: {first_sighting_ts, symbol, last_gates_ok,
    gate_fails, refreshed_ts, safety_ts, misses, bands_true}}. Survivors are added/refreshed;
    rejected watched tokens count a consecutive gate failure and are evicted at
    WATCH_MAX_GATE_FAILS; tokens past BAND_WATCH_WINDOW_S or with an A row are evicted."""
    state = state if isinstance(state, dict) else {}
    ledger_index = ledger_index or {}
    for s in survivors or []:
        try:
            token = str(s["token"]).lower()
            info = ledger_index.get(token) or {}
            w = state.get(token) or {}
            fs = w.get("first_sighting_ts")
            if fs is None:
                fs = info.get("first_sighting_ts")
            if fs is None:
                fs = now_s
            bands_true = set(w.get("bands_true") or ())
            bands_true |= {b for b, v in (s.get("verdicts") or {}).items() if v is True}
            state[token] = {
                "first_sighting_ts": float(fs), "symbol": s.get("symbol") or w.get("symbol") or "?",
                "last_gates_ok": True, "gate_fails": 0, "refreshed_ts": float(now_s),
                "safety_ts": (s.get("safety_ts") if s.get("safety_ts") is not None
                              else w.get("safety_ts")),
                "misses": _misses_of(s), "bands_true": sorted(bands_true),
            }
        except Exception as exc:
            print(f"  [runtime] watch_update skipped {s.get('token')!r}: {type(exc).__name__}: {exc}")
    for t in rejected_tokens or ():
        token = str(t).lower()
        w = state.get(token)
        if w is None:
            continue
        w["last_gates_ok"] = False
        w["gate_fails"] = int(w.get("gate_fails") or 0) + 1
        w["refreshed_ts"] = float(now_s)
        if w["gate_fails"] >= config.WATCH_MAX_GATE_FAILS:
            del state[token]
    for token in list(state):
        w = state[token]
        fs = w.get("first_sighting_ts")
        past = fs is None or (now_s - float(fs)) > config.BAND_WATCH_WINDOW_S
        if past or bool((ledger_index.get(token) or {}).get("has_a")):
            del state[token]
    return state


def watch_due(state: dict, ledger_index: dict, now_s: float) -> list:
    """Tokens to re-screen this run: inside the window, no A row; fewest champion misses first
    (closest to A), then least recently refreshed, then token for determinism."""
    ledger_index = ledger_index or {}
    due = []
    for token, w in (state or {}).items():
        try:
            fs = w.get("first_sighting_ts")
            if fs is None or not (0.0 <= now_s - float(fs) <= config.BAND_WATCH_WINDOW_S):
                continue
            if (ledger_index.get(token) or {}).get("has_a"):
                continue
            due.append((int(w.get("misses") if w.get("misses") is not None else 99),
                        float(w.get("refreshed_ts") or 0.0), token))
        except Exception:
            continue
    due.sort()
    return [t for _m, _r, t in due]


def load_watchlist(path: str | None = None) -> dict:
    path = path or config.WATCHLIST_PATH
    if not os.path.exists(path):
        return {}
    try:
        with open(path) as f:
            raw = json.load(f)
        return raw if isinstance(raw, dict) else {}
    except Exception as exc:
        print(f"  [runtime] watchlist {path} unreadable ({exc}); starting empty")
        return {}


def _nan_to_none(obj):
    if isinstance(obj, float) and (obj != obj or obj in (math.inf, -math.inf)):
        return None
    if isinstance(obj, dict):
        return {k: _nan_to_none(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_nan_to_none(v) for v in obj]
    return obj


def save_watchlist(state: dict, path: str | None = None) -> None:
    path = path or config.WATCHLIST_PATH
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w") as f:
        json.dump(_nan_to_none(state), f, indent=1, sort_keys=True, allow_nan=False)
    os.replace(tmp, path)


# ── smoke test (offline) ───────────────────────────────────────────────────────────
if __name__ == "__main__":
    import tempfile
    reg = B.load_registry()
    champ = config.DEFAULT_ENTRY_BAND
    clean = B.clean_fixture()
    tok = clean["token"]
    market = {k: clean[k] for k in ("price_usd", "liq_usd", "mcap", "fdv", "vol_h1", "vol_h6",
                                     "vol_h24", "buys_h1", "sells_h1", "buys_h24", "sells_h24",
                                     "price_chg_h1", "pair_age_min", "dex")}
    safety = {k: v for k, v in clean.items() if k not in market
              and k not in ("token", "score", "first_sighting", "sighting_age_s")}
    safety["sources_used"] = ["rpc"]; safety["pass"] = 2          # extra keys are dropped

    # 1. build_feat: exact key set; NaN/inf/pd.NA/numpy -> None; extras dropped
    import numpy as np
    import pandas as pd
    m_bad = dict(market, liq_usd=float("nan"), vol_h24=float("inf"), buys_h1=np.int64(7),
                 price_chg_h1=np.float64("nan"))
    s_bad = dict(safety, top10_pct=pd.NA, dev_pct=np.float32(1.5), sources_dark=["blockscout"])
    feat = build_feat(tok.upper(), m_bad, s_bad, float("nan"), True, np.float64(12.0))
    assert set(feat) == set(config.FEATURE_FIELDS), set(feat) ^ set(config.FEATURE_FIELDS)
    assert feat["liq_usd"] is None and feat["vol_h24"] is None and feat["top10_pct"] is None
    assert feat["price_chg_h1"] is None and feat["score"] is None
    assert feat["buys_h1"] == 7 and type(feat["buys_h1"]) is int
    assert abs(feat["dev_pct"] - 1.5) < 1e-6 and type(feat["dev_pct"]) is float
    assert feat["token"] == tok and feat["sighting_age_s"] == 12.0 and feat["first_sighting"] is True
    assert feat["sources_dark"] == ["blockscout"] and "pass" not in feat
    assert build_feat(tok, {}, {}, None, False, None)["sources_dark"] == []
    print("build_feat ok: NaN/inf/pd.NA -> None; keys == FEATURE_FIELDS")

    good = build_feat(tok, market, safety, clean["score"], True, 0.0)
    assert good == clean, [k for k in clean if good[k] != clean[k]]
    verdicts = evaluate_bands(good, reg, champ)
    assert set(verdicts) == set(reg.names())
    assert verdicts[champ] is True and verdicts[INVERSE] is False
    v_na = evaluate_bands(dict(good, score=None), reg, champ)
    assert v_na[champ] is None and v_na[INVERSE] is None
    assert champion_reason(good, reg, champ, verdicts) == ""
    r_na = champion_reason(dict(good, score=None), reg, champ, v_na)
    assert r_na.startswith("NA: ") and "score" in r_na, r_na
    low = dict(good, score=50.0)
    r_low = champion_reason(low, reg, champ, evaluate_bands(low, reg, champ))
    assert "score below" in r_low, r_low
    print(f"evaluate_bands: {verdicts}\nchampion_reason (NA): {r_na!r}  (low): {r_low!r}")
    assert tier_for(True, verdicts, champ) == "A" and tier_for(True, v_na, champ) == "B"

    # 2. the 3-run scenario: B sighting -> still B -> promotion  ==  [first_sighting], [], [promotion]
    def surv(feat, verdicts, extra=None):
        d = {"token": feat["token"], "symbol": "DEMO", "gates_ok": True, "verdicts": verdicts,
             "feat": feat, "market": market, "score": feat["score"], "gates": {},
             "sources_dark": [], "deployer": "0xdev", "url": "https://example.invalid"}
        d.update(extra or {})
        return d

    t0 = 1_000_000.0
    fb = dict(good, score=50.0, sighting_age_s=0.0)                  # champion False -> B
    vb = evaluate_bands(fb, reg, champ)
    assert vb[champ] is False
    ev1 = decide_events([surv(fb, vb)], {}, t0, champ, reg)
    assert [e["event_kind"] for e in ev1] == ["first_sighting"] and ev1[0]["tier"] == "B"
    assert ev1[0]["fired_band"] is None and ev1[0]["prior_event_seq"] is None and ev1[0]["band"] == champ
    idx = {tok: {"n": 1, "has_a": False, "first_sighting_ts": t0, "fired": set(), "last_alert_ts": t0}}
    fired_at_sighting = {b for b, v in vb.items() if v is True}
    ev2 = decide_events([surv(dict(fb, sighting_age_s=300.0), vb)], idx, t0 + 300, champ, reg,
                        prior_true={tok: fired_at_sighting})
    assert ev2 == [], ev2
    fa = dict(good, sighting_age_s=7200.0)                            # matured: champion True
    va = evaluate_bands(fa, reg, champ)
    ev3 = decide_events([surv(fa, va)], idx, t0 + 7200, champ, reg, prior_true={tok: fired_at_sighting})
    assert [e["event_kind"] for e in ev3] == ["promotion"] and ev3[0]["tier"] == "A"
    assert ev3[0]["fired_band"] == champ
    print("3-run scenario: [first_sighting], [], [promotion]  ok")
    # without prior_true the bands that were True at sighting (not recorded in fired) re-fire on run 2
    ev2b = decide_events([surv(dict(fb, sighting_age_s=300.0), vb)], idx, t0 + 300, champ, reg)
    assert [e["event_kind"] for e in ev2b] == ["band_fire"] and ev2b[0]["tier"] == "B"
    assert ev2b[0]["fired_band"] == min(fired_at_sighting), ev2b[0]["fired_band"]

    # 3. band_fire for a non-champion band on run 2 (nothing True at sighting)
    v0 = {n: (None if n == INVERSE else False) for n in reg.names()}
    v0[INVERSE] = True
    ev_a = decide_events([surv(fb, v0)], {}, t0, champ, reg)
    assert ev_a[0]["event_kind"] == "first_sighting"
    v1 = dict(v0, band_graduated_only=True, band_dev_score_ge70=True, ctl_random_band=True)
    ev_b = decide_events([surv(fb, v1)], idx, t0 + 600, champ, reg)
    assert len(ev_b) == 1 and ev_b[0]["event_kind"] == "band_fire" and ev_b[0]["tier"] == "B"
    assert ev_b[0]["fired_band"] == "band_dev_score_ge70", "alphabetically first newly-firing, controls excluded"
    idx2 = {tok: dict(idx[tok], n=2, fired={"band_dev_score_ge70"})}
    ev_c = decide_events([surv(fb, v1)], idx2, t0 + 900, champ, reg)
    assert ev_c[0]["fired_band"] == "band_graduated_only"
    # a control alone never opens a row; the champion never opens a band_fire
    assert decide_events([surv(fb, dict(v0, ctl_random_band=True))], idx, t0 + 600, champ, reg) == []
    idx_a = {tok: dict(idx[tok], has_a=True, fired={champ})}
    only_champ = dict(v0, **{champ: True, INVERSE: False})
    assert decide_events([surv(fa, only_champ)], idx_a, t0 + 600, champ, reg) == [], "has_a: no second promotion"
    ev_after_a = decide_events([surv(fa, va)], idx_a, t0 + 600, champ, reg)
    assert [e["event_kind"] for e in ev_after_a] == ["band_fire"], "other bands may still fire after an A row"
    # outside the window: nothing (neither promotion nor band_fire)
    late = t0 + config.BAND_WATCH_WINDOW_S + 1
    assert decide_events([surv(fa, va), surv(fb, v1)], idx, late, champ, reg) == []
    # gates failed on a known token: nothing
    assert decide_events([surv(fa, va, {"gates_ok": False})], idx, t0 + 600, champ, reg) == []
    print("band_fire ordering / controls / champion / window / gates ok")

    # 4. the cap: n >= BAND_MAX_EVENTS_PER_TOKEN blocks band_fire but not a first promotion
    idx_cap = {tok: dict(idx[tok], n=config.BAND_MAX_EVENTS_PER_TOKEN)}
    assert decide_events([surv(fb, v1)], idx_cap, t0 + 600, champ, reg) == []
    ev_cap = decide_events([surv(fa, va)], idx_cap, t0 + 600, champ, reg)
    assert [e["event_kind"] for e in ev_cap] == ["promotion"]
    idx_cap_a = {tok: dict(idx_cap[tok], has_a=True)}
    assert decide_events([surv(fa, va)], idx_cap_a, t0 + 600, champ, reg) == []
    print(f"cap ({config.BAND_MAX_EVENTS_PER_TOKEN}) ok: band_fire blocked, first promotion allowed")

    # 5. watchlist
    with tempfile.TemporaryDirectory() as d:
        wp = os.path.join(d, "watchlist.json")
        st = load_watchlist(wp)
        assert st == {}
        tok2 = "0x" + "cd" * 20
        st = watch_update(st, [surv(fb, vb), surv(dict(fa, token=tok2), va, {"symbol": "TWO"})],
                          [], {}, t0)
        assert set(st) == {tok, tok2} and st[tok]["first_sighting_ts"] == t0
        assert st[tok]["misses"] >= 1 and st[tok2]["misses"] == 0
        assert st[tok]["bands_true"] == sorted(fired_at_sighting)
        assert watch_due(st, {}, t0 + 60) == [tok2, tok], "fewest misses first"
        st["x"] = {"first_sighting_ts": float("nan"), "misses": 0}
        save_watchlist(st, wp)
        assert '"NaN"' not in open(wp).read() and "NaN" not in open(wp).read()
        st = load_watchlist(wp)
        assert set(st) == {tok, tok2, "x"} and st["x"]["first_sighting_ts"] is None
        # two consecutive gate failures evict; one does not
        st = watch_update(st, [], [tok], {}, t0 + 300)
        assert st[tok]["gate_fails"] == 1 and st[tok]["last_gates_ok"] is False
        st = watch_update(st, [surv(fb, vb)], [], {}, t0 + 600)          # a pass resets the count
        assert st[tok]["gate_fails"] == 0 and st[tok]["first_sighting_ts"] == t0
        st = watch_update(st, [], [tok], {}, t0 + 900)
        st = watch_update(st, [], [tok], {}, t0 + 1200)
        assert tok not in st and "x" not in st, "evicted after WATCH_MAX_GATE_FAILS / NaN sighting"
        # an A row evicts; the window evicts
        st = watch_update(st, [], [], {tok2: {"has_a": True, "first_sighting_ts": t0}}, t0 + 1500)
        assert tok2 not in st
        st = watch_update({}, [surv(fb, vb)], [], {}, t0)
        assert watch_due(st, {}, t0 + config.BAND_WATCH_WINDOW_S) == [tok]
        assert watch_due(st, {}, t0 + config.BAND_WATCH_WINDOW_S + 1) == []
        assert watch_due(st, {tok: {"has_a": True}}, t0 + 10) == []
        st = watch_update(st, [], [], {}, t0 + config.BAND_WATCH_WINDOW_S + 1)
        assert st == {}
        # first_sighting_ts comes from the ledger when the watchlist is rebuilt
        st = watch_update({}, [surv(fb, vb)], [], {tok: {"first_sighting_ts": t0 - 100, "has_a": False}}, t0)
        assert st[tok]["first_sighting_ts"] == t0 - 100
        assert not [f for f in os.listdir(d) if f.endswith(".tmp")]
    print("watchlist ok")
    print("OK — runtime.py assertions hold.")
