"""
The trial counter — selfimprove/trials.json — the one number every Deflated-Sharpe gate
deflates by, and the one file that must only ever grow.

WHY. A loop that proposes changes is itself a trial generator. ~/entry_bot/CLAUDE.md measured
what happens when the search is not counted: deflated Sharpe fell from 0.962 to 0.886 purely
from counting honestly, and a greedy stack over ~70 uncorrected comparisons produced a rule that
died on the first robustness check. signal_lab/registry.py hardcodes n_trials = 50 and so
cannot see its own searching. Here every policy ever scored, every band ever scored and every
nomination ever made is a distinct name in a per-family list; the exit DSR deflates by
len(policies_ever_scored), the entry DSR by len(bands_ever_scored); cumulative_trials is the
sum, informational. Deleting a policy or a band does NOT remove it — you cannot un-look at a
result — so bump() never decrements and family_count() is monotone across the life of the repo.

Writes are atomic (tmp + os.replace, allow_nan=False) because the Sunday jobs and the research
job both write this file and it is committed. No wall-clock: the file carries no timestamps.
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config   # noqa: E402

FAMILIES = {"policies": "policies_ever_scored", "bands": "bands_ever_scored",
            "nominations": "nominations_ever"}
_NOTE = ("Distinct names ever scored per family, plus every nomination. Deflated Sharpe is "
         "deflated by the family's own count (exit: policies_ever_scored; entry: "
         "bands_ever_scored). Lists only grow: deleting a policy or band does NOT remove it "
         "here - you cannot un-look at a result.")


def _empty() -> dict:
    d = {k: [] for k in FAMILIES.values()}
    d["cumulative_trials"] = 0
    d["note"] = _NOTE
    return d


def _clean(obj):
    """NaN/inf -> None so allow_nan=False can never raise on a write."""
    if isinstance(obj, float) and (obj != obj or obj in (float("inf"), float("-inf"))):
        return None
    if isinstance(obj, dict):
        return {k: _clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_clean(v) for v in obj]
    return obj


def _atomic_json(path: str, obj) -> None:
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w") as f:
        json.dump(_clean(obj), f, indent=2, allow_nan=False)
        f.write("\n")
    os.replace(tmp, path)


def load(path: str | None = None) -> dict:
    """The trials state with every family list present. A missing or corrupt file yields the
    empty shape (printed, never raised) — but see bump(): it refuses to shrink a list."""
    path = path or config.TRIALS_PATH
    st = _empty()
    if not os.path.exists(path):
        return st
    try:
        with open(path) as f:
            raw = json.load(f)
    except Exception as exc:
        print(f"  [trials] {path} unreadable ({exc}); treating as empty")
        return st
    if not isinstance(raw, dict):
        return st
    for key in FAMILIES.values():
        vals = raw.get(key) or []
        seen: list = []
        for v in vals:
            s = str(v)
            if s not in seen:
                seen.append(s)
        st[key] = seen
    st["note"] = raw.get("note") or _NOTE
    st["cumulative_trials"] = sum(len(st[k]) for k in FAMILIES.values())
    return st


def family_count(family: str, path: str | None = None) -> int:
    key = FAMILIES[family]
    return len(load(path)[key])


def bump(family: str, names, path: str | None = None) -> int:
    """Add the names not already present to the family's list, recompute cumulative_trials,
    write atomically. Returns the family's new count. Never decrements: names are appended in
    the order given, existing names are untouched, and an empty/duplicate call is a no-op that
    still returns the count."""
    if family not in FAMILIES:
        raise ValueError(f"unknown trial family {family!r}; one of {sorted(FAMILIES)}")
    path = path or config.TRIALS_PATH
    key = FAMILIES[family]
    st = load(path)
    before = list(st[key])
    if isinstance(names, str):
        names = [names]
    added = []
    for n in names or []:
        s = str(n).strip()
        if s and s not in st[key]:
            st[key].append(s)
            added.append(s)
    assert st[key][:len(before)] == before, "a trial list may never shrink or reorder"
    st["cumulative_trials"] = sum(len(st[k]) for k in FAMILIES.values())
    if added or not os.path.exists(path):
        _atomic_json(path, st)
    return len(st[key])


if __name__ == "__main__":
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "trials.json")
        assert family_count("bands", p) == 0 and load(p)["cumulative_trials"] == 0
        n = bump("bands", ["band_a_strict", "band_no_age"], p)
        assert n == 2
        n = bump("bands", ["band_no_age", "band_a_strict"], p)         # idempotent, order kept
        assert n == 2 and load(p)["bands_ever_scored"] == ["band_a_strict", "band_no_age"]
        n = bump("bands", "band_score60", p)                           # a bare string works
        assert n == 3
        assert bump("policies", ["sell_15m"], p) == 1
        assert bump("nominations", ["band_score60@42"], p) == 1
        st = load(p)
        print(json.dumps({k: v for k, v in st.items() if k != "note"}, indent=1))
        assert st["cumulative_trials"] == 5
        assert family_count("bands", p) == 3 and family_count("policies", p) == 1
        # growth only: a re-load after a bump never shows fewer names
        bump("bands", ["band_holders500"], p)
        assert family_count("bands", p) == 4 and load(p)["cumulative_trials"] == 6
        # the on-disk file is strict JSON with the note and no tmp left behind
        raw = json.load(open(p))
        assert raw["note"] and not [f for f in os.listdir(d) if f.endswith(".tmp")]
        try:
            bump("clf", ["x"], p)
            raise SystemExit("unknown family must raise")
        except ValueError:
            pass
        # a corrupt file is treated as empty on read, and bump rewrites it whole
        with open(p, "w") as f:
            f.write("{nope")
        assert load(p)["cumulative_trials"] == 0
    print("live file:", {k: (len(v) if isinstance(v, list) else v)
                         for k, v in load().items() if k != "note"})
    print("OK — trials.py assertions hold.")
