#!/bin/bash
# Sunday 11:00 (launchd com.yousefjan.robinhood-improve): the weekly self-improvement pass.
#
#   1. sync the Mac tree to origin/main (ff-only; a diverged tree is reported, never forced)
#   2. back up the Mac-local live book, then run the EXIT gate (improve.py --apply --send)
#   3. run the ENTRY-BAND gate (entry_lab/improve_bands.py --apply --send)
#   4. write data/livebook_summary.json and publish champion / trials / history / proposals /
#      summary to origin/main from a detached temp worktree (publish.py), then ff-sync again so
#      the Mac tree equals origin/main
#   5. exit 0 ALWAYS (jarvis reads a non-zero launchd exit as a fault)
#
# --send on the two gates means EVENT alerts only (PROMOTED / DEMOTED / NOMINATED / APPARATUS
# FAULT / PUBLISH FAILED / PAUSED). The ONE weekly summary is sent by run_research.sh at 12:00,
# which reads this run's verdicts. Nothing here touches keys or funds.
set -uo pipefail
export PATH="$HOME/.npm-global/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
REPO="$HOME/robinhood_screener"
PY="/Library/Frameworks/Python.framework/Versions/3.9/bin/python3"
cd "$REPO" || exit 0
STAMP="$(date -u +%FT%TZ)"
echo "[improve] $STAMP start"

# 1. sync
if ! git fetch -q origin; then
  echo "[improve] git fetch failed — evaluating on the local tree, publishing skipped"
  NOSYNC=1
else
  NOSYNC=0
  if ! git merge -q --ff-only origin/main 2>/dev/null; then
    echo "[improve] local main has diverged from origin/main (or the tree is dirty) — evaluating on the local tree; publish will still target origin/main"
  fi
fi

# 2. back up the live book (Mac-local, gitignored) before anything reads it
mkdir -p "$REPO/data/backups"
[ -f "$REPO/data/livebook.json" ] && cp "$REPO/data/livebook.json" "$REPO/data/backups/livebook_$(date +%Y%m%d).json"
ls -t "$REPO"/data/backups/livebook_*.json 2>/dev/null | tail -n +9 | xargs rm -f 2>/dev/null

# exit gate
"$PY" selfimprove/improve.py --apply --send 2>&1 || echo "[improve] improve.py exited non-zero (continuing)"
# entry-band gate
"$PY" selfimprove/entry_lab/improve_bands.py --apply --send 2>&1 || echo "[improve] improve_bands.py exited non-zero (continuing)"

# 4. summary json + publish
"$PY" selfimprove/improve.py --summary-json 2>&1 || echo "[improve] summary json failed (continuing)"
FILES="selfimprove/champion.json selfimprove/trials.json selfimprove/improve_history.jsonl data/entry_lab_history.jsonl data/livebook_summary.json selfimprove/candidates/registry.json"
for f in $(ls data/proposals/*.md 2>/dev/null | tail -n 4); do FILES="$FILES $f"; done
EXIST=""
for f in $FILES; do [ -f "$f" ] && EXIST="$EXIST $f"; done
if [ "$NOSYNC" = "0" ] && [ -n "$EXIST" ]; then
  "$PY" -c "
import sys; sys.path.insert(0, '.')
from selfimprove import publish
ok = publish.publish_files('''$EXIST'''.split(), 'selfimprove: weekly gate ' + '$STAMP')
print('[improve] published' if ok else '[improve] PUBLISH FAILED — local state stands; reconcile() runs next week')
sys.exit(0)
" 2>&1
  git merge -q --ff-only origin/main 2>/dev/null || true
fi
echo "[improve] $(date -u +%FT%TZ) done"
exit 0
