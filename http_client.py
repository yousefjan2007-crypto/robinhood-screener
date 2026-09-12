"""
Shared HTTP client for robinhood_screener — pure stdlib urllib + certifi SSL, per-host
throttle (sleep held INSIDE the lock), retry with backoff, transparent gzip, optional JSON
disk cache, a per-host HEALTH registry, and per-host browser header sets with
rotate-once-on-challenge.

Lineage: solana_screener (throttle/cache/retry) → robinhood_screener (post_json, 429 backoff,
400/404 terminal, SOURCE_HEALTH + bot-challenge detection) → ai_visibility (readable error
bodies). This is the union.

THE RETURN CONTRACT IS THREE-WAY, and every caller must honour it:

    ok        — the parsed JSON object / list
    absent    — the NOT_FOUND sentinel: the server ANSWERED 400/404. "No such thing."
    deferred  — None: network failure, timeout, 429, 5xx, or a bot challenge. "Unknown —
                retry later, NEVER score." Conflating deferred with absent fabricated 472 of
                1,400 rows as dead tokens in a sibling project. Do not.

Never raises: one bad token never kills a run.
"""
from __future__ import annotations

import gzip
import json
import os
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib

import certifi

import config

_SSL = ssl.create_default_context(cafile=certifi.where())
_last_call: dict = {}
_throttle_lock = threading.Lock()
_HOST_HZ = dict(config.HOST_RATE_HZ)      # every value is a named config constant
_urlopen = urllib.request.urlopen         # module-level so verify can inject a fake server


class _NotFound(dict):
    """Sentinel for 'the server answered: no such resource'. Falsy on purpose (like None) so
    a lazy `if not x` still treats it as 'nothing usable' — but distinguishable by identity."""
    __slots__ = ()

    def __repr__(self):
        return "NOT_FOUND"


NOT_FOUND = _NotFound({"__absent__": True})


def is_absent(x) -> bool:
    return x is NOT_FOUND or (isinstance(x, dict) and x.get("__absent__") is True)


def is_deferred(x) -> bool:
    return x is None


# ── source health ────────────────────────────────────────────────────────────────
# Per-host counters for THIS process (scripts are one-shot, so "this process" == "this run").
# A screener whose source is dark must say so, not print "nothing found" and look quiet.
SOURCE_HEALTH: dict = {}
_health_lock = threading.Lock()
_host_header_idx: dict = {}
_CHALLENGE_HEADERS = ("cf-mitigated", "cf-chl-bypass", "x-datadome")


def _is_bot_challenge(code, exc) -> bool:
    """A Cloudflare/WAF challenge is NOT a rate limit and NOT a missing resource: it is the host
    declining to serve automated clients. Recorded distinctly; never defeated."""
    if code not in (401, 403, 503):
        return False
    hdrs = getattr(exc, "headers", None)
    if hdrs is None:
        return False
    try:
        if any(hdrs.get(h) for h in _CHALLENGE_HEADERS):
            return True
        server = str(hdrs.get("server") or "").lower()
        ctype = str(hdrs.get("content-type") or "").lower()
        return server in ("cloudflare", "akamaighost") and "text/html" in ctype
    except Exception:
        return False


def _record(host: str, ok: bool, code=None, exc=None, challenge: bool = False) -> None:
    with _health_lock:
        h = SOURCE_HEALTH.setdefault(
            host, {"ok": 0, "fail": 0, "absent": 0, "last_status": None, "last_error": None,
                   "bot_challenge": False, "blocked_reason": None})
        if ok:
            h["ok"] += 1
            return
        h["fail"] += 1
        h["last_status"] = code
        h["last_error"] = str(exc)[:200] if exc is not None else None
        if challenge:
            h["bot_challenge"] = True


def mark_blocked(host: str, reason: str) -> None:
    """Skip a host for this run (e.g. a cross-run Blockscout backoff decided by run.py)."""
    with _health_lock:
        h = SOURCE_HEALTH.setdefault(
            host, {"ok": 0, "fail": 0, "absent": 0, "last_status": None, "last_error": None,
                   "bot_challenge": False, "blocked_reason": None})
        h["bot_challenge"] = True
        h["blocked_reason"] = reason


def is_blocked(host: str) -> bool:
    """True when this host answered with a bot challenge (or was marked blocked) this run."""
    return bool(SOURCE_HEALTH.get(host, {}).get("bot_challenge"))


def degraded_hosts() -> dict:
    """Hosts that failed at least once and served nothing this run — gone dark."""
    return {h: v for h, v in SOURCE_HEALTH.items()
            if (v["fail"] and not v["ok"]) or v["bot_challenge"]}


def health_report() -> str:
    if not SOURCE_HEALTH:
        return "no HTTP calls made"
    out = []
    for host, v in sorted(SOURCE_HEALTH.items()):
        tag = ("BOT-CHALLENGED" if v["bot_challenge"] else
               "DARK" if v["fail"] and not v["ok"] else
               "degraded" if v["fail"] else "ok")
        line = f"{host}: {tag} ({v['ok']} ok / {v['fail']} failed / {v['absent']} absent"
        if v["last_status"]:
            line += f", last HTTP {v['last_status']}"
        if v["blocked_reason"]:
            line += f", {v['blocked_reason']}"
        out.append(line + ")")
    return "\n".join(out)


def health_snapshot() -> dict:
    with _health_lock:
        return {h: dict(v) for h, v in SOURCE_HEALTH.items()}


def reset_health() -> None:
    with _health_lock:
        SOURCE_HEALTH.clear()
        _host_header_idx.clear()


# ── throttle / headers ───────────────────────────────────────────────────────────
def _throttle(host: str) -> None:
    """Wait until this host's next slot is due. Held under a lock, and the sleep happens
    INSIDE it — that is the whole point. Unsynchronised, N threads all read the same stale
    _last_call, all compute the same gap, sleep the same interval and fire together, so a
    "1 Hz" ceiling becomes N requests in one instant (measured: 8 callers in 0.87 s)."""
    with _throttle_lock:
        hz = _HOST_HZ.get(host, config.DEFAULT_RATE_HZ)
        gap = 1.0 / hz
        dt = time.monotonic() - _last_call.get(host, 0.0)
        if dt < gap:
            time.sleep(gap - dt)
        _last_call[host] = time.monotonic()


def _headers_for(host: str, extra: dict | None) -> dict:
    hdrs = {"User-Agent": config.USER_AGENT, "Accept": "application/json",
            "Accept-Encoding": "gzip, deflate"}
    sets = config.HOST_HEADER_SETS.get(host)
    if sets:
        hdrs.update(sets[_host_header_idx.get(host, 0) % len(sets)])
    if extra:
        hdrs.update(extra)
    return hdrs


def _rotate_headers(host: str) -> bool:
    """Advance to the next header set for this host. Returns False when there is none left."""
    sets = config.HOST_HEADER_SETS.get(host)
    if not sets:
        return False
    idx = _host_header_idx.get(host, 0)
    if idx + 1 >= len(sets):
        return False
    _host_header_idx[host] = idx + 1
    return True


def _describe(exc) -> str:
    """The server's message, readable: decompress (error bodies are gzipped too — we asked
    for gzip) and unwrap {"error": {"message": ...}}."""
    body = ""
    try:
        if isinstance(exc, urllib.error.HTTPError):
            raw = exc.read()
            enc = (exc.headers.get("Content-Encoding") or "").lower()
            if enc == "gzip":
                raw = gzip.decompress(raw)
            elif enc == "deflate":
                raw = zlib.decompress(raw)
            body = raw.decode("utf-8", "replace").strip()
            try:
                err = json.loads(body).get("error")
                if isinstance(err, dict) and err.get("message"):
                    body = err["message"]
                elif isinstance(err, str):
                    body = err
            except Exception:
                pass
            body = body[:300]
    except Exception:
        pass
    return f"{exc}{' :: ' + body if body else ''}"


# ── the request loop ─────────────────────────────────────────────────────────────
def _request(url: str, data: bytes | None, headers: dict | None):
    """GET/POST with throttle, retry, gzip. Returns parsed JSON | NOT_FOUND | None."""
    host = urllib.parse.urlparse(url).netloc
    if is_blocked(host):
        return None                          # deferred: the host is dark this run
    rotated = False
    attempt = 0
    while attempt < config.HTTP_RETRIES:
        hdrs = _headers_for(host, headers)
        try:
            _throttle(host)
            req = urllib.request.Request(url, data=data, headers=hdrs)
            with _urlopen(req, timeout=config.HTTP_TIMEOUT, context=_SSL) as resp:
                raw = resp.read()
                enc = (resp.headers.get("Content-Encoding") or "").lower()
                if enc == "gzip":
                    raw = gzip.decompress(raw)
                elif enc == "deflate":
                    raw = zlib.decompress(raw)
                out = json.loads(raw.decode("utf-8")) if raw.strip() else {}
            _record(host, True)
            return out
        except Exception as exc:
            code = getattr(exc, "code", None)
            if code in (400, 404, 422):
                # the server answered: deterministic "not there" — retrying cannot help
                # (422 is ScanHood's explicit 'quote could not be built' = no route)
                with _health_lock:
                    h = SOURCE_HEALTH.setdefault(
                        host, {"ok": 0, "fail": 0, "absent": 0, "last_status": None,
                               "last_error": None, "bot_challenge": False,
                               "blocked_reason": None})
                    h["ok"] += 1
                    h["absent"] += 1
                return NOT_FOUND
            if _is_bot_challenge(code, exc):
                if not rotated and _rotate_headers(host):
                    rotated = True           # one retry with the next header set, not counted
                    continue
                first = not SOURCE_HEALTH.get(host, {}).get("bot_challenge")
                _record(host, False, code, exc, challenge=True)
                if first:
                    print(f"  [http] BOT-CHALLENGED {host}: HTTP {code} (WAF/bot check — not a "
                          f"rate limit; retrying cannot help). First URL: {url.split('?')[0]}")
                return None
            attempt += 1
            if attempt >= config.HTTP_RETRIES:
                _record(host, False, code, exc)
                print(f"  [http {code or 'err'}] {url.split('?')[0]}: {_describe(exc)}")
                return None
            # 429 = a per-minute window (GeckoTerminal) — wait it out, don't hammer
            time.sleep(20.0 * attempt if code == 429 else 1.5 * attempt)
    return None


def _atomic_write_json(path: str, obj) -> None:
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f)
    os.replace(tmp, path)


def _cache_read(cache_path: str | None, max_age_sec: float | None):
    if not (cache_path and os.path.exists(cache_path)):
        return None, False
    age = time.time() - os.path.getmtime(cache_path)
    if max_age_sec is not None and age > max_age_sec:
        return None, False
    try:
        with open(cache_path) as f:
            obj = json.load(f)
    except Exception:
        return None, False
    if isinstance(obj, dict) and obj.get("__absent__") is True:
        return NOT_FOUND, True
    return obj, True


def get_json(url: str, cache_path: str | None = None, max_age_sec: float | None = None,
             headers: dict | None = None, cache_404: bool = False):
    """GET a JSON document. A fresh cache (< max_age_sec; None = never expires) short-circuits
    BEFORE the throttle, so warm runs are instant and cost no rate budget. Returns the parsed
    object, NOT_FOUND (server said 400/404; negatively cached when cache_404), or None
    (deferred: must be retried, never scored)."""
    obj, hit = _cache_read(cache_path, max_age_sec)
    if hit:
        return obj
    data = _request(url, data=None, headers=headers)
    if cache_path:
        try:
            if data is not None and not is_absent(data):
                _atomic_write_json(cache_path, data)
            elif is_absent(data) and cache_404:
                _atomic_write_json(cache_path, {"__absent__": True})
        except Exception:
            pass
    return data


def post_json(url: str, payload, headers: dict | None = None,
              cache_path: str | None = None, max_age_sec: float | None = None):
    """POST a JSON body (JSON-RPC etc.). Same throttle/retry/SSL/contract as get_json. Caching
    a POST is opt-in (immutable facts only — a stale quote is a fabricated fill)."""
    obj, hit = _cache_read(cache_path, max_age_sec)
    if hit:
        return obj
    hdrs = {"Content-Type": "application/json"}
    if headers:
        hdrs.update(headers)
    data = _request(url, data=json.dumps(payload).encode("utf-8"), headers=hdrs)
    if cache_path and data is not None and not is_absent(data):
        try:
            _atomic_write_json(cache_path, data)
        except Exception:
            pass
    return data


if __name__ == "__main__":
    print("robinhood_screener http_client")
    # 1. throttle timing on a fake host
    _HOST_HZ["fake.invalid"] = 5.0
    t0 = time.monotonic()
    _throttle("fake.invalid"); _throttle("fake.invalid")
    dt = time.monotonic() - t0
    print(f"  throttle: two calls took {dt:.2f}s (expect >= 0.20s)  {'ok' if dt >= 0.19 else 'FAILED'}")
    # 2. cache hit short-circuits the network; a cached negative marker returns NOT_FOUND
    cache = os.path.join(config.CACHE_DIR, "_smoke.json")
    _atomic_write_json(cache, {"cached": True})
    print(f"  cache hit: {get_json('https://example.invalid/never', cache_path=cache, max_age_sec=3600)}")
    _atomic_write_json(cache, {"__absent__": True})
    print(f"  cached negative: {get_json('https://example.invalid/never', cache_path=cache, max_age_sec=3600)!r}")
    os.remove(cache)
    # 3. a dark host is deferred (None) and reported DARK
    got = get_json("https://verify.invalid/x")
    print(f"  dark host → {got!r}  (None = deferred)")
    # 4. a real 404 is absent (NOT_FOUND), not deferred
    nf = get_json(f"{config.BLOCKSCOUT_BASE}/api/v2/tokens/0x0000000000000000000000000000000000000001")
    print(f"  blockscout 404 → {nf!r}  (NOT_FOUND = absent)")
    # 5. Blockscout with the browser header set
    d = get_json(f"{config.BLOCKSCOUT_BASE}/api/v2/tokens/{config.MIZUKARA}")
    print(f"  blockscout MIZUKARA: {'ok — ' + str(d.get('symbol')) if d and not is_absent(d) else repr(d)}")
    # 6. RPC
    rpc = post_json(config.RPC_URL, {"jsonrpc": "2.0", "id": 1, "method": "eth_blockNumber", "params": []})
    print(f"  rpc eth_blockNumber: {int(rpc['result'], 16) if rpc and rpc.get('result') else repr(rpc)}")
    print("\n--- source health ---")
    print(health_report())
