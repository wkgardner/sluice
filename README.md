# sluice

A **state-only pipeline control plane and daemon**. It runs a long-lived
pipeline unattended and lets operators steer it **without ever touching the
running process**: they change state in a database, and the daemon reacts on
its next look.

sluice runs a wastewater metagenomic-sequencing pipeline in production. Each
week it picks up new sequencing runs, brings the data onto shared storage, and
drives them through profiling and virus detection, posting progress to Slack.

## What it does

The production pipeline is a fixed list of **gates**, each a unit of work that
runs once per sequencing run, in order:

| Order | Gate | What it does |
|---|---|---|
| 0 | `experiment` | Finds or creates the run's LabKey experiment record |
| 1 | `download` | Copies the run's FASTQs from the sequencing server (rsync over SSH) |
| 2 | `checksum` | Verifies every file against the sequencer's md5 list |
| 3 | `samplesheet` | Writes the samplesheet both pipelines read |
| 4 | `registration` | Registers the run's samples in LabKey and reports any it can't |
| 5 | `profiling` | Runs the REMV Nextflow pipeline (Deacon, sylph, EsViritu) on HTCondor |
| 6 | `nvd` | Runs the NVD Nextflow pipeline and uploads its hits to LabKey |

Both Nextflow gates post a "started" message with a link to monitor the run on
Seqera Platform. A failed gate **quarantines** the run (it stops, it never
retries itself) until an operator fixes the cause and runs `sluice clear-error`.

The gate list lives in a versioned **profile** (`sluice/gates/profiles.py`;
currently `production-v3.3`). Each run is stamped with the profile it started
under, so changing the pipeline never changes the rules for a run already in
flight.

## Core principles

1. **The database is the single source of truth.** One SQLite file (WAL mode);
   no authoritative state lives in any process's memory.
2. **State-only control.** Every operator command only *writes state* (plus an
   audit row and a structured event). It never starts work or signals the daemon.
3. **The daemon is the only actor.** The CLI and the daemon meet at the
   database, never by talking to each other.
4. **Stateless between looks.** Each cycle the daemon re-derives the whole to-do
   list from the database, so editing the database *is* editing its plan.
5. **One-way dependency.** `gates → core`; `core` never imports `gates`.
   Enforced by `tests/test_dependency_arrows.py`.
6. **Gates return, the daemon writes.** A gate reports a result; only the daemon
   records it (ADR-0002).

## Repository layout

| Path | Holds |
|---|---|
| `sluice/core/` | The pipeline-agnostic framework: daemon, gate contract, reconcile loop, control operations, events, database |
| `sluice/gates/` | The production gates and profiles, built on that contract |
| `sluice/cli/` | The `sluice` operator command line |
| `sluice/config/` | The config schema that `sluice setup` and `config.example.toml` are generated from |
| `docs/adr/` | Binding design decisions |
| `tests/` | The pytest suite |

## Getting started

This project uses [pixi](https://pixi.sh). Platforms: `linux-64` (production)
and `osx-arm64` (development).

```sh
pixi install     # install the environment from pixi.lock
pixi run test    # run the test suite
./install.sh     # put a `sluice` wrapper on your PATH (~/.local/bin)
sluice setup     # write ~/.sluice/config.toml, every key explained in place
sluice doctor    # check that this installation can actually work
sluice daemon    # start the reconcile loop
```

Day-to-day commands:

```sh
sluice status                  # every run, and whether the daemon is paused
sluice show <run>              # one run in full, including which gates remain
sluice hold <run> / unhold     # stop or restart one run advancing
sluice clear-error <run>       # retry a quarantined run once the cause is fixed
sluice reset-gate <run> <gate> # run one gate again
sluice pause / resume          # stop or restart every run advancing
```

Every command returns immediately; the daemon acts on it within one poll
interval. Run `sluice --help` for the full list.

## Configuration and secrets

Real infrastructure values (hosts, paths, LabKey lists, the Slack channel) live
**only** in an untracked `config.toml`, so none of them are in this repository.

- `sluice setup` writes one from the schema. `config.example.toml` is the
  committed, placeholders-only copy of that schema.
- Section headers are for reading only; a key works wherever it sits in the file.
- **Secrets are never config values.** Each one lives in its own file outside
  the repo, and config names that file: `password_file`,
  `labkey_api_key_file`, `slack_bot_token_file`.
- A missing required key is a **load-time error**, never a silent default.
- The state database and logs live under `~/.sluice/` and are never committed.

## Documentation

- [`docs/OPERATING.md`](docs/OPERATING.md): for whoever is watching the
  pipeline. Start here when a run is stuck.
- [`docs/DEPLOYING-CHTC.md`](docs/DEPLOYING-CHTC.md): first-install checklist on
  a CHTC login node.
- [`docs/ADDING-A-GATE.md`](docs/ADDING-A-GATE.md): how to add a unit of
  pipeline work.
- [`docs/adr/`](docs/adr/): the decisions the code is built on.
- [`CONTEXT.md`](CONTEXT.md): the glossary of words this codebase uses.
- [`docs/superpowers/specs/`](docs/superpowers/specs/): the original
  architecture design and its errata.
