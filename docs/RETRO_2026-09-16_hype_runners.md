# Retrospective — the three hype runners of 2026-09-16

Descriptive. **n = 3**, the three were chosen *because they ran*, and **no rule is derived** from
them — here or anywhere downstream.

Every number below is pinned to `origin/main` at **bcb7fa9** (`data/ledger.csv`: 775 event rows,
759 distinct tokens) and to GeckoTerminal OHLCV fetched on 2026-09-17 through
`selfimprove/paths.py`. Two data-quality columns travel with every price statement: **`res`**, the
bar resolution the series came back at, and **`pages`**, how many backward OHLCV pages it took to
reach the alert. An hour-long bar cannot resolve a token that peaks four minutes in, and a row
that needed page 2 was invisible to this lab until 2026-09-17.

## What this cannot tell you

1. **The sample is the outcome.** These three tokens are here because they went up. Selecting on
   the dependent variable produces a set in which every "predictor" looks predictive. Nothing
   below is a base rate and nothing below can be turned into one.
2. **No GMGN field was observed at any of their sightings.** The Trenches feed and the wallet-tag
   second opinion arrived on 2026-09-13; none of these three addresses appears anywhere in this
   repo's GMGN cache (checked: 0 of 3). Anything said here about GMGN columns is said about
   *other* tokens, in §6, as a base-rate panel.
3. **n = 3, on one day, in one market regime.** The project's floor for a bound is
   `MIN_BOOTSTRAP_CLUSTERS = 12` day-clusters. Three tokens on one afternoon is one cluster.
4. **No rule is derived here.** Every entry band and exit policy in this system is pre-registered,
   counted as a trial forever, and judged forward-only by a gate with its own negative controls. A
   retrospective cannot promote anything, and this one does not try. The one exit rule simulated
   below is a dict handed straight to `policies.simulate`; it is **not registered and not a
   trial**.

## The three tokens, minute by minute

Measured on the **paged 1-minute** path. "ATH ×" is the highest bar high divided by the **first
candle's open at that resolution**, and "min to peak" counts from that first candle.

| token | res | pages | first candle (UTC) | min to peak | ATH × from first open | hour-1 share of lifetime volume | ledger row |
|---|---|---|---|---|---|---|---|
| FOMOPAD `0x33e9…977b` | 1m | 2 | 2026-09-16 14:47 | +29 | 32.4× (1.59734e-03 / 4.92561e-05) | 83 % ($3.86M of $4.67M) | yes — `event_seq` 621, tier B |
| LITVM `0x4f10…9bb4` (the real one) | 1m | 1 | 2026-09-16 15:18 | +17 | 60.2× (2.96453e-03 / 4.92046e-05) | 99 % ($5.13M of $5.18M) | **not a ledger row** |
| DYNABLOCKS `0x6d10…1e18` (#2, Bankr, RBLX-quoted) | 1m | 1 | 2026-09-16 15:46 | +8 | 20.0× (4.07809e-04 / 2.03746e-05) | 89 % ($0.97M of $1.08M) | **not a ledger row** |

Where they stood at the last bar (2026-09-17 ~18:00 UTC): FOMOPAD 6.01e-05 — **−96.2 % from its
ATH**, still 1.22× its first open; LITVM 1.60e-05 — **−99.5 % from its ATH**, 0.33× its first open;
DYNABLOCKS #2 7.18e-05 — **−82.4 % from its ATH**, 3.52× its first open. The dominant DYNABLOCKS
contract (`0x0397…cb72`, created 15:47:07Z) is not priced here: no pool id was pinned for it, and
it too has no ledger row.

**The multiple depends on the resolution, which is why `res` is a column.** FOMOPAD's 15-minute
series opens its first candle at 5.374e-05 and gives 29.7×; the 1-minute series reaches one bucket
further back, opens at 4.92561e-05, and gives 32.4×. Same peak price (1.59734e-03), different
denominator. Neither is wrong; quoting one without the resolution would be.

**And `pages` is not decoration.** Measured on 2026-09-17, FOMOPAD's unpaged `limit=1000` minute
page covered 09-16 22:21Z → 09-17 18:06Z — it *starts 7.5 hours after the 14:51:50Z alert*. The
second page (`before_timestamp=1789597260`) covers 14:47Z → 22:20Z and contains it. Before paging
existed, this row scored `no_cover`: the lab would have reported that a token which traded $3.9M
in its first hour had no price.

### FOMOPAD — the only one of the three with a ledger row

Entries are the open of the first 1-minute bar at or after the offset: **+0 min = 2.90875e-04**,
+5 min = 4.57273e-04, +15 min = 8.53415e-04. Net return per $1 after the round-trip cost model
(0.0340 at $10 positions), shown as **pessimistic / optimistic** within-bar reading — never the
optimistic side alone.

| policy | +0 min | +5 min | +15 min |
|---|---|---|---|
| hold_to_end | −0.800 / −0.800 | −0.873 / −0.873 | −0.932 / −0.932 |
| sell_15m | +2.160 / +2.160 | +1.847 / +1.847 | −0.074 / −0.074 |
| sell_30m | +1.716 / +1.716 | +0.449 / +0.449 | −0.641 / −0.641 |
| sell_1h | −0.130 / −0.130 | −0.499 / −0.499 | −0.820 / −0.820 |
| sell_2h | −0.509 / −0.509 | −0.696 / −0.696 | −0.853 / −0.853 |
| sell_3h | −0.601 / −0.601 | −0.676 / −0.676 | −0.883 / −0.883 |
| sell_6h | −0.634 / −0.634 | −0.741 / −0.741 | −0.825 / −0.825 |
| stop_30 | −0.324 / −0.324 | −0.324 / −0.324 | −0.324 / −0.324 |
| stop_50 | −0.517 / −0.517 | −0.517 / −0.517 | −0.517 / −0.517 |
| cfg_ladder | +1.223 / +1.223 | +0.029 / +0.029 | −0.932 / −0.932 |
| cfg_ladder_stop | +1.294 / +1.294 | +0.208 / +0.208 | −0.517 / −0.517 |
| trail_30 | +0.126 / +0.024 | +0.045 / −0.034 | +0.143 / +0.037 |
| trail_50 | +0.173 / +0.173 | −0.254 / −0.254 | −0.184 / −0.184 |
| tp2_half_trail30 | +0.126 / +0.024 | +0.045 / −0.034 | +0.143 / +0.037 |
| tp2_half_stop50_6h | +0.208 / +0.208 | +0.208 / +0.208 | −0.517 / −0.517 |
| cfg_ladder_trail30_6h | +0.126 / +0.024 | +0.045 / −0.034 | +0.143 / +0.037 |
| `ctl_exit_immediately` | +0.024 / +0.024 | +0.158 / +0.158 | +0.077 / +0.077 |
| `ctl_random_exit` | −0.614 / −0.614 | −0.824 / −0.824 | −0.910 / −0.910 |

Read the last two rows first. `ctl_exit_immediately` — sell at the close of the entry bar, the
control that is supposed to be worthless — beats eleven of the sixteen policies at +0 min and
every one of them at +15 min. On one path, on one day, a negative control wins. That is the whole
argument of §1 in one line.

What ordering buys, and only ordering: on this path the **1.5× take-profit was touched at the
14:53Z bar and the −50 % stop not until 15:40Z — take-profit first**. No horizon snapshot contains
that fact; `max_ret_seen` and `min_ret_seen` are both realised here, so without the ordered path
every one of these policies is simultaneously a winner and a loser. An unregistered illustration,
`{ladder [[1.5, 1.0]], stop 0.5}`, returns **+0.449** on this path — identically at 1-minute and at
15-minute bars, because one rung and one stop are coarse enough to survive the resolution change.

`trail_30` is the counter-example: **+0.126 / +0.024 at 1m, but +1.235 / −0.034 at 15m** — a
1.27-wide bracket. A trailing rule read on 15-minute bars is barely a measurement at all.

## What the screener saw

**FOMOPAD** was sighted at `pair_age_min` 4.07 through the `gt_new_pools` feed and ledgered as
`event_seq` 621, `event_kind` `first_sighting`, **tier B** — the champion band `band_volume_early`
said False, `champion_reason` "buys below 2× sells in hour 1": 1,273 buys against 1,137 sells =
1.12×, against `BAND_VE_BUY_SELL_RATIO = 2.0`. The token cleared the hard gates and failed the
alert band, which is exactly what B means. No alert was sent, and the B arm is the gate-matched
control by construction, so this is a recorded negative, not a miss of the apparatus.

The recorded entry price was 3.463e-04 and the stored `max_ret_seen` is **+3.5 % (1.035×)**. The
true 1-minute high over that same entry is 1.59734e-03 = **4.61×**. Nothing is wrong with the
ledger: `max_ret_seen` is a spot sample of Dexscreener `priceUsd` taken on the run grid
(`sources/dexscreener.py`), so it is **a floor on the peak, never an estimate of it** — and the
next scan after the 14:51 run did not land until 18:29 (+13,082 s), long after the 15:16Z high.
The same effect, measured on the whole sparse-era cohort (rows alerted after 2026-09-14T15:50Z):
the ledger understates the true 24 h peak on **33 of 41** rows with GT coverage, and the ≥1.5×
rate reads 24 % from the ledger against 49 % from the paths. On the dense-era control day (09-13)
the median true/ledger ratio is 1.09×. The fix is the scan cadence, not the statistic.

**The real LITVM and both DYNABLOCKS contracts were never ledgered at all** — their creation
blocks fell inside skipped discovery catch-up ranges. What the ledger does hold under the name
"LITVM" is `0xc997…567a` at `event_seq` 628: a clone, with a frozen quote, for which GeckoTerminal
has 7 bars in total and none after its alert, so `fetch_path` returns `no_cover`. A retrospective
that scored that row would be scoring a different token with the same ticker.

## Top-10 holder share depends on who you ask

| token | top-10 share, Blockscout at sighting | top-10 share, GeckoTerminal later |
|---|---|---|
| FOMOPAD | 14.9 % | 39.2 % (22 h later) |
| DYNABLOCKS | — | 71 % (now) |
| LITVM | — | 59 % (now) |

The hard gate reads Blockscout. A 24-point gap between two sources on the same token means the
concentration figure is a property of *the source and the instant*, not of the token, and that any
threshold placed on it is a threshold on a measurement process. This is context for reading §6,
not an argument for changing a gate.

## The clone denominator

Each of these names is not one token. On this chain the same three names carry **13, 6 and 11**
distinct same-name tokens, at roughly $30k liquidity, $25–38k lifetime volume, and buys/sells of
1.4–1.5×. Those clones **clear the hard gates** — `LIQ_FLOOR_USD = $10,000` and
`MIN_VOL_H24_USD = $20,000` — and would be ledgered like any other survivor.

What they do not clear is the champion band's hour-1 volume clause: `BAND_VE_MIN_VOL_H1_USD =
$50,000` at `BAND_VE_MAX_AGE_MIN = 30`. The three runners did $0.97M–$5.13M in hour one. So at
≤ 15 minutes of age, **the $50k hour-1 volume floor is the clone filter** — the name, the theme and
the launchpad are shared across the swarm; the first hour's flow is not. Stated as a mechanism
worth knowing, with the swarm sizes attached; it is not a claim that the floor is at the right
value, which only the entry gate can decide.

## The cached-GMGN ∩ ledger panel

Recomputed for this document against the cache in this repo and the ledger at **bcb7fa9**:

| quantity | value |
|---|---|
| distinct addresses across `cache/gmgn_info_*.json` + `cache/gmgn_trenches_*.json` | 188 (176 from Trenches alone) |
| of those, present in the ledger | **26**, on 26 rows |
| their tier mix | 26 / 26 tier **B** |
| `ret_6h` ≥ 2× | **0 / 26** (median `ret_6h` −0.340) |
| `max_ret_seen` ≥ 2× | 2 / 26 — and that column is a floor (§3) |
| Almost-bonded (`near_completion`): `top_10_holder_rate` < 0.20 | 59 / 60 — the column does not discriminate |
| Almost-bonded: median `progress` | **0.0266** |
| Almost-bonded: `visiting_count` non-zero | 20 / 60, max 5 (Migrated reaches 51) |
| the three runners' addresses in the GMGN cache | 0 / 3 |

Two things follow, both about instrumentation rather than about markets. The pre-declared band
`band_almost_bonded` requires `BAND_AB_MIN_PROGRESS = 0.5`, so on this snapshot it **rejects
essentially the entire column it is named after** (median progress 0.027) — a pre-declared null
that the entry scorecard's inert check should be expected to flag, exactly as it flagged
`band_no_age`. And `top_10_holder_rate` below 0.20 on 59 of 60 rows carries no information at this
threshold. This is **one snapshot from one Trenches pull**: a base-rate panel for reading the
columns, not a test of anything, and 26 rows across a handful of days is far below any bound.

## Refuses to claim

This document does **not** claim, and no later edit should let it claim:

- **no hit rate** — not for the champion band, not for hour-1 volume, not for anything. Three
  winners with no denominator is not a rate.
- **no lift** — nothing here is compared against a matched unselected pool, so no selection effect
  is measured or implied.
- **no threshold** — not $50k hour-1 volume, not 2× buy/sell flow, not a top-10 share, not a
  holding time. The numbers in §5 describe what these three did; they are not proposed cut-points.
- **no exit policy recommendation.** The FOMOPAD table is one path. A negative control wins on it.
  `sell_15m` returning +2.160 at +0 min is a fact about one token and evidence of nothing.
- **no "X predicts Y" sentence** — not visiting counts, not holder counts, not buyer/seller ratios,
  not launchpad identity.
- **no bound of any kind.** For scale: the 2026-09-17 backfill of the recent cohort priced 93 rows
  at 1-minute resolution across **one** alert day. One cluster. `MIN_BOOTSTRAP_CLUSTERS` is 12, and
  a bound from fewer than that is not a bound.

## Where the test actually is

The only thing that can move this system is a **pre-registered** band or policy, judged
**forward-only** by the entry and exit gates, deflated by every trial the family has ever run, with
the negative controls as the kill switch and the paper scorecard as the judge. Ordered price paths
change what those gates can *see* — that a stop was hit before a take-profit, or the reverse — and
nothing else. The honest prior is unchanged: this class of trade is **−EV**, and "NO CHANGE" for
months is the expected outcome.

*Not financial advice. Alert-only; this system holds no keys and moves no funds.*
