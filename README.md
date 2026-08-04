# sluice

A **state-only pipeline control plane and daemon**. It runs a long-lived
pipeline unattended and lets multiple operators steer it **without ever touching
the running process** — they change state in a database, and the daemon reacts.

The repository ships two layers at once:

- **Framework core** (`sluice/core/`) — pipeline-agnostic machinery: the daemon,
  the gate contract, the state machine, the control operations, the control API,
  and the CLI.
- **Production gates** (`sluice/gates/`) — a real wastewater metagenomic-
  sequencing pipeline (download → checksum → merge → NVD preprocess → Globus
  transfer → LabKey register) built on the contract. All infrastructure
  specifics live in gitignored config, so the repo is public *and* the thing
  that actually runs in production.

sluice generalizes the in-production `python-poller` orchestrator into a
controllable, inspectable, restartable control plane.

## Core principles (the spine)

1. **The database is the single source of truth.** One SQLite file (WAL mode);
   no authoritative state lives in any process's memory.
2. **State-only control.** Every operator action only *writes state* (plus an
   audit row and a wide event). It never starts a stage or signals the daemon.
3. **The daemon is the only actor.** The CLI/API and the daemon meet at the
   database, never by talking to each other.
4. **Stateless between looks.** Each cycle the daemon re-derives the whole to-do
   list from the database — editing the database *is* editing its plan.
5. **One-way dependency.** `gates → core`; `core` never imports `gates`. Enforced
   by `tests/test_dependency_arrows.py`.
6. **Two write paths.** Control writes go through the API; the daemon writes its
   own progress directly, so the pipeline never depends on the API being up.

## Status

Bootstrap. The package structure, config discipline, CI, and preserved design
docs are in place; the dependency-arrow guard is the only test with teeth yet.
Pipeline logic is built in phases:

- **Plan A — Control core** (next): db, gate contract, context, profile,
  reconcile, daemon, control ops, audit, events, plus reference/mock gates.
- **Plan B — Production gates**: the real gates, ported from `python-poller`.
- **Plan C — API + CLI**: FastAPI wrapping the control ops; the `sluice` CLI.

The whole-system design is
[`docs/superpowers/specs/2026-07-16-sluice-architecture-design-of-record.md`](docs/superpowers/specs/2026-07-16-sluice-architecture-design-of-record.md).
Where the ported code will contradict it, see
[`docs/superpowers/specs/sluice-spec-errata.md`](docs/superpowers/specs/sluice-spec-errata.md).

## Developing

This project uses [pixi](https://pixi.sh). Platforms: `linux-64` (production) and
`osx-arm64` (dev).

```sh
pixi install     # solve + install the environment from pixi.lock
pixi run test    # run the pytest suite
```

## Configuration & secrets

Real infrastructure values live **only** in a gitignored `config.toml`. The
normal path is `sluice setup`, which writes one from the schema with every key
explained in place. To start from the committed example instead:

```sh
cp config.example.toml config.toml   # then replace every <placeholder>
```

- `config.example.toml` is **placeholders only** and is the committed schema.
- Section headers group keys for reading only — sluice discards them at load, so
  a key works wherever you put it in the file.
- Secrets are never config *values*: they live in untracked files referenced by
  the `*_file` keys, or in env. The Slack bot token is read from
  `SLACK_BOT_TOKEN`, never from config.
- A missing required key is a **load-time error**, not a silent production
  default.
- The state DB (`*.db`) and `logs/` are gitignored — state is never committed.
