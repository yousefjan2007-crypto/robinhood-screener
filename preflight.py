"""
Pre-flight probe: what does each source look like FROM HERE (the GitHub runner's IP, or this
Mac)? Commits nothing, sends nothing. Exists because the Blockscout Cloudflare fix was verified
only from the Mac's residential IP — a runner in an Azure range may be challenged differently —
and because the plan's per-run budget assumes the public RPC and KyberSwap answer at all.

    python3 preflight.py          # prints one line per probe; exit 0 always
"""
from __future__ import annotations

import json
import os
import sys
import time

import config
import http_client
from sources import blockscout, dexscreener, geckoterminal, gmgn, kyber, rpc


def _line(name: str, ok, detail: str = "") -> None:
    tag = "ok" if ok is True else ("ABSENT" if ok == "absent" else "DEFERRED/DARK")
    print(f"  {name:32s} {tag:14s} {detail}")


def main() -> int:
    t0 = time.monotonic()
    now_s = time.time()
    print(f"robinhood_screener preflight  IS_CI={config.IS_CI}  runner={os.environ.get('RUNNER_NAME', '-')}")
    # Blockscout: each header set on its own, with the health registry reset between them
    for i, hdrs in enumerate(config.BLOCKSCOUT_HEADER_SETS):
        http_client.reset_health()
        http_client._host_header_idx["robinhoodchain.blockscout.com"] = i
        d = http_client.get_json(f"{config.BLOCKSCOUT_BASE}/api/v2/stats")
        blocked = http_client.is_blocked("robinhoodchain.blockscout.com")
        _line(f"blockscout header set {i}", (d is not None and not http_client.is_absent(d)) and not blocked,
              "BOT-CHALLENGED" if blocked else (f"avg block {d.get('average_block_time')} ms" if isinstance(d, dict) else ""))
    http_client.reset_health()
    head = rpc.block_number()
    _line("rpc eth_blockNumber", head is not None, str(head))
    o = rpc.owner(config.MIZUKARA)
    _line("rpc eth_call owner(MIZUKARA)", o is not None, str(o))
    hp = rpc.honeypot_roundtrip(config.MIZUKARA)
    _line("rpc router round trip", hp.get("status") == "ok", f"loss {hp.get('roundtrip_loss_pct')}% route {hp.get('route')}")
    mc = rpc.multicall3([(config.MIZUKARA, config.SEL_DECIMALS)])
    _line("rpc multicall3", mc is not None, str(mc)[:60])
    logs = rpc.discover_logs(max(0, (head or 0) - 1500), head or 0) if head else []
    _line("rpc discover_logs (last 1500 blocks)", bool(head), f"{len(logs)} launches")
    k = kyber.route(config.WETH, config.MIZUKARA, 10 ** 16)
    _line("kyber route", (k is not None and not http_client.is_absent(k)) or "absent" if http_client.is_absent(k) else k is not None,
          f"gas_usd {k.get('gas_usd') if isinstance(k, dict) and not http_client.is_absent(k) else '-'}")
    m = dexscreener.enrich_many([config.MIZUKARA], now_s, max_age_sec=0)
    _line("dexscreener enrich_many", config.MIZUKARA.lower() in m.get("ok", {}), f"deferred={len(m.get('deferred', []))}")
    w = dexscreener.weth_price_usd(now_s)
    _line("dexscreener WETH price", w is not None, str(w))
    g = geckoterminal.token_info(config.MIZUKARA)
    _line("geckoterminal token_info", g is not None and not http_client.is_absent(g),
          f"holders {g.get('holders_count') if isinstance(g, dict) else '-'}")
    np_ = geckoterminal.new_pools(1)
    _line("geckoterminal new_pools", bool(np_), f"{len(np_)} rows")
    gm = gmgn.trenches(cache_s=0) if gmgn._api_key() else None
    _line("gmgn trenches (robinhood)", gm is not None,
          "no key configured" if not gmgn._api_key() else
          (" ".join(f"{c}={len(gm.get(c) or [])}" for c in config.GMGN_TRENCHES_COLUMNS) if gm else
           "banned/dark" if http_client.is_blocked("openapi.gmgn.ai") else ""))
    bi = blockscout.address_info(config.MIZUKARA)
    _line("blockscout address_info", bi is not None and not http_client.is_absent(bi),
          f"impl {bi.get('impl_name') if isinstance(bi, dict) and not http_client.is_absent(bi) else '-'}")
    print("\n--- source health ---")
    print(http_client.health_report())
    print(f"\npreflight wall time {time.monotonic() - t0:.1f}s")
    print(json.dumps({"blockscout_blocked": http_client.is_blocked("robinhoodchain.blockscout.com"),
                      "head": head, "is_ci": config.IS_CI}))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # a preflight must never fail the workflow
        print(f"preflight error: {exc}")
        sys.exit(0)
