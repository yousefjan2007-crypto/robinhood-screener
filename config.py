"""
Central configuration for robinhood_screener — an honest memecoin screener / alerter for
Robinhood Chain (chainId 4663, an Arbitrum-Orbit L2), rebuilt 2026-09-12 as a port of the
solana_screener architecture: hard gates → soft score → A/B tiers (A alerted, B silently
ledgered as the control) → write-once forward-return ledger → paper fills at real quotes →
live multi-policy paper book → weekly self-improvement behind a statistical gate.

Self-contained sibling project: no imports from other projects (the statistical guards in
selfimprove/ borrow ~/entry_bot/stats.py by sys.path APPEND, which is why the BOOTSTRAP_* and
FDR_Q constants below must exist with these names). Push secrets are reused from the same
shared Mac secrets file signal_lab and solana_screener read.

Ethos (shared with the rest of the workspace): every threshold lives HERE so the whole
screen re-tunes from one file; any randomness goes through np.random.default_rng(SEED);
no wall-clock ever enters a compute/scoring path (each entry point captures time.time()
ONCE and threads it through). ALERT-ONLY — nothing here touches keys or funds; paper fills
are quotes only. Not financial advice.
"""
from __future__ import annotations

import json
import os

# ── paths ────────────────────────────────────────────────────────────────────────
HOME = os.path.expanduser("~")
ROOT = os.path.dirname(os.path.abspath(__file__))
VRP_BACKTEST = os.path.join(HOME, "vrp_backtest")   # the shared Mac secrets file lives here
SOLANA_SCREENER = os.path.join(HOME, "solana_screener")   # sibling screener: its GMGN key is borrowed (read-only)

DATA_DIR = os.path.join(ROOT, "data")       # ledger + state — COMMITTED back on cloud runs
CACHE_DIR = os.path.join(ROOT, "cache")     # ephemeral API caches — gitignored
DOCS_DIR = os.path.join(ROOT, "docs")       # GitHub Pages source (the phone dashboard)
ARCHIVE_DIR = os.path.join(DATA_DIR, "archive", "proven_dev")
SELFIMPROVE_DIR = os.path.join(ROOT, "selfimprove")

LEDGER_PATH = os.path.join(DATA_DIR, "ledger.csv")            # MUST stay the alphabetically
                                                              # first *ledger*.csv (jarvis)
STATE_PATH = os.path.join(DATA_DIR, "alert_state.json")       # cooldowns + degradation clocks
SEEN_PATH = os.path.join(DATA_DIR, "seen.json")               # short-TTL reject memory
RECHECK_PATH = os.path.join(DATA_DIR, "recheck.json")         # young tokens to look at again
WATCHLIST_PATH = os.path.join(DATA_DIR, "watchlist.json")     # survivors inside the 24h window
CURSOR_PATH = os.path.join(DATA_DIR, "discovery_cursor.json") # last block scanned for logs
SCAN_PATH = os.path.join(DATA_DIR, "latest_scan.json")        # the point-in-time feature store
BAND_VERDICTS_PATH = os.path.join(DATA_DIR, "band_verdicts.csv")
PAPER_LEDGER_PATH = os.path.join(DATA_DIR, "paper_ledger.csv")       # A book (sorts AFTER ledger.csv)
PAPER_POSITIONS_PATH = os.path.join(DATA_DIR, "paper_positions.json")
LIVEBOOK_SUMMARY_PATH = os.path.join(DATA_DIR, "livebook_summary.json")  # weekly, from the Mac
ENTRY_LAB_HISTORY_PATH = os.path.join(DATA_DIR, "entry_lab_history.jsonl")
RUN_LOG_PATH = os.path.join(DATA_DIR, "run_log.jsonl")            # one line per run, rolling 7 days
RUN_LOG_KEEP_S = 7 * 86400
PROPOSALS_DIR = os.path.join(DATA_DIR, "proposals")
CHAMPION_PATH = os.path.join(SELFIMPROVE_DIR, "champion.json")
PAUSE_PATH = os.path.join(SELFIMPROVE_DIR, "PAUSE")
TRIALS_PATH = os.path.join(SELFIMPROVE_DIR, "trials.json")
IMPROVE_HISTORY_PATH = os.path.join(SELFIMPROVE_DIR, "improve_history.jsonl")
REGISTRY_PATH = os.path.join(SELFIMPROVE_DIR, "candidates", "registry.json")
LEGACY_TOKEN_CREATORS_PATH = os.path.join(ARCHIVE_DIR, "token_creators.json")

for _d in (DATA_DIR, CACHE_DIR, PROPOSALS_DIR):
    os.makedirs(_d, exist_ok=True)

IS_CI = os.environ.get("GITHUB_ACTIONS") == "true"   # the Actions runner has its own IP
SEED = 42  # any randomness → np.random.default_rng(SEED); never global np.random.*

# ── the scan keeper (.github/keeper.sh on GitHub Actions; never on the Mac) ──────
# The scan loop is a self-chaining Actions job: one keeper run scans every KEEPER_CADENCE_S,
# dispatches its successor into the other concurrency slot KEEPER_HANDOFF_LEAD_S before its
# own KEEPER_MAX_S, and hands off through data/keeper_handoff.json (ready → done). The */5
# watchdog restarts a dead keeper and alerts SCAN STALE past KEEPER_STALE_S. keeper.sh reads
# these with one `python3 -c "import config; ..."` — never a literal in the script.
KEEPER_CADENCE_S = 240             # one scan per 240 s (the retired Mac dispatch's interval)
KEEPER_MAX_S = 20400               # 340 min per keeper run: margin under the job's timeout-minutes 355
KEEPER_HANDOFF_LEAD_S = 600        # dispatch the successor this long before KEEPER_MAX_S
KEEPER_HANDOFF_WAIT_S = 600        # a successor waits this long for the predecessor's `done`, then starts anyway
KEEPER_STALE_S = 1800              # the tripwire (dashboard red, watchdog SCAN STALE): a scan older than 30 min
PAGES_EVERY_N_ITERATIONS = 2       # dispatch the Pages deploy on every 2nd successful push (its own workflow/group)
KEEPER_CIRCUIT_FAILURES = 3        # keeper runs concluded `failure` inside KEEPER_CIRCUIT_WINDOW_S that open the breaker:
KEEPER_CIRCUIT_WINDOW_S = 7200     # no watchdog restart and no exit-3 self-dispatch until a human looks (2 h)

# ── statistical guards (selfimprove/) ────────────────────────────────────────────
# Read by ~/entry_bot/stats.py (imported by sys.path APPEND so THIS config wins).
FDR_Q = 0.10                 # Benjamini-YEKUTIELI (valid under arbitrary dependence), not BH:
                             # exit policies / entry bands are correlated restatements of each other
BOOTSTRAP_REPS = 4000
BOOTSTRAP_ALPHA = 0.025      # the decision criterion is the 2.5th percentile of a day-clustered
                             # bootstrap, never a mean
MIN_BOOTSTRAP_CLUSTERS = 12  # PRINT floor only: a bound from fewer clusters is not a bound;
                             # promotion floors are far higher (IMPROVE_/BAND_PROMOTE_MIN_CLUSTERS)

# ── HTTP tunables ────────────────────────────────────────────────────────────────
HTTP_TIMEOUT = 20
HTTP_RETRIES = 3
USER_AGENT = "robinhood_screener research (personal use)"
DEXSCREENER_RATE_HZ = 4.0          # undocumented for tokens/v1; solana runs it at this rate
GECKOTERMINAL_RATE_HZ_MAC = 0.25   # free tier is a hard 30/min PER IP, shared on the Mac with
                                   # solana-listener/-livebook/backfills; 0.4 Hz measured 47% 429s
GECKOTERMINAL_RATE_HZ_RUNNER = 0.4 # the Actions runner is alone on its IP; still 24/min
GECKOTERMINAL_RATE_HZ = GECKOTERMINAL_RATE_HZ_RUNNER if IS_CI else GECKOTERMINAL_RATE_HZ_MAC
BLOCKSCOUT_RATE_HZ = 2.0           # undocumented; 20 rapid calls returned 200 (2026-09-12)
RPC_RATE_HZ = 2.0                  # public RPC 429s under bursts of ~5+/s; 2 Hz leaves headroom
                                   # for retries. Throughput comes from Multicall3, not from Hz.
SCANHOOD_RATE_HZ = 3.0             # free tier ~5 req/s per IP
ROBINX_RATE_HZ = 0.5               # free-tier caps unpublished ("rate-capped") → conservative
KYBER_RATE_HZ = 1.0                # undocumented; 10 rapid calls OK
GMGN_RATE_HZ = 0.5                 # openapi.gmgn.ai: a weighted leaky bucket (rate 20 / capacity 20; trenches weight 3,
                                   # token/info weight 1) on paper, but the FREE tier banned a probe after 13 spaced calls
                                   # (429 RATE_LIMIT_BANNED, 2026-09-12) and a ban extends 5 s per retry — so 0.5 Hz, and
                                   # a 429 is terminal for the run (HOST_429_TERMINAL), never waited out
HOST_RATE_HZ = {                   # assembled from the named constants — http_client has no literals
    "api.dexscreener.com": DEXSCREENER_RATE_HZ,
    "api.geckoterminal.com": GECKOTERMINAL_RATE_HZ,
    "robinhoodchain.blockscout.com": BLOCKSCOUT_RATE_HZ,
    "rpc.mainnet.chain.robinhood.com": RPC_RATE_HZ,
    "scanhood.xyz": SCANHOOD_RATE_HZ,
    "api.robinx.io": ROBINX_RATE_HZ,
    "aggregator-api.kyberswap.com": KYBER_RATE_HZ,
    "openapi.gmgn.ai": GMGN_RATE_HZ,
}
DEFAULT_RATE_HZ = 2.0
HOST_429_TERMINAL = ("openapi.gmgn.ai",)   # a 429 here is a BAN: one request, deferred, dark for the run (no wait-and-retry)
ENRICH_CACHE_MIN = 2         # dexscreener market snapshots
INFO_CACHE_MIN = 10          # mutable per-token facts (holders, top-10, GT info, RobinX wallet)
GT_NEW_POOLS_CACHE_S = 60    # the new_pools feed page
SCANHOOD_FEED_CACHE_S = 120  # ScanHood launch feed (cron-refreshed upstream)
SCANHOOD_STOCKS_CACHE_S = 24 * 3600   # the official tokenized-stock list
SCANHOOD_REP_CACHE_S = 6 * 3600       # deployer-reputation dump (13.5 MB)
BLOCKSCOUT_LOGS_MAX_PAGES = 4         # 50 decoded logs/page; a creation tx has ~34
FOREVER_CACHE_DAYS = 3650    # immutable facts: creation tx, sender, decimals, pair address
# Blockscout sits behind a Cloudflare managed challenge that is keyed on the User-Agent: the
# project UA ALWAYS gets 403 (72k of them, 2026-08-23 → 09-12). Both header sets below were
# verified 200 from the Mac on 2026-09-12. Policy (http_client): on a bot-challenge response
# rotate to the next set and retry ONCE, then record BOT-CHALLENGED and stop for the run.
_CHROME_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")
BLOCKSCOUT_HEADER_SETS = [
    {"User-Agent": _CHROME_UA, "Referer": "https://robinhoodchain.blockscout.com/"},
    {"User-Agent": _CHROME_UA, "sec-ch-ua": '"Chromium";v="140", "Not=A?Brand";v="24"',
     "sec-ch-ua-platform": '"macOS"'},
]
HOST_HEADER_SETS = {"robinhoodchain.blockscout.com": BLOCKSCOUT_HEADER_SETS}
BLOCKSCOUT_ENABLED_ON_RUNNER = True   # flipped by the Phase-2 pre-flight if the runner IP is challenged
BLOCKSCOUT_BACKOFF_S = 6 * 3600       # after two consecutive challenged runs, skip the host this long

# ── chain constants ──────────────────────────────────────────────────────────────
CHAIN_ID = 4663                    # "HOOD" on a T9 keypad
RPC_URL = "https://rpc.mainnet.chain.robinhood.com"
BLOCKSCOUT_BASE = "https://robinhoodchain.blockscout.com"
GT_NETWORK = "robinhood"           # GeckoTerminal network slug
DEX_CHAIN = "robinhood"            # Dexscreener chain slug
# ── GMGN (openapi.gmgn.ai) — the Trenches feed + the wallet-tag second opinion; keyed ──────
# GMGN lists Robinhood Chain as a first-class chain (slug `robinhood`; verified live 2026-09-12/13:
# POST /v1/trenches returned 60 rows per column, GET /v1/token/info and /v1/token/security answer).
# Its Trenches feed applies a fixed launchpad allow-list that omits pons_v2 and bare V2/V3/V4 pools
# (72 of the top-100 rank rows on 2026-09-12), so it is a discovery HEDGE beside the log cursor,
# never a replacement. The body shape below is GMGN's own client's (OpenApiClient.ts
# buildTrenchesBody): without `version: v2` + `quote_address_type` the server answers code 0 with
# EMPTY columns — a trap, not an outage.
GMGN_CHAIN = "robinhood"
GMGN_BASE = "https://openapi.gmgn.ai"
GMGN_TOKEN_URL = "https://gmgn.ai/{chain}/token/{token}"          # the terminal deep link in every alert
BLOCKSCOUT_TOKEN_URL = BLOCKSCOUT_BASE + "/token/{token}"          # the explorer deep link beside it
GMGN_TRENCHES_COLUMNS = ("new_creation", "near_completion", "completed")   # New / Almost bonded / Migrated
GMGN_TRENCHES_LIMIT = 80           # the documented maximum per column
GMGN_TRENCHES_FILTERS = ("offchain", "onchain")
GMGN_QUOTE_ADDRESS_TYPES = (11, 20, 24, 12, 0)   # TRENCHES_QUOTE_ADDRESS_TYPES["robinhood"] in GMGN's client
GMGN_TRENCHES_CACHE_S = 120        # one POST per run; the feed moves every minute
GMGN_INFO_BUDGET_PER_RUN = 20      # /v1/token/info calls per run (weight 1 each), pass-2 order
BLOCK_TIME_S = 0.101               # Blockscout /api/v2/stats average_block_time, 2026-09-12
WETH = "0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73"           # router.WETH()
UNIV2_FACTORY = "0x8bceaa40b9acdfaedf85adf4ff01f5ad6517937f"  # router.factory(); owns the MIZUKARA pair
UNIV2_ROUTER = "0x89e5db8b5aa49aa85ac63f691524311aeb649eba"   # Router02: getAmountsOut works on V2 pools,
                                                              # reverts on V3/V4
V3_FACTORY = "0x1f7d7550b1b028f7571e69a784071f0205fd2efa"     # emits PoolCreated (V3-style)
MULTICALL3 = "0xcA11bde05977b3631167028862bE2a173976CA11"     # canonical Multicall3, deployed on 4663
FLAP_ROUTER = "0x26605f322f7fF986f381bB9A6e3f5DAb0bEaEb09"    # Flap launchpad (emits TokenCreated)
# Launchpad factories (deployer attribution = creation tx SENDER, never the factory).
FACTORIES = {
    "flap": FLAP_ROUTER,
    "pons": "0xA5aAb3F0c6EeadF30Ef1D3Eb997108E976351feB",
    "pons2": "0xD9ec2db5F3d1B236843925949fE5bd8a3836FccB",
    "pons_legacy": "0x0c37a24F5D23A486FA692d1500881d698B1F77a4",
    "launchhood": "0x62B33a039D289cBDa50EBEb72FE4261449E61bCF",
    "scanhood": "0x75eCa6306FFA2D6A66AD841072cF6E805F0D35A7",
    "scanhood2": "0x4271Be655d27B8E4bB5CE4C98C6feF0678b45056",
    "scanhood3": "0x7A8Db326E50a6E8CbC0616e0636B63737b5E84c8",
    "scanhood4": "0xd80d33b3d486797f34fd1c694232d423f8c609d8",   # from ScanHood's launch feed
    "scanhood5": "0xcb242be59fc4469c43538aac8a6adcdb8f231615",
    "scanhood6": "0x18a4a3f068b5cb951475f4c99f2b4c32e6e7c0f7",
}
# event topics (keccak of the signature), recovered from Blockscout decoded logs 2026-09-12
TOPIC_PAIR_CREATED = "0x0d3648bd0f6ba80134a33ba9275ac585d9d315f0ad8355cddefde31afa28d0e9"
TOPIC_POOL_CREATED = "0x783cca1c0412dd0d695e784568c96da2e9c22ff989357a2e8b1d9b2b4e6b7118"
TOPIC_FLAP_TOKEN_CREATED = "0x504e7f360b2e5fe33cbaaae4c593bc55305328341bf79009e43e0e3b7f699603"
TOPIC_FLAP_TOKEN_BOUGHT = "0xa800a2038683844fac66747f771bfdfae862eb28b16bcfa387afa9fbacce8ff7"
TOPIC_FLAP_PROGRESS = "0x4c35e20d1e9bce377c7d9ec1572d934e46d62961f4da8af5beb8002d5906742d"
TOPIC_SWAP_V2 = "0xd78ad95fa46c994b6551d0da85fc275fe613ce37657fb8d5e3d130840159d822"
# ── the launchpad that produced the chain's mid-cap winners (measured 2026-09-12) ──
# CATGPT ($15M) and ANTHROPIG ($4.7M) both came from Bankr's LongLaunchFactory, a Doppler
# integration: DopplerERC20V1 EIP-1167 clones auctioned in Uniswap V4 pools under the Doppler
# hook, paired against tokenized stocks / "1x Long" tokens / USDG / native ETH — never only
# WETH. ~3,250 launches/day; ~5 % clear the market gates. No V2 pair, so the router legs are
# blind (Kyber routes them keylessly); the token owner is the protocol's shared contract, so
# "renounced" is impossible by design; the launch tx is often sent by a shared service wallet.
LONGLAUNCH_FACTORY = "0x1Eef016F22A943abC7DD11422EDeE9D235942104"   # eip1967 proxy; GT dex id bankr-robinhood
TOPIC_LONGLAUNCH_CREATE = "0x04c10fe2cc69507cbff4c84fc99414ca507db49d5c39eff84b67ea319f43ccf0"
                                   # topics[1] token, topics[2] launcher; data words: [offset, numeraire,
                                   # initializer, hook, migrator, governance(0xdead = none), ...]
V4_POOL_MANAGER = "0x8366a39CC670B4001A1121B8F6A443A643e40951"
TOPIC_V4_INITIALIZE = "0xdd466e674ea557f56295e2d0218a125ea4b4f0f6f3307b95f85e6110838d6438"
                                   # Initialize(id, currency0, currency1, fee, tickSpacing, hooks, sqrtPriceX96, tick):
                                   # topics[1] pool id, topics[2..3] currencies; data words [fee, tickSpacing, hooks, ...]
DOPPLER_HOOK = "0x4e3468951D49f2EEa976eD0D6e75fFCb44a9a544"
TRUSTED_V4_HOOKS = {DOPPLER_HOOK.lower(): "bankr"}   # hook → launchpad name; liquidity is in the hook's custody
V4_DISCOVERY_HOOKS_ONLY = False    # hook-less V4 pools held the day's biggest winners (AnsemCat $102M, MEME, BONER,
                                   # FLYBRAIN, CME on 2026-09-12); the numeraire rule drops the ambiguous rows
NUMERAIRE_MIN_POOLS_IN_WINDOW = 2  # a V4 currency seen on >= 2 pools in one log window is a numeraire, not a launch
PROTOCOL_OWNERS = {                # token owner() addresses that are a launchpad's shared contract, not a dev
    "0xeb7c034704ef8dcd2d32324c1545f62fb4ad0862": "bankr",   # owner of every LongLaunch DopplerERC20V1 clone
}
LAUNCH_SERVICE_MIN_CREATES = 20    # a launcher with this many prior Create events is an agent/service wallet:
                                   # its history says nothing about THIS token's dev (creator gate passes through)
LAUNCHPAD_CREATOR_MAX_PRIOR_TOKENS = 10   # launcher wallets on Bankr are often apps/agents launching for many
                                   # users (CATGPT's launcher: 7 launches, 16.8k txs); the serial-deployer risk is
                                   # still caught by CREATOR_MAX_DEAD_FRAC over the launcher's PRIOR tokens
SCANHOOD_BUDGET_PER_RUN = 24       # ScanHood answers in 0.3–7 s per token (measured 2026-09-12) and is a WEAK
SCANHOOD_WORKERS = 4               # fallback (verdict / sellable / template); 56 sequential calls ate the whole
                                   # 240 s budget in pass 1, so it is fetched concurrently, deepest liquidity first
KYBER_ROUNDTRIP_BUDGET_PER_RUN = 8    # no-V2-pair tokens probed through Kyber (2 calls each at 1 Hz), liq-desc;
                                   # 12 put a Mac dry run at 170 s of the 200 s budget
KYBER_PROBE_WETH_WEI = 10**16      # 0.01 WETH, same probe as the router legs
# Pons (GT dex id pons-v2-dex): the chain's largest launchpad by count — 750–1,250 launches per
# HOUR measured 2026-09-12, each with a singleton-AMM pool (32-byte ids) at creation; only 1–4 %
# ever get a Dexscreener market and < 1 % clear the market gates (≈ 100–200/day). FRONTIER's
# second listing, DOGGO, UP and MONEY came through it. The GT new-pools feed is dominated by it.
PONS_FACTORY = "0x7eD598BcEf8bd9Edd8C97A195C6d13f40801EC7e"
TOPIC_PONS_CREATE = "0x8d4aad4953d0ca700d468f3753aa14432d1b35b43ec6409f051fb6aa43a89607"
                                   # topics[1] token, topics[2] pool id (32 bytes), topics[3] creator; data: 3 amounts
DISCOVERY_LOG_SOURCES = {          # address → (kind, topic0)
    UNIV2_FACTORY: ("pair_v2", TOPIC_PAIR_CREATED),
    V3_FACTORY: ("pool_v3", TOPIC_POOL_CREATED),
    FLAP_ROUTER: ("flap_create", TOPIC_FLAP_TOKEN_CREATED),
    LONGLAUNCH_FACTORY: ("longlaunch_create", TOPIC_LONGLAUNCH_CREATE),
    V4_POOL_MANAGER: ("pool_v4", TOPIC_V4_INITIALIZE),
    PONS_FACTORY: ("pons_create", TOPIC_PONS_CREATE),
}
# launchpad launches are ~99 % absent from Dexscreener at the first scan and mostly stay so: ONE
# recheck at 30 min, never the 6 h slot (which would hold ~20k dead launches in recheck.json)
RECHECK_SCHEDULE_BY_KIND = {"pons_create": (1800,), "longlaunch_create": (1800,), "pool_v4": (1800,)}
# reference winners (what "a coin worth finding" looks like on this chain; at-launch numbers in verify.py)
REFERENCE_TOKENS = {
    "CATGPT": "0xd6FDE6a3Fc6Ab2d83b2BE58383944CA1baDe1E18",      # launched 2026-09-11 23:40Z, $15M on 09-12
    "ANTHROPIG": "0x351Ab2C51e223B28D219fE28cc3956410CC11e18",   # launched 2026-09-11 23:55Z, $4.7M on 09-12
}
# 4-byte selectors
SEL_OWNER = "0x8da5cb5b"
SEL_BALANCE_OF = "0x70a08231"
SEL_TOTAL_SUPPLY = "0x18160ddd"
SEL_DECIMALS = "0x313ce567"
SEL_GET_AMOUNTS_OUT = "0xd06ca61f"
SEL_GET_PAIR = "0xe6a43905"
SEL_GET_RESERVES = "0x0902f1ac"
SEL_AGGREGATE3 = "0x82ad56cb"
BURN_ADDRESSES = ("0x000000000000000000000000000000000000dead",
                  "0x0000000000000000000000000000000000000000")
LP_LOCKER_ADDRESSES = ()           # none verified on 4663 yet; extend when one is
NATIVE_ETH_SENTINELS = ("0x0000000000000000000000000000000000000000",   # uniswap-v4 native-ETH pools
                        "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee")   # pons-v2 native-ETH pools
USDG = "0x5fc5360d0400a0fd4f2af552add042d716f1d168"           # Global Dollar, the second most common V4 leg
QUOTE_TOKENS = {WETH.lower(), USDG, *NATIVE_ETH_SENTINELS}   # the "other leg" of a pair; the non-quote leg is the candidate
UNIV2_FEE_BPS = 30                 # 997/1000 constant-product fallback when the router reverts
MULTICALL_CHUNK = 50               # calls per aggregate3 eth_call
CREATION_WINDOW_BLOCKS = 500_000   # attribution fallback: backward eth_getLogs window per step
CREATION_MAX_LOOKBACK_BLOCKS = 5_000_000   # ~6 days; older tokens are left to the explorer
MIN_LOG_WINDOW = 512               # the halving floor for range-too-large errors
# canonical test-case anchors (module smoke tests / verify.py fixtures)
MIZUKARA = "0x407470F85e0b342a52AaE2F191E135cEF2947777"
MIZUKARA_DEV = "0xeB5ce5008aC0856eD50Edc224b90EEE31558ed3D"
MIZUKARA_POOL = "0xaEca58d7971EAC1909b512b5DE0129791883A906"
# Infra/RWA are not launches: tokenized stocks (ScanHood official feed) are excluded dynamically;
# this symbol backstop catches bridged/infra assets besides.
EXCLUDE_SYMBOLS = {"WETH", "USDG", "USDE", "SYRUPUSDG", "USDC", "USDT", "WBTC", "CBBTC", "VIRTUAL"}
EXCLUDE_SYMBOL_PATTERNS = (r"x\d+[LS]$",)   # leveraged tokenized-stock legs (OPENAIx1L, NVDAx3L): LongLaunch
                                            # numeraires, not launches; GT's new-pools feed cannot tell them apart


# ── discovery ─────────────────────────────────────────────────────────────────────
# Exact discovery is a block cursor over factory/launchpad logs; it is correct at ANY cadence
# (GitHub's cron fired 13.7×/day against a nominal 288 on this account — measured 2026-09-12).
DISCOVERY_BACKFILL_BLOCKS = 3_000        # ~5 min at 0.101 s/block: the first run with no cursor
DISCOVERY_MAX_CATCHUP_BLOCKS = 300_000   # ~8.4 h: after a longer outage skip ahead and log the gap
LOG_WINDOW_BLOCKS = 100_000              # Flap ≈ 4k logs / 100k blocks — under the node's 10k cap
DISCOVERY_MAX_LOG_TOKENS_PER_RUN = 200   # log tokens are NEVER truncated: the cursor advances only
                                         # to the block of the last log actually processed
DISCOVERY_FEEDS = ("gt_new_pools", "gmgn_trenches")      # cheap hedge for factories not in the log set; the
                                         # scanhood.launch_feed / robinx.feed_new adapters exist but are off
GT_NEW_POOLS_PAGES = 2
DISCOVER_QUOTA = {"logs": 200, "watchlist": 60, "rechecks": 40, "feeds": 20}  # unused quota spills forward
MAX_DISCOVER = 400                       # bounds only the Dexscreener enrich set (30 addrs/call); Pons + hook-less
                                         # V4 + Bankr ≈ 100 log tokens per 5-min run, 99 % absent → cheap
SEEN_TTL_S = 6 * 3600                    # rejects only; the ledger dedups survivors
RECHECK_SCHEDULE_S = (1800, 21600)       # a token that failed ONLY size gates, or has no pair yet,
                                         # is re-enriched at +30m and +6h
RECHECK_MAX = 3000                       # ≈ 2 h of launchpad launches at one 30-min recheck each
RECHECK_PER_RUN = 40
GT_INFO_BUDGET_PER_RUN = 8               # full pass-2 tokens per run: Blockscout answers in ~5 s per call (measured
                                         # 2026-09-12), so a full pass 2 is ~13 s per token even after the trims
GT_INFO_REFRESH_S = 1800                 # watched tokens refresh pass-2 facts at most this often
SAFETY_REFRESH_S = 1800                  # ...and chain facts (one Multicall3 batch) at most this often
WATCH_REFRESH_PER_RUN = 6                # pass-2 refreshes per run, closest-to-A first
WATCH_MAX_GATE_FAILS = 2                 # consecutive hard-gate failures evict a watched token
RUN_TIME_BUDGET_S = 240                  # GLOBAL: checked between tokens in every stage; cuts are
                                         # listed in latest_scan.json.deferred_by_stage, never seen
PASS2_WORKERS = 3                        # per-host throttle lock serialises each host anyway
BAND_WATCH_WINDOW_S = 24 * 3600          # solana promotions arrived median 8.9h, max 26.2h after sighting
BAND_MAX_EVENTS_PER_TOKEN = 4            # first sighting + champion promotion + 2 band fires

# ── HARD GATES (reject a token outright; EVM semantics) ───────────────────────────
# A gate fails closed ONLY on a positive finding from a source that answered; an absent or
# dark source passes through and is named in sources_dark (the GMGN contract from solana).
LIQ_FLOOR_USD = 10_000.0           # <$10k = brutal slippage / no exit
MIN_VOL_H24_USD = 20_000.0         # need real flow to be able to sell
TOP10_MAX_PCT = 40.0               # DEXTools danger line (contract + burn addresses excluded)
DEV_MAX_PCT = 5.0                  # DEXTools danger line
LP_LOCKED_MIN_PCT = 90.0           # LP burned (0xdead/0x0) or held by a known locker
HONEYPOT_PROBE_WETH_WEI = 10 ** 16 # 0.01 WETH buy leg, then sell the proceeds back
SELL_TAX_MAX_ROUNDTRIP_PCT = 15.0  # 2×0.3% fee + impact: 1.44% measured on MIZUKARA at the 0.01 WETH
                                   # probe (2026-09-12); >15% is a tax trap
REQUIRE_OWNER_RENOUNCED = True     # owner() must be 0x0 (or absent); an owner can mint/pause/blacklist
REJECT_IF_SCAM = True              # Blockscout's chain-native is_scam flag
TEMPLATE_BLOCKLIST: set = set()    # proxy implementation names known bad (empty until measured)
CREATOR_MAX_PRIOR_TOKENS = 3       # serial deployers = scam factories
CREATOR_DEAD_MCAP_USD = 30_000.0   # a prior launch below this counts as dead
CREATOR_MAX_DEAD_FRAC = 0.5        # reject if > half of prior launches are dead (min 2 prior)
SNIPER_FIRST_BLOCKS = 3            # swaps in the first N blocks after the pair …
SNIPER_SWAPS_MAX = 20              # … above this = bot cluster

# ── SOFT SCORE (rank survivors 0-100; ported verbatim from solana) ────────────────
HOLDERS_SAFE = 1000
HOLDERS_DANGER = 200
VMC_CAP = 3.0
AGE_MIN_MINUTES = 15
AGE_SWEET_MINUTES = 360
AGE_MAX_MINUTES = 4320
SOFT_WEIGHTS = {"vol_mcap": 0.30, "buy_sell": 0.20, "holders": 0.20,
                "concentration": 0.15, "age": 0.15}

# ── risk / exit discipline shipped inside each alert (alert-only) ─────────────────
STACK_USD = 500.0
POSITION_PCT = 0.02                # $10 per alert — tiny by design
MAX_CONCURRENT = 10
HARD_STOP_PCT = 0.50
TP_LADDER = [(2.0, 0.50), (5.0, 0.25), (10.0, 0.15)]
MOONBAG_PCT = 0.10

# ── the default A-tier band ("band_a_strict") — the HC checks in screen.hc_checks ─
# A-tier means best SURVIVAL odds under every gate, NOT predicted ROI. On solana the strict
# band was the best of 14 bands tested and every relaxation was worse (n=16 — hold it loosely).
HC_MIN_SCORE = 70.0
HC_TOP10_MAX_PCT = 20.0
HC_MIN_HOLDERS = 1000
HC_MIN_LIQ_USD = 25_000.0
HC_CREATOR_MAX_PRIOR = 1
HC_MIN_AGE_MINUTES = 90            # $Cubrate: 60% of rugs collapse <20 min post-graduation
HC_MAX_HOLDERS_PER_MIN = 8.0       # organic-growth cap (Cubrate: 67/min = wallet farm)
HC_MAX_TX_PER_HOLDER_H1 = 3.0      # bot-churn cap
HC_MAX_ROUNDTRIP_PCT = 8.0         # near-fee-only slippage on the probe
HC_REQUIRE_TEMPLATE_KNOWN = True   # impl name whitelisted or source verified
TEMPLATE_WHITELIST = {"FlapTaxTokenV3", "DopplerERC20V1"}   # Flap launchpad clone; Bankr/Doppler clone
HC_REQUIRE_LP_KNOWN = True         # an unknown LP status can be B, never A
HC_MAX_DEV_PCT = 2.0
HC_GMGN_BUNDLER_RATIO_MAX = 0.10   # band_gmgn_clean: GMGN bundler wallets / holders — organic 0.00-0.01 (WIF 0.002,
                                   # trumplet 0.01), the $Cubrate wallet farm 1.42 (solana calibration 2026-07-05)
DEFAULT_ENTRY_BAND = "band_a_strict"     # the fallback / demotion target; the LIVE champion is selfimprove/champion.json
# band_volume_early — the operator's thesis (2026-09-12): the money is in early entries on coins that
# already trade heavily with buyers dominating; measured at sighting on the day's survivors,
# FRONTIER (23x), ROBOJENSEN (3.7x), DEGENFLY (3.3x) all had buys >= 2x sells in hour one while
# every balanced-flow sighting went flat or to zero (n=9, in-sample — a hypothesis, not evidence)
BAND_VE_MAX_AGE_MIN = 30.0
BAND_VE_MIN_VOL_H1_USD = 50_000.0
BAND_VE_MIN_LIQ_USD = 10_000.0
BAND_VE_BUY_SELL_RATIO = 2.0
BAND_VE_MAX_MCAP_USD = 2_000_000.0
# band_new_creation / band_almost_bonded — GMGN Trenches' New and Almost-bonded columns as pre-declared
# bands (2026-09-13): silent B arms judged by the entry gate, never a tier. New = pair age <= 15 min with
# the liquidity floor and a known sell round trip; Almost bonded = GMGN curve progress >= 0.5, not completed.
BAND_NC_MAX_AGE_MIN = 15.0
BAND_AB_MIN_PROGRESS = 0.5

# ── entry lab (selfimprove/entry_lab) ─────────────────────────────────────────────
# The ONE flat dict every band reads. build_feat() produces EXACTLY these keys (None when
# unknown); verify asserts set equality. Order: Dexscreener market, safety.py, runtime.
FEATURE_FIELDS = (
    # market (sources/dexscreener.py)
    "price_usd", "liq_usd", "mcap", "fdv", "vol_h1", "vol_h6", "vol_h24",
    "buys_h1", "sells_h1", "buys_h24", "sells_h24", "price_chg_h1", "pair_age_min", "dex",
    # safety (sources/safety.py)
    "owner_state", "owner_renounced", "lp_locked_pct", "lp_check_source", "honeypot",
    "roundtrip_loss_pct", "is_scam", "template_name", "is_proxy", "verified_source",
    "total_holders", "holders_source", "top10_pct", "top10_pct_gt", "lp_share_pct", "dev_pct",
    "deployer", "creator_prior_tokens", "creator_dead_frac", "creator_score", "dev_sniped",
    "sniper_swaps_first_blocks", "buys_per_buyer_m5", "tx_per_holder_total", "gt_score",
    "gt_verified", "launchpad_graduation_pct", "launchpad_completed",
    "launchpad_completed_age_s", "holders_updated_age_s", "scanhood_verdict",
    "scanhood_sellable", "sources_dark",
    # safety (sources/gmgn.py via safety._apply_gmgn): features only, never a hard gate
    "gmgn_launchpad_platform", "gmgn_progress", "gmgn_bundler_ratio", "gmgn_sniper_hold_pct",
    "gmgn_insider_hold_pct", "gmgn_fresh_wallet_pct", "gmgn_rat_vol_pct", "gmgn_smart_degen_count",
    "gmgn_is_wash_trading", "gmgn_holders",
    # runtime
    "token", "score", "first_sighting", "sighting_age_s",
)
BAND_MIN_COVERAGE = 0.80           # a band NA on >20% of rows is ineligible
BAND_OUTCOME_METRIC = "ret_6h"     # solana: winners peak ~4h after entry and give back by 24h;
                                   # 24h/7d are reported, never decided on
BAND_AGE_BUCKETS_S = (0, 1800, 5400, 21600, 86400)  # same-day, same-age-bucket comparison pool
BAND_MIN_UNSELECTED_PER_DAY_ABS = 2  # a day is dropped from the lift only if n_unsel < max(2, n_sel)
BAND_SHUFFLE_REPS = 1000           # within-day permutation calibration; at 200 the estimate
                                   # exceeded 0.05 in ~2% of null runs (a false VOID a year)
BAND_SHUFFLE_MAX_FP = 0.05
BAND_DSR_GATE = 0.95
BAND_PROMOTE_MIN_CLUSTERS = 40     # solana measured: 12 clusters = 6.3% actual coverage vs 2.5%
BAND_MIN_SELECTED = 150            # inherited from solana; re-derive once 300 events exist
BAND_FWD_MIN_SELECTED = 100
BAND_FWD_MIN_DAYS = 40
BAND_CTL_RANDOM_RATE = 0.10        # ≈ the champion's expected share of survivors
BAND_RENOMINATE_COOLDOWN_DAYS = 90
BAND_KILL_AFTER_DAYS = 180         # K5: no band (net of cost) above zero → "no entry signal"
BAND_SCORE60_MIN = 60.0
BAND_HOLDERS500_MIN = 500
BAND_DEV_SCORE_MIN = 70            # RobinX's own webhook threshold
BAND_GRADUATED_MAX_AGE_S = 3600
BAND_TOP10_LE15_MAX = 15.0
BAND_HOLDERS_MAX_STALE_S = 3600    # GT holder snapshots are 8 min..23 h stale
BAND_LP_BURNED_MIN_PCT = 95.0
BAND_ROUND_TRIP_LOSS_MAX_PCT = 6.0
BAND_MAX_GAPPED_SHARE = 0.25       # the entry gate VOIDs when more than this share of the rows that
                                   # CARRY a lag cell were sampled later than LEDGER_MAX_CELL_LAG_S:
                                   # a scan grid that collapsed is not a forward-return sample
BAND_CTL_RANDOM_CHANGED_ON = "2026-09-17"   # the day _ctl_random_band dropped its unrealisable delay;
                                   # before it the control returned 0 on 672/672 committed verdict rows,
                                   # so its earlier verdicts carry no evidence
RESEARCH_MAX_NEW_CANDIDATES_PER_WEEK = 2
RESEARCH_MAX_REGISTERED = 40       # every registration deflates every later DSR
RESEARCH_MAX_TURNS = 40

# ── alerting ──────────────────────────────────────────────────────────────────────
ALERT_TOP_N = 5
ALERT_COOLDOWN_HOURS = 24
DEGRADED_ALERT_COOLDOWN_HOURS = 6
FOOTER = ("Probabilistic screen, NOT a guarantee. A-tier = best SURVIVAL odds under every gate, "
          "not predicted ROI. On this chain the raw control went to zero 96% of the time at 7d. "
          "Alerts + paper only; nothing here touches keys or funds. Not financial advice.")

# ── ledger forward horizons (seconds) ─────────────────────────────────────────────
LEDGER_HORIZONS = {"1h": 3600, "6h": 21600, "24h": 86400, "7d": 604800}
SUPPLY_DRIFT_MAX = 1.5             # quote-integrity: implied supply must stay within this factor
SUSPECT_TICKS_MAX = 5              # ...else the row goes terminally 'suspect'
DEAD_CONFIRM_TICKS = 2             # an EVM pair can drop from the index for one poll; -100% only on
                                   # the 2nd consecutive absence
RUG_LIQ_USD = 500.0                # rugged_after = liquidity collapsed below this while priced
LEDGER_MAX_POLL_ROWS = 3000        # 100 batched Dexscreener calls ≈ 25 s
LEDGER_MIN_ENTRY_PRICE = 1e-30     # Racoon's 5.6e-36 entry poisoned every mean; excluded
LEDGER_ROTATE_AFTER_DAYS = 90      # resolved rows older than this move to data/resolved_YYYY.csv
LEDGER_MAX_CELL_LAG_S = 900        # a forward cell is a SPOT sample on the scan grid; this is 3-4x the
                                   # 240 s cadence. Measured on the committed grid: healthy days fill
                                   # 0-3% of 6h cells later than this, the 2026-09-14..17 outage days
                                   # 38-100%. Cells later than this are excluded from every scorecard.
LEDGER_MAX_PLAUSIBLE_MULT = 1000.0  # = LIVEBOOK_MAX_PLAUSIBLE_MULT; the EDDICE 11e6x row (event_seq 498,
                                   # mcap_6h 4.3e11 at a CONSTANT implied supply, so SUPPLY_DRIFT_MAX
                                   # could not see it — the quote leg itself was mispriced)

# ── paper execution + quotes (NO keys, NO funds, quotes only) ─────────────────────
PAPER_EXEC = True
PAPER_SLIPPAGE_BPS = 100           # bar-simulator cost per side; live fills use the quote as-is
PAPER_GAS_USD_PER_SWAP = 0.07      # KyberSwap gasUsd 0.067-0.075 at 289k gas (2026-09-12); a bare
                                   # V2 swap is ~$0.035 — the aggregator figure is the upper bound
PAPER_PENDING_MAX_RUNS = 6         # a deferred buy/exit quote is retried this many runs
USE_MULTICALL3 = True              # one eth_call for every open position; per-token fallback
WETH_PX_CACHE_S = 120
KYBER_ROUTES_URL = "https://aggregator-api.kyberswap.com/robinhood/api/v1/routes"
SCANHOOD_QUOTE_URL = "https://scanhood.xyz/api/quote"

# ── live multi-policy paper book (selfimprove/livebook.py; Mac, gitignored state) ──
LIVEBOOK_FEED_TIERS = ("A", "B")   # B = passed hard gates, failed the champion band: the control
LIVEBOOK_MAX_OPEN = 40             # A/promotion rows always admitted; B refused when full
MAX_ENTRY_LAG_S = 15 * 60          # keeper cadence 240 s + run ≤3 min + tick ≤1 min ⇒ 3-8 min expected
LIVEBOOK_TICK_INTERVAL_S = 60.0
LIVEBOOK_TICK_INTERVAL_LATE_S = 900.0
LIVEBOOK_DECISION_HORIZON_S = 6 * 3600
LIVEBOOK_MAX_TRACK_S = 12 * 3600
LIVEBOOK_MAX_SCORABLE_GAP_S = 180.0  # a fill after a longer tick gap is not what the policy would
                                     # have done (Mac sleep); scored NaN, counted as gapped
LIVEBOOK_TICK_JUMP_MAX = 10.0
LIVEBOOK_XSOURCE_TOL = 3.0
LIVEBOOK_MAX_PLAUSIBLE_MULT = 1000.0
LIVEBOOK_SUSPECT_TICKS_MAX = 5
LIVEBOOK_DEFERRED_TICKS_MAX = 30
LEDGER_POLL_S = 300.0

# ── the promotion gate (selfimprove/improve.py) ───────────────────────────────────
IMPROVE_DEFAULT_EXIT_CHAMPION = "cfg_ladder_stop"  # the plan alerts print and paper_exec executes
IMPROVE_SCORE_STRATUM = "pooled"   # A-stratum printed with its own n/days in every proposal
IMPROVE_MIN_POSITIONS = 300
IMPROVE_PROMOTE_MIN_CLUSTERS = 40  # a family max over 16 policies crosses zero 33% at 12 clusters
IMPROVE_DSR_GATE = 0.95
IMPROVE_FWD_MIN_POSITIONS = 30
IMPROVE_FWD_MIN_DAYS = 40
IMPROVE_RENOMINATE_COOLDOWN_DAYS = 90
IMPROVE_PUBLISH_RETRIES = 5

# ── price paths (selfimprove/paths.py) ────────────────────────────────────────────
PATHS_CACHE_MAX_AGE_S = 7 * 86400
PATHS_LIMIT = 1000
PATHS_MISPRICED_TOL = 3.0
# One OHLCV page is PATHS_LIMIT bars, and a busy pool emits a bar a minute: the newest minute/1
# page starts only ~16 h back, so an alert older than that read as `no_cover` however alive the
# token was (FOMOPAD's unpaged page began three hours AFTER its alert). `before_timestamp` walks
# backward; 4 pages reach ~2.8 days at 1m, ~42 days at 15m. The cap is a call-budget bound, not a
# depth claim — GeckoTerminal is 30/min per IP shared with every other job on this machine.
PATHS_MAX_PAGES = 4


def _check_perms(path):
    """Warn when a credential file is readable by anyone but its owner.

    Secrets sit in plaintext behind FileVault; the file mode is the only thing between
    them and every other process on this machine. A file created by a shell redirect lands
    at the umask default (0644) — this warns rather than assumes. Never fails.
    """
    try:
        mode = os.stat(path).st_mode & 0o777
    except OSError:
        return
    if mode & 0o077:
        print("  [warn] %s is mode %o (world-readable) - run: chmod 600 %s"
              % (path, mode, path))


def load_credentials() -> dict:
    """Load push secrets without hardcoding them. Priority, so the SAME code runs both on a
    GitHub Actions runner and on the Mac:
      1. env vars (GitHub Secrets): NTFY_TOPIC, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, GMGN_API_KEY.
      2. robinhood_screener/config.local.json (gitignored local override).
      3. the shared Mac secrets file (telegram + ntfy only), and the sibling solana_screener's
         config.local.json for the GMGN key only (the same key serves both chains).
    Returns {ntfy_topic, telegram:{bot_token,chat_id}, gmgn_api_key}. Values are never printed."""
    creds = {"ntfy_topic": os.environ.get("NTFY_TOPIC"), "telegram": {},
             "gmgn_api_key": os.environ.get("GMGN_API_KEY") or None}
    bt, cid = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if bt and cid:
        creds["telegram"] = {"bot_token": bt, "chat_id": cid}

    local = os.path.join(ROOT, "config.local.json")
    if os.path.exists(local):
        _check_perms(local)
        try:
            loc = json.load(open(local))
            for k, v in loc.items():
                if v and not creds.get(k):
                    creds[k] = v
        except Exception:
            pass

    if not creds["telegram"]:
        mon = os.path.join(VRP_BACKTEST, "monitor_config.json")
        if os.path.exists(mon):
            _check_perms(mon)
            try:
                m = json.load(open(mon))
                creds["telegram"] = m.get("telegram", {})
                creds["ntfy_topic"] = creds["ntfy_topic"] or m.get("ntfy_topic")
            except Exception:
                pass
    if not creds.get("gmgn_api_key"):
        sib = os.path.join(SOLANA_SCREENER, "config.local.json")
        if os.path.exists(sib):
            _check_perms(sib)
            try:
                creds["gmgn_api_key"] = json.load(open(sib)).get("gmgn_api_key") or None
            except Exception:
                pass
    return creds


if __name__ == "__main__":
    print("robinhood_screener config")
    print(f"  ROOT      {ROOT}")
    print(f"  chain     id={CHAIN_ID}  rpc={RPC_URL}  IS_CI={IS_CI}")
    print(f"  gates: liq>=${LIQ_FLOOR_USD:,.0f}  vol24>=${MIN_VOL_H24_USD:,.0f}  top10<={TOP10_MAX_PCT:.0f}%  "
          f"dev<={DEV_MAX_PCT:.0f}%  LP>={LP_LOCKED_MIN_PCT:.0f}%  roundtrip<={SELL_TAX_MAX_ROUNDTRIP_PCT:.0f}%  "
          f"owner renounced={REQUIRE_OWNER_RENOUNCED}")
    print(f"  A band: {DEFAULT_ENTRY_BAND}  score>={HC_MIN_SCORE:.0f} age>={HC_MIN_AGE_MINUTES}m "
          f"holders>={HC_MIN_HOLDERS} liq>=${HC_MIN_LIQ_USD:,.0f}")
    print(f"  rates: {HOST_RATE_HZ}")
    print(f"  feature fields: {len(FEATURE_FIELDS)}")
    # presence only — the values themselves are never printed (this goes to a log)
    c = load_credentials()
    print("  creds present:", {"ntfy_topic": bool(c.get("ntfy_topic")),
                               "telegram": bool(c.get("telegram")),
                               "gmgn_api_key": bool(c.get("gmgn_api_key"))})
