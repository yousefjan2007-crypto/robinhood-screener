# GMGN Trenches × robinhood_screener

GMGN's **Trenches** page is a launch-lifecycle scanner: three columns, one per bonding-curve stage,
side by side on desktop, each with its own filter panel at its top-right. Filters and layout are
saved **per chain** and do not transfer between chains. GMGN lists Robinhood Chain as a first-class
chain under the slug `robinhood` (web, app and the keyed OpenAPI; verified live 2026-09-12/13).

| UI column | API `type` | what it holds | this repo's analogue |
|---|---|---|---|
| **New** | `new_creation` | just minted, still on the curve (~60-row live window) | LongLaunch / hooked-V4 log → `recheck.json` until a pair exists → `first_sighting` row; `band_new_creation` |
| **Almost bonded** | `near_completion` (GMGN returns it under `pump`) | curve filling, `progress` 0–1 | `launchpad_graduation_pct` (GeckoTerminal), `gmgn_progress`; `band_almost_bonded` |
| **Migrated** | `completed` | curve done, pool open (`open_timestamp`) | `launchpad_completed`; `band_graduated_only`, the strict band |

Robinhood URLs: Trenches `https://gmgn.ai/?chain=robinhood`, token page
`https://gmgn.ai/robinhood/token/0x…` (every alert and dashboard card carries it), security page
`https://gmgn.ai/security?chain=robinhood`. Desktop hotkeys select a column with the Column Key +
a Row Key; `X` previews the token's X account, `V` its website. Whether the app moves between
stages by tabs or swipe is not documented.

**What the Robinhood columns contain** (one live pull, 2026-09-13 01:30 UTC, 60 rows each): New =
`longxyz` 51 / `flap` 5 / `livo`, `o1`, `pons`, `bankr` 1 each; Almost bonded = `longxyz` 30,
`noxafi` 8, `bags` 7, `bankr` 5, `flap` 3, `pons` 3, `o1` 2, `apestore` 1; Migrated = `pons` 40,
`longxyz` 14, `bankr` 6. The `longxyz` addresses end in `1e18` — the same LongLaunch `Create` logs
the block cursor reads (inferred from the address salt, not stated by GMGN).

**The coverage hole.** Trenches applies a fixed server-side launchpad allow-list for `robinhood`
that GMGN says "may lag behind newly launched platforms". A live `/v1/market/rank` sample had 72 of
the top 100 rows on platforms outside it (`pons_v2` 67, `o1_rwa`, `pair_fund`, `pool_uniswap_v3`,
`pools_trade_instant`), and a hook-less V4 winner is tagged with an empty platform. So Trenches and
the log cursor see different universes; the feed is a discovery **hedge** here, never a replacement.

## Filters — the repo's own gates, translated (Migrated column)

Field names from GMGN's client (`TRENCHES_FILTER_FIELDS`). Rates are 0–1, money is USD, durations on
Trenches accept **s/m only** (`90m`, `4320m`; `h`/`d` are rejected). Unknown filter keys are
**silently ignored** — a typo loosens the filter with no error. Measured on the live `completed`
column, 60 unfiltered rows, 2026-09-12 16:20 UTC:

| repo constant | value | GMGN field | binds? |
|---|---|---|---|
| `HC_MIN_HOLDERS` | 1000 | `min_holder_count` 1000 | mildly (31/60) |
| `HC_TOP10_MAX_PCT` | 20 % | `max_top_holder_rate` 0.20 | barely (58/60) |
| `HC_MIN_LIQ_USD` | $25k | `min_liquidity` 25000 | **the binding gate (5/60)** |
| `HC_MIN_AGE_MINUTES` | 90 | `min_created` `90m` — on `completed` it counts from **migration** | drops 1 of 6 |
| `HC_MAX_DEV_PCT` | 2 % | `max_creator_balance_rate` 0.02 | never (max seen 0.0094) |
| sniper hold 10 % | | `max_top70_sniper_hold_rate` 0.10 | never (max 0.0001) |
| insider 15 % / bundler 0.5 | | `max_insider_ratio`, `max_bundler_rate` | **no-ops here** — rows carry `suspected_insider_hold_rate` / `bundler_mhr`, not these keys |
| `HC_MAX_ROUNDTRIP_PCT` | 8 % | none (`sell_tax` is display-only) | — |

The three-gate set (holders, top-10, liquidity) returned 6 rows, 5 with the age gate, and both
reference winners (CATGPT, ANTHROPIG) pass it. Relaxing holders to 200 → 16 rows, to 100 → 21,
entirely by admitting low-holder, high-liquidity coins. Add the Token Audit toggles that mirror the
hard gates (Not Honeypot, Owner Renounced, Open Source). `smart_degen_count ≥ 1` (preset
`smart-money`) is GMGN's one extra signal — use it as a sort, not a gate.

**New column**, from `band_volume_early` (approximate; GMGN exposes 24 h counts): `max_created 30m`,
`min_liquidity 10000`, `max_marketcap 2000000`, sort `volume_1h` desc, then read buys vs sells on
the card. GMGN's own New-column advice: mcap > $20k, ≥ 100 tx, ≥ 1 social, preset `safe`
(`max_rug_ratio 0.3`, `max_bundler_rate 0.3`, `max_insider_ratio 0.3`). **Almost bonded:**
`min_progress 0.5`, `min_holder_count 100`, `min_liquidity 10000`; GMGN suggests mcap > $50k.

Base rates to keep in view: curve launches that ever graduate 0.2–2 % depending on the window
(arXiv 2607.02823; Solana Compass), 68.7 % never trade after creation day (CoinGecko, 2026-06-23);
on this chain ~3,250 LongLaunch launches/day, 71 % never get a pair.

## The fields the screener reads (and where each one can come from)

Mapped **only** from keys seen in the cached payloads (`cache/gmgn_trenches_*.json`,
`cache/gmgn_info_*.json`); nothing is assumed from the docs. Rates arrive 0–1 (sometimes as strings)
and are stored ×100; `is_honeypot` arrives as a **string**, not a bool.

| feature | Trenches row key | `/v1/token/info` key | shape |
|---|---|---|---|
| `gmgn_visiting_count` | `visiting_count` | `data.visiting_count` | int — viewers on the board right now (0–5 observed on Almost bonded, up to 51 on Migrated) |
| `gmgn_top10_holder_pct` | `top_10_holder_rate` | `stat.top_10_holder_rate` | 0–1 rate → % (< 0.20 on 59 of 60 Almost-bonded rows: the column barely discriminates) |
| `gmgn_market_cap` | `market_cap` | — | USD |
| `gmgn_volume_24h` | `volume_24h` | — | USD, 24 h (GMGN publishes no hour-1 volume) |
| `gmgn_buys_24h` / `gmgn_sells_24h` | `buys_24h` / `sells_24h` | — | int counts, 24 h |
| `gmgn_is_honeypot` | `is_honeypot` | — | **string** 'yes' / 'no' / 'unknown' → True / False / **None** |
| `gmgn_created_ts` | `created_timestamp` | `data.creation_timestamp` | unix seconds |
| `gmgn_progress` | `progress` | `data.launchpad_progress` | 0–1 (the info payload's key is NOT `progress`) |
| `gmgn_launchpad_platform` | `launchpad_platform` | `data.launchpad_platform` | str |
| `gmgn_holders` | `holder_count` | `stat.holder_count` | int |
| `gmgn_sniper_hold_pct` | `top70_sniper_hold_rate` | `stat.top70_sniper_hold_rate` | rate → % |
| `gmgn_fresh_wallet_pct` | `fresh_wallet_rate` | `stat.fresh_wallet_rate` | rate → % |
| `gmgn_rat_vol_pct` | `rat_trader_amount_rate` | `stat.top_rat_trader_percentage` | rate → % |
| `gmgn_smart_degen_count` | `smart_degen_count` | `wallet_tags_stat.smart_wallets` | int |
| `gmgn_bundler_ratio` | — | `wallet_tags_stat.bundler_wallets ÷ stat.holder_count` | ratio (organic 0.00–0.01; the $Cubrate wallet farm 1.42) |
| `gmgn_is_wash_trading` | `is_wash_trading` | — | bool |
| `gmgn_insider_hold_pct` | `suspected_insider_hold_rate` | — | rate → % |

The last two rows are the reason the **row** is attached at pass 1: `/v1/token/info` carries neither
key in any of 16 cached payloads, and neither do the market-cap, volume, flow or honeypot fields, so
a token that only ever gets a `/v1/token/info` call can never have them. The row is free once the
feed has been pulled, and `latest_scan.json.gmgn_coverage` (the share of survivors with a known
`gmgn_visiting_count`) says how far it actually reaches — read that number before trusting any band
that depends on a GMGN field.

## How the scanner uses GMGN

- `sources/gmgn.py` — one `POST /v1/trenches?chain=robinhood` per run (all three columns, GMGN's
  own `version: v2` + `quote_address_type` body; without them the server answers code 0 with empty
  columns) as a discovery hedge under the `feeds` quota; `GET /v1/token/info` for the first
  `GMGN_INFO_BUDGET_PER_RUN` pass-2 tokens (the bundler ratio = bundler wallets / holders).
- The `gmgn_*` fields are **features, never a hard gate**; a dark GMGN is named in `sources_dark`
  and the card prints "GMGN: unavailable (passed through)". A 429 is a ban whose cooldown extends
  per retry, so it is terminal for the run (`HOST_429_TERMINAL`), and the rate is pinned at 0.5 Hz.
- Three pre-declared candidate bands, silent B arms judged by the Sunday entry gate with the
  controls: `band_gmgn_clean` (strict band + organic wallet tags), `band_new_creation` (the New
  column), `band_almost_bonded` (the Almost-bonded column). Nothing is promoted on in-sample
  evidence; "NO CHANGE" for months is the expected outcome.
- The key comes from the env (`GMGN_API_KEY`, a GitHub secret piped by `cloud_secrets.py`) or the
  gitignored `config.local.json`; it is never in source.
