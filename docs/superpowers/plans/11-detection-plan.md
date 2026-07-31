# Plan 11 — Production detection (Plan B, increment 1)

**Type:** implementation plan, LIGHTWEIGHT MODE — one implementer, single pass, one
review. Every unit below is specified to a named failing test.

**Modules owned:** `sluice/core/daemon.py` (the detection-source seam: a small,
surgical change to `detect_new_runs` + passthrough on `run_cycle`/`run_forever`) and
**`sluice/gates/detection.py` (new)** — the production rsync lister. Tests:
core-seam tests join `tests/test_daemon.py`; adapter tests are the new
`tests/test_detection.py`.

**Ground truth:** gate map `docs/superpowers/plans/10-plan-b-gate-map.md` §8 step 1
(FINAL) and its two caveats (detection is NOT a gate; no LIVE `production-v3.1`
stamping without the registry entry); design of record §7/§8 (detection outside the
per-run loop; version frozen at detection); errata **D4**; ADR-0003 (layering),
ADR-0005 (no deletion); the live poller (**PP**) `poller.py:176-307` (listing +
parsing), `:158-169` (`load_password`), `:335-357` (`-avP` download — NOT ours),
`:360-379` (`is_connection_blocked_error`); subsystem map porting notes (detection
idempotent/state-only; `has_md5`/file-count are facts about the world; sshpass
password-on-argv exposure at PP `:335`/`:491` note); `sluice/core/db.py:381-421`
(`insert_run` — idempotent, already takes `size_tb`); `daemon.py:190-241`
(`detect_new_runs` as built), `tests/test_daemon.py:206-286` (the closed-import ast
pin — **`subprocess` is forbidden in core**, which forces the seam), and
`tests/test_dependency_arrows.py` (gates → core one-way).

---

## 1. Scope & non-goals

### Owned

| Thing | Public surface |
|---|---|
| The detection-source seam (core) | `detect_new_runs(config, *, lister=None, notifier=None) -> list[str]` — new keyword-only `lister` |
| The reference lister (core; the Plan-A sweep, extracted) | `watch_dir_lister(config) -> list[tuple[str, float | None]]` |
| Seam passthrough (core) | `run_cycle(..., lister=None, ...)`, `run_forever(..., lister=None, ...)` |
| The production lister (outside core) | `sluice/gates/detection.py`: `parse_rsync_listing(output)`, `build_listing_command(config)`, `list_remote_runs(config)`, `RemoteListingError` |
| Config surface | new required key **`remote_source_path`** (remote mode); existing `remote_user`, `remote_host`, `password_file`, `local_source_dir` get their consumers; `config.example.json` updated, placeholders only |

**The candidate type is a plain 2-tuple `(name: str, size_tb: float | None)`** — no
`NamedTuple`/dataclass. Deliberate: `typing`/`dataclasses` are outside daemon.py's
pinned import set (`tests/test_daemon.py:206-226`), and amending a shipped pin for a
convenience type is not worth it. A lister is any
`Callable[[Mapping], Sequence[tuple[str, float | None]]]` — the **`disk_floor_check`
precedent** (`daemon.py:446`), a plain injected callable, not a Protocol.
(`Notifier` is a Protocol because it has three methods; a lister has one operation.
Decided — do not build a Protocol.)

**Why `sluice/gates/detection.py` and not core:** core cannot import `subprocess`
(the ast pin) and must import with `sluice.gates` absent. The `sluice.gates` package
already sits on the forbidden-for-core side of the dependency arrow, so a module
there is auto-guarded by `tests/test_dependency_arrows.py` with zero test changes.
Its module docstring MUST state: *this is a world adapter injected at the
composition root; it is NOT a Gate, exports no `Gate` object, and never appears in
any profile* (gate map §3 "Not gates"). Do not create a new top-level package — it
would silently be importable from core (the `FORBIDDEN` tuple names only
gates/api/cli).

### Explicitly NOT owned (and where it belongs)

- **Downloading anything.** Detection only LISTS (`--list-only`). `rsync -avP`,
  partial-resume, the D4 wave ramp, `is_connection_blocked_error` backoff, and disk
  pre-flight are the **`download` gate's** business (gate map §8 step 5). Unit 5
  pins `--list-only` on every argv this module can build.
- **The `production-v3.1` profile / gates / migration 007** — later increments. This
  increment ships the machinery plus the go-live interlock (§2, unit 7), not the
  profile entry.
- **Notifier enrichment** (`open_run_thread(experiment_id=, size_tb=)` — A5): the
  Slack notifier increment. Detection stamps `size_tb` on the **row** now; the
  notify call stays `safe_notify(notifier, "open_run_thread", name)` unchanged.
- **`watch_dir` deprecation.** The Plan-A sweep survives as `watch_dir_lister`, the
  default when no lister is injected — it is the reference-mode/test detection
  source, not dead code. Every existing detection test stays green untouched.
- **Eligibility beyond candidacy.** PP's `should_download_folder` remote-facts checks
  (`has_md5`, file count — PP `:1412-1417`) are folded into *candidacy* (unit 4), so
  nothing about the remote listing ever leaks into `reconcile.next_gate`'s signature
  (subsystem-map porting note). Reconcile stays pure; no new run column.

---

## 2. Errata & obligations in scope — decisions taken, no TBDs

- **Gate-map caveat 1 — detection is NOT a gate.** Structural: no `Gate` object, no
  satisfaction column, no profile entry; detection is row-existence via
  `insert_run` (map §3, resolving the subsystem-map open question about `detected`
  occupying a gate slot, PP `poller_state_db.py:33`).
- **Gate-map caveat 2 — the go-live interlock.** `detect_new_runs` **never
  hardcodes a pipeline version**: the stamp comes from `metadata.pipeline_profile`,
  read inside the insert transaction (as built, `daemon.py:220-231`), and
  `prepare_database` already validates that value against the assembled registry at
  startup (`daemon.py:171-187`). Therefore stamping `production-v3.1` is
  *impossible* until a deployment both assembles a registry containing it and sets
  it as the default — and a mismatch dies as **one loud `UnknownProfileError` at
  startup, never N per-run quarantines**. Unit 7 makes this executable with the
  literal string. The map's "profile entry lands in the same increment" resolves
  as: the *interlock* lands now; the entry lands with the gate increments; going
  LIVE (wiring `list_remote_runs` + setting the default) is a composition-root act
  that is structurally blocked until then.
- **D4 (connection-blocked ramp) — boundary drawn.** Detection is **one** rsync
  connection (`--list-only`), so it needs no wave ramp; the ramp is ported in the
  `download` gate (map §4 D4). D4's *relevance here* is posture: Mizzou's sshd
  refusing a connection is an expected transient world condition, so an unreachable
  remote must **degrade to zero candidates plus one structured error event**
  (`detection.failed`) and never crash the cycle — the same fail-open shape as the
  D1 disk check (`daemon.py:485-493`) and PP's own return-`[]`
  (`poller.py:222-225`). Exception: a **`KeyError` from the lister propagates**
  (missing required config key = deployment bug, fail loud — consistent with
  `detect_new_runs`'s existing `Raises: KeyError` contract). Unit 2.
- **C2's lesson applied — no overloaded/implicit remote path.** PP hardcodes the
  remote path as `:.` (`poller.py:215`); a baked-in path is a production default in
  code. New required key **`remote_source_path`** (the operator sets `"."` for
  Mizzou), read by subscript, no fallback. Unit 5.
- **sshpass password-on-argv (subsystem-map security note, PP `:335`/`:491`).**
  Deliberate deviation from production: `sshpass -e` with the password in the
  `SSHPASS` environment variable of the child, **never on argv** (PP puts it on the
  process table of every rsync). Password still comes from `password_file`
  (stripped, PP `:158-169`), and it must never appear in any log line or error
  message. Units 5–6 pin this.
- **A5 (partial).** `size_tb` is computed from the listing (PP `:288`, `:303`) and
  stamped via `insert_run`'s existing `size_tb` parameter at detection — Will reads
  sizes, so the row carries it from birth. Thread-metadata display is the notifier
  increment's.
- **F1 (run identity).** `name` = the top-level remote folder name, exactly PP's
  grouping key (`poller.py:259`). Core additionally rejects any candidate name that
  is not a safe path component (unit 1) — a malicious/garbled listing must never
  mint a run named `..`.

**⚑ none.** No genuine user forks; the two deliberate deviations to eyeball at
review are `sshpass -e` (requires sshpass ≥1.06, standard) and the stricter
candidacy rule (manifest **and** `*001.fastq.gz`, per gate map §8 — stricter than
PP's `has_md5 and len(files) > 0` at `poller.py:301`, which a manifest-only folder
passes).

---

## 3. Units, in dependency order

Units 1–3 are core (`daemon.py`, tests appended to `tests/test_daemon.py`); units
4–6 are the adapter (`sluice/gates/detection.py`, tests in the new
`tests/test_detection.py`); unit 7 is the interlock + config example. All DB tests
run on real files under `tmp_path` via the existing `config`/`prepared` fixtures;
event assertions use the existing structlog-capture helpers
(`_events_named`). **No network and no real rsync anywhere in the suite**: units
1–3 use in-test fake listers; unit 6 monkeypatches `subprocess.run`.

### Unit 1 — the lister seam in `detect_new_runs` (+ `watch_dir_lister` extraction)

```python
def watch_dir_lister(config) -> list[tuple[str, float | None]]: ...
def detect_new_runs(config, *, lister=None, notifier=None) -> list[str]: ...
```

Behavior: extract the current subdirectory sweep (`daemon.py:216-218`) into
`watch_dir_lister` (returns `(name, None)` tuples, sorted; `KeyError` on missing
`watch_dir`). `detect_new_runs` calls `lister or watch_dir_lister` exactly once,
**before and outside any transaction** (a network listing inside a `write_txn`
would be the long-transaction sin); sorts candidates by **name only**
(`key=lambda c: c[0]` — never whole-tuple sort, which raises `TypeError` comparing
`None` with a float on a duplicate name); skips any candidate whose name fails the
existing `_is_safe_path_component` rule with one `detection.unsafe_name` error
event; then in ONE `write_txn` reads `metadata.pipeline_profile` (unchanged,
including the unseeded-`RuntimeError` guard) and calls
`insert_run(conn, name, pipeline_version=stamp, size_tb=size_tb)` per candidate.
Post-commit `run.detected` + `open_run_thread` choreography unchanged. `watch_dir`
is **only** read inside `watch_dir_lister` — an injected lister needs no
`watch_dir` key. Add `watch_dir_lister` to `__all__`.

Failing tests that define done:

- **Regression pin (no new test):** every existing detection/cycle test in
  `tests/test_daemon.py` (incl. `test_daemon_import_surface_is_closed`) passes
  **unmodified** — `ALLOWED_DAEMON_IMPORTS` is not edited; no new import lands in
  `daemon.py`.
- `test_detect_new_runs_uses_injected_lister_and_stamps_size_tb` — config copy
  **without** `watch_dir`; fake lister returns
  `[("runB", 1.25), ("runA", None), ("runB", 1.25)]` (duplicate deliberate);
  returns `["runA", "runB"]`; `runB` row has `size_tb == 1.25`, `runA` row NULL;
  both stamped with the seeded `metadata.pipeline_profile` value (read back, not a
  literal); exactly **one** `run.detected` event per name (the duplicate is
  silenced by `insert_run`'s idempotent bool); no `watch_dir` `KeyError`.
- `test_detect_new_runs_rejects_unsafe_candidate_names` — lister returns
  `[("../evil", None), ("ok_run", None)]`; returns `["ok_run"]`; one
  `detection.unsafe_name` event carrying the rejected name; `SELECT COUNT(*)`
  shows one row.
- `test_detect_new_runs_calls_lister_outside_any_write_transaction` — the fake
  lister itself opens `write_txn` on the same DB and writes (e.g.
  `set_metadata(conn, "probe", "x")`) before returning `[]`; the call succeeds and
  the probe key reads back. A `detect_new_runs` that held its write transaction
  across the lister call deadlocks/raises here — the short-transaction invariant,
  made executable.

### Unit 2 — failure containment (the D4 posture)

Behavior: wrap the lister call:

```python
try:
    candidates = active_lister(config)
except KeyError:
    raise                        # config bug — fail loud (existing contract)
except Exception as listing_error:
    log.error("detection.failed", **{
        "error.type": type(listing_error).__name__,
        "error.message": str(listing_error),
    })
    return []
```

Zero candidates, one error-level event, cycle proceeds. (Consequence, accepted: a
persistent world error — e.g. a typo'd `password_file` path — degrades every cycle
with a visible `detection.failed` line rather than killing the daemon; the D1
fail-open precedent.)

- `test_detection_failure_degrades_to_zero_candidates_and_one_event` — lister
  raises `RuntimeError("connection closed by remote")`; returns `[]`; exactly one
  `detection.failed` event with `error.type == "RuntimeError"`; zero runs rows.
- `test_detection_failure_does_not_stop_the_cycle` — seed one active run via the
  watch-dir path first; then `run_cycle` with a raising lister:
  `result["detected"] == []` and the pre-existing run appears in
  `result["runs"]` (the drive still happened).
- `test_detection_config_keyerror_still_fails_loud` — lister raises
  `KeyError("password_file")`; `detect_new_runs` propagates it.

### Unit 3 — passthrough on `run_cycle` / `run_forever`

Behavior: both gain keyword-only `lister=None`, handed unchanged to
`detect_new_runs`. Nothing else moves (pause head-check still short-circuits
detection; disk floor still runs before it).

- `test_run_cycle_passes_lister_through` — `run_cycle(prepared,
  registry=REFERENCE_PROFILES, lister=fake)` → `result["detected"]` names the
  fake's candidates and the rows exist.
- `test_run_forever_passes_lister_through` — `run_forever(..., max_cycles=1,
  lister=fake)` (existing run_forever test scaffolding) detects the fake's
  candidate.

### Unit 4 — `parse_rsync_listing` (pure parser, the PP port)

```python
def parse_rsync_listing(output: str) -> list[tuple[str, float]]:
```

Pure function, no logger, no I/O. Port of PP `poller.py:228-307` with the gate-map
§8 candidacy rule. Per line: split into 5 fields (`perms, size, date, time, path`);
skip blank/short lines and the `.` marker; a top-level `d`-entry registers a
folder; a top-level plain **file** is ignored (a loose fastq at the root is not a
run — PP's `else/continue` at `:264-274`); a nested `d`-entry is skipped (duplicate
downloads, PP `:261-263`); a nested file joins its top-level folder, its size
(comma-stripped `int`, unparsable skipped — PP `:287-290`) accumulated. A folder is
a **candidate** iff it has a checksum manifest (path lowercased endswith
`md5s.txt` or `.sha256` — PP `:292-296`) **AND** at least one file whose final
path component endswith `001.fastq.gz` (production's own `--include` filter, PP
`:196`; gate map §8 — *not* merely "any file", which is where PP `:301` is looser).
`size_tb = total_bytes / 1024**4` (PP `:303`). Return sorted by name.

Give the implementer this fixture shape (real `rsync --list-only -r` format) in the
test file:

```
drwxr-xr-x          4,096 2026/07/20 09:15:01 .
drwxr-xr-x          4,096 2026/07/20 09:15:01 RUN_A
-rw-r--r--  1,234,567,890 2026/07/20 09:20:44 RUN_A/S1_L001_R1_001.fastq.gz
-rw-r--r--  1,234,567,890 2026/07/20 09:21:03 RUN_A/S1_L001_R2_001.fastq.gz
-rw-r--r--          1,024 2026/07/20 09:22:00 RUN_A/md5s.txt
drwxr-xr-x          4,096 2026/07/20 09:23:00 RUN_A/subdir
-rw-r--r--            512 2026/07/21 10:00:00 stray_001.fastq.gz
```

- `test_parse_groups_by_top_level_folder_and_computes_size_tb` — the fixture yields
  exactly `[("RUN_A", pytest.approx((2 * 1_234_567_890 + 1024) / 1024**4))]`; the
  stray root file and the nested `subdir` entry produce nothing.
- `test_parse_requires_manifest_and_a_001_fastq` — parametrized: fastq-only folder
  → excluded; **manifest-only folder → excluded** (the stricter-than-PP rule);
  `.sha256` + fastq → included; `MD5S.TXT`/`.SHA256` (uppercase) + fastq →
  included (case-insensitivity, PP `:294`); a folder whose only fastq is
  `S1_R1.fastq.gz` (no `001`) → excluded.
- `test_parse_survives_noise` — blank lines, a 3-field line, a comma-grouped size,
  and one row with a non-numeric size field: no exception, size accumulation skips
  the bad row only.

### Unit 5 — `build_listing_command`

```python
def build_listing_command(config) -> tuple[list[str], dict[str, str]]:
```

Returns `(argv, env_overlay)`. **Local mode** iff `config.get("local_source_dir")`
is truthy (PP `:190` — presence check, not a value default; the example config
documents it as optional-or-empty): argv is exactly

```python
["rsync", "--list-only", "--include=*/", "--include=*001.fastq.gz",
 "--include=*md5s.txt", "--include=*.sha256", "--exclude=*", "-r",
 config["local_source_dir"] + "/"]
```

(trailing slash load-bearing — lists the directory's *contents* so top-level
entries are the run folders, PP `:201`), overlay `{}`. **Remote mode** otherwise:
`remote_user`, `remote_host`, `remote_source_path`, `password_file` all read by
subscript (`KeyError` = load error, no fallback); password =
`Path(password_file).read_text().strip()`; argv is exactly

```python
["sshpass", "-e", "rsync", "--list-only", "--include=*/",
 "--include=*001.fastq.gz", "--include=*md5s.txt", "--include=*.sha256",
 "--exclude=*", "-r", f"{remote_user}@{remote_host}:{remote_source_path}"]
```

overlay `{"SSHPASS": password}` — the argv-exposure fix over PP `:204-216`.

- `test_remote_command_is_pinned_and_password_never_on_argv` — write a password
  file containing `s3cret\n`; exact argv equality against the list above; overlay
  `== {"SSHPASS": "s3cret"}` (stripped); `"s3cret" not in " ".join(argv)`; `"-p"`
  not in argv. The exact-equality pin is what keeps `--list-only` and the three
  include filters from drifting — this argv IS the detection contract with the
  remote.
- `test_local_mode_command_lists_directory_contents` — config with
  `local_source_dir="/data/src"`: exact argv with trailing-slash source, overlay
  `{}`, and no `sshpass` element; a config with `local_source_dir=""` falls
  through to remote mode (the example-config "or empty" convention).
- `test_missing_remote_key_fails_loud` — parametrized over each of the four remote
  keys deleted in turn (with `local_source_dir` absent): `KeyError` naming it.

### Unit 6 — `list_remote_runs` (the thin subprocess adapter)

```python
class RemoteListingError(RuntimeError): ...
def list_remote_runs(config) -> list[tuple[str, float | None]]:
```

Behavior: `argv, overlay = build_listing_command(config)`;
`subprocess.run(argv, env={**os.environ, **overlay}, capture_output=True,
text=True, check=True)`; return `parse_rsync_listing(result.stdout)`. On
`CalledProcessError` raise
`RemoteListingError(f"remote listing failed (exit {e.returncode}): {e.stderr}")
from e` — stderr included (the operator's diagnostic), **argv never included**
(local mode aside, the command line is boring; the rule keeps any future slip of a
secret out of events, since this message becomes `detection.failed`'s
`error.message` via unit 2). The adapter is thin by design — parsing is unit 4's
tested territory; two behavioral tests only:

- `test_list_remote_runs_parses_subprocess_stdout` — monkeypatch `subprocess.run`
  with a fake capturing its arguments and returning the unit-4 fixture as stdout;
  result equals the parser's output; the fake received the unit-5 argv and an env
  containing `SSHPASS`.
- `test_list_remote_runs_wraps_failure_with_stderr_never_password` — fake raises
  `CalledProcessError(30, argv, stderr="Connection closed by 203.0.113.7")`;
  `RemoteListingError` message contains the stderr text and contains neither the
  password nor any argv element (`"sshpass" not in str(exc)`); `__cause__` is the
  `CalledProcessError`.

### Unit 7 — the go-live interlock + config example (mostly green-on-arrival)

No new production code beyond `config.example.json` gaining
`"remote_source_path": "<remote-path-to-list-usually-.>"` (placeholder only; the
four sibling keys already exist there). The interlock test pins the literal string
the map's caveat is about:

- `test_production_stamp_requires_registry_entry_at_startup` (in
  `tests/test_daemon.py`) — `prepare_database(config,
  registry=REFERENCE_PROFILES, default_pipeline_version="production-v3.1")` raises
  `UnknownProfileError`; a fresh connection shows zero `runs` rows and no seeded
  `metadata.pipeline_profile`. Green against the built `prepare_database` — its
  value is the literal `production-v3.1` pin: whoever adds the profile entry in a
  later increment flips this scenario from "dies at startup" to "live", and until
  then the composition root *cannot* wire production detection wrongly.
- `test_detection_module_is_list_only_and_core_clean` (in
  `tests/test_detection.py`) — `sluice/gates/detection.py` source assertions:
  every occurrence of the string `rsync` in a built argv is covered by the
  exact-equality tests, so here just: `"-avP" not in source` (detection never
  transfers — the resume-first boundary belongs to the download gate) and the
  module's imports include `sluice.core` nothing-at-all or core-only names — plus
  `__all__ == ["RemoteListingError", "build_listing_command", "list_remote_runs",
  "parse_rsync_listing"]` (the audit-plan surface-guard precedent).

---

## 4. Invariant checks

| Invariant | How this increment could violate it | Guard |
|---|---|---|
| DB single source of truth; short transactions | a network listing inside the insert `write_txn` | `test_detect_new_runs_calls_lister_outside_any_write_transaction` (unit 1) |
| State-only detection; daemon is the only actor; detection only LISTS | the lister transferring files or the adapter growing an `-avP` path | argv exact-equality pins `--list-only` (unit 5); `-avP`-absence source check (unit 7) |
| `gates → core`, never the reverse; core imports no `subprocess` | putting the rsync lister in core, or daemon importing the gates module | `tests/test_daemon.py::test_daemon_import_surface_is_closed` unmodified (unit 1 regression pin); `tests/test_dependency_arrows.py` auto-covers; the seam is injection-only |
| Version frozen at detection; no hardcoded pipeline version | detection stamping a literal `production-v3.1` | unit 1's stamp-equals-metadata assertion; the startup interlock `test_production_stamp_requires_registry_entry_at_startup` (unit 7) |
| No secrets in argv/logs/events | PP's `sshpass -p` port, or stderr wrapping leaking the command line | password-absent-from-argv (unit 5); error-message-contains-stderr-never-password/argv (unit 6) |
| No production defaults in code; missing key = load error | defaulting the remote path to `.`, or a watch_dir fallback in production mode | `remote_source_path` by subscript (unit 5, `test_missing_remote_key_fails_loud`); `KeyError` propagation through the containment catch (unit 2) |
| Per-cycle resilience — a broken world never kills the daemon | an unreachable remote crashing `run_cycle` | `test_detection_failure_does_not_stop_the_cycle` (unit 2) |
| Identity → path safety | a listing minting a run named `..` | `test_detect_new_runs_rejects_unsafe_candidate_names` (unit 1) |
| No file deletion (ADR-0005) | n/a — module writes nothing to disk | daemon ast pin (unchanged); adapter builds argv only |

---

## 5. Done criteria

1. `sluice/gates/detection.py` exists with the four-name `__all__`; docstring
   states it is a world adapter, not a Gate; `pixi run lint` clean; docstrings per
   ADR-0001 (`Raises:` exhaustive on `list_remote_runs` and
   `build_listing_command`).
2. `daemon.py` has the `lister` seam on `detect_new_runs`/`run_cycle`/
   `run_forever`, `watch_dir_lister` exported, containment per unit 2 — with
   `ALLOWED_DAEMON_IMPORTS` and every pre-existing test in `tests/test_daemon.py`
   **untouched and green**.
3. `tests/test_detection.py` green with **no network and no rsync executed** (fake
   listers; monkeypatched `subprocess.run`); the ~15 new tests across both files
   pass; full suite green.
4. `config.example.json` carries `remote_source_path` (placeholder only).
5. Observable end state: a composition root can wire
   `run_forever(..., lister=list_remote_runs)` and, with the reference registry,
   it **fails loudly at startup** if pointed at `production-v3.1` — while the
   Plan-A watch-dir behavior is byte-for-byte what it was when no lister is
   injected.

---

## 6. Carry-forwards (named, so no increment rediscovers them)

- **Increment 2 (CephFS disk strategy):** nothing from here; the disk-floor seam is
  already separate.
- **Increments 3–8 (profile + gates):** the `production-v3.1` registry entry flips
  unit 7's interlock scenario live — that increment should add the composed
  positive test (registry containing the profile + `lister=list_remote_runs` fake
  → run inserted stamped `production-v3.1`). The **`download` gate** owns
  `rsync -avP`, partial resume, D4's `download_wave_size`/
  `download_ramp_delay_seconds` ramp and `is_connection_blocked_error` — and
  should reuse the **`sshpass -e`/`SSHPASS`** shape established here (never `-p`;
  consider extracting a shared helper in `sluice/gates/` then, not now).
- **Notifier increment (A5):** `open_run_thread` grows `size_tb=`/`experiment_id=`
  arguments; detection already stamps `size_tb` on the row — read it from there,
  do not re-list.
- **Plan C (composition root / CLI):** wiring `lister=list_remote_runs` vs default
  watch-dir is a deployment decision made where the registry and notifier are
  assembled; `sluice doctor` should check the remote-mode key quartet and that
  `password_file` exists and is non-empty.
- **Known accepted behaviors (documented, not bugs):** a folder mid-upload is
  invisible until its manifest lands (the manifest is the completeness signal —
  inherited from PP); a persistent world error emits one `detection.failed` per
  cycle forever until fixed (fail-open, D1 posture); files added remotely *after*
  a run's `downloaded=1` are not re-fetched (PP-identical; rewind is the
  operator's `reset_gate`).
