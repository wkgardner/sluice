# ADR-0007: Shared operations — the database is claimed by one host, not one user

Status: accepted (2026-09-01, decided by Will)

## Context

sluice needs several people to operate it: check state, hold a run, clear a
quarantine. Until now the installation was tied to one person by file
permissions — `config.toml` at 0600, `secrets/` at 0700 — and there was no way
for a colleague to do any of it.

The obvious answer was Plan C, the FastAPI control-write path. It is the wrong
answer for this problem. The API adds **remote** access, not multi-user access;
design §13 lists *"Fine-grained app-level authz (SSH + file perms are the
interim)"* as out of scope, so an HTTP layer would leave access control exactly
where it is. What the design already specifies for this case is §5's
single-host invariant: *"The DB is only ever opened from the one host the daemon
and operators share (operators SSH in)."*

Two facts about the deployment decided the shape:

1. **The daemon runs in a screen session on a CHTC login node.** That host is
   one operators can SSH into, so the design's intended model is available.
2. **The state file lives on the shared home directory.** CHTC has no scratch,
   and node-local storage would lose the entire run history on a reboot. §5 says
   the DB "must live on local/scratch disk, not a share mounted by other
   machines"; that instruction cannot be followed here without trading
   corruption risk for guaranteed data loss.

Fact 2 is what makes fact 1 dangerous. Nothing stopped a colleague landing on a
different login node and running any sluice command, which would make them a
second NFS client on a WAL database — the exact cross-host network-FS locking §5
forbids. Silent, and it takes the run history with it.

`fcntl.flock` does not help. It is per-machine, so a second *daemon* on another
node takes its own lock and both run against the same file. That gap has existed
since the daemon was written; it was invisible while only one host was in use.

## Decision

**1. The database is claimed by a host, and the claim is enforced.**

The daemon writes its hostname to `<state_file>.host` at every startup.
`assert_daemon_host` refuses any command whose machine does not match, naming the
host to go to. It runs before the daemon takes its lock, so it also refuses a
second daemon on a second node — the half the flock structurally cannot see.

The claim is a **plain file beside the database, not a `poller_metadata` row.**
This is the load-bearing detail: reading a metadata row means opening SQLite,
which creates the WAL `-shm` segment, which is precisely the cross-host act being
prevented. A guard that had to open the database to decide whether the database
may be opened would be no guard at all.

An unclaimed database is usable from anywhere. The claim arrives with the daemon;
refusing before then would make a fresh install unusable by the person setting it
up.

**2. We keep the database on shared storage, knowingly.**

A deviation from §5's "local/scratch disk" instruction, with the host claim as the
compensating control. §5's stated reason for that instruction is cross-host
locking; the claim addresses the reason directly rather than the instruction
literally. Durability wins: a corrupted database is recoverable from a backup, a
database on a node that got rebuilt is not.

**3. Sharing is a Unix group, and it covers the config and state only.**

`sluice setup --shared-group GROUP` sets `config.toml` to 0640 and the state
directory to setgid 2770. **`secrets/` stays 0700.** Someone clearing a
quarantine never needs the rsync password or the LabKey key — only the daemon
does — and that asymmetry is what makes widening safe at all.

The state directory must be setgid. Without it SQLite's `-wal` and `-shm` files
are created owned by whoever ran the command that made them, and the next
operator cannot write them: an install that works for one person at a time and
fails on the second, intermittently.

**4. The audit actor is the OS user, and cannot be chosen.**

`--actor` is removed, not ignored. It accepted any string, which was harmless
while one person could write to the database and became fake evidence the moment
several could — a trail recording who *claimed* to act is worse than no trail,
because it reads as authoritative. An OS that cannot name its user is now a
refusal rather than a row attributed to `"unknown"`.

## Consequences

Multi-user operation works today with no service to run, no port to secure, and
no authentication to write. SSH is the transport and already authenticates; the
OS user is the identity and cannot be forged through the CLI.

**A script passing `--actor` now fails** with a usage error. Deliberate: silently
ignoring it would record something other than what the script says.

**Authorization is still all-or-nothing.** Anyone in the group can hold, skip or
force past a protected run. `protected_by` remains advisory between cooperating
people, exactly as before — this ADR does not change what a permitted user may
do, only who is permitted and whether the record of it is trustworthy.

**The guard stops accidents, not determination.** Someone who deletes
`<state_file>.host`, or writes to the database from Python instead of the CLI,
is past it. The threat model is a helpful colleague on the wrong login node, and
that is fully covered.

**Plan C keeps its justification and loses its urgency**: operating sluice from a
laptop without SSHing to CHTC. When it is built, the actor must come from the
authenticated identity rather than a request field, for the reason in decision 4.
