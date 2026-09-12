"""
Data sources for Robinhood Chain (chainId 4663). Each module wraps ONE vendor behind
http_client's three-way contract (ok / NOT_FOUND = absent / None = deferred) and returns
normalized flat dicts. What only each source can do:

  rpc.py           chain truth via eth_call / eth_getLogs: owner(), LP burn %, the honeypot
                   round trip (getAmountsOut both legs), Multicall3 batching, first-block
                   discovery on factory/launchpad logs, creation-tx attribution fallback.
  blockscout.py    holders (paged, contracts excluded), transfer/holder counters, is_scam,
                   proxy implementation name (contract template), creation-tx sender.
  geckoterminal.py new_pools feed with buyer/seller counts, /tokens/{a}/info (holder count,
                   top-10 %, honeypot flag, dev holding, launchpad graduation), OHLCV paths.
  dexscreener.py   market snapshot (liq / mcap / volumes / txns / age / socials), batched
                   30 addresses per call, the ONLY source of forward-return fills; WETH price.
  scanhood.py      chain-specific verdict + sell simulation, read-only swap quote, launch feed.
  robinx.py        FREE tier: deployer track record (launched / real / dead / score), insider
                   flags, a 10-min-delayed launch feed.
  kyber.py         keyless aggregator route quote — paper fills for tokens with no V2 pair.
  safety.py        THE ADAPTER BOUNDARY: fuses the above into the one flat dict that
                   screen.hard_gates / screen.hc_checks consume, with the pass-through rule.
"""
