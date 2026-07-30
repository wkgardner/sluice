"""Behavioral tests for ``sluice.core.control`` — the operator control ops (plan 08).

Control is design §9's op table made real: pure functions of shape
``op(db_path, actor, *args) -> ControlResult`` that validate, mutate in one
short transaction, write one audit row, emit one wide event, and return a
frozen result. Control is state-only (§2): no op starts or stops work, no op
calls a gate function, and — ADR-0005 — no op reads, writes, or deletes a
file. Ops touch exactly one thing: the database.
"""

import ast
import contextlib
import dataclasses
import inspect
import json
import pathlib
import sqlite3

import pytest
import structlog.testing

# --------------------------------------------------------------------------
# Unit 1 — skeleton: exceptions, ControlResult, import hygiene.
# --------------------------------------------------------------------------

# The closed import set (plan 08 §1): stdlib for the result type and JSON
# column handling, plus exactly the built core modules control composes. With
# nothing else importable, "control never starts or stops work — and never
# touches a file" is a compile surface, not a promise (ADR-0005 structural
# half).
ALLOWED_CONTROL_IMPORTS = {
    "dataclasses",
    "json",
    "types",
    "sqlite3",
    "collections.abc",
    "sluice.core.db",
    "sluice.core.audit",
    "sluice.core.events",
    "sluice.core.profile",
    "sluice.core.reconcile",
    "sluice.core.gate",
}

# Forbidden imports asserted BY NAME so the failure message teaches (plan 08
# unit 1): each name is a distinct policy with its own reason.
FORBIDDEN_CONTROL_IMPORTS = {
    "subprocess": (
        "control never starts or stops work (design §2/§9) — an op that "
        "could spawn a process could act, not just mutate state"
    ),
    "os": (
        "no filesystem or environment reach — ADR-0005: ops touch exactly "
        "one thing, the database"
    ),
    "shutil": (
        "automatic artifact deletion is the exact failure class ADR-0005 "
        "removes; rewind is column-clear only, deletion is the operator's"
    ),
    "pathlib": (
        "no path type means no path with which to touch a file (ADR-0005 "
        "made structural)"
    ),
    "signal": (
        "stopping work is the daemon's reaction to lifecycle state (§7 "
        "decision A) — an op never signals anything"
    ),
    "socket": ("state-only ops reach no network (design §2)"),
    "urllib": ("state-only ops reach no network (design §2)"),
    "http": ("state-only ops reach no network (design §2)"),
    "threading": (
        "the daemon is the only actor (design §2); control runs no "
        "background machinery of its own"
    ),
    "time": (
        "the clock is db.now_iso — one spelling, one format (plan 08 §1); "
        "a second clock site could diverge"
    ),
    "datetime": (
        "the clock is db.now_iso — one spelling, one format (plan 08 §1); "
        "a second clock site could diverge"
    ),
    "structlog": (
        "control's only emission is events.control_event — a module that "
        "could open its own log stream could bypass the canonical line "
        "(plan 08 §1; plan 03 §7 persist-then-emit)"
    ),
    "slack_sdk": (
        "control ops never post to Slack (design §11) and core never "
        "imports a service SDK (ADR-0003 decision 3)"
    ),
    "sluice.core.context": (
        "an op with no resolve_context has no path to a workdir — "
        "ADR-0005 made structural (plan 08 §1)"
    ),
    "sluice.core.mock_gates": (
        "gates reach ops only via the injected registry — control is "
        "gate-agnostic (plan 08 §1)"
    ),
    "sluice.gates": (
        "layering is gates -> core, never the reverse (ADR-0003); control "
        "is gate-agnostic by construction"
    ),
    "sluice.api": ("core never imports the API layer (ADR-0003 layering)"),
    "sluice.cli": ("core never imports the CLI layer (ADR-0003 layering)"),
}


def test_control_import_hygiene():
    """``control.py``'s imports are a subset of plan 08 §1's closed set, with every forbidden module absent even function-locally.

    ADR-0005's structural half: with ``os``/``shutil``/``pathlib`` (and
    ``sluice.core.context``) unimportable, no control op has any route to a
    file, and with ``subprocess``/``signal``/``socket`` unimportable, no op
    can start or stop work — "state-only control" becomes a compile surface
    rather than a promise. Parsed with ``ast``, not inspected at runtime,
    because a function-local import — precisely how a "just this once"
    deletion or process spawn sneaks in — never shows up in the module
    namespace (the smuggling-guard technique from
    ``test_db_imports_only_stdlib`` / ``test_audit_import_hygiene``).
    """
    # Local import: control is the module under construction; a module-level
    # import would fail collection for every other test in this file.
    import sluice.core.control

    source_path = inspect.getsourcefile(sluice.core.control)
    tree = ast.parse(pathlib.Path(source_path).read_text(encoding="utf-8"))

    # Collect fully-qualified module names for every import statement anywhere
    # in the tree (ast.walk descends into function bodies, so function-local
    # smuggling is covered). For ``from X import Y`` we record the module X
    # (absolute) or resolve the relative base against this module's package,
    # sluice.core — so ``from .context import resolve_context`` cannot dodge
    # the guard. For ``from <package> import <submodule>`` shapes such as
    # ``from sluice.core import db``, the alias-qualified candidate
    # ``sluice.core.db`` is what gets judged, so a package-prefix import
    # cannot dodge it either.
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
            else:
                resolved = node.module
            if resolved is None:
                continue
            if resolved in ("sluice", "sluice.core"):
                # ``from sluice.core import X``: the imported thing is the
                # submodule named in the alias — judge that, not the package.
                imported_modules.update(
                    f"{resolved}.{alias.name}" for alias in node.names
                )
            else:
                imported_modules.add(resolved)

    def is_allowed(module_name: str) -> bool:
        """True when ``module_name`` is an allowed module or a submodule of one."""
        return any(
            module_name == allowed or module_name.startswith(f"{allowed}.")
            for allowed in ALLOWED_CONTROL_IMPORTS
        )

    outside_the_closed_set = sorted(
        module for module in imported_modules if not is_allowed(module)
    )
    assert outside_the_closed_set == [], (
        f"{source_path} imports {outside_the_closed_set}, outside the closed "
        f"set {sorted(ALLOWED_CONTROL_IMPORTS)} (plan 08 §1): control needs "
        "the result-type stdlib plus the built core modules it composes, and "
        "nothing that could reach a file, a process, a network, a second "
        "clock, or a gate"
    )

    forbidden_found = {
        module: reason
        for forbidden, reason in FORBIDDEN_CONTROL_IMPORTS.items()
        for module in sorted(imported_modules)
        if module == forbidden or module.startswith(f"{forbidden}.")
    }
    assert forbidden_found == {}, (
        f"{source_path} imports forbidden modules:\n"
        + "\n".join(f"  {name}: {why}" for name, why in forbidden_found.items())
    )


def test_control_result_is_frozen_sealed_and_serializable():
    """``ControlResult`` is frozen, its ``changes`` mapping is sealed and detached, and ``to_fields()`` JSON round-trips with exactly the seven pinned keys.

    Pins plan 08 §2's refinement of spec §9's "returns a dict" into a frozen
    record honoring "no authoritative in-memory state": the result *reports*
    what was committed, it is not the state. Three drops a refactor could make
    silently while every op test still passed: losing ``frozen=True`` (field
    rebinding would mutate a report after the fact), losing the sealed
    ``MappingProxyType`` snapshot (a live aliased ``changes`` dict would let a
    caller — or a later op reusing the constructing dict — rewrite what the
    record claims was written), and a ``to_fields()`` that leaks a
    non-JSON-shaped mapping or drifts its key set (it is the payload for
    ``control_event(result=...)`` and Plan C's response body — a pinned seam).
    """
    # Local import: control is the module under construction (same rationale
    # as the import-hygiene test above).
    from sluice.core.control import ControlResult

    constructing_changes = {"paused": "1"}
    result = ControlResult(
        action="pause",
        actor="will",
        target_run=None,
        status="ok",
        message=None,
        changes=constructing_changes,
        audit_id=7,
    )

    # Frozen: field rebinding raises (the record is a report, not state).
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.status = "noop"  # type: ignore[misc]

    # Sealed: item-write on the changes mapping raises.
    with pytest.raises(TypeError):
        result.changes["injected"] = "value"  # type: ignore[index]

    # Detached: mutating the constructing dict after the fact must not show
    # through the proxy — sealed at construction, not merely aliased.
    constructing_changes["paused"] = "0"
    assert result.changes["paused"] == "1", (
        "result.changes reflected a post-construction mutation of the "
        "constructing dict — changes must be snapshotted (dict copy under the "
        "MappingProxyType), not aliased to a live mapping (plan 08 §2, house "
        "pattern from RunContext/Profile)."
    )

    # Serializable: to_fields() is plain JSON-shaped data (a raw
    # MappingProxyType would make json.dumps raise), and the round-tripped
    # payload carries exactly the seven pinned keys with the constructed
    # values — dict equality pins the key set (no extras, no omissions) and
    # the values in one assertion.
    round_tripped = json.loads(json.dumps(result.to_fields()))
    assert round_tripped == {
        "action": "pause",
        "actor": "will",
        "target_run": None,
        "status": "ok",
        "message": None,
        "changes": {"paused": "1"},
        "audit_id": 7,
    }, (
        "to_fields() must round-trip through JSON with exactly the keys "
        "{action, actor, target_run, status, message, changes, audit_id} — "
        "it is the control_event payload and Plan C's response body (plan 08 "
        "§2), so key drift here is API drift."
    )


def test_exception_families_partition_audited_from_validation():
    """The exception tree makes plan 08 §2's audited/not-audited line a type fact: the four policy refusals subclass ``ControlError`` (a ``RuntimeError``), while ``UnknownRunError``/``UnknownGateError`` subclass ``KeyError`` and are NOT ``ControlError``.

    Every op decides "write a refused audit row and emit, then raise" versus
    "raise silently" by the exception's family, not per-op judgment: a policy
    refusal of a real action (protected run, fabrication, active-run
    quiescence, cross-disposition lifecycle) is exactly what the trail is for,
    while a typo'd run or gate name identifies no real state — auditing it
    would seed the trail with junk ``target_run`` values. If a refactor slid
    ``UnknownRunError`` into the ``ControlError`` family (or dropped a refusal
    out of it), every op's audit behavior would flip at once with no per-op
    test naming the cause — so the partition is pinned here, on the types
    themselves.
    """
    # Local import: control is the module under construction (same rationale
    # as the import-hygiene test above).
    from sluice.core.control import (
        ActiveRunError,
        ControlError,
        FabricationError,
        LifecycleError,
        ProtectedError,
        UnknownGateError,
        UnknownRunError,
    )

    # The audited family's root is itself a RuntimeError (plan 08 §2's
    # signature: ControlError(RuntimeError)) — Plan C maps the family to one
    # HTTP status, so the root's identity is part of the contract.
    assert issubclass(ControlError, RuntimeError), (
        "ControlError must subclass RuntimeError (plan 08 §2) — it is the "
        "root of the audited-refusal family Plan C maps to a conflict status"
    )

    audited_refusal_families = (
        ProtectedError,
        FabricationError,
        ActiveRunError,
        LifecycleError,
    )
    for refusal_type in audited_refusal_families:
        assert issubclass(refusal_type, ControlError), (
            f"{refusal_type.__name__} must subclass ControlError: it refuses "
            "a real action, so ops must audit and emit it before raising "
            "(plan 08 §2 — refusals are part of the trail)"
        )

    validation_failure_families = (UnknownRunError, UnknownGateError)
    for validation_type in validation_failure_families:
        assert issubclass(validation_type, KeyError), (
            f"{validation_type.__name__} must subclass KeyError (plan 08 §2) "
            "— a lookup that identified nothing real"
        )
        assert not issubclass(validation_type, ControlError), (
            f"{validation_type.__name__} must NOT be a ControlError: a typo'd "
            "name identifies no real state to act on, so it is never audited "
            "and never emitted (plan 08 §2 — no junk target_run in the trail)"
        )


# --------------------------------------------------------------------------
# Unit 2 — pause / resume / is_paused: the simplest full choreography.
# --------------------------------------------------------------------------


def test_pause_writes_state_audit_and_event_in_the_binding_order(tmp_path):
    """One ``pause`` call lands metadata state + its audit row atomically, emits exactly one global ``control.finished``, and returns a ``ControlResult`` naming the committed audit row.

    The §2 binding choreography exercised end to end for the first time — the
    pattern every later op repeats (mutate → audit in ONE ``write_txn``, event
    after commit, structured Result last). Each assertion pins a distinct
    sluice decision a partial implementation could drop while others still
    held: the B5 vocabulary (``poller_metadata['paused'] == "1"`` — the
    literal key and value grammar the daemon's ``is_paused`` will parse); the
    audit facts (D-11: ``action`` is the public function name ``"pause"``;
    F1: a global op's ``target_run`` is SQL NULL; ``args_json`` carries the
    op's scalar params only, so ``'{"reason": "drill"}'`` exactly;
    ``result="ok"``); the event half (exactly one ``control.finished`` with
    ``control.actor``, and NO ``run.name`` key — a fabricated one would
    corrupt subsystem 9's per-run jsonl routing, plan 03); and the returned
    record's ``audit_id`` equal to the trail row's ``id`` — the result
    *reports* what was committed, it does not restate intent.
    """
    # Local import: control is the module under construction (same rationale
    # as the unit-1 tests above).
    from sluice.core.control import pause
    from sluice.core.db import get_connection, get_metadata, initialize_database

    database_path = tmp_path / "sluice.db"
    initialize_database(database_path)

    with structlog.testing.capture_logs() as captured_log_entries:
        returned_result = pause(database_path, "will", reason="drill")

    # State + audit read back on a FRESH connection: what landed is what was
    # committed, not what some still-open op connection could see.
    verification_connection = get_connection(database_path)
    try:
        assert get_metadata(verification_connection, "paused") == "1", (
            "pause must upsert poller_metadata['paused'] = '1' — the B5 "
            "vocabulary control owns (plan 08 §2): the literal key 'paused' "
            "and the literal value '1' are what the daemon's is_paused parse "
            "keys on"
        )
        audit_rows = verification_connection.execute(
            "SELECT * FROM control_audit"
        ).fetchall()
    finally:
        verification_connection.close()

    assert len(audit_rows) == 1, (
        "one pause call must write exactly ONE control_audit row (§9: each "
        f"state-writing op writes a row — one, not zero, not two); got "
        f"{len(audit_rows)}"
    )
    audit_row = audit_rows[0]
    assert audit_row["action"] == "pause", (
        "the audited action string is the op's public function name — the "
        "D-11 closed vocabulary (plan 08 §2); got "
        f"{audit_row['action']!r}"
    )
    assert audit_row["target_run"] is None, (
        "a global op audits with target_run = SQL NULL (F1 / plan 08 §2 B5) "
        "— pause targets the world, not a run; got "
        f"{audit_row['target_run']!r}"
    )
    assert audit_row["args_json"] == '{"reason": "drill"}', (
        "args_json records the op's scalar parameters, exactly — here just "
        "the reason (plan 08 §2: registry and machinery are never "
        f"serialized); got {audit_row['args_json']!r}"
    )
    assert audit_row["result"] == "ok", (
        "a successful op audits result='ok' — the plan 07 §2 vocabulary "
        f"'ok' | 'noop: <why>' | 'refused: <why>'; got {audit_row['result']!r}"
    )

    control_finished_entries = [
        entry for entry in captured_log_entries if entry["event"] == "control.finished"
    ]
    assert len(control_finished_entries) == 1, (
        "one pause call must emit exactly ONE control.finished event, after "
        "commit (persist-then-emit, plan 03 §7; every op calls control_event "
        f"exactly once, plan 08 §2); captured {captured_log_entries!r}"
    )
    emitted_entry = control_finished_entries[0]
    assert emitted_entry["control.actor"] == "will", (
        "the emitted line must attribute the acting operator via "
        f"control.actor (plan 03 §3.2); got {emitted_entry!r}"
    )
    assert "run.name" not in emitted_entry, (
        "a global op's control.finished must carry NO run.name key — "
        "subsystem 9's per-run jsonl routing keys on that literal, and a "
        "fabricated run.name would misfile the world-targeting op (plan 03 / "
        f"plan 08 §2 B5); got {emitted_entry!r}"
    )

    assert returned_result.status == "ok", (
        "the returned ControlResult must report status 'ok' for a real state "
        f"change (plan 08 §2 — refusals raise, they never return); got "
        f"{returned_result.status!r}"
    )
    assert returned_result.audit_id == audit_row["id"], (
        "ControlResult.audit_id must equal the committed control_audit row's "
        "id — the frozen record reports what landed, it is not a second "
        f"authority (plan 08 §2); got {returned_result.audit_id!r} vs row id "
        f"{audit_row['id']!r}"
    )


def test_pause_and_resume_are_audited_noops_when_state_already_matches(tmp_path):
    """A ``pause`` of an already-paused system and a ``resume`` of a never-paused one both return ``status="noop"``, write a ``"noop: <why>"`` audit row, and change no metadata.

    Pins the noop half of the §2 decision step (ok | noop | refusal) the very
    first time it can fire: a redundant op is NOT an error and NOT a silent
    skip — it is a real attempt an operator made, so the trail records it
    (plan 07 §2's ``result`` vocabulary, ``"noop: <why>"``), while the mutate
    step is skipped entirely (metadata byte-identical, ``changes`` empty —
    §2's ControlResult contract). Two regressions this catches: an
    implementation that "helpfully" re-writes the same value (turning every
    noop into a phantom ok that lies in ``changes`` and the trail), and one
    that returns early without auditing (making redundant operator attempts
    invisible — §5's "every control action" includes these).
    """
    # Local import: control is the module under construction (same rationale
    # as the unit-1 tests above).
    from sluice.core.control import pause, resume
    from sluice.core.db import get_connection, get_metadata, initialize_database

    # --- pause twice: the second is an audited noop, state untouched. -----
    paused_database_path = tmp_path / "paused.db"
    initialize_database(paused_database_path)
    pause(paused_database_path, "will", reason="drill")

    second_pause_result = pause(paused_database_path, "will", reason="drill")
    assert second_pause_result.status == "noop", (
        "pausing an already-paused system must return status='noop' — the "
        "state already matches, so there is nothing to write (plan 08 §2 "
        "decision step); got "
        f"{second_pause_result.status!r}"
    )
    assert dict(second_pause_result.changes) == {}, (
        "a noop's ControlResult.changes must be empty (plan 08 §2: changes = "
        "column/key -> value WRITTEN, and a noop writes nothing); got "
        f"{dict(second_pause_result.changes)!r}"
    )

    verification_connection = get_connection(paused_database_path)
    try:
        assert get_metadata(verification_connection, "paused") == "1", (
            "the noop pause must leave poller_metadata['paused'] == '1' "
            "unchanged — the mutate step is skipped, not repeated"
        )
        pause_audit_rows = verification_connection.execute(
            "SELECT * FROM control_audit ORDER BY id"
        ).fetchall()
    finally:
        verification_connection.close()

    assert len(pause_audit_rows) == 2, (
        "the redundant pause is still a control action: two calls, two audit "
        "rows (§5: 'every control action' includes noops); got "
        f"{len(pause_audit_rows)}"
    )
    assert pause_audit_rows[1]["result"] == "noop: already paused", (
        "the second row must record result='noop: already paused' — the "
        "pinned plan 08 unit-2 wording of the 'noop: <why>' vocabulary; got "
        f"{pause_audit_rows[1]['result']!r}"
    )

    # --- resume on a never-paused DB: likewise an audited noop. -----------
    never_paused_database_path = tmp_path / "never_paused.db"
    initialize_database(never_paused_database_path)

    resume_result = resume(never_paused_database_path, "will", reason="oops")
    assert resume_result.status == "noop", (
        "resuming a never-paused system must return status='noop' (plan 08 "
        f"unit 2); got {resume_result.status!r}"
    )

    verification_connection = get_connection(never_paused_database_path)
    try:
        # Sentinel default distinguishes 'key absent' from a written '0': the
        # noop must skip the mutate step, not upsert an equivalent value.
        assert (
            get_metadata(verification_connection, "paused", default="ABSENT")
            == "ABSENT"
        ), (
            "the noop resume must write NO metadata — the 'paused' key stays "
            "absent, it is not upserted to '0' (plan 08 §2: mutate only on ok)"
        )
        resume_audit_rows = verification_connection.execute(
            "SELECT * FROM control_audit ORDER BY id"
        ).fetchall()
    finally:
        verification_connection.close()

    assert len(resume_audit_rows) == 1, (
        "the noop resume must write exactly one audit row; got "
        f"{len(resume_audit_rows)}"
    )
    assert resume_audit_rows[0]["action"] == "resume", (
        "the audited action string is the op's public function name (D-11); "
        f"got {resume_audit_rows[0]['action']!r}"
    )
    assert resume_audit_rows[0]["result"].startswith("noop: "), (
        "the noop resume must audit in the 'noop: <why>' vocabulary (plan 07 "
        f"§2 / plan 08 §2); got {resume_audit_rows[0]['result']!r}"
    )


@pytest.mark.parametrize(
    ("stored_paused_value", "expected_is_paused"),
    [
        pytest.param(None, False, id="key-absent-means-not-paused"),
        pytest.param("0", False, id="literal-0-means-not-paused"),
        pytest.param("1", True, id="only-literal-1-means-paused"),
    ],
)
def test_is_paused_only_the_literal_1_means_paused(
    tmp_path, stored_paused_value, expected_is_paused
):
    """``is_paused(conn)`` is the single canonical parse of the B5 vocabulary: only the literal ``"1"`` means paused — key absent and ``"0"`` both mean not paused.

    The daemon's loop head (subsystem 9) reuses exactly this function, so the
    sluice decision pinned here is the value grammar's one-sidedness: a fresh
    database (key never written) and a resumed one (``"0"``) are both
    NOT paused, and no other truthy-looking value is granted pause power. The
    regression this catches is a "helpful" looser parse (e.g. any non-empty /
    non-"0" value, or bool(int(value))) that would silently pause the fleet on
    a garbled metadata write instead of ignoring it.
    """
    # Local import: control is the module under construction (same rationale
    # as the unit-1 tests above).
    from sluice.core.control import PAUSED_KEY, is_paused
    from sluice.core.db import get_connection, initialize_database, set_metadata

    database_path = tmp_path / "sluice.db"
    initialize_database(database_path)

    connection = get_connection(database_path)
    try:
        if stored_paused_value is not None:
            # Plant the metadata value directly (via the public vocabulary
            # key) rather than through pause/resume: the parse must be
            # judged against the stored bytes themselves, independent of
            # which writer produced them.
            set_metadata(connection, PAUSED_KEY, stored_paused_value)
        assert is_paused(connection) is expected_is_paused, (
            f"with poller_metadata[{PAUSED_KEY!r}] "
            f"{'absent' if stored_paused_value is None else f'= {stored_paused_value!r}'}, "
            f"is_paused must return {expected_is_paused} — only the literal "
            "'1' means paused (plan 08 §2 B5: the one canonical parse the "
            "daemon reuses at its loop head)"
        )
    finally:
        connection.close()


# --------------------------------------------------------------------------
# Unit 3 — hold / unhold: the first run-targeting op.
# --------------------------------------------------------------------------


def test_hold_sets_lifecycle_trio_and_shares_the_audit_instant(tmp_path):
    """``hold`` writes the full lifecycle trio with ``lifecycle_at`` string-equal to its audit row's ``ts``, and the held run vanishes from ``select_active_runs``.

    The first run-targeting op, pinning two sluice decisions at once. One:
    every lifecycle transition writes the TRIO (plan 08 §2 — ``lifecycle_by``,
    ``lifecycle_at``, ``lifecycle_reason``, never a bare ``lifecycle`` flip),
    so the row always names its latest disposition's provenance; and the
    stamp instant is the SAME string as the audit row's ``ts`` — the
    shared-instant ``ts=`` parameter (plan 07 unit 1) exercised for real. A
    second ``now_iso()`` call inside the op would usually produce an equal
    string and only occasionally differ (microsecond boundary), so
    string-equality against the trail is the honest pin that state and
    evidence cite one instant. Two: ``lifecycle='held'`` is exactly what
    removes the run from the daemon's eligibility set — ``hold`` "stops"
    work purely by making the run unselectable (``ACTIVE_RUNS_WHERE``), which
    is the whole state-only mechanism §7 decision A stands on.
    """
    # Local import: control is the module under construction (same rationale
    # as the unit-1 tests above).
    from sluice.core.control import hold
    from sluice.core.db import (
        get_connection,
        get_run,
        initialize_database,
        insert_run,
        select_active_runs,
    )

    database_path = tmp_path / "sluice.db"
    initialize_database(database_path)
    with contextlib.closing(get_connection(database_path)) as setup_connection:
        insert_run(setup_connection, "run-001", pipeline_version="reference-v1")
        # Precondition, so the vanishing assertion below is meaningful: a
        # freshly-detected run IS daemon-eligible.
        assert [row["name"] for row in select_active_runs(setup_connection)] == [
            "run-001"
        ]

    hold(database_path, "will", "run-001", reason="instrument recheck")

    # Read back on a FRESH connection: what landed is what was committed.
    with contextlib.closing(get_connection(database_path)) as verification_connection:
        held_row = get_run(verification_connection, "run-001")
        audit_rows = verification_connection.execute(
            "SELECT * FROM control_audit"
        ).fetchall()
        active_run_names = [
            row["name"] for row in select_active_runs(verification_connection)
        ]

    assert held_row["lifecycle"] == "held", (
        "hold must write lifecycle='held' — the disposition the daemon's "
        "checkpoints react to (§7 decision A); got "
        f"{held_row['lifecycle']!r}"
    )
    assert held_row["lifecycle_by"] == "will", (
        "hold must stamp lifecycle_by with the acting operator — the trio "
        "names the disposition's provenance (plan 08 §2); got "
        f"{held_row['lifecycle_by']!r}"
    )
    assert held_row["lifecycle_reason"] == "instrument recheck", (
        "hold must stamp lifecycle_reason with the operator's given reason "
        f"(plan 08 §2 — the trio, never a bare flip); got "
        f"{held_row['lifecycle_reason']!r}"
    )

    assert len(audit_rows) == 1, (
        f"one hold call must write exactly ONE control_audit row; got {len(audit_rows)}"
    )
    assert held_row["lifecycle_at"] == audit_rows[0]["ts"], (
        "lifecycle_at must be STRING-EQUAL to the audit row's ts — one "
        "now_iso() instant shared between state stamp and audit row (§2 "
        "binding choreography: shared_now, passed via record_control_action's "
        f"ts= parameter); got lifecycle_at={held_row['lifecycle_at']!r} vs "
        f"ts={audit_rows[0]['ts']!r} — a second clock read means state and "
        "evidence can cite different instants"
    )

    assert active_run_names == [], (
        "the held run must vanish from select_active_runs — "
        "lifecycle='held' fails ACTIVE_RUNS_WHERE, which is the entire "
        "state-only mechanism by which hold stops the daemon driving the run "
        f"(design §2/§7); still active: {active_run_names!r}"
    )


def test_hold_is_state_only_no_gate_function_runs(tmp_path):
    """``hold`` on a run whose workdir holds beta's in-flight artifacts leaves the workdir byte-identical and no ``mock_beta.terminated`` trace — and its signature has no ``registry`` parameter.

    Spec §9's ``hold`` row says "via terminate", which reads as if the op
    itself interrupts the gate — plan 08 §7 decision A pins the opposite:
    ``hold`` writes ``lifecycle='held'`` and RETURNS; calling
    ``gate.terminate(ctx)`` is the *daemon's reaction* at its next checkpoint.
    Beta's terminate touches ``mock_beta.terminated`` precisely so termination
    is assertable from disk alone (plan 02 §3.3), which makes its absence here
    the honest behavioral pin that no terminate ran. The file-set snapshot
    additionally pins ADR-0005's "ops touch exactly one thing: the database" —
    behaviorally, not just via the import-hygiene ast guard. The signature pin
    is the structural half: a state-only op cannot even NAME a gate, so a
    future refactor that threads ``registry`` into ``hold`` "for symmetry"
    with the gate-state ops — the first step toward an op-side terminate —
    goes red before any terminate call exists.
    """
    # Local import: control is the module under construction (same rationale
    # as the unit-1 tests above).
    from sluice.core.control import hold
    from sluice.core.db import get_connection, initialize_database, insert_run

    database_path = tmp_path / "sluice.db"
    initialize_database(database_path)
    with contextlib.closing(get_connection(database_path)) as setup_connection:
        insert_run(setup_connection, "run-001", pipeline_version="reference-v1")

    # A workdir mid-flight, name-keyed under a staging root exactly as
    # resolve_context lays it out ({staging}/{run name}, plan 04 §3.3),
    # holding beta's artifacts: the interruptible gate could plausibly be
    # running right now — the exact moment an operator reaches for hold. The
    # artifact names are plan 02 §3.3's pinned literals (MOCK_BETA.produces),
    # spelled out so this test states on its face which files must survive.
    workdir = tmp_path / "staging" / "run-001"
    workdir.mkdir(parents=True)
    (workdir / "mock_alpha.marker").write_text("", encoding="utf-8")
    (workdir / "mock_beta.instruction").write_text("success", encoding="utf-8")
    (workdir / "mock_beta.marker").write_text("", encoding="utf-8")
    workdir_contents_before = {
        artifact_path.name: artifact_path.read_bytes()
        for artifact_path in workdir.iterdir()
    }

    hold(database_path, "will", "run-001", reason="instrument recheck")

    assert not (workdir / "mock_beta.terminated").exists(), (
        "hold must never call a gate's terminate — beta's terminate touches "
        "mock_beta.terminated as its from-disk-assertable trace (plan 02 "
        "§3.3), and that trace appeared. Stopping work is the DAEMON's "
        "reaction to lifecycle='held' at its next checkpoint (§7 decision A); "
        "the op only writes state"
    )
    workdir_contents_after = {
        artifact_path.name: artifact_path.read_bytes()
        for artifact_path in workdir.iterdir()
    }
    assert workdir_contents_after == workdir_contents_before, (
        "hold must leave the workdir file set and contents byte-identical — "
        "ops touch exactly one thing, the database (ADR-0005 / plan 08 §1); "
        f"before: {sorted(workdir_contents_before)!r}, after: "
        f"{sorted(workdir_contents_after)!r}"
    )

    assert "registry" not in inspect.signature(hold).parameters, (
        "hold must have NO registry parameter — state-only ops structurally "
        "cannot even name a gate (plan 08 unit 3): handing hold a registry "
        "is the first step toward an op-side terminate, which belongs to the "
        "daemon alone (§7 decision A)"
    )


@pytest.mark.parametrize(
    ("initial_lifecycle", "op_name", "expected_outcome"),
    [
        pytest.param("held", "hold", "noop", id="held-then-hold-is-an-audited-noop"),
        pytest.param("skipped", "hold", "refused", id="skipped-then-hold-is-refused"),
        pytest.param("held", "unhold", "ok", id="held-then-unhold-restamps-the-trio"),
        pytest.param(
            "active", "unhold", "noop", id="active-then-unhold-is-an-audited-noop"
        ),
        pytest.param(
            "skipped", "unhold", "refused", id="skipped-then-unhold-is-refused"
        ),
    ],
)
def test_hold_and_unhold_lifecycle_matrix(
    tmp_path, initial_lifecycle, op_name, expected_outcome
):
    """``hold``/``unhold`` follow plan 08 §2's lifecycle matrix: same-state is an audited noop, the cross-disposition ``skipped`` state is an audited-and-emitted ``LifecycleError`` refusal that leaves the other operator's disposition untouched, and a real ``unhold`` re-stamps the full trio to the unholding actor.

    ``lifecycle`` is ONE field, so the failure mode this matrix exists to
    prevent is an op silently undoing another operator's disposition: an
    ``unhold`` (or ``hold``) that blindly wrote over ``skipped`` would erase a
    skip decision nobody revoked. The matrix pins all three branches of the §2
    decision step on the first op pair that has them: cross-state → refusal
    (``LifecycleError`` raised AFTER its ``"refused: ..."`` audit row commits
    and its ``control.finished`` event — carrying ``"status": "refused"`` in
    ``control.result`` — is emitted, so the trail and the stream record the
    blocked attempt while the stored disposition and its provenance survive
    byte-identical); same-state → an audited ``"noop: ..."`` that skips the
    mutate (the planted trio survives — a "helpful" re-stamp would rewrite
    provenance to an actor who changed nothing); and the legitimate held →
    unhold transition re-stamps the FULL trio to the unholding actor at the
    shared audit instant (§2: "including un-ops, so the row always names its
    latest disposition's provenance" — an unhold that only flipped
    ``lifecycle`` would leave the row citing the holder as the author of its
    activeness).
    """
    # Local import: control is the module under construction (same rationale
    # as the unit-1 tests above).
    from sluice.core.control import LifecycleError, hold, unhold
    from sluice.core.db import (
        get_connection,
        get_run,
        initialize_database,
        insert_run,
        update_run,
    )

    op_under_test = {"hold": hold, "unhold": unhold}[op_name]

    database_path = tmp_path / "sluice.db"
    initialize_database(database_path)
    with contextlib.closing(get_connection(database_path)) as setup_connection:
        # A fresh insert IS the 'active' case (the schema default); for the
        # others, plant the pre-existing disposition directly with its full
        # provenance trio, as any real transition would have written it —
        # planting via update_run rather than through skip/hold both keeps the
        # matrix judged against the stored lifecycle value itself and avoids
        # depending on unit 4's skip, which does not exist yet.
        insert_run(setup_connection, "run-001", pipeline_version="reference-v1")
        if initial_lifecycle != "active":
            update_run(
                setup_connection,
                "run-001",
                {
                    "lifecycle": initial_lifecycle,
                    "lifecycle_by": "first-operator",
                    "lifecycle_at": "2026-07-28T00:00:00+00:00",
                    "lifecycle_reason": "planted disposition",
                },
            )

    with structlog.testing.capture_logs() as captured_log_entries:
        if expected_outcome == "refused":
            with pytest.raises(LifecycleError):
                op_under_test(
                    database_path, "second-operator", "run-001", reason="matrix probe"
                )
        else:
            returned_result = op_under_test(
                database_path, "second-operator", "run-001", reason="matrix probe"
            )

    with contextlib.closing(get_connection(database_path)) as verification_connection:
        run_row = get_run(verification_connection, "run-001")
        audit_rows = verification_connection.execute(
            "SELECT * FROM control_audit"
        ).fetchall()

    # Every attempt — ok, noop, AND refusal — is one audit row and one event:
    # §5's "every control action" includes redundant and blocked ones, and
    # every op calls control_event exactly once after its audit row (plan 03
    # §7 / plan 08 §2 — the stream and the trail agree on every attempt).
    assert len(audit_rows) == 1, (
        f"one {op_name} attempt must write exactly ONE control_audit row "
        f"(refusals and noops included — §5 'every control action'); got "
        f"{len(audit_rows)}"
    )
    audit_row = audit_rows[0]
    assert audit_row["action"] == op_name, (
        "the audited action string is the op's public function name (D-11); "
        f"got {audit_row['action']!r}"
    )
    control_finished_entries = [
        entry for entry in captured_log_entries if entry["event"] == "control.finished"
    ]
    assert len(control_finished_entries) == 1, (
        f"one {op_name} attempt must emit exactly ONE control.finished — "
        "noops and refusals included (plan 08 §2: the event stream and the "
        f"trail agree on every attempt); captured {captured_log_entries!r}"
    )

    if expected_outcome == "ok":
        assert returned_result.status == "ok", (
            "held -> unhold is the legitimate transition and must report "
            f"status='ok'; got {returned_result.status!r}"
        )
        assert audit_row["result"] == "ok", (
            f"the real transition audits result='ok'; got {audit_row['result']!r}"
        )
        assert run_row["lifecycle"] == "active", (
            "unhold of a held run must restore lifecycle='active' — the run "
            f"re-enters ACTIVE_RUNS_WHERE eligibility; got {run_row['lifecycle']!r}"
        )
        assert run_row["lifecycle_by"] == "second-operator", (
            "unhold must re-stamp lifecycle_by to the UNHOLDING actor — the "
            "trio is written on un-ops too, so the row names its latest "
            "disposition's provenance (plan 08 §2); got "
            f"{run_row['lifecycle_by']!r} (still the holder means only the "
            "lifecycle flag was flipped)"
        )
        assert run_row["lifecycle_reason"] == "matrix probe", (
            "unhold must re-stamp lifecycle_reason with the unholding "
            f"operator's reason (the trio, never a bare flip); got "
            f"{run_row['lifecycle_reason']!r}"
        )
        assert run_row["lifecycle_at"] == audit_row["ts"], (
            "the re-stamped lifecycle_at must be STRING-EQUAL to the audit "
            "row's ts — un-ops share one now_iso() instant with their audit "
            f"row too (§2 choreography); got {run_row['lifecycle_at']!r} vs "
            f"ts={audit_row['ts']!r}"
        )
    elif expected_outcome == "noop":
        assert returned_result.status == "noop", (
            f"{initial_lifecycle} -> {op_name} is same-state and must report "
            f"status='noop' (plan 08 §2 matrix); got {returned_result.status!r}"
        )
        assert audit_row["result"].startswith("noop: "), (
            "the redundant attempt audits in the 'noop: <why>' vocabulary "
            f"(plan 07 §2); got {audit_row['result']!r}"
        )
        assert run_row["lifecycle"] == initial_lifecycle, (
            f"the noop must leave lifecycle={initial_lifecycle!r} unchanged; "
            f"got {run_row['lifecycle']!r}"
        )
        # The mutate step is SKIPPED on a noop (unit-2 precedent): the stored
        # provenance survives — re-stamping the trio here would credit the
        # disposition to an actor who changed nothing.
        expected_untouched_lifecycle_by = (
            "first-operator" if initial_lifecycle != "active" else None
        )
        assert run_row["lifecycle_by"] == expected_untouched_lifecycle_by, (
            "the noop must not re-stamp the trio — lifecycle_by should still "
            f"be {expected_untouched_lifecycle_by!r} (the planted provenance), "
            f"got {run_row['lifecycle_by']!r}"
        )
    else:  # refused: the cross-disposition guard, audited and emitted.
        assert audit_row["result"].startswith("refused: "), (
            f"skipped -> {op_name} must audit result='refused: <why>' — a "
            "blocked attempt on real state is exactly what the trail is for "
            f"(plan 08 §2); got {audit_row['result']!r}"
        )
        assert control_finished_entries[0]["control.result"]["status"] == "refused", (
            "the refusal's control.finished must carry 'status': 'refused' in "
            "control.result — the stream records the blocked attempt (plan 08 "
            f"§2); got {control_finished_entries[0]!r}"
        )
        assert run_row["lifecycle"] == "skipped", (
            f"the refused {op_name} must leave lifecycle='skipped' untouched "
            "— cross-disposition writes would silently undo another "
            f"operator's skip (plan 08 §2 matrix); got {run_row['lifecycle']!r}"
        )
        assert run_row["lifecycle_by"] == "first-operator", (
            "the refused attempt must leave the skip's provenance untouched — "
            f"lifecycle_by should still be 'first-operator', got "
            f"{run_row['lifecycle_by']!r}"
        )


def test_hold_unknown_run_fails_loud_with_no_audit_and_no_event(tmp_path):
    """``hold`` of a nonexistent run raises ``UnknownRunError`` leaving the audit trail untouched and emitting zero ``control.finished`` events.

    The validation side of plan 08 §2's audited/not-audited line, exercised on
    a real op for the first time: a typo'd run name identifies no real state
    to act on, so it is never audited (a row would seed the trail with junk
    ``target_run`` values) and never emitted. The zero-events half doubles as
    the no-event-without-commit pin (plan 03 §7 persist-then-emit): the raise
    aborts the op's transaction, so nothing was committed for an event to
    describe — an implementation that emits before (or despite) the rollback
    would announce a hold that never landed, and the daemon-facing jsonl
    stream would disagree with the database. The regression this catches is
    the tempting "log every attempt" refactor that routes validation failures
    through the same audit-and-emit tail as ``ControlError`` refusals.
    """
    # Local import: control is the module under construction (same rationale
    # as the unit-1 tests above).
    from sluice.core.control import UnknownRunError, hold
    from sluice.core.db import get_connection, initialize_database, insert_run

    database_path = tmp_path / "sluice.db"
    initialize_database(database_path)
    # One real run exists, so the miss below is a genuine lookup failure among
    # real rows — not a degenerate query against an empty table.
    with contextlib.closing(get_connection(database_path)) as setup_connection:
        insert_run(setup_connection, "run-001", pipeline_version="reference-v1")
        audit_row_count_before = setup_connection.execute(
            "SELECT COUNT(*) FROM control_audit"
        ).fetchone()[0]

    with structlog.testing.capture_logs() as captured_log_entries:
        with pytest.raises(UnknownRunError):
            hold(database_path, "will", "run-999", reason="typo'd name")

    with contextlib.closing(get_connection(database_path)) as verification_connection:
        audit_row_count_after = verification_connection.execute(
            "SELECT COUNT(*) FROM control_audit"
        ).fetchone()[0]
    assert audit_row_count_after == audit_row_count_before, (
        "an unknown-run hold must leave control_audit untouched — validation "
        "failures are silent to the trail (plan 08 §2: auditing a typo would "
        "seed junk target_run values); the trail grew from "
        f"{audit_row_count_before} to {audit_row_count_after} rows"
    )

    control_finished_entries = [
        entry for entry in captured_log_entries if entry["event"] == "control.finished"
    ]
    assert control_finished_entries == [], (
        "an unknown-run hold must emit ZERO control.finished events — nothing "
        "was committed for an event to describe (no-event-without-commit, "
        "plan 03 §7 / plan 08 §2), and validation failures are never emitted; "
        f"captured {control_finished_entries!r}"
    )


def test_hold_works_on_a_quarantined_run(tmp_path):
    """``hold`` of a quarantined run succeeds with ``status="ok"`` and leaves the ``errored_at``/``error_reason`` pair byte-identical — quarantine and disposition are orthogonal (F4).

    The eligibility predicate is two AND-ed clauses (``lifecycle = 'active'
    AND errored_at IS NULL``), deliberately independent axes: quarantine
    records *what went wrong*, disposition records *what the operator wants*.
    A quarantined run is exactly one an operator plausibly holds (park it
    while investigating the error), so the two mutants this test kills are
    both real F4 confusions: a hold that REFUSES because ``errored_at`` is
    set (conflating the axes would leave quarantined runs un-holdable, forcing
    ``clear_error`` first and losing the error evidence mid-investigation),
    and a hold that "helpfully" clears the error pair on its way (an
    unaudited back-door ``clear_error`` — ADR-0002's sole sanctioned recovery
    is the explicit op, and unit 5's both-ops recovery depends on hold never
    touching the pair).
    """
    # Local import: control is the module under construction (same rationale
    # as the unit-1 tests above).
    from sluice.core.control import hold
    from sluice.core.db import (
        get_connection,
        get_run,
        initialize_database,
        insert_run,
        update_run,
    )

    database_path = tmp_path / "sluice.db"
    initialize_database(database_path)
    # Plant the quarantine pair together (the paired CHECK forbids a
    # half-set), exactly as the daemon's failure persist would have written
    # it — the run is active AND quarantined when the operator reaches for
    # hold.
    planted_errored_at = "2026-07-28T00:00:00+00:00"
    planted_error_reason = "mock_beta: scripted failure"
    with contextlib.closing(get_connection(database_path)) as setup_connection:
        insert_run(setup_connection, "run-001", pipeline_version="reference-v1")
        update_run(
            setup_connection,
            "run-001",
            {
                "errored_at": planted_errored_at,
                "error_reason": planted_error_reason,
            },
        )

    returned_result = hold(
        database_path, "will", "run-001", reason="park while investigating"
    )

    assert returned_result.status == "ok", (
        "hold of a quarantined run must succeed with status='ok' — "
        "quarantine and disposition are orthogonal axes (plan 08 §2, F4), so "
        "errored_at set is no reason to refuse a disposition write; got "
        f"{returned_result.status!r}"
    )

    with contextlib.closing(get_connection(database_path)) as verification_connection:
        held_row = get_run(verification_connection, "run-001")

    assert held_row["lifecycle"] == "held", (
        "the quarantined run must actually be held — the disposition axis "
        f"moves independently of the error axis (F4); got "
        f"{held_row['lifecycle']!r}"
    )
    assert held_row["errored_at"] == planted_errored_at, (
        "hold must leave errored_at byte-identical — clearing it would be an "
        "unaudited back-door clear_error (ADR-0002: the explicit op is the "
        "sole sanctioned recovery), and unit 5's held-AND-quarantined "
        f"recovery depends on the pair surviving; got "
        f"{held_row['errored_at']!r}, planted {planted_errored_at!r}"
    )
    assert held_row["error_reason"] == planted_error_reason, (
        "hold must leave error_reason byte-identical — the error evidence "
        "survives the disposition change (quarantine is orthogonal, F4); got "
        f"{held_row['error_reason']!r}, planted {planted_error_reason!r}"
    )


# --------------------------------------------------------------------------
# Unit 4 — skip / unskip: protection debuts.
# --------------------------------------------------------------------------


def test_skip_sets_lifecycle_and_leaves_completed_at_null(tmp_path):
    """``skip`` writes ``lifecycle='skipped'`` with the full provenance trio, removes the run from ``select_active_runs``, and leaves ``completed_at`` SQL NULL — a skip is a disposition, never a fabricated completion.

    The spec §9 pin this exists for is the ``completed_at IS NULL`` half:
    "we are done driving this run" (disposition) and "the pipeline finished
    this run" (completion) are different facts recorded in different columns,
    and the tempting mutant is a skip that stamps ``completed_at`` as a
    terminal marker. That stamp would be a lie with teeth — ``run_outcome``
    and ``get_status`` would render an abandoned run as completed, and a later
    ``unskip`` would resume a run whose row already claims it finished. The
    trio and eligibility assertions pin the same state-only mechanism as
    ``hold`` (plan 08 §2: every transition writes the trio, so the row names
    its latest disposition's provenance; ``lifecycle='skipped'`` fails
    ``ACTIVE_RUNS_WHERE``, which is the entire way a skip stops the daemon),
    now on the op that debuts them for the skip disposition.
    """
    # Local import: control is the module under construction (same rationale
    # as the unit-1 tests above).
    from sluice.core.control import skip
    from sluice.core.db import (
        get_connection,
        get_run,
        initialize_database,
        insert_run,
        select_active_runs,
    )

    database_path = tmp_path / "sluice.db"
    initialize_database(database_path)
    with contextlib.closing(get_connection(database_path)) as setup_connection:
        insert_run(setup_connection, "run-001", pipeline_version="reference-v1")
        # Precondition, so the vanishing assertion below is meaningful: a
        # freshly-detected run IS daemon-eligible.
        assert [row["name"] for row in select_active_runs(setup_connection)] == [
            "run-001"
        ]

    skip(database_path, "will", "run-001", reason="flowcell unusable")

    # Read back on a FRESH connection: what landed is what was committed.
    with contextlib.closing(get_connection(database_path)) as verification_connection:
        skipped_row = get_run(verification_connection, "run-001")
        audit_rows = verification_connection.execute(
            "SELECT * FROM control_audit"
        ).fetchall()
        active_run_names = [
            row["name"] for row in select_active_runs(verification_connection)
        ]

    assert skipped_row["lifecycle"] == "skipped", (
        "skip must write lifecycle='skipped' — the disposition the daemon's "
        f"eligibility predicate excludes (design §5/§9); got "
        f"{skipped_row['lifecycle']!r}"
    )
    assert skipped_row["lifecycle_by"] == "will", (
        "skip must stamp lifecycle_by with the acting operator — every "
        "transition writes the trio, so the row names its disposition's "
        f"provenance (plan 08 §2); got {skipped_row['lifecycle_by']!r}"
    )
    assert skipped_row["lifecycle_reason"] == "flowcell unusable", (
        "skip must stamp lifecycle_reason with the operator's given reason "
        f"(plan 08 §2 — the trio, never a bare flip); got "
        f"{skipped_row['lifecycle_reason']!r}"
    )
    assert skipped_row["completed_at"] is None, (
        "skip must leave completed_at SQL NULL — the spec §9 pin: a skipped "
        "run was abandoned, not finished, and stamping completed_at would "
        "make run_outcome/get_status render the abandonment as pipeline "
        f"completion; got {skipped_row['completed_at']!r}"
    )
    assert active_run_names == [], (
        "the skipped run must vanish from select_active_runs — "
        "lifecycle='skipped' fails ACTIVE_RUNS_WHERE, which is the entire "
        "state-only mechanism by which skip stops the daemon driving the run "
        f"(design §2); still active: {active_run_names!r}"
    )

    # State + audit land together (§2 binding choreography; the invariant
    # every op test asserts): one call, one row, the D-11 action name.
    assert len(audit_rows) == 1, (
        f"one skip call must write exactly ONE control_audit row; got {len(audit_rows)}"
    )
    assert audit_rows[0]["action"] == "skip", (
        "the audited action string is the op's public function name (D-11); "
        f"got {audit_rows[0]['action']!r}"
    )
    assert audit_rows[0]["result"] == "ok", (
        "a successful skip audits result='ok' (plan 07 §2 vocabulary); got "
        f"{audit_rows[0]['result']!r}"
    )


def test_skip_on_protected_run_refuses_unless_force(tmp_path):
    """``skip`` of a protected run raises ``ProtectedError`` naming the protector, audits the refusal, and changes nothing — while ``force=True`` bypasses the refusal, audits the flag, and leaves ``protected_at``/``protected_by`` intact.

    Protection's debut (errata A3): ``skip`` is in the protected set because
    it removes a run from the daemon's reach — exactly the class of
    progress-destroying disposition ``protect`` exists to guard. Three
    mutants this test kills, each a real A3/§2 confusion: a skip that
    ignores ``protected_at`` entirely (protection would guard nothing); a
    refusal that skips its audit row or mutates state before refusing
    (plan 08 §2: a ``ControlError`` is a refused REAL action — audited
    ``"refused: <why>"`` with the run row byte-identical, unlike silent
    validation); and a ``force=True`` that "consumes" protection by
    clearing ``protected_*`` on its way through (force bypasses ONE
    refusal, it never unprotects — ``unprotect`` is the sole clear path).
    The error naming ``protected_by`` and the audited ``"force": true``
    are the trail's evidence: who guarded the run, and that the operator
    explicitly overrode the guard.
    """
    # Local import: control is the module under construction (same rationale
    # as the unit-1 tests above).
    from sluice.core.control import ProtectedError, skip
    from sluice.core.db import (
        get_connection,
        get_run,
        initialize_database,
        insert_run,
        update_run,
    )

    database_path = tmp_path / "sluice.db"
    initialize_database(database_path)
    # Plant the protection pair together (the paired CHECK forbids a
    # half-set), exactly as unit 6's `protect` op will write it. The
    # protector is deliberately NOT the skipping actor: the refusal must
    # name whoever guarded the run, not whoever is knocking.
    planted_protected_at = "2026-07-28T00:00:00+00:00"
    planted_protected_by = "alice"
    with contextlib.closing(get_connection(database_path)) as setup_connection:
        insert_run(setup_connection, "run-001", pipeline_version="reference-v1")
        update_run(
            setup_connection,
            "run-001",
            {
                "protected_at": planted_protected_at,
                "protected_by": planted_protected_by,
            },
        )
        row_before_refusal = tuple(get_run(setup_connection, "run-001"))

    # --- Without force: a policy refusal — audited, emitted, raised. ---
    with pytest.raises(ProtectedError) as raised_refusal:
        skip(database_path, "will", "run-001", reason="flowcell unusable")

    assert planted_protected_by in str(raised_refusal.value), (
        "the ProtectedError must name protected_by — the operator's next "
        "move is to talk to whoever guarded the run (errata A3); got "
        f"{str(raised_refusal.value)!r}"
    )

    with contextlib.closing(get_connection(database_path)) as verification_connection:
        row_after_refusal = tuple(get_run(verification_connection, "run-001"))
        refusal_audit_rows = verification_connection.execute(
            "SELECT * FROM control_audit ORDER BY id"
        ).fetchall()

    assert row_after_refusal == row_before_refusal, (
        "a refused skip must leave the run row byte-identical — a refusal "
        "writes ONLY its audit row (plan 08 §2 choreography: mutate skipped, "
        "audit committed, then raise); the row changed: "
        f"before {row_before_refusal!r}, after {row_after_refusal!r}"
    )
    assert len(refusal_audit_rows) == 1, (
        "the refused skip must write exactly ONE control_audit row — §5's "
        "'every control action' includes refused attempts (a refused op on a "
        "protected run is exactly what a trail is for); got "
        f"{len(refusal_audit_rows)}"
    )
    assert refusal_audit_rows[0]["action"] == "skip", (
        "the refusal audits under the op's public function name (D-11); got "
        f"{refusal_audit_rows[0]['action']!r}"
    )
    assert refusal_audit_rows[0]["result"].startswith("refused: "), (
        "a ProtectedError refusal audits result='refused: <why>' (plan 07 §2 "
        f"vocabulary); got {refusal_audit_rows[0]['result']!r}"
    )

    # --- With force: the operator takes responsibility — the op proceeds. ---
    forced_result = skip(
        database_path, "will", "run-001", reason="flowcell unusable", force=True
    )
    assert forced_result.status == "ok", (
        "skip with force=True on a protected run must succeed — force "
        f"bypasses the ProtectedError refusal (spec §9); got "
        f"{forced_result.status!r}"
    )

    with contextlib.closing(get_connection(database_path)) as after_force_connection:
        forced_row = get_run(after_force_connection, "run-001")
        all_audit_rows = after_force_connection.execute(
            "SELECT * FROM control_audit ORDER BY id"
        ).fetchall()

    assert forced_row["lifecycle"] == "skipped", (
        "the forced skip must actually land lifecycle='skipped'; got "
        f"{forced_row['lifecycle']!r}"
    )
    assert len(all_audit_rows) == 2, (
        "the forced skip writes its own audit row after the refusal's; got "
        f"{len(all_audit_rows)} rows"
    )
    forced_audit_args = json.loads(all_audit_rows[1]["args_json"])
    assert forced_audit_args.get("force") is True, (
        'the forced skip\'s audit args_json must include "force": true — '
        "the trail's evidence that the operator explicitly overrode the "
        f"guard (plan 08 §2: args record the op's scalar parameters); got "
        f"{all_audit_rows[1]['args_json']!r}"
    )
    assert forced_row["protected_at"] == planted_protected_at, (
        "force must leave protected_at byte-identical — force bypasses one "
        "refusal, it never unprotects (errata A3: unprotect is the sole "
        f"clear path); got {forced_row['protected_at']!r}"
    )
    assert forced_row["protected_by"] == planted_protected_by, (
        "force must leave protected_by byte-identical — the run remains "
        f"guarded after the override (errata A3); got "
        f"{forced_row['protected_by']!r}"
    )


@pytest.mark.parametrize(
    ("initial_lifecycle", "op_name", "expected_outcome"),
    [
        pytest.param(
            "skipped", "skip", "noop", id="skipped-then-skip-is-an-audited-noop"
        ),
        pytest.param("held", "skip", "refused", id="held-then-skip-is-refused"),
        pytest.param(
            "skipped", "unskip", "ok", id="skipped-then-unskip-restamps-the-trio"
        ),
        pytest.param(
            "active", "unskip", "noop", id="active-then-unskip-is-an-audited-noop"
        ),
        pytest.param("held", "unskip", "refused", id="held-then-unskip-is-refused"),
    ],
)
def test_skip_and_unskip_lifecycle_matrix(
    tmp_path, initial_lifecycle, op_name, expected_outcome
):
    """``skip``/``unskip`` follow plan 08 §2's lifecycle matrix with roles swapped from ``hold``/``unhold``: same-state is an audited noop, the cross-disposition ``held`` state is an audited-and-emitted ``LifecycleError`` refusal leaving the hold untouched, and a real ``unskip`` re-stamps the full trio to the unskipping actor.

    The mirror of unit 3's matrix (plan 08 unit 4), guarding the same single
    ``lifecycle`` field from the other side: a ``skip`` (or ``unskip``) that
    blindly wrote over ``held`` would silently release — or discard — another
    operator's hold that nobody revoked. All three branches of the §2 decision
    step are pinned for the skip pair: cross-state → refusal (``LifecycleError``
    raised AFTER its ``"refused: ..."`` audit row commits and its
    ``control.finished`` — carrying ``"status": "refused"`` in
    ``control.result`` — is emitted, with the hold's disposition and provenance
    surviving byte-identical); same-state → an audited ``"noop: ..."`` that
    skips the mutate (the planted trio survives — a "helpful" re-stamp would
    credit the disposition to an actor who changed nothing); and the
    legitimate skipped → unskip transition re-stamps the FULL trio to the
    unskipping actor at the shared audit instant (§2: un-ops write the trio
    too, so the row always names its latest disposition's provenance).
    """
    # Local import: control is the module under construction (same rationale
    # as the unit-1 tests above).
    from sluice.core.control import LifecycleError, skip, unskip
    from sluice.core.db import (
        get_connection,
        get_run,
        initialize_database,
        insert_run,
        update_run,
    )

    op_under_test = {"skip": skip, "unskip": unskip}[op_name]

    database_path = tmp_path / "sluice.db"
    initialize_database(database_path)
    with contextlib.closing(get_connection(database_path)) as setup_connection:
        # A fresh insert IS the 'active' case (the schema default); for the
        # others, plant the pre-existing disposition directly with its full
        # provenance trio, as any real transition would have written it —
        # planting via update_run keeps the matrix judged against the stored
        # lifecycle value itself, independent of which op wrote it.
        insert_run(setup_connection, "run-001", pipeline_version="reference-v1")
        if initial_lifecycle != "active":
            update_run(
                setup_connection,
                "run-001",
                {
                    "lifecycle": initial_lifecycle,
                    "lifecycle_by": "first-operator",
                    "lifecycle_at": "2026-07-28T00:00:00+00:00",
                    "lifecycle_reason": "planted disposition",
                },
            )

    with structlog.testing.capture_logs() as captured_log_entries:
        if expected_outcome == "refused":
            with pytest.raises(LifecycleError):
                op_under_test(
                    database_path, "second-operator", "run-001", reason="matrix probe"
                )
        else:
            returned_result = op_under_test(
                database_path, "second-operator", "run-001", reason="matrix probe"
            )

    with contextlib.closing(get_connection(database_path)) as verification_connection:
        run_row = get_run(verification_connection, "run-001")
        audit_rows = verification_connection.execute(
            "SELECT * FROM control_audit"
        ).fetchall()

    # Every attempt — ok, noop, AND refusal — is one audit row and one event:
    # §5's "every control action" includes redundant and blocked ones, and
    # every op calls control_event exactly once after its audit row (plan 03
    # §7 / plan 08 §2 — the stream and the trail agree on every attempt).
    assert len(audit_rows) == 1, (
        f"one {op_name} attempt must write exactly ONE control_audit row "
        f"(refusals and noops included — §5 'every control action'); got "
        f"{len(audit_rows)}"
    )
    audit_row = audit_rows[0]
    assert audit_row["action"] == op_name, (
        "the audited action string is the op's public function name (D-11); "
        f"got {audit_row['action']!r}"
    )
    control_finished_entries = [
        entry for entry in captured_log_entries if entry["event"] == "control.finished"
    ]
    assert len(control_finished_entries) == 1, (
        f"one {op_name} attempt must emit exactly ONE control.finished — "
        "noops and refusals included (plan 08 §2: the event stream and the "
        f"trail agree on every attempt); captured {captured_log_entries!r}"
    )

    if expected_outcome == "ok":
        assert returned_result.status == "ok", (
            "skipped -> unskip is the legitimate transition and must report "
            f"status='ok'; got {returned_result.status!r}"
        )
        assert audit_row["result"] == "ok", (
            f"the real transition audits result='ok'; got {audit_row['result']!r}"
        )
        assert run_row["lifecycle"] == "active", (
            "unskip of a skipped run must restore lifecycle='active' — the "
            f"run re-enters ACTIVE_RUNS_WHERE eligibility; got "
            f"{run_row['lifecycle']!r}"
        )
        assert run_row["lifecycle_by"] == "second-operator", (
            "unskip must re-stamp lifecycle_by to the UNSKIPPING actor — the "
            "trio is written on un-ops too, so the row names its latest "
            "disposition's provenance (plan 08 §2); got "
            f"{run_row['lifecycle_by']!r} (still the skipper means only the "
            "lifecycle flag was flipped)"
        )
        assert run_row["lifecycle_reason"] == "matrix probe", (
            "unskip must re-stamp lifecycle_reason with the unskipping "
            f"operator's reason (the trio, never a bare flip); got "
            f"{run_row['lifecycle_reason']!r}"
        )
        assert run_row["lifecycle_at"] == audit_row["ts"], (
            "the re-stamped lifecycle_at must be STRING-EQUAL to the audit "
            "row's ts — un-ops share one now_iso() instant with their audit "
            f"row too (§2 choreography); got {run_row['lifecycle_at']!r} vs "
            f"ts={audit_row['ts']!r}"
        )
    elif expected_outcome == "noop":
        assert returned_result.status == "noop", (
            f"{initial_lifecycle} -> {op_name} is same-state and must report "
            f"status='noop' (plan 08 §2 matrix); got {returned_result.status!r}"
        )
        assert audit_row["result"].startswith("noop: "), (
            "the redundant attempt audits in the 'noop: <why>' vocabulary "
            f"(plan 07 §2); got {audit_row['result']!r}"
        )
        assert run_row["lifecycle"] == initial_lifecycle, (
            f"the noop must leave lifecycle={initial_lifecycle!r} unchanged; "
            f"got {run_row['lifecycle']!r}"
        )
        # The mutate step is SKIPPED on a noop (unit-2 precedent): the stored
        # provenance survives — re-stamping the trio here would credit the
        # disposition to an actor who changed nothing.
        expected_untouched_lifecycle_by = (
            "first-operator" if initial_lifecycle != "active" else None
        )
        assert run_row["lifecycle_by"] == expected_untouched_lifecycle_by, (
            "the noop must not re-stamp the trio — lifecycle_by should still "
            f"be {expected_untouched_lifecycle_by!r} (the planted provenance), "
            f"got {run_row['lifecycle_by']!r}"
        )
    else:  # refused: the cross-disposition guard, audited and emitted.
        assert audit_row["result"].startswith("refused: "), (
            f"held -> {op_name} must audit result='refused: <why>' — a "
            "blocked attempt on real state is exactly what the trail is for "
            f"(plan 08 §2); got {audit_row['result']!r}"
        )
        assert control_finished_entries[0]["control.result"]["status"] == "refused", (
            "the refusal's control.finished must carry 'status': 'refused' in "
            "control.result — the stream records the blocked attempt (plan 08 "
            f"§2); got {control_finished_entries[0]!r}"
        )
        assert run_row["lifecycle"] == "held", (
            f"the refused {op_name} must leave lifecycle='held' untouched — "
            "cross-disposition writes would silently undo another operator's "
            f"hold (plan 08 §2 matrix); got {run_row['lifecycle']!r}"
        )
        assert run_row["lifecycle_by"] == "first-operator", (
            "the refused attempt must leave the hold's provenance untouched — "
            f"lifecycle_by should still be 'first-operator', got "
            f"{run_row['lifecycle_by']!r}"
        )


# --------------------------------------------------------------------------
# Unit 5 — clear_error: ADR-0002's sole sanctioned recovery.
# --------------------------------------------------------------------------


def test_clear_error_clears_the_pair_and_restores_eligibility(tmp_path):
    """``clear_error`` on a quarantined run NULLs ``errored_at`` and ``error_reason`` together, the run reappears in ``select_active_runs``, and the audit row records the forgiven reason.

    ADR-0002's sole sanctioned recovery: quarantine holds until an operator
    explicitly forgives it, and this op IS that forgiveness. Three sluice
    decisions pinned. One: the pair clears in one write (the paired CHECK
    makes a half-clear impossible — asserting both NULL after one call is the
    behavioral face of that schema decision). Two: clearing the pair is
    exactly what restores ``ACTIVE_RUNS_WHERE`` eligibility — state-only
    recovery, no daemon poke; the run simply becomes selectable again on the
    daemon's next stateless look. Three: the trail captures WHAT was forgiven
    — after the clear the run row itself no longer remembers the error, so
    the cleared ``error_reason`` must survive in the audit row's args/result
    detail or the evidence of what the operator waved through is gone with
    the state it excuses.
    """
    # Local import: control is the module under construction (same rationale
    # as the unit-1 tests above).
    from sluice.core.control import clear_error
    from sluice.core.db import (
        get_connection,
        get_run,
        initialize_database,
        insert_run,
        select_active_runs,
        update_run,
    )

    database_path = tmp_path / "sluice.db"
    initialize_database(database_path)
    # Plant the quarantine pair together (the paired CHECK forbids a
    # half-set), exactly as the daemon's failure persist would have written
    # it. The reason string is what the audit assertion below must find.
    planted_error_reason = "mock_beta: scripted failure"
    with contextlib.closing(get_connection(database_path)) as setup_connection:
        insert_run(setup_connection, "run-001", pipeline_version="reference-v1")
        update_run(
            setup_connection,
            "run-001",
            {
                "errored_at": "2026-07-28T00:00:00+00:00",
                "error_reason": planted_error_reason,
            },
        )
        # Precondition, so the reappearance assertion below is meaningful:
        # the quarantined run fails ACTIVE_RUNS_WHERE's errored_at IS NULL
        # clause and is invisible to the daemon.
        assert select_active_runs(setup_connection) == []

    returned_result = clear_error(
        database_path, "will", "run-001", reason="disk fixed, retrying"
    )

    assert returned_result.status == "ok", (
        "clear_error of a quarantined run is a real state change and must "
        f"report status='ok' (plan 08 §2); got {returned_result.status!r}"
    )

    # Read back on a FRESH connection: what landed is what was committed.
    with contextlib.closing(get_connection(database_path)) as verification_connection:
        cleared_row = get_run(verification_connection, "run-001")
        audit_rows = verification_connection.execute(
            "SELECT * FROM control_audit"
        ).fetchall()
        active_run_names = [
            row["name"] for row in select_active_runs(verification_connection)
        ]

    assert cleared_row["errored_at"] is None, (
        "clear_error must NULL errored_at — the quarantine stamp is forgiven "
        f"(ADR-0002's sole sanctioned recovery); got {cleared_row['errored_at']!r}"
    )
    assert cleared_row["error_reason"] is None, (
        "clear_error must NULL error_reason in the SAME write — the paired "
        "CHECK makes a half-clear impossible, and a surviving reason would "
        f"describe an error the row no longer records; got "
        f"{cleared_row['error_reason']!r}"
    )
    assert active_run_names == ["run-001"], (
        "the cleared run must reappear in select_active_runs — NULLing "
        "errored_at is exactly what restores ACTIVE_RUNS_WHERE eligibility "
        "(state-only recovery: the daemon's next stateless look re-selects "
        f"it, no poke needed); active: {active_run_names!r}"
    )

    # State + audit land together (§2 binding choreography): one call, one
    # row, the D-11 action name — and the forgiven reason preserved.
    assert len(audit_rows) == 1, (
        "one clear_error call must write exactly ONE control_audit row; got "
        f"{len(audit_rows)}"
    )
    audit_row = audit_rows[0]
    assert audit_row["action"] == "clear_error", (
        "the audited action string is the op's public function name (D-11); "
        f"got {audit_row['action']!r}"
    )
    assert audit_row["result"].startswith("ok"), (
        "a successful clear_error audits in the 'ok' vocabulary (plan 07 §2); "
        f"got {audit_row['result']!r}"
    )
    audit_evidence = f"{audit_row['args_json']} {audit_row['result']}"
    assert planted_error_reason in audit_evidence, (
        "the audit row must record the CLEARED error_reason in its args/"
        "result detail (plan 08 unit 5: the evidence of what was forgiven) — "
        "after the clear, the run row no longer remembers the error, so the "
        f"trail is the only record; searched args_json="
        f"{audit_row['args_json']!r} and result={audit_row['result']!r} for "
        f"{planted_error_reason!r}"
    )


def test_clear_error_on_a_clean_run_is_a_noop(tmp_path):
    """``clear_error`` on a never-quarantined run reports ``status='noop'`` and still writes an audit row in the ``"noop: <why>"`` vocabulary.

    The decision pinned: forgiving a run that has nothing to forgive is NOT a
    state change (a mutant that takes the ok path would report ``"ok"`` and
    audit ``"ok"``, fabricating a recovery that never happened), yet it is
    still an audited attempt (plan 08 §2: every state-writing op's call lands
    in the trail, noops included — a mutant that silently returns without the
    audit row leaves the operator's attempt invisible). Both halves asserted.
    """
    from sluice.core.control import clear_error
    from sluice.core.db import get_connection, initialize_database, insert_run

    database_path = tmp_path / "sluice.db"
    initialize_database(database_path)
    with contextlib.closing(get_connection(database_path)) as setup_connection:
        # A freshly-inserted run: errored_at/error_reason are NULL — clean.
        insert_run(setup_connection, "run-001", pipeline_version="reference-v1")

    returned_result = clear_error(
        database_path, "will", "run-001", reason="just in case"
    )

    assert returned_result.status == "noop", (
        "clear_error on a clean run must report status='noop' — there was no "
        "quarantine to forgive, and claiming 'ok' would fabricate a recovery "
        f"that never happened (plan 08 unit 5); got {returned_result.status!r}"
    )

    with contextlib.closing(get_connection(database_path)) as verification_connection:
        audit_rows = verification_connection.execute(
            "SELECT * FROM control_audit"
        ).fetchall()

    assert len(audit_rows) == 1, (
        "the noop is still AUDITED — one clear_error call writes exactly one "
        "control_audit row even when the mutate is skipped (plan 08 §2: the "
        f"trail records every attempt); got {len(audit_rows)} rows"
    )
    assert audit_rows[0]["action"] == "clear_error", (
        "the audited action string is the op's public function name (D-11); "
        f"got {audit_rows[0]['action']!r}"
    )
    assert audit_rows[0]["result"].startswith("noop: "), (
        "the redundant attempt audits in the 'noop: <why>' vocabulary (plan "
        f"07 §2 / plan 08 §2); got {audit_rows[0]['result']!r}"
    )


@pytest.mark.parametrize(
    ("first_recovery_op_name", "second_recovery_op_name"),
    [
        ("clear_error", "unhold"),
        ("unhold", "clear_error"),
    ],
    ids=["clear-error-first", "unhold-first"],
)
def test_recovery_from_held_and_quarantined_needs_both_ops(
    tmp_path, first_recovery_op_name, second_recovery_op_name
):
    """A run both held and quarantined re-enters ``select_active_runs`` only after BOTH ``unhold`` and ``clear_error``, in either order.

    D-06/E-12 via F4: eligibility is the AND of two orthogonal clauses
    (``lifecycle='active'`` AND ``errored_at IS NULL``), and each recovery op
    clears exactly its own axis — never the other's. The regression this
    catches is an over-reaching recovery op: a ``clear_error`` that
    "helpfully" reactivates the run (silently undoing another operator's
    hold) or an ``unhold`` that quietly forgives a quarantine nobody
    examined. Under such a mutant the first op alone would restore
    eligibility and the mid-recovery absence assertion — plus the
    untouched-axis assertion beside it — goes red. Both orders are asserted
    because the composition must be symmetric: neither op is a prerequisite
    of the other, they simply each own one clause of the predicate.
    """
    import sluice.core.control as control
    from sluice.core.db import (
        get_connection,
        get_run,
        initialize_database,
        insert_run,
        select_active_runs,
        update_run,
    )

    database_path = tmp_path / "sluice.db"
    initialize_database(database_path)
    with contextlib.closing(get_connection(database_path)) as setup_connection:
        insert_run(setup_connection, "run-001", pipeline_version="reference-v1")
    # Hold through the real op (the disposition an operator actually took),
    # then plant the quarantine pair exactly as the daemon's failure persist
    # would have written it — the two ineligibility axes now both apply.
    control.hold(database_path, "will", "run-001", reason="investigating")
    with contextlib.closing(get_connection(database_path)) as setup_connection:
        update_run(
            setup_connection,
            "run-001",
            {
                "errored_at": "2026-07-28T00:00:00+00:00",
                "error_reason": "mock_beta: scripted failure",
            },
        )
        # Precondition: both clauses of ACTIVE_RUNS_WHERE fail, so the later
        # mid-recovery absence assertion cannot pass vacuously.
        assert select_active_runs(setup_connection) == []

    first_recovery_op = getattr(control, first_recovery_op_name)
    second_recovery_op = getattr(control, second_recovery_op_name)

    first_result = first_recovery_op(
        database_path, "will", "run-001", reason="recovering"
    )
    assert first_result.status == "ok", (
        f"{first_recovery_op_name} on a held+quarantined run is a real state "
        "change on its own axis and must report 'ok' — otherwise the absence "
        f"assertion below proves nothing; got {first_result.status!r}"
    )

    with contextlib.closing(get_connection(database_path)) as midpoint_connection:
        midpoint_row = get_run(midpoint_connection, "run-001")
        midpoint_active_names = [
            row["name"] for row in select_active_runs(midpoint_connection)
        ]

    # The heart of D-06/E-12: one op down, the OTHER axis still blocks
    # eligibility — each recovery op clears only its own clause of the
    # AND-ed predicate, never reaching across into the other operator
    # decision it would silently undo.
    assert midpoint_active_names == [], (
        f"after {first_recovery_op_name} alone the run must STILL be absent "
        "from select_active_runs — eligibility is lifecycle='active' AND "
        "errored_at IS NULL (F4), and one op clears only one clause; got "
        f"active runs {midpoint_active_names!r}"
    )
    if first_recovery_op_name == "clear_error":
        assert midpoint_row["lifecycle"] == "held", (
            "clear_error must leave the hold in place — reactivating the run "
            "would silently undo another operator's disposition (plan 08 §2 "
            f"lifecycle doctrine); got lifecycle={midpoint_row['lifecycle']!r}"
        )
    else:
        assert midpoint_row["errored_at"] is not None, (
            "unhold must leave the quarantine stamp in place — forgiving an "
            "error is clear_error's job alone (ADR-0002's sole sanctioned "
            f"recovery); got errored_at={midpoint_row['errored_at']!r}"
        )

    second_result = second_recovery_op(
        database_path, "will", "run-001", reason="recovering"
    )
    assert second_result.status == "ok", (
        f"{second_recovery_op_name} clears the remaining axis — a real "
        f"change, so 'ok'; got {second_result.status!r}"
    )

    with contextlib.closing(get_connection(database_path)) as final_connection:
        final_active_names = [
            row["name"] for row in select_active_runs(final_connection)
        ]
    assert final_active_names == ["run-001"], (
        "with BOTH axes cleared the run must reappear in select_active_runs "
        "— recovery composes, and the daemon's next stateless look re-selects "
        f"it; got active runs {final_active_names!r}"
    )


# --------------------------------------------------------------------------
# Unit 6 — protect / unprotect: operator-owned protection (errata A3).
# --------------------------------------------------------------------------


def test_protect_stamps_the_paired_columns(tmp_path):
    """``protect`` stamps ``protected_by`` with the acting operator and ``protected_at`` string-equal to its audit row's ``ts``, with the operator's reason riding the audit args.

    Errata A3's net-new protection pair made real — the ops are named
    ``protect``/``unprotect`` precisely so nobody reads them as the dead
    ``locked_at`` processing mutex. Three sluice decisions pinned. One: the
    pair is written TOGETHER in one call (migration 001's paired CHECK is
    satisfied by construction — both columns non-NULL after one op is the
    behavioral face of that schema decision), and ``protected_by`` names the
    protecting actor: it is what every later ``ProtectedError`` surfaces so a
    refused operator knows whom to talk to. Two: ``protected_at`` is
    STRING-EQUAL to the audit row's ``ts`` — one shared ``now_iso()`` instant
    between state stamp and evidence (§2 choreography); a second clock read
    would usually match and only occasionally differ, so string equality
    against the trail is the honest pin. Three: there is deliberately NO
    ``protected_reason`` column (plan 08 unit 6), so the operator's reason
    must ride the audit row's args — the trail is the only place the WHY of a
    protection survives, and an op that dropped the reason on the floor would
    lose it entirely.
    """
    # Local import: control is the module under construction (same rationale
    # as the unit-1 tests above).
    from sluice.core.control import protect
    from sluice.core.db import (
        get_connection,
        get_run,
        initialize_database,
        insert_run,
    )

    database_path = tmp_path / "sluice.db"
    initialize_database(database_path)
    with contextlib.closing(get_connection(database_path)) as setup_connection:
        insert_run(setup_connection, "run-001", pipeline_version="reference-v1")

    returned_result = protect(
        database_path, "will", "run-001", reason="precious flowcell"
    )

    assert returned_result.status == "ok", (
        "protect of an unprotected run is a real state change and must "
        f"report status='ok' (plan 08 §2); got {returned_result.status!r}"
    )

    # Read back on a FRESH connection: what landed is what was committed.
    with contextlib.closing(get_connection(database_path)) as verification_connection:
        protected_row = get_run(verification_connection, "run-001")
        audit_rows = verification_connection.execute(
            "SELECT * FROM control_audit"
        ).fetchall()

    assert protected_row["protected_by"] == "will", (
        "protect must stamp protected_by with the ACTING operator — it is "
        "what a later ProtectedError names so the refused operator knows "
        f"whom to talk to (errata A3); got {protected_row['protected_by']!r}"
    )
    assert len(audit_rows) == 1, (
        "one protect call must write exactly ONE control_audit row (§2 "
        f"binding choreography); got {len(audit_rows)}"
    )
    audit_row = audit_rows[0]
    assert audit_row["action"] == "protect", (
        "the audited action string is the op's public function name (D-11); "
        f"got {audit_row['action']!r}"
    )
    assert audit_row["result"] == "ok", (
        "a successful protect audits result='ok' (plan 07 §2 vocabulary); "
        f"got {audit_row['result']!r}"
    )
    assert protected_row["protected_at"] == audit_row["ts"], (
        "protected_at must be STRING-EQUAL to the audit row's ts — one "
        "now_iso() instant shared between the state stamp and its evidence "
        f"(§2 choreography: shared_now); got "
        f"protected_at={protected_row['protected_at']!r} vs "
        f"ts={audit_row['ts']!r} — a second clock read means state and "
        "evidence can cite different instants"
    )
    # There is deliberately no protected_reason column (plan 08 unit 6), so
    # the audit args are the ONLY surviving record of why the run was guarded.
    assert json.loads(audit_row["args_json"]) == {"reason": "precious flowcell"}, (
        "protect's args_json must record exactly the op's scalar parameters "
        "— here the reason, which has no run-row column to live in, so the "
        "trail is its sole record (plan 08 unit 6 / §2: args record scalars "
        f"only); got {audit_row['args_json']!r}"
    )


def test_protect_and_unprotect_are_noops_when_already_so(tmp_path):
    """Redundant ``protect``/``unprotect`` are audited noops that leave the pair untouched, ``unprotect`` clears BOTH columns, and neither op takes ``force``.

    Three sluice decisions pinned (plan 08 unit 6 / §2 A3). One: a second
    ``protect`` — even by a DIFFERENT actor — is ``status='noop'`` and the
    mutate is SKIPPED, so the original protector's ``protected_by`` and
    ``protected_at`` survive byte-identical: a mutant that re-stamps on the
    "noop" path would silently transfer protection ownership to whoever asked
    last, and every later ``ProtectedError`` would name the wrong person to
    talk to. Two: ``unprotect`` clears ``protected_at`` AND ``protected_by``
    together — migration 001's paired CHECK means a half-clear is an
    IntegrityError, so both-NULL after one op is the behavioral face of that
    schema decision; a second ``unprotect`` is then an audited noop (the
    trail records every attempt, redundant ones included, in the
    ``"noop: <why>"`` vocabulary). Three: NO ``force`` parameter exists on
    either signature — protection is operator-owned and ``unprotect`` IS the
    override (§2 A3), so a ``force`` flag here would imply a second,
    stronger authority that the design deliberately does not have.
    """
    from sluice.core.control import protect, unprotect
    from sluice.core.db import (
        get_connection,
        get_run,
        initialize_database,
        insert_run,
    )

    # The signature pin needs no database: protection is operator-owned, so
    # neither op models an override of itself — unprotect IS the override.
    for operation in (protect, unprotect):
        assert "force" not in inspect.signature(operation).parameters, (
            f"{operation.__name__} must NOT take a force parameter — "
            "protection is operator-owned and unprotect is the only "
            "override (plan 08 §2 A3); a force flag would fabricate a "
            "second, stronger authority"
        )

    database_path = tmp_path / "sluice.db"
    initialize_database(database_path)
    with contextlib.closing(get_connection(database_path)) as setup_connection:
        insert_run(setup_connection, "run-001", pipeline_version="reference-v1")

    protect(database_path, "will", "run-001", reason="precious flowcell")
    with contextlib.closing(get_connection(database_path)) as stamp_connection:
        originally_protected_row = get_run(stamp_connection, "run-001")
    original_protected_at = originally_protected_row["protected_at"]

    # --- Redundant protect, by a DIFFERENT actor: noop, stamp survives. ---
    redundant_protect_result = protect(
        database_path, "sam", "run-001", reason="also precious"
    )
    assert redundant_protect_result.status == "noop", (
        "protect of an already-protected run must report status='noop' — "
        "claiming 'ok' would fabricate a state change that never happened "
        f"(plan 08 unit 6); got {redundant_protect_result.status!r}"
    )
    with contextlib.closing(get_connection(database_path)) as noop_connection:
        row_after_redundant_protect = get_run(noop_connection, "run-001")
    assert row_after_redundant_protect["protected_by"] == "will", (
        "the noop path must SKIP the mutate: protected_by still names the "
        "ORIGINAL protector, not the redundant asker — a re-stamp would "
        "silently transfer protection ownership and every later "
        "ProtectedError would name the wrong person (errata A3); got "
        f"{row_after_redundant_protect['protected_by']!r}"
    )
    assert row_after_redundant_protect["protected_at"] == original_protected_at, (
        "protected_at must survive a redundant protect byte-identical — the "
        "pair records the ORIGINAL protection instant, not the latest ask; "
        f"got {row_after_redundant_protect['protected_at']!r} vs original "
        f"{original_protected_at!r}"
    )

    # --- unprotect: clears BOTH columns in one op (paired CHECK). ---
    unprotect_result = unprotect(database_path, "will", "run-001", reason="handled")
    assert unprotect_result.status == "ok", (
        "unprotect of a protected run is a real state change and must "
        f"report status='ok'; got {unprotect_result.status!r}"
    )
    with contextlib.closing(get_connection(database_path)) as cleared_connection:
        row_after_unprotect = get_run(cleared_connection, "run-001")
    assert (
        row_after_unprotect["protected_at"] is None
        and row_after_unprotect["protected_by"] is None
    ), (
        "unprotect must clear BOTH protected_at and protected_by together — "
        "migration 001's paired CHECK makes a half-clear impossible, and "
        "both-NULL is the behavioral face of that schema decision (plan 08 "
        f"unit 6); got protected_at="
        f"{row_after_unprotect['protected_at']!r}, protected_by="
        f"{row_after_unprotect['protected_by']!r}"
    )

    # --- Redundant unprotect: noop; both noops landed in the trail. ---
    redundant_unprotect_result = unprotect(database_path, "will", "run-001")
    assert redundant_unprotect_result.status == "noop", (
        "unprotect of a never/no-longer-protected run must report "
        "status='noop' — there is no protection to lift; got "
        f"{redundant_unprotect_result.status!r}"
    )

    with contextlib.closing(get_connection(database_path)) as trail_connection:
        audited_results = [
            row["result"]
            for row in trail_connection.execute(
                "SELECT result FROM control_audit ORDER BY id"
            ).fetchall()
        ]
    assert len(audited_results) == 4, (
        "all four attempts — protect, redundant protect, unprotect, "
        "redundant unprotect — must each write exactly one audit row (plan "
        "08 §2: the trail records every attempt, noops included); got "
        f"{len(audited_results)} rows: {audited_results!r}"
    )
    assert audited_results[1].startswith("noop: ") and audited_results[3].startswith(
        "noop: "
    ), (
        "both redundant attempts audit in the 'noop: <why>' vocabulary "
        f"(plan 07 §2 / plan 08 §2); got {audited_results!r}"
    )


# --------------------------------------------------------------------------
# Unit 7 — set_default_profile: the metadata-owned active default (plan 05 §7).
# --------------------------------------------------------------------------


def test_set_default_profile_validates_then_upserts(tmp_path):
    """``set_default_profile`` validates the version against the registry BEFORE writing — a known version upserts ``poller_metadata['pipeline_profile']`` with a global-op audit row, an unknown one propagates ``UnknownProfileError`` leaving metadata and trail untouched — and an in-flight run's stamped ``pipeline_version`` survives either way.

    Three sluice decisions pinned (plan 08 unit 7 / §2). One: the
    validate-THEN-upsert order — ``load_profile`` gates the write, so the
    active default can never name a profile the registry cannot resolve (a
    write-first mutant would leave every future detection stamping an
    unloadable version, quarantining the fleet at its first resolve). Two: the
    §2 audited/not-audited line for this op's failure mode — an unknown
    version is VALIDATION (``UnknownProfileError`` is a ``KeyError``, not a
    ``ControlError``), so it is never audited and never emitted; only the real
    upsert lands in the trail, as a global op (``target_run`` SQL NULL, no
    ``run.name`` on the wire — F1/plan 03). Three: the §8 freeze — the op
    steers what FUTURE detections stamp and touches no existing run's
    ``pipeline_version``; a "helpful" retroactive re-stamp would swap the
    contract out from under every in-flight run's recorded progress.
    """
    from sluice.core.control import PIPELINE_PROFILE_KEY, set_default_profile
    from sluice.core.db import (
        get_connection,
        get_metadata,
        get_run,
        initialize_database,
        insert_run,
        set_metadata,
    )
    from sluice.core.profile import REFERENCE_PROFILES, UnknownProfileError

    database_path = tmp_path / "sluice.db"
    initialize_database(database_path)
    with contextlib.closing(get_connection(database_path)) as setup_connection:
        # An in-flight run stamped with the current default: the §8 freeze
        # assertion below needs a run that COULD be re-stamped, to prove it
        # is not.
        insert_run(setup_connection, "run-001", pipeline_version="reference-v1")
        # Seed the active default exactly as subsystem 9's startup seed would
        # (plan 05 §7), so the op below exercises a genuine UPSERT over an
        # existing key, not just a first insert.
        set_metadata(setup_connection, PIPELINE_PROFILE_KEY, "reference-v1")

    # --- Known version: validate via the registry, then upsert. -----------
    with structlog.testing.capture_logs() as captured_log_entries:
        returned_result = set_default_profile(
            database_path,
            "will",
            "reference-v1-alpha-disabled",
            registry=REFERENCE_PROFILES,
        )

    assert returned_result.status == "ok", (
        "setting a registered version is a real state change and must report "
        f"status='ok'; got {returned_result.status!r}"
    )

    with contextlib.closing(get_connection(database_path)) as verification_connection:
        assert (
            get_metadata(verification_connection, PIPELINE_PROFILE_KEY)
            == "reference-v1-alpha-disabled"
        ), (
            "set_default_profile must upsert "
            "poller_metadata['pipeline_profile'] to the new version — the "
            "active default is DB metadata, never config (plan 05 §7 / plan "
            "08 §2), and this key is what subsystem 9 stamps future "
            "detections from"
        )
        audit_rows = verification_connection.execute(
            "SELECT * FROM control_audit"
        ).fetchall()
        run_row_after_success = get_run(verification_connection, "run-001")

    assert len(audit_rows) == 1, (
        "one successful set_default_profile must write exactly ONE "
        f"control_audit row; got {len(audit_rows)}"
    )
    audit_row = audit_rows[0]
    assert audit_row["action"] == "set_default_profile", (
        "the audited action string is the op's public function name (D-11); "
        f"got {audit_row['action']!r}"
    )
    assert audit_row["target_run"] is None, (
        "set_default_profile is a GLOBAL op and audits target_run = SQL NULL "
        "(F1 / plan 08 unit 7) — it targets what future detections stamp, "
        f"not any run; got {audit_row['target_run']!r}"
    )
    assert audit_row["result"] == "ok", (
        "the successful upsert audits result='ok' (plan 07 §2 vocabulary); "
        f"got {audit_row['result']!r}"
    )

    control_finished_entries = [
        entry for entry in captured_log_entries if entry["event"] == "control.finished"
    ]
    assert len(control_finished_entries) == 1, (
        "the successful op must emit exactly ONE control.finished (every op "
        "calls control_event exactly once after its audit row, plan 03 §7 / "
        f"plan 08 §2); captured {captured_log_entries!r}"
    )
    assert "run.name" not in control_finished_entries[0], (
        "a global op's control.finished must carry NO run.name key — a "
        "fabricated one would corrupt per-run jsonl routing (plan 03 / plan "
        f"08 §2 B5); got {control_finished_entries[0]!r}"
    )

    assert run_row_after_success["pipeline_version"] == "reference-v1", (
        "the in-flight run's stamped pipeline_version must be untouched — "
        "the §8 freeze: set_default_profile steers FUTURE stamps only, and a "
        "retroactive re-stamp would swap the contract out from under the "
        f"run's recorded progress; got "
        f"{run_row_after_success['pipeline_version']!r}"
    )

    # --- Unknown version: fail-loud validation, nothing written. ----------
    with structlog.testing.capture_logs() as captured_failure_entries:
        with pytest.raises(UnknownProfileError):
            set_default_profile(
                database_path,
                "will",
                "reference-v99-typo",
                registry=REFERENCE_PROFILES,
            )

    with contextlib.closing(get_connection(database_path)) as failure_connection:
        assert (
            get_metadata(failure_connection, PIPELINE_PROFILE_KEY)
            == "reference-v1-alpha-disabled"
        ), (
            "an unknown version must leave the active default unchanged — "
            "load_profile validates BEFORE the upsert (plan 08 §2), so a "
            "version the registry cannot resolve never becomes the default "
            "future detections stamp from"
        )
        audit_row_count_after_failure = failure_connection.execute(
            "SELECT COUNT(*) FROM control_audit"
        ).fetchone()[0]

    assert audit_row_count_after_failure == 1, (
        "the unknown-version attempt must write NO audit row — "
        "UnknownProfileError is validation (a KeyError, not a ControlError), "
        "and validation failures are silent to the trail (plan 08 §2); "
        f"expected the trail to still hold 1 row, got "
        f"{audit_row_count_after_failure}"
    )
    failure_finished_entries = [
        entry
        for entry in captured_failure_entries
        if entry["event"] == "control.finished"
    ]
    assert failure_finished_entries == [], (
        "the unknown-version attempt must emit ZERO control.finished events "
        "— nothing was committed for an event to describe (plan 03 §7 "
        f"persist-then-emit / plan 08 §2); captured {failure_finished_entries!r}"
    )


# --------------------------------------------------------------------------
# Unit 8 — set_gate: the B2 no-fabrication mechanism.
# --------------------------------------------------------------------------


def test_set_gate_refuses_to_fabricate_produces_without_force(tmp_path):
    """``set_gate`` on a gate with non-empty ``produces`` raises ``FabricationError`` naming the unfabricatable outputs, leaves the satisfaction column 0, and audits the attempt as ``refused: ...``.

    Errata B2's mechanism (plan 08 §2/unit 8): a state-only op can fabricate
    NO artifact, so marking a producing gate satisfied without its outputs
    would record a lie every downstream consumer (reconcile's ``next_gate``,
    dependents' ``needs``) then builds on. The refusal is keyed on the gate's
    ``produces`` tuple — ``mock_alpha`` produces ``mock_alpha.marker``, and
    the error must NAME that output so the operator learns exactly what they
    would be vouching for with ``force=True`` (in Plan B this same key makes
    the experiment gate's ``experiment_id`` refusal mechanical, no new code).
    The regression this catches: an implementation that writes the column
    first and checks ``produces`` after (state landed despite the raise), or
    one that skips the audit row on refusal — §5 says every control action
    including refused attempts lands in the trail; a refused fabrication is
    exactly what a trail is for. ``FabricationError`` is a ``ControlError``,
    so unlike the unknown-gate validation path this attempt IS audited.
    """
    # Local import: control is the module under construction (same rationale
    # as the unit-1 tests above).
    from sluice.core.control import FabricationError, set_gate
    from sluice.core.db import get_connection, get_run, initialize_database, insert_run
    from sluice.core.profile import REFERENCE_PROFILES

    database_path = tmp_path / "sluice.db"
    initialize_database(database_path)
    with contextlib.closing(get_connection(database_path)) as setup_connection:
        insert_run(setup_connection, "run-001", pipeline_version="reference-v1")

    with pytest.raises(FabricationError) as raised_refusal:
        set_gate(
            database_path,
            "will",
            "run-001",
            "mock_alpha",
            registry=REFERENCE_PROFILES,
        )

    assert "mock_alpha.marker" in str(raised_refusal.value), (
        "the FabricationError message must NAME the unfabricatable produces "
        "(plan 08 unit 8 / errata B2) — mock_alpha's produces is the literal "
        "'mock_alpha.marker' (plan 02 §3.3), and naming it tells the operator "
        "exactly what artifact force=True would vouch for; got "
        f"{str(raised_refusal.value)!r}"
    )

    # Read back on a FRESH connection: what landed is what was committed.
    with contextlib.closing(get_connection(database_path)) as verification_connection:
        run_row = get_run(verification_connection, "run-001")
        audit_rows = verification_connection.execute(
            "SELECT * FROM control_audit"
        ).fetchall()

    assert run_row["mock_alpha_done"] == 0, (
        "the refused set_gate must leave mock_alpha_done at 0 — a refusal "
        "commits ONLY its audit row, never the mutation (plan 08 §2 "
        "choreography: the refusal path skips the update and exits the "
        f"write_txn cleanly); got {run_row['mock_alpha_done']!r}"
    )

    assert len(audit_rows) == 1, (
        "the refused attempt must write exactly ONE control_audit row — "
        "FabricationError is a ControlError, and the ControlError family is "
        "AUDITED before it raises (plan 08 §2: §5's 'every control action' "
        f"includes refused attempts); got {len(audit_rows)} rows"
    )
    audit_row = audit_rows[0]
    assert audit_row["action"] == "set_gate", (
        "the audited action string is the op's public function name (D-11); "
        f"got {audit_row['action']!r}"
    )
    assert audit_row["result"].startswith("refused: "), (
        "the refusal audits result='refused: <why>' — the plan 07 §2 "
        "vocabulary distinguishing a policy refusal from ok/noop, so the "
        f"trail shows the attempt AND its denial; got {audit_row['result']!r}"
    )


def test_set_gate_force_sets_the_column_without_doing_the_work(tmp_path):
    """``set_gate(..., force=True)`` writes ``mock_alpha_done = 1`` while the workdir stays empty of the marker, and the audit args record the override.

    Spec §9's asymmetric mirror of the refusal above: with ``force`` the
    operator takes responsibility (e.g. artifacts restored by hand), so the
    column flips WITHOUT the gate running — no marker file appears, because a
    state-only op touches exactly one thing, the database (ADR-0005). The
    audited ``"force": true`` is the trail's evidence that satisfaction was
    vouched for, not earned; without it a later investigator could not
    distinguish a forced stamp from a daemon persist.
    """
    from sluice.core.control import set_gate
    from sluice.core.db import get_connection, get_run, initialize_database, insert_run
    from sluice.core.profile import REFERENCE_PROFILES

    database_path = tmp_path / "sluice.db"
    initialize_database(database_path)
    with contextlib.closing(get_connection(database_path)) as setup_connection:
        insert_run(setup_connection, "run-001", pipeline_version="reference-v1")
    workdir = tmp_path / "staging" / "run-001"
    workdir.mkdir(parents=True)

    returned_result = set_gate(
        database_path,
        "will",
        "run-001",
        "mock_alpha",
        registry=REFERENCE_PROFILES,
        force=True,
        reason="marker restored by hand",
    )

    assert returned_result.status == "ok", (
        "the forced set_gate must succeed — force bypasses the "
        f"FabricationError refusal (spec §9); got {returned_result.status!r}"
    )
    with contextlib.closing(get_connection(database_path)) as verification_connection:
        run_row = get_run(verification_connection, "run-001")
        audit_rows = verification_connection.execute(
            "SELECT * FROM control_audit"
        ).fetchall()

    assert run_row["mock_alpha_done"] == 1, (
        "force=True must actually write the satisfaction column — satisfied "
        f"without run (spec §9); got {run_row['mock_alpha_done']!r}"
    )
    assert not (workdir / "mock_alpha.marker").exists(), (
        "set_gate must NOT create the marker — the op never calls gate.run "
        "and never touches a file (ADR-0005): the column is vouched for, "
        "not earned"
    )
    assert len(audit_rows) == 1
    forced_audit_args = json.loads(audit_rows[0]["args_json"])
    assert forced_audit_args.get("force") is True, (
        'the audit args must carry "force": true — the trail\'s evidence of '
        f"the explicit override (plan 08 unit 8); got "
        f"{audit_rows[0]['args_json']!r}"
    )


def test_set_gate_without_force_succeeds_for_a_produces_free_gate(tmp_path):
    """``set_gate`` on a gate whose ``produces`` is empty sets the column with no ``force`` — the B2 refusal is keyed on ``produces``, never blanket.

    Plan 02 §2 B2 cited by plan 08 unit 8: the refusal exists because a
    state-only op cannot fabricate ARTIFACTS, so a gate that produces nothing
    has nothing to fabricate and sets freely. The mutant this kills is a
    blanket "set_gate always needs force" — which would train operators to
    reflexively pass force, destroying the flag's evidentiary value for the
    gates where it matters (the Plan B experiment gate's ``experiment_id``).
    A test-local registry supplies the produces-free gate; it reuses alpha's
    real satisfaction column, so the write path is the production one.
    """
    from sluice.core.control import set_gate
    from sluice.core.db import get_connection, get_run, initialize_database, insert_run
    from sluice.core.gate import Gate, GateOutcome, GateResult
    from sluice.core.profile import Applicability, build_profile

    produces_free_gate = Gate(
        id="paperwork",
        name="Produces-free reference gate",
        satisfied_column="mock_alpha_done",
        order=1,
        needs=(),
        produces=(),
        run=lambda context: GateResult(GateOutcome.SUCCESS),
        invalidate=lambda context: None,
    )
    test_profile = build_profile(
        "produces-free-v1", [(produces_free_gate, Applicability.REQUIRED)]
    )
    test_registry = {"produces-free-v1": test_profile}

    database_path = tmp_path / "sluice.db"
    initialize_database(database_path)
    with contextlib.closing(get_connection(database_path)) as setup_connection:
        insert_run(setup_connection, "run-001", pipeline_version="produces-free-v1")

    returned_result = set_gate(
        database_path, "will", "run-001", "paperwork", registry=test_registry
    )

    assert returned_result.status == "ok", (
        "a produces-free gate must set WITHOUT force — the refusal is keyed "
        "on the gate's produces tuple, not on the op (plan 02 §2 B2); got "
        f"{returned_result.status!r}"
    )
    with contextlib.closing(get_connection(database_path)) as verification_connection:
        run_row = get_run(verification_connection, "run-001")
    assert run_row["mock_alpha_done"] == 1


@pytest.mark.parametrize(
    "rejection_case",
    ["unknown-gate", "hostile-gate-id", "disabled-by-profile"],
)
def test_set_gate_rejects_unknown_hostile_and_out_of_contract_gates(
    tmp_path, rejection_case
):
    """``set_gate`` fails ``UnknownGateError`` with no audit for unknown and hostile gate ids (the hostile SQL never executing), and refuses a profile-DISABLED gate as an audited out-of-contract ``ControlError``.

    Three distinct lines pinned in one sweep. Unknown id: validation — no
    real state was identified, so no audit row (plan 08 §2). Hostile id
    (``"x'; DROP TABLE runs;--"``): the injection defense's front door —
    caller input becomes a column name only by resolving to a Gate record in
    the registry, so the hostile string dies at the lookup and ``runs``
    survives (plan 08 §4; ``update_run``'s whitelist is the backstop, never
    the first line). DISABLED-in-profile: a REAL gate the run's contract
    excludes — that is policy, not a typo, so the refusal IS audited
    (``ControlError``) and the column stays 0: satisfying an out-of-contract
    gate would record progress the contract cannot even see.
    """
    from sluice.core.control import ControlError, UnknownGateError, set_gate
    from sluice.core.db import get_connection, get_run, initialize_database, insert_run
    from sluice.core.profile import REFERENCE_PROFILES

    database_path = tmp_path / "sluice.db"
    initialize_database(database_path)
    pipeline_version = (
        "reference-v1-alpha-disabled"
        if rejection_case == "disabled-by-profile"
        else "reference-v1"
    )
    with contextlib.closing(get_connection(database_path)) as setup_connection:
        insert_run(setup_connection, "run-001", pipeline_version=pipeline_version)

    if rejection_case == "disabled-by-profile":
        with pytest.raises(ControlError, match="out of contract"):
            set_gate(
                database_path,
                "will",
                "run-001",
                "mock_alpha",
                registry=REFERENCE_PROFILES,
            )
        with contextlib.closing(
            get_connection(database_path)
        ) as verification_connection:
            run_row = get_run(verification_connection, "run-001")
            audit_rows = verification_connection.execute(
                "SELECT * FROM control_audit"
            ).fetchall()
        assert run_row["mock_alpha_done"] == 0, (
            "the out-of-contract refusal must leave the column at 0 — a "
            "DISABLED gate's satisfaction would be progress the contract "
            f"cannot see; got {run_row['mock_alpha_done']!r}"
        )
        assert len(audit_rows) == 1 and audit_rows[0]["result"].startswith(
            "refused: "
        ), (
            "the out-of-contract case is a POLICY refusal of a real gate — "
            "audited 'refused: <why>' (plan 08 unit 8), unlike the "
            f"unknown-gate validation path; got {audit_rows!r}"
        )
    else:
        hostile_or_unknown_gate_id = (
            "x'; DROP TABLE runs;--"
            if rejection_case == "hostile-gate-id"
            else "no_such_gate"
        )
        with pytest.raises(UnknownGateError):
            set_gate(
                database_path,
                "will",
                "run-001",
                hostile_or_unknown_gate_id,
                registry=REFERENCE_PROFILES,
            )
        with contextlib.closing(
            get_connection(database_path)
        ) as verification_connection:
            surviving_tables = {
                row["name"]
                for row in verification_connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            audit_row_count = verification_connection.execute(
                "SELECT COUNT(*) FROM control_audit"
            ).fetchone()[0]
        assert "runs" in surviving_tables, (
            "the hostile gate id must never reach SQL — it dies at the "
            "registry lookup (plan 08 §4 injection defense), so runs "
            "survives; the table is GONE"
        )
        assert audit_row_count == 0, (
            "an unknown/hostile gate id is validation — no real state was "
            "identified, so the trail stays untouched (plan 08 §2); got "
            f"{audit_row_count} rows"
        )


def test_set_gate_on_a_satisfied_gate_is_a_noop(tmp_path):
    """``set_gate`` on an already-satisfied gate is an audited noop that leaves the column untouched — even without ``force`` on a producing gate.

    The decision-order pin (plan 08 unit 8: out-of-contract → satisfied →
    fabrication): an already-satisfied producing gate must short-circuit at
    the noop, NOT raise ``FabricationError`` — there is nothing to fabricate,
    the work is already recorded — and the redundant attempt still lands in
    the trail in the ``"noop: <why>"`` vocabulary.
    """
    from sluice.core.control import set_gate
    from sluice.core.db import (
        get_connection,
        get_run,
        initialize_database,
        insert_run,
        update_run,
    )
    from sluice.core.profile import REFERENCE_PROFILES

    database_path = tmp_path / "sluice.db"
    initialize_database(database_path)
    with contextlib.closing(get_connection(database_path)) as setup_connection:
        insert_run(setup_connection, "run-001", pipeline_version="reference-v1")
        update_run(setup_connection, "run-001", {"mock_alpha_done": 1})

    returned_result = set_gate(
        database_path, "will", "run-001", "mock_alpha", registry=REFERENCE_PROFILES
    )

    assert returned_result.status == "noop", (
        "an already-satisfied gate must be a noop — NOT a FabricationError: "
        "the satisfied check precedes the produces check (plan 08 unit 8 "
        f"decision order); got {returned_result.status!r}"
    )
    with contextlib.closing(get_connection(database_path)) as verification_connection:
        run_row = get_run(verification_connection, "run-001")
        audit_rows = verification_connection.execute(
            "SELECT * FROM control_audit"
        ).fetchall()
    assert run_row["mock_alpha_done"] == 1
    assert len(audit_rows) == 1 and audit_rows[0]["result"].startswith("noop: ")


# --------------------------------------------------------------------------
# Unit 9 — migration 006: the excluded_gates overlay column (⚑2).
# --------------------------------------------------------------------------


def test_migration_006_adds_a_nullable_json_checked_excluded_gates(
    tmp_path, monkeypatch
):
    """Migration 006 on a POPULATED version-5 database adds ``excluded_gates`` with pre-existing rows NULL, a CHECK rejecting non-JSON, and JSON arrays accepted.

    The ladder-growth pattern of plan 02's F3-revision, now for control's
    column (⚑2): rows inserted before 006 land NULL — correct, nothing was
    ever excluded from them, and NULL (never ``'[]'``) is the no-exclusions
    spelling (no-fabrication). The CHECK is the schema's only shape guard —
    ``json_valid`` — so raw garbage dies at the INSERT boundary while the
    array-of-strings shape is ``run_exclusions``' job (a JSON-valid non-array
    passes here by design; unit 11 pins the loud parse).
    """
    import sluice.core.db

    database_path = tmp_path / "sluice.db"
    full_migration_ladder = sluice.core.db.MIGRATIONS

    # A populated version-5 database: the first five migrations, one row.
    monkeypatch.setattr(sluice.core.db, "MIGRATIONS", full_migration_ladder[:5])
    sluice.core.db.initialize_database(database_path)
    with contextlib.closing(
        sluice.core.db.get_connection(database_path)
    ) as setup_connection:
        sluice.core.db.insert_run(
            setup_connection, "preexisting", pipeline_version="reference-v1"
        )

    # The ladder grows to 006; re-initialization applies only the pending one.
    monkeypatch.setattr(sluice.core.db, "MIGRATIONS", full_migration_ladder)
    applied_versions = sluice.core.db.initialize_database(database_path)
    assert applied_versions == [6], (
        "re-initializing a version-5 database must apply exactly migration 6 "
        f"(the ladder ends at 006 — plan 08 unit 9); applied {applied_versions!r}"
    )

    with contextlib.closing(
        sluice.core.db.get_connection(database_path)
    ) as verification_connection:
        preexisting_row = sluice.core.db.get_run(verification_connection, "preexisting")
        assert preexisting_row["excluded_gates"] is None, (
            "pre-existing rows must land NULL — no exclusions were ever "
            "recorded for them, and NULL (not '[]') is the no-exclusions "
            f"spelling (⚑2 no-fabrication); got "
            f"{preexisting_row['excluded_gates']!r}"
        )

        with pytest.raises(sqlite3.IntegrityError):
            verification_connection.execute(
                "UPDATE runs SET excluded_gates = 'not json' WHERE name = 'preexisting'"
            )

        verification_connection.execute(
            "UPDATE runs SET excluded_gates = '[\"mock_beta\"]' "
            "WHERE name = 'preexisting'"
        )
        assert (
            sluice.core.db.get_run(verification_connection, "preexisting")[
                "excluded_gates"
            ]
            == '["mock_beta"]'
        )


# --------------------------------------------------------------------------
# Unit 11 — exclude_gate / include_gate / run_exclusions (⚑2).
# --------------------------------------------------------------------------


def test_exclude_gate_resumes_a_stuck_run_the_adr_0004_miniature(tmp_path):
    """``exclude_gate`` writes exactly ``'["mock_beta"]'``, and the composed honoring loop — ``apply_exclusions(load_profile(...), run_exclusions(row))`` into reconcile — reports the stuck run resumed and complete, with zero daemon or reconcile changes.

    THE load-bearing ⚑2 test (ADR-0004 d.3 in miniature): remediation is
    exclusion, not skipping. A run stuck at beta (alpha done, beta 0) has
    beta removed from its contract; because reconcile is stateless and
    consumes whatever Profile it is handed, ``next_gate`` over the DERIVED
    contract returns ``None`` and ``is_complete`` is True on the very next
    look — statelessness doing the work. Composing the derivation IN THIS
    TEST proves the reader the op's write depends on exists (the
    no-writer-without-reader constraint closed within Plan A); the daemon
    (subsystem 9) is bound to this exact composition by plan 08 §6.
    """
    from sluice.core.control import exclude_gate, run_exclusions
    from sluice.core.db import get_connection, get_run, initialize_database, insert_run
    from sluice.core.db import update_run as db_update_run
    from sluice.core.profile import REFERENCE_PROFILES, apply_exclusions, load_profile
    from sluice.core.reconcile import is_complete, next_gate

    database_path = tmp_path / "sluice.db"
    initialize_database(database_path)
    with contextlib.closing(get_connection(database_path)) as setup_connection:
        insert_run(setup_connection, "run-001", pipeline_version="reference-v1")
        db_update_run(setup_connection, "run-001", {"mock_alpha_done": 1})
        # Precondition: the run is genuinely stuck at beta under its base
        # contract, so the resumption below is meaningful.
        stuck_row = get_run(setup_connection, "run-001")
        base_profile = load_profile(stuck_row["pipeline_version"], REFERENCE_PROFILES)
        assert next_gate(stuck_row, base_profile).id == "mock_beta"
        assert not is_complete(stuck_row, base_profile)

    returned_result = exclude_gate(
        database_path,
        "will",
        "run-001",
        "mock_beta",
        registry=REFERENCE_PROFILES,
        reason="beta service retired for this run",
    )
    assert returned_result.status == "ok"

    with contextlib.closing(get_connection(database_path)) as verification_connection:
        run_row = get_run(verification_connection, "run-001")

    assert run_row["excluded_gates"] == '["mock_beta"]', (
        "the column must store exactly '[\"mock_beta\"]' — a sorted JSON "
        "array of gate ids, the pinned overlay format the daemon parses "
        f"(⚑2); got {run_row['excluded_gates']!r}"
    )

    # The full honoring loop, composed verbatim as plan 08 §6 binds the
    # daemon to it: write -> derive -> reconcile.
    derived_contract = apply_exclusions(
        load_profile(run_row["pipeline_version"], REFERENCE_PROFILES),
        run_exclusions(run_row),
    )
    assert next_gate(run_row, derived_contract) is None, (
        "reconcile over the DERIVED contract must select nothing — the run "
        "resumed past the excluded step with zero reconcile changes "
        "(statelessness doing the work, ADR-0004 d.3)"
    )
    assert is_complete(run_row, derived_contract), (
        "the derived contract must be complete — alpha (the only remaining "
        "contract gate) is satisfied; the excluded beta is satisfied by "
        "absence (ADR-0004 d.2)"
    )


def test_exclude_gate_is_idempotent_sorted_and_deduplicated(tmp_path):
    """A repeated ``exclude_gate`` is an audited noop leaving the column byte-identical, and excluding beta then alpha stores the SORTED ``'["mock_alpha", "mock_beta"]'``.

    Deterministic bytes (plan 08 unit 11, the audit-plan ethic): the same
    SET of exclusions must serialize to the same string regardless of the
    order operators applied them, so diffing two rows — or two audit
    entries — never shows a phantom change. The noop half re-pins the §2
    decision step on this op: a "helpful" rewrite of the same array would
    turn every redundant exclude into a phantom ok.
    """
    from sluice.core.control import exclude_gate
    from sluice.core.db import get_connection, get_run, initialize_database, insert_run
    from sluice.core.profile import REFERENCE_PROFILES

    database_path = tmp_path / "sluice.db"
    initialize_database(database_path)
    with contextlib.closing(get_connection(database_path)) as setup_connection:
        insert_run(setup_connection, "run-001", pipeline_version="reference-v1")

    exclude_gate(
        database_path, "will", "run-001", "mock_beta", registry=REFERENCE_PROFILES
    )
    second_exclude_result = exclude_gate(
        database_path, "will", "run-001", "mock_beta", registry=REFERENCE_PROFILES
    )
    assert second_exclude_result.status == "noop", (
        "excluding an already-excluded gate must be an audited noop; got "
        f"{second_exclude_result.status!r}"
    )
    with contextlib.closing(get_connection(database_path)) as midpoint_connection:
        assert (
            get_run(midpoint_connection, "run-001")["excluded_gates"] == '["mock_beta"]'
        ), "the noop must leave the stored array byte-identical"
        noop_audit_results = [
            row["result"]
            for row in midpoint_connection.execute(
                "SELECT result FROM control_audit ORDER BY id"
            )
        ]
    assert len(noop_audit_results) == 2 and noop_audit_results[1].startswith("noop: ")

    exclude_gate(
        database_path, "will", "run-001", "mock_alpha", registry=REFERENCE_PROFILES
    )
    with contextlib.closing(get_connection(database_path)) as verification_connection:
        stored_exclusions = get_run(verification_connection, "run-001")[
            "excluded_gates"
        ]
    assert stored_exclusions == '["mock_alpha", "mock_beta"]', (
        "beta-then-alpha must store the SORTED array — deterministic bytes "
        "for the same exclusion set, independent of application order "
        f"(plan 08 unit 11); got {stored_exclusions!r}"
    )


@pytest.mark.parametrize(
    "guard_case",
    ["unknown-gate", "protected-without-force", "already-disabled-by-profile"],
)
def test_exclude_gate_guards(tmp_path, guard_case):
    """``exclude_gate`` fails ``UnknownGateError`` unaudited for a gate the profile lacks, refuses a protected run with ``ProtectedError`` unless ``force``, and refuses a profile-DISABLED gate as an audited ``ControlError``.

    The same three-way line as ``set_gate``'s, on the contract-editing op: a
    typo identifies nothing (silent validation); protection guards a
    destructive contract edit (A3 — audited refusal, force bypasses without
    unprotecting); and excluding a gate the profile already disables would
    record an exclusion with no effect — refused loudly ("already out of
    contract by profile") rather than stored as dead weight that a later
    ``include_gate`` would appear to honor.
    """
    from sluice.core.control import (
        ControlError,
        ProtectedError,
        UnknownGateError,
        exclude_gate,
    )
    from sluice.core.db import (
        get_connection,
        get_run,
        initialize_database,
        insert_run,
        update_run,
    )
    from sluice.core.profile import REFERENCE_PROFILES

    database_path = tmp_path / "sluice.db"
    initialize_database(database_path)
    pipeline_version = (
        "reference-v1-alpha-disabled"
        if guard_case == "already-disabled-by-profile"
        else "reference-v1"
    )
    with contextlib.closing(get_connection(database_path)) as setup_connection:
        insert_run(setup_connection, "run-001", pipeline_version=pipeline_version)
        if guard_case == "protected-without-force":
            update_run(
                setup_connection,
                "run-001",
                {
                    "protected_at": "2026-07-28T00:00:00+00:00",
                    "protected_by": "alice",
                },
            )

    if guard_case == "unknown-gate":
        with pytest.raises(UnknownGateError):
            exclude_gate(
                database_path,
                "will",
                "run-001",
                "no_such_gate",
                registry=REFERENCE_PROFILES,
            )
        with contextlib.closing(get_connection(database_path)) as check_connection:
            audit_row_count = check_connection.execute(
                "SELECT COUNT(*) FROM control_audit"
            ).fetchone()[0]
        assert audit_row_count == 0, (
            "an unknown gate is validation — never audited (plan 08 §2)"
        )
    elif guard_case == "protected-without-force":
        with pytest.raises(ProtectedError, match="alice"):
            exclude_gate(
                database_path,
                "will",
                "run-001",
                "mock_beta",
                registry=REFERENCE_PROFILES,
            )
        with contextlib.closing(get_connection(database_path)) as check_connection:
            refused_row = get_run(check_connection, "run-001")
            refusal_audit_rows = check_connection.execute(
                "SELECT * FROM control_audit ORDER BY id"
            ).fetchall()
        assert refused_row["excluded_gates"] is None, (
            "the refused exclude must write no exclusion"
        )
        assert len(refusal_audit_rows) == 1 and refusal_audit_rows[0][
            "result"
        ].startswith("refused: ")

        forced_result = exclude_gate(
            database_path,
            "will",
            "run-001",
            "mock_beta",
            registry=REFERENCE_PROFILES,
            force=True,
        )
        assert forced_result.status == "ok"
        with contextlib.closing(get_connection(database_path)) as forced_connection:
            forced_row = get_run(forced_connection, "run-001")
        assert forced_row["excluded_gates"] == '["mock_beta"]'
        assert forced_row["protected_by"] == "alice", (
            "force bypasses one refusal and never unprotects (errata A3)"
        )
    else:  # already-disabled-by-profile
        with pytest.raises(ControlError, match="already out of contract"):
            exclude_gate(
                database_path,
                "will",
                "run-001",
                "mock_alpha",
                registry=REFERENCE_PROFILES,
            )
        with contextlib.closing(get_connection(database_path)) as check_connection:
            refused_row = get_run(check_connection, "run-001")
            audit_rows = check_connection.execute(
                "SELECT * FROM control_audit"
            ).fetchall()
        assert refused_row["excluded_gates"] is None
        assert len(audit_rows) == 1 and audit_rows[0]["result"].startswith(
            "refused: "
        ), "the profile-DISABLED case is a policy refusal — audited"


def test_include_gate_restores_and_empty_becomes_null(tmp_path):
    """``include_gate`` of the excluded gate makes reconcile select it again, the LAST removal stores SQL NULL (never ``'[]'``), and a never-excluded gate is an audited noop.

    The mirror op's three pins (plan 08 unit 11): restoration is proven the
    same way exclusion was — through the recomposed contract, where
    ``next_gate`` selects beta again; the empty case stores NULL because
    ``'[]'`` would fabricate a "someone excluded then included things" shape
    for the common nothing-ever-excluded state (the same no-fabrication
    ethic as the migration's NULL default); and including what was never
    excluded is a noop, not an error — the desired state already holds.
    """
    from sluice.core.control import exclude_gate, include_gate, run_exclusions
    from sluice.core.db import (
        get_connection,
        get_run,
        initialize_database,
        insert_run,
        update_run,
    )
    from sluice.core.profile import REFERENCE_PROFILES, apply_exclusions, load_profile
    from sluice.core.reconcile import next_gate

    database_path = tmp_path / "sluice.db"
    initialize_database(database_path)
    with contextlib.closing(get_connection(database_path)) as setup_connection:
        insert_run(setup_connection, "run-001", pipeline_version="reference-v1")
        update_run(setup_connection, "run-001", {"mock_alpha_done": 1})
    exclude_gate(
        database_path, "will", "run-001", "mock_beta", registry=REFERENCE_PROFILES
    )

    returned_result = include_gate(
        database_path, "will", "run-001", "mock_beta", registry=REFERENCE_PROFILES
    )
    assert returned_result.status == "ok"

    with contextlib.closing(get_connection(database_path)) as verification_connection:
        restored_row = get_run(verification_connection, "run-001")

    assert restored_row["excluded_gates"] is None, (
        "removing the LAST exclusion must store SQL NULL, never '[]' — the "
        "no-exclusions spelling is NULL (⚑2 no-fabrication); got "
        f"{restored_row['excluded_gates']!r}"
    )
    recomposed_contract = apply_exclusions(
        load_profile(restored_row["pipeline_version"], REFERENCE_PROFILES),
        run_exclusions(restored_row),
    )
    assert next_gate(restored_row, recomposed_contract).id == "mock_beta", (
        "after include, reconcile over the recomposed contract must select "
        "beta again — the restoration is real, not just a column edit"
    )

    never_excluded_result = include_gate(
        database_path, "will", "run-001", "mock_alpha", registry=REFERENCE_PROFILES
    )
    assert never_excluded_result.status == "noop", (
        "including a never-excluded gate must be an audited noop; got "
        f"{never_excluded_result.status!r}"
    )


def test_include_gate_clears_a_now_stale_completed_at(tmp_path):
    """``include_gate`` on a run completed UNDER an exclusion clears ``completed_at`` in the same transaction, while a still-true stamp survives an include of an already-satisfied gate.

    F2 (plan 08 §2, review-confirmed): a run stamped complete while beta was
    excluded has ``completed_at`` become a LIE the moment beta re-enters the
    contract unsatisfied — exactly the stale cache the reset family clears,
    so the op clears it atomically with the overlay rewrite (a daemon
    reading between two separate transactions would see a "complete" run
    with an incomplete contract). The control case guards the mirror mutant:
    an include that blindly NULLs ``completed_at`` would erase a stamp that
    is still TRUE when the re-included gate's column is already 1.
    """
    from sluice.core.control import exclude_gate, include_gate, run_exclusions
    from sluice.core.db import (
        get_connection,
        get_run,
        initialize_database,
        insert_run,
        update_run,
    )
    from sluice.core.profile import REFERENCE_PROFILES, apply_exclusions, load_profile
    from sluice.core.reconcile import next_gate

    database_path = tmp_path / "sluice.db"
    initialize_database(database_path)
    completion_stamp = "2026-07-28T12:00:00+00:00"
    with contextlib.closing(get_connection(database_path)) as setup_connection:
        # Run A: completed under a beta exclusion — beta's column is 0, yet
        # completed_at was legitimately stamped over the derived contract.
        insert_run(setup_connection, "run-stale", pipeline_version="reference-v1")
        update_run(setup_connection, "run-stale", {"mock_alpha_done": 1})
        # Run B: everything genuinely done — the still-true control case.
        insert_run(setup_connection, "run-true", pipeline_version="reference-v1")
        update_run(
            setup_connection,
            "run-true",
            {"mock_alpha_done": 1, "mock_beta_done": 1},
        )
    for run_name in ("run-stale", "run-true"):
        exclude_gate(
            database_path, "will", run_name, "mock_beta", registry=REFERENCE_PROFILES
        )
    with contextlib.closing(get_connection(database_path)) as stamp_connection:
        for run_name in ("run-stale", "run-true"):
            update_run(stamp_connection, run_name, {"completed_at": completion_stamp})

    include_gate(
        database_path, "will", "run-stale", "mock_beta", registry=REFERENCE_PROFILES
    )
    include_gate(
        database_path, "will", "run-true", "mock_beta", registry=REFERENCE_PROFILES
    )

    with contextlib.closing(get_connection(database_path)) as verification_connection:
        stale_row = get_run(verification_connection, "run-stale")
        true_row = get_run(verification_connection, "run-true")

    assert stale_row["completed_at"] is None, (
        "re-adding an unsatisfied step to a completed run's contract makes "
        "the stamp a lie — include_gate must clear completed_at in the SAME "
        f"transaction (F2, plan 08 §2); got {stale_row['completed_at']!r}"
    )
    recomposed_contract = apply_exclusions(
        load_profile(stale_row["pipeline_version"], REFERENCE_PROFILES),
        run_exclusions(stale_row),
    )
    assert next_gate(stale_row, recomposed_contract).id == "mock_beta", (
        "reconcile must select the re-included gate — the run resumes"
    )

    assert true_row["completed_at"] == completion_stamp, (
        "including a gate whose column is already 1 leaves the contract "
        "complete — the still-true completed_at must survive byte-identical "
        f"(F2 control case); got {true_row['completed_at']!r}"
    )


def test_run_exclusions_parses_and_fails_loud_on_shape_drift(tmp_path):
    """``run_exclusions`` maps NULL to ``()``, a JSON array to its tuple, and raises ``ValueError`` — never a silent ``()`` — on a JSON-valid non-array.

    Guardrail §8's sharpest instance (plan 08 unit 11): the migration CHECK
    only guarantees VALID JSON, so a planted ``'{"a": 1}'`` reaches the
    parser — and a silent-fallback parse returning ``()`` would silently
    UNEXCLUDE every gate, resurrecting contract steps an operator removed.
    The loud raise is caught per-run in exactly one place, ``get_status``
    (F5); every other consumer must propagate it.
    """
    from sluice.core.control import run_exclusions
    from sluice.core.db import get_connection, get_run, initialize_database, insert_run

    database_path = tmp_path / "sluice.db"
    initialize_database(database_path)
    with contextlib.closing(get_connection(database_path)) as connection:
        insert_run(connection, "run-001", pipeline_version="reference-v1")

        assert run_exclusions(get_run(connection, "run-001")) == (), (
            "NULL means no exclusions — the empty tuple"
        )

        connection.execute(
            "UPDATE runs SET excluded_gates = '[\"a\", \"b\"]' WHERE name = 'run-001'"
        )
        assert run_exclusions(get_run(connection, "run-001")) == ("a", "b")

        # A JSON-valid NON-ARRAY passes the migration CHECK; only the parser
        # can catch the shape drift — planted by raw SQL exactly as a bug or
        # a hand edit would.
        connection.execute(
            "UPDATE runs SET excluded_gates = '{\"a\": 1}' WHERE name = 'run-001'"
        )
        with pytest.raises(ValueError, match="array"):
            run_exclusions(get_run(connection, "run-001"))


# --------------------------------------------------------------------------
# Unit 12 — reset_gate: column-clear only (⚑1 / ADR-0005).
# --------------------------------------------------------------------------


def test_reset_gate_refuses_on_a_daemon_eligible_run(tmp_path):
    """``reset_gate`` on an active, non-quarantined run raises ``ActiveRunError``, audits the refusal, and leaves the column untouched.

    The §2 residual posture made executable (⚑3 as superseded by ADR-0005):
    the rationale is NOT file safety — nothing deletes files — but
    predictability: the daemon may be mid-gate on an eligible run, and its
    persist would immediately re-stamp the very column this op just cleared,
    a surprising no-op. Refusing keeps the rewind's effect predictable: the
    op works only on runs the daemon cannot be driving (held, skipped, or
    quarantined). The predicate is exactly ``ACTIVE_RUNS_WHERE``.
    """
    from sluice.core.control import ActiveRunError, reset_gate
    from sluice.core.db import (
        get_connection,
        get_run,
        initialize_database,
        insert_run,
        update_run,
    )
    from sluice.core.profile import REFERENCE_PROFILES

    database_path = tmp_path / "sluice.db"
    initialize_database(database_path)
    with contextlib.closing(get_connection(database_path)) as setup_connection:
        insert_run(setup_connection, "run-001", pipeline_version="reference-v1")
        update_run(setup_connection, "run-001", {"mock_alpha_done": 1})

    with pytest.raises(ActiveRunError):
        reset_gate(
            database_path,
            "will",
            "run-001",
            "mock_alpha",
            registry=REFERENCE_PROFILES,
        )

    with contextlib.closing(get_connection(database_path)) as verification_connection:
        run_row = get_run(verification_connection, "run-001")
        audit_rows = verification_connection.execute(
            "SELECT * FROM control_audit"
        ).fetchall()
    assert run_row["mock_alpha_done"] == 1, (
        "the refused rewind must leave the satisfaction column untouched; "
        f"got {run_row['mock_alpha_done']!r}"
    )
    assert len(audit_rows) == 1 and audit_rows[0]["result"].startswith("refused: "), (
        "ActiveRunError is a ControlError — the refusal is audited "
        f"'refused: <why>' (plan 08 §2); got {audit_rows!r}"
    )


def test_reset_gate_clears_column_and_stale_completed_at_leaving_artifacts(tmp_path):
    """``reset_gate`` on a held run clears the column and ``completed_at`` in one audited write while the on-disk marker SURVIVES, and reconcile re-selects the gate — rewind with no pointer and no invalidate.

    ADR-0005 d.2 behaviorally: rewind is column-clear only. The marker
    surviving is THE pin — under the superseded design this op called
    ``invalidate`` and the file would be gone; deletion is now the
    operator's, manually, and the daemon's re-run tolerates the leftover
    (§4 re-entrancy, exercised for real in unit 16). Re-selection via
    ``next_gate`` proves the rewind mechanism whole: no pointer column, just
    a cleared column that the stateless reconcile notices.
    """
    from sluice.core.control import hold, reset_gate
    from sluice.core.db import (
        get_connection,
        get_run,
        initialize_database,
        insert_run,
        update_run,
    )
    from sluice.core.profile import REFERENCE_PROFILE, REFERENCE_PROFILES
    from sluice.core.reconcile import next_gate

    database_path = tmp_path / "sluice.db"
    initialize_database(database_path)
    with contextlib.closing(get_connection(database_path)) as setup_connection:
        insert_run(setup_connection, "run-001", pipeline_version="reference-v1")
        update_run(
            setup_connection,
            "run-001",
            {"mock_alpha_done": 1, "completed_at": "2026-07-28T12:00:00+00:00"},
        )
    hold(database_path, "will", "run-001", reason="rewinding alpha")

    workdir = tmp_path / "staging" / "run-001"
    workdir.mkdir(parents=True)
    marker_path = workdir / "mock_alpha.marker"
    marker_path.write_text("", encoding="utf-8")

    returned_result = reset_gate(
        database_path, "will", "run-001", "mock_alpha", registry=REFERENCE_PROFILES
    )
    assert returned_result.status == "ok"

    with contextlib.closing(get_connection(database_path)) as verification_connection:
        rewound_row = get_run(verification_connection, "run-001")
        audit_actions = [
            row["action"]
            for row in verification_connection.execute(
                "SELECT action FROM control_audit ORDER BY id"
            )
        ]

    assert rewound_row["mock_alpha_done"] == 0, (
        "reset_gate must clear the satisfaction column; got "
        f"{rewound_row['mock_alpha_done']!r}"
    )
    assert rewound_row["completed_at"] is None, (
        "reset_gate must clear the now-stale completed_at in the same "
        "transaction — a cleared gate makes the completion stamp a lie "
        f"(plan 08 unit 12); got {rewound_row['completed_at']!r}"
    )
    assert marker_path.exists(), (
        "THE ADR-0005 pin: the artifact must still be on disk — rewind is "
        "column-clear only, no invalidate, no file I/O; deletion is the "
        "operator's, manually (ADR-0005 d.1/d.2)"
    )
    assert audit_actions == ["hold", "reset_gate"], (
        "state cleared and audit recorded together — the rewind is one "
        f"audited op (D-11 action names); got {audit_actions!r}"
    )
    assert next_gate(rewound_row, REFERENCE_PROFILE).id == "mock_alpha", (
        "reconcile must re-select the cleared gate on the next look — the "
        "rewind is proven by re-selection: no pointer, no invalidate (⚑1)"
    )


def test_reset_gate_on_an_unsatisfied_gate_is_an_audited_noop(tmp_path):
    """``reset_gate`` on a gate whose column is already 0 reports ``noop``, writes nothing, and still audits.

    The ⚑1 amendment in code: with no ``invalidate`` there is no
    partial-state discard to perform on an unsatisfied gate — §4 re-entrancy
    at the next ``run`` is the whole story for whatever half-finished
    artifacts exist, so the op has literally nothing to write. The run row
    must come back byte-identical (a mutant that "re-clears" would stamp a
    phantom completed_at NULL over... nothing, but would still report a
    fabricated 'ok').
    """
    from sluice.core.control import hold, reset_gate
    from sluice.core.db import get_connection, get_run, initialize_database, insert_run
    from sluice.core.profile import REFERENCE_PROFILES

    database_path = tmp_path / "sluice.db"
    initialize_database(database_path)
    with contextlib.closing(get_connection(database_path)) as setup_connection:
        insert_run(setup_connection, "run-001", pipeline_version="reference-v1")
    hold(database_path, "will", "run-001", reason="quiescing first")
    with contextlib.closing(get_connection(database_path)) as snapshot_connection:
        row_before = tuple(get_run(snapshot_connection, "run-001"))

    returned_result = reset_gate(
        database_path, "will", "run-001", "mock_alpha", registry=REFERENCE_PROFILES
    )

    assert returned_result.status == "noop", (
        "an already-unsatisfied gate has nothing to rewind — status='noop' "
        f"(plan 08 unit 12); got {returned_result.status!r}"
    )
    assert dict(returned_result.changes) == {}
    with contextlib.closing(get_connection(database_path)) as verification_connection:
        row_after = tuple(get_run(verification_connection, "run-001"))
        noop_audit_rows = verification_connection.execute(
            "SELECT * FROM control_audit WHERE action = 'reset_gate'"
        ).fetchall()
    assert row_after == row_before, "the noop must write nothing to the run row"
    assert len(noop_audit_rows) == 1 and noop_audit_rows[0]["result"].startswith(
        "noop: "
    )


def test_reset_gate_guards(tmp_path):
    """``reset_gate`` on a protected (held) run raises ``ProtectedError`` unless ``force``, and an unknown gate fails ``UnknownGateError`` with no audit row.

    The A3 + validation guards on the first rewind op: protection outranks
    the rewind because clearing recorded progress is exactly what
    ``protect`` exists to stop (the refusal is audited — a real blocked
    action); ``force=True`` bypasses it and ``protected_*`` survive. The
    unknown gate dies at the registry lookup — validation, silent to the
    trail.
    """
    from sluice.core.control import ProtectedError, UnknownGateError, hold, reset_gate
    from sluice.core.db import (
        get_connection,
        get_run,
        initialize_database,
        insert_run,
        update_run,
    )
    from sluice.core.profile import REFERENCE_PROFILES

    database_path = tmp_path / "sluice.db"
    initialize_database(database_path)
    with contextlib.closing(get_connection(database_path)) as setup_connection:
        insert_run(setup_connection, "run-001", pipeline_version="reference-v1")
        update_run(
            setup_connection,
            "run-001",
            {
                "mock_alpha_done": 1,
                "protected_at": "2026-07-28T00:00:00+00:00",
                "protected_by": "alice",
            },
        )
    hold(database_path, "will", "run-001", reason="quiescing first")

    with pytest.raises(ProtectedError, match="alice"):
        reset_gate(
            database_path,
            "will",
            "run-001",
            "mock_alpha",
            registry=REFERENCE_PROFILES,
        )
    with contextlib.closing(get_connection(database_path)) as refusal_connection:
        assert get_run(refusal_connection, "run-001")["mock_alpha_done"] == 1
        refusal_results = [
            row["result"]
            for row in refusal_connection.execute(
                "SELECT result FROM control_audit WHERE action = 'reset_gate'"
            )
        ]
    assert refusal_results == [
        "refused: run is protected by alice — unprotect it or pass force=True"
    ]

    forced_result = reset_gate(
        database_path,
        "will",
        "run-001",
        "mock_alpha",
        registry=REFERENCE_PROFILES,
        force=True,
    )
    assert forced_result.status == "ok"
    with contextlib.closing(get_connection(database_path)) as forced_connection:
        forced_row = get_run(forced_connection, "run-001")
        audit_count_before_unknown = forced_connection.execute(
            "SELECT COUNT(*) FROM control_audit"
        ).fetchone()[0]
    assert forced_row["mock_alpha_done"] == 0
    assert forced_row["protected_by"] == "alice", (
        "force bypasses one refusal and never unprotects (errata A3)"
    )

    with pytest.raises(UnknownGateError):
        reset_gate(
            database_path,
            "will",
            "run-001",
            "no_such_gate",
            registry=REFERENCE_PROFILES,
        )
    with contextlib.closing(get_connection(database_path)) as unknown_connection:
        audit_count_after_unknown = unknown_connection.execute(
            "SELECT COUNT(*) FROM control_audit"
        ).fetchone()[0]
    assert audit_count_after_unknown == audit_count_before_unknown, (
        "an unknown gate is validation — never audited"
    )


# --------------------------------------------------------------------------
# Unit 13 — backtrack: the pointerless column-clear cascade (⚑1).
# --------------------------------------------------------------------------


def test_backtrack_rewinds_target_and_everything_after(tmp_path):
    """``backtrack`` to alpha on a fully-satisfied held run clears BOTH columns and ``completed_at`` in one audited write, both artifact files survive, and reconcile re-selects alpha.

    The cascade half of ⚑1: rewinding to a gate must also clear every LATER
    gate — beta consumed alpha's output, so alpha-only clearing would leave
    beta's satisfaction describing work built on a stamp the operator just
    disowned. One transaction, one ``update_run``; the artifacts surviving
    is ADR-0005 total over the cascade (the file the superseded design
    deleted per gate).
    """
    from sluice.core.control import backtrack, hold
    from sluice.core.db import (
        get_connection,
        get_run,
        initialize_database,
        insert_run,
        update_run,
    )
    from sluice.core.profile import REFERENCE_PROFILE, REFERENCE_PROFILES
    from sluice.core.reconcile import next_gate

    database_path = tmp_path / "sluice.db"
    initialize_database(database_path)
    with contextlib.closing(get_connection(database_path)) as setup_connection:
        insert_run(setup_connection, "run-001", pipeline_version="reference-v1")
        update_run(
            setup_connection,
            "run-001",
            {
                "mock_alpha_done": 1,
                "mock_beta_done": 1,
                "completed_at": "2026-07-28T12:00:00+00:00",
            },
        )
    hold(database_path, "will", "run-001", reason="rewinding to alpha")

    workdir = tmp_path / "staging" / "run-001"
    workdir.mkdir(parents=True)
    alpha_marker = workdir / "mock_alpha.marker"
    beta_marker = workdir / "mock_beta.marker"
    alpha_marker.write_text("", encoding="utf-8")
    beta_marker.write_text("", encoding="utf-8")

    returned_result = backtrack(
        database_path, "will", "run-001", "mock_alpha", registry=REFERENCE_PROFILES
    )
    assert returned_result.status == "ok"

    with contextlib.closing(get_connection(database_path)) as verification_connection:
        rewound_row = get_run(verification_connection, "run-001")
        backtrack_audit_rows = verification_connection.execute(
            "SELECT * FROM control_audit WHERE action = 'backtrack'"
        ).fetchall()

    assert rewound_row["mock_alpha_done"] == 0 and rewound_row["mock_beta_done"] == 0, (
        "backtrack to alpha must clear alpha AND the later beta — the "
        "cascade (⚑1); got alpha="
        f"{rewound_row['mock_alpha_done']!r}, beta="
        f"{rewound_row['mock_beta_done']!r}"
    )
    assert rewound_row["completed_at"] is None
    assert alpha_marker.exists() and beta_marker.exists(), (
        "BOTH artifact files must still be on disk — the cascade clears "
        "columns, never artifacts (ADR-0005)"
    )
    assert len(backtrack_audit_rows) == 1 and backtrack_audit_rows[0]["result"] == "ok"
    assert next_gate(rewound_row, REFERENCE_PROFILE).id == "mock_alpha", (
        "reconcile must re-select alpha — the whole rewind is re-selection "
        "over cleared columns"
    )


def test_backtrack_to_the_last_gate_touches_only_it(tmp_path):
    """``backtrack`` to beta clears beta's column only — alpha's satisfaction and every other column survive byte-identical.

    The cascade's lower bound: "the named gate and every later gate" of a
    backtrack to the LAST gate is just that gate, so a mutant that clears
    from the profile's start (or clears everything) is caught by alpha's
    surviving 1. Byte-identical row comparison outside the expected columns
    keeps the op honest about its footprint.
    """
    from sluice.core.control import backtrack, hold
    from sluice.core.db import (
        get_connection,
        get_run,
        initialize_database,
        insert_run,
        update_run,
    )
    from sluice.core.profile import REFERENCE_PROFILES

    database_path = tmp_path / "sluice.db"
    initialize_database(database_path)
    with contextlib.closing(get_connection(database_path)) as setup_connection:
        insert_run(setup_connection, "run-001", pipeline_version="reference-v1")
        update_run(
            setup_connection,
            "run-001",
            {"mock_alpha_done": 1, "mock_beta_done": 1},
        )
    hold(database_path, "will", "run-001", reason="rewinding beta only")
    with contextlib.closing(get_connection(database_path)) as snapshot_connection:
        row_before = dict(get_run(snapshot_connection, "run-001"))

    backtrack(
        database_path, "will", "run-001", "mock_beta", registry=REFERENCE_PROFILES
    )

    with contextlib.closing(get_connection(database_path)) as verification_connection:
        row_after = dict(get_run(verification_connection, "run-001"))

    assert row_after["mock_beta_done"] == 0, "the target gate's column clears"
    assert row_after["mock_alpha_done"] == 1, (
        "alpha PRECEDES the target and must survive — backtrack clears the "
        "target and later gates only (plan 08 unit 13); got "
        f"{row_after['mock_alpha_done']!r}"
    )
    expected_row = dict(row_before)
    expected_row["mock_beta_done"] = 0
    expected_row["completed_at"] = None
    assert row_after == expected_row, (
        "beyond beta's column and completed_at, the row must be "
        "byte-identical — the op's footprint is exactly its declaration"
    )


def test_backtrack_cascades_over_the_base_profile_including_excluded_gates(tmp_path):
    """``backtrack`` to alpha on a run with beta EXCLUDED still clears beta's column, derived-contract completion re-derives false, and alpha is re-selected.

    F3 (review-confirmed, plan 08 §2 ⚑1): the cascade iterates the BASE
    stamped profile, not the exclusion-derived contract. Clearing the
    excluded gate's column is harmless — it is out of the contract, ignored
    by ``is_complete``/``next_gate`` — and it is what guarantees a LATER
    ``include_gate`` never resurrects a stale satisfied column: the mutant
    that cascades over the derived contract would leave beta's 1 behind,
    and a subsequent include would report beta done without beta ever
    re-running after the rewind.
    """
    from sluice.core.control import backtrack, exclude_gate, hold, run_exclusions
    from sluice.core.db import (
        get_connection,
        get_run,
        initialize_database,
        insert_run,
        update_run,
    )
    from sluice.core.profile import REFERENCE_PROFILES, apply_exclusions, load_profile
    from sluice.core.reconcile import is_complete, next_gate

    database_path = tmp_path / "sluice.db"
    initialize_database(database_path)
    with contextlib.closing(get_connection(database_path)) as setup_connection:
        insert_run(setup_connection, "run-001", pipeline_version="reference-v1")
        update_run(
            setup_connection,
            "run-001",
            {"mock_alpha_done": 1, "mock_beta_done": 1},
        )
    exclude_gate(
        database_path, "will", "run-001", "mock_beta", registry=REFERENCE_PROFILES
    )
    hold(database_path, "will", "run-001", reason="rewinding under exclusion")

    backtrack(
        database_path, "will", "run-001", "mock_alpha", registry=REFERENCE_PROFILES
    )

    with contextlib.closing(get_connection(database_path)) as verification_connection:
        rewound_row = get_run(verification_connection, "run-001")

    assert rewound_row["mock_alpha_done"] == 0 and rewound_row["mock_beta_done"] == 0, (
        "the cascade iterates the BASE profile (F3): the EXCLUDED beta's "
        "column clears too, so a later include_gate finds no stale "
        "satisfied column; got alpha="
        f"{rewound_row['mock_alpha_done']!r}, beta="
        f"{rewound_row['mock_beta_done']!r}"
    )
    derived_contract = apply_exclusions(
        load_profile(rewound_row["pipeline_version"], REFERENCE_PROFILES),
        run_exclusions(rewound_row),
    )
    assert not is_complete(rewound_row, derived_contract), (
        "derived-contract completion must re-derive FALSE after the rewind"
    )
    assert next_gate(rewound_row, derived_contract).id == "mock_alpha"


def test_backtrack_never_writes_the_pointer_columns(tmp_path):
    """After a successful ``backtrack``, ``run_from_step`` and ``run_from_fresh`` are both SQL NULL — the dormant pointer columns stay dormant.

    ⚑1's no-pointer resolution and plan 06's binding constraint, executable:
    "a pointer-writing op with no honoring reader is a defect" — nothing in
    Plan A reads these columns, so an op that wrote one would ship exactly
    that defect. This is the schema-level witness that the rewind mechanism
    is re-selection over cleared columns and nothing else.
    """
    from sluice.core.control import backtrack, hold
    from sluice.core.db import (
        get_connection,
        get_run,
        initialize_database,
        insert_run,
        update_run,
    )
    from sluice.core.profile import REFERENCE_PROFILES

    database_path = tmp_path / "sluice.db"
    initialize_database(database_path)
    with contextlib.closing(get_connection(database_path)) as setup_connection:
        insert_run(setup_connection, "run-001", pipeline_version="reference-v1")
        update_run(
            setup_connection,
            "run-001",
            {"mock_alpha_done": 1, "mock_beta_done": 1},
        )
    hold(database_path, "will", "run-001", reason="rewinding")

    backtrack(
        database_path, "will", "run-001", "mock_alpha", registry=REFERENCE_PROFILES
    )

    with contextlib.closing(get_connection(database_path)) as verification_connection:
        rewound_row = get_run(verification_connection, "run-001")
    assert (
        rewound_row["run_from_step"] is None and rewound_row["run_from_fresh"] is None
    ), (
        "backtrack must write NEITHER pointer column — pointerless rewind "
        "(⚑1): a pointer write with no honoring reader is a defect (plan "
        f"06); got run_from_step={rewound_row['run_from_step']!r}, "
        f"run_from_fresh={rewound_row['run_from_fresh']!r}"
    )


def test_backtrack_guards(tmp_path):
    """``backtrack`` raises ``ActiveRunError`` on a daemon-eligible run and ``ProtectedError`` on a protected held run unless ``force`` — both audited refusals leaving the columns untouched.

    The same two policy guards as ``reset_gate``, pinned on the cascade op
    because a mutant could plausibly guard one rewind entry point and not
    the other; both rows land in the trail as ``refused: <why>``.
    """
    from sluice.core.control import (
        ActiveRunError,
        ProtectedError,
        backtrack,
        hold,
    )
    from sluice.core.db import (
        get_connection,
        get_run,
        initialize_database,
        insert_run,
        update_run,
    )
    from sluice.core.profile import REFERENCE_PROFILES

    database_path = tmp_path / "sluice.db"
    initialize_database(database_path)
    with contextlib.closing(get_connection(database_path)) as setup_connection:
        insert_run(setup_connection, "run-001", pipeline_version="reference-v1")
        update_run(
            setup_connection,
            "run-001",
            {
                "mock_alpha_done": 1,
                "protected_at": "2026-07-28T00:00:00+00:00",
                "protected_by": "alice",
            },
        )

    # Eligible (active, not quarantined): the quiescence guard fires first.
    with pytest.raises(ActiveRunError):
        backtrack(
            database_path,
            "will",
            "run-001",
            "mock_alpha",
            registry=REFERENCE_PROFILES,
        )

    hold(database_path, "will", "run-001", reason="quiescing")

    with pytest.raises(ProtectedError, match="alice"):
        backtrack(
            database_path,
            "will",
            "run-001",
            "mock_alpha",
            registry=REFERENCE_PROFILES,
        )

    with contextlib.closing(get_connection(database_path)) as verification_connection:
        guarded_row = get_run(verification_connection, "run-001")
        backtrack_results = [
            row["result"]
            for row in verification_connection.execute(
                "SELECT result FROM control_audit WHERE action = 'backtrack' "
                "ORDER BY id"
            )
        ]
    assert guarded_row["mock_alpha_done"] == 1, "both refusals must write nothing"
    assert len(backtrack_results) == 2 and all(
        result.startswith("refused: ") for result in backtrack_results
    ), f"both refusals are audited (plan 08 §2); got {backtrack_results!r}"

    forced_result = backtrack(
        database_path,
        "will",
        "run-001",
        "mock_alpha",
        registry=REFERENCE_PROFILES,
        force=True,
    )
    assert forced_result.status == "ok"


# --------------------------------------------------------------------------
# Unit 14 — reset: whole-run column-clear preserving identity (ADR-0005).
# --------------------------------------------------------------------------


def test_reset_restores_the_freshly_detected_shape_preserving_identity(tmp_path):
    """``reset`` on a held, progressed, excluded run clears every state column and re-activates it with the resetting actor's trio — while the workdir, identity columns, ``slack_thread_ts``, and ``protected_*`` survive byte-identical.

    The whole-run rewind under ADR-0005 and §2 B2: "freshly detected" means
    the STATE shape (gate columns 0, no completion, no quarantine, no
    exclusions, no globus handle, dormant pointers NULL, lifecycle active)
    — never the identity. ``experiment_id`` and ``slack_thread_ts`` are the
    named non-recomputable handles (B2/A5): clearing them would orphan a
    LabKey experiment or a Slack thread that no re-run can rediscover. The
    workdir surviving untouched is ADR-0005 on the most destructive op in
    the table.
    """
    from sluice.core.control import hold, reset
    from sluice.core.db import (
        get_connection,
        get_run,
        initialize_database,
        insert_run,
        update_run,
    )
    from sluice.core.profile import REFERENCE_PROFILES

    database_path = tmp_path / "sluice.db"
    initialize_database(database_path)
    with contextlib.closing(get_connection(database_path)) as setup_connection:
        insert_run(
            setup_connection,
            "run-001",
            pipeline_version="reference-v1",
            detected_at="2026-07-20T08:00:00+00:00",
            experiment_id=4711,
            size_tb=1.25,
        )
        update_run(
            setup_connection,
            "run-001",
            {
                "mock_alpha_done": 1,
                "mock_beta_done": 1,
                "completed_at": "2026-07-28T12:00:00+00:00",
                "excluded_gates": '["mock_beta"]',
                "globus_task_id": "globus-task-42",
                "slack_thread_ts": "1234567890.123456",
            },
        )
    hold(database_path, "will", "run-001", reason="pre-reset quiesce")

    workdir = tmp_path / "staging" / "run-001"
    workdir.mkdir(parents=True)
    (workdir / "mock_alpha.marker").write_text("", encoding="utf-8")
    (workdir / "mock_beta.marker").write_text("beta bytes", encoding="utf-8")
    workdir_contents_before = {
        artifact_path.name: artifact_path.read_bytes()
        for artifact_path in workdir.iterdir()
    }

    returned_result = reset(
        database_path, "sam", "run-001", registry=REFERENCE_PROFILES, reason="redo all"
    )
    assert returned_result.status == "ok"

    with contextlib.closing(get_connection(database_path)) as verification_connection:
        reset_row = get_run(verification_connection, "run-001")
        reset_audit_rows = verification_connection.execute(
            "SELECT * FROM control_audit WHERE action = 'reset'"
        ).fetchall()

    # Cleared: the freshly-detected state shape.
    assert reset_row["mock_alpha_done"] == 0 and reset_row["mock_beta_done"] == 0
    for cleared_column in (
        "completed_at",
        "errored_at",
        "error_reason",
        "excluded_gates",
        "globus_task_id",
        "run_from_step",
        "run_from_fresh",
    ):
        assert reset_row[cleared_column] is None, (
            f"reset must NULL {cleared_column} — fresh means the full "
            f"stamped contract, no cached state (plan 08 unit 14); got "
            f"{reset_row[cleared_column]!r}"
        )
    assert reset_row["lifecycle"] == "active"
    assert reset_row["lifecycle_by"] == "sam", (
        "the trio must record the RESETTING actor — the row names its "
        f"latest disposition's provenance; got {reset_row['lifecycle_by']!r}"
    )
    assert len(reset_audit_rows) == 1
    assert reset_row["lifecycle_at"] == reset_audit_rows[0]["ts"], (
        "state stamp and audit ts share one now_iso() instant (§2)"
    )

    # Preserved, value by value: identity + non-recomputable handles.
    assert reset_row["name"] == "run-001"
    assert reset_row["experiment_id"] == 4711, (
        "experiment_id must survive — a LabKey experiment is not "
        "recomputable, and Plan B's re-entrant experiment gate REUSES it "
        f"(§2 B2); got {reset_row['experiment_id']!r}"
    )
    assert reset_row["size_tb"] == 1.25
    assert reset_row["detected_at"] == "2026-07-20T08:00:00+00:00"
    assert reset_row["pipeline_version"] == "reference-v1", (
        "the stamped pipeline_version must survive — stamp once, never change (§8)"
    )
    assert reset_row["slack_thread_ts"] == "1234567890.123456", (
        "slack_thread_ts must survive — Slack handles are not recomputable "
        f"(A5); got {reset_row['slack_thread_ts']!r}"
    )

    workdir_contents_after = {
        artifact_path.name: artifact_path.read_bytes()
        for artifact_path in workdir.iterdir()
    }
    assert workdir_contents_after == workdir_contents_before, (
        "the workdir file set and contents must be unchanged — reset "
        "touches exactly one thing, the database (ADR-0005); the re-running "
        "gates' re-entrant runs (or the operator, manually) handle the old "
        "artifacts"
    )


def test_reset_on_a_quarantined_run_needs_no_hold_and_clears_the_error(tmp_path):
    """``reset`` succeeds DIRECTLY on a quarantined run — no prior ``hold`` — and clears the error pair with everything else.

    The §2 residual posture's sanctioned entry states are held, skipped, or
    quarantined: a quarantined run already fails ``ACTIVE_RUNS_WHERE`` (the
    daemon cannot be driving it), so demanding a hold first would be
    ceremony with no rationale. The error pair clearing with the rest is
    the "freshly detected" shape — a reset run starts over clean, not
    quarantined.
    """
    from sluice.core.control import reset
    from sluice.core.db import (
        get_connection,
        get_run,
        initialize_database,
        insert_run,
        select_active_runs,
        update_run,
    )
    from sluice.core.profile import REFERENCE_PROFILES

    database_path = tmp_path / "sluice.db"
    initialize_database(database_path)
    with contextlib.closing(get_connection(database_path)) as setup_connection:
        insert_run(setup_connection, "run-001", pipeline_version="reference-v1")
        update_run(
            setup_connection,
            "run-001",
            {
                "mock_alpha_done": 1,
                "errored_at": "2026-07-28T00:00:00+00:00",
                "error_reason": "mock_beta: scripted failure",
            },
        )

    returned_result = reset(
        database_path, "will", "run-001", registry=REFERENCE_PROFILES
    )

    assert returned_result.status == "ok", (
        "a quarantined run is already ineligible (quiescent) — reset must "
        f"succeed with no prior hold (plan 08 unit 14); got "
        f"{returned_result.status!r}"
    )
    with contextlib.closing(get_connection(database_path)) as verification_connection:
        reset_row = get_run(verification_connection, "run-001")
        active_names = [
            row["name"] for row in select_active_runs(verification_connection)
        ]
    assert reset_row["errored_at"] is None and reset_row["error_reason"] is None, (
        "reset clears the quarantine pair with everything else — fresh means clean"
    )
    assert reset_row["mock_alpha_done"] == 0
    assert active_names == ["run-001"], (
        "the reset run is active and eligible again — the daemon's next "
        "look starts it over"
    )


def test_reset_guards_and_force_preserves_protection(tmp_path):
    """``reset`` refuses a daemon-eligible run with ``ActiveRunError`` and a protected held run with ``ProtectedError``; ``force=True`` then succeeds with ``protected_*`` surviving.

    The rewind family's guard pair on its most destructive member, plus the
    A3 force semantics: protection is BYPASSED, never consumed — after a
    forced reset the run is still guarded, so the next destructive op still
    refuses. A force that cleared protection would silently downgrade the
    run's guard as a side effect of using it.
    """
    from sluice.core.control import (
        ActiveRunError,
        ProtectedError,
        hold,
        reset,
    )
    from sluice.core.db import (
        get_connection,
        get_run,
        initialize_database,
        insert_run,
        update_run,
    )
    from sluice.core.profile import REFERENCE_PROFILES

    database_path = tmp_path / "sluice.db"
    initialize_database(database_path)
    with contextlib.closing(get_connection(database_path)) as setup_connection:
        insert_run(setup_connection, "run-001", pipeline_version="reference-v1")
        update_run(
            setup_connection,
            "run-001",
            {
                "mock_alpha_done": 1,
                "protected_at": "2026-07-28T00:00:00+00:00",
                "protected_by": "alice",
            },
        )

    with pytest.raises(ActiveRunError):
        reset(database_path, "will", "run-001", registry=REFERENCE_PROFILES)

    hold(database_path, "will", "run-001", reason="quiescing")

    with pytest.raises(ProtectedError, match="alice"):
        reset(database_path, "will", "run-001", registry=REFERENCE_PROFILES)

    with contextlib.closing(get_connection(database_path)) as refusal_connection:
        assert get_run(refusal_connection, "run-001")["mock_alpha_done"] == 1, (
            "both refusals must write nothing"
        )

    forced_result = reset(
        database_path, "will", "run-001", registry=REFERENCE_PROFILES, force=True
    )
    assert forced_result.status == "ok"
    with contextlib.closing(get_connection(database_path)) as verification_connection:
        forced_row = get_run(verification_connection, "run-001")
    assert forced_row["mock_alpha_done"] == 0, "the forced reset landed"
    assert (
        forced_row["protected_at"] == "2026-07-28T00:00:00+00:00"
        and forced_row["protected_by"] == "alice"
    ), (
        "protected_* must SURVIVE the forced reset — force bypasses one "
        "refusal, it never unprotects (errata A3 / plan 08 unit 14)"
    )


# --------------------------------------------------------------------------
# Unit 15 — get_status: the pure read.
# --------------------------------------------------------------------------


def test_get_status_single_run_reports_the_derived_contract(tmp_path):
    """Single-run ``get_status`` reports ``complete`` over the DERIVED contract, the exclusion list, a partition covering only contract gates, and an ``experiment_id`` key present even when ``None``.

    The operator's view must be the contract's truth, not the base
    profile's: with alpha satisfied and beta excluded the run IS complete
    (ADR-0004 d.2 — beta satisfied by absence), and the gates partition
    covers ONLY the contract (no phantom 'unsatisfied mock_beta' scaring an
    operator about a step that no longer exists for this run). The
    always-present ``experiment_id`` is the deliberate contrast with the
    event builders' omit-when-NULL: a status table renders the blank, and
    experiment_id is the reference key surfaced everywhere.
    """
    from sluice.core.control import exclude_gate, get_status
    from sluice.core.db import (
        get_connection,
        initialize_database,
        insert_run,
        update_run,
    )
    from sluice.core.profile import REFERENCE_PROFILES

    database_path = tmp_path / "sluice.db"
    initialize_database(database_path)
    with contextlib.closing(get_connection(database_path)) as setup_connection:
        insert_run(setup_connection, "run-001", pipeline_version="reference-v1")
        update_run(setup_connection, "run-001", {"mock_alpha_done": 1})
    exclude_gate(
        database_path, "will", "run-001", "mock_beta", registry=REFERENCE_PROFILES
    )

    run_status = get_status(database_path, "run-001", registry=REFERENCE_PROFILES)

    assert run_status["name"] == "run-001"
    assert "experiment_id" in run_status and run_status["experiment_id"] is None, (
        "the experiment_id KEY must always be present, None allowed — the "
        "operator's reference key is surfaced everywhere (plan 08 unit 15); "
        f"got {run_status!r}"
    )
    assert run_status["complete"] is True, (
        "complete is derived over the DERIVED contract — alpha done, beta "
        "excluded means complete (ADR-0004 d.2); got "
        f"{run_status['complete']!r}"
    )
    assert run_status["excluded_gates"] == ["mock_beta"]
    assert run_status["gates"] == {"satisfied": ["mock_alpha"], "unsatisfied": []}, (
        "the partition covers ONLY contract gates — the excluded beta must "
        f"appear in neither list; got {run_status['gates']!r}"
    )
    assert run_status["pipeline_version"] == "reference-v1"
    assert run_status["detected_at"] is not None
    assert run_status["completed_at"] is None


def test_get_status_overview_reports_global_and_per_run_state(tmp_path):
    """The overview shape carries ``paused``/``pipeline_profile`` plus one summary per run — the quarantined one classified ``quarantined`` with its ``error_reason``.

    The fleet view an operator (and Plan C) reads first: global facts once,
    then every run — quarantined and completed included, because a status
    board that hid exactly the runs needing attention would be worthless.
    ``outcome`` reuses ``events.run_outcome`` (one classification authority,
    plan 08 §2), so the quarantined entry's precedence is the §3.3 one.
    """
    from sluice.core.control import PIPELINE_PROFILE_KEY, get_status, pause
    from sluice.core.db import (
        get_connection,
        initialize_database,
        insert_run,
        set_metadata,
        update_run,
    )
    from sluice.core.profile import REFERENCE_PROFILES

    database_path = tmp_path / "sluice.db"
    initialize_database(database_path)
    with contextlib.closing(get_connection(database_path)) as setup_connection:
        set_metadata(setup_connection, PIPELINE_PROFILE_KEY, "reference-v1")
        insert_run(
            setup_connection,
            "run-001",
            pipeline_version="reference-v1",
            detected_at="2026-07-20T08:00:00+00:00",
        )
        insert_run(
            setup_connection,
            "run-002",
            pipeline_version="reference-v1",
            detected_at="2026-07-21T08:00:00+00:00",
        )
        update_run(
            setup_connection,
            "run-002",
            {
                "errored_at": "2026-07-28T00:00:00+00:00",
                "error_reason": "mock_beta: scripted failure",
            },
        )
    pause(database_path, "will", reason="maintenance window")

    overview = get_status(database_path, registry=REFERENCE_PROFILES)

    assert overview["paused"] is True, (
        "the overview must echo the paused flag via the one canonical parse "
        f"(is_paused); got {overview['paused']!r}"
    )
    assert overview["pipeline_profile"] == "reference-v1"
    assert [entry["name"] for entry in overview["runs"]] == ["run-001", "run-002"], (
        "every run appears, in deterministic (detected_at, name) order; got "
        f"{[entry['name'] for entry in overview['runs']]!r}"
    )
    quarantined_entry = overview["runs"][1]
    assert quarantined_entry["outcome"] == "quarantined", (
        "the quarantined run must be classified via run_outcome — the "
        f"actionable fact outranks all (plan 03 §3.3); got "
        f"{quarantined_entry['outcome']!r}"
    )
    assert quarantined_entry["error_reason"] == "mock_beta: scripted failure", (
        "the quarantined entry carries its error_reason — what the operator must act on"
    )
    healthy_entry = overview["runs"][0]
    assert healthy_entry["outcome"] == "active"
    assert healthy_entry["complete"] is False
    assert "experiment_id" in healthy_entry


def test_get_status_is_a_pure_read(tmp_path):
    """``get_status`` writes no audit row, changes no run row or metadata, and emits zero events.

    Plan 07 §6 adopted (plan 08 §2): it mutates nothing, so §9's "each op
    writes a row" reads as each STATE-WRITING op — and consequently the
    function takes no ``actor``, because there is nothing to attribute. A
    status poll running every few seconds must not grow the trail or the
    event stream by its own observation.
    """
    from sluice.core.control import get_status
    from sluice.core.db import (
        all_metadata,
        get_connection,
        get_run,
        initialize_database,
        insert_run,
    )
    from sluice.core.profile import REFERENCE_PROFILES

    database_path = tmp_path / "sluice.db"
    initialize_database(database_path)
    with contextlib.closing(get_connection(database_path)) as setup_connection:
        insert_run(setup_connection, "run-001", pipeline_version="reference-v1")

    with contextlib.closing(get_connection(database_path)) as snapshot_connection:
        row_before = tuple(get_run(snapshot_connection, "run-001"))
        metadata_before = all_metadata(snapshot_connection)
        audit_count_before = snapshot_connection.execute(
            "SELECT COUNT(*) FROM control_audit"
        ).fetchone()[0]

    with structlog.testing.capture_logs() as captured_log_entries:
        get_status(database_path, registry=REFERENCE_PROFILES)
        get_status(database_path, "run-001", registry=REFERENCE_PROFILES)

    with contextlib.closing(get_connection(database_path)) as verification_connection:
        row_after = tuple(get_run(verification_connection, "run-001"))
        metadata_after = all_metadata(verification_connection)
        audit_count_after = verification_connection.execute(
            "SELECT COUNT(*) FROM control_audit"
        ).fetchone()[0]

    assert row_after == row_before
    assert metadata_after == metadata_before
    assert audit_count_after == audit_count_before, (
        "a pure read must not grow the trail — observation is not action "
        f"(plan 08 §2); grew {audit_count_before} -> {audit_count_after}"
    )
    assert [
        entry for entry in captured_log_entries if entry["event"] == "control.finished"
    ] == [], "get_status emits nothing — there is no committed write to describe"


def test_get_status_reports_a_drifted_profile_instead_of_raising(tmp_path):
    """A run stamped with an unregistered version gets a ``profile_error`` naming it — no fabricated ``complete``/``gates`` — while the other runs report normally.

    Status is TOTAL (plan 08 unit 15): one broken run must neither hide the
    fleet (a raise would blank the whole board at exactly the moment an
    operator needs it) nor be rendered healthy (fabricating ``complete``
    for a contract that cannot even be resolved would be a lie). The
    per-run error field is the honest middle.
    """
    from sluice.core.control import get_status
    from sluice.core.db import get_connection, initialize_database, insert_run
    from sluice.core.profile import REFERENCE_PROFILES

    database_path = tmp_path / "sluice.db"
    initialize_database(database_path)
    with contextlib.closing(get_connection(database_path)) as setup_connection:
        insert_run(
            setup_connection,
            "run-drifted",
            pipeline_version="drifted-v9",
            detected_at="2026-07-20T08:00:00+00:00",
        )
        insert_run(
            setup_connection,
            "run-healthy",
            pipeline_version="reference-v1",
            detected_at="2026-07-21T08:00:00+00:00",
        )

    overview = get_status(database_path, registry=REFERENCE_PROFILES)

    entries_by_name = {entry["name"]: entry for entry in overview["runs"]}
    drifted_entry = entries_by_name["run-drifted"]
    assert "drifted-v9" in drifted_entry["profile_error"], (
        "the drifted entry's profile_error must NAME the unresolvable "
        f"version; got {drifted_entry!r}"
    )
    assert "complete" not in drifted_entry and "gates" not in drifted_entry, (
        "no fabricated completion facts for a contract that cannot be "
        f"resolved (plan 08 unit 15); got {drifted_entry!r}"
    )
    healthy_entry = entries_by_name["run-healthy"]
    assert (
        healthy_entry["complete"] is False and "profile_error" not in healthy_entry
    ), (
        "the OTHER runs must report normally — status is total, the broken "
        f"run does not poison the fleet; got {healthy_entry!r}"
    )


def test_get_status_reports_corrupt_exclusions_instead_of_raising(tmp_path):
    """A run whose ``excluded_gates`` is a JSON-valid non-array gets an ``exclusions_error`` naming the bad shape — no fabricated ``complete``/``excluded_gates`` — while the other runs report normally.

    F5, mirroring the drift test: ``run_exclusions``' loud ``ValueError``
    (unit 11) is caught per-run HERE and only here — every other consumer
    propagates it, but the status board must stay total under one corrupt
    row. Fabricating ``complete`` from the base contract would silently
    ignore whatever the operator had excluded.
    """
    from sluice.core.control import get_status
    from sluice.core.db import get_connection, initialize_database, insert_run
    from sluice.core.profile import REFERENCE_PROFILES

    database_path = tmp_path / "sluice.db"
    initialize_database(database_path)
    with contextlib.closing(get_connection(database_path)) as setup_connection:
        insert_run(
            setup_connection,
            "run-corrupt",
            pipeline_version="reference-v1",
            detected_at="2026-07-20T08:00:00+00:00",
        )
        insert_run(
            setup_connection,
            "run-healthy",
            pipeline_version="reference-v1",
            detected_at="2026-07-21T08:00:00+00:00",
        )
        # JSON-valid non-array: passes the migration CHECK, planted by raw
        # SQL exactly as a bug or hand edit would.
        setup_connection.execute(
            "UPDATE runs SET excluded_gates = '{\"a\": 1}' WHERE name = 'run-corrupt'"
        )

    overview = get_status(database_path, registry=REFERENCE_PROFILES)

    entries_by_name = {entry["name"]: entry for entry in overview["runs"]}
    corrupt_entry = entries_by_name["run-corrupt"]
    assert '{"a": 1}' in corrupt_entry["exclusions_error"], (
        "the corrupt entry's exclusions_error must NAME the bad stored "
        f"shape (F5); got {corrupt_entry!r}"
    )
    assert "complete" not in corrupt_entry and "excluded_gates" not in corrupt_entry, (
        "no fabricated completion or exclusion facts over an unparseable "
        f"overlay (plan 08 unit 15); got {corrupt_entry!r}"
    )
    healthy_entry = entries_by_name["run-healthy"]
    assert "exclusions_error" not in healthy_entry and "complete" in healthy_entry


def test_get_status_unknown_run_fails_loud(tmp_path):
    """The named-run form raises ``UnknownRunError`` for a run that does not exist.

    The single-run shape is an answer about ONE run; inventing an empty
    entry for a typo'd name would let a monitoring caller poll a
    nonexistent run forever without noticing (guardrail §8 — fail loud,
    Plan C maps this to 404).
    """
    from sluice.core.control import UnknownRunError, get_status
    from sluice.core.db import initialize_database
    from sluice.core.profile import REFERENCE_PROFILES

    database_path = tmp_path / "sluice.db"
    initialize_database(database_path)

    with pytest.raises(UnknownRunError):
        get_status(database_path, "run-999", registry=REFERENCE_PROFILES)


# --------------------------------------------------------------------------
# Unit 16 — end-to-end journey + surface (green-on-arrival).
# --------------------------------------------------------------------------


def test_operator_journey_end_to_end(tmp_path):
    """A run is fully controllable through the pure ops alone — paused, held, rewound (column-clear, marker intact, re-entrant re-run), quarantined, cleared, excluded, complete — with the audit trail listing exactly the op sequence in action order.

    The §12 deliverable over the real schema (plan 08 unit 16): the test
    plays the daemon VERBATIM where the daemon would act (resolve_context,
    mkdir, ``gate.run``, persist in a short txn — plan 06 §7's shape) and
    plays the operator through control ops everywhere else, proving the two
    write paths compose. The rewind leg is ADR-0005's soundness argument
    demonstrated: reset alpha (marker survives), unhold, RE-RUN alpha over
    the existing marker — SUCCESS, §4 re-entrancy for real. The ordered
    trail read closes it: ``ORDER BY id`` (never ts — plan 07 §6) lists
    exactly the operator's actions, each under its D-11 function name.
    """
    import sluice.core.control as control
    from sluice.core.context import resolve_context
    from sluice.core.db import (
        get_connection,
        get_run,
        initialize_database,
        insert_run,
        update_run,
        write_txn,
    )
    from sluice.core.gate import GateOutcome
    from sluice.core.mock_gates import MOCK_ALPHA, MOCK_BETA
    from sluice.core.profile import REFERENCE_PROFILES, apply_exclusions, load_profile
    from sluice.core.reconcile import is_complete, next_gate

    database_path = tmp_path / "sluice.db"
    initialize_database(database_path)
    config = {
        "local_staging_dir": str(tmp_path / "staging"),
        "state_file": str(database_path),
    }
    with contextlib.closing(get_connection(database_path)) as setup_connection:
        insert_run(setup_connection, "run-001", pipeline_version="reference-v1")

    def daemon_look():
        """One daemon look, plan 06 §7's shape: fresh row, derived contract, next gate."""
        with contextlib.closing(get_connection(database_path)) as look_connection:
            row = get_run(look_connection, "run-001")
        derived = apply_exclusions(
            load_profile(row["pipeline_version"], REFERENCE_PROFILES),
            control.run_exclusions(row),
        )
        return row, derived, next_gate(row, derived)

    # Operator: pause the world, then resume it.
    control.pause(database_path, "will", reason="drill")
    control.resume(database_path, "will", reason="drill over")

    # Daemon, verbatim: resolve context, mkdir, run alpha, persist.
    row, derived, selected_gate = daemon_look()
    assert selected_gate.id == "mock_alpha"
    run_context = resolve_context(row, config)
    run_context.workdir.mkdir(parents=True)
    assert MOCK_ALPHA.run(run_context).outcome is GateOutcome.SUCCESS
    with write_txn(database_path) as persist_connection:
        update_run(persist_connection, "run-001", {MOCK_ALPHA.satisfied_column: 1})
    alpha_marker = run_context.workdir / "mock_alpha.marker"
    assert alpha_marker.exists()

    # Operator: hold, rewind alpha (column-clear only), release.
    control.hold(database_path, "will", "run-001", reason="suspect alpha output")
    control.reset_gate(
        database_path, "will", "run-001", "mock_alpha", registry=REFERENCE_PROFILES
    )
    row, derived, selected_gate = daemon_look()
    assert row["mock_alpha_done"] == 0 and alpha_marker.exists(), (
        "the rewind cleared the column and LEFT the marker (ADR-0005)"
    )
    control.unhold(database_path, "will", "run-001", reason="re-running")

    # Daemon: re-run alpha OVER the existing marker — §4 re-entrancy, the
    # exact property ADR-0005's column-clear-only rewind stands on.
    row, derived, selected_gate = daemon_look()
    assert selected_gate.id == "mock_alpha"
    assert MOCK_ALPHA.run(run_context).outcome is GateOutcome.SUCCESS, (
        "alpha must re-enter over its own pre-existing marker and succeed — "
        "§4 re-entrancy exercised for real"
    )
    with write_txn(database_path) as persist_connection:
        update_run(persist_connection, "run-001", {MOCK_ALPHA.satisfied_column: 1})

    # Daemon: beta is scripted to fail; persist the quarantine pair.
    (run_context.workdir / "mock_beta.instruction").write_text(
        "failure", encoding="utf-8"
    )
    row, derived, selected_gate = daemon_look()
    assert selected_gate.id == "mock_beta"
    beta_result = MOCK_BETA.run(run_context)
    assert beta_result.outcome is GateOutcome.FAILURE
    with write_txn(database_path) as persist_connection:
        update_run(
            persist_connection,
            "run-001",
            {
                "errored_at": "2026-07-29T00:00:00+00:00",
                "error_reason": beta_result.reason,
            },
        )

    # Operator: forgive the quarantine, then remediate by exclusion.
    control.clear_error(
        database_path, "will", "run-001", reason="beta is optional here"
    )
    control.exclude_gate(
        database_path,
        "will",
        "run-001",
        "mock_beta",
        registry=REFERENCE_PROFILES,
        reason="beta retired for this run",
    )

    # The daemon's next look: contract complete, nothing selected.
    row, derived, selected_gate = daemon_look()
    assert selected_gate is None and is_complete(row, derived), (
        "after the exclusion the derived contract is complete — the stuck "
        "run resumed through the pure ops alone (§12)"
    )

    with contextlib.closing(get_connection(database_path)) as trail_connection:
        audited_actions = [
            row["action"]
            for row in trail_connection.execute(
                "SELECT action FROM control_audit ORDER BY id"
            )
        ]
    assert audited_actions == [
        "pause",
        "resume",
        "hold",
        "reset_gate",
        "unhold",
        "clear_error",
        "exclude_gate",
    ], (
        "the trail ORDER BY id must list exactly the operator's op sequence "
        "in action order — daemon persists are never audited (plan 07 §6) "
        f"and every op landed exactly one row (D-11); got {audited_actions!r}"
    )


def test_no_control_op_ever_touches_the_workdir(tmp_path):
    """After one op from EVERY mutating op class — pause, protect, set_gate(force), exclude_gate(force), hold, reset_gate(force), backtrack(force), reset(force), skip(force) — the workdir file set and contents are byte-identical.

    ADR-0005 made total over the whole surface, behaviorally: the ast
    import guard (unit 1) proves control CANNOT reach a file, and this test
    proves it DOES not — covering the rewind family, whose superseded
    design deleted artifacts, on a run whose workdir holds every mock
    artifact. The op order is chosen so each call is legal under the
    lifecycle/quiescence rules (rewinds on a held run; skip after reset
    re-activates).
    """
    import sluice.core.control as control
    from sluice.core.db import get_connection, initialize_database, insert_run
    from sluice.core.profile import REFERENCE_PROFILES

    database_path = tmp_path / "sluice.db"
    initialize_database(database_path)
    with contextlib.closing(get_connection(database_path)) as setup_connection:
        insert_run(setup_connection, "run-001", pipeline_version="reference-v1")

    workdir = tmp_path / "staging" / "run-001"
    workdir.mkdir(parents=True)
    (workdir / "mock_alpha.marker").write_text("alpha bytes", encoding="utf-8")
    (workdir / "mock_beta.instruction").write_text("success", encoding="utf-8")
    (workdir / "mock_beta.marker").write_text("beta bytes", encoding="utf-8")
    (workdir / "mock_beta.terminated").write_text("", encoding="utf-8")
    workdir_contents_before = {
        artifact_path.name: artifact_path.read_bytes()
        for artifact_path in workdir.iterdir()
    }

    control.pause(database_path, "will", reason="sweep")
    control.protect(database_path, "will", "run-001", reason="sweep")
    control.set_gate(
        database_path,
        "will",
        "run-001",
        "mock_alpha",
        registry=REFERENCE_PROFILES,
        force=True,
    )
    control.exclude_gate(
        database_path,
        "will",
        "run-001",
        "mock_beta",
        registry=REFERENCE_PROFILES,
        force=True,
    )
    control.hold(database_path, "will", "run-001", reason="sweep")
    control.reset_gate(
        database_path,
        "will",
        "run-001",
        "mock_alpha",
        registry=REFERENCE_PROFILES,
        force=True,
    )
    control.backtrack(
        database_path,
        "will",
        "run-001",
        "mock_alpha",
        registry=REFERENCE_PROFILES,
        force=True,
    )
    control.reset(
        database_path, "will", "run-001", registry=REFERENCE_PROFILES, force=True
    )
    control.skip(database_path, "will", "run-001", reason="sweep done", force=True)

    workdir_contents_after = {
        artifact_path.name: artifact_path.read_bytes()
        for artifact_path in workdir.iterdir()
    }
    assert workdir_contents_after == workdir_contents_before, (
        "after all nine mutating op classes the workdir must be "
        "byte-identical — ADR-0005 total over the whole surface: ops touch "
        "exactly one thing, the database; before: "
        f"{sorted(workdir_contents_before)!r}, after: "
        f"{sorted(workdir_contents_after)!r}"
    )


def test_control_public_surface_is_exactly_the_declared_ops():
    """``__all__`` equals the §1 table exactly, every op's identifiers are positional in declared order with all options keyword-only, ``registry`` never has a default, and the no-actor exemptions hold.

    The subsystem-closing surface pin (plan 08 unit 16, H-12 corrected):
    Plan C wraps each op VERBATIM, so the signature shape is the API
    contract — ``db_path``/``actor`` first, target identifiers positional
    exactly as declared in §3, and ``reason``/``force``/``registry``
    keyword-only (a positional force would let a caller pass ``True`` by
    accident into the one flag that overrides protection). ``registry``
    required-with-no-default is guardrail §8: omitting it must TypeError,
    never fall back to a production registry. ``is_paused``/
    ``run_exclusions``/``get_status`` take no ``actor`` — nothing to
    attribute on a read.
    """
    import sluice.core.control as control

    assert set(control.__all__) == {
        # Types + constants.
        "ControlResult",
        "ControlError",
        "ProtectedError",
        "FabricationError",
        "ActiveRunError",
        "LifecycleError",
        "UnknownRunError",
        "UnknownGateError",
        "PAUSED_KEY",
        "PIPELINE_PROFILE_KEY",
        # Global ops + reader.
        "pause",
        "resume",
        "set_default_profile",
        "is_paused",
        # Disposition.
        "hold",
        "unhold",
        "skip",
        "unskip",
        # Recovery.
        "clear_error",
        # Protection.
        "protect",
        "unprotect",
        # Gate state + reader.
        "set_gate",
        "reset_gate",
        "backtrack",
        "exclude_gate",
        "include_gate",
        "run_exclusions",
        # Whole-run + read-only.
        "reset",
        "get_status",
    }, "the public surface is exactly the plan 08 §1 table — set equality"

    declared_positional_shapes = {
        "pause": ("db_path", "actor"),
        "resume": ("db_path", "actor"),
        "set_default_profile": ("db_path", "actor", "pipeline_version"),
        "hold": ("db_path", "actor", "run_name"),
        "unhold": ("db_path", "actor", "run_name"),
        "skip": ("db_path", "actor", "run_name"),
        "unskip": ("db_path", "actor", "run_name"),
        "clear_error": ("db_path", "actor", "run_name"),
        "protect": ("db_path", "actor", "run_name"),
        "unprotect": ("db_path", "actor", "run_name"),
        "set_gate": ("db_path", "actor", "run_name", "gate_id"),
        "reset_gate": ("db_path", "actor", "run_name", "gate_id"),
        "backtrack": ("db_path", "actor", "run_name", "gate_id"),
        "exclude_gate": ("db_path", "actor", "run_name", "gate_id"),
        "include_gate": ("db_path", "actor", "run_name", "gate_id"),
        "reset": ("db_path", "actor", "run_name"),
    }
    for op_name, expected_positional in declared_positional_shapes.items():
        op_signature = inspect.signature(getattr(control, op_name))
        positional_parameter_names = tuple(
            parameter.name
            for parameter in op_signature.parameters.values()
            if parameter.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
        )
        assert positional_parameter_names == expected_positional, (
            f"{op_name}'s positional parameters must be exactly "
            f"{expected_positional} in order (plan 08 §3 declared shape — "
            f"Plan C wraps it verbatim); got {positional_parameter_names!r}"
        )
        for parameter in op_signature.parameters.values():
            if parameter.name in ("reason", "force", "registry"):
                assert parameter.kind is inspect.Parameter.KEYWORD_ONLY, (
                    f"{op_name}'s {parameter.name} must be KEYWORD-ONLY "
                    f"(plan 08 §1/§3); got {parameter.kind!r}"
                )
        if "registry" in op_signature.parameters:
            assert (
                op_signature.parameters["registry"].default is inspect.Parameter.empty
            ), (
                f"{op_name}'s registry must have NO default — omitting it "
                "must TypeError, never fall back to a production registry "
                "(guardrail §8)"
            )

    for no_actor_name in ("is_paused", "run_exclusions", "get_status"):
        no_actor_signature = inspect.signature(getattr(control, no_actor_name))
        assert "actor" not in no_actor_signature.parameters, (
            f"{no_actor_name} takes NO actor — a pure read has nothing to "
            "attribute (plan 08 §2)"
        )
    get_status_signature = inspect.signature(control.get_status)
    assert (
        get_status_signature.parameters["run_name"].default is None
        and get_status_signature.parameters["registry"].kind
        is inspect.Parameter.KEYWORD_ONLY
    ), "get_status is (db_path, run_name=None, *, registry) — the §3 shape"


@pytest.mark.parametrize(
    "op_case",
    [
        "skip-on-already-skipped",
        "set_gate-on-already-satisfied",
        "reset_gate-on-already-clear",
    ],
)
def test_protection_outranks_the_same_state_noop(tmp_path, op_case):
    """A protected run ALREADY in the op's target state raises ``ProtectedError`` — protection-first, never the same-state noop.

    The review-pinned precedence (plan 08 review, non-blocking nit 1): every
    protected-set op consults protection BEFORE its already-in-target-state
    check, so a protected run answers "hands off" even when the op would
    change nothing — informative where a noop is not, and one uniform answer
    instead of a per-state maybe. The refusal is audited and the row
    survives byte-identical; a refactor that reorders the elif chain to
    noop-first flips all three cases at once and goes red here.
    """
    from sluice.core.control import ProtectedError, reset_gate, set_gate, skip
    from sluice.core.db import (
        get_connection,
        get_run,
        initialize_database,
        insert_run,
        update_run,
    )
    from sluice.core.profile import REFERENCE_PROFILES

    database_path = tmp_path / "sluice.db"
    initialize_database(database_path)
    with contextlib.closing(get_connection(database_path)) as setup_connection:
        insert_run(setup_connection, "run-001", pipeline_version="reference-v1")
        already_in_target_state = {
            # skip's target: lifecycle already 'skipped' (planted with trio).
            "skip-on-already-skipped": {
                "lifecycle": "skipped",
                "lifecycle_by": "first-operator",
                "lifecycle_at": "2026-07-28T00:00:00+00:00",
                "lifecycle_reason": "planted disposition",
            },
            # set_gate's target: the column already 1.
            "set_gate-on-already-satisfied": {"mock_alpha_done": 1},
            # reset_gate's target: the column already 0 — held so the
            # quiescence guard passes and protection is what answers.
            "reset_gate-on-already-clear": {
                "lifecycle": "held",
                "lifecycle_by": "first-operator",
                "lifecycle_at": "2026-07-28T00:00:00+00:00",
                "lifecycle_reason": "planted disposition",
            },
        }[op_case]
        update_run(
            setup_connection,
            "run-001",
            {
                **already_in_target_state,
                "protected_at": "2026-07-28T00:00:00+00:00",
                "protected_by": "alice",
            },
        )
        row_before = tuple(get_run(setup_connection, "run-001"))

    with pytest.raises(ProtectedError, match="alice"):
        if op_case == "skip-on-already-skipped":
            skip(database_path, "will", "run-001", reason="probe")
        elif op_case == "set_gate-on-already-satisfied":
            set_gate(
                database_path,
                "will",
                "run-001",
                "mock_alpha",
                registry=REFERENCE_PROFILES,
            )
        else:
            reset_gate(
                database_path,
                "will",
                "run-001",
                "mock_alpha",
                registry=REFERENCE_PROFILES,
            )

    with contextlib.closing(get_connection(database_path)) as verification_connection:
        row_after = tuple(get_run(verification_connection, "run-001"))
        audit_rows = verification_connection.execute(
            "SELECT * FROM control_audit"
        ).fetchall()
    assert row_after == row_before, (
        "the protection refusal must write nothing — the run row survives "
        "byte-identical even though the op would have been a noop anyway"
    )
    assert len(audit_rows) == 1 and audit_rows[0]["result"].startswith("refused: "), (
        "protection-first means the trail records a REFUSAL, not a noop — "
        f"one audited 'refused: <why>' row (plan 08 §2); got "
        f"{[row['result'] for row in audit_rows]!r}"
    )
