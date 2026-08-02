# Operating sluice

A guide for whoever is watching the pipeline. Assumes no knowledge of the codebase.

If you read only one section, read **"The one thing to understand"** and **"A run is stuck. Now
what?"** — together they cover almost everything that comes up.

---

## The one thing to understand

**sluice is not a queue. It is a loop.**

Every few minutes the daemon wakes up, looks at the database, and asks of each run: *what is the
next thing that should happen?* Then it does exactly one thing and goes back to sleep.

Nothing is ever "sent" or "submitted" to it. Commands you type change **stored state**, and the
daemon notices on its next pass. This has three consequences worth internalising:

- **Commands return instantly and appear to do nothing.** That is correct. `sluice hold SEQ_001`
  changes a column; the daemon acts on it within one poll interval.
- **Holding a run does not stop work already running.** An in-flight gate finishes. The *next* gate
  never starts.
- **You cannot break it by running a command twice.** Every command is idempotent — running it
  again reports "nothing to do".

## Vocabulary

| Term | Meaning |
|---|---|
| **run** | One sequencing output directory moving through the pipeline. |
| **experiment id** | The LabKey experiment number. **This is the number you care about** — it is what you see in LabKey and what names the run's folder on disk. |
| **gate** | One step of work (create the experiment, download, verify checksums, …). |
| **quarantined** | A gate failed. The run stops and waits for a human. There is no automatic retry — this is deliberate. |
| **held** | A person told the daemon to leave this run alone. |
| **complete** | Every gate in this run's contract succeeded. |

**Quarantined and held are independent.** A run can be both. Clearing one does not clear the other
— if a run is both, it needs `clear-error` *and* `unhold`.

---

## Installing and upgrading

```bash
sluice setup
```

Creates `~/.sluice/` — config, database, logs, credential files — and migrates the database. Then
edit `~/.sluice/config.json` to fill in the values it lists as needed, and put your credentials in
the empty files under `~/.sluice/secrets/`.

**Run the same command again after every sluice release.** It adds keys the new version introduced
and leaves everything you have already set completely alone — no value you tuned is ever
overwritten, so you do not need to read a diff first. Use `--dry-run` to see what it would change.

If it reports keys "not used by this version", they are left in place untouched.

---

## Everyday commands

All commands take `--config PATH`, or read `$SLUICE_CONFIG`, or fall back to `./config.json`.
After `sluice setup`, point `$SLUICE_CONFIG` at `~/.sluice/config.json` (or pass `--config`).

### See everything

```bash
sluice status          # or, from a checkout: pixi run sluice status
```

```
pipeline profile  production-v3.1
daemon            running

RUN             EXPERIMENT  OUTCOME      LIFECYCLE  NOTE
SEQ_20260728_A  4468        completed    active
SEQ_20260801_A  4471        active       active
SEQ_20260802_B  4472        quarantined  active     HASH_MISMATCH: 2 of 8 file(s) missing or corrupt
SEQ_20260802_C  —           held         held

1 run(s) quarantined and waiting for an operator.
Fix the cause, then: sluice clear-error <run>
```

Outcomes are colour-coded: green completed, **red quarantined**, yellow held, dim skipped. On a
healthy screen, red means something needs you.

### Look at one run closely

```bash
sluice show SEQ_20260802_A
```

Shows which gates are done, which are still pending, and the full error. **"Which gate is it stuck
on?"** is answered by the `gates pending` line — the first one listed is where it stopped.

### Everything, as JSON

```bash
sluice --json status
```

For scripting. Logs go to stderr, so `sluice --json status | jq` works cleanly.

---

## A run is stuck. Now what?

**1. Find out why.**

```bash
sluice show <run>
```

The `error` line says what failed. The messages are written to be read — they name the run, the
files, and the shortfall.

**2. Fix the underlying cause.** This is the part sluice cannot do for you. Some common ones:

| Error starts with | What it means | Typical fix |
|---|---|---|
| `MANIFEST_MISSING` | The delivery has no `md5s.txt` / `.sha256` to verify against. | Talk to whoever produced the delivery. **Re-running will not help** — the manifest was never there. |
| `HASH_MISMATCH` | Files are present but corrupt or incomplete. | Usually a truncated transfer. Clearing the error is often enough — the download resumes and repairs. |
| `insufficient disk space` | The CephFS quota does not have room. | Free space, or raise the quota. The message says how much is needed. |
| `download gate: ... listed zero files` | The run directory looked empty. | Check the run really exists on the sequencing server and is readable. |
| `experiment gate: LabKey ...` | LabKey was unreachable or refused. | Check LabKey is up and the credentials are valid. |
| `samplesheet gate: ...` | NVD's samplesheet tool failed. | The tool's own error follows the colon. Often a missing lane file or a bad `nvd_repo_dir`. |
| `nvd gate: ... exited N` | The NVD pipeline itself failed. | The message names the log file. Read it, fix the cause, then clear the error — the run resumes from Nextflow's last checkpoint. |
| `nvd_launch_base ... is inside local_staging_dir` | A configuration mistake, not a run problem. | The NVD launch directory must be on local disk, not the shared staging tree, or resuming silently does nothing. Fix the config and clear the error. |

**3. Let it try again.**

```bash
sluice clear-error <run> --reason "why this happened"
```

The daemon retries the failed gate on its next pass. **Gates resume rather than restart** — a
half-downloaded run continues where it stopped rather than transferring everything again.

The `--reason` is recorded in the audit trail. Write it for whoever reads it in three months.

---

## Stopping things

**One run:**

```bash
sluice hold SEQ_001 --reason "lab asked us to wait"
sluice unhold SEQ_001
```

**Everything:**

```bash
sluice pause --reason "storage maintenance"
sluice resume
```

Use `pause` when something is wrong fleet-wide — a full disk, an unreachable server, bad config. No
run advances until you `resume`. In-flight gates still finish.

**A run that should never be processed:**

```bash
sluice skip SEQ_001 --reason "duplicate delivery"
```

Reverse with `unskip`.

---

## What sluice will never do

Worth knowing, so you can rule things out when something looks wrong:

- **It never deletes your data.** Not on failure, not on rewind, not ever (ADR-0005). If files need
  removing, a human removes them.
- **It never retries a failed gate on its own.** A quarantined run waits indefinitely. This is
  deliberate: silent retries hide real problems and can burn multi-terabyte transfers in a loop.
- **It never processes a run under a pipeline it does not recognise.** A run stamped with an unknown
  profile quarantines immediately rather than guessing.

## Who did what

Every state change is recorded in the `control_audit` table with the actor, the time, the reason,
and exactly what changed. Commands print the audit row id:

```
hold SEQ_001: ok
    lifecycle -> held
    audited as #42
```

The actor defaults to your OS username. Override it with `--actor` if you are acting on someone
else's behalf.

---

## When to get hold of Will

Most things can wait. These probably cannot:

- **Every run is quarantining with the same error.** Something environmental broke — credentials,
  a mount, the server.
- **`sluice status` itself fails.** The database or config is unreachable.
- **A run says `complete` but the data looks wrong.** That is a correctness problem, not an
  operational one.
- **A `MANIFEST_MISSING` on a delivery that should have had one.** Worth flagging to the sequencing
  facility rather than sitting on.

## Current pipeline scope

As of 2026-08-02 the pipeline runs: **experiment → download → checksum → samplesheet → nvd**.

A run that passes all three is reported `complete`, and that is truthful — it is complete *under the
contract that exists today*. The Globus backup pair is deferred until Will returns; when it lands, runs simply continue further
before completing. Nothing behind it changes, and nothing already done is redone.

**NVD runs take hours.** A run sitting on the `nvd` gate is normal, not stuck. To tell working from
hung, look at the NVD log — its path is in the run's quarantine reason if it failed, and otherwise
under `nvd_log_dir/<experiment id>.log`. That log is **appended**, never overwritten, so a retried
run still has the failed attempt's output above the new one.

If an NVD run is interrupted, clearing the error resumes it from Nextflow's last checkpoint rather
than restarting the whole thing — provided nobody has moved the launch directory.
