# Plan 05 — `profile` subsystem (Plan A, subsystem 5 of 9)

**Module owned:** `sluice/core/profile.py` (single file) — the pipeline-profile model:
the ordered gate set plus per-gate applicability for a given `pipeline_version`.
**Build order:** db → gate (+ mock gates) → events → context → **profile** →
reconcile → audit → control → daemon
**Tests:** consolidated in `tests/test_profile.py` (one file per subsystem; db/gate/events/context precedent).

Ground truth: design of record **§8** (profile shape; `pipeline_version` frozen at
detection; disabled-is-satisfied; optional-runs-when-due-but-does-not-block), **§4**
(applicability is NOT on the gate — the gate is intrinsic, the profile is policy), **§7**
(`load_profile(run.pipeline_version)` in the loop; `reconcile.next_gate` is a pure
function of `(run_row, profile)`; "complete" = every required gate of the run's own
`pipeline_version` is satisfied); errata **B1** (mechanism), **B3**, B4 (cite ADR-0002),
B7 (deferred by name); subsystem map lines 397 (stamp-at-detection), 476/477/634 (order +
applicability), 544 (completion derived), 1045 (legacy stamping); ADR-0002 (gates never
write state; profiles never carry retry), ADR-0003 (layering yes, stdlib-only no); plan 02
(`Gate`/`validate_gates`/`MOCK_GATES`), plan 04 §7 (profile needs nothing from context);
the four DECIDED FACTS below; **⚑1/⚑2 both decided 2026-07-23 (see the sign-off
section)**.

---

## 1. Scope & non-goals

### Owned

| Thing | Public surface |
|---|---|
| Applicability vocabulary | `Applicability` — `REQUIRED` / `OPTIONAL` / `DISABLED` |
| The profile record | `Profile(pipeline_version, gates, applicability)` — frozen, validated |
| Authoring factory | `build_profile(pipeline_version, entries)` — from `(gate, applicability)` pairs |
| Completion-predicate seam | `required_gates(profile) -> tuple[Gate, ...]` |
| Registry resolver (the freeze) | `load_profile(pipeline_version, registry) -> Profile`; `UnknownProfileError` |
| The Plan A reference profiles (⚑2, decided: two) | `REFERENCE_PROFILE`, `REFERENCE_PROFILE_ALPHA_DISABLED`, `REFERENCE_PROFILES` (the two-entry registry) |

`profile.py` declares `__all__ = ["Applicability", "Profile", "REFERENCE_PROFILE",
"REFERENCE_PROFILES", "REFERENCE_PROFILE_ALPHA_DISABLED", "UnknownProfileError",
"build_profile", "load_profile", "required_gates"]` (9 names); unit 7 asserts set
equality (db unit-7 precedent).

**Allowed imports (closed set): stdlib + `sluice.core.gate` + `sluice.core.mock_gates`.**
Profile consumes `validate_gates`/`Gate` (plan 02) and references `MOCK_ALPHA`/`MOCK_BETA`
to build the reference profiles — both are core. It reads no config, opens no connection,
holds no clock, emits no event: a profile is pure data + pure functions over its inputs,
exactly like `resolve_context` and `reconcile.next_gate`. `sqlite3`, `sluice.core.db`,
`slack_sdk`, `structlog`, and any `sluice.gates`/`api`/`cli` are **forbidden by name**
(unit 1). The structural consequence is load-bearing: a module that cannot reach the DB
**cannot read `metadata.pipeline_profile`**, so it cannot silently re-resolve an in-flight
run's frozen `pipeline_version` against the active default — the freeze (§2 fact 1) is
guaranteed by what the module can even name.

### The four DECIDED FACTS (user-decided; locked, not reopened)

1. **`pipeline_version` is stamped at detection and keys everything.** `load_profile`
   resolves a run against the string already frozen in `runs.pipeline_version` — **never**
   against the current `metadata.pipeline_profile`. A run always completes under the
   definition it started with (§8, subsystem map:397).
2. **Gates never write state (ADR-0002).** Profiles describe which gates apply; they carry
   no retry/attempts field and no run-state handle.
3. **The Plan A reference profile covers the two mock gates.** `REFERENCE_PROFILE` is
   `pipeline_version="reference-v1"` over `MOCK_ALPHA`/`MOCK_BETA` — the profile that lets
   the daemon and control ops be tested end-to-end with no external services (§12).
   **⚑2 (DECIDED 2026-07-23, by the user) extends this:** core also ships a second
   variant, `REFERENCE_PROFILE_ALPHA_DISABLED` (`"reference-v1-alpha-disabled"`, same two
   mock gates, alpha `DISABLED`), so the full loop proves disabled-is-satisfied by
   consuming a shipped profile, not a test-built fixture.
4. **Lean surfaces; Plan B production profiles arrive with their gates.** No production
   gate name, column, or profile appears here (B1 not pre-empted).

Plus the user constraint carried from plan 04: **no `/scratch` and no path defaults
anywhere** (CHTC has no `/scratch`). The profile subsystem holds no path and reads no
config, so it inherits this trivially — but it never introduces one either.

### Explicitly NOT owned (and where it belongs)

- **`reconcile.next_gate`, the selection algorithm, and the completion predicate that
  combines the profile with run state** — **subsystem 6 (`reconcile`)**. The profile is a
  pure function of the *profile* (no run row); reconcile is the pure function of
  `(run_row, profile)`. The profile provides the data (`gates` in order, the
  `applicability` map, `required_gates`); reconcile does the state-combining decisions
  (spec §7). See the named carry-forwards in §7 — in particular **the treatment of
  `disabled`/`optional`** (the profile *records* applicability; reconcile *implements*
  "disabled is treated as already-satisfied" and "optional runs when due").
- **Stamping `pipeline_version` at detection; reading/seeding `metadata.pipeline_profile`
  (the active default); assembling the registry from reference + production profiles** —
  **subsystem 9 (`daemon`)** and the outer layer that imports both `gates` and `core`.
  Core cannot import `sluice.gates`, so it cannot build a registry that names production
  profiles; the daemon injects the assembled registry into `load_profile` (the §7 seam).
- **Config loading / any config key** — the profile draws **nothing** from config.
  `config.example.json` has no profile key (verified); profiles are code-defined records,
  and the active-default id lives in `poller_metadata.pipeline_profile` (DB state written
  by subsystem 8/9), not in config. Named in §7.
- **Production gates, production profiles, the 6-vs-8-gate / registration-meaning /
  experiment-gate questions (B1/B2)** — **Plan B**. The profile mechanism established here
  is B1's *resolution vehicle* (§2 B1); the concrete profiles are Plan B's.
- **Writing any `runs`/`poller_metadata` state** — the profile is read-only over its
  inputs; it holds no connection.

---

## 2. Errata & policy resolutions in scope — decisions taken, no TBDs

### B1 — applicability is the mechanism; the production content is deferred. RESOLVED (mechanism).

Errata B1's live production knowledge is that today's per-step switches — `nvd_auto_run`
(PP `poller.py:1229`), `labkey_enabled` (`poller.py:1469`),
`labkey_auto_register_enabled` (`poller.py:1190`), `labkey_metadata_check_enabled`
(`poller.py:1203`) — are **global config booleans checked inside `run`**, and that
`nvd_auto_run: false` makes a run **loop forever** because `pipeline_completed` is never
set (`poller.py:1229-1237`; subsystem map:634). Spec §8's fix is to model these as
**profile `disabled` applicability** (subsystem map:477), where a disabled gate is treated
as already-satisfied and therefore does not block completion. This subsystem **builds that
mechanism** (`Applicability.DISABLED`, and `required_gates` excluding disabled gates so a
disabled gate cannot block "complete") and **deliberately pre-empts none of B1's content**:
no production gate, no `nvd`/`labkey` name, no 6-vs-8 decision appears here (fact 4).
Carried forward by name (§7): Plan B expresses each config switch as a profile that marks
the gate `disabled`, **not** as `if config.get(...)` inside `run`.

Corollary the engineer must not trip on — **disabling ≠ omitting.** To turn a step off,
a profile keeps the gate in its ordered set with `Applicability.DISABLED`; it does **not**
drop the gate id from the list. `validate_gates` enforces needs-closure (a `need` must
reference a gate *in the set*), so omitting a gate that a downstream gate needs raises at
construction. This matches §8 exactly ("Removing a step … = a profile that marks that gate
`disabled`"). Reconcile then treats the disabled gate as satisfied so its dependents
unblock (§7 carry-forward). The shipped `REFERENCE_PROFILE_ALPHA_DISABLED` (⚑2, unit 6)
is this corollary's living exemplar: alpha is disabled yet stays in the gate set, so
beta's `needs=("mock_alpha",)` still resolves.

### B3 — `Gate.order` is TRUE execution order; the guard extends into profiles. RESOLVED.

`Gate.order` correctness is mechanically enforced by `validate_gates` (plan 02 unit 4:
duplicate ids/columns/orders, unknown `need`, a `need` that does not strictly precede its
needer — the trap of transcribing PP's `PIPELINE_GATE_COLUMNS`, `poller_state_db.py:32-42`,
which lists download before the registration that must run first). **`Profile.__post_init__`
runs every profile's gate set through `validate_gates`** (unit 2), so a B3-deadlocking
profile fails **at profile construction**, not at 2am in the daemon. Plan B's production
profiles inherit this guard automatically — a bad order transcription becomes a failed test,
never a wedged run.

### B4 — retry/quarantine. Cited, not reopened (ADR-0002).

Every gate failure quarantines (ADR-0002 decision 1). The `Profile` record carries **no**
`retryable`/`max_attempts`/applicability-retry field — the fields-equality test (unit 2)
makes adding one a visible contract change. Applicability is `required｜optional｜disabled`
and nothing else (unit 1 closed vocabulary). If retry is ever wanted it arrives as a new
ADR, never as a quiet profile field.

### The `pipeline_version` freeze, made structural. RESOLVED (decided fact 1).

`load_profile(pipeline_version, registry)` (unit 5) resolves **by the run's stamped version
string** and **fails loud** (`UnknownProfileError`) on a version the registry does not
carry — it **never** falls back to the active default. Two guards make the freeze real:
(a) load_profile has no default registry and no fallback branch (unit 5); (b) the module
structurally cannot reach `metadata.pipeline_profile` at all (unit 1 import hygiene — no
`sqlite3`, no `sluice.core.db`). This is the whole point of freezing at detection: changing
`metadata.pipeline_profile` later must never silently reinterpret an in-flight run (§8).

### Legacy / backfilled `pipeline_version`. Deferred by name (subsystem map:1045).

The subsystem map's open question — what version do historical rows get stamped with — has
**no Plan A instance** (greenfield DB + drain; no state migration, HANDOFF §5). Plan A's
consequence is only that `load_profile` is fail-loud, so any row bearing a version absent
from the injected registry raises. Carried forward (§7): if Plan B ever imports historical
rows it must stamp a real profile id **or** register a reserved profile (e.g. `legacy-v0`
marking every gate satisfied); it may not rely on a silent fallback, because there is none.

---

## 3. The contract, authoritative

### 3.1 Surface

```python
# sluice/core/profile.py — imports: stdlib + sluice.core.gate + sluice.core.mock_gates ONLY

class Applicability(enum.Enum):
    REQUIRED = "required"   # must be satisfied for the run to be complete (§7/§8)
    OPTIONAL = "optional"   # runs when due, but does NOT block completion (§8)
    DISABLED = "disabled"   # treated as already-satisfied; never runs, never blocks (§8)

@dataclasses.dataclass(frozen=True)
class Profile:
    pipeline_version: str                            # the id runs.pipeline_version references
    gates: tuple[Gate, ...]                          # execution order (validate_gates)
    applicability: Mapping[str, Applicability]       # gate.id -> applicability, read-only
    # __post_init__ (order matters):
    #   1. gates = validate_gates(gates)  -> stored ascending by Gate.order (B3, dup guards)
    #   2. set(applicability) == {g.id for g in gates}  exactly, else ValueError naming the
    #      missing / stray gate id  (a gate reconcile cannot classify, or a typo'd id)
    #   3. every applicability value is an Applicability member, else TypeError
    #   4. applicability = MappingProxyType(dict(applicability))  -> read-only snapshot

class UnknownProfileError(KeyError):
    """load_profile was asked for a pipeline_version the registry does not carry."""

def build_profile(pipeline_version: str,
                  entries: Iterable[tuple[Gate, Applicability]]) -> Profile: ...

def required_gates(profile: Profile) -> tuple[Gate, ...]: ...

def load_profile(pipeline_version: str, registry: Mapping[str, Profile]) -> Profile: ...

REFERENCE_PROFILE: Profile           # build_profile("reference-v1",
                                     #   [(MOCK_ALPHA, REQUIRED), (MOCK_BETA, REQUIRED)])
REFERENCE_PROFILE_ALPHA_DISABLED: Profile   # ⚑2 (decided): build_profile(
                                     #   "reference-v1-alpha-disabled",
                                     #   [(MOCK_ALPHA, DISABLED), (MOCK_BETA, REQUIRED)])
REFERENCE_PROFILES: Mapping[str, Profile]   # both, each keyed by its own pipeline_version
```

Functional shaping (project preference): a **frozen dataclass** plus **module-level
functions** — no methods, no builder class. `build_profile`, `required_gates`,
`load_profile` are all pure `(...) -> ...`, the same shape as the control ops and
`reconcile.next_gate`. Validation lives at the record boundary (`Profile.__post_init__`),
so **any** `Profile` is coherent however constructed — `build_profile` is a thin splitter
that delegates every check to the record (the same layering as `Gate.__post_init__` +
`validate_gates`, plan 02).

Deliberate non-fields, stated so nobody adds them: no `config` (profiles are code-defined,
fact 4 / §1), no run-state field, no retry field (B4/ADR-0002), no notifier. Unit 2's
fields-equality test makes each a conscious contract change.

### 3.2 `pipeline_version` naming, the registry seam, and registry coherence

`Profile.pipeline_version` is deliberately the **same term** as the `runs.pipeline_version`
column that references it and the `metadata.pipeline_profile` key that names the active
default — a profile **is** the pipeline-version definition (§8). The registry is
`{pipeline_version: Profile}`; `REFERENCE_PROFILES` carries both shipped profiles, each
keyed by its own `pipeline_version` (unit 6) — the seam `load_profile` keys on and the
daemon extends. `"reference-v1"` is the established cross-subsystem constant: the db plan's
examples stamp `insert_run(..., pipeline_version="reference-v1")` and seed
`metadata.pipeline_profile = "reference-v1"` (db plan units 9/13); the ⚑2 variant id
`"reference-v1-alpha-disabled"` follows the same convention, naming its configuration in
the id. Seeding that metadata is subsystem 9's job (§7); this subsystem only defines the
profiles whose ids match it.

**Registry coherence is not assumed — it is checked.** `load_profile` is not a bare
`registry[key]`: after resolving, it verifies `resolved.pipeline_version == the lookup key`
and raises `ValueError` otherwise (unit 5). Without this, an injected entry whose key does
not equal its profile's own id silently resolves every run bearing that key against the
**wrong definition** — a class of bug the freeze exists to prevent. This is the per-run
backstop; the *duplicate-key* half (a production profile keyed `"reference-v1"` shadowing
the reference at merge time) is caught earlier, at registry assembly, by the daemon (§7).

### 3.3 What `required_gates` is (and is not)

`required_gates(profile)` returns `tuple(g for g in profile.gates if
profile.applicability[g.id] is Applicability.REQUIRED)` — the required subset, in execution
order, a **pure function of the profile with no run row**. It is the *completion-predicate
seam*: reconcile's "complete" is `all(row[g.satisfied_column] for g in
required_gates(profile))` (§7 "every required gate … is satisfied"), which reconcile
composes by reading run state — that composition is reconcile's, not the profile's. An
**all-optional / all-disabled** profile yields `required_gates(profile) == ()`, so
reconcile's `all([])` is vacuously true (complete at detection). That edge is real and
legitimate; reconcile must handle it (§7). Reconcile reads `profile.applicability[gate.id]`
directly for its per-gate `required｜optional｜disabled` selection decision — the map is
public, validated-complete, and read-only, so the lookup never misses for a gate in
`profile.gates`.

---

## 4. Units, in dependency order (7 units)

Every unit is one RED → GREEN → REFACTOR cycle starting from the named failing test —
**except unit 7**, which adds no production code and is green-on-arrival by design
(integration/surface tests pinning the composed behavior of units 1–6; the plan-02 unit-9
carve-out convention). `__all__` grows as names land — unit 1 declares
`__all__ = ["Applicability"]`, each unit extends it, unit 7 asserts the closed equality.
Tests live in `tests/test_profile.py`. No shared `make_profile` helper: profiles are cheap
to build inline with `build_profile` and the mock gates, and each test names exactly the
applicability shape its behavior is about. Every unit's code follows ADR-0001 (Google
docstrings, one why-paragraph citing §/errata, constraints not justifications).

---

### Unit 1 — `Applicability` + module bootstrap + import hygiene

RED: `tests/test_profile.py` fails at `import sluice.core.profile` (module absent).

**Tests**
- `test_applicability_vocabulary_is_the_spec_three` —
  `{a.name: a.value for a in Applicability} == {"REQUIRED": "required", "OPTIONAL":
  "optional", "DISABLED": "disabled"}`. Equality, not subset: the three are the §8
  vocabulary and the closed set is itself the B1/B4 surface — no fourth applicability, no
  retry tier, creeps in.
- `test_profile_is_core_clean_by_import` — ast-parse `sluice/core/profile.py`: every
  `Import`/`ImportFrom` root is in `sys.stdlib_module_names` or is `sluice`; every
  `sluice.*` import is exactly `sluice.core.gate` or `sluice.core.mock_gates`; and —
  asserted **by name** so the message teaches — none of `sqlite3`, `sluice.core.db`,
  `slack_sdk`, `structlog`, `sluice.gates`, `sluice.api`, `sluice.cli` appears. (Technique
  of `test_db_imports_only_stdlib` / `test_events_is_core_clean_by_import` /
  `test_context_is_stdlib_only_and_db_free`; `tests/test_dependency_arrows.py`
  auto-discovers the new module via `pkgutil.walk_packages` — no edit there.) **This one
  test carries the freeze's structural backstop:** a module that cannot name `sqlite3` or
  `sluice.core.db` cannot read `metadata.pipeline_profile`, so it cannot re-resolve an
  in-flight run's frozen version against the active default (§2 fact 1).

---

### Unit 2 — `Profile` record + `__post_init__` validation

**Tests**
- `test_profile_fields_are_exactly_pipeline_version_gates_applicability` —
  `[f.name for f in dataclasses.fields(Profile)] == ["pipeline_version", "gates",
  "applicability"]`, all required (no defaults). Carries fact 4 (LEAN — no config, no
  run-state field) and B4 (no retry field): editing it is a conscious contract change.
- `test_profile_is_frozen` — assigning to `Profile.gates` on an instance raises
  `dataclasses.FrozenInstanceError`. The DB and the profile registry are the truth; a
  profile must not become a second mutable in-flight authority (spine).
- `test_profile_runs_gates_through_validate_gates` — **the B3 extension.** Construct a
  `Profile` whose gate set transcribes the PP trap: a `download`-shaped gate at `order=1`
  with `needs=("registration",)` and a `registration`-shaped gate at `order=2` → the
  constructor raises `ValueError` naming both (propagated from `validate_gates`). Docstring
  cites errata B3 (`PIPELINE_GATE_COLUMNS`, PP `poller_state_db.py:32-42`): a deadlocking
  profile fails at construction, not at daemon runtime.
- `test_profile_stores_gates_in_execution_order` — build with the gates passed
  **reversed** (`gates=(MOCK_BETA, MOCK_ALPHA)`); `profile.gates == (MOCK_ALPHA,
  MOCK_BETA)` (validate_gates sorted them by `order`). Reconcile consumes execution order,
  so a registry author's listing order must not leak into scheduling.
- `test_profile_applicability_must_cover_exactly_the_gates` — parametrized: (a) a gate in
  `gates` with **no** entry in `applicability` → `ValueError` naming that gate id (a gate
  reconcile could not classify); (b) an `applicability` key for a gate **not** in `gates`
  → `ValueError` naming the stray id (a typo'd gate id). Fail loud at construction, both
  directions.
- `test_profile_rejects_a_non_applicability_value` — an `applicability` value of the bare
  string `"required"` (not `Applicability.REQUIRED`) → `TypeError`. Reconcile branches with
  `is` on the enum; a bare string mis-branches at selection time (mirror `GateResult`'s
  outcome type check, plan 02 unit 2).
- `test_profile_applicability_is_read_only` — mutating `profile.applicability["mock_alpha"]
  = Applicability.DISABLED` (and `del`) raises `TypeError` (`MappingProxyType`); and
  mutating the **original** dict passed to the constructor does not change
  `profile.applicability` (the `dict(...)` snapshot). A live shared applicability map would
  let one holder silently re-classify a gate for every other — a mutable in-memory
  authority the spine forbids (mirror `RunContext.config`, plan 04 unit 1).

---

### Unit 3 — `build_profile` (the authoring factory)

`build_profile(pipeline_version, entries)` splits `(gate, applicability)` pairs into the
two parallel collections `Profile` stores, then constructs `Profile` (which validates).
The pairs form is the authoring convenience: keeping a gate and its applicability
**adjacent** prevents the "gates list and applicability map drift out of sync" bug that two
separate arguments invite.

**Tests**
- `test_build_profile_splits_pairs_into_gates_and_applicability` —
  `p = build_profile("reference-v1", [(MOCK_ALPHA, Applicability.REQUIRED), (MOCK_BETA,
  Applicability.OPTIONAL)])`; `p.pipeline_version == "reference-v1"`,
  `p.gates == (MOCK_ALPHA, MOCK_BETA)`, `dict(p.applicability) == {"mock_alpha":
  Applicability.REQUIRED, "mock_beta": Applicability.OPTIONAL}`.
- `test_build_profile_rejects_a_duplicate_gate` — two pairs both naming `MOCK_ALPHA` →
  `ValueError` (propagated from `validate_gates`' duplicate-id check). The load-bearing
  part: the naive `{g.id: a for g, a in entries}` applicability dict would silently drop one
  of the duplicates and hide the bug — `validate_gates` on the `gates` tuple catches the
  duplicate gate, so `build_profile` cannot smuggle a collapsed set past construction.

---

### Unit 4 — `required_gates`

**Tests**
- `test_required_gates_returns_only_required_in_order` — parametrized over three profiles
  built with `build_profile` over the mock gates: (ALPHA REQUIRED, BETA OPTIONAL) →
  `(MOCK_ALPHA,)`; (ALPHA REQUIRED, BETA REQUIRED) → `(MOCK_ALPHA, MOCK_BETA)` in execution
  order; (ALPHA DISABLED, BETA REQUIRED) → `(MOCK_BETA,)` — a **disabled** gate is not
  required, so it can never block completion (§8/B1). The completion-predicate seam
  reconcile composes: `all(row[g.satisfied_column] for g in required_gates(profile))`.
- `test_required_gates_is_empty_for_an_all_optional_profile` — a profile with both mock
  gates `OPTIONAL` (and, separately, both `DISABLED`) → `required_gates(profile) == ()`.
  The vacuous-completion edge: reconcile's `all([])` is true → the run is complete at
  detection. Pins the edge as a fact reconcile must handle (§7 carry-forward), not an
  accident.

---

### Unit 5 — `load_profile` + `UnknownProfileError` (the freeze enforcement + registry coherence)

`load_profile(pipeline_version, registry)` returns `registry[pipeline_version]`, raising
`UnknownProfileError` if absent — **no default registry, no fallback branch** — and raising
`ValueError` if the resolved profile's own `pipeline_version` does not equal the lookup key
(a mis-assembled registry that would resolve runs against the wrong definition, §3.2). Its
registries are built **inline** here via `build_profile` over the mock gates (available
since unit 3); this keeps the RED honest — unit 5 depends on nothing from unit 6, so a test
failing here is a real behavioral RED, not an `ImportError` for a name that has not landed.

**Tests**
- `test_load_profile_resolves_by_version_string` — build two registries inline:
  `ref = build_profile("reference-v1", [(MOCK_ALPHA, Applicability.REQUIRED), (MOCK_BETA,
  Applicability.REQUIRED)])`, `other = build_profile("other-v1", [(MOCK_ALPHA,
  Applicability.OPTIONAL), (MOCK_BETA, Applicability.OPTIONAL)])`,
  `registry = {"reference-v1": ref, "other-v1": other}`;
  `load_profile("reference-v1", registry) is ref`. Resolves by the stamped version, from
  the injected registry.
- `test_load_profile_fails_loud_on_an_unknown_version` — **the freeze.** With
  `registry = {"reference-v1": build_profile("reference-v1", [(MOCK_ALPHA,
  Applicability.REQUIRED), (MOCK_BETA, Applicability.REQUIRED)])}`,
  `load_profile("ghost-v9", registry)` raises `UnknownProfileError`; `isinstance(exc,
  KeyError)` is true (mirror `UnknownColumnError`; caller hazard — a bare `except KeyError`
  around `load_profile` swallows it, and the daemon must catch it explicitly to quarantine
  the run per §7); the message contains `"ghost-v9"` and lists the known ids. It **never**
  returns the active-default profile: an unknown version is an error, not a silent
  reinterpretation (§8; guardrail §8 no-silent-fallback).
- `test_load_profile_rejects_a_key_id_mismatch` — **registry coherence (§3.2).** A registry
  `{"mislabeled": build_profile("reference-v1", [(MOCK_ALPHA, Applicability.REQUIRED),
  (MOCK_BETA, Applicability.REQUIRED)])}` — the key `"mislabeled"` does not equal the
  profile's own `pipeline_version` `"reference-v1"`. `load_profile("mislabeled", registry)`
  raises `ValueError` naming **both** the lookup key and the profile's id. Without this a
  run stamped `"mislabeled"` would resolve to a profile that is not its definition; the
  daemon's merge (§7) still needs its own same-id-collision guard, which this per-run check
  cannot see.
- `test_load_profile_requires_an_explicit_registry` —
  `list(inspect.signature(load_profile).parameters) == ["pipeline_version", "registry"]`,
  and `registry` has no default. No production default baked in (guardrail §8), and the
  explicit registry is the injection seam: core cannot import `sluice.gates`, so the daemon
  must assemble and pass the registry (§7). Editing this signature to add a default is a
  conscious change that would let a Plan B daemon silently resolve only reference profiles.

---

### Unit 6 — `REFERENCE_PROFILE` + `REFERENCE_PROFILE_ALPHA_DISABLED` + `REFERENCE_PROFILES` (⚑2: two shipped profiles)

The disabled variant disables **`MOCK_ALPHA` — the *needed* gate — not beta**,
deliberately: a required beta whose `needs=("mock_alpha",)` points at a disabled gate
exercises **both** halves of disabled-is-satisfied through the full loop (reconcile must
unblock beta from *applicability*, since alpha's column stays `0`; and completion is
beta-only), where disabling the terminal beta would prove only the completion half.
`MOCK_BETA.run` reads its instruction file, never alpha's marker (plan 02 §3.3), so the
variant runs clean with alpha never executed.

**Tests**
- `test_reference_profile_covers_the_two_mock_gates_both_required` —
  `REFERENCE_PROFILE.pipeline_version == "reference-v1"`;
  `REFERENCE_PROFILE.gates == MOCK_GATES` (`== (MOCK_ALPHA, MOCK_BETA)`, execution order);
  `required_gates(REFERENCE_PROFILE) == MOCK_GATES` (both **required**). Decided fact 3 /
  design §12: the reference profile runs both mock gates to completion, proving the contract
  end-to-end with no external services.
- `test_reference_disabled_variant_keeps_the_disabled_gate_in_the_set` — **⚑2's exemplar;
  disabling ≠ omitting (§2 B1).**
  `REFERENCE_PROFILE_ALPHA_DISABLED.pipeline_version == "reference-v1-alpha-disabled"`;
  `REFERENCE_PROFILE_ALPHA_DISABLED.gates == MOCK_GATES` — alpha is **still in the gate
  set** (needs-closure: beta's `needs=("mock_alpha",)` resolves; dropping alpha instead
  would raise in `validate_gates`);
  `REFERENCE_PROFILE_ALPHA_DISABLED.applicability["mock_alpha"] is Applicability.DISABLED`
  and `["mock_beta"] is Applicability.REQUIRED`;
  `required_gates(REFERENCE_PROFILE_ALPHA_DISABLED) == (MOCK_BETA,)` — the disabled gate
  is excluded from the completion predicate while remaining in the profile's gate set.
- `test_reference_registry_is_keyed_by_pipeline_version` —
  `dict(REFERENCE_PROFILES) == {"reference-v1": REFERENCE_PROFILE,
  "reference-v1-alpha-disabled": REFERENCE_PROFILE_ALPHA_DISABLED}` — **both shipped
  profiles coexist under distinct keys** — and
  `all(key == profile.pipeline_version for key, profile in REFERENCE_PROFILES.items())`,
  so every entry passes `load_profile`'s registry-coherence check (§3.2): a key/id
  mismatch (which `load_profile` rejects) cannot originate from the reference registry.

---

### Unit 7 — surface + real-`sqlite3.Row` integration (green-on-arrival)

Adds no production code except the final `__all__`; the RED start is the `__all__` equality
test (units 1–6 grew it as names landed).

**Tests**
- `test_profile_exports_exactly_the_documented_surface` —
  `set(sluice.core.profile.__all__)` equals the §1 nine-name set (scope guard).
- `test_reference_profile_resolves_from_a_real_run_row` — `initialize_database(tmp)`;
  `insert_run(conn, "r1", pipeline_version="reference-v1")` (the version the daemon stamps
  from `metadata.pipeline_profile`, db plan); `row = get_run(conn, "r1")` (a real
  `sqlite3.Row`); `load_profile(row["pipeline_version"], REFERENCE_PROFILES) is
  REFERENCE_PROFILE`. **The load-bearing seam:** the string frozen into
  `runs.pipeline_version` resolves to the reference profile off a real row, against the real
  schema's column name — a drift in either fails here, not in the daemon. The **variant
  leg** (⚑2): `insert_run(conn, "r3", pipeline_version="reference-v1-alpha-disabled")`
  resolves to `REFERENCE_PROFILE_ALPHA_DISABLED` the same way — two runs frozen to
  different shipped definitions coexist in one DB, each resolving to its own. Then the
  **freeze leg**: `insert_run(conn, "r2", pipeline_version="drifted-v9")`;
  `load_profile(get_run(conn, "r2")["pipeline_version"], REFERENCE_PROFILES)` raises
  `UnknownProfileError` — a run frozen to a version the registry does not carry fails loud,
  never silently reinterpreted against `reference-v1`. (The test imports `sluice.core.db`;
  the *module under test* still does not — unit 1 stands.)

---

## 5. Invariant checks

| Invariant | How this subsystem could violate it | Guarding test |
|---|---|---|
| **Applicability lives in the profile, not the gate** (§4) | an applicability field on `Gate`; the profile not owning the vocabulary | plan 02 unit 3 fields-equality (inherited, `Gate` has none) + `test_applicability_vocabulary_is_the_spec_three` (unit 1) + the `applicability` field of `Profile` (unit 2) |
| **`pipeline_version` frozen at detection; never re-resolved against the active default** (§8, fact 1) | `load_profile` falling back to the default; reading `metadata.pipeline_profile` | `test_load_profile_fails_loud_on_an_unknown_version` (unit 5) + `test_profile_is_core_clean_by_import` (unit 1: structurally cannot reach the DB) + the freeze leg of `test_reference_profile_resolves_from_a_real_run_row` (unit 7) |
| **A run resolves to its OWN definition, never a mis-keyed one** (§3.2) | a registry entry whose key ≠ its `Profile.pipeline_version` | `test_load_profile_rejects_a_key_id_mismatch` (unit 5, per run) + `test_reference_registry_is_keyed_by_pipeline_version` (unit 6, both shipped entries) + the daemon's merge-collision guard (§7, at assembly) |
| **`gates → core`; core imports with gates absent** | importing `sluice.gates`/api/cli or third-party | `test_profile_is_core_clean_by_import` (unit 1); `tests/test_dependency_arrows.py` auto-covers the new module |
| **`Gate.order` is TRUE execution order (B3), enforced for every profile** | a profile transcribing `PIPELINE_GATE_COLUMNS` order | `test_profile_runs_gates_through_validate_gates` (unit 2) |
| **A profile is a coherent, needs-closed gate set; disabling ≠ omitting** (§2 B1) | omitting a needed gate; a gate with no applicability; a stray applicability id | `validate_gates` via unit 2 (needs-closure) + `test_profile_applicability_must_cover_exactly_the_gates` (unit 2) + `test_reference_disabled_variant_keeps_the_disabled_gate_in_the_set` (unit 6, the shipped exemplar) |
| **No production defaults baked in; profile draws no config** (guardrail §8, fact 4) | a default registry; a config-derived profile | `test_load_profile_requires_an_explicit_registry` (unit 5) + no-config import hygiene (unit 1) |
| **No mutable in-memory authority; profile frozen, applicability read-only** (spine) | `frozen=False`; a live shared applicability map | `test_profile_is_frozen`, `test_profile_applicability_is_read_only` (unit 2) |
| **Reconcile stays a pure function of `(run_row, profile)`** (§7) | the profile providing impure/ambiguous/unordered data | validate_gates ordering + `is`-branch-safe enum values (unit 2); `required_gates` purity (unit 4) |
| **No retry field / all-failures-quarantine** (B4/ADR-0002) | `retryable`/`max_attempts` on `Profile`; a fourth applicability | fields-equality (unit 2) + closed `Applicability` (unit 1) |
| **DB is the single truth; profile writes no state, holds no connection** | a builder taking a connection/db path | unit 1 import hygiene (structural — no `sqlite3`/db) |

---

## 6. Done criteria

1. `pixi run test` fully green: every pre-existing test (`tests/test_dependency_arrows.py`
   now auto-covering `sluice.core.profile`, `tests/test_guard_hook.py`, db/gate/events/
   context suites) plus the new `tests/test_profile.py`.
2. `sluice/core/profile.py` imports only stdlib + `sluice.core.gate` +
   `sluice.core.mock_gates`; `sqlite3`, `sluice.core.db`, `slack_sdk`, `structlog`, and any
   `sluice.gates`/`api`/`cli` appear nowhere in it — so it structurally cannot read
   `metadata.pipeline_profile`.
3. `dataclasses.fields(Profile)` is exactly `["pipeline_version", "gates", "applicability"]`
   — no config, no run-state, no retry field; the record is frozen and its `applicability`
   is read-only; every profile runs its gates through `validate_gates`, so a B3 order
   transcription raises at construction.
4. `Applicability` is exactly `{required, optional, disabled}` — a closed vocabulary.
5. `load_profile` resolves by the run's stamped `pipeline_version` against an
   explicitly-injected registry, fails loud (`UnknownProfileError`) on an unknown version —
   never a fallback to the active default — and rejects (`ValueError`) a registry whose key
   does not equal its `Profile.pipeline_version`. **Two** shipped reference profiles (⚑2)
   resolve off real `sqlite3.Row`s: `REFERENCE_PROFILE` (`"reference-v1"`, both mock gates
   required) and `REFERENCE_PROFILE_ALPHA_DISABLED` (`"reference-v1-alpha-disabled"`,
   alpha disabled yet still in the gate set, `required_gates == (MOCK_BETA,)`); both
   coexist in `REFERENCE_PROFILES` under distinct keys, each key equal to its profile's
   id; a row stamped with an unregistered version raises.
6. `build_profile` authors a profile from `(gate, applicability)` pairs; `required_gates`
   returns the required subset in execution order and `()` for an all-optional/all-disabled
   profile.
7. Downstream can start: subsystem 6 (`reconcile`) gets `Profile` + `required_gates` + the
   disabled/optional-treatment, completion, and vacuous-completion carry-forwards;
   subsystem 9 (`daemon`) gets `load_profile` + the two-profile `REFERENCE_PROFILES`
   (including the shipped disabled variant for its full-loop disabled-is-satisfied proof —
   no test-built fixture needed) + the stamp-at-detection, registry-assembly (with
   merge-collision detection), and quarantine-on-`UnknownProfileError` obligations; Plan B
   gets the applicability-models-config-switches resolution (B1), the `reap`-gate (B7),
   and the legacy-stamping carry-forwards (§7).

---

## 7. Carry-forwards (named, so no plan rediscovers them)

- **Subsystem 6 (reconcile) — the treatment of the applicability the profile records.**
  The profile *records* `required｜optional｜disabled`; reconcile *implements*:
  - **`disabled` is treated as already-satisfied** for selection (skip it), for
    **needs-satisfaction** (a gate that `needs` a disabled gate is unblocked — because a
    disabled gate stays in `profile.gates` with column `0`, reconcile must satisfy the need
    from *applicability*, not the column), and for completion (disabled never blocks). §8.
    The shipped `REFERENCE_PROFILE_ALPHA_DISABLED` (⚑2, unit 6) is the ready-made test
    substrate: beta required, its needed alpha disabled.
  - **`optional` "runs when due but does not block completion"** — reconcile defines "due"
    (spec §7 "a due optional"). **⚑1 (APPROVED 2026-07-23): the required-needs-optional
    deadlock rule is decided at reconcile, jointly with this definition** — whether a
    REQUIRED gate may depend on an OPTIONAL gate is coupled to "due"; the profile does
    **not** pre-empt it, and reconcile carries the deadlock risk explicitly.
  - **Completion** is `all(row[g.satisfied_column] for g in required_gates(profile))`,
    evaluated against the run's own stamped `pipeline_version` (§7 "every required gate of
    the run's own `pipeline_version`"), and it must handle the vacuous case
    `required_gates(profile) == ()` → complete (unit 4). `next_gate is None` ⇔ complete
    (plan 02 §7). The completion write (`completed_at`) precedes `emit_run_event` (plan 03
    §7 persist-then-emit), and per errata **C4** must never stamp complete over a gate that
    was *skipped* rather than *satisfied* (plan 02 §2 C4).
  - Reads `profile.gates` (ordered) and `profile.applicability[gate.id]` directly (the map
    is public, validated-complete, read-only); consumes `required_gates`. Cite this plan;
    do not re-decide the profile shape or reopen B4.
- **Subsystem 9 (daemon) — resolution, injection, and stamping.**
  - **Stamps `runs.pipeline_version = get_metadata(conn, "pipeline_profile")` at
    detection** (subsystem map:397; `insert_run` never restamps, `db.py:362`), and **seeds
    `metadata.pipeline_profile = "reference-v1"`** (or the deployment default) so a
    newly-detected run stamps a version the registry resolves. Thereafter it calls
    `load_profile(run["pipeline_version"], registry)` — resolving the **stamped** version,
    never re-reading the default (the freeze).
  - **Assembles the registry** from `REFERENCE_PROFILES` plus the injected
    `production_profiles`, **detecting key collisions at assembly** — not with a silent
    `{**REFERENCE_PROFILES, **production_profiles}` spread. A production profile keyed
    `"reference-v1"` would otherwise shadow the reference profile with **no error**; assemble
    by an explicit union that raises on any duplicate `pipeline_version` (reject a key
    already present), so a collision fails loud at startup. Each profile is keyed by its own
    `pipeline_version` (`{p.pipeline_version: p for p in ...}`), which `load_profile`'s
    key-equals-id check (§3.2) backstops per run. `production_profiles` is injected by the
    outer layer that legitimately imports both `sluice.gates` and `sluice.core` (core
    cannot); this is the same dependency-injection shape as the daemon-injected `Notifier`
    (plan 03 ⚑5).
  - **The full-loop disabled-is-satisfied proof consumes the shipped
    `REFERENCE_PROFILE_ALPHA_DISABLED`** (⚑2, decided): stamp a test run
    `pipeline_version="reference-v1-alpha-disabled"` and drive the loop — beta runs and
    completes with alpha never executed and `mock_alpha_done` still `0`. The daemon plan
    does **not** build its own disabled-variant fixture; the variant ships in core.
  - **Handles `UnknownProfileError` as a quarantine-grade failure for that one run, never a
    loop crash** (mirror plan 04 §7's traversing-name handling): a run frozen to an
    unregistered version is a state anomaly to surface, not a reason to stop reconciling
    every other run.
- **Plan B — production profiles arrive with their gates.**
  - Each production profile is `build_profile(pipeline_version, [...])` over the production
    gate records; it passes through `validate_gates` automatically (the B3 guard extends —
    a bad order transcription is a failed test, not a deadlock).
  - **Model `nvd_auto_run`/`labkey_enabled`/`labkey_auto_register_enabled`/
    `labkey_metadata_check_enabled` as `Applicability.DISABLED` in a profile**, NOT as
    `if config.get(...)` inside `run` (subsystem map:477/634; PP `poller.py:1229`,`:1469`,
    `:1190`,`:1203`). Disabling keeps the gate in the ordered set (disabling ≠ omitting,
    §2 B1) so its dependents' `needs` resolve; reconcile treats it as satisfied. This is the
    B1 config-switch-to-profile resolution the mechanism here exists for.
  - **Raw-FASTQ cleanup is its own optional `reap` gate (errata B7), never a `merge` side
    effect.** PP's `cleanup_raw_fastq_files` is disabled (`poller.py:1170-1173`, commit
    `d198073`); its absence is the *only* reason `merge` is invertible today. Model raw
    deletion as a distinct gate ordered **after the last consumer of the raw files**, so
    `merge` stays permanently non-destructive and `merge.invalidate`/`backtrack` stay honest
    (deleted multi-TB raw FASTQs are irrecoverable). Its enable/disable is already
    expressible via `Applicability` (optional/disabled) — the mechanism exists; the `reap`
    gate itself is deferred content. This is the errata's recorded recommendation; Plan A
    pre-empts none of it.
  - **B1/B2 content** (registration = experiment-creation vs authority-list; is
    `samplesheet` a gate; where `experiment_id` is stamped) is decided with the production
    gate definitions; profile applicability then expresses each gate's enable/disable.
  - **Legacy stamping** (subsystem map:1045): if historical rows are imported, stamp a real
    profile id or register a reserved profile (e.g. `legacy-v0`); `load_profile` is fail-loud,
    so there is no silent fallback to lean on.
- **The active-default id is DB metadata, never config** (§1): whoever seeds and updates
  `poller_metadata.pipeline_profile` (subsystem 8 `set`/9 `daemon`) owns it; the profile
  subsystem never reads it. `config.example.json` has no profile key and gains none.

---

## ⚑ Human sign-off — both DECIDED (2026-07-23, decided by the user)

1. **⚑1 — required-gate-depends-on-optional-gate deadlock — APPROVED AS RECOMMENDED
   (2026-07-23, decided by the user): no profile-side ban in Plan A.**
   `Profile.__post_init__` does not forbid a `REQUIRED` gate needing an `OPTIONAL` gate.
   Rationale as recommended: whether that shape deadlocks depends entirely on reconcile's
   definition of "a due optional" (spec §7) — if reconcile makes an optional gate *due*
   precisely because a selectable required gate needs it, the shape is valid and a
   profile-side ban would wrongly reject it. The deadlock rule is therefore **decided at
   reconcile, jointly with the definition of "due"**; the named carry-forward (§7) stands
   exactly as written, and reconcile carries the deadlock risk explicitly. If a
   profile-side guard is ever wanted after "due" is fixed, it is added consciously to
   `__post_init__` with its own test.
2. **⚑2 — reference profiles — DECIDED: ship two (2026-07-23, decided by the user),
   against the recommendation to ship one.** Core ships **both**
   `REFERENCE_PROFILE` (`"reference-v1"`, both mock gates `REQUIRED`) **and**
   `REFERENCE_PROFILE_ALPHA_DISABLED` (`"reference-v1-alpha-disabled"`, same two mock
   gates, `MOCK_ALPHA` `DISABLED` / `MOCK_BETA` `REQUIRED`) in the two-entry
   `REFERENCE_PROFILES`, each keyed by its own `pipeline_version` (passing the §3.2
   registry-coherence check). The variant disables the *needed* gate so the full loop
   proves both halves of disabled-is-satisfied — needs-unblocking from applicability and
   exclusion from completion (unit 6). Integrated: §1 surface (9 names), §3.1/§3.2,
   unit 6 (the variant's named tests), unit 7 (surface count + variant resolution leg),
   the invariant table, done criteria 5/7, and the §7 daemon carry-forward (the daemon
   consumes the shipped variant; it builds no disabled-variant fixture of its own).
