#!/bin/bash
# run_research.sh — the weekly headless Claude Code research session (Sunday 12:00, launchd
# com.yousefjan.robinhood-research). It proposes CANDIDATE bands/policies as code; nothing it
# does can touch the champion.
#
# SINCE THE GATES MOVED TO THE CLOUD (.github/workflows/weekly.yml, Sunday 10:00 UTC) this is the
# only Sunday job left on the Mac, and it is OPTIONAL: the two gates, the summary json, the
# publish and the ONE weekly message all run on the runner whether this session runs or not.
# Two consequences, both below: step 0 carries the ff-only sync the deleted run_improve.sh used
# to do (the Mac tree must still equal origin/main before a human reads it), and finish() SENDS
# the summary only under RESEARCH_SEND_SUMMARY=1 — by default it prints the same lines with
# --dry, because weekly.yml already sent the week's one message and two would be worse than none.
#
# WORKTREE DISCIPLINE. Every step after the context dump runs inside a DETACHED TEMPORARY
# WORKTREE created with mktemp -d OUTSIDE the repo (a worktree inside it would carry a
# data/ledger.csv that a sibling voice assistant globs for). The Mac checkout is never
# checked out, pulled, committed to or reset by this script, so:
#   * it can never deadlock on a dirty tree — the solana improve job leaves its tree dirty
#     every Sunday, and a dirty-tree refusal there would fire forever;
#   * the model edits a copy of origin/main, never the user's files;
#   * the worktree is removed by an EXIT trap whatever happens.
# The trusted copies of allowlist.py and weekly_summary.py are the MAC TREE's ($REPO), never the
# worktree's: a session could otherwise weaken the allowlist in the same diff it needs to pass.
#
# EVERY FAILURE FUNNELS INTO THE SUMMARY. There is no `set -e`: a missing claude binary, a spent
# budget, a PAUSE file, a fetch failure, an allowlist violation or a red verify.py all set
# REASON and fall through to weekly_summary.py, which renders the same lines on every path out —
# silence from a Sunday job is indistinguishable from a broken job. It is DELIVERED from
# weekly.yml (the one message, with its own research line); here it is rendered with --dry unless
# RESEARCH_SEND_SUMMARY=1 says the cloud job is down and this run owns delivery. The script exits
# 0 always (launchd contract).
#
# MERGE RULE. A research branch merges only if (a) the diff is inside the allowlist, (b) no more
# new candidate modules than the weekly budget, (c) register.py --scan has run, and (d) verify.py
# is GREEN in the worktree (missing verify.py counts as red — never merge unverified). Anything
# else pushes the branch for a human and merges nothing.
#
# Env: RESEARCH_DRYRUN=1 skips the claude call (a dummy proposal exercises the plumbing), NEVER
#      pushes to origin and never syncs the Mac tree; RESEARCH_ORIGIN=<temp bare repo> makes a
#      dry run push there instead, so the push/merge path is testable; RESEARCH_SEND_SUMMARY=1
#      delivers the summary from here instead of rendering it (weekly.yml owns delivery);
#      RESEARCH_MAX_TURNS caps the session; CLAUDE_BIN overrides the binary; REPO/PY override the
#      tree and interpreter.

set -uo pipefail
export PATH="$HOME/.npm-global/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:${PATH:-}"

REPO="${REPO:-$HOME/robinhood_screener}"
PY="${PY:-/Library/Frameworks/Python.framework/Versions/3.9/bin/python3}"
CLAUDE_BIN="${CLAUDE_BIN:-$(command -v claude || true)}"
DATE="$(date +%Y-%m-%d)"
LOGDIR="$REPO/selfimprove/research/logs"
CTXDIR="$REPO/selfimprove/research/context"
DRY="${RESEARCH_DRYRUN:-0}"
MAX_TURNS="${RESEARCH_MAX_TURNS:-40}"
CAFF="$(command -v caffeinate || true)"
mkdir -p "$LOGDIR" "$CTXDIR"

REASON=""          # the one-line outcome the summary carries
WT=""              # the temporary worktree (empty until step 5)
N=0                # registration budget remaining this week

log() { printf '[research %s] %s\n' "$(date +%H:%M:%S)" "$*"; }

cleanup() {
    if [ -n "$WT" ]; then
        git -C "$REPO" worktree remove --force "$WT" >/dev/null 2>&1
        rm -rf "$WT" >/dev/null 2>&1
    fi
    git -C "$REPO" worktree prune >/dev/null 2>&1
}
trap cleanup EXIT

# The weekly summary, RENDERED on every path out of this script so the outcome is always in the
# log. weekly.yml sends the week's ONE message; this one is --dry unless RESEARCH_SEND_SUMMARY=1
# hands delivery back (the cloud job down, or a deliberate local send).
finish() {
    log "outcome: ${REASON:-no outcome recorded}"
    if [ "${RESEARCH_SEND_SUMMARY:-0}" = "1" ] && [ "$DRY" != "1" ]; then MODE="--send"; else MODE="--dry"; fi
    (cd "$REPO" && "$PY" selfimprove/weekly_summary.py "$MODE" --research "${REASON:-no outcome recorded}") \
        2>&1 | tee -a "$LOGDIR/run_$DATE.log"
    exit 0
}

# Push the research branch somewhere a human can read it. Never to origin under DRYRUN.
push_branch_for_human() {
    if [ "$DRY" = "1" ]; then
        if [ -n "${RESEARCH_ORIGIN:-}" ]; then
            git -C "$WT" push -q "$RESEARCH_ORIGIN" "research/$DATE" 2>&1 | tail -1 || true
            log "DRYRUN: pushed research/$DATE to RESEARCH_ORIGIN for the human"
        else
            log "DRYRUN: would have pushed research/$DATE to origin for the human (not pushed)"
        fi
    else
        git -C "$WT" push -q origin "research/$DATE" 2>&1 | tail -1 || true
    fi
}

# ── 0. sync the Mac tree to origin/main — inherited from the deleted run_improve.sh ──────
# ff-only and never forced: a diverged or dirty tree is REPORTED and the run continues (the
# session's own worktree is cut from origin/main regardless, so nothing below depends on this).
# Skipped under DRYRUN: the plumbing test must not move the user's tree.
if [ "$DRY" != "1" ]; then
    if git -C "$REPO" fetch -q origin; then
        git -C "$REPO" merge -q --ff-only origin/main 2>/dev/null \
            || log "local main has diverged from origin/main (or the tree is dirty) — not forced"
    else
        log "git fetch failed — the Mac tree is not synced; the session still reads origin/main below"
    fi
fi

# ── 2. refusals (each exits 0 through the summary) ───────────────────────────────
if [ -z "$CLAUDE_BIN" ] && [ "$DRY" != "1" ]; then
    REASON="research skipped: claude binary not found on PATH"; finish
fi
if [ -f "$REPO/selfimprove/PAUSE" ]; then
    REASON="research paused (selfimprove/PAUSE present)"; finish
fi
N="$(cd "$REPO" && "$PY" selfimprove/candidates/register.py --budget-remaining 2>/dev/null | tail -1)"
case "$N" in ''|*[!0-9]*) N=0 ;; esac
if [ "$N" -le 0 ]; then
    REASON="research skipped: registration budget spent (0 new candidates allowed this week)"; finish
fi
log "budget: $N new candidate(s) allowed this week"

# ── 3. fetch (the worktree is cut from origin/main, never from the Mac HEAD) ─────
if ! git -C "$REPO" fetch -q origin 2>&1 | tee -a "$LOGDIR/run_$DATE.log"; then
    REASON="research skipped: git fetch origin failed"; finish
fi
if ! git -C "$REPO" rev-parse -q --verify origin/main >/dev/null 2>&1; then
    REASON="research skipped: origin/main not present after fetch"; finish
fi

# ── 4. context dump for the session (gitignored; copied into the worktree) ───────
CTX="$CTXDIR/context_$DATE.md"
{
    echo "# research context — $DATE"
    echo
    echo "## entry-lab scorecard (selfimprove/entry_lab/scorecard.py --markdown)"
    echo
    (cd "$REPO" && "$PY" selfimprove/entry_lab/scorecard.py --markdown 2>&1) \
        || echo "(scorecard unavailable this week — see the error above; treat every sample size as 0)"
    echo
    echo "## latest proposals (data/proposals/)"
    ls -1t "$REPO"/data/proposals/entry-*.md 2>/dev/null | head -1 | xargs -I{} basename {} || true
    ls -1t "$REPO"/data/proposals/proposal-*.md 2>/dev/null | head -1 | xargs -I{} basename {} || true
    echo
    echo "## selfimprove/champion.json"; echo '```json'
    cat "$REPO/selfimprove/champion.json" 2>/dev/null || echo "{}"
    echo '```'; echo
    echo "## selfimprove/trials.json"; echo '```json'
    cat "$REPO/selfimprove/trials.json" 2>/dev/null || echo "{}"
    echo '```'; echo
    echo "## last 4 research proposals (selfimprove/research/proposals/)"
    ls -1t "$REPO"/selfimprove/research/proposals/PROPOSAL_*.md 2>/dev/null | head -4 | xargs -I{} basename {} || true
    echo
    echo "## registration budget"
    echo "python3 selfimprove/candidates/register.py --budget-remaining -> $N"
} > "$CTX" 2>&1
log "context written: $CTX ($(wc -l < "$CTX" | tr -d ' ') lines)"

# ── 5. the throwaway worktree + branch ───────────────────────────────────────────
WT="$(mktemp -d "${TMPDIR:-/tmp}/rh_research_XXXXXX")"
if ! git -C "$REPO" worktree add --detach "$WT" origin/main >/dev/null 2>&1; then
    REASON="research skipped: could not create the temporary worktree"; finish
fi
git -C "$WT" checkout -q -B "research/$DATE"
mkdir -p "$WT/selfimprove/research/context" "$WT/selfimprove/research/proposals"
cp "$CTX" "$WT/selfimprove/research/context/"

# ── 6. the session (or, under DRYRUN, a dummy proposal that exercises the plumbing) ─
if [ "$DRY" = "1" ]; then
    printf '# PROPOSAL %s (RESEARCH_DRYRUN at %s)\n\nDry-run plumbing check; no hypothesis, no candidate.\n' \
        "$DATE" "$(date +%H:%M:%S)" > "$WT/selfimprove/research/proposals/PROPOSAL_$DATE.md"
    log "DRYRUN: claude not invoked; dummy proposal written"
    # test hook (DRYRUN only): RESEARCH_DRYRUN_TOUCH=<repo-relative path> plants a file in the
    # worktree so the allowlist-violation path can be exercised without a model.
    if [ -n "${RESEARCH_DRYRUN_TOUCH:-}" ]; then
        mkdir -p "$WT/$(dirname "$RESEARCH_DRYRUN_TOUCH")"
        echo "# planted by RESEARCH_DRYRUN_TOUCH" >> "$WT/$RESEARCH_DRYRUN_TOUCH"
    fi
else
    PROMPT="$(sed "s/<date>/$DATE/g" "$WT/selfimprove/research/research_prompt.md")"
    log "starting claude -p (max-turns $MAX_TURNS) in $WT"
    (cd "$WT" && ${CAFF:+"$CAFF" -i} "$CLAUDE_BIN" -p "$PROMPT" --permission-mode acceptEdits \
        --max-turns "$MAX_TURNS" 2>&1 | tee "$LOGDIR/run_$DATE.log")
fi

# ── 7. commit whatever the session left ──────────────────────────────────────────
git -C "$WT" add -A
git -C "$WT" -c user.name=robinhood_research -c user.email=research@local \
    commit -q -m "research $DATE" || true
CHANGED="$(git -C "$WT" diff --name-only origin/main...HEAD)"
if [ -z "$CHANGED" ]; then
    REASON="research: session produced no changes (nothing to merge)"; finish
fi

# ── 8. allowlist — the enforcement point (the Mac tree's copy, never the worktree's) ─
OFFENDERS="$(printf '%s\n' "$CHANGED" | "$PY" "$REPO/selfimprove/research/allowlist.py" 2>&1)"
ALLOW_RC=$?
NEW_CANDS="$(git -C "$WT" diff --name-only --diff-filter=A origin/main...HEAD \
    | grep -cE '^selfimprove/candidates/[a-z][a-z0-9_]*\.py$' || true)"
if [ "$ALLOW_RC" -ne 0 ]; then
    REASON="$(printf '%s' "$OFFENDERS" | tail -1)"
    log "$REASON — merging nothing; pushing the branch for the human"
    push_branch_for_human; finish
fi
if [ "${NEW_CANDS:-0}" -gt "$N" ]; then
    REASON="ALLOWLIST VIOLATION: $NEW_CANDS new candidate module(s) exceed the weekly budget of $N"
    log "$REASON — merging nothing; pushing the branch for the human"
    push_branch_for_human; finish
fi
log "allowlist ok: $(printf '%s\n' "$CHANGED" | grep -c .) path(s), $NEW_CANDS new candidate(s)"

# ── 9. register (validates in a subprocess, bumps trials; at most N) ─────────────
REG_OUT="$(cd "$WT" && "$PY" selfimprove/candidates/register.py --scan --max-new "$N" 2>&1)"
printf '%s\n' "$REG_OUT" | tee -a "$LOGDIR/run_$DATE.log"
REG_N="$(printf '%s\n' "$REG_OUT" | grep -c '^  registered ' || true)"
git -C "$WT" add -A
git -C "$WT" -c user.name=robinhood_research -c user.email=research@local \
    commit -q -m "research $DATE: register" || true

# ── 10. gate: verify.py green in the worktree, else the branch goes to a human ───
VERIFY_RC=1
if [ -f "$WT/verify.py" ]; then
    (cd "$WT" && "$PY" verify.py > "$LOGDIR/verify_$DATE.log" 2>&1); VERIFY_RC=$?
else
    echo "verify.py missing in the worktree — treated as FAILED" > "$LOGDIR/verify_$DATE.log"
fi
if [ "$VERIFY_RC" -ne 0 ]; then
    REASON="research: verify red (rc=$VERIFY_RC) — branch research/$DATE pushed for the human, nothing merged"
    log "$REASON"
    push_branch_for_human; finish
fi

git -C "$WT" checkout -q --detach origin/main
if ! git -C "$WT" merge -q --no-ff -m "research $DATE (verify green)" "research/$DATE"; then
    REASON="research: verify green but the merge onto origin/main failed — branch pushed for the human"
    git -C "$WT" merge --abort >/dev/null 2>&1 || true
    push_branch_for_human; finish
fi
SUMMARY_TAIL="$(printf '%s\n' "$CHANGED" | tr '\n' ' ')"
if [ "$DRY" = "1" ] && [ -z "${RESEARCH_ORIGIN:-}" ]; then
    REASON="DRYRUN: verify green; would have merged and pushed to origin/main: $SUMMARY_TAIL"
    log "$REASON (not pushed)"; finish
fi
PUSH_TO="origin"; [ "$DRY" = "1" ] && PUSH_TO="$RESEARCH_ORIGIN"
PUSHED=0
for attempt in 1 2 3; do
    if git -C "$WT" push -q "$PUSH_TO" HEAD:main 2>&1 | tail -1; then PUSHED=1; break; fi
    log "push attempt $attempt rejected; fetching and rebasing"
    # FETCH_HEAD == origin/main when PUSH_TO is origin; it is also right when PUSH_TO is the
    # RESEARCH_ORIGIN path of a dry run (a URL fetch never updates origin/main).
    git -C "$WT" fetch -q "$PUSH_TO" main && git -C "$WT" rebase -q FETCH_HEAD || true
done
if [ "$PUSHED" = "1" ]; then
    REASON="research merged (verify green; $NEW_CANDS new candidate file(s), ${REG_N:-0} registered): $SUMMARY_TAIL"
else
    REASON="research: verify green but the push to $PUSH_TO/main was rejected 3x — branch pushed for the human"
    push_branch_for_human
fi

# ── 11. always: the ONE weekly summary; exit 0 ───────────────────────────────────
finish
