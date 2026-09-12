# Archived: the proven-dev screener (2026-07-23 → 2026-09-12)

These files are the complete state of the original `robinhood_screener`, which tested the
hypothesis that a new Robinhood Chain token deployed by a wallet with a prior $10M+ launch
has better forward returns than the chain's base rate. The hypothesis was dropped on
2026-09-12 and the repo was rebuilt as a port of the solana_screener architecture.

| file | what it is |
|---|---|
| `provendev_forward_returns.csv` | the old ledger: 2,770 rows (16 P1, 2,754 control), 2026-07-24 → 2026-09-05, with 1h/6h/24h/7d forward returns. Renamed so it never shadows `data/ledger.csv` (a sibling voice assistant reads the alphabetically-first `*ledger*.csv`). |
| `proven_devs.json` | 27 deployers credited with a ≥$10M peak launch, all from the single 2026-07-24 full-chain sweep. |
| `token_creators.json` | 21,062 token → deployer attributions (creation-tx sender, factory-aware), 13,878 unique deployers. Still useful as a static index of prior launches per wallet. |
| `peak_mcap.json` | 5,289 peak-market-cap records (max daily OHLCV high × total supply; volume-supported bars only). |
| `sweep_candidates.jsonl` | 34,846 ERC-20s with ≥25 holders enumerated by the sweep. |
| `sweep_report.md` | the sweep's report: 27 golden tokens, 1 excluded artifact. |
| `provendev_alert_state.json` | the old cooldown / seen-token state. |
| `social_verdicts.json`, `mizukara_report.md` | **kept offline, gitignored**: Claude-written assessments of social claims that pair wallet addresses with named accounts. Not published. |
| `history.bundle` | **gitignored**: a full `git bundle` of the pre-rebuild history (also on the local branch `proven-dev-history`, tag `proven-dev-final`). |
| `com.yousefjan.robinhood-*.plist` | the retired launchd jobs (every-2-min local scan; the 127.0.0.1:8899 log-scraping dashboard). |

## Why it was dropped

- The forward-return evidence was weak: 16 P1 rows, and every bootstrap confidence interval on the
  P1-vs-liquidity-matched-control median difference contained zero at all four horizons. The one
  robust finding was survival (P1 tokens rarely went to zero: 0/15 dead at 7d vs 58% for a
  gate-matched control), not returns.
- `entry_bot/CLAUDE.md` documented the P1 tier as a look-ahead bug (GeckoTerminal `new_pools`
  returns new *pools*, so a fresh pool on an old token read as a launch; `ledger.token ==
  best_token` in the first alerts) and found that the chain's 27 golden tokens came from 27
  distinct deployers — devs rotate wallets per launch, so a bare-wallet "proven dev" is
  structurally unmeasurable.
- The control sample was not gate-matched (2,711 of 2,754 control rows failed the safety gates
  the P tiers had to pass), so the comparison was confounded by liquidity.
- From 2026-08-23 the deployer-attribution source (Blockscout) returned a Cloudflare challenge
  on every call, and the ledger recorded 3 rows in 20 days while alerts kept firing from the
  cached index — the outage was invisible.

The peak-mcap method and its two artifact guards (`PEAK_BAR_MIN_VOL_FRAC`, `PEAK_PER_HOLDER_MAX_USD`)
were derived from real incidents (ROBY/VEXA launch wicks, SHERIFF's $1B-on-1,090-holders) and are
preserved in `sweep_report.md` and in `entry_bot/corpus/peaks.py`.
