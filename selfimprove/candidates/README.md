# Candidates — the research pool

**A candidate is a hypothesis and a trial.** A band (`kind: band`) is a pure function
`verdict(feat) -> True | False | None` over the flat feature dict `config.FEATURE_FIELDS`; a
policy (`kind: policy`) is an exit plan `{ladder?, stop?, trail?, trail_arm?, flow?,
max_hold_s?}` — `selfimprove/policies.py:_POLICY_KEYS` is the authority, `validate_policy` the
shape check. `trail_arm` is the multiple of entry the high-water mark must reach before `trail`
is live at all (a number > 1; it needs a `trail` to arm), and `flow` is the post-arm 5-minute
flow rule (a dict with exactly `policies.FLOW_KEYS`; it needs `trail_arm`) that only the **live
book** can run — `policies.simulate` has no 5-minute feed and scores a flow policy NaN. Every
registration writes the name into `selfimprove/trials.json`, which only grows — you cannot
un-look at a result — and the count deflates every later Deflated-Sharpe gate for the whole
family (`bands_ever_scored` for entry, `policies_ever_scored` for exit). A candidate therefore
has to be worth paying for in statistical power. `RESEARCH_MAX_REGISTERED` caps the pool at 40
and `RESEARCH_MAX_NEW_CANDIDATES_PER_WEEK` caps new registrations at 2. The two controls
(`ctl_random_band`, `ctl_inverse_band`) are permanent, never count as trials and must fail
every gate; if one passes, the run is void and the apparatus is at fault.

**What a module may import.** Only `math`, `json`, `config`, `clf_runtime`, `selfimprove`,
`typing`, `__future__` and `hashlib`. `bands.static_ok()` walks the AST before any import and
rejects everything else, plus any attribute chain touching `os.environ`, `time`, `datetime`,
`random`, `numpy.random`, `subprocess`, `urllib`, `http_client`, `requests`, `socket` or
`pathlib`, and any call to `open()`, `__import__()`, `exec()` or `eval()`. A band is a function
of `feat` and nothing else: no clock (`sighting_age_s` is the only age it may read), no RNG, no
network, no files. A module that fails the check is skipped with a printed warning and its
verdicts are NA; a module that is not in `registry.json` is never imported at all; a `verdict`
that raises is NA. A candidate bug can never break alerting. `_template.py` is the exact shape.

**Registration.** `python3 selfimprove/candidates/register.py --scan [--max-new N]` finds
modules under this directory that are not yet in `registry.json`, validates each in a
subprocess (`python3 -I -S`, repo root only on `sys.path`: static check; `NAME` equals the file
name; `KIND`, `RATIONALE`, `CONSUMED_DATA` declared; for bands `REQUIRES ⊆ FEATURE_FIELDS`,
deterministic on 50 fixture dicts, NA when any `REQUIRES` field is None, and not inert against
the champion on the fixtures; for policies `policies.validate_policy` is clean), refuses more
than N per run, appends `{name, kind, module, status: candidate, registered_ts,
registered_at_event_seq, nominated_at_event_seq: null, rationale, requires, proposal}` where
`registered_at_event_seq` is the highest `event_seq` in `origin/main:data/ledger.csv` at that
moment, and bumps the family's trial list. `--budget-remaining` prints how many registrations
this week may still make.

**Nomination.** A registered band is scored every Sunday by `entry_lab/improve_bands.py`
against the same-day, same-age-bucket unselected pool and the champion, net of the modelled
round-trip cost, with day-clustered lower bounds, a within-day shuffle calibration and the
cumulative-trial DSR. A candidate that clears every in-sample check is *nominated*, not
promoted: `nominated_at_event_seq` is stamped with the ledger's current `max(event_seq)` and the
nomination itself is counted in `trials.json` (`nominations_ever`). Nomination is sticky, one
nominee per arm, with a `BAND_RENOMINATE_COOLDOWN_DAYS` cooldown after a failure.

**Forward-only judgment.** Only rows with `event_seq > nominated_at_event_seq` — events the
nominee had never seen when it was chosen — decide. The forward prefix is the earliest matured
events in sequence order, all rows included, cut when the nominee has selected
`BAND_FWD_MIN_SELECTED` rows across `BAND_FWD_MIN_DAYS` days. It is judged once: pass and the
nominee becomes champion (with a one-shot demotion test on its own forward prefix afterwards);
fail and it is marked `failed_nominee` and cannot be renominated until the cooldown elapses.
Its in-sample scorecard can never promote it, however good it looks.

**What the research job can never edit.** The weekly headless session works on a throwaway
branch inside a detached temporary worktree and its diff is allowlisted to
`selfimprove/candidates/<name>.py` and `selfimprove/research/proposals/PROPOSAL_<date>.md`.
It may not touch `config.py`, `screen.py`, `run.py`, `alerts.py`, `ledger.py`, `verify.py`,
`selfimprove/champion.json`, `selfimprove/trials.json`, this directory's `registry.json`,
`register.py`, `_template.py`, anything under `data/`, or anything that sends. It never
weakens a hard gate and never promotes anything; `register.py --scan` and `verify.py` decide
whether its branch merges, and the promotion gate alone decides whether a candidate ever runs.
