# Go-live checklist — what has to be true before a paper result is allowed to mean anything

This file exists because the interesting decision is not "does the screener run". It is: **may a
paper scorecard be read as evidence about a band?** Items 1–6 say the apparatus is honest; item 7
is the one pre-registered judge; items 8–11 are the decision itself, which is the operator's and
is deliberately hard to reach.

Each item is one command and one binary pass condition. Nothing here writes anything, except the
two items that say so (3 and 10) — and those are the human's to run, never an assistant's.
Run everything from the repo root of a tree that is up to date with `origin/main`.

---

### 1. The keeper is alive and scanning on its grid

```bash
gh run list -w robinhood-screener -s in_progress
python3 -c "import json,statistics as st; t=sorted(json.loads(l)['scan_ts'] for l in open('data/run_log.jsonl'))[-720:]; g=[b-a for a,b in zip(t,t[1:])]; print('scans',len(t),'median',round(st.median(g),1),'s  max',round(max(g),1),'s  over',round((t[-1]-t[0])/3600,1),'h')"
```

**Pass:** exactly one in-progress run (titled `keeper · <trigger> · slot=a|b`; two only during a
handoff), and over the last ~720 scans the **median gap is ≤ 300 s** with **no gap above
`KEEPER_STALE_S` = 1800 s**. Do not require every gap at 240 s: the gap is the cadence plus the
run, and a slow scan stretches it — measured 240.03 s median over the first 596 scans, with 16
gaps above 260 s (max 591 s) all attributable to slow runs, none to a handoff.

### 2. The watchdog is green

```bash
gh run list -w robinhood-keeper-watchdog -L 50
```

**Pass:** every listed run `completed success`, and **no SCAN STALE alert has arrived in 48 h**.
A SCAN STALE means the keeper died and the restart is the only reason scanning resumed; a run
that is not `success` means the restart authority itself is broken, which is worse.

### 3. The cloud has its secrets and no source is dark

```bash
python3 cloud_secrets.py --check          # THE HUMAN runs this; values never touch argv
python3 -c "import json; print(json.loads(open('data/run_log.jsonl').read().strip().splitlines()[-1])['dark'])"
```

**Pass:** all four names — `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `NTFY_TOPIC`,
`GMGN_API_KEY` — are listed, **and** the newest run-log line prints `[]`. A non-empty `dark` list
is not a failure of the scan (a dark source passes through and is named), but it does mean the
run's features were thinner than the ones a band was designed against. `python3 preflight.py`
probes every source from wherever it runs if you need to know which one is unhappy.

### 4. The Mac tree is clean and equal to `origin/main`

```bash
git status -sb
```

**Pass:** the first line is exactly `## main...origin/main` with **nothing** after it and no
further lines. `[ahead N]` or an uncommitted edit means the Sunday research session's ff-merge
will refuse, and it means a local file is about to be read as if it were the record.

### 5. The cloud's own numbers are real, not a collapsed grid

```bash
python3 -c "import csv,json,collections; b='band_volume_early'; v=[x for x in csv.DictReader(open('data/band_verdicts.csv')) if x['band']==b]; d=json.load(open('data/latest_scan.json')); L=[json.loads(l) for l in open('data/run_log.jsonl')][-100:]; print(b,collections.Counter(x['verdict'] for x in v),'| cursor_lag',d['cursor_lag_blocks'],'catchup',d['catchup'],'| runs with no time-budget cut',sum(1 for x in L if not x.get('deferred')),'/',len(L),'| recheck',len(json.load(open('data/recheck.json'))))"
```

**Pass** (substitute the band you care about): the band's verdicts are **not all `NA`**,
`cursor_lag` is under 5,000 blocks with `catchup` false, **≥ 90 of the last 100 runs took no
time-budget cut**, and the recheck queue is under `RECHECK_MAX` = 3000. A queue sitting **at**
`RECHECK_MAX` is being evicted unlooked-at, which silently changes what the band ever sees.
`latest_scan.json.deferred_by_stage` is a different quantity: `pass2_overflow` /
`pass2_rescheduled` are the overflow ladder working (a survivor past the pass-2 budget returns at
its recheck pace instead of looping), not a cut.

### 6. The live book is actually feeding the band under test

```bash
python3 selfimprove/livebook.py --scorecard
```

**Pass:** the header names the band under test with an **`n` that grows week over week**, the
**entry-lag median is ≤ 240 s**, and the `band_under_test_full` refusals stay under the 5 %
tolerance the paper gate applies (`PAPER_GATE_MAX_REFUSED_SHARE`). Today that header reads
`band under test: none`, which is the correct reading of an unopened window — and it is why the
paper gate says "none registered" rather than "pass".

### 7. The 7-day paper gate — the ONE pre-registered judge

```bash
python3 selfimprove/improve.py --band-scorecard
```

**Pass:** it prints **PASS** with the window **closed** (`now >= start + 7 d + LIVEBOOK_MAX_TRACK_S`
and no eligible position still open), `n >= PAPER_GATE_MIN_FILLS` = 20 fills, the policy's own
`net_lb > 0`, **both exit controls at `net_lb <= 0`**, neither control inert, and the gapped and
refused shares inside their bounds — **and** the matching `paper_verdict:<band>/<policy>@<start>=pass`
line is on `origin/main` in `selfimprove/trials.json`. Only the `--apply` path (`weekly.yml`'s
Gates step) mints that line; a Mac run prints the would-be line marked "(not recorded: dry)" and
writes nothing, on purpose — a permanent one-shot record must not exist in two versions. Read the
`n_days` on the verdict line: below `MIN_BOOTSTRAP_CLUSTERS` = 12 alert-days it says, in those
words, **"a number, not a bound (floor 12)"**, and that is what it is. **A verdict is not a
promotion**: neither `champion.json` nor `registry.json` nor either Sunday gate reads it.

### 8. "Repeatedly positive" — the operator's own judgment, over more than one window

```bash
python3 -c "import json; print([t for t in json.load(open('selfimprove/trials.json')).get('nominations_ever',[]) if t.startswith('paper')])"
```

**Pass:** at least **two consecutive 7-day windows** have closed with a PASS. Each window mints one
permanent line — `paper:<band>/<policy>@<start>` — in `trials.json`'s **`nominations_ever`**
(`improve.paper_gate` → `trials.bump("nominations", …)`, on the `--apply` path only). That line
deflates **no** Sharpe gate: the exit DSR counts `policies_ever_scored`, the entry DSR counts
`bands_ever_scored`, and the paper gate's own DSR counts `policies_ever_scored` too. What deflates
a later gate is **registration** (`register.py --scan`), not opening a window. What a window does
cost is the one-shot: a **third** window on one (band, policy) pair is refused unless its start is
at least `PAPER_GATE_WINDOW_DAYS + BAND_RENOMINATE_COOLDOWN_DAYS` = 7 + 90 = **97 days** after the
latest prior window's start — so this is a small, expensive number of attempts, not a search. Two
is a recommendation, not a threshold the code enforces: one 7-day window carries at most 7
alert-days, and this repo's own floor for calling a bound a bound is **12**. Two windows is the cheapest way to clear that floor while keeping each
verdict one-shot.

### 9. The honest prior has not changed

```bash
python3 -c "import ledger; ledger.summary()"
```

**Pass:** you can state, out loud, that a PASS on one 7-day window is **consistent with no edge**.
The base rate for this class of trade is negative expectancy; the gate is calibrated to refuse,
and "NO CHANGE for months" is the expected outcome, not a malfunction. If the A-vs-B scorecard
does not clearly separate, the band is not adding signal and the paper gate's verdict is about
the exit policy's behaviour, not about the band's edge.

### 10. Only then, and only by hand: set the champion

```bash
python3 selfimprove/champion.py --set entry_band=<band> --reason "<why, in one sentence>" --publish
```

**Pass:** `selfimprove/champion.json` on `origin/main` names the band, and the next scan's
`latest_scan.json.champion.entry_band` agrees. `--set exit=<policy>` is the same move on the other
arm and refuses anything that is not an executable registered policy. What this does and does not
change is written out in `docs/DESIGN.md` ("What the champion decision changes"). Two things it
does **not** do. It never flips `candidates/registry.json` — that status moves only on a gate
promotion. And it does not arm the Sunday demotion test: `champion.py` writes
`promoted_at_event_seq: null` on every manual set, and the entry gate builds its one-shot demotion
block only when that field is non-null, which only a gate promotion ever writes. **There is no
automatic revert behind this command.** A hand-set band stands until you revert it by hand with
the same command (`--set entry_band=band_a_strict`), so treat item 9's honest prior as the only
thing standing between a manual set and a band that alerts for months on one window's evidence.
The band the gate would revert *to* is still `DEFAULT_ENTRY_BAND`, on a non-positive forward lift
and with no positive proof required — but only after a **gate** promotion.

Before this command the band must already be registered —
`python3 selfimprove/candidates/register.py --scan`, itself a permanent counted trial that
deflates every later DSR in its family. `--set entry_band=` validates no name at all, so an
unregistered band leaves `champion.json` naming it while every scan silently falls back to
`DEFAULT_ENTRY_BAND` — one printed line, no alert. The exit arm is the strict one: rc 2.

### 11. Real money is a different plan and a different program

```bash
grep -n "never touches keys or funds" README.md
```

**Pass:** README's one-line promise still matches the code — nothing here holds a key, signs a
transaction, or moves a balance, and no item above changes that. Automating real trades is a
separate plan with a separate executor, a separate risk budget and its own kill conditions. A
green checklist authorizes exactly one thing: reading the paper scorecard as evidence.

---

## Sizing, from this book's own fills

The −50 % stop is a **trigger, not a cap**. On the committed live book, `stop_50`'s own stop fills
realized a median of **−73 %**, about **one in eight at or below −99 %** (27 of 222), and **61 of
its 397 completed positions closed at $0 with no route** at all — measured **2026-09-19** over 417
positions and 8,089 fill rows. Size every position so that a 100 % loss is acceptable. And read
those numbers as a floor on the pain, not a forecast of it: real fills at your size into the
$25–50k pools this screener finds will be **worse** than the $10 paper quotes they came from, which
already embed quoted slippage but not gas and not the impact of a larger order.

Reproduce it:

```bash
python3 -c "import csv,json,statistics as st; b=json.load(open('data/livebook.json')); c={(p['token'],int(p['event_seq'])):float(p.get('cost_usd') or 0) for p in b.values()}; r=sorted(float(x['usd_proceeds'])/c[(x['token'],int(x['event_seq']))]-1 for x in csv.DictReader(open('data/livebook_fills.csv')) if x['policy']=='stop_50' and x['side']=='stop' and c.get((x['token'],int(x['event_seq'])))); n=sum(1 for p in b.values() if (p.get('policies') or {}).get('stop_50',{}).get('closed')); z=sum(1 for p in b.values() if (p.get('policies') or {}).get('stop_50',{}).get('close_reason')=='no_route'); print('stop fills',len(r),'median',round(st.median(r)*100,1),'% | <=-99%:',sum(1 for x in r if x<=-0.99),'| $0 no route',z,'of',n,'closed')"
```
