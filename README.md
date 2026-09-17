# robinhood_screener

An **alert-only** memecoin screener for **Robinhood Chain** (chainId 4663), rebuilt on 2026-09-12 as
an EVM port of the `solana_screener` system: discover new tokens from factory logs → reject rugs,
honeypots and serial deployers → rank the survivors → alert the champion band with a pre-computed
size / stop / take-profit plan → ledger every survivor's forward returns, with the non-alerted
survivors as a silent control → simulate the alerted book at real router quotes → re-score the
exit policy and the entry band every Sunday behind a statistical gate.

**It never touches keys or funds. Nothing here is financial advice.**

## The honest part (read this first)

The system this is a port of has been running on Solana for two months. Its measured state is the
prior for everything below, and it is not encouraging:

- **The A tier has 16 rows, ever** (15 alert-days). A beats B at 1 h and 6 h medians (+31.5 % vs
  −88.7 % @6h) and loses at 24 h and 7 d; **0 of 16 A rows are positive at 7 d**, and A's own 6 h
  mean has a day-clustered confidence bound that spans zero. Every relaxation of the A band tested
  in-sample was worse than the strict band.
- **Winners are transient peaks.** Clean ≥3x winners peak a median **~4 h** after entry; 71 % give
  back more than 80 % of the peak by 24 h; the winners' median 7 d return is −88 %.
- **No exit policy beats "sell instantly".** On 435 completed live paper positions the best "policy"
  was the negative control `ctl_exit_immediately` at −3.45 % — that number *is* the round-trip cost.
  Every real policy was worse. The promotion gate refused every one of its eight runs.
- **Nothing at entry separates winners out of sample.** Two pre-registered classifier rounds scored
  AUC 0.546 and 0.541; the top decile hit 29–33 % against a 33.3 % gross breakeven.

So: **A-tier means best *survival* odds under every gate, not predicted ROI.** The base rate is
negative expectancy. A losing scorecard is the screen doing its job — telling you not to scale.

### The reference winners (what "a coin worth finding" looks like here)

The two mid-cap winners used to calibrate this port are **CATGPT** ($15M, 4,370 holders) and
**ANTHROPIG** ($4.7M), both launched 2026-09-11 around 23:45 UTC through **Bankr's
`LongLaunchFactory`** — a Doppler integration that auctions `DopplerERC20V1` clones in Uniswap V4
pools under the Doppler hook, paired against tokenized stocks and "1x Long" tokens rather than
WETH. Measured on GeckoTerminal's 5-minute bars, CATGPT's path from its first bar: an entry at
+15 min was 7× at 14 h, an entry at +90 min (where the strict band's age check first allows A)
was 2.5×, and the peak (17× from the first bar) came 13.7 h in; the +90 min entry was only 1.07×
six hours later. The launchpad is a firehose — **~3,250 launches a day**, 71 % of which never get
a Dexscreener pair and ~5 % (~160/day) clear both market gates — so the two winners were 2 of
roughly 2,000 launches that day. **Before 2026-09-12 this port could neither discover them (no V4
or LongLaunch logs) nor pass them (their owner is the launchpad's shared contract, so "renounced"
is impossible by design).** The Solana screener never sighted the real OTC either; every
"OTC"/"stonkape" it ledgered was a copycat that went to zero. Coverage, not judgment, was the gap.

### What this repo used to be, and why that was dropped

Until 2026-09-12 this was a "proven dev" screener: alert when a new token's deployer had previously
launched a $10M+ token. It was dropped because (a) the P1 tier was a look-ahead bug (a fresh pool on
an old token read as a launch); (b) the chain's 27 golden tokens came from **27 distinct deployers**
— devs rotate wallets, so a bare-wallet track record is structurally unmeasurable; (c) the control
sample was not gate-matched (2,711 of 2,754 control rows failed the gates the alerted tiers had to
pass); (d) from 2026-08-23 the attribution source returned a Cloudflare challenge on every call and
the outage was invisible for 20 days. The data, the sweep report and the reasoning are preserved in
[`data/archive/proven_dev/README.md`](data/archive/proven_dev/README.md). The social-attribution
files from that era are held offline and are not in this repository's history.

## How it works

Every run advances a block cursor over the UniswapV2 `PairCreated`, Flap `TokenCreated` and V3
`PoolCreated` logs (exact at any cadence — a gap is caught up block by block, never skipped), adds
due rechecks, the 24 h watchlist and a GeckoTerminal new-pools hedge, batch-enriches everything via
Dexscreener, and runs the hard gates twice: pass 1 on market data + one Multicall3 batch of chain
facts (owner renounced, LP burned %, honeypot round trip) + ScanHood; pass 2, under a per-run budget,
on GeckoTerminal info + Blockscout holders/template/scam flag + RobinX deployer record. Survivors are
soft-scored, one flat feature dict is built, every registered entry band is evaluated, and the
champion band alone sets tier A and alerts. Events become ledger rows with a monotonic `event_seq`;
every band's verdict is recorded per event; forward returns fill from batched snapshots; A rows
drive idempotent exit signals and paper fills at router quotes. The cloud commits `data/` and
publishes `docs/` every run. On the Mac a 60 s ticker runs 16 exit policies and 2 controls off one
shared fill per alert; on Sundays the improve jobs re-score both arms behind the gate and a headless
Claude session proposes new candidates that merge only if `verify.py` is green.

| File | Role |
|---|---|
| `config.py` | Every threshold, weight, rate limit, path and the credential chain. Nothing tunable lives elsewhere. |
| `http_client.py` | stdlib urllib + certifi, per-host throttle with the sleep inside the lock, disk cache, the three-way `ok / absent / deferred` contract, source-health registry, rotate-once browser headers for Blockscout. |
| `screen.py` | **Pure.** `hard_gates`, `soft_score`, `hc_checks` (the one A-tier implementation), `high_conviction`, `gates_bitmask`. |
| `run.py` | The one-shot cloud scan: discover → enrich → pass 1 → pass 2 → bands → events → ledger → exits → paper → state → `latest_scan.json`. |
| `ledger.py` | Event rows keyed `(token, alert_ts)`; write-once time-gated 1h/6h/24h/7d cells; quote-integrity gate; exit events from the row's own frozen plan; the A-vs-B scorecard. |
| `alerts.py` | macOS / ntfy / Telegram delivery with retries; entry, exit, degraded-source, event and weekly formats. |
| `quotes.py` | Sell/buy quotes for the paper books: V2 router first, Multicall3-batched, Kyber and ScanHood only when no V2 pair exists; the on-chain-first "dead" rule. |
| `paper_exec.py` | The cloud A book: paper fills at real quotes for every alert and exit, plan frozen per position, pending quotes retried. |
| `dashboard.py` | Static renderer of `docs/index.html` from the committed files: scan, scorecard, paper book, source health, **measured** runs in the last 24 h. |
| `preflight.py` | Probes every source from wherever it runs (the Actions runner or the Mac); commits nothing. |
| `cloud_secrets.py` | Pipes the alert secrets into `gh secret set` over stdin. The human runs it. |
| `verify.py` | The invariant suite — the only tests. Offline, fail-fast. |
| `sources/` | One module per vendor (`rpc`, `blockscout`, `geckoterminal`, `dexscreener`, `scanhood`, `robinx`, `kyber`, `gmgn`) plus `safety.py`, the adapter that fuses them into the one flat safety dict with the pass-through rule. |
| `selfimprove/` | The exit loop: `policies.py` (16 policies + 2 controls), `livebook.py` (the Mac's live multi-policy book), `paths.py`/`backfill.py`/`evaluate.py` (bar backtest), `improve.py` (the 7-check exit gate), `champion.py` (the sole writer of `champion.json`), `publish.py`, `weekly_summary.py`, `run_improve.sh`, `trials.json`. |
| `selfimprove/entry_lab/` | The entry loop: `bands.py` (bands as pure functions + controls + the registry loader), `runtime.py` (feature normalizer, band evaluation, event decisions, watchlist), `store.py` (the verdict sidecar), `scorecard.py`, `improve_bands.py` (the 9-check entry gate). |
| `selfimprove/candidates/` | The research pool: `registry.json`, `register.py --scan`, `_template.py`, and every candidate module the weekly session has proposed. |
| `selfimprove/research/` | `run_research.sh` (the Sunday headless Claude session), `research_prompt.md`, `allowlist.py`, `proposals/`. |
| `launchd/` | The four Mac jobs: `dispatch` (5 min), `livebook` (60 s), `improve` (Sun 11:00), `research` (Sun 12:00). |
| `.github/workflows/` | `screener.yml` (the cloud scan + Pages deploy) and `verify.yml` (the invariant suite on human pushes). |
| `docs/DESIGN.md` | The reconciled design: resolved decisions and module contracts. |
| `docs/RETRO_2026-09-16_hype_runners.md` | What three 2026-09-16 runners did minute by minute on ordered 1-minute paths — descriptive, n = 3, chosen on the outcome; no rule is derived from it. |

## Tiers, the control group and the event model

Every hard-gate survivor is ledgered. **A** = passed the hard gates *and* the champion entry band
at that instant; alerted. **B** = passed the hard gates, failed the champion band at that instant;
silently ledgered as the control. The band is a claim and the ledger is its falsification test:
`python3 ledger.py` prints A against B per horizon and says in its own output that if A does not
clearly beat B, the band is not adding signal.

A ledger row is an **event**, not a token. `first_sighting` is the first row a token gets;
`band_fire` opens a row when a registered band's verdict first flips True on the watchlist;
`promotion` opens an A row when the champion's verdict flips True (typically hours later — the
strict band needs 90 min of age). Every row carries `event_seq` (minted `1 + max`, never reused),
`event_kind`, `fired_band` and `prior_event_seq`, and every band's verdict is recorded at every
event in `data/band_verdicts.csv`. **A promoted token's earlier B row stays in the B arm forever**
(intention-to-treat); every table prints `promoted-B n` beside B's n and nothing filters on it.
Excluding those rows would remove from the control exactly the tokens that survived long enough to
be promoted.

## What the gates can and cannot see on this chain

The **pass-through rule**: a hard gate fails closed only on a *positive* finding from a source that
answered. A source that is dark, rate-limited or has no record passes through and is named in
`sources_dark`. The A-tier checks are stricter: a check whose input is unknown or came from a
degraded source is `None`, and an unknown can never be A (tier B with `band_na_reason`).

**Launchpad tokens (Bankr / Doppler on Uniswap V4).** Discovery reads the factory's `Create`
log (token, launcher, numeraire, hook) and the PoolManager's `Initialize` log for pools under a
trusted hook (the token is the leg that is not a quote token, a numeraire declared in the same
window, or a currency repeated across pools). The gate analogues: the token's `owner()` is a
known **protocol** contract (not a dev key) and passes; the liquidity sits in the hook's custody
by construction, so "LP known" is answered by the launchpad without a burn percentage; the
honeypot round trip runs through **KyberSwap** (keyless; both legs routed ⇒ the loss, a sell leg
with no route on a minutes-old pool stays unknown); the launcher's whole-life launch history comes
from one indexed log query and is judged by the **dead fraction of its prior launches** — an
app/agent wallet with 20+ launches passes only on a known dead fraction within the cap; the dev
holding is the launcher's own balance when the launcher is a person. `DopplerERC20V1` is on the
template whitelist. The strict champion band still needs a known dev holding and a low launcher
count, so agent-launched tokens sit in **B** under it; the built-in candidate
`band_launchpad_lenient` drops exactly those checks on the launchpad and the entry lab decides
whether the leniency pays.

**Pons and hook-less V4.** Pons (`pons-v2-dex`) is the chain's largest launchpad by count —
750 to 1,250 launches an hour, each with a singleton-AMM pool at creation; only 1–4 % ever get a
Dexscreener market and under 1 % clear the market gates. Its `Create` log is a discovery source
(token, pool id, creator), an absent launch is rechecked **once** at 30 minutes, and Pons pools
have no V2 pair, so their round trip comes from Kyber when it routes them and stays unknown
otherwise. Hook-less Uniswap V4 pools held the day's largest winners (AnsemCat, MEME, BONER,
FLYBRAIN) and are read the same way as the hooked ones, with the numeraire rule dropping any
pool whose token leg cannot be told from its quote leg.

**What is alerted (operator decision, 2026-09-12).** The live champion entry band is
`band_volume_early`, set by hand with a recorded reason: pool age ≤ 30 min, hour-1 volume ≥ $50k,
liquidity ≥ $10k, market cap ≤ $2M and buys ≥ 2× sells. It encodes the operator's thesis that
the money is in early entries on coins that already trade heavily with buyers dominating; on the
first day's 40 survivors the three that went 3–23× all had that flow at sighting and every
balanced-flow sighting went flat or to zero (n = 9, in-sample — a hypothesis, not evidence).
A-tier therefore no longer means "best survival odds"; it means "early, heavily traded, buyer-
dominated, and past every hard gate". `band_a_strict` stays a scored candidate, the ledger and
the paper book judge the alerted band against the silent control, and the one-shot demotion test
reverts the champion to `band_a_strict` if its selection lift over the same-day, same-age pool
is not positive. The FOMO the screener ledgered on day one went from $44k to $568 within hours:
the exit policy, judged by the live book, matters as much as the entry.

Some of the Solana gates have **no analogue** here. There is no funding-graph clustering of insider
wallets, no behavioural wallet tags, no freeze authority, and honeypot.is rejects the chain; GoPlus
lists it but covers ~3 % of tokens, so it is never a gate. The substitutes are the router round trip
(`getAmountsOut` both legs via `eth_call`), `owner() == 0x0`, `balanceOf(0xdead) / totalSupply()` on
the pair, Blockscout's implementation name as a contract-template whitelist, and creation-tx logs
for dev sniping. **The LP-burn and round-trip checks are exact only for UniswapV2 pairs.** A token
whose only pool is V3, V4 or a launchpad curve has an unknown LP status and no router quote: it can
be B, and only rarely A. That bias is stated on the dashboard and in the weekly summary.

| Source | Cost | What only it provides |
|---|---|---|
| public RPC (`sources/rpc.py`) | free, keyless | chain truth: owner, LP burn, the honeypot round trip, Multicall3 batching, factory-log discovery, creation-tx fallback |
| Blockscout | free, keyless | holders paged with contracts excluded, transfer/holder counters, `is_scam`, proxy implementation name, creation-tx sender. Sits behind a Cloudflare challenge keyed on the User-Agent: a browser UA + Referer answers 200; on a challenge the client rotates header set once, then marks the host dark for the run and backs off across runs. |
| GeckoTerminal | free, 30/min per IP | new-pools feed with buyer counts, holder count and top-10 % (8 min–23 h stale), dev holding, launchpad graduation, honeypot flag, OHLCV |
| Dexscreener | free, keyless | the market snapshot, 30 addresses per call, and the **only** source of forward-return fills; the WETH price |
| ScanHood | free, keyless | chain-specific verdict + sell simulation, a read-only swap quote, the launch feed |
| RobinX | free tier | deployer track record (launched / real / dead / score), insider flags |
| KyberSwap | free, keyless | aggregator route with USD legs and gas — paper fills for tokens with no V2 pair |
| GMGN (`openapi.gmgn.ai`) | keyed, free tier | the Trenches feed (New / Almost bonded / Migrated) as a discovery hedge, and the behavioural wallet tags — bundler wallets ÷ holders, sniper / insider / fresh-wallet hold rates, smart-money count, wash-trading flag — as pass-2 **features, never a gate**. Its Trenches allow-list omits `pons_v2` and bare V2/V3/V4 pools (72 of the top-100 rank rows, 2026-09-12), so it complements the log cursor; a 429 is a ban and is terminal for the run. See [`docs/GMGN_TRENCHES.md`](docs/GMGN_TRENCHES.md). |

## The two death tests (they differ on purpose)

The **ledger** declares a token dead when Dexscreener's index no longer returns it for
`DEAD_CONFIRM_TICKS` consecutive polls (one gap is not death) — a forward-return question, answered
by the source that fills every other cell, recorded as −100 % and never dropped. The **paper books**
decide "no route" **on-chain first**: absent only when the V2 pair exists with reserves `(0, 0)` and
the router reverts, or when no V2 pair exists and both aggregators explicitly report no route; any
unanswered probe is deferred and retried, never filled. The first is honesty about outcomes, the
second is honesty about execution; a $0 fill on the book must never depend on an aggregator having
answered, and a rate limit must never be read as death. The weekly summary prints the ledger's dead
share beside the book's no-route count so the two rules stay auditable against each other.

## The exit plan and how the champion changes

Every alert carries the champion exit plan (`cfg_ladder_stop` by default: the take-profit ladder plus
the hard stop), and the ledger row and paper position freeze that plan at record time. The plan is
one of 16 policies + 2 negative controls that the Mac's live book runs simultaneously off **one shared
fill per alert**, behind a quote-integrity gate, so any difference between two policies is caused by
the exit alone.

The exit gate (`selfimprove/improve.py`, Sundays) is seven checks: a day-clustered lower bound on the
challenger's own returns above zero, a **paired** lower bound against the champion, a bound above the
best control, Deflated Sharpe ≥ 0.95 at the cumulative trial count, 300 positions and 40 alert-days,
and a **forward-only** test — nomination stamps the book's sequence number and only positions opened
after it may judge the nominee, once. **Both negative controls must fail**; if `ctl_exit_immediately`
or `ctl_random_exit` clears the gate the run is void and the apparatus is at fault. After a promotion
a one-shot **demotion** test judges the new champion's own forward prefix once; falling to or below
the best control reverts it to the default with no positive proof required. Promotion, demotion and
nomination write `selfimprove/champion.json` through its sole writer and are applied **only under
`--apply` on Sundays**. Human overrides: create `selfimprove/PAUSE` (both gates evaluate and report but
write nothing), or `python3 selfimprove/champion.py --set exit=<name> --reason "..."`. The off switch
is `gh workflow disable robinhood-screener` plus `launchctl bootout` of the dispatch job.

## The entry lab and the weekly research session

An entry band is a pure function `verdict(feat) -> True | False | None` over one flat feature dict
— no clock, no network, no randomness, enforced by an AST check before import. The champion is
`band_a_strict` (every A-tier check True). Every registered band's verdict is recorded at every
event, so bands are scored on rows they never selected as well as rows they did. The entry gate
(`selfimprove/entry_lab/improve_bands.py`) compares a band's picks with the **same-day,
same-age-bucket** unselected pool (a maturation band must not win merely by entering later), needs
a paired lift over the champion, a lift over the timing-matched random control, an own lower bound
**net of the modelled round-trip cost**, Deflated Sharpe at the number of bands ever scored, a
within-day shuffled-label calibration, Benjamini–Yekutieli across bands, 40 retained days and 150
selected rows, then the same forward-only nomination, one-shot judgment and demotion test as the
exit loop. **K5**: if after 180 days no band clears zero net of cost, the summary prints "no entry
signal" and the research prompt carries that verdict.

Sundays at 12:00 a headless `claude -p` session runs in a **temporary worktree outside the repo**,
reads the scorecards, and may create at most two candidate modules under `selfimprove/candidates/`
plus one proposal file. The diff is checked against an allowlist by the Mac's own copy of
`allowlist.py`, `register.py --scan` validates each module in an isolated subprocess (import
allowlist, determinism on fixtures, NA on unknown fields, not inert against the champion), and the
branch merges only if `verify.py` is green; otherwise it is pushed for a human. A candidate enters
the pool, never the champion. **Every registration is a counted trial** in `selfimprove/trials.json`,
which only grows, and deflates every later Sharpe gate for its family — the pool is capped at 40.

## The paper book and the automation condition

Every A alert and every exit is also filled on paper at a real quote for the plan's actual position
size ($10) in `data/paper_ledger.csv`. Measured on a ~2.3 WETH V2 pool (the V2 mechanics fixture):
a $10 round trip costs 0.93 % in quote (0.6 % fees + ~0.3 % impact) plus two ~$0.07 gas legs —
**≈ 2.3 % all-in**, with the fixed gas term alone worth 1.4 % at this size. On CATGPT's V4 pool a
$10 round trip through Kyber quoted at −0.96 % before gas (1.23 % at 0.01 WETH). A protective exit on a coin with no route
fills at **$0**, exactly like real life.

**Real automated trading is justified ONLY if the PAPER scorecard is repeatedly positive** — never
the frictionless ledger's. The gap between the two is the execution cost; if the edge does not
survive quoted slippage, an execution layer would automate losses faster.

## Deployment

The cloud (GitHub Actions) is the system of record: it runs the scan, commits `data/` and deploys
`docs/` to Pages from a workflow (`pages.yml`, exempt from the 10-builds/hour limit). The trigger
is **not** GitHub's cron: measured on this account, the `*/5` schedule fired **13.7×/day against a
nominal 288** (2026-09-12). The scan is therefore a self-chaining **keeper** job (`.github/keeper.sh`):
one run scans every 240 s for up to 340 min, dispatches its successor into the other concurrency
slot and hands off through `data/keeper_handoff.json`; a `*/5` watchdog workflow restarts a dead
keeper and alerts SCAN STALE past 30 min; the Mac dispatch job is retired (a one-shot `mode=run`
exits without scanning while a keeper is alive). Discovery, horizons, the watchlist and rechecks are
written to be correct at any cadence; the dashboard and the weekly summary report the **measured**
runs in the last 24 h, never the nominal number. The Mac still runs the 60 s live book (it reads
`origin/main` with `git fetch` + `git show`, never `git pull`), the Sunday improve chain (11:00) and
the research session (12:00).

## Running it

```bash
python3 verify.py                                   # the invariant suite; run after any change
python3 run.py                                      # dry run: nothing written, nothing sent
python3 run.py --commit                             # write ledger / state / scan, send nothing
python3 run.py --send                               # write AND alert (what the workflow runs)
python3 preflight.py                                # probe every source from here; commits nothing
python3 ledger.py                                   # the A-vs-B scorecard
python3 paper_exec.py                               # the paper A book (add --live to mark it)
python3 selfimprove/livebook.py --tick              # one live-book cycle (launchd, every 60 s)
python3 selfimprove/livebook.py --scorecard         # per-policy live P&L
python3 selfimprove/improve.py                      # exit gate, dry (add --apply --send on Sundays)
python3 selfimprove/entry_lab/improve_bands.py      # entry-band gate, dry
python3 selfimprove/weekly_summary.py --dry         # the one weekly message, printed
python3 selfimprove/candidates/register.py --scan   # validate + register new candidate modules
python3 selfimprove/champion.py --set exit=hold_to_end --reason "manual"   # human override
python3 cloud_secrets.py --check                    # which secret NAMES the repo has
RESEARCH_DRYRUN=1 bash selfimprove/research/run_research.sh   # exercise the research plumbing, no model, no push
```

Every module has a `__main__` smoke test (`python3 screen.py`, `python3 quotes.py`,
`python3 sources/rpc.py`, ...). Dependencies: `pandas>=2,<4` and `certifi`; the Sunday statistics
additionally use numpy/scipy on the Mac.

## Secrets

`config.load_credentials()` reads the environment, then a gitignored `config.local.json`, then the
shared Mac secrets file — only the Telegram bot token, chat id and ntfy topic. `cloud_secrets.py`
pipes those same values into `gh secret set` over stdin, never argv, never printed. Every credential
file is mode 0600 and the loader warns otherwise. Nothing is hardcoded.

## Honest expectations

- Measured volumes (26 survivors/day and 0.26 first-sighting A rows/day on Solana; ~8.7 gate-passers
  per day here under the old, laxer gates) mean **3–9 survivors/day and 0.2–2 champion alerts/day**.
- At those rates the exit gate's floors put the earliest possible exit-policy promotion at
  **≈ 80 days after the live book's first tick**, and the entry-band gate (40 retained days and 150
  selected rows, then 40 forward days and 100 rows) at **≈ 6–9 months**. The weekly summary
  recomputes both from the trailing 28 days of observed counts.
- At the floors, Deflated Sharpe ≥ 0.95 needs a per-day Sharpe of roughly **0.5** on the nominee's
  own day means (t ≈ 3.5); the lower-bound checks alone need ≈ 2. **No measured policy is within 5×
  of that.** The negative controls are the only instrument separating skill from machinery.
- Entry promotions require an edge net of the ~3.4 % modelled round trip.
- **"NO CHANGE" for months is the expected outcome.** The gate refusing is the system working.

## Risks and caveats

- Blockscout from the Actions runner's IP is unverified; `preflight.py` measures it. If challenged,
  pass 2 runs on GeckoTerminal + ScanHood, holders/template checks are NA (tier B, never a false A),
  and the DEGRADED alert, dashboard line and weekly dark share quantify it.
- Cadence depends on the Mac dispatching. Without it GitHub's cron gives ~14 runs/day; the cursor
  catches up exactly (up to 8.4 h per run), but horizon cells carry more drift and watchlist
  promotions are rarer.
- Aggregator outages leave tokens with no V2 pair `unpriced` (reported, split by pool type).
- V3/V4-pool tokens rarely reach A-tier — LP status unknown by construction.
- Entry lag is 3–8 min (dispatch + run + commit + fetch), recorded as `entry_lag_s` on every live
  position; anything past 15 min is refused and logged. The book judges "this exit policy given a
  late entry", not a t=0 entry.
- The public RPC and GeckoTerminal budgets bound throughput; the global time budget defers visibly
  (`deferred_by_stage`) and never marks a deferred token as seen.
- History is never rewritten after go-live; the repo grows by one commit per run, bounded by the
  no-per-row-stamp rule and 90-day rotation of resolved rows.

*Not financial advice. Memecoins are near-100 %-loss-prone; the chain's $10M+ launch of July 2026
traded at an $8k market cap on 2026-09-12, and the reference winners above are one day old.*
