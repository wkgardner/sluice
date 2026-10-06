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

**First time on a machine**, from the sluice checkout:

```bash
./install.sh
```

This puts a `sluice` command on your PATH so every command in this guide works as written,
from any directory. It needs no sudo, changes nothing outside `~/.local/bin`, and is safe to
re-run. If it tells you `~/.local/bin` is not on your PATH, it prints the exact line to add
to your shell profile.

If you would rather not install anything, every command below also works as
`pixi run sluice <command>` from inside the checkout, or from anywhere as
`pixi run --manifest-path /path/to/sluice/pixi.toml sluice <command>`.

**Then, and again after every sluice release:**

```bash
sluice setup
```

Creates `~/.sluice/` — config, database, logs, credential files — and migrates the database. Then
edit `~/.sluice/config.toml` to fill in the values it lists, and put your credentials in the empty
files under `~/.sluice/secrets/`.

The config is grouped into sections with every key explained in a comment above it, so the file
tells you what it wants without you coming back here. **Those sections are for reading only** —
sluice discards them when it loads the file, so a key works wherever you put it, and moving one
between sections changes nothing. The one thing that is an error is setting the same key twice in
two different sections; sluice refuses to load that rather than silently picking one.

On a later run it adds keys the new version introduced and leaves everything you have already set
alone: no value you tuned is ever overwritten. Use `--dry-run` to see what it would change.

Re-running against an existing config **asks first**, and copies the old file to
`config.toml.bak` before writing. Your values survive either way. The backup is there because the
file is regenerated from the schema, which loses any comments or ordering you added by hand — and
loses unsaved changes if an editor has the file open, so **close it before re-running.** Pass
`--yes` to skip the question. In a script, setup refuses rather than rewriting unasked.

If it reports keys "not used by this version", they are left in place, collected under an
`[unrecognised]` section so you can see what is no longer read.

### Coming from a `config.json`

Installs from before sluice used TOML have a `config.json`. The next `sluice setup` migrates it:
your values move into `config.toml`, and **the old file is left exactly where it is**. Nothing is
overwritten, so there is nothing to confirm and no backup to take — the old file is the backup.
Delete it once you are happy, and until you do, `config.toml` is the one that counts.

Every command still reads a `config.json` when no `config.toml` sits beside it, so upgrading
sluice does not break anything before you get to `sluice setup`.

---

## Pointing sluice at your data

sluice reads runs from one of two places: a **remote server over SSH** (normal operation)
or a **local directory** (rehearsal, no credentials needed). It picks local mode whenever
`local_source_dir` has a value, so leave that key empty for normal operation.

### Remote

```toml
[detection]
remote_user        = "<remote_user>"
remote_host        = "<remote_host>"
remote_source_path = "<remote_source_path>"
password_file      = "~/.sluice/secrets/remote_password"
```

The first three are yours to fill in — the real host, account and path live in your
config, never in this guide.

`remote_source_path` has one rule, and breaking it fails quietly rather than loudly.

**It must point at the folder that CONTAINS run directories** — not at a run, and not at a
delivery folder inside a run. Given this on the server:

```
~/deliveries/                          <- remote_source_path points HERE
   20251120_A01234_0001_RUNNAME/       <- this becomes the run
      <delivery-folder>/
         ..._001.fastq.gz
         .verified-copy.sha256
```

set `remote_source_path` to `deliveries/`. The run is then
`20251120_A01234_0001_RUNNAME`. Pointing it one level deeper at the delivery folder finds
nothing, because the fastq files would be loose at the top level and a loose fastq is not
a run.

Relative paths resolve against the SSH user's home directory. The trailing slash matters to
`rsync`, but sluice adds it for you, so `deliveries` and `deliveries/` behave the same.

### What sluice treats as a run

A folder is picked up only if it contains **both**:

- a checksum manifest — a file ending `md5s.txt` or `.sha256` (including hidden ones like
  `.verified-copy.sha256`), and
- at least one file ending `001.fastq.gz`.

Anything else is ignored, so unrelated folders sitting beside yours on the server do no
harm. The files can sit at any depth inside the run folder — the delivery-folder layer
above is found fine — and all of them count toward that one run.

If a folder you expect is not being picked up, it is almost always one of those two
missing, or `remote_source_path` pointing at the wrong level.

### Local, for rehearsal

```toml
local_source_dir = "/path/to/a/folder/of/runs"
```

Same layout rules, same run-detection filter, no SSH and no credentials. Clear the key to
go back to remote.

---

## Checking the installation

```bash
sluice doctor
```

`sluice setup` makes sure your config has every setting. `sluice doctor` makes
sure those settings point at things that are really there — the directories
exist, the credential files are filled in, the NVD repository is where you said.

Run it after `sluice setup`, and any time something looks wrong.

The first line of the report is the config file it read. If doctor keeps
reporting something you have already fixed, check that line — you are almost
certainly editing a different file from the one it is reading.

Each line is one check:

- **ok** — verified.
- **warn** — sluice will run, but something should be fixed. A credential file
  readable by other users, a working directory that is missing and will be
  recreated, or an optional setting still holding the `<placeholder>` the
  example config ships — that last one is passed through to the tool exactly as
  written, so fill it in or clear the key.
- **FAIL** — sluice cannot work until you fix it. Every failure names what to
  change: usually a config setting, sometimes a command to run.
- **skip** — the check does not apply to your setup. Settings for the source
  mode you are not using are skipped, as are notification settings when Slack
  is turned off.

Doctor exits 0 unless something failed, so it is safe to put in a script.
Warnings do not make it exit non-zero.

**Doctor never changes anything.** It only reads — no config written, no
directory created, no run state touched. (Reading the database can leave SQLite's
own `-wal`/`-shm` files beside it, exactly as any read does; none of your data
changes.) If it reports a problem, it tells you what to edit; it will not edit it
for you.

---

## Running the daemon

Nothing in sluice moves data except the daemon. The other commands only change stored
state; the daemon is what notices and acts.

It runs in the foreground until interrupted, and **nothing supervises it** — no service, no
restart-on-boot. So it lives in a `screen` or `tmux` session:

```bash
screen -S sluice          # or: tmux new -s sluice
sluice daemon
```

Detach with `Ctrl-A D` in screen, or `Ctrl-B D` in tmux. Come back with:

```bash
screen -r sluice          # or: tmux attach -t sluice
```

### After a reboot, sluice does not come back on its own

**You have to start it again.** There is no service, and nothing will remind you. If runs
stop being picked up and nothing else explains it, this is the first thing to check.

### How to tell whether it is actually running

`sluice status` tells you, on its first line:

```
daemon    running, last look 2026-08-03T14:22:07+00:00
daemon    NOT RUNNING — start it: sluice daemon          ← red
```

That is a real check, not a guess — it asks the operating system who holds the daemon's lock
file. **This only works from the machine the daemon runs on**, which is where you are anyway,
because every sluice command is refused from anywhere else.

The timestamp is the last time the daemon *began* a look. It being old is not by itself a
problem: an NVD run takes hours, and during one the daemon is alive and working rather than
looping. `running` with an old timestamp and an active run is normal. `running` with an old
timestamp and nothing in flight is worth asking about.

Starting a second daemon on the same machine is safe: it fails immediately on the lock file
rather than racing the first one.

---

## Letting other people operate sluice

By default an installation belongs to one person: the config and credentials are
readable only by whoever ran `sluice setup`. To let a team operate it:

```bash
sluice setup --shared-group sluice-ops
```

Everyone in that Unix group can then see run state, hold, unhold, skip and clear
errors. **Your credentials are not shared** — `secrets/` stays yours alone, because
operating sluice never needs the passwords the daemon uses. Only the config and the
state directory are widened.

### Everyone must run from the machine the daemon is on

This is not a convention, it is enforced. The state database is claimed by whichever
host the daemon starts on, and a command from any other machine is refused:

```
sluice: this sluice database belongs to 'submit1' and you are on 'submit2'.
```

The reason is worth knowing, because the failure it prevents is silent. The database
lives on your shared home directory, and SQLite databases corrupt when two different
machines open them at once. There is no error when it happens — you find out later,
when the run history is gone. So sluice refuses instead, and tells you where to go.

The same check stops a second daemon starting on a second node, which the normal
single-instance lock cannot see (that lock only works within one machine).

If you move the daemon to a different host, just start it there; the next start
re-claims the database, and everyone follows it.

### Who did what

Every state change records the OS user who made it, and there is no way to record
anything else. `sluice audit`-style questions are answered from that trail, so it
has to be a name nobody could have chosen for themselves.

If you have scripts passing `--actor`, that flag is gone and they will fail loudly
rather than quietly recording the wrong person.

---

## Everyday commands

All commands take `--config PATH`, or read `$SLUICE_CONFIG`, or fall back to `./config.toml`.
After `sluice setup`, point `$SLUICE_CONFIG` at `~/.sluice/config.toml` (or pass `--config`).

### See everything

```bash
sluice status
```

```
pipeline profile  production-v3.1
daemon            running, last look 2026-08-03T14:22:07+00:00

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
| `profiling gate: ... exited N` | The silly-fast-bfx pipeline itself failed. | The message names the log (`profiling_log_dir/<experiment id>.log`). Read it, fix the cause, then clear the error — the run resumes from Nextflow's last checkpoint. **NVD waits until profiling passes.** |
| `profiling gate: ... N failed task(s)` | Nextflow exited 0 but some tasks failed (the site config is ignoring task errors). | The message names the `execution_trace.txt`; its `FAILED` rows say which tasks. Fix and clear the error. |
| `profiling gate: ... wrote no .../execution_trace.txt` | Nextflow exited 0 without producing a trace, so there is no evidence the work happened. | Read the log it names. Usually the pipeline stopped before starting any task (bad params file, schema validation). |
| `profiling_launch_base ... is inside local_staging_dir` | A configuration mistake, not a run problem. | Same rule as NVD's launch directory: local disk only. Fix the config and clear the error. |
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

Runs detected under **`production-v3.2`** go: **experiment → download → checksum → samplesheet →
profiling → nvd**. Runs detected earlier stay on **`production-v3.1`** (the same chain without
`profiling`) for good — a run keeps the profile it was stamped with, so nothing old re-runs. Which
profile new runs get is switched with `sluice set-default-profile <version>`; `sluice status` shows
the current one.

A run that passes every gate of its profile is reported `complete`, and that is truthful — it is complete *under the
contract that exists today*. The Globus backup pair is deferred until Will returns; when it lands, runs simply continue further
before completing. Nothing behind it changes, and nothing already done is redone.

**The first gate talks to LabKey**, so a working LabKey configuration is required before any run
gets past step one. `experiment` creates the run's LabKey experiment and records the experiment ID
that everything downstream — including the staging directory name — is keyed on. A run with LabKey
misconfigured quarantines immediately on its first look, with the reason naming LabKey.

There is currently **no operator command to exclude a gate from a run.** The capability exists
inside sluice and is audited, but it is not wired to the command line yet, so a pipeline stage
cannot be skipped for a single run today. `sluice show <run>` will display exclusions if any exist.
If you need one, that is a "get hold of Will" situation.

**Profiling (silly-fast-bfx) runs before NVD** — a quick Deacon + Sylph screen of the raw reads,
published under `profiling_results_base/<experiment id>/profiling-results/`. It is quick compared
with NVD, but on CHTC it still waits in the HTCondor queue. **If it fails, the run is quarantined
and NVD does not start** until the cause is fixed and the error cleared. Its log is
`profiling_log_dir/<experiment id>.log`, appended across retries like NVD's.

**NVD runs take hours.** A run sitting on the `nvd` gate is normal, not stuck. To tell working from
hung, look at the NVD log — its path is in the run's quarantine reason if it failed, and otherwise
under `nvd_log_dir/<experiment id>.log`. That log is **appended**, never overwritten, so a retried
run still has the failed attempt's output above the new one.

If an NVD run is interrupted, clearing the error resumes it from Nextflow's last checkpoint rather
than restarting the whole thing — provided nobody has moved the launch directory.
