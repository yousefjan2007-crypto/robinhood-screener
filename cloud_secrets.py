"""
Set the GitHub Actions secrets for the cloud scan WITHOUT a value ever touching argv, a log, or
shell history. THE HUMAN RUNS THIS (an assistant may not enter credentials).

    python3 cloud_secrets.py           # pipe TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID / NTFY_TOPIC
                                       # from config.load_credentials() into `gh secret set`
    python3 cloud_secrets.py --check   # list the secret NAMES the repo has

Values go to `gh` over stdin — never as a command-line argument (argv is visible to every process
and lands in shell history), and this script prints only names and lengths.
"""
from __future__ import annotations

import subprocess
import sys

import config

REPO = "yousefjan2007-crypto/robinhood-screener"


def _set(name: str, value: str) -> bool:
    if not value:
        print(f"  {name}: EMPTY in load_credentials() — not set")
        return False
    r = subprocess.run(["gh", "secret", "set", name, "-R", REPO], input=value.encode(),
                       capture_output=True)
    ok = r.returncode == 0
    print(f"  {name}: {'set' if ok else 'FAILED'} (len {len(value)})"
          + ("" if ok else f" — {r.stderr.decode(errors='replace').strip()[:200]}"))
    return ok


def main(argv: list) -> int:
    if "--check" in argv:
        return subprocess.run(["gh", "secret", "list", "-R", REPO]).returncode
    creds = config.load_credentials()
    tg = creds.get("telegram") or {}
    results = [_set("TELEGRAM_BOT_TOKEN", tg.get("bot_token") or ""),
               _set("TELEGRAM_CHAT_ID", str(tg.get("chat_id") or "")),
               _set("NTFY_TOPIC", creds.get("ntfy_topic") or "")]
    print("done" if all(results) else "some secrets were NOT set — fix and rerun")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
