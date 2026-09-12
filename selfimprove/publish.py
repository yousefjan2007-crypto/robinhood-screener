"""
Publish selected files to origin/main from a DETACHED TEMPORARY WORKTREE — never from the
user's checkout, never touching HEAD.

Why this shape: the improve job runs on the Mac while the cloud bot commits every few minutes.
A `git pull` in the working tree could conflict or move the user's HEAD; a plain `git push`
from a stale tree would be rejected. So: fetch, add a detached worktree of origin/main under
mkdtemp() OUTSIDE the repo (a worktree inside it would carry a data/ledger.csv that a sibling
voice assistant globs for), copy in exactly the listed files, commit as robinhood-improve[bot],
push HEAD:main, and on a non-fast-forward rejection fetch and retry. The worktree is removed in
a finally. reconcile() republishes a local champion.json that differs from origin/main (the
crash-after-write case).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402

REMOTE = os.environ.get("PUBLISH_REMOTE", "origin")
BRANCH = os.environ.get("PUBLISH_BRANCH", "main")
BOT_NAME, BOT_EMAIL = "robinhood-improve[bot]", "bot@users.noreply.github.com"


def _git(args: list, cwd: str, check: bool = True, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                          timeout=timeout, check=check)


def _rel(path: str) -> str:
    return os.path.relpath(os.path.abspath(path), config.ROOT)


def publish_files(paths: list, message: str, retries: int | None = None,
                  root: str | None = None) -> bool:
    """Commit + push the listed repo-relative (or absolute-under-root) files. Returns True when
    origin/main carries them. Never raises; prints why on failure."""
    root = root or config.ROOT
    retries = retries if retries is not None else config.IMPROVE_PUBLISH_RETRIES
    rels = [_rel(p) if os.path.isabs(p) else p for p in paths]
    missing = [r for r in rels if not os.path.exists(os.path.join(root, r))]
    if missing:
        print(f"  [publish] refusing: missing {missing}")
        return False
    for attempt in range(1, retries + 1):
        wt = tempfile.mkdtemp(prefix="rh_publish_")
        try:
            _git(["fetch", "-q", REMOTE], root)
            _git(["worktree", "add", "--detach", wt, f"{REMOTE}/{BRANCH}"], root)
            for r in rels:
                dst = os.path.join(wt, r)
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copyfile(os.path.join(root, r), dst)
            _git(["add", "--", *rels], wt)
            if _git(["diff", "--cached", "--quiet"], wt, check=False).returncode == 0:
                print("  [publish] nothing to publish (origin/main already has these files)")
                return True
            _git(["-c", f"user.name={BOT_NAME}", "-c", f"user.email={BOT_EMAIL}",
                  "commit", "-q", "-m", message], wt)
            r = _git(["push", "-q", REMOTE, f"HEAD:{BRANCH}"], wt, check=False)
            if r.returncode == 0:
                print(f"  [publish] pushed {len(rels)} file(s) to {REMOTE}/{BRANCH}")
                return True
            print(f"  [publish] push rejected (attempt {attempt}/{retries}): "
                  f"{r.stderr.strip().splitlines()[-1] if r.stderr.strip() else '?'}")
        except Exception as exc:
            print(f"  [publish] attempt {attempt}/{retries} failed: {exc}")
        finally:
            _git(["worktree", "remove", "--force", wt], root, check=False)
            shutil.rmtree(wt, ignore_errors=True)
            _git(["worktree", "prune"], root, check=False)
    return False


def origin_blob(rel: str, root: str | None = None) -> str | None:
    """`git show origin/main:<rel>` as text; None when absent (exit 128) — silently."""
    root = root or config.ROOT
    r = _git(["show", f"{REMOTE}/{BRANCH}:{rel}"], root, check=False)
    return r.stdout if r.returncode == 0 else None


def reconcile(root: str | None = None) -> bool:
    """Republish champion.json when the local copy differs from origin/main. True when in sync."""
    root = root or config.ROOT
    rel = _rel(config.CHAMPION_PATH)
    local = open(config.CHAMPION_PATH).read() if os.path.exists(config.CHAMPION_PATH) else None
    if local is None:
        return True
    _git(["fetch", "-q", REMOTE], root, check=False)
    remote = origin_blob(rel, root)
    if remote == local:
        return True
    print("  [publish] local champion.json differs from origin/main — republishing")
    return publish_files([config.CHAMPION_PATH], "champion: reconcile local state", root=root)


if __name__ == "__main__":
    # Offline self-test against a temporary bare origin: publish, then a competing commit,
    # then publish again (retry after a non-fast-forward), then reconcile.
    import json
    with tempfile.TemporaryDirectory() as d:
        bare = os.path.join(d, "origin.git"); work = os.path.join(d, "work"); other = os.path.join(d, "other")
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", bare], check=True)
        subprocess.run(["git", "init", "-q", "-b", "main", work], check=True)
        _git(["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "root"], work)
        _git(["remote", "add", "origin", bare], work); _git(["push", "-q", "origin", "main"], work)
        os.makedirs(os.path.join(work, "selfimprove"))
        json.dump({"schema": 2}, open(os.path.join(work, "selfimprove", "champion.json"), "w"))
        # monkeypatch the repo root
        config.ROOT = work; config.CHAMPION_PATH = os.path.join(work, "selfimprove", "champion.json")
        assert publish_files(["selfimprove/champion.json"], "test publish", root=work)
        # competing commit on origin
        subprocess.run(["git", "clone", "-q", "-b", "main", bare, other], check=True)
        open(os.path.join(other, "x.txt"), "w").write("x")
        _git(["add", "x.txt"], other)
        _git(["-c", "user.name=o", "-c", "user.email=o@o", "commit", "-q", "-m", "competing"], other)
        _git(["push", "-q", "origin", "main"], other)
        json.dump({"schema": 2, "locked": True}, open(config.CHAMPION_PATH, "w"))
        assert publish_files(["selfimprove/champion.json"], "second publish", root=work)
        assert origin_blob("selfimprove/champion.json", work) == open(config.CHAMPION_PATH).read()
        assert origin_blob("x.txt", work) == "x", "the competing commit survived the retry"
        assert origin_blob("nope.txt", work) is None
        assert reconcile(work) is True
        # the user's worktree/HEAD untouched: still on main, no x.txt checked out
        assert _git(["rev-parse", "--abbrev-ref", "HEAD"], work).stdout.strip() == "main"
        assert not os.path.exists(os.path.join(work, "x.txt"))
        assert not any(n.startswith("rh_publish_") for n in os.listdir(tempfile.gettempdir()))
    print("publish self-test ok (temp bare origin, non-fast-forward retry, reconcile, HEAD untouched)")
