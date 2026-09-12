"""
The band-verdict sidecar: data/band_verdicts.csv, long format, append-only.

WHAT. One line per (event_seq, band): `event_seq, token, alert_ts, band, verdict` with verdict
in {1, 0, NA}. Written by run.py right after ledger.record_rows() mints the event_seq, for
EVENT rows only (a survivor that opened no event row this run has no line). Joins back to the
ledger on event_seq (or on (token, alert_ts) — both are written so a rebuilt ledger can still be
matched).

WHY A SIDECAR AND NOT A COLUMN. Adding a band must never touch the ledger schema (jarvis reads
data/ledger.csv by header; ledger.load reindexes to COLUMNS). A long-format sidecar is
append-only, one band more is one line more per event, and it is regenerable from the committed
latest_scan.json history if ever lost. Every band is scored on IDENTICAL forward returns because
every band's verdict is recorded at the SAME instant as the row's entry price — that is what
makes the scorecard a fair comparison and the controls meaningful.

Append-only discipline: header written once, csv module, flush + fsync before returning (the
cloud job commits the file seconds later; a torn line would poison every later pivot).
"""
from __future__ import annotations

import csv
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import config   # noqa: E402

COLUMNS = ["event_seq", "token", "alert_ts", "band", "verdict"]
NA = "NA"


def encode_verdict(v) -> str:
    """True -> '1', False -> '0', anything else (None, NaN, garbage) -> 'NA'."""
    if v is True:
        return "1"
    if v is False:
        return "0"
    return NA


def append_verdicts(rows: list, path: str | None = None) -> int:
    """rows: [{event_seq, token, alert_ts, verdicts: {band: True|False|None}}, ...].
    Writes one line per (event_seq, band), bands in sorted order. Returns lines written.
    Never raises on a bad row — it is skipped with a printed note."""
    path = path or config.BAND_VERDICTS_PATH
    lines = []
    for r in rows or []:
        try:
            seq = int(r["event_seq"])
            token = str(r["token"]).lower()
            ts = float(r["alert_ts"])
            verdicts = r.get("verdicts") or {}
        except Exception as exc:
            print(f"  [store] skipped a verdict row ({type(exc).__name__}: {exc})")
            continue
        for band in sorted(verdicts):
            lines.append([seq, token, repr(ts), str(band), encode_verdict(verdicts[band])])
    if not lines:
        return 0
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    need_header = not os.path.exists(path) or os.path.getsize(path) == 0
    with open(path, "a", newline="") as f:
        w = csv.writer(f)
        if need_header:
            w.writerow(COLUMNS)
        w.writerows(lines)
        f.flush()
        os.fsync(f.fileno())
    return len(lines)


def load_verdicts(path: str | None = None):
    """The long sidecar as a DataFrame (event_seq int, alert_ts float, verdict str in 1/0/NA).
    Empty frame with the right columns when the file is missing."""
    import pandas as pd
    path = path or config.BAND_VERDICTS_PATH
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return pd.DataFrame(columns=COLUMNS)
    df = pd.read_csv(path, dtype=str).reindex(columns=COLUMNS)
    df["event_seq"] = pd.to_numeric(df["event_seq"], errors="coerce")
    df = df.dropna(subset=["event_seq"])
    df["event_seq"] = df["event_seq"].astype(int)
    df["alert_ts"] = pd.to_numeric(df["alert_ts"], errors="coerce")
    df["token"] = df["token"].astype(str).str.lower()
    df["verdict"] = df["verdict"].astype(str).where(df["verdict"].isin(["1", "0"]), NA)
    return df.reset_index(drop=True)


def pivot_verdicts(df):
    """Wide: index event_seq; columns token, alert_ts, then one float column per band with
    1.0 / 0.0 / NaN (NA). A duplicated (event_seq, band) keeps the LAST line written."""
    import pandas as pd
    if df is None or len(df) == 0:
        return pd.DataFrame(columns=["token", "alert_ts"]).rename_axis("event_seq")
    d = df.drop_duplicates(subset=["event_seq", "band"], keep="last").copy()
    d["_v"] = d["verdict"].map({"1": 1.0, "0": 0.0})
    wide = d.pivot(index="event_seq", columns="band", values="_v")
    wide.columns.name = None
    meta = d.drop_duplicates(subset=["event_seq"], keep="last").set_index("event_seq")[["token", "alert_ts"]]
    out = meta.join(wide, how="left").sort_index()
    return out


if __name__ == "__main__":
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "band_verdicts.csv")
        n1 = append_verdicts([
            {"event_seq": 1, "token": "0xABC", "alert_ts": 1_000_000.0,
             "verdicts": {"band_a_strict": False, "ctl_random_band": None, "band_x": True}},
            {"event_seq": 2, "token": "0xdef", "alert_ts": 1_000_300.0,
             "verdicts": {"band_a_strict": True, "ctl_random_band": False, "band_x": float("nan")}},
        ], p)
        n2 = append_verdicts([{"event_seq": "bad", "token": "0x1", "alert_ts": 1.0, "verdicts": {}}], p)
        n3 = append_verdicts([{"event_seq": 3, "token": "0x999", "alert_ts": 1_000_600.0,
                               "verdicts": {"band_a_strict": None}}], p)
        raw = open(p).read().splitlines()
        print("\n".join(raw))
        assert n1 == 6 and n2 == 0 and n3 == 1
        assert raw[0] == ",".join(COLUMNS) and len(raw) == 8, "header once, append-only"
        assert raw.count(",".join(COLUMNS)) == 1
        assert raw[1] == "1,0xabc,1000000.0,band_a_strict,0"
        df = load_verdicts(p)
        assert len(df) == 7 and set(df["verdict"]) == {"1", "0", "NA"}
        assert df["event_seq"].dtype.kind == "i"
        wide = pivot_verdicts(df)
        print(wide)
        assert list(wide.index) == [1, 2, 3]
        assert wide.at[1, "band_a_strict"] == 0.0 and wide.at[2, "band_a_strict"] == 1.0
        assert wide.at[1, "ctl_random_band"] != wide.at[1, "ctl_random_band"], "NA -> NaN"
        assert wide.at[2, "band_x"] != wide.at[2, "band_x"], "NaN verdict encoded as NA"
        assert wide.at[3, "token"] == "0x999"
        assert len(load_verdicts(os.path.join(d, "missing.csv"))) == 0
        assert len(pivot_verdicts(load_verdicts(os.path.join(d, "missing.csv")))) == 0
    print("OK — store.py assertions hold.")
