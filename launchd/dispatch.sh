#!/bin/bash
# Fire the cloud scan. Exit 0 always (jarvis reads non-zero launchd exits as faults); the outcome
# is one line in dispatch.out.log. `gh` needs the `workflow` scope (present on this account).
export PATH="$HOME/.npm-global/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
REPO="yousefjan2007-crypto/robinhood-screener"
if out=$(gh workflow run robinhood-screener -R "$REPO" -f trigger=dispatch -f mode=run 2>&1); then
  echo "$(date -u +%FT%TZ) dispatched"
else
  echo "$(date -u +%FT%TZ) dispatch FAILED: ${out//$'\n'/ }"
fi
exit 0
