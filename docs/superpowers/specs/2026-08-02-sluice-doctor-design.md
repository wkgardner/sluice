# `sluice doctor` — design

**Date:** 2026-08-02 (amended 2026-08-03 — §8a, §11)
**Status:** approved (brainstormed and grilled with Will, 2026-08-02)
**Resolves:** errata **E2** (ambient, unhealth-checked credentials)
**Depends on:** plan 15 increment 8 (`sluice setup` + the config schema)

---

## 1. The problem

`sluice setup` proves a config is **complete** — every key the code reads is present.
Nothing proves it **works**. Errata E2 names the gap: an expired credential, a missing
`nvd` CLI, a missing Slack token, or unreadable CephFS xattrs are all invisible until
a run hits the gate that needs them.

This matters now because of the deadline. Will hands sluice to a colleague at the end
of the week of 2026-08-02 and is then away. The failure class doctor addresses —
"the config is fine, the world isn't" — is exactly what surfaces on deployment day
with nobody around who knows the system.

## 2. What doctor is not

- **It never mutates.** It reports the NVD repo's git ref; it never checks it out
  (plan 15 §10). It creates no directory, writes no config, touches no run state.
  ADR-0005's posture applied to diagnosis: doctor tells you, you decide.
- **It is not a gate.** Considered and rejected during design. A gate is per-*run*
  work that quarantines *one run* on failure (ADR-0002). A revoked LabKey key is not
  wrong with run 47, it is wrong with the installation; as a gate it would quarantine
  run after run — the exact slow bleed the startup gate (§6) exists to prevent — and
  it has no honest `satisfied_column`, because a run does not "complete preflight".
- **It does not re-check while running.** See §9.

## 3. Decisions

| # | Decision | Reasoning |
|---|---|---|
| D1 | **Local checks by default; network behind `--comprehensive`.** | Doctor must be safe to run reflexively. Real round-trips touch production services and take seconds, so they are opt-in. |
| D2 | **Checks live in doctor's own map, keyed by gate id, filtered by the active profile.** No change to the eleven-field `Gate` contract. | A twelfth `preflight` field was proposed and rejected: it would put world-probing into a contract that has stayed pure data, and needs an ADR. The drift guard (§7) supplies the coupling instead. |
| D3 | **`sluice daemon` runs the local checks at startup and refuses to start on any FAIL.** | Starting with a broken late gate downloads several multi-TB runs that pile up unprocessed, stacking a disk-full problem on the original one and firing the B5 disk-floor auto-pause. Refusing leaves the data safely on the sequencer. |
| D4 | **A hidden bypass flag exists**, absent from `--help` and **not named in the failure message**. | Preflight code is new; a false positive that bricks the pipeline for a week is worse than the bleed it prevents. Hiding it keeps it a deliberate act rather than the obvious next keystroke, so it cannot silently become the default in a systemd unit. |
| D5 | **Verifiability is classified in the config schema** via a new `ProbeKind` on `ConfigField`, **with no default**. | See §5. No default is what delivers the no-decoupling property: a defaulted field forgets by construction. |
| D6 | **Severity is derived from `FieldKind`, never hand-assigned.** | `MANAGED_PATH` missing → WARN (setup creates those; sluice can recreate). `EXTERNAL_PATH` / `SECRET_FILE` missing → FAIL (only a human can supply it). Derived rules cannot drift the way a hand-maintained severity column would. |
| D7 | **Comprehensive probes reuse the real adapters** — `make_transfer_client`, `make_experiment_client`, `make_nvd_client`, the Slack notifier. | A doctor pass then proves *the gate* will work rather than proving a parallel reimplementation works. This is the property that would have caught the trailing-slash bug in `3696c94`. |
| D8 | **Ship as three increments, A → B → C** (§8). | Each leaves a shippable system (the plan 15 §3a principle). If one slips it must be C. |

## 4. Architecture

New module **`sluice/doctor.py`**, at the package root — a consumer of both `core`
and `gates`, the same position `cli` occupies.

**The binding constraint:** `sluice.core` may not import `sluice.gates` / `api` / `cli`
(ADR-0003, enforced by `tests/test_dependency_arrows.py`). A profile-aware doctor must
know about production gates, so `core/daemon.py` **cannot call it**. The startup gate
therefore runs from the **CLI `daemon` command**, which is already the composition
root, immediately before `run_forever`. No arrow moves.

```
sluice/doctor.py   ->  sluice.core.{profile,db,gate}, sluice.gates.*
sluice/cli         ->  sluice.doctor            (startup gate + `sluice doctor`)
sluice/core        ->  (unchanged; knows nothing about doctor)
```

### The check record

One small frozen record per check: an id, the area it belongs to (gate id or a
pseudo-area like `universal` / `notifier`), a severity, and a callable returning
either "ok" or a problem string. Probes are injected the same way gate world-adapters
are, so unit tests need no network and no filesystem fixtures beyond `tmp_path`.

### Selecting checks

1. Resolve the active profile (`PRODUCTION_VERSION` against the assembled registry).
2. Take its gates' ids.
3. Run: every universal check, the checks for those gate ids, and the notifier checks
   **iff `slack_enabled`**.

Gates that are deferred or DISABLED contribute nothing, so doctor never complains
about Globus or `registration`.

### Detection mode

Detection is in local mode **iff `local_source_dir` is truthy**
(`sluice/gates/detection.py:136`) — currently an inline rule. Doctor needs the same
rule to avoid demanding `remote_host` in local mode, which would make it a **third
predicate twin**. Export **`is_local_mode(config)`** from `detection` and have both
callers use it, exactly as plan 14 did for the eligibility and safe-path rules.

## 5. `ProbeKind` — classifying what is verifiable

The existing `FieldKind` is a **setup**-oriented taxonomy ("who supplies this, and can
setup create it"), not a doctor-oriented one. Keying the drift guard off it would
exempt every host, endpoint and executable in the system — `remote_host`,
`remote_source_path`, `remote_user`, `labkey_server`, `labkey_experiments_project`,
`labkey_schema`, `labkey_query`, `nvd_command` are all `FieldKind.VALUE`, and they are
precisely the deployment-day failures E2 is about.

So `ConfigField` gains **one new field, read only by doctor and its guard**:

```python
ConfigField(
    "remote_host",
    FieldKind.VALUE,                 # unchanged — setup still treats it as a value
    "source",
    "Hostname of the sequencing facility server.",
    probe=ProbeKind.HOST_REACHABLE,  # doctor now knows to probe it
),
```

`ProbeKind` members: `PATH_EXISTS`, `SECRET_READABLE`, `HOST_REACHABLE`,
`EXECUTABLE`, `ENDPOINT`, `NONE`.

**`FieldKind` is deliberately left alone.** Adding members there would change how
`sluice setup` branches, and setup is shipped and working days before handover; a new
independent field cannot regress it.

**`probe` takes no default.** All 44 entries must state one, including
`ProbeKind.NONE`. Adding a config key is then impossible without answering "is this
checkable?" — which is the whole point of choosing the drift guard over a contract
change.

### Which tier each probe runs in

| `ProbeKind` | Local tier | `--comprehensive` adds |
|---|---|---|
| `PATH_EXISTS` | exists, right type, readable | — |
| `SECRET_READABLE` | exists and is non-empty (**FAIL** if not); permissions wider than owner-only (**WARN**) | the credential is *accepted* by its service |
| `HOST_REACHABLE` | set, not a `<placeholder>` | real connection through the adapter |
| `EXECUTABLE` | resolvable on `PATH` | executes and reports its version |
| `ENDPOINT` | set, not a `<placeholder>` | a cheap read against it |
| `NONE` | present (already covered by the schema guard) | — |

Every non-`NONE` key gets the universal local check "present and not a leftover
`<placeholder>` from setup" for free.

## 6. Behaviour

**Exit codes:** `0` when nothing FAILs (WARNs still exit 0, so a degraded-but-working
install is scriptable), `1` when anything FAILs.

**Output:** a rich table grouped by area — status, check name, detail — then a summary
line. Honours the existing global `--json` flag (`sluice --json doctor`), which already
routes logs to stderr so the payload stays pipeable into `jq`.

**Before setup has run:** if the config or database is absent, doctor says so once and
tells the operator to run `sluice setup`, rather than emitting thirty derived failures.

**Startup gate:** local checks only — never the network. A startup path that can hang
on an unreachable host is worse than the problem it solves. Fast, deterministic,
refuses on FAIL with the findings printed and logged; bypassable only via D4.

## 7. The drift guard

A test in the existing style of `tests/test_config_schema.py`, reusing its
`_config_reads_in` ast helper (which already distinguishes config-shaped names from
`run_row[...]` and API-response subscripts):

> For every gate in the shipped production profile, every config key that gate's
> module reads whose `ProbeKind` is not `NONE` must have a doctor check registered
> under that gate's id.

A new gate that reads a new path, secret, host or executable fails the suite until
doctor knows about it. This is the coupling that replaces the rejected twelfth field.

## 8. Increments

Each leaves a shippable system.

**A — `sluice doctor` proves the install.**
`ProbeKind` + 44 classifications; `is_local_mode` exported from detection; the check
record and runner; profile-aware selection; ~15 local checks; the CLI command; the
drift guard; an `docs/OPERATING.md` section.

**B — `--comprehensive`.**
The network tier through the four real adapters (D7). rsync `--list-only` against the
real source path; a **read-only** LabKey probe (`find_experiment_by_run_name` with a
sentinel that will not exist — never `create_experiment`); `nvd --version` executing;
Slack `auth.test`.

Plus the **source manifest visibility report** (§8a), which reuses the listing the
rsync probe already fetches.

### 8a. Source manifest visibility (increment B)

Doctor reports, from the listing it already has:

```
source: 7 run directories visible, 6 with a recognisable manifest
        1 skipped: 240815_A01234_0123_XYZ
```

A directory present at the source but carrying no recognisable manifest is a **WARN**,
naming the directory.

**Why this check exists.** Detection filters a folder out unless it has both a
`001.fastq.gz` and a manifest. The rsync `--include` filters that build the listing are
**case-sensitive** — `*md5s.txt` and `*.sha256` (`detection.py:143-144`, `163-164`;
python-poller `poller.py:197-198`, `211-212`) — so the lowercasing at
`detection.py:88` never gets the chance to help: a manifest named `MD5S.TXT`,
`.SHA256`, or anything not ending in those two strings is never listed, the folder
looks manifest-less, and **the run is filtered out of detection entirely**. No error,
no quarantine, no notification — the run simply never appears.

python-poller has behaved this way for its whole life, so sluice's port is faithful
rather than regressed; the trap is inherited, not introduced. It matters now because
the sequencing core **has already changed its delivery format once**, from MD5 to
SHA-256. The next variation lands while nobody is watching, and the symptom is runs
quietly not being picked up — the hardest failure for an operator to diagnose, because
nothing is wrong anywhere they would think to look.

This check does not fix the trap; it makes it **visible**, which is the appropriate
scope for a diagnostic. Widening the include filters is a detection change, out of
scope here — see §11.

**C — the startup gate.**
The check in the CLI `daemon` command before `run_forever`; the hidden bypass flag;
a WARN logged on every cycle while running bypassed, so a permanently-bypassed daemon
is visible rather than silent.

**Order is A → B → C.** A is the foundation for both others. B is what Will needs
before Friday — it is how deployment day proves the credentials and paths work.
C protects the colleague afterwards and is the most deferrable: with A and B, Will can
run doctor by hand before handing over, and local conditions rarely break
spontaneously between restarts.

## 9. Deferred, with reasoning

**Periodic credential re-checking while the daemon runs.** Tempting: every N cycles,
run the comprehensive checks and auto-pause via the existing B5 mechanism (audited
`control.pause`, actor `"daemon"`, operator-resume only) on failure. **Not built,
deliberately:**

1. It duplicates existing signal. A dead LabKey key does not bleed silently — the
   experiment gate is order 0, fails closed, quarantines the run with a specific
   reason, and the Slack notifier posts it. The colleague sees it within one poll
   interval.
2. A transient network blip would auto-pause the whole pipeline and require an
   operator to resume — firing unattended and repeatedly, exactly when nobody who
   knows the system is around.
3. It is scope that is not needed before handover.

Revisit if quarantine-plus-Slack proves to be insufficient signal in practice.

**Globus credential checks** (the original E2 example) are deferred with the Globus
gates themselves (plan 15 §3a). When `globus_submit` / `globus_verify` land, their
doctor checks land with them — the drift guard will insist.

## 10. Testing

- Check functions are pure over an injected probe, so the local tier tests with
  `tmp_path` and no network, and the comprehensive tier tests against fake adapters.
- **Mutation witnesses**, per the project standard: each check is proven by breaking
  the thing it checks and watching it go FAIL — remove the NVD repo, empty the secret
  file, point `nvd_launch_base` inside `local_staging_dir` — then restoring.
- The drift guard is proven by adding a probe-bearing key with no check and confirming
  the suite fails, in the manner of `tests/test_predicate_twins.py`.

## 11. Open items

- **Should detection's manifest matching be widened?** §8a makes the silent-skip trap
  visible but deliberately does not fix it. The fix would be case-insensitive
  `--include` filters (`*[mM][dD]5s.txt` style, or `--ignore-case` where rsync supports
  it) so an uppercase manifest is listed and the existing lowercasing at
  `detection.py:88` becomes reachable. That is a **detection** change affecting which
  runs enter the system at all, so it wants its own increment, its own test, and a
  deliberate decision — not a side effect of building a diagnostic. Raised by the
  comparison against python-poller during this design; not scheduled.

- `docs/superpowers/specs/` is untracked in git; this spec is allowlisted **by name**
  rather than by glob, because the errata and design-of-record in that directory may
  carry production values (the gate map needed redaction before it could be
  committed). Whether to track the rest of that directory is Will's call, separately.
