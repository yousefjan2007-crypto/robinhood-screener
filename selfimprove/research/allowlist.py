"""
The research-branch allowlist — the ONE enforcement point between a headless model's diff
and origin/main.

WHAT. allowlist_ok(paths) -> (ok, offenders) accepts exactly three shapes:
  * selfimprove/candidates/<name>.py   where <name> matches ^[a-z][a-z0-9_]*$ and is not
                                       register.py (the registrar), _template.py or __init__.py
  * selfimprove/research/proposals/PROPOSAL_YYYY-MM-DD.md   (the one write-up per session)
  * selfimprove/research/context/...   (gitignored scratch; listed so a stray add is harmless)
Everything else is an offender: config.py, screen.py, run.py, alerts.py, ledger.py, verify.py,
champion.json, trials.json, registry.json, register.py, _template.py, anything under data/ or
docs/, workflows, plists, this file.

WHY A GIT-DIFF ALLOWLIST AND NOT TOOL PERMISSIONS. `claude -p --permission-mode acceptEdits`
can edit any file in its cwd; --allowedTools/--disallowedTools are defence in depth, not a
boundary (a model that wants to change a threshold can write it with any tool that writes).
The boundary that holds is: the session runs on a throwaway branch inside a throwaway
worktree, and run_research.sh merges NOTHING unless `git diff --name-only origin/main...HEAD`
passes this function. An offender pushes the branch for a human and the merge is skipped.
The wrapper runs THE MAC TREE'S copy of this file, never the worktree's — a research session
could otherwise weaken the allowlist in the same diff it needs to pass.

CLI: reads repo-relative paths from stdin (one per line), prints the offenders, exits 1 if any.
`--selftest` is offline and asserts the rejects/accepts listed in the docstring.
"""
from __future__ import annotations

import re
import sys

CANDIDATE_RE = re.compile(r"^selfimprove/candidates/[a-z][a-z0-9_]*\.py$")
PROPOSAL_RE = re.compile(r"^selfimprove/research/proposals/PROPOSAL_\d{4}-\d{2}-\d{2}\.md$")
CONTEXT_RE = re.compile(r"^selfimprove/research/context/.+$")
CANDIDATE_RESERVED = {"selfimprove/candidates/register.py",
                      "selfimprove/candidates/_template.py",
                      "selfimprove/candidates/__init__.py"}


def path_ok(path: str) -> bool:
    p = str(path).strip()
    if not p or p.startswith("/") or ".." in p.split("/"):
        return False
    if p in CANDIDATE_RESERVED:
        return False
    return bool(CANDIDATE_RE.match(p) or PROPOSAL_RE.match(p) or CONTEXT_RE.match(p))


def allowlist_ok(paths) -> tuple:
    """(ok, offenders). Blank lines are ignored; an empty diff is ok (nothing to merge, and
    nothing to refuse). Never raises: a non-string entry is an offender by its repr."""
    offenders = []
    for raw in paths or []:
        try:
            p = str(raw).strip()
        except Exception:
            offenders.append(repr(raw))
            continue
        if not p:
            continue
        if not path_ok(p):
            offenders.append(p)
    return (not offenders), offenders


def _selftest() -> int:
    rejected = [
        "config.py", "screen.py", "run.py", "alerts.py", "ledger.py", "verify.py",
        "selfimprove/champion.json", "selfimprove/trials.json",
        "selfimprove/candidates/registry.json", "selfimprove/candidates/register.py",
        "selfimprove/candidates/_template.py", "selfimprove/candidates/__init__.py",
        "selfimprove/candidates/Band_Upper.py", "selfimprove/candidates/9lives.py",
        "selfimprove/candidates/sub/dir.py", "selfimprove/candidates/x.pyc",
        "selfimprove/research/allowlist.py", "selfimprove/research/run_research.sh",
        "selfimprove/research/research_prompt.md",
        "selfimprove/research/proposals/PROPOSAL_2026-9-1.md",
        "selfimprove/research/proposals/notes.md",
        "selfimprove/research/proposals/PROPOSAL_2026-09-13.md.bak",
        "data/anything", "data/ledger.csv", "data/proposals/entry-x.md", "docs/DESIGN.md",
        ".github/workflows/screener.yml", "launchd/com.yousefjan.robinhood-research.plist",
        "/selfimprove/candidates/abs.py", "selfimprove/candidates/../config.py",
    ]
    for p in rejected:
        ok, off = allowlist_ok([p])
        assert not ok and off == [p], (p, ok, off)
    accepted = ["selfimprove/candidates/band_liq_rt.py", "selfimprove/candidates/sell_45m.py",
                "selfimprove/research/proposals/PROPOSAL_2026-09-13.md",
                "selfimprove/research/context/context_2026-09-13.md"]
    ok, off = allowlist_ok(accepted)
    assert ok and off == [], (ok, off)
    ok, off = allowlist_ok(accepted + ["config.py", "", "  ", "data/x"])
    assert not ok and off == ["config.py", "data/x"], off
    assert allowlist_ok([]) == (True, []) and allowlist_ok(None) == (True, [])
    assert allowlist_ok([None]) == (False, ["None"])
    print(f"allowlist selftest ok: {len(rejected)} rejected shapes, {len(accepted)} accepted")
    return 0


def main(argv: list) -> int:
    if "--selftest" in argv:
        return _selftest()
    paths = [ln.rstrip("\n") for ln in sys.stdin]
    ok, offenders = allowlist_ok(paths)
    if ok:
        n = sum(1 for p in paths if p.strip())
        print(f"allowlist ok: {n} path(s) inside the research allowlist")
        return 0
    print("ALLOWLIST VIOLATION: " + " ".join(offenders))
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
