"""Behavioral tests for ``sluice.core.context`` — RunContext + resolve_context (plan 04).

Context derives paths and carries config; it never reaches the database, a
notifier, or a gate module (plan 04 §1). The first guard here is structural:
a module that cannot even name a SQL client cannot open a connection or query,
so "resolve_context does no DB work" (fact 3) and "RunContext carries no
notifier" (fact 1) are guaranteed by what the module *can even name*.
"""

import ast
import dataclasses
import inspect
import pathlib
import re
import sqlite3
import sys

import pytest


def make_config(**overrides) -> dict:
    """Build a config dict with the two keys resolve_context requires, plus overrides.

    Defaults per plan 04 §4: ``local_staging_dir="/staging"``,
    ``state_file="data/sluice_state.db"``. Each test names only the keys its
    behavior is about; everything else rides on these placeholders.
    """
    config = {
        "local_staging_dir": "/staging",
        "state_file": "data/sluice_state.db",
    }
    config.update(overrides)
    return config


def test_context_is_stdlib_only_and_db_free():
    """``context.py`` imports stdlib only; ``sqlite3``/db/``slack_sdk``/gates/api/cli forbidden by name.

    One test, three invariants (plan 04 unit 1): the file-level ``gates → core``
    arrow (no ``sluice.gates``/``api``/``cli``), fact 3's structural "no DB
    work" guarantee (no ``sqlite3``, no ``sluice.core.db`` — nothing to query
    with), and fact 1's structural "no notifier" (no ``slack_sdk`` to build
    one). Parsed with ``ast``, not inspected at runtime, because a
    function-local import — precisely how a "just this once" query sneaks in —
    never shows up in the module namespace (same technique as
    ``test_db_imports_only_stdlib`` / ``test_events_is_core_clean_by_import``).
    """
    # Local import: context is the module under construction; a module-level
    # import would fail collection for every other test in this file.
    import sluice.core.context

    source_path = inspect.getsourcefile(sluice.core.context)
    tree = ast.parse(pathlib.Path(source_path).read_text(encoding="utf-8"))

    # Collect fully-qualified module names for every import statement. For
    # ``from X import Y`` we record the module X (absolute) or resolve the
    # relative base against this module's package, sluice.core — so
    # ``from .db import connect`` cannot dodge the by-name guards below.
    imported_modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                relative_base_parts = "sluice.core".split(".")
                # level=1 is the containing package; each extra level climbs.
                if node.level > 1:
                    relative_base_parts = relative_base_parts[: -(node.level - 1)]
                resolved = ".".join(relative_base_parts)
                if node.module:
                    resolved = f"{resolved}.{node.module}"
                imported_modules.add(resolved)
            elif node.module:
                imported_modules.add(node.module)

    # Forbidden imports asserted BY NAME so the failure message teaches
    # (plan 04 unit 1): each name is a distinct policy with its own reason.
    forbidden_by_name = {
        "sqlite3": (
            "context must not reach a SQL client — resolve_context is a PURE "
            "derivation that performs no DB queries (fact 3); the db path "
            "travels as a RunContext *field*, never a handle to query with"
        ),
        "sluice.core.db": (
            "context must not reach the db primitives — a module that cannot "
            "open a connection cannot query or write run state (fact 3, the "
            "structural no-DB-work guarantee)"
        ),
        "slack_sdk": (
            "RunContext carries NO notifier (fact 1, events plan ⚑5 APPROVED "
            "daemon-only caller model) — a module that cannot name slack_sdk "
            "cannot construct one"
        ),
        "sluice.gates": (
            "the dependency arrow is gates → core; core context importing a "
            "gates module would reverse it (plan 04 §1)"
        ),
        "sluice.api": (
            "the dependency arrow is api → core; core context importing api "
            "would reverse it (plan 04 §1)"
        ),
        "sluice.cli": (
            "the dependency arrow is cli → core; core context importing cli "
            "would reverse it (plan 04 §1)"
        ),
    }
    for forbidden_module, reason in forbidden_by_name.items():
        matches = sorted(
            module
            for module in imported_modules
            if module == forbidden_module or module.startswith(f"{forbidden_module}.")
        )
        assert matches == [], f"{source_path} imports {matches}: {reason}"

    # The closed set (plan 04 §1): stdlib ONLY — no sluice-internal imports at
    # all (context needs no gate contract, unlike events), no third party.
    non_stdlib_roots = sorted(
        {module.split(".")[0] for module in imported_modules} - sys.stdlib_module_names
    )
    assert non_stdlib_roots == [], (
        f"{source_path} imports non-stdlib modules: {non_stdlib_roots} — "
        "context's allowed import set is stdlib only (plan 04 §1: dataclasses, "
        "pathlib, types, collections.abc)"
    )


def test_context_derives_no_path_from_dunder_file():
    """The raw source of ``context.py`` never contains the token ``__file__`` (A7/D2).

    Errata A7: python-poller derives the NVD *launch* dir from
    ``Path(__file__).resolve().parent`` (``nvd_helpers.py:186,199``) — an
    install-location coupling that D2 shows is dangerous: a launch dir silently
    relocated onto CephFS makes ``--resume`` a no-op, forcing a multi-hour
    re-run. This standing structural guard keeps the trap out of the context
    module permanently: no RunContext path — present or future — may ever be
    derived from where the code happens to be installed. A deliberate raw
    substring check (not ast), so the token cannot hide in a string, an f-string,
    or an eval — if ``__file__`` appears at all, in any form, this fails.
    """
    # Local import: context is the module under construction (same rationale
    # as the import-hygiene test above).
    import sluice.core.context

    source_path = inspect.getsourcefile(sluice.core.context)
    raw_source = pathlib.Path(source_path).read_text(encoding="utf-8")

    assert "__file__" not in raw_source, (
        f"{source_path} contains the token __file__ — no context path may be "
        "derived from the module's install location (errata A7: PP's NVD "
        "launch dir used Path(__file__), and D2 shows that coupling silently "
        "breaks --resume when the install relocates). Paths come from config "
        "and the run row ONLY (plan 04 §2 A7/D2)."
    )


def test_run_context_fields_are_exactly_the_closed_contract():
    """``RunContext``'s field set is exactly ``[workdir, db_path, config, run_row, announce_start]``.

    This one test carries facts 1 and 2 (plan 04 §2): no notifier field (the
    daemon-only caller model — events plan ⚑5 APPROVED — supersedes spec §6's
    "notifier" line), and no destination/remote path fields (``raw_seqs_dir``,
    ``nvd_launch_dir``, … are named Plan B deferrals). The fourth field
    ``run_row`` is the ADR-0006 d.3 read-path snapshot — a conscious contract
    change with the same governance as the gate field-pin (ADR-0006 d.1). The
    set is LEAN and CLOSED: adding any further field — a notifier reversal or a
    Plan B path — must edit this list consciously, backed by a plan revision or
    ADR, never as a quiet extra field. The first THREE stay required (no
    default): a defaulted ``config``/``workdir``/``db_path`` would let a context
    be constructed on a silently-assumed value (guardrail §8). ``run_row`` is
    deliberately optional with a sealed empty default (ADR-0006 finding 1) so a
    context resolved before any token exists — and the two direct constructions
    below — need not name it. ``announce_start`` (ADR-0008) is the one narrow
    opening in "no notifier": an optional one-string callback, ``None`` by
    default, that the Nextflow gates use to post their start with the Seqera link.
    """
    # Local import: context is the module under construction (same rationale
    # as the import-hygiene test above).
    from sluice.core.context import RunContext

    declared_fields = dataclasses.fields(RunContext)

    assert [field.name for field in declared_fields] == [
        "workdir",
        "db_path",
        "config",
        "run_row",
        "announce_start",
    ], (
        "RunContext's field set is the closed contract: workdir, db_path, "
        "config (plan 04 facts 1-2) plus run_row (ADR-0006 d.3 read path) and "
        "announce_start (ADR-0008). No notifier (events plan ⚑5), no "
        "destination/remote path fields (Plan B). Changing this set requires a "
        "plan revision or ADR."
    )

    fields_by_name = {field.name: field for field in declared_fields}
    # The first three stay strictly required — a defaulted one would license a
    # silent fallback (guardrail §8).
    for required_field_name in ("workdir", "db_path", "config"):
        assert fields_by_name[required_field_name].default is dataclasses.MISSING
        assert (
            fields_by_name[required_field_name].default_factory is dataclasses.MISSING
        )
    # run_row is optional with a sealed empty-mapping default (ADR-0006 d.3 /
    # finding 1): the read-path snapshot is filled by resolve_context, so a
    # direct construction that omits it must stay valid.
    assert len(fields_by_name["run_row"].default) == 0
    # announce_start defaults to None: no gate announces anything unless the
    # daemon hands it the callback (ADR-0008).
    assert fields_by_name["announce_start"].default is None


def test_run_context_is_frozen():
    """Assigning to a field on a ``RunContext`` instance raises ``FrozenInstanceError``.

    Pins the spec §6 frozen choice — a refactor could silently drop
    ``frozen=True`` and every other behavior would still pass. The DB is the
    single source of truth for run state, so a context object must not become
    a second mutable in-memory authority mid-run (plan 04 unit 1, §5 spine
    invariant).
    """
    # Local import: context is the module under construction (same rationale
    # as the import-hygiene test above).
    from sluice.core.context import RunContext

    context = RunContext(
        workdir=pathlib.Path("/staging/run_A"),
        db_path=pathlib.Path("data/sluice_state.db"),
        config={"local_staging_dir": "/staging"},
    )

    with pytest.raises(dataclasses.FrozenInstanceError):
        context.workdir = pathlib.Path("/somewhere/else")  # type: ignore[misc]


def test_run_context_config_is_read_only():
    """``ctx.config`` rejects item mutation and is a snapshot isolated from the caller's dict.

    ``frozen=True`` only stops field *rebinding* — it does nothing to the
    mutability of the mapping the ``config`` field points at. A RunContext
    whose config were a live shared dict would let one gate rewrite config for
    every other gate (and for the daemon), creating exactly the mutable
    in-memory authority the spine forbids (plan 04 unit 1, §3.1: wrap in a
    read-only snapshot). Three assertions, one behavior: writes raise,
    deletes raise, and later mutation of the original dict does not show
    through — so the record is sealed at construction, not merely aliased.
    """
    # Local import: context is the module under construction (same rationale
    # as the import-hygiene test above).
    from sluice.core.context import RunContext

    original_config = {"local_staging_dir": "/staging"}
    context = RunContext(
        workdir=pathlib.Path("/staging/run_A"),
        db_path=pathlib.Path("data/sluice_state.db"),
        config=original_config,
    )

    with pytest.raises(TypeError):
        context.config["injected"] = "value"  # type: ignore[index]

    with pytest.raises(TypeError):
        del context.config["local_staging_dir"]  # type: ignore[attr-defined]

    # Snapshot isolation: a caller mutating its own dict after construction
    # must not reach inside the frozen record (the dict(config) copy).
    original_config["local_staging_dir"] = "/mutated/elsewhere"
    assert context.config["local_staging_dir"] == "/staging", (
        "ctx.config reflected a post-construction mutation of the caller's "
        "dict — config must be snapshotted at construction (plan 04 unit 1), "
        "not aliased to a live shared mapping."
    )


def test_run_row_snapshot_is_sealed_and_detached():
    """``ctx.run_row`` rejects item mutation and is detached from the source dict.

    ADR-0006 d.3 seal, the sibling of the config seal: the run-row snapshot must
    not become a second mutable in-memory authority (the DB is the single source
    of truth). Constructed DIRECTLY with a PLAIN dict (never a raw
    ``sqlite3.Row`` — that path flows through ``resolve_context``): item writes
    raise ``TypeError`` (the ``MappingProxyType`` wrapper), and a later mutation
    of the caller's dict does not show through (the ``dict(run_row)`` copy).
    """
    # Local import: context is the module under construction (same rationale
    # as the import-hygiene test above).
    from sluice.core.context import RunContext

    original_run_row = {"experiment_id": 4242}
    context = RunContext(
        workdir=pathlib.Path("/staging/run_A"),
        db_path=pathlib.Path("data/sluice_state.db"),
        config=make_config(),
        run_row=original_run_row,
    )

    with pytest.raises(TypeError):
        context.run_row["experiment_id"] = 9  # type: ignore[index]

    # Snapshot isolation: mutating the source dict after construction must not
    # reach inside the frozen record.
    original_run_row["experiment_id"] = 9999
    assert context.run_row["experiment_id"] == 4242, (
        "ctx.run_row reflected a post-construction mutation of the caller's "
        "dict — the run-row snapshot must be sealed at construction (ADR-0006 "
        "d.3), not aliased to a live shared mapping."
    )


def test_resolve_context_derives_workdir_db_path_and_config():
    """``resolve_context(row, config)`` derives workdir={staging}/{name}, db_path={state_file}, and carries config.

    The happy path pins all three §3.2 derivations at once (plan 04 unit 2):
    ``workdir`` is keyed on ``run_row["name"]`` under
    ``config["local_staging_dir"]`` — the B2 resolution, ⚑1 APPROVED — and on
    NO other root (never a ``/scratch``/system-temp default: CHTC has no
    ``/scratch``, §3.3); ``db_path`` comes from ``config["state_file"]``
    (fact 3 — the db path travels as an output field, never an input handle);
    and the config itself is carried through on the context. A regression in
    any of the three — a wrong root, an ``experiment_id``-keyed path, a
    dropped db_path — surfaces here before the daemon ever calls it.
    """
    # Local import: resolve_context is the unit under construction; a
    # module-level import would fail collection for every other test here.
    from sluice.core.context import resolve_context

    config = make_config(local_staging_dir="/staging", state_file="data/x.db")
    row = {"name": "run_A", "experiment_id": None}

    ctx = resolve_context(row, config)

    assert ctx.workdir == pathlib.Path("/staging/run_A"), (
        "workdir must be Path(config['local_staging_dir']) / row['name'] — "
        "name-keyed under the config staging root and nothing else "
        "(plan 04 §3.2, B2 ⚑1 approved; §3.3: no /scratch or system-temp root)."
    )
    assert ctx.db_path == pathlib.Path("data/x.db"), (
        "db_path must be Path(config['state_file']) — the state DB path "
        "travels as a RunContext field sourced from config (plan 04 fact 3)."
    )
    assert ctx.config["local_staging_dir"] == "/staging", (
        "the passed config must be carried on the context (plan 04 §3.2 — "
        "read-only carriage is pinned by the unit-1 config tests)."
    )


def test_resolve_context_creates_no_filesystem_state(tmp_path):
    """``resolve_context`` computes paths but never creates them — no mkdir, no db file.

    Fact 3 (plan 04 §2): resolve_context is a PURE derivation — it computes
    paths, never ``mkdir``s; the daemon creates ``ctx.workdir`` before running
    a gate (§7). Both config paths point under directories that do NOT exist,
    so any filesystem mutation — an eager ``workdir.mkdir``, a ``.resolve()``
    that materializes state, a db file opened "just to check" — leaves a
    visible trace this test catches. Without this pin, an eager-mkdir
    regression would silently move directory creation from the daemon (which
    owns it, with ``exist_ok=True`` semantics) into the pure layer.
    """
    # Local import: resolve_context is the unit under construction (same
    # rationale as the happy-path test above).
    from sluice.core.context import resolve_context

    staging_root = tmp_path / "staging"
    state_file = tmp_path / "state" / "sluice_state.db"
    config = make_config(
        local_staging_dir=str(staging_root),
        state_file=str(state_file),
    )

    ctx = resolve_context({"name": "run_A", "experiment_id": None}, config)

    assert not ctx.workdir.exists(), (
        "resolve_context created ctx.workdir — it must compute the path only; "
        "the daemon owns mkdir (plan 04 fact 3, §7)."
    )
    assert not staging_root.exists(), (
        "resolve_context created the staging parent directory — a pure "
        "derivation must not touch the filesystem (plan 04 fact 3)."
    )
    assert not state_file.exists() and not state_file.parent.exists(), (
        "resolve_context created state under the db path — it must never "
        "open or touch the database file (plan 04 fact 3)."
    )


def test_resolve_context_is_deterministic():
    """Two calls of ``resolve_context`` with the same row and config return equal ``RunContext``s.

    A pure function of state has no hidden input (plan 04 fact 3, unit 2): the
    context must be a function of the row and config ALONE. The regression this
    catches is a hidden per-call input the happy-path test cannot see — e.g. a
    "resolved_at" timestamp or nonce stashed into the carried config, or any
    clock/filesystem/module-global read that varies between calls — which would
    make two derivations of the *same* run unequal and break spec §6's resume
    guarantee (a restarted daemon must rebuild the identical context from the
    row alone). Equality is the frozen dataclass's generated ``__eq__``;
    ``MappingProxyType`` compares by its underlying mapping, so the carried
    config participates in the comparison.
    """
    # Local import: resolve_context is the unit under construction (same
    # rationale as the happy-path test above).
    from sluice.core.context import resolve_context

    config = make_config()
    row = {"name": "run_A", "experiment_id": None}

    first_context = resolve_context(row, config)
    second_context = resolve_context(row, config)

    assert first_context == second_context, (
        "two resolve_context calls with the same row and config produced "
        "unequal RunContexts — the derivation read a hidden input (clock, "
        "filesystem, or module global); it must be a pure, deterministic "
        "function of (row, config) only (plan 04 fact 3, spec §6 resume)."
    )


def test_resolve_context_signature_is_row_and_config_only():
    """``resolve_context`` takes exactly ``(run_row, config)`` — no connection, no db-path input.

    Fact 3 as a signature fact (plan 04 unit 2): resolve_context is handed NO
    connection and NO db path, so it structurally cannot query the database —
    the db path is an *output* field (sourced from ``config["state_file"]``),
    never an input handle. The regression this catches is the plausible
    convenience edit — ``resolve_context(run_row, config, db_path=...)`` or a
    connection parameter "just for a lookup" — which would quietly reopen the
    purity seam the plan closed. Editing this signature is a conscious purity
    change requiring a plan revision.
    """
    # Local import: resolve_context is the unit under construction (same
    # rationale as the happy-path test above).
    from sluice.core.context import resolve_context

    assert list(inspect.signature(resolve_context).parameters) == [
        "run_row",
        "config",
    ], (
        "resolve_context's signature must be exactly (run_row, config) — a "
        "pure derivation receives the already-fetched row and the loaded "
        "config, and NOTHING it could query or mutate with (plan 04 fact 3, "
        "§3.1)."
    )


@pytest.mark.parametrize("missing_key", ["local_staging_dir", "state_file"])
def test_resolve_context_fails_loud_on_a_missing_config_key(missing_key):
    """A config missing ``local_staging_dir`` or ``state_file`` raises ``KeyError`` naming the key.

    Guardrail §8 (plan 04 §3.3): required keys are read by subscript, never
    ``.get(key, default)``. The regression this catches is the "helpful"
    fallback edit — in particular a defaulted ``/scratch``/system-temp workdir
    root, which on the deployment cluster (CHTC, which has NO ``/scratch``
    filesystem) would be not just a forbidden silent fallback but a broken
    path at runtime. A missing key must fail loud, naming the key, so the
    error is a fixable config problem and never a run staged under a root
    nobody configured.
    """
    # Local import: resolve_context is the unit under construction (same
    # rationale as the happy-path test above).
    from sluice.core.context import resolve_context

    config = make_config()
    del config[missing_key]

    with pytest.raises(KeyError, match=missing_key):
        resolve_context({"name": "run_A", "experiment_id": None}, config)


@pytest.mark.parametrize(
    "traversing_name",
    ["../evil", "a/b", "..\\evil", "..", ".", ""],
)
def test_resolve_context_rejects_a_traversing_run_name(traversing_name):
    r"""A run name that is not a single safe path component raises ``ValueError`` naming it.

    resolve_context is the boundary where a stored string identity becomes a
    filesystem path (plan 04 §3.3), so it is the one place to refuse a
    traversing name: without this defense,
    ``Path("/staging") / "../../etc"`` escapes the staging root and a gate
    would read/write outside it. Rejected by contract: any name containing
    ``/`` or ``\``, or equal to ``""``, ``"."``, or ``".."``. Sequencing
    folder names are always single components in production, so a legitimate
    name never trips this — proven by the plain-name control below, which
    guards against an over-broad rejection as much as the parametrized cases
    guard against a missing one.
    """
    # Local import: resolve_context is the unit under construction (same
    # rationale as the happy-path test above).
    from sluice.core.context import resolve_context

    config = make_config(local_staging_dir="/staging")

    # The error must NAME the offending value (plan 04 §3.3) so the daemon's
    # quarantine-grade log (§7) says which stored name was refused. re.escape
    # because the names contain regex metacharacters ("..", "\\"); for the
    # empty name there is no value to show, so only the raise is pinned
    # (match="" would vacuously pass against any message).
    offending_value_pattern = re.escape(traversing_name) if traversing_name else None
    with pytest.raises(ValueError, match=offending_value_pattern):
        resolve_context({"name": traversing_name, "experiment_id": None}, config)

    # Control: a plain single-component name resolves normally — the defense
    # rejects traversal, not legitimate run names.
    ctx = resolve_context({"name": "run_A", "experiment_id": None}, config)
    assert ctx.workdir == pathlib.Path("/staging/run_A"), (
        "a plain single-component name must still resolve — the traversal "
        "defense must not reject legitimate run names (plan 04 §3.3)."
    )


def test_resolve_context_derivation_ignores_resume_tokens():
    """The path DERIVATION ignores resume tokens; ADR-0006 d.3 now snapshots them onto run_row.

    Fact 4 / errata A5 (plan 04 §2): the non-recomputable persisted set is
    {``globus_task_id``, ``slack_thread_ts``} — Globus- and Slack-assigned
    handles that stay in the run row for Plan B's resume/monitoring reads. The
    original invariant here (full-context equality across rows differing only in
    tokens) is DELIBERATELY superseded by ADR-0006 d.3: ``resolve_context`` now
    carries a sealed snapshot of the whole run row so a resuming gate reads its
    prior tokens off ``context.run_row`` with no DB touch — carrying the token
    is the point, not a leak.

    NARROWED AGAIN by ⚑A (gate map §5): the derivation no longer reads exactly
    one column. It reads **two** — ``name`` and ``experiment_id`` — because
    production stages a run under its experiment number. So the permanent
    guarantee is now specifically about the RESUME tokens: ``globus_task_id`` and
    ``slack_thread_ts`` are Globus- and Slack-assigned handles that must NEVER
    fold into a path, so all three rows below (which differ only in those tokens,
    with ``experiment_id`` held constant) derive the SAME workdir/db_path, while
    ``run_row`` faithfully reflects whatever each row carried.

    ``experiment_id`` being path-forming while these two are not is the whole
    distinction: it is a stable identity the operator also sees in LabKey, they
    are opaque handles for a specific in-flight operation. The spec §6 resume
    guarantee (a restarted daemon rebuilds the identical context from the SAME
    row) is pinned by ``test_resolve_context_is_deterministic``.
    """
    # Local import: resolve_context is the unit under construction (same
    # rationale as the happy-path test above).
    from sluice.core.context import resolve_context

    config = make_config()

    # The same run three ways: tokens set (a resumed run), tokens NULL (a
    # fresh run, as sqlite3.Row would present unset columns), keys absent
    # entirely.
    # experiment_id is held constant across all three (⚑A: it IS path-forming,
    # so varying it here would confound what this test is about).
    row_with_tokens_set = {
        "name": "run_A",
        "experiment_id": None,
        "globus_task_id": "task-xyz",
        "slack_thread_ts": "1721.001",
    }
    row_with_null_tokens = {
        "name": "run_A",
        "experiment_id": None,
        "globus_task_id": None,
        "slack_thread_ts": None,
    }
    row_without_token_keys = {"name": "run_A", "experiment_id": None}

    context_from_set_tokens = resolve_context(row_with_tokens_set, config)
    context_from_null_tokens = resolve_context(row_with_null_tokens, config)
    context_from_absent_keys = resolve_context(row_without_token_keys, config)

    # The path derivation ignores the tokens: same workdir and db_path across
    # all three rows (the tokens never fold into a path).
    workdirs = {
        context_from_set_tokens.workdir,
        context_from_null_tokens.workdir,
        context_from_absent_keys.workdir,
    }
    db_paths = {
        context_from_set_tokens.db_path,
        context_from_null_tokens.db_path,
        context_from_absent_keys.db_path,
    }
    assert workdirs == {pathlib.Path("/staging/run_A")} and db_paths == {
        pathlib.Path("data/sluice_state.db")
    }, (
        "resolve_context folded a resume token into workdir/db_path — the path "
        "derivation must read only row['name'] (plan 04 fact 4, errata A5); "
        "tokens ride on the run_row snapshot, never the derived paths."
    )

    # ADR-0006 d.3: the snapshot faithfully carries whatever the row held —
    # a set token is present, an absent key stays absent.
    assert context_from_set_tokens.run_row["globus_task_id"] == "task-xyz", (
        "resolve_context must snapshot a set resume token onto run_row so a "
        "resuming gate reads it with no DB touch (ADR-0006 d.3)."
    )
    assert "globus_task_id" not in context_from_absent_keys.run_row, (
        "resolve_context must snapshot exactly the row's keys — it must not "
        "fabricate a token key the row never carried (ADR-0006 d.3; no "
        "fabrication of persisted state, plan 04 fact 4, errata A5)."
    )


# ---------------------------------------------------------------------------
# Unit 3 — surface + real-sqlite3.Row integration + the mock-gate workdir
# contract (plan 04 §4 unit 3). Green-on-arrival by design: no production
# code lands here; these tests pin the COMPOSED behavior of units 1-2
# against the real db and gate substrate (the plan-02 unit-9 carve-out
# convention).
# ---------------------------------------------------------------------------


def test_context_exports_exactly_the_documented_surface():
    """``context.__all__`` is exactly ``{RunContext, resolve_context, is_safe_path_component}``.

    The scope guard (db unit-7 precedent, plan 04 §1): the module's public
    surface is CLOSED at these names. A quietly-exported helper — a path
    builder, a config validator — would become load-bearing API the moment a
    downstream subsystem imported it; growing the surface must be a conscious
    contract change that edits this set, never an incidental ``__all__`` entry.

    ``is_safe_path_component`` is exactly such a conscious change (plan 14
    unit 1): the traversal rule was already load-bearing in two modules as a
    hand-copied twin, so exporting it collapses the copies rather than growing
    the API — the daemon's private ``_is_safe_path_component`` was deleted in
    the same change.

    ``with_start_announcer`` is another (ADR-0008): the daemon's one way to
    attach the Nextflow gates' start callback without widening
    ``resolve_context``'s pinned ``(run_row, config)`` signature.
    """
    # Local import: context is the module under construction (same rationale
    # as the import-hygiene test above).
    import sluice.core.context

    assert set(sluice.core.context.__all__) == {
        "RunContext",
        "resolve_context",
        "is_safe_path_component",
        "with_start_announcer",
    }, (
        "sluice.core.context's public surface must be exactly RunContext + "
        "resolve_context + is_safe_path_component (plan 04 §1, plan 14 unit "
        "1) — growing or shrinking it is a conscious contract change, never a "
        "quiet export."
    )


def test_resolve_context_reads_a_real_sqlite_row(tmp_path):
    """The whole derivation runs off ``row["name"]`` on a real ``sqlite3.Row`` from ``get_run``.

    The load-bearing cross-subsystem proof for the db side (plan 04 unit 3):
    every unit-2 test used a plain-dict row double, so a drift between the
    real schema's column name and the key resolve_context subscripts — a
    renamed ``name`` column, or resolve_context reading a key ``sqlite3.Row``
    spells differently — would slip past unit 2 and surface only in the
    daemon. Here the row comes from the real primitives on a real temp DB
    (initialize_database → insert_run → get_run), so that drift fails HERE.
    It also proves resolve_context never needed ``experiment_id``: the real
    row's is NULL and the derivation succeeds untroubled (B2 / fact 2 — the
    name-keyed workdir sidesteps the experiment_id chicken-and-egg).
    """
    # Local imports: the db primitives are unit 3's substrate only — a
    # module-level import would couple every unit-1/2 test to the db module.
    from sluice.core.context import resolve_context
    from sluice.core.db import get_connection, get_run, initialize_database, insert_run

    db_path = tmp_path / "sluice_state.db"
    initialize_database(db_path)
    connection = get_connection(db_path)
    try:
        insert_run(connection, "run_A", pipeline_version="v1")
        row = get_run(connection, "run_A")
    finally:
        connection.close()

    # The real substrate, not a double: get_run hands back a sqlite3.Row.
    assert isinstance(row, sqlite3.Row)
    # The precondition B2/fact 2 rest on: no experiment gate has run, so the
    # real row's experiment_id is NULL — and the derivation below never needs it.
    assert row["experiment_id"] is None

    config = make_config(
        local_staging_dir=str(tmp_path / "staging"),
        state_file=str(db_path),
    )

    ctx = resolve_context(row, config)

    assert ctx.workdir == tmp_path / "staging" / "run_A", (
        "resolve_context on a REAL sqlite3.Row must derive the same "
        "name-keyed workdir as on a dict double — a schema/column-name drift "
        "between db and context surfaces here, not in the daemon "
        "(plan 04 unit 3, §3.1 run_row duck type)."
    )
    assert ctx.db_path == pathlib.Path(config["state_file"]), (
        "db_path must come from config['state_file'] regardless of row type "
        "(plan 04 fact 3 — the db path travels as a field)."
    )


def test_resolve_context_captures_run_row_snapshot(tmp_path):
    """``resolve_context`` snapshots a real ``sqlite3.Row`` onto ``ctx.run_row`` (ADR-0006 d.3).

    The load-bearing conversion proof: ``resolve_context`` is the ONE place a
    real ``sqlite3.Row`` becomes the run-row snapshot, and it must do so with a
    keyed comprehension (``dict(<Row>)`` raises — a ``Row`` iterates values, not
    items). A run inserted with ``experiment_id=4242`` yields a context whose
    ``run_row`` carries both the identity token and the run ``name``, readable
    off the context with no further DB touch — the exact resume read a future
    gate performs (globus task id, experiment id).
    """
    # Local imports: the db primitives are this proof's substrate only.
    from sluice.core.context import resolve_context
    from sluice.core.db import get_connection, get_run, initialize_database, insert_run

    db_path = tmp_path / "sluice_state.db"
    initialize_database(db_path)
    connection = get_connection(db_path)
    try:
        insert_run(connection, "run_A", pipeline_version="v1", experiment_id=4242)
        row = get_run(connection, "run_A")
    finally:
        connection.close()

    # The real substrate, not a double: get_run hands back a sqlite3.Row.
    assert isinstance(row, sqlite3.Row)

    config = make_config(
        local_staging_dir=str(tmp_path / "staging"),
        state_file=str(db_path),
    )

    ctx = resolve_context(row, config)

    assert ctx.run_row["experiment_id"] == 4242, (
        "resolve_context must snapshot the row's experiment_id onto "
        "ctx.run_row — the ADR-0006 d.3 resume read (a gate reads its prior "
        "token off the context, no DB touch)."
    )
    assert ctx.run_row["name"] == "run_A", (
        "the run name must survive the keyed-comprehension Row conversion — "
        "dict(<sqlite3.Row>) would have raised, so the snapshot proves the "
        "conversion is keyed (ADR-0006 d.3)."
    )


def test_resolved_context_satisfies_the_mock_gate_workdir_contract(tmp_path):
    """A resolved context drives ``MOCK_ALPHA.run(ctx)`` to SUCCESS after the caller mkdirs.

    The load-bearing cross-subsystem proof for the gate side (plan 04 §3.4,
    plan 02 §7): the gate contract's duck-typed context requires exactly one
    attribute, ``workdir`` — and RunContext must satisfy it, or subsystem 4
    never delivered what subsystem 2 was owed. This is also the daemon's
    choreography in miniature (fact 3 / §7): resolve_context COMPUTED the
    path (creating nothing — the bare ``mkdir(parents=True)``, no
    ``exist_ok``, would raise ``FileExistsError`` had it pre-created the
    dir), the CALLER created it, and the GATE wrote its marker under it.
    """
    # Local imports: the db + gate substrate is unit 3's only (same rationale
    # as the real-row test above).
    from sluice.core.context import resolve_context
    from sluice.core.db import get_connection, get_run, initialize_database, insert_run
    from sluice.core.gate import GateOutcome
    from sluice.core.mock_gates import MOCK_ALPHA

    db_path = tmp_path / "sluice_state.db"
    initialize_database(db_path)
    connection = get_connection(db_path)
    try:
        insert_run(connection, "run_A", pipeline_version="v1")
        row = get_run(connection, "run_A")
    finally:
        connection.close()

    config = make_config(
        local_staging_dir=str(tmp_path / "staging"),
        state_file=str(db_path),
    )
    ctx = resolve_context(row, config)

    # The caller/daemon's job, never resolve_context's (fact 3, §7). No
    # exist_ok: if resolve_context had eagerly created the workdir, this
    # raises FileExistsError and the compute-vs-mkdir split is broken.
    ctx.workdir.mkdir(parents=True)

    result = MOCK_ALPHA.run(ctx)

    assert result.outcome is GateOutcome.SUCCESS, (
        "MOCK_ALPHA.run(ctx) must succeed when handed a resolved RunContext — "
        "RunContext must satisfy the gate contract's workdir-only duck type "
        "(plan 04 §3.4, plan 02 §3.4/§7)."
    )
    assert (ctx.workdir / "mock_alpha.marker").exists(), (
        "the gate must have written its marker under ctx.workdir — the "
        "resolved workdir is where gate work lands (plan 04 unit 3: "
        "resolve_context computed the path, the caller created it, the gate "
        "wrote under it)."
    )


# ---------------------------------------------------------------------------
# Plan 14 unit 1 — the safe-path-component rule, exported.
#
# The rule had two hand-copied homes: inline in resolve_context and, as
# daemon._is_safe_path_component, in the jsonl sink. Security-load-bearing in
# both (it is what stops a run named ".." escaping onto disk), so two copies
# was one too many. context owns the identity->path boundary, so it owns the
# rule; daemon imports it and its private copy is deleted.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "unsafe_name",
    ["../evil", "a/b", "..\\evil", "..", ".", ""],
)
def test_is_safe_path_component_rejects_unsafe_names(unsafe_name):
    r"""The exported predicate refuses any name that is not a single safe component.

    The same contract ``resolve_context`` has always enforced inline, now
    callable: a name containing ``/`` or ``\``, or equal to ``""``/``"."``/
    ``".."``, is not a single path component. This is the whole traversal
    defense, so the parametrized cases are the security surface.
    """
    from sluice.core.context import is_safe_path_component

    assert is_safe_path_component(unsafe_name) is False, (
        f"{unsafe_name!r} is not a single safe path component and must be "
        "refused — this predicate is the traversal defense (plan 04 §3.3)."
    )


def test_is_safe_path_component_accepts_a_plain_run_name():
    """A legitimate single-component run name passes.

    The control against an over-broad rule: production sequencing folder names
    are always single components, so the defense must reject traversal without
    rejecting real work.
    """
    from sluice.core.context import is_safe_path_component

    assert is_safe_path_component("run_A") is True, (
        "a plain single-component name must pass — the traversal defense "
        "must not reject legitimate run names (plan 04 §3.3)."
    )


# ---------------------------------------------------------------------------
# ⚑A (gate map §5, APPROVED 2026-07-30) — experiment_id-keyed staging.
#
# Production stages a run under its LabKey experiment number, not its folder
# name: {local_staging_dir}/{experiment_id}/{raw_seqs,merged_seqs,nvd-results}.
# But gate 0 is what CREATES the experiment, so experiment_id is NULL when the
# daemon resolves the context for it — and the daemon mkdirs workdir
# unconditionally for every gate. Reading (b): key on experiment_id when it is
# set, fall back to the Plan-A name-keyed path while it is NULL. workdir stays
# a non-optional Path, so the unconditional mkdir stays sound.
# ---------------------------------------------------------------------------


def test_workdir_is_experiment_id_keyed_once_the_experiment_exists():
    """With ``experiment_id`` set, ``workdir`` is ``{local_staging_dir}/{experiment_id}``.

    The production layout (⚑A). Every gate after order 0 sees this path, which is
    why the experiment number is the key a human uses to find a run's files on
    disk — it matches what they read in LabKey.
    """
    from sluice.core.context import resolve_context

    config = make_config(local_staging_dir="/staging")

    ctx = resolve_context({"name": "run_A", "experiment_id": 4471}, config)

    assert ctx.workdir == pathlib.Path("/staging/4471"), (
        "with experiment_id set, staging must be keyed by the experiment "
        "number, not the run name (⚑A)."
    )


def test_workdir_falls_back_to_the_run_name_before_the_experiment_exists():
    """With ``experiment_id`` NULL, ``workdir`` stays the Plan-A name-keyed path.

    Gate 0 runs before any experiment exists, and the daemon mkdirs its workdir
    unconditionally, so there must always be a real derivable path. Gate 0 does
    not stage data there — it just needs the path to be non-null and mkdir-able.
    """
    from sluice.core.context import resolve_context

    config = make_config(local_staging_dir="/staging")

    ctx = resolve_context({"name": "run_A", "experiment_id": None}, config)

    assert ctx.workdir == pathlib.Path("/staging/run_A"), (
        "while experiment_id is NULL the name-keyed fallback must apply, so "
        "gate 0 always has a mkdir-able workdir (⚑A)."
    )


def test_unsafe_run_name_is_refused_even_on_the_experiment_keyed_branch():
    """An unsafe run NAME is refused whether or not ``experiment_id`` is set.

    The traversal defense is unconditional on purpose. Keying the path on the
    experiment number would technically keep a hostile name off *this* path, but
    "resolve_context never builds a context for an unsafe name" is a simpler
    invariant than "…unless the experiment exists", and the name travels onward
    on ``context.run_row`` to gates that build their own paths from it. Costs one
    comparison; removes a whole class of "but which branch was I on?" reasoning.
    """
    from sluice.core.context import resolve_context

    config = make_config(local_staging_dir="/staging")

    with pytest.raises(ValueError, match=re.escape("../evil")):
        resolve_context({"name": "../evil", "experiment_id": 4471}, config)


def test_workdir_does_not_resolve_symlinks(tmp_path):
    r"""``workdir`` preserves a symlinked staging root — ``.absolute()``, never ``.resolve()``.

    Load-bearing for NVD (⚑A, PP ``:1113-1118``): the ``/staging`` symlink must
    survive into the paths NVD's by-lane globs are built from, because execute
    nodes resolve that symlink themselves. Calling ``.resolve()`` here would bake
    in this host's real path and break those globs off-node.
    """
    from sluice.core.context import resolve_context

    real_staging = tmp_path / "real_staging"
    real_staging.mkdir()
    linked_staging = tmp_path / "staging_link"
    linked_staging.symlink_to(real_staging)

    config = make_config(local_staging_dir=str(linked_staging))
    ctx = resolve_context({"name": "run_A", "experiment_id": 4471}, config)

    assert str(linked_staging) in str(ctx.workdir), (
        "the symlinked staging root must survive into workdir — resolving it "
        "would break NVD's by-lane globs on execute nodes (⚑A)."
    )
