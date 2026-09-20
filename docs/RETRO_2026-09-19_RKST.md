# Retrospective — $RKST ("rocket strategy"), asked because it "went up so much"

Descriptive. **n = 1**, the token was chosen *because it was believed to have run*, and
**no rule is derived** from it — here or anywhere downstream.

Every number below is pinned to `origin/main` at **7942a6f** (`data/ledger.csv`: 1,412 event rows,
1,346 distinct tokens, earliest `alert_ts` 2026-09-12T12:21:41Z) and to GeckoTerminal OHLCV,
Dexscreener and chain facts fetched **2026-09-19 ~14:25Z** through this repo's own adapters
(`selfimprove/paths.py`, `sources/dexscreener.py`, `sources/safety.py`, `screen.hard_gates`). Two
data-quality columns travel with every price statement: **`res`**, the bar resolution the series
came back at, and **`pages`**, how many backward OHLCV pages it took to reach the launch.

**Token.** `0x8d1612b4b78ebf08cfbf01a04fa270ccbb0509a2`, symbol RKST, name "rocket strategy",
18 decimals, total supply 1e9. Priced on `0x1934f4ed…12e5` (`uniswap-v4-robinhood`, the deepest
**base-leg** pool at $241,563 reserve). The address does **not** end in `1e18`, so it is not a
salt-mined Bankr launch; the token carries 20+ pools including one on `pons-v2-dex`.

## What this cannot tell you

1. **The sample is the outcome, and the question presupposes it.** This token is here because it
   was thought to have gone up. Selecting on the dependent variable produces a set in which every
   "predictor" looks predictive. Nothing below is a base rate and nothing below can be turned into
   one.
2. **Nothing here is causal.** This document can say *when* RKST moved and *what co-occurred with*
   the move. It cannot say *why*, and no sentence below should be read as "X caused Y". The title's
   question is answered only in the weak sense of "here is what the instruments recorded".
3. **n = 1, over 13.7 days, in one market regime.** The project's floor for a bound is
   `MIN_BOOTSTRAP_CLUSTERS = 12` day-clusters. One token is not a cluster.
4. **The screener never saw this token** (§3). Every number here comes from a post-hoc external
   fetch, not from a point-in-time capture on the run grid. There is no `latest_scan.json` feature
   row for RKST and there never was.
5. **No rule is derived here.** Every entry band and exit policy in this system is pre-registered,
   counted as a trial forever, and judged forward-only by a gate with its own negative controls. A
   retrospective cannot promote anything, and this one does not try.

## 1. The premise, as measured

**RKST is not going up. It is down over every window longer than six hours.** Hourly closes on the
deepest base-leg pool; last bar 2026-09-19 13:00Z at **0.00321216** (`res` 1h, `pages` 2).

| window | reference | reference px | → now | change |
|---|---|---|---|---|
| 1h | 09-19 12:00Z | 0.00336043 | 0.00321216 | **−4.4 %** |
| 6h | 09-19 07:00Z | 0.00287085 | 0.00321216 | +11.9 % |
| 24h | 09-18 13:00Z | 0.00407647 | 0.00321216 | **−21.2 %** |
| 48h | 09-17 13:00Z | 0.00305546 | 0.00321216 | +5.1 % |
| 7d | 09-12 13:00Z | 0.00485051 | 0.00321216 | **−33.8 %** |
| full life | 09-05 23:00Z open | 0.00158537 | 0.00321216 | **+102.6 % (2.03×)** |

All-time high **0.0128661** on 09-05 at 23:16Z — now **−75.0 %** from it. All-time low 0.00155709
on 09-17 04:00Z — now +106.3 % from it. Dexscreener's own `priceChange.h24` read **−26.27 %** on
the same fetch.

So the defensible readings of "went up so much" are (a) **+103 % over its full life**, or (b)
**+106 % off the 09-17 low**, neither of which is a move happening now. The ATH is 13.7 days old.

## 2. What actually happened, in three episodes

Measured on the **paged 1-minute** path (`res` 1m, `pages` 4, 2,964 bars, 09-05 23:07Z →
09-19 13:58Z; the series reaches the launch, so nothing here rests on a truncated window).

| # | when | move | volume |
|---|---|---|---|
| 1 | 09-05 23:07Z → **23:16Z** | **6.05×** in **9 minutes** (0.00212699 → 0.0128661) | hour-1 **$1,141,533** |
| 2 | 09-05 23:00Z low → 09-07 14:00Z | 7.20× over 39 h (0.00158537 → 0.0114136) | days 1–3 $2,469,153 |
| 3 | 09-11 00:00Z → 18:00Z | 3.06× intraday (0.00265324 → 0.00811848) | day $162,017 |

**The multiple depends on the resolution, which is why `res` is a column.** The launch hour reads
**8.12×** against the *hourly* open (0.00158537) and **6.05×** against the *minute* open
(0.00212699). Same peak (0.0128661), different denominator. Neither is wrong; quoting one without
the resolution would be.

**Volume is front-loaded to a degree that dominates everything else about this token.** Lifetime
volume $2,978,930, of which:

| window | volume | share of lifetime |
|---|---|---|
| hour 1 | $1,141,533 | **38.3 %** |
| day 1 | $1,713,390 | **57.5 %** |
| days 1–3 | $2,469,153 | **82.9 %** |
| last 24 h | $24,238 | **0.8 %** |

Whatever happened to RKST happened on 2026-09-05 through 09-07 and, to a much smaller extent, on
09-11. The last 24 hours are 0.8 % of its lifetime flow, on 89 buys against 124 sells.

## 3. What the screener saw: nothing, and that is not a miss

RKST appears in **no** file this system owns — not `data/ledger.csv`, not `band_verdicts.csv`, not
`seen.json` / `recheck.json` / `watchlist.json`, not `latest_scan.json`, not the live book, not the
GMGN cache.

The reason is dates, not a discovery bug. RKST's first pair was created **2026-09-05 22:15:36Z**.
The ledger's earliest `alert_ts` is **2026-09-12 12:21:41Z** — the screener was rebuilt on 09-12 and
its block cursor has never pointed anywhere near 09-05. **RKST predates the system by 6.6 days.**
This is categorically different from the 09-16 retrospective's LITVM and DYNABLOCKS, which fell
inside *skipped* catch-up ranges while the system was running; RKST was never in scope at all.

Consequence for reading §4: those gate readings are **today's** facts on a 13.7-day-old token. They
are not, and cannot be reconstructed as, what the gates would have said at 23:07Z on 09-05.

## 4. Today's gate reading (2026-09-19 ~14:25Z, post-hoc)

`screen.hard_gates` returns **True**. No gate fails; seven are `None` (Solana-era fields with no EVM
source — `mint_revoked`, `freeze_revoked`, `insider_ok`, `insider_net_ok`, `graph_insiders_ok`,
`risk_ok`, `no_danger_risk`), which per the fail-closed-only-on-a-positive-finding rule pass through
rather than reject. `sources_dark` is **empty** — rpc, kyber, scanhood, geckoterminal, blockscout,
robinx and gmgn all answered.

| field | value |
|---|---|
| `owner_state` | `no_owner_fn` — no owner function exists, so there is nothing to renounce |
| `honeypot` / `scanhood_sellable` | False / True (`scanhood_verdict` PASS) |
| `roundtrip_loss_pct` | 0.35 – 0.48 % across two probes |
| `is_scam` / `is_proxy` / `verified_source` | False / False / **True** |
| `total_holders` | **2,138** (Blockscout) |
| `top10_pct` | **23.30 %** (Blockscout) — but **63.26 %** from GeckoTerminal |
| `lp_share_pct` | 30.98 % |
| `dev_pct` | **0.45 %** |
| `deployer` | `0xc0fc15882356ceeaff3e7b3a82c84a6aa047d72c` |
| `creator_prior_tokens` / `creator_dead_frac` / `creator_score` | **None / None / None — unknown** |
| `gt_score` / `gt_verified` | 62.79 / True |
| `launchpad_completed` / `launchpad_graduation_pct` | **True / 100.0** |
| `tx_per_holder_total` | 55.14 |
| market | px $0.002997, liq $164,625, mcap $2,175,634, fdv $2,570,168, vol24 $64,580, 89 buys / 124 sells |
| socials | website, X and Telegram all present on the Dexscreener record |

**The top-10 share disagrees with itself by 40 points** — 23.30 % (Blockscout, what the hard gate
reads) against 63.26 % (GeckoTerminal), on the same token in the same minute. The 09-16
retrospective recorded a 24-point gap on FOMOPAD and drew the same conclusion: a concentration
figure is a property of *the source and the instant*, not of the token. This is context for reading
the table, not an argument for changing a gate.

## 5. The "strategy" cluster, and a co-movement test that mostly fails

A Dexscreener name search for `strategy` / `strat` on this chain returns **20 distinct token
addresses** — a visible MicroStrategy/treasury meta. By liquidity: STRATEGY "Strategy Coin"
($857k liq, $6.64M fdv), SAYLORMOON ($338k), **RKST ($165k)**, NASTY "NotaStrategy" ($140k),
STRATTON "Stratton Market" ($113k), MACRO "MacroStrategy" ($87k), then QSTRAT, QUOSTR, OSR, SBR
"Strategic Bitcoin", MSTR "Meme Stock Treasury", RSTR "Robinhood Hat Strategy" and others. The meta
is still spawning: `musebookSTR` launched 09-18 20:07Z and read +643 % on the day. (One match is a
spam token whose *name* is a multi-kilobyte concatenation of tickers; a name search is a substring
match, not a category.)

The obvious hypothesis is that RKST moved with the meta rather than on anything token-specific.
**At hourly resolution that hypothesis fails outright:**

| peer | n (contiguous hours) | Pearson r of hourly log returns |
|---|---|---|
| STRATEGY | 284 | −0.018 |
| SAYLORMOON | 284 | +0.049 |
| STRATTON | 271 | +0.146 |
| NASTY | 196 | −0.236 |

Hourly bars on thin tokens are stale-biased toward zero correlation, so the same test on **daily**
closes, with Fisher-z intervals and a Benjamini–Yekutieli correction over the six peers tested
(m = 6, c(m) = 2.450, q = 0.10):

| peer | n (days) | r | 95 % CI | p | BY |
|---|---|---|---|---|---|
| **STRATTON** | 14 | **+0.768** | [+0.40, +0.92] | 0.001 | **passes** (thr 0.0068) |
| MACRO | 9 | −0.568 | [−0.89, +0.15] | 0.114 | fails |
| STRATEGY | 14 | +0.368 | [−0.20, +0.75] | 0.200 | fails |
| QSTRAT | 12 | −0.140 | [−0.66, +0.47] | 0.672 | fails |
| NASTY | 12 | +0.068 | [−0.53, +0.62] | 0.838 | fails |
| SAYLORMOON | 14 | +0.007 | [−0.53, +0.54] | 0.981 | fails |

**One pair of six survives BY: STRATTON.** Five do not, and their intervals are so wide (typically
±0.5) that the daily cut is uninformative about them either way. Read the surviving pair as a
**lead, not a finding**: the six peers were picked by liquidity rank *after* looking at the cluster,
and BY corrects for the six tests actually run, not for the choice of which six to run. A
pre-registered peer set, measured forward, is the only version of this that could mean anything.

So: the meta exists and RKST sits near the top of it by size, but this data does **not** establish
that RKST's moves are the meta's moves.

## 6. The one thing that is genuinely unusual, stated carefully

RKST **survived**. The 09-16 retrospective's three runners stood at −96.2 % (FOMOPAD), −99.5 %
(LITVM) and −82.4 % (DYNABLOCKS #2) from their ATHs when measured on 2026-09-17 ~18:00Z. RKST
stands at **−75.0 %** from its ATH measured on 2026-09-19 ~13:00Z — a *different as-of date*, so
this is not a like-for-like comparison and must not be quoted as one — while still holding 2,138
holders, $165k of liquidity in its headline pool (~$500k across all pools), a $2.18M market cap, a
verified non-proxy contract, a dev wallet at 0.45 %, and a completed launchpad graduation, 13.7 days
after launch.

That is the whole of it. On a chain whose typical outcome is −96 % inside 48 hours, *still existing
at seven figures after two weeks* is the anomaly — not the 6× nine-minute launch candle, which is
the most ordinary thing in this dataset. **Why** it survived is exactly what this document cannot
tell you: survival with n = 1 has no denominator, and the tokens that did the same thing and then
died are not in this sample because nobody asked about them.

## Refuses to claim

This document does **not** claim, and no later edit should let it claim:

- **no cause.** Not the meta, not the socials, not the launchpad graduation, not the low dev
  balance. Nothing here is a mechanism; everything here is a co-occurrence.
- **no hit rate and no lift** — one token with no matched unselected pool measures no selection
  effect.
- **no threshold** — not the $50k hour-1 volume floor, not a top-10 share, not a holder count, not
  a dev percentage. The numbers in §4 describe one token on one day; they are not cut-points.
- **no entry-band or exit-policy implication.** RKST was never ledgered, never scored, never in the
  book. No policy was simulated on it here, deliberately.
- **no "X predicts Y" sentence** — not holders, not liquidity, not graduation status, not the
  presence of a website.
- **no bound of any kind.** One token, 15 daily observations, `MIN_BOOTSTRAP_CLUSTERS` is 12 and a
  bound from fewer than that is not a bound. The STRATTON correlation is a lead, not a result.
- **no claim that RKST is currently rising.** It is −21.2 % on 24 h and −33.8 % on 7 d (§1).

## Where the test actually is

The only thing that can move this system is a **pre-registered** band or policy, judged
**forward-only** by the entry and exit gates, deflated by every trial the family has ever run, with
the negative controls as the kill switch and the paper scorecard as the judge. A token the screener
never saw cannot inform any of that. The honest prior is unchanged: this class of trade is **−EV**,
and "NO CHANGE" for months is the expected outcome.

*Not financial advice. Alert-only; this system holds no keys and moves no funds.*
