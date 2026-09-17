"""
Alert delivery for robinhood_screener — the notify primitives (macOS Notification Center,
ntfy.sh, Telegram) ported from solana_screener/alerts.py, which took them from
vrp_backtest/monitor.py; same certifi SSL idiom, same 3x retry with 2s*i backoff (a DNS drop
on wake-from-sleep once lost an alert forever — scripts are one-shot, so a send that fails
once has no second chance without the retry). Credentials via config.load_credentials()
(env → config.local.json → the shared Mac secrets file), never hardcoded, never printed.

What this module formats, and why it is shaped this way:
  • format_alert            — the A-tier card. Every EVM safety fact the gates keyed on is
                              printed (owner, LP burned % + which source said so, sell
                              round-trip %, template, deployer history, sniped-at-create) so
                              the reader can see WHY the token passed, plus the PLAN line
                              from the exit champion (champion.describe_plan) so discipline
                              is decided at alert time, not in the moment.
  • format_exit_alert       — tp / stop / trail / time_exit signals from ledger.update_forward.
                              Entries are probabilistic; exits are mechanical.
  • format_degraded_notice  — when a source is dark the gates keyed on it PASS THROUGH
                              (screen.py's pass-through rule), so "no A-tier this run" is not
                              evidence of a quiet chain. Measured on solana: 39 of 42 real
                              alerts sat in the control arm after an outage-shaped run.
  • format_event / format_weekly — the self-improvement loop's event and summary messages.

Rendering never raises: a None or a malformed value prints as '?' rather than killing the
run that produced the alert. Every threshold comes from config. No wall-clock here — the
only timestamp printed (promotion date) is formatted from champion.json by describe_plan.
ALERT-ONLY: nothing here places an order or touches keys. Not financial advice.
"""
from __future__ import annotations

import ssl
import subprocess
import sys
import time
import urllib.parse
import urllib.request

import certifi

import config
from selfimprove import champion

_SSL_CTX = ssl.create_default_context(cafile=certifi.where())
TELEGRAM_MAX_BODY = 4000          # Telegram's hard message cap is 4096 chars incl. markup
_TRUNC = "…(truncated)"
EVENT_KINDS = ("PROMOTED", "DEMOTED", "NOMINATED", "APPARATUS FAULT", "PUBLISH FAILED", "PAUSED",
               "SCAN STALE")           # SCAN STALE: the keeper watchdog (watchdog.py) past KEEPER_STALE_S


# ── delivery primitives ───────────────────────────────────────────────────────────
def _send_with_retries(name: str, req: urllib.request.Request, attempts: int = 3) -> None:
    """Alerts are rare and the scripts one-shot, so a transient network blip
    (e.g. a DNS drop on wake-from-sleep) would otherwise lose the alert forever."""
    for i in range(1, attempts + 1):
        try:
            urllib.request.urlopen(req, timeout=10, context=_SSL_CTX)
            return
        except Exception as e:
            print(f"[{name} error] attempt {i}/{attempts}: {e}", file=sys.stderr)
            if i < attempts:
                time.sleep(2 * i)


def macos_notify(title: str, body: str) -> None:
    title = title.replace('"', '\\"')
    body = body.replace('"', '\\"').replace("\n", " — ")
    script = f'display notification "{body}" with title "{title}" sound name "Glass"'
    try:
        subprocess.run(["osascript", "-e", script], check=False)
    except Exception:
        pass  # not on macOS (the Linux Actions runner) — osascript absent; skip silently


def ntfy_notify(topic: str, title: str, body: str) -> None:
    if not topic:
        return
    req = urllib.request.Request(f"https://ntfy.sh/{topic}", data=body.encode("utf-8"),
                                 method="POST")
    # HTTP headers are latin-1 only — degrade fancy punctuation instead of crashing
    req.add_header("Title", title.encode("latin-1", "replace").decode("latin-1"))
    req.add_header("Priority", "high")
    req.add_header("Tags", "chart_with_upwards_trend,rotating_light")
    _send_with_retries("ntfy", req)


def _esc(s: str) -> str:
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def telegram_text(title: str, body: str) -> str:
    """The HTML payload Telegram receives: bold title, <pre> body, &<> escaped, body
    truncated above TELEGRAM_MAX_BODY (the API rejects >4096 chars outright — a long
    A-tier card with five tokens would otherwise be dropped, not shortened)."""
    if len(body) > TELEGRAM_MAX_BODY:
        body = body[:TELEGRAM_MAX_BODY - len(_TRUNC)] + _TRUNC
    return f"<b>{_esc(title)}</b>\n<pre>{_esc(body)}</pre>"


def telegram_notify(bot_token: str, chat_id: str, title: str, body: str) -> None:
    if not bot_token or not chat_id:
        return
    data = urllib.parse.urlencode({"chat_id": str(chat_id), "text": telegram_text(title, body),
                                   "parse_mode": "HTML"}).encode("utf-8")
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    _send_with_retries("telegram", urllib.request.Request(url, data=data))


def send_all(title: str, body: str, dry_run: bool = True) -> None:
    """Send to every configured channel. dry_run just prints to the console. Never raises:
    a channel with missing credentials is a no-op, a failing channel is logged to stderr."""
    print(f"\n=== ALERT {'(DRY RUN — not sent)' if dry_run else '(SENDING)'} ===")
    print(title)
    print(body)
    if dry_run:
        return
    try:
        creds = config.load_credentials()
    except Exception as e:
        print(f"[alerts] credentials unavailable: {e}", file=sys.stderr)
        creds = {}
    try:
        macos_notify(title, body)
    except Exception:
        pass
    try:
        ntfy_notify(creds.get("ntfy_topic"), title, body)
    except Exception as e:
        print(f"[ntfy error] {e}", file=sys.stderr)
    try:
        tg = creds.get("telegram") or {}
        telegram_notify(tg.get("bot_token"), tg.get("chat_id"), title, body)
    except Exception as e:
        print(f"[telegram error] {e}", file=sys.stderr)


# ── safe formatting helpers (a None or a bad value prints as '?', never raises) ───
def _f(v, spec: str = "", fallback: str = "?") -> str:
    """format(v, spec) with '?' for None/NaN/unformattable — an alert must render even when
    a source answered with garbage for one field."""
    if v is None:
        return fallback
    try:
        if isinstance(v, float) and v != v:
            return fallback
        return format(v, spec) if spec else str(v)
    except (TypeError, ValueError):
        return fallback


def _pct(v, spec: str = ".0f") -> str:
    s = _f(v, spec)
    return s if s == "?" else s + "%"


def _frac_pct(v) -> str:
    """0.35 → '35%' (a fraction, not a percent)."""
    if v is None:
        return "?"
    try:
        return f"{float(v):.0%}"
    except (TypeError, ValueError):
        return "?"


def _usd(v, spec: str = ",.0f") -> str:
    s = _f(v, spec)
    return s if s == "?" else "$" + s


def _plan(entry_price) -> str:
    """The PLAN line for one token, from the exit champion in selfimprove/champion.json. A
    corrupt or missing state file yields the default plan (champion.state prints, never
    raises); a bad entry price yields a plan around $0 rather than no alert."""
    try:
        px = float(entry_price) if entry_price is not None else 0.0
        if px != px:
            px = 0.0
    except (TypeError, ValueError):
        px = 0.0
    try:
        st = champion.state()
        return champion.describe_plan(champion.exit_plan(), px, promoted=st["exit"])
    except Exception as e:
        return f"PLAN [unavailable: {e}]"


def _gmgn_line(s: dict) -> str:
    """GMGN's wallet-tag second opinion, or its absence stated: a dark or unconsulted GMGN passes
    through (no gate reads it) and the card says so, exactly like solana's alert did."""
    if all(s.get(k) is None for k in ("gmgn_bundler_ratio", "gmgn_smart_degen_count", "gmgn_progress",
                                      "gmgn_sniper_hold_pct")):
        return "   GMGN: unavailable (passed through)"
    prog = s.get("gmgn_progress")
    try:
        prog_txt = "?" if prog is None else f"{100 * float(prog):.0f}%"
    except (TypeError, ValueError):
        prog_txt = "?"
    wash = s.get("gmgn_is_wash_trading")
    return (f"   GMGN: bundler ratio {_f(s.get('gmgn_bundler_ratio'), '.2f')} · snipers hold "
            f"{_pct(s.get('gmgn_sniper_hold_pct'), '.1f')} · insiders {_pct(s.get('gmgn_insider_hold_pct'), '.1f')} "
            f"· smart-money {_f(s.get('gmgn_smart_degen_count'))} · wash {'?' if wash is None else wash} "
            f"· launchpad {s.get('gmgn_launchpad_platform') or '?'} · curve {prog_txt}")


# ── the A-tier card ───────────────────────────────────────────────────────────────
def _token_block(s: dict) -> str:
    dark = s.get("sources_dark") or []
    if isinstance(dark, str):
        dark = [d for d in dark.split(",") if d]
    misses = s.get("hc_misses") or []
    lines = [
        f"⚡ {s.get('symbol') or '?'}  score {_f(s.get('score'), '.0f')}/100  "
        f"[{s.get('event_kind') or '?'}]",
        f"   price {_usd(s.get('price_usd'), '.6g')}  mcap {_usd(s.get('mcap'))}  "
        f"liq {_usd(s.get('liq_usd'))}  age {_f(s.get('pair_age_min'), '.0f')}m",
        f"   holders {_f(s.get('total_holders'))} ({s.get('holders_source') or '?'})  "
        f"top10 {_pct(s.get('top10_pct'))}  vol24 {_usd(s.get('vol_h24'))}",
        f"   owner {s.get('owner_state') or '?'} · LP burned {_pct(s.get('lp_locked_pct'))} "
        f"({s.get('lp_check_source') or '?'}) · sell round-trip {_pct(s.get('roundtrip_loss_pct'), '.1f')} "
        f"· template {s.get('template_name') or '?'}",
        f"   deployer prior launches {_f(s.get('creator_prior_tokens'))} "
        f"(dead {_frac_pct(s.get('creator_dead_frac'))}) · dev score {_f(s.get('creator_score'))} "
        f"· sniped-at-create {_f(s.get('dev_sniped'))}",
        _gmgn_line(s),
    ]
    if dark:
        lines.append(f"   ⚠ passed through (dark): {', '.join(str(d) for d in dark)}")
    if misses:                                   # a B card: say exactly what it fell short of
        lines.append(f"   short of {s.get('band') or config.DEFAULT_ENTRY_BAND}: "
                     + "; ".join(str(m) for m in misses))
    lines.append(f"   {_plan(s.get('price_usd'))}")
    lines.append(f"   {s.get('url') or ''}")
    tok = str(s.get("token") or "").lower()
    if tok:                                      # the terminal + explorer deep links (the human acts there)
        lines.append(f"   gmgn {config.GMGN_TOKEN_URL.format(chain=config.GMGN_CHAIN, token=tok)}"
                     f" · explorer {config.BLOCKSCOUT_TOKEN_URL.format(token=tok)}")
    return "\n".join(lines)


def format_alert(survivors: list, degraded=(), band: str = config.DEFAULT_ENTRY_BAND,
                 champion_reason: str | None = None) -> tuple:
    """survivors: ranked A-tier rows — each the flat feat dict (config.FEATURE_FIELDS) plus
    symbol, url, event_kind, hc_misses (list[str]), sources_dark (list). Returns (title, body).
    A-tier = every hard gate passed AND the champion band's verdict True — best SURVIVAL
    odds, deliberately NOT phrased as predicted ROI. `degraded` = host names dark this run;
    `champion_reason` (optional) = one line from runtime.champion_reason, printed under the
    header when the champion is not the default."""
    n = len(survivors)
    title = f"robinhood_screener A-TIER [{band}]: {n} passed every safety gate"
    head = []
    if degraded:
        head.append(f"⚠ DEGRADED: {', '.join(str(h) for h in degraded)} dark this run — "
                    f"gates keyed on them passed through")
    if champion_reason:
        head.append(str(champion_reason))
    blocks = []
    for s in survivors:
        try:
            blocks.append(_token_block(s or {}))
        except Exception as e:                   # never lose the whole alert to one row
            blocks.append(f"⚡ {(s or {}).get('symbol', '?')}  (render error: {e})")
    parts = head + blocks
    body = "\n\n".join(parts) + ("\n\n" if parts else "") + config.FOOTER
    return title, body


# ── exit signals ──────────────────────────────────────────────────────────────────
def _exit_line(e: dict) -> str:
    kind = e.get("kind")
    sym = e.get("symbol") or "?"
    ret = e.get("ret")
    price = e.get("price")
    try:
        dead = price is None or float(price) <= 0
    except (TypeError, ValueError):
        dead = True
    ret_s = _frac_pct(ret) if ret is None else (f"{float(ret):+.0%}" if ret == ret else "?")
    px_s = "(no price — token looks dead)" if dead else f"price {_usd(price, '.6g')}"
    if kind == "stop":
        return (f"🛑 {sym}  {ret_s}  {px_s}\n"
                f"   Hard stop hit. Plan: exit the remainder. No averaging down.")
    if kind == "trail":
        return (f"📉 {sym} trail hit at {ret_s} off the high-water mark — exit the remainder"
                + ("" if dead else f"  ({px_s})"))
    if kind == "time_exit":
        return f"⏱ {sym}  time exit due: exit ALL at {ret_s}" + ("" if dead else f"  ({px_s})")
    if kind == "tp":
        levels = e.get("levels") or []
        try:
            rungs = ", ".join(f"at {float(m):g}x sell {float(f) * 100:.0f}%" for m, f in levels)
        except Exception:
            rungs = "?"
        return (f"🎯 {sym}  {_f(e.get('mult'), '.1f')}x  {px_s} — sell the rung fractions per "
                f"the ladder ({rungs or '?'}). Winners round-trip to zero here — take it.")
    return f"• {sym}  {kind or '?'}  {ret_s}  {px_s}"


def format_exit_alert(events: list) -> tuple:
    """events from ledger.update_forward: {kind: tp|stop|trail|time_exit, token, symbol,
    event_seq, price, ret, mult, levels?, due_ts?, gap_s}. Returns (title, body). This is the
    discipline half of the system: entries are probabilistic, exits are mechanical."""
    title = f"robinhood_screener EXITS: {len(events)} signal(s) on open alerts"
    lines = []
    for e in events:
        try:
            lines.append(_exit_line(e or {}))
        except Exception as ex:
            lines.append(f"• {(e or {}).get('symbol', '?')}  (render error: {ex})")
    body = "\n\n".join(lines) + ("\n\n" if lines else "") + config.FOOTER
    return title, body


# ── apparatus / loop messages ─────────────────────────────────────────────────────
def format_degraded_notice(health_text: str, hosts, fields) -> tuple:
    """A source went dark this run. The gates keyed on it PASSED THROUGH (they fail closed
    only on a positive finding), so the run's silence is not the chain's silence."""
    hosts = [str(h) for h in (hosts or [])]
    fields = [str(f) for f in (fields or [])]
    title = f"robinhood_screener DEGRADED: {len(hosts)} source(s) dark"
    lines = [
        f"⚠ dark this run: {', '.join(hosts) or '?'}",
        f"gates passed through (unknown, not checked): {', '.join(fields) or 'none named'}",
        "The A-tier band refuses unknown data (a None check is never A), so no false A was "
        "sent — but 'no A-tier this run' is NOT evidence of a quiet chain: tokens screened "
        "while these hosts were dark were tiered on partial facts.",
        "",
        "source health:",
        str(health_text or "(none)"),
    ]
    return title, "\n".join(lines) + "\n\n" + config.FOOTER


def format_event(kind: str, lines) -> tuple:
    """Self-improvement loop events: PROMOTED / DEMOTED / NOMINATED / APPARATUS FAULT /
    PUBLISH FAILED / PAUSED. An unknown kind still renders (prefixed) — the loop must be able
    to report whatever happened."""
    k = str(kind or "EVENT").upper()
    if k not in EVENT_KINDS:
        k = f"EVENT {k}"
    title = f"robinhood_screener {k}"
    if isinstance(lines, str):
        lines = [lines]
    body = "\n".join(str(x) for x in (lines or [])) + "\n\n" + config.FOOTER
    return title, body


def format_weekly(lines) -> tuple:
    """The ONE weekly summary (sent at the end of run_research.sh)."""
    title = "robinhood_screener WEEKLY summary"
    if isinstance(lines, str):
        lines = [lines]
    body = "\n".join(str(x) for x in (lines or [])) + "\n\n" + config.FOOTER
    return title, body


if __name__ == "__main__":
    # Offline smoke test: render every message kind from fixtures. NEVER sends.
    clean_m = {"price_usd": 0.001, "liq_usd": 45_000.0, "vol_h24": 120_000.0, "mcap": 250_000.0,
               "fdv": 250_000.0, "buys_h1": 320, "sells_h1": 180, "pair_age_min": 180.0,
               "vol_h1": 9000.0, "vol_h6": 40000.0, "buys_h24": 3000, "sells_h24": 2500,
               "price_chg_h1": 4.0, "dex": "uniswap"}
    clean_s = {"owner_state": "renounced", "owner_renounced": True, "lp_locked_pct": 100.0,
               "lp_check_source": "rpc_v2", "honeypot": False, "roundtrip_loss_pct": 4.8,
               "is_scam": False, "template_name": "FlapTaxTokenV3", "is_proxy": True,
               "verified_source": True, "total_holders": 1400, "holders_source": "blockscout",
               "top10_pct": 18.0, "top10_pct_gt": 20.0, "lp_share_pct": 30.0, "dev_pct": 1.2,
               "deployer": "0xdev", "creator_prior_tokens": 0, "creator_dead_frac": 0.0,
               "creator_score": 80.0, "dev_sniped": False, "sniper_swaps_first_blocks": 4,
               "buys_per_buyer_m5": 1.1, "tx_per_holder_total": 5.0, "gt_score": 70.0,
               "gt_verified": True, "launchpad_graduation_pct": 100.0, "launchpad_completed": True,
               "launchpad_completed_age_s": 9000.0, "holders_updated_age_s": 600.0,
               "scanhood_verdict": "PASS", "scanhood_sellable": True, "sources_dark": []}
    feat = {k: None for k in config.FEATURE_FIELDS}
    feat.update(clean_m); feat.update(clean_s)
    feat.update({"token": config.MIZUKARA.lower(), "score": 78.4, "first_sighting": True,
                 "sighting_age_s": 0.0})
    assert set(feat) == set(config.FEATURE_FIELDS)
    surv = dict(feat, symbol="MIZUKARA", event_kind="promotion", hc_misses=[],
                url=f"https://dexscreener.com/robinhood/{config.MIZUKARA_POOL.lower()}")

    t, b = format_alert([surv])
    assert config.FOOTER in b and config.DEFAULT_ENTRY_BAND in t and "PLAN [" in b, b
    assert "DEGRADED" not in b and "passed through (dark)" not in b
    assert "LP burned 100%" in b and "sell round-trip 4.8%" in b and "template FlapTaxTokenV3" in b
    send_all(t, b, dry_run=True)

    # degraded run + a token with a dark source + None fields (must print '?', never raise)
    dark = dict(surv, sources_dark=["blockscout"], total_holders=None, holders_source=None,
                template_name=None, creator_score=float("nan"))
    t2, b2 = format_alert([dark], degraded=["robinhoodchain.blockscout.com"], band="band_a_strict")
    assert "⚠ DEGRADED: robinhoodchain.blockscout.com dark this run" in b2
    assert "passed through (dark): blockscout" in b2 and "holders ? (?)" in b2
    assert "dev score ?" in b2 and "template ?" in b2
    # B card renders its misses
    tb, bb = format_alert([dict(surv, hc_misses=["holders below 1000 (or not exact)"])],
                          champion_reason="champion band_a_strict (default)")
    assert "short of band_a_strict: holders below 1000" in bb and "champion band_a_strict" in bb
    # a malformed row never loses the alert
    _, bx = format_alert([{"symbol": "BAD", "price_usd": "garbage"}, None])
    assert "BAD" in bx and config.FOOTER in bx

    # exits — all four kinds, incl. a dead stop
    base = {"token": "0xabc", "event_seq": 7, "gap_s": 300.0}
    events = [
        dict(base, kind="stop", symbol="DEADCOIN", price=0.0, ret=-1.0, mult=0.0),
        dict(base, kind="stop", symbol="HALF", price=0.0005, ret=-0.5, mult=0.5),
        dict(base, kind="trail", symbol="TRAILER", price=0.0014, ret=0.4, mult=1.4),
        dict(base, kind="time_exit", symbol="TIMER", price=0.0011, ret=0.1, mult=1.1,
             due_ts=1_000_900.0),
        dict(base, kind="tp", symbol="WINNER", price=0.0021, ret=1.1, mult=2.1,
             levels=[(2.0, 0.5)]),
    ]
    t3, b3 = format_exit_alert(events)
    print(f"\n{t3}\n{b3}")
    assert "no price — token looks dead" in b3 and "🛑 DEADCOIN  -100%" in b3
    assert "🛑 HALF  -50%  price $0.0005" in b3
    assert "📉 TRAILER trail hit at +40% off the high-water mark" in b3
    assert "⏱ TIMER  time exit due: exit ALL at +10%" in b3
    assert "🎯 WINNER  2.1x  price $0.0021" in b3 and "at 2x sell 50%" in b3
    assert "5 signal(s)" in t3 and config.FOOTER in b3

    # degraded notice / loop events / weekly
    t4, b4 = format_degraded_notice("robinhoodchain.blockscout.com: BOT-CHALLENGED (0 ok / 3 failed)",
                                    ["robinhoodchain.blockscout.com"], ["template_ok", "top10_ok"])
    print(f"\n{t4}\n{b4}")
    assert "NOT evidence of a quiet chain" in b4 and "template_ok, top10_ok" in b4
    t5, b5 = format_event("PROMOTED", ["exit: cfg_ladder_stop → sell_3h", "lb +0.12 (n=340, 44 days)"])
    print(f"\n{t5}\n{b5}")
    assert t5 == "robinhood_screener PROMOTED" and "sell_3h" in b5 and config.FOOTER in b5
    assert format_event("APPARATUS FAULT", "ctl_random_exit cleared the gate")[0].endswith("APPARATUS FAULT")
    assert format_event("bogus", [])[0] == "robinhood_screener EVENT BOGUS"
    t6, b6 = format_weekly(["runs/day (measured): 13.7", "champions: exit=cfg_ladder_stop"])
    assert t6 == "robinhood_screener WEEKLY summary" and "13.7" in b6 and config.FOOTER in b6

    # Telegram payload: escaping + truncation
    tg = telegram_text("A & B <c>", "x" * 5000)
    assert tg.startswith("<b>A &amp; B &lt;c&gt;</b>\n<pre>") and tg.endswith(_TRUNC + "</pre>")
    assert len(tg) < TELEGRAM_MAX_BODY + 100
    assert "&lt;script&gt;" in telegram_text("t", "<script>")
    print("\nalerts smoke test OK (nothing sent)")
