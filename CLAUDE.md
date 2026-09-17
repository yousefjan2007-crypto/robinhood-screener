# CLAUDE.md

Guidance for Claude Code sessions working in this repository.

## What this is

An **alert-only** Robinhood Chain (chainId 4663) memecoin screener, rebuilt 2026-09-12 as an EVM
port of `solana_screener`: factory-log discovery → hard gates twice → champion entry band sets tier
A → event-keyed ledger with a silent B control → paper fills at real router quotes → a Mac live book
running 16 exit policies + 2 controls → Sunday promotion gates for the exit policy and the entry
band → a weekly headless research session that can only add *candidates*. **It never touches keys
or funds**; the pre-committed condition in `README.md` and `paper_exec.py` is that real automation
is justified only if the **paper** scorecard is repeatedly positive. `README.md` carries the honest
framing and the file table; `docs/DESIGN.md` carries the resolved decisions and module contracts.

## Commands

Python 3.9.5 on the Mac, 3.11 on the Actions runner; deps `pandas>=2,<4` + `certifi` (Sunday
statistics use numpy/scipy on the Mac only). No pytest, no venv. **Every module has a `__main__`
smoke test** — `python3 <module>.py` is the fastest way to inspect any piece.

```bash
python3 verify.py                                   # THE test suite: offline, fail-fast; mac_only sections SKIP under CI
python3 run.py                                      # dry: writes nothing, sends nothing
python3 run.py --commit                             # write ledger/state/scan/verdicts, send nothing
python3 run.py --send                               # write AND alert — only the workflow runs this
python3 preflight.py                                # source probes from wherever it runs; commits nothing
python3 sources/gmgn.py                             # one live Trenches pull + one token_info (needs the GMGN key)
python3 ledger.py                                   # A-vs-B scorecard, promoted-B n, suspect line
python3 paper_exec.py                               # paper A book (--live marks the open book: one Multicall3)
python3 dashboard.py --write                        # render docs/index.html (no flag = smoke test only)
python3 selfimprove/livebook.py --tick|--scorecard  # the Mac 60 s book / per-policy live P&L
python3 selfimprove/improve.py [--apply --send]     # exit gate; --selftest = offline only
python3 selfimprove/entry_lab/improve_bands.py [--apply --send]   # entry gate; --selftest
python3 selfimprove/entry_lab/scorecard.py --markdown
python3 selfimprove/weekly_summary.py --dry|--send  # the ONE weekly message
python3 selfimprove/candidates/register.py --scan [--max-new N] | --budget-remaining | --selftest
python3 selfimprove/champion.py --set exit=<policy>|entry_band=<band> --reason "..." [--publish]
python3 cloud_secrets.py [--check]                  # THE HUMAN runs this; values go over stdin
bash selfimprove/run_improve.sh                     # the Sunday 11:00 chain (ff-sync, gates, publish)
RESEARCH_DRYRUN=1 bash selfimprove/research/run_research.sh   # plumbing only: no claude, never pushes
                                                              # (RESEARCH_ORIGIN=<bare repo> tests the push path)
```

Mac launchd jobs (`launchd/`, labels `com.yousefjan.robinhood-*`, all exit 0): `dispatch`
(StartInterval 240 → `gh workflow run robinhood-screener -f trigger=dispatch`; a scan job runs ~5 min so runs go back to back), `livebook`
(StartInterval 60, `livebook.py --tick`), `improve` (Sun 11:00, `run_improve.sh`), `research`
(Sun 12:00, `run_research.sh`). The retired `robinhood-screener` / `robinhood-dashboard` labels
must stay absent. `--send` on the two gates means **event alerts only** (PROMOTED / DEMOTED /
NOMINATED / APPARATUS FAULT / PUBLISH FAILED / PAUSED); the weekly summary is sent once, by
`run_research.sh`, whether research ran or not.

## Architecture

**Cloud vs Mac.** GitHub Actions (`screener.yml`) is the system of record: `run.py --send`,
`dashboard.py --write`, commit `data/` + `docs/` as `robinhood-screener[bot]`, deploy Pages from
the workflow. The trigger is the Mac's dispatch job; GitHub's cron is the fallback (measured
13.7 fires/day against a nominal 288 on this account). `latest_scan.json.trigger` records which.
The Mac runs the live book, the Sunday gates and the research session, and publishes
`selfimprove/*` + `data/proposals/` + `data/livebook_summary.json` through `publish.py` from a
detached temp worktree — path sets disjoint from the runner's, so `git pull --rebase` on both sides
is conflict-free.

**Discovery.** `data/discovery_cursor.json` is a block cursor over `DISCOVERY_LOG_SOURCES`; log
tokens are never truncated (`DISCOVERY_MAX_LOG_TOKENS_PER_RUN` caps the window; the cursor advances
only to the last processed log; catch-up ≤ 300k blocks in 100k windows). Per-source quotas
`DISCOVER_QUOTA` (logs, watchlist, rechecks, feeds) spill forward; `MAX_DISCOVER` bounds only the
Dexscreener enrich set. A log token with no Dexscreener pair goes to recheck with its `disc` record
and is never marked seen until it has a market snapshot. The watchlist keeps every survivor 24 h:
Dexscreener re-enrich every run, chain facts via one Multicall3 at most every `SAFETY_REFRESH_S`,
pass-2 refresh at most every `GT_INFO_REFRESH_S` for `WATCH_REFRESH_PER_RUN` tokens ordered by
fewest `hc_checks` misses. `RUN_TIME_BUDGET_S` is global; cuts land in `deferred_by_stage`.

**Two passes, one adapter.** `sources/safety.py` is the boundary: `pass1_many` (Multicall3 chain
facts + ScanHood + legacy creator index) and `pass2` (GT info → Blockscout → RobinX → GMGN, threaded,
budgeted) produce the one flat safety dict; `screen.hard_gates` runs after each. GMGN (`sources/gmgn.py`,
keyed) is the Trenches feed (`DISCOVERY_FEEDS`, one POST per run for New / Almost bonded / Migrated)
plus `/v1/token/info` wallet tags for the first `GMGN_INFO_BUDGET_PER_RUN` pass-2 tokens; its `gmgn_*`
fields are **features only** (three pre-declared candidate bands read them: `band_gmgn_clean`,
`band_new_creation`, `band_almost_bonded`), never a hard gate, and a dark GMGN is named in
`sources_dark`. `docs/GMGN_TRENCHES.md` is the operator's Trenches guide. `screen.hc_checks(feat)` is
the **single** implementation of the A-tier checks (`None` for unknown / degraded inputs);
`high_conviction` and `band_a_strict` both derive from it. `entry_lab/runtime.build_feat` is the
single normalizer to `config.FEATURE_FIELDS` (NaN/inf/NA → `None`, every key present).

**Events and the ledger.** Rows keyed `(token, alert_ts)` with `event_seq` (monotonic from 1),
`event_kind ∈ {first_sighting, band_fire, promotion}`, `fired_band`, `prior_event_seq`, `plan_name`,
`gates_mask`, `sources_dark`. `runtime.decide_events` decides; `ledger.record_rows` mints; `store.
append_verdicts` records every band's verdict for event rows only. Full gates / hc_checks / plan
live once per event in `latest_scan.json` (the point-in-time feature store). Forward cells are
write-once and time-gated; exit events come only from the row's own frozen plan and only for
`tier != "B"`.

**The three-way contract everywhere.** `http_client.get_json` returns `obj` / `NOT_FOUND` (400/404,
falsy) / `None` (network, 429, 5xx, challenge). Every source propagates it; `deferred` is retried and
**never scored**; `absent` is a fact. `quotes.py` (used only by `paper_exec` and `livebook`) applies
the on-chain-first rule: absent ⇔ V2 pair with reserves `(0,0)` and router revert/zero, or no V2 pair
and Kyber *and* ScanHood explicit no-route; any unanswered probe on the active branch ⇒ deferred.
The ledger's death test is different by design: index-absence for `DEAD_CONFIRM_TICKS` polls.

**The live book.** `selfimprove/livebook.py` keys positions by `(token, event_seq)` — a promotion
row opens a second position beside the token's B row. Feed from `origin/main:data/ledger.csv` via
`publish.origin_blob` (fetch + show, exit 128 ⇒ empty, silently). One shared buy per alert, one
`quote_sell_many` per tick, the quote-integrity gate (R1 implausible multiple, R2 uncorroborated
jump vs Dexscreener, R3 `amount_out > reserve_weth`; downside never gated), `suspect` after 5,
`unpriced` after 30 deferred, `gap_s` on every tick and fill, `gapped` = NaN for that policy when a
market-decision close follows a gap > `LIVEBOOK_MAX_SCORABLE_GAP_S`. Every state file is gitignored.

**The gates.** Exit (`improve.py`): apparatus faults first ⇒ VOID, then seven checks, sticky
nomination stamping `alert_seq`, forward-only prefix, one-shot judgment, renomination cooldown, the
one-shot demotion test, `--apply` only when every check passes and not paused. Entry
(`improve_bands.py`): VOID iff any control has `own_lb > 0` or out-selects the champion or is
inert; nine checks (paired lift, lift over controls, own LB net of cost, DSR at
`len(bands_ever_scored)`, 40 days, 150 rows, forward-only prefix, BY, coverage). `ctl_random_band`
fires at a sha256-derived delay for a sha256-selected 10 %; `ctl_inverse_band` is computed on the
fly as `champion == 0`, never read from the sidecar. `selfimprove/PAUSE` or `champion.json.locked`
⇒ evaluate and report, write nothing.

**State files with one owner.** `selfimprove/champion.json` (schema 2, `exit` + `entry_band` arms,
`history`) is written only by `champion.write_state` (read-modify-write, tmp + `os.replace`).
`selfimprove/trials.json` only grows (`policies_ever_scored`, `bands_ever_scored`,
`nominations_ever`); a deleted candidate is still a trial. `candidates/registry.json` is the one
registry for bands and policies; `register.py --scan` validates in `python3 -I -S` with the import
allowlist (`math, json, config, clf_runtime, selfimprove, typing, __future__, hashlib`). The
research diff allowlist (`research/allowlist.py`, run from the **Mac tree's** copy) admits only
`selfimprove/candidates/<name>.py` and `selfimprove/research/proposals/PROPOSAL_<date>.md`.

## Load-bearing invariants (verify.py checks these; keep them true)

- **No wall-clock in any scoring path.** `screen.py`, `bands.py` and every candidate pass an AST
  walk for `datetime`/`time`/`urllib`/`random`/`os.environ`/network. `run.py` and every other entry
  point capture `time.time()` **once** and thread `now_s`; `time.monotonic()` only for durations.
- **Fail closed only on a positive finding.** A dark or absent source passes through and is named
  in `sources_dark`; **unknown is never A** (`hc_checks` returns `None`, not `False`).
- **A can never be laxer than the hard gates** — `tier_for` composes `gates_ok and verdict is True`.
- **Controls must fail.** A control clearing either gate voids the run and alerts APPARATUS FAULT.
- **Deferred is never scored** — not in the ledger, the book, the paths, or the paper fills.
- **Every state write is atomic** (tmp + `os.replace`, `allow_nan=False` after a NaN→None pass).
- **Promoted-B rows stay in B** (intention-to-treat); no code path filters on `promoted_ts`.
- **Never mark a token seen without a market snapshot** (deferred / absent ⇒ recheck, not seen).
- **B never emits exits; cells are write-once and time-gated; forward update is one batched call.**
- **History is never rewritten after go-live.** The one rewrite (dropping the social-attribution
  files) happened before the repo went public.
- **`docs/` and `selfimprove/research/` carry no home paths and never name the shared secrets
  file** — verify greps for both. Keep `README.md` clean the same way.
- **The $Cubrate replay is never A-tier.** Literal at-alert numbers, permanent fixture.

## Named incidents (why the code looks the way it does)

- **Blockscout 403, 2026-08-23** — a Cloudflare managed challenge keyed on the User-Agent took the
  old screener dark for 20 days while alerts kept firing from cache. Produced the browser header
  sets, rotate-once-then-BOT-CHALLENGED, the cross-run backoff, `preflight.py`, the DEGRADED alert
  and the dashboard's dark share.
- **The P1 look-ahead** — the proven-dev tier read a fresh pool on an old token as a launch; 27
  golden tokens had 27 distinct deployers. The hypothesis was dropped, not patched.
- **Solana tier freeze, 39 of 42** — the ledger froze the tier at first sighting, so 39 of 42 real
  A alerts sat in the control arm and the live book was 438/439 B. Hence the event model and the
  `(token, event_seq)` book.
- **FOMO / Bull500 quote artifacts (solana)** — 16 of solana's 51 "≥3x winners" were bad quotes. Hence the
  ledger's implied-supply gate (`SUPPLY_DRIFT_MAX`) and the book's three-rule integrity gate.
- **The 199-fills durability incident (2026-08-15, solana)** — a crash between a CSV append and the state
  save replayed one fill 199 times. Fills and ticks are now flushed after the atomic book save.
- **The 472-of-1,400 fabrication (sibling corpus)** — a sibling corpus read a rate limit as "no route" and wrote
  472 dead rows. Hence the three-way contract and `paths.top_pool` not using a helper that
  collapses absent and deferred.
- **$Cubrate (2026-07-05, solana)** — an A alert at score 80.5 went −99 % in 15 min on farmed
  metrics. Hence `HC_MIN_AGE_MINUTES`, `HC_MAX_HOLDERS_PER_MIN`, `HC_MAX_TX_PER_HOLDER_H1`.
- **V4 pool ID as a pair (found at build time)** — Dexscreener reports a 32-byte pool ID for V4
  pools; querying it as a V2 pair made every LP/route leg fail and the token read as "rpc dark".
  `chain_facts_many` accepts only 20-byte pair addresses and falls back to `factory.getPair`.
- **`band_no_age` inert (found at build time)** — with `HC_MIN_HOLDERS=1000` and
  `HC_MAX_HOLDERS_PER_MIN=8`, age ≥ 125 min is implied, so dropping the age check can never flip a
  verdict. Registered as a pre-declared null; the scorecard's inert check flags it.
- **GitHub cron 13.7×/day** — measured on the live solana workflow; hence Mac dispatch and
  "correct at any cadence" discovery.

## Gotchas

- **The chain's winners come from Bankr's LongLaunchFactory (Doppler on Uniswap V4)**, not
  from V2 pairs or Flap: `config.REFERENCE_TOKENS` (CATGPT, ANTHROPIG) are the recall fixtures in
  verify section M. Their `owner()` is `PROTOCOL_OWNERS` ("protocol", passes), their LP is the
  hook's custody (`lp_check_source v4_launchpad:bankr`, no %), their round trip comes from Kyber,
  and the launch tx is often sent by an app/agent wallet — the creator gate then reads the dead
  fraction of the launcher's prior launches, never the count alone. Every token address ends in
  `1e18` (salt-mined). Numeraires are tokenized stocks / "1x Long" tokens / USDG / native ETH /
  other memecoins, so a V4 `Initialize` leg is a numeraire when it is a quote token, declared by
  a `Create` in the same window, or repeated across pools; otherwise the row is dropped.
- **Pons (`PONS_FACTORY`) is the largest launchpad by count** (750–1,250 launches/hour, 1–4 %
  ever indexed): its Create log is a source, absent launches get ONE 30-min recheck
  (`RECHECK_SCHEDULE_BY_KIND`), and `DISCOVERY_MAX_LOG_TOKENS_PER_RUN` is 200 for that reason.
  Hook-less V4 pools are a source too (`V4_DISCOVERY_HOOKS_ONLY = False`).
- **The live champion is `band_volume_early`, set by the operator on 2026-09-12** (age ≤ 30 min,
  hour-1 volume ≥ $50k, buys ≥ 2× sells, mcap ≤ $2M). `DEFAULT_ENTRY_BAND` is still
  `band_a_strict`: it is the fallback and the demotion target, not the alerted band. Do not
  "fix" the champion back without the operator; `champion.py --set` is the path either way.
- **MIZUKARA remains only as the V2-mechanics smoke fixture** (a renounced owner, a burned V2
  pair, router legs) in the sources' `__main__` blocks; it is not a reference for what to find.

- **GeckoTerminal is a hard 30/min per IP, shared on the Mac** with the sibling screeners; the Mac
  rate is 0.25 Hz (0.4 Hz gave ~47 % 429s, measured on the solana screener). The runner has its own IP and uses 0.4 Hz.
- **`git fetch` + `git show origin/main:` — never `git pull`** in the 60 s book. A collection job
  must not be able to move HEAD or conflict.
- **The Mac tree must equal `origin/main` before the Sunday jobs.** `run_improve.sh` ff-merges
  first and again after publishing; a diverged tree is reported, never forced. Hand-edits left
  uncommitted on the Mac will make it diverge.
- **A sibling voice assistant reads the alphabetically-first `*ledger*.csv` under this repo.**
  `data/ledger.csv` must stay that file: no `*ledger*.csv` under `.claude/`, `.github/`, `cache/`
  or `data/archive/`; temp worktrees are created with `mkdtemp()` *outside* the repo.
- **The runner has no `entry_bot` and no scipy**, so the DSR pin, the statsmodels BY comparison,
  launchd and publish checks are `mac_only` and print SKIP under `GITHUB_ACTIONS`. `entry_bot` is
  `sys.path.append`ed, never inserted — both repos have a top-level `config.py`.
- **ScanHood's sell simulation fails on some tokens** (`sellable: null`, "could not simulate a
  sell"). `None` is unknown, never `False` — pass-through, not a reject.
- **RobinX does not attribute Flap (launchpad) launches**: `deployer: null` for the MIZUKARA dev.
  A null RobinX record must never override the creation-tx-sender attribution.
- **V4 pool IDs are 32 bytes**; V3 pools are not V2 pairs either. Only a 42-char `0x` string is a
  pair; everything else goes through `factory.getPair`, which answers `0x0` as a fact.
- **Dexscreener omits unknown addresses from a batch response instead of 404ing** — absence is
  inferred from the list, never from an HTTP status; a non-list body is deferred, not absent.
- **`http_client` treats only 400/404 as absent**; ScanHood's explicit no-route is a 422 and
  surfaces as deferred after retries. `quotes.py` handles it; do not read it as death elsewhere.
- **GMGN's `/v1/trenches` answers code 0 with EMPTY columns to a body without `version: v2` and
  `quote_address_type`** — a trap that reads like "nothing new", not an outage; `gmgn.build_trenches_body`
  is the one place the shape lives (GMGN's own client's). A 429 there is a **ban** whose cooldown
  extends 5 s per retry, so `HOST_429_TERMINAL` makes it one request, deferred, dark for the run. The
  auth timestamp must be fresh per request (`AUTH_TIMESTAMP_EXPIRED` when run.py's start-of-run `now_s`
  reached pass 2) — the one wall-clock in a source module, pinned by verify to `gmgn._query`. Its
  Trenches allow-list omits `pons_v2` and bare V2/V3/V4 pools, so the feed is a hedge, never the cursor.
- **The workflow does `git add data/ docs/`** — anything new and non-ignored under `data/` is
  committed automatically; the livebook files, `cache/` and `data/backups/` are gitignored.
- **Alerts go out EARLY in a run**: tokens the champion band selects on pass-1 facts get pass 2
  first and are alerted before the rest of pass 2, the watchlist refresh and the forward update;
  `latest_scan.json.stage_seconds.alert_sent` is the measured latency inside the run.
- **Entry lag is 3–8 min by construction** (dispatch + run + commit + fetch); refusals past
  `MAX_ENTRY_LAG_S` land in `data/livebook_missed.jsonl`. Read `entry_lag_s` before trusting a
  live number.
- Everything is stdlib urllib + certifi; system certs fail with `CERTIFICATE_VERIFY_FAILED`.
- Never `echo` a key into a config file; `cloud_secrets.py` and `load_credentials()` are the only
  paths, and an assistant may not enter credentials.

## Ethos

Inherited from the workspace and non-negotiable: the decision criterion is a **day-clustered
bootstrap lower bound, never a mean**; multiple-testing correction is Benjamini–**Yekutieli**;
Deflated Sharpe is deflated by every trial ever run in the family; a bound from fewer than
`MIN_BOOTSTRAP_CLUSTERS` clusters is not a bound; every experiment ships with a kill condition
(A-vs-B, the controls, K5, the demotion test); nomination is forward-only and judged once; nothing
is promoted on in-sample evidence, however good it looks. The honest prior is that this class of
trade is −EV and that **"NO CHANGE" for months is the expected outcome**. Preserve the measured
numbers in the README when editing it — adjectives are not evidence.
