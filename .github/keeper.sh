#!/usr/bin/env bash
# robinhood_screener — the scan keeper. GitHub Actions ONLY (flock is util-linux; never run this
# on the Mac). One keeper run = a loop of `run.py --send` → `dashboard.py --write` → commit + push
# every KEEPER_CADENCE_S for up to KEEPER_MAX_S, then a handoff to its successor:
#
#   predecessor (slot a)                                  successor (slot b)
#   lead window: dispatches the successor  ──────────▶    starts; sees a keeper running
#   keeps scanning; polls origin every 15 s     ◀──────   writes {"ready": <id>}, pushes, polls
#   sees ready: last commit, pulls origin, ──────────▶    sees done — or no keeper running any
#   writes {"done"} on THAT base, exits 0                 more — pull --rebase, enters the loop
#
# Every git write happens under flock on data/.keeper.lock. The paper book (book_loop:
# selfimprove/livebook.py --tick every LIVEBOOK_TICK_INTERVAL_S, KEEPER_BOOK=1) takes the SAME
# flock(2) from Python (livebook._state_lock) around its state reads and its write phase only —
# never around a quote, so a 26–160 s tick never holds a commit up and a commit never interleaves
# with a write; its four state files ride the same commit. Every write follows ONE routine (commit_push):
# add data/ docs/ → commit → up to 5× (pull
# --rebase --autostash → push), a failed rebase aborted, never forced; three iterations without a
# successful push ⇒ exit 3 (the workflow's always() step uploads data/ as an artifact and the
# successor starts from a clean checkout). The handoff marker has its own routine (push_marker):
# origin is pulled FIRST and the marker written on that base — a marker committed on a stale
# base conflicts on its one line against every retry and never lands, and the successor would
# wait the whole KEEPER_HANDOFF_WAIT_S for nothing. Every constant comes from config.py through
# one python3 call — never a literal here.
#
# Every `gh run list` answer is three-valued — alive | none | unknown — and a FAILED call is
# unknown, never "none": the one-shot guard skips, nothing is dispatched, a successor keeps
# waiting. An API blip (5xx, 429) must never put two scans side by side or queue a redundant
# keeper; only a definite "none" acts (fail closed).
#
# Modes:
#   (no args)                 the keeper loop (env: GH_TOKEN, KEEPER_SLOT, TRIGGER, the run.py secrets)
#   --keeper-alive            exit 0 iff ANOTHER robinhood-screener run whose title starts with
#                             "keeper" is queued/in_progress/waiting/pending/requested (the current
#                             $GITHUB_RUN_ID excluded); 1 = definitely none; 2 = unknown (gh failed)
#   --ensure-keeper [trigger] dispatch a keeper into slot a when --keeper-alive is DEFINITELY none
#                             (default trigger "ensure"; the watchdog passes "watchdog"): exit 0
#                             alive or dispatched, 1 dispatch failed, 2 unknown (nothing dispatched)
#   --circuit-open            exit 0 iff >= KEEPER_CIRCUIT_FAILURES keeper runs concluded failure
#                             inside KEEPER_CIRCUIT_WINDOW_S — the breaker the watchdog and the
#                             exit-3 self-dispatch share; 1 = closed; 2 = unknown (gh failed)
#   --commit-push             the one flock commit+push routine, for the one-shot Persist step:
#                             exit 0 pushed, 10 nothing to commit, 1 push failed after retries
set -u -o pipefail

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
CIRCUIT_FAILURES=0; CIRCUIT_WINDOW_S=0
# the paper book loop: KEEPER_BOOK=1 (the workflow's default; 0 = the book stays off) ticks
# livebook.py every BOOK_TICK_S beside the scan; the tick fences its own reads and writes with
# the same lock from Python
KEEPER_BOOK="${KEEPER_BOOK:-1}"; BOOK_TICK_S=0; BOOK_STOP_WAIT_S=0; BOOK_PID=""; BOOK_STOP=""

log() { echo "$(date -u +%FT%TZ) keeper $*"; }
now() { date +%s; }
elapsed() { echo $(( $(now) - T0 )); }

read_config() {  # ONE python call; the script never carries a cadence, a breaker or a tick literal
  read -r KEEPER_CADENCE_S KEEPER_MAX_S KEEPER_HANDOFF_LEAD_S PAGES_EVERY_N KEEPER_HANDOFF_WAIT_S CIRCUIT_FAILURES CIRCUIT_WINDOW_S BOOK_TICK_S BOOK_STOP_WAIT_S \
    < <(python3 -c "import config; print(config.KEEPER_CADENCE_S, config.KEEPER_MAX_S, config.KEEPER_HANDOFF_LEAD_S, config.PAGES_EVERY_N_ITERATIONS, config.KEEPER_HANDOFF_WAIT_S, config.KEEPER_CIRCUIT_FAILURES, config.KEEPER_CIRCUIT_WINDOW_S, int(config.LIVEBOOK_TICK_INTERVAL_S), config.KEEPER_BOOK_STOP_WAIT_S)") \
    || { log "config unreadable"; exit 1; }
}

git_identity() {
  git config user.name "robinhood-screener[bot]"
  git config user.email "bot@users.noreply.github.com"
}

# ── GitHub: who is alive (three-valued), the breaker, dispatching ─────────────────
_keeper_state() {  # $@ = run statuses. Prints alive | none | unknown. One request per status: the
  local s out found=0 failed=0   # API filters a single status, and a 5.7 h keeper falls off an
  for s in "$@"; do              # unfiltered first page. alive = ANY other keeper run in one of
    if out=$(gh run list -w robinhood-screener --status "$s" -L 50 --json databaseId,displayTitle \
               --jq ".[] | select(.displayTitle | startswith(\"keeper\")) | select(.databaseId != $RUN_ID) | .databaseId" 2>/dev/null); then
      [ -n "$out" ] && found=1   # them (a positive finding beats a failed sibling query); none
    else                         # only when EVERY query answered; unknown when one failed —
      failed=1                   # the callers fail closed on unknown
    fi
  done
  if [ "$found" = 1 ]; then echo alive; elif [ "$failed" = 1 ]; then echo unknown; else echo none; fi
}
keeper_state()  { _keeper_state queued in_progress waiting pending requested; }
running_state() { _keeper_state in_progress; }   # a merely QUEUED keeper counts as alive (never
                                                 # dispatch beside it) but can never write `done`,
                                                 # so a successor waits only on a RUNNING one
keeper_alive() {  # the CLI contract: 0 alive | 1 definitely none | 2 unknown
  case "$(keeper_state)" in alive) return 0 ;; none) return 1 ;; *) return 2 ;; esac
}

circuit_open() {  # 0 open (>= CIRCUIT_FAILURES keeper runs concluded failure inside CIRCUIT_WINDOW_S)
  local since n   # | 1 closed | 2 unknown. The ONE breaker: the watchdog's restart and the exit-3
                  # self-dispatch both consult it, so a broken keeper cannot loop forever
  since=$(python3 -c "import time; print(time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(time.time() - $CIRCUIT_WINDOW_S)))") \
    || { log "circuit: since unreadable (unknown)"; return 2; }
  n=$(gh run list -w robinhood-screener --status failure --created ">=$since" -L 50 --json displayTitle \
        --jq '[.[] | select(.displayTitle | startswith("keeper"))] | length' 2>/dev/null) \
    || { log "circuit: gh failed (unknown)"; return 2; }
  case "$n" in ''|*[!0-9]*) log "circuit: unparsable count '$n' (unknown)"; return 2 ;; esac
  log "circuit: keeper failures since $since = $n (open at $CIRCUIT_FAILURES)"
  [ "$n" -ge "$CIRCUIT_FAILURES" ]
}

other_slot() { if [ "$SLOT" = a ]; then echo b; else echo a; fi; }

dispatch_keeper() {  # $1 = slot, $2 = trigger
  if gh workflow run robinhood-screener --ref main -f mode=keeper -f "trigger=$2" -f "slot=$1"; then
    log "dispatched keeper slot=$1 trigger=$2"
    return 0
  fi
  log "dispatch FAILED slot=$1 trigger=$2"
  return 1
}

ensure_keeper() {  # a keeper in slot a only when NONE is definitely alive ($1 = trigger):
  case "$(keeper_state)" in   # 0 alive or dispatched | 1 dispatch failed | 2 unknown, nothing dispatched
    alive)   return 0 ;;
    unknown) log "keeper state unknown (gh failed); nothing dispatched"; return 2 ;;
  esac
  dispatch_keeper a "${1:-ensure}"
}

in_lead_window() { [ "$(elapsed)" -ge $(( KEEPER_MAX_S - KEEPER_HANDOFF_LEAD_S )) ]; }

maybe_dispatch_successor() {  # idempotent: every lead-window iteration retries a lost dispatch;
  local st                    # an unknown state dispatches nothing and is retried next iteration
  in_lead_window || return 0
  st=$(keeper_state)
  [ "$st" = unknown ] && log "successor: keeper state unknown (gh failed); dispatch deferred"
  [ "$st" = none ] || return 0
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

marker_only_local_commit() {  # exactly one commit ahead of origin/main, and it touches only the marker
  [ "$(git rev-list --count origin/main..HEAD 2>/dev/null)" = 1 ] \
    && [ "$(git show --format= --name-only HEAD 2>/dev/null)" = "$HANDOFF" ]
}

_push_marker() {  # under the lock. $1 = ready|done, $2 = ts. 0 = on origin | 1 = not delivered
  local k           # (logged, never fatal: a successor also breaks its wait when no keeper runs)
  for k in 1 2 3 4 5; do
    # origin FIRST. The successor's `ready` was seen through FETCH_HEAD and is not in this tree:
    # a `done` committed on the stale line conflicts against origin on every retry and never
    # lands. Pulled first, the marker is written on the current line and replays cleanly.
    if ! git pull --rebase --autostash -q origin main; then
      git rebase --abort 2>/dev/null; git merge --abort 2>/dev/null
      # a lost push race (origin moved between our pull and our push): re-sync and re-apply the
      # marker rather than replay the same conflicting commit. Only when the marker commit is the
      # ONLY local commit can the one conflicting line be the marker's, and ours — the newer state
      # — wins. Scan commits stuck behind a data conflict are never resolved blindly: the marker
      # stays undelivered and the successor's dead-predecessor break covers the handoff.
      marker_only_local_commit || { log "marker $1: local commits conflict with origin; not delivered"; return 1; }
      git pull --rebase --autostash -X theirs -q origin main \
        || { git rebase --abort 2>/dev/null; git merge --abort 2>/dev/null; sleep 5; continue; }
    fi
    write_handoff "$1" "$2"
    git add "$HANDOFF"
    git commit -q -m "keeper $1 $(date -u +%FT%TZ)" >/dev/null 2>&1 || true   # unchanged ⇒ already committed on this base
    git push -q origin HEAD:main && return 0
    sleep 5
  done
  return 1
}
push_marker() { with_lock _push_marker "$@"; }

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

# ── the paper book: livebook.py --tick beside the scan; the Python side holds the lock ─
# KEEPER_BOOK=1 runs a background loop of one tick per BOOK_TICK_S. The tick is NOT run under
# with_lock: livebook._state_lock takes flock(2) on the same data/.keeper.lock (compatible with
# util-linux flock(1) — both are flock(2)) around the tick's state reads and its write phase
# only, and every quote runs outside it. A whole tick under the lock measured p50 26 s / max
# 161 s on the Mac book (45 Kyber-quoted positions at 1 Hz): commit_push would wait behind it
# and with_lock's 120 s timeout would trip. What the lock still guarantees: a snapshot never
# captures livebook.json from tick N beside fills from tick N+1 (the 199-fills class) — the
# write phase is one hold and commit_push holds the same file — and no read of the book or the
# ledger lands in `git pull --rebase`'s transient rewrite of the checkout. The tick itself
# never sleeps; the loop naps between ticks. It starts only AFTER await_predecessor (a
# successor never ticks before the predecessor's `done`, or its absence) and is stopped — the
# in-flight tick waited for — BEFORE finish's final commit, so the predecessor's last tick is
# in its final snapshot and the successor's first tick follows it by about one push/poll cycle
# (the first tick logs the gap it actually saw). Four state files ride `git add data/`;
# data/livebook_ticks.jsonl stays gitignored — the run's artifact carries it. The feed reads
# THIS checkout (LIVEBOOK_FEED_SOURCE=worktree): no git in a tick.
book_last_tick_ts() {  # the restored snapshot's newest last_tick_ts (integer s); empty when no book
  python3 -c 'import json
try:
    b = json.load(open("data/livebook.json"))
    ts = [float(p.get("last_tick_ts") or 0) for p in b.values() if isinstance(p, dict)]
    if ts:
        print(int(max(ts)))
except Exception:
    pass' 2>/dev/null
}

book_loop() {  # the background loop; TERM ⇒ finish the in-flight tick, then leave
  trap 'BOOK_STOP=1' TERM
  local n=0 t rc last s
  last=$(book_last_tick_ts)
  while [ -z "$BOOK_STOP" ]; do
    n=$(( n + 1 )); t=$(now)
    if [ "$n" = 1 ]; then
      if [ -n "$last" ]; then log "book first tick: gap since snapshot's last_tick_ts = $(( t - last )) s"
      else log "book first tick: no snapshot (empty book)"; fi
    fi
    python3 -u selfimprove/livebook.py --tick; rc=$?   # the lock is taken inside, around the writes
    if [ "$rc" != 0 ] || [ $(( n % 10 )) = 0 ]; then log "book tick n=$n rc=$rc wall=$(( $(now) - t ))s"; fi
    [ -z "$BOOK_STOP" ] || break
    s=$(( t + BOOK_TICK_S - $(now) ))
    [ "$s" -gt 0 ] && nap "$s"
  done
  log "book loop exit after $n tick(s)"
}

book_start() {  # after await_predecessor; KEEPER_BOOK=0 leaves the book off
  if [ "$KEEPER_BOOK" != 1 ]; then log "book loop disabled (KEEPER_BOOK=$KEEPER_BOOK)"; return 0; fi
  # never before the seed: with the .gitignore flip merged but the Mac snapshot not yet published,
  # a fresh book here would collide with the seed on livebook.json at the next commit_push
  # (five aborted rebases ⇒ exit 3 and ~12 min of unpushed scan state). Tracked ⇒ seeded.
  if ! git ls-files --error-unmatch data/livebook.json >/dev/null 2>&1; then
    log "book loop disabled: data/livebook.json is not tracked in this checkout — seed the cloud book first (docs/DESIGN.md, the cutover row)"
    return 0
  fi
  export LIVEBOOK_FEED_SOURCE=worktree      # the tick reads THIS checkout's ledger + sidecar, never git
  book_loop &
  BOOK_PID=$!
  log "book loop started pid=$BOOK_PID tick=${BOOK_TICK_S}s feed=worktree"
}

book_stop() {  # before finish's final commit: TERM the loop, wait (bounded) for the in-flight tick
  [ -n "$BOOK_PID" ] || return 0
  kill -TERM "$BOOK_PID" 2>/dev/null
  local i=0
  while kill -0 "$BOOK_PID" 2>/dev/null && [ "$i" -lt "$BOOK_STOP_WAIT_S" ]; do sleep 1; i=$(( i + 1 )); done
  if kill -0 "$BOOK_PID" 2>/dev/null; then
    log "book loop still running after ${BOOK_STOP_WAIT_S}s — not waited for further (its write phase is under the lock; the commit cannot interleave)"
  else
    wait "$BOOK_PID" 2>/dev/null
    log "book loop stopped (${i}s)"
  fi
  BOOK_PID=""
}

# ── the keeper ────────────────────────────────────────────────────────────────────
keeper_start() {
  gh auth status >/dev/null 2>&1 || { log "gh auth status FAILED (GH_TOKEN?)"; exit 1; }
  git_identity
  read_config
  trap on_signal TERM INT
  T0=$(now)
  log "START run_id=$RUN_ID slot=$SLOT cadence=${KEEPER_CADENCE_S}s max=${KEEPER_MAX_S}s lead=${KEEPER_HANDOFF_LEAD_S}s wait=${KEEPER_HANDOFF_WAIT_S}s pages_every=$PAGES_EVERY_N circuit=${CIRCUIT_FAILURES}/${CIRCUIT_WINDOW_S}s book=$KEEPER_BOOK/${BOOK_TICK_S}s"
}

await_predecessor() {  # successor side: announce `ready`, wait for `done` — or for the predecessor
  local st             # to be gone — then sync. Unknown (gh failed) is treated as running: we wait.
  st=$(running_state)
  if [ "$st" = none ]; then log "no predecessor running; entering the loop"; return 0; fi
  [ "$st" = unknown ] && log "predecessor state unknown (gh failed); waiting as if one were running"
  READY_TS=$(now)
  push_marker ready "$READY_TS"; log "ready marker push rc=$?"
  local deadline=$(( $(now) + KEEPER_HANDOFF_WAIT_S ))
  while [ "$STOP" = 0 ] && [ "$(now)" -lt "$deadline" ]; do
    if predecessor_done; then log "handoff: predecessor done; syncing"; sync_main; return 0; fi
    # a predecessor that died without `done` (cancelled, timed out, a lost runner) must not idle
    # us for the whole wait: break only on a DEFINITE none — unknown keeps waiting (fail closed)
    if [ "$(running_state)" = none ]; then log "handoff: predecessor gone (no keeper running); syncing"; sync_main; return 0; fi
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
  local c
  if [ -z "$REASON" ]; then   # max-time: a late successor gets until KEEPER_MAX_S to announce itself
    while [ "$STOP" = 0 ] && [ "$(elapsed)" -lt "$KEEPER_MAX_S" ]; do
      if successor_ready origin; then REASON=handoff; break; fi
      nap "$POLL_S"
    done
    [ -n "$REASON" ] || REASON=max-time
  fi
  book_stop                                                 # the book's last tick rides the final snapshot
  commit_push; log "final push rc=$?"                       # straggler state first (usually 10: none)
  push_marker done "$(now)"; log "done marker rc=$?"        # true on every exit, written on origin's base:
  log "EXIT reason=$REASON elapsed=$(elapsed)s dispatched_successor=$DISPATCHED"   # a waiting successor is released at once
  if [ "$1" = 3 ]; then
    # the watchdog's breaker, applied here too: a persistent push failure must not loop keeper →
    # exit 3 → dispatch → … forever. Open or unknown ⇒ the watchdog owns the restart.
    circuit_open; c=$?
    if [ "$c" != 1 ]; then
      log "self-dispatch skipped: circuit $([ "$c" = 0 ] && echo open || echo unknown) — the watchdog owns restarts"
    else
      case "$(keeper_state)" in
        none)  dispatch_keeper "$(other_slot)" keeper ;;   # a clean checkout is the cure
        alive) log "self-dispatch skipped: a keeper is alive" ;;
        *)     log "self-dispatch skipped: keeper state unknown (gh failed) — the watchdog owns restarts" ;;
      esac
    fi
    exit 3
  fi
  exit 0
}

keeper_main() {
  keeper_start
  await_predecessor
  book_start                                # never before the predecessor's `done` (or its absence)
  main_loop; local rc=$?
  finish "$rc"
}

if [ "${KEEPER_LIB_ONLY:-0}" = 1 ]; then return 0 2>/dev/null; fi   # `KEEPER_LIB_ONLY=1 source` = functions only (tests)

case "${1:-}" in
  --keeper-alive)  keeper_alive ;;
  --ensure-keeper) ensure_keeper "${2:-ensure}" ;;
  --circuit-open)  read_config; circuit_open ;;
  --commit-push)   git_identity; commit_push ;;
  "")              keeper_main ;;
  *) echo "usage: keeper.sh [--keeper-alive | --ensure-keeper [trigger] | --circuit-open | --commit-push]" >&2; exit 64 ;;
esac
