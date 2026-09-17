#!/usr/bin/env bash
# robinhood_screener — the scan keeper. GitHub Actions ONLY (flock is util-linux; never run this
# on the Mac). One keeper run = a loop of `run.py --send` → `dashboard.py --write` → commit + push
# every KEEPER_CADENCE_S for up to KEEPER_MAX_S, then a handoff to its successor:
#
#   predecessor (slot a)                                  successor (slot b)
#   lead window: dispatches the successor  ──────────▶    starts; sees a keeper alive
#   keeps scanning; polls origin every 15 s     ◀──────   writes {"ready": <id>}, pushes, polls
#   sees ready: last commit, writes {"done"}  ──────────▶  sees done: pull --rebase, enters the loop
#   exits 0
#
# Every git write happens under flock on data/.keeper.lock (Phase 4 puts the paper book under the
# same lock) and follows ONE routine (commit_push): add data/ docs/ → commit → up to 5× (pull
# --rebase --autostash → push), a failed rebase aborted, never forced; three iterations without a
# successful push ⇒ exit 3 (the workflow's always() step uploads data/ as an artifact and the
# successor starts from a clean checkout). Every constant comes from config.py through one
# python3 call — never a literal here.
#
# Modes:
#   (no args)                 the keeper loop (env: GH_TOKEN, KEEPER_SLOT, TRIGGER, the run.py secrets)
#   --keeper-alive            exit 0 iff ANOTHER robinhood-screener run whose title starts with
#                             "keeper" is queued/in_progress/waiting/pending/requested; the current
#                             $GITHUB_RUN_ID is excluded (a keeper must be able to see "none alive")
#   --ensure-keeper [trigger] dispatch a keeper into slot a when --keeper-alive fails (default
#                             trigger "ensure"; the watchdog passes "watchdog")
#   --commit-push             the one flock commit+push routine, for the one-shot Persist step:
#                             exit 0 pushed, 10 nothing to commit, 1 push failed after retries
set -u

HANDOFF="data/keeper_handoff.json"
LOCK="data/.keeper.lock"
POLL_S=15
RUN_ID="${GITHUB_RUN_ID:-0}"
case "$RUN_ID" in ''|*[!0-9]*) RUN_ID=0 ;; esac
SLOT="${KEEPER_SLOT:-a}"
[ -n "${GITHUB_REPOSITORY:-}" ] && export GH_REPO="$GITHUB_REPOSITORY"

# loop state (globals: the trap and the pacing read them)
T0=0; READY_TS=0; STOP=0; REASON=""; DISPATCHED=0; SUNDAY_DONE=0
KEEPER_CADENCE_S=0; KEEPER_MAX_S=0; KEEPER_HANDOFF_LEAD_S=0; KEEPER_HANDOFF_WAIT_S=0; PAGES_EVERY_N=0

log() { echo "$(date -u +%FT%TZ) keeper $*"; }
now() { date +%s; }
elapsed() { echo $(( $(now) - T0 )); }

read_config() {  # ONE python call; the script never carries a cadence literal
  read -r KEEPER_CADENCE_S KEEPER_MAX_S KEEPER_HANDOFF_LEAD_S PAGES_EVERY_N KEEPER_HANDOFF_WAIT_S \
    < <(python3 -c "import config; print(config.KEEPER_CADENCE_S, config.KEEPER_MAX_S, config.KEEPER_HANDOFF_LEAD_S, config.PAGES_EVERY_N_ITERATIONS, config.KEEPER_HANDOFF_WAIT_S)") \
    || { log "config unreadable"; exit 1; }
}

git_identity() {
  git config user.name "robinhood-screener[bot]"
  git config user.email "bot@users.noreply.github.com"
}

# ── GitHub: who is alive, dispatching ─────────────────────────────────────────────
alive_keepers() {  # ids of the OTHER unfinished keeper runs. One request per status: the API
  local s          # filters a single status, and a 5.7 h keeper falls off an unfiltered first page
  for s in queued in_progress waiting pending requested; do
    gh run list -w robinhood-screener --status "$s" -L 50 --json databaseId,displayTitle \
      --jq ".[] | select(.displayTitle | startswith(\"keeper\")) | select(.databaseId != $RUN_ID) | .databaseId" 2>/dev/null
  done
}
keeper_alive() { [ -n "$(alive_keepers)" ]; }
running_keepers() {  # in_progress only: a merely QUEUED keeper can never write `done`, so it is
  gh run list -w robinhood-screener --status in_progress -L 50 --json databaseId,displayTitle \
    --jq ".[] | select(.displayTitle | startswith(\"keeper\")) | select(.databaseId != $RUN_ID) | .databaseId" 2>/dev/null
}                    # not a predecessor to wait on (await_predecessor), though it counts as alive

other_slot() { if [ "$SLOT" = a ]; then echo b; else echo a; fi; }

dispatch_keeper() {  # $1 = slot, $2 = trigger
  if gh workflow run robinhood-screener --ref main -f mode=keeper -f "trigger=$2" -f "slot=$1"; then
    log "dispatched keeper slot=$1 trigger=$2"
    return 0
  fi
  log "dispatch FAILED slot=$1 trigger=$2"
  return 1
}

ensure_keeper() {  # a keeper in slot a when none other is alive ($1 = trigger)
  keeper_alive && return 0
  dispatch_keeper a "${1:-ensure}"
}

in_lead_window() { [ "$(elapsed)" -ge $(( KEEPER_MAX_S - KEEPER_HANDOFF_LEAD_S )) ]; }

maybe_dispatch_successor() {  # idempotent: every lead-window iteration retries a lost dispatch
  in_lead_window || return 0
  keeper_alive && return 0
  dispatch_keeper "$(other_slot)" keeper && DISPATCHED=1
}

dispatch_pages() { gh workflow run robinhood-pages --ref main || log "pages dispatch failed (non-fatal)"; }

sunday_insurance() {  # once per run: Sunday >= 10:00 UTC and no robinhood-weekly run today ⇒
  [ "$SUNDAY_DONE" = 1 ] && return 0   # dispatch it. A no-op until weekly.yml exists (Phase 8).
  { [ "$(date -u +%u)" = 7 ] && [ $(( 10#$(date -u +%H) )) -ge 10 ]; } || return 0
  SUNDAY_DONE=1
  gh workflow view robinhood-weekly >/dev/null 2>&1 || { log "sunday insurance: robinhood-weekly absent; skipped"; return 0; }
  local n
  n=$(gh run list -w robinhood-weekly --created "$(date -u +%F)" --json databaseId --jq length 2>/dev/null || echo x)
  if [ "$n" = 0 ]; then
    gh workflow run robinhood-weekly --ref main && log "sunday insurance: dispatched robinhood-weekly"
  else
    log "sunday insurance: robinhood-weekly runs today=$n; nothing to do"
  fi
}

# ── git: ONE routine, under the lock ──────────────────────────────────────────────
with_lock() {  # run "$@" holding data/.keeper.lock (the lock file is gitignored)
  ( flock -w 120 9 || { log "lock timeout on $LOCK"; exit 1; }; "$@" ) 9>"$LOCK"
}

sync_main() {  # up to 5×: rebase our commits onto origin/main; a failed rebase is aborted
  local k
  for k in 1 2 3 4 5; do
    git pull --rebase --autostash -q origin main && return 0
    git rebase --abort 2>/dev/null; git merge --abort 2>/dev/null
    sleep 5
  done
  return 1
}

_commit_push() {  # under the lock. 0 pushed | 10 nothing to commit | 1 push failed
  local k
  git add data/ docs/
  git commit -q -m "screener state $(date -u +%FT%TZ)" || return 10
  for k in 1 2 3 4 5; do
    if git pull --rebase --autostash -q origin main && git push -q origin HEAD:main; then return 0; fi
    git rebase --abort 2>/dev/null; git merge --abort 2>/dev/null
    sleep 5
  done
  return 1
}
commit_push() { with_lock _commit_push; }

push_word() {  # $1 = commit_push rc, $2 = consecutive failures
  case "$1" in 0) echo ok ;; 10) echo none ;; *) echo "fail($2)" ;; esac
}

# ── the handoff marker: data/keeper_handoff.json — tiny, atomic, committed ────────
write_handoff() {  # $1 = ready|done, $2 = ts; tmp + mv (the *.tmp is gitignored, the mv is atomic)
  local tmp="$HANDOFF.$$.tmp"
  printf '{"%s": %s, "ts": %s}\n' "$1" "$RUN_ID" "$2" > "$tmp" && mv -f "$tmp" "$HANDOFF"
}
handoff_origin() { git fetch -q origin main 2>/dev/null && git show "FETCH_HEAD:$HANDOFF" 2>/dev/null; }
handoff_local() { cat "$HANDOFF" 2>/dev/null; }
handoff_marker() {  # stdin = the marker json; prints "<run id> <ts>" when key $1 is present
  python3 -c 'import json, sys
try:
    d = json.load(sys.stdin)
    k = sys.argv[1]
    if isinstance(d, dict) and k in d:
        print(int(d[k]), int(d.get("ts") or 0))
except Exception:
    pass' "$1"
}
successor_ready() {  # $1 = origin|local: a `ready` from ANOTHER run stamped at/after our start
  local m
  if [ "$1" = origin ]; then m=$(handoff_origin | handoff_marker ready); else m=$(handoff_local | handoff_marker ready); fi
  [ -n "$m" ] || return 1
  set -- $m
  [ "$1" != "$RUN_ID" ] && [ "$2" -ge "$T0" ]
}
predecessor_done() {  # a `done` from ANOTHER run stamped at/after our own `ready`
  local m
  m=$(handoff_origin | handoff_marker done)
  [ -n "$m" ] || return 1
  set -- $m
  [ "$1" != "$RUN_ID" ] && [ "$2" -ge "$READY_TS" ]
}

# ── pacing and signals ────────────────────────────────────────────────────────────
on_signal() { STOP=1; [ -n "$REASON" ] || REASON=stop; log "signal received: stopping after the current step"; }
nap() { sleep "$1" & wait $!; }   # interruptible: a trapped signal returns from `wait` at once

pace() {  # sleep until $1 + KEEPER_CADENCE_S in POLL_S slices (never a fixed sleep); in the lead
  local deadline=$(( $1 + KEEPER_CADENCE_S )) t slice   # window each slice also polls origin for `ready`
  while [ "$STOP" = 0 ]; do
    t=$(now)
    [ "$t" -ge "$deadline" ] && return 0
    if in_lead_window && successor_ready origin; then STOP=1; REASON=handoff; return 0; fi
    slice=$(( deadline - t )); [ "$slice" -gt "$POLL_S" ] && slice=$POLL_S
    nap "$slice"
  done
}

# ── the keeper ────────────────────────────────────────────────────────────────────
keeper_start() {
  gh auth status >/dev/null 2>&1 || { log "gh auth status FAILED (GH_TOKEN?)"; exit 1; }
  git_identity
  read_config
  trap on_signal TERM INT
  T0=$(now)
  log "START run_id=$RUN_ID slot=$SLOT cadence=${KEEPER_CADENCE_S}s max=${KEEPER_MAX_S}s lead=${KEEPER_HANDOFF_LEAD_S}s wait=${KEEPER_HANDOFF_WAIT_S}s pages_every=$PAGES_EVERY_N"
}

await_predecessor() {  # successor side: announce `ready`, wait for `done` (or the timeout), sync
  if [ -z "$(running_keepers)" ]; then log "no predecessor running; entering the loop"; return 0; fi
  READY_TS=$(now)
  write_handoff ready "$READY_TS"
  commit_push; log "ready marker push rc=$?"
  local deadline=$(( $(now) + KEEPER_HANDOFF_WAIT_S ))
  while [ "$STOP" = 0 ] && [ "$(now)" -lt "$deadline" ]; do
    if predecessor_done; then log "handoff: predecessor done; syncing"; sync_main; return 0; fi
    nap "$POLL_S"
  done
  log "handoff timeout — starting anyway"
  sync_main
}

main_loop() {  # returns 3 on persistent push failure, else 0
  local iter=0 start rc pushrc nopush=0 pushes=0
  while [ "$STOP" = 0 ] && [ $(( $(elapsed) + 300 )) -lt "$KEEPER_MAX_S" ]; do
    iter=$(( iter + 1 )); start=$(now)
    python3 run.py --send; rc=$?
    python3 dashboard.py --write || log "dashboard.py --write rc=$? (non-fatal)"
    commit_push; pushrc=$?
    case "$pushrc" in
      0)  nopush=0; pushes=$(( pushes + 1 ))
          [ $(( pushes % PAGES_EVERY_N )) -eq 0 ] && dispatch_pages ;;
      10) ;;                                          # nothing to commit is not a failure
      *)  nopush=$(( nopush + 1 )) ;;
    esac
    log "iter=$iter run_rc=$rc push=$(push_word "$pushrc" "$nopush") elapsed=$(elapsed)s wall=$(( $(now) - start ))s"
    if [ "$nopush" -ge 3 ]; then REASON=push-failures; return 3; fi
    sunday_insurance
    maybe_dispatch_successor
    if successor_ready local; then STOP=1; REASON=handoff; break; fi   # the rebase brought it in
    pace "$start"
  done
  return 0
}

finish() {  # $1 = main_loop rc. The final commit + push, the marker, the reason, the exit code
  if [ -z "$REASON" ]; then   # max-time: a late successor gets until KEEPER_MAX_S to announce itself
    while [ "$STOP" = 0 ] && [ "$(elapsed)" -lt "$KEEPER_MAX_S" ]; do
      if successor_ready origin; then REASON=handoff; break; fi
      nap "$POLL_S"
    done
    [ -n "$REASON" ] || REASON=max-time
  fi
  write_handoff done "$(now)"   # true on every exit; a waiting successor is released at once
  commit_push; log "final push rc=$?"
  log "EXIT reason=$REASON elapsed=$(elapsed)s dispatched_successor=$DISPATCHED"
  if [ "$1" = 3 ]; then
    keeper_alive || dispatch_keeper "$(other_slot)" keeper   # a clean checkout is the cure
    exit 3
  fi
  exit 0
}

keeper_main() {
  keeper_start
  await_predecessor
  main_loop; local rc=$?
  finish "$rc"
}

if [ "${KEEPER_LIB_ONLY:-0}" = 1 ]; then return 0 2>/dev/null; fi   # `KEEPER_LIB_ONLY=1 source` = functions only (tests)

case "${1:-}" in
  --keeper-alive)  keeper_alive ;;
  --ensure-keeper) ensure_keeper "${2:-ensure}" ;;
  --commit-push)   git_identity; commit_push ;;
  "")              keeper_main ;;
  *) echo "usage: keeper.sh [--keeper-alive | --ensure-keeper [trigger] | --commit-push]" >&2; exit 2 ;;
esac
