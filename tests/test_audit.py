"""Behavioral tests for ``sluice.core.audit`` — the ``control_audit`` writer (plan 07).

Audit is the §9 middle step's primitive: every control op writes one append-only
``control_audit`` row (design §5) through ``record_control_action``, inside the
op's own transaction — the writer never opens, commits, or rolls one back
(plan 01 F2). The trail is state, never a log line or a Slack post (§11), and
it orders by ``id``, never ``ts`` (an NTP step can move wall-clock backwards).
"""

import ast
import inspect
import pathlib
import re
from datetime import datetime, timedelta

import pytest

import sluice.core.db

# --------------------------------------------------------------------------
# Unit 1 — record_control_action (the writer): import hygiene first.
# --------------------------------------------------------------------------

# The closed import set (plan 07 §1): everything the writer needs, nothing that
# would let it grow a second capability. With only ``now_iso`` importable from
# db, ``get_connection``/``write_txn`` are structurally unreachable — the
# caller-supplies-the-connection contract is a compile surface, not a promise.
ALLOWED_AUDIT_IMPORTS = {"json", "sqlite3", "collections.abc", "sluice.core.db"}

# Forbidden imports asserted BY NAME so the failure message teaches (plan 07
# unit 1): each name is a distinct policy with its own reason.
FORBIDDEN_AUDIT_IMPORTS = {
    "structlog": (
        "the audit row is state, never a log line — the wide event half of "
        "§9 is events.control_event, emitted by the op after commit "
        "(plan 03 §7 persist-then-emit); audit emits nothing"
    ),
    "slack_sdk": (
        "control actions never post to Slack (design §11) and core never "
        "imports a service SDK (ADR-0003 decision 3)"
    ),
    "sluice.gates": (
        "layering is gates -> core, never the reverse (ADR-0003); the audit "
        "writer is gate-agnostic by construction"
    ),
    "sluice.api": ("core never imports the API layer (ADR-0003 layering)"),
    "sluice.cli": ("core never imports the CLI layer (ADR-0003 layering)"),
    "datetime": (
        "every timestamp routes through db.now_iso — one clock spelling, "
        "one format (plan 07 §1); a second clock site could diverge"
    ),
    "time": (
        "every timestamp routes through db.now_iso — one clock spelling, "
        "one format (plan 07 §1); a second clock site could diverge"
    ),
    "os": (
        "a state-only writer starts nothing and reaches nothing — no "
        "environment, no filesystem beyond the caller's connection (§2)"
    ),
    "subprocess": (
        "a state-only writer starts nothing — control mutates state, it "
        "never acts (design §2)"
    ),
    "socket": ("a state-only writer reaches no network (design §2)"),
    "urllib": ("a state-only writer reaches no network (design §2)"),
    "http": ("a state-only writer reaches no network (design §2)"),
}


def test_audit_import_hygiene():
    """``audit.py`` imports only ``{json, sqlite3, collections.abc, sluice.core.db}``, and from db exactly ``now_iso``.

    The plan-07 structural guard: the only-``now_iso`` pin is load-bearing —
    with ``get_connection``/``write_txn`` unreachable, audit structurally
    cannot open its own connection or transaction, so §9's "the op owns the
    transaction" choreography holds by what the module can even name.
    Likewise no ``structlog``/``slack_sdk`` (the row-vs-event split of §11 and
    plan 03 §7 made structural) and no ``datetime``/``time`` (one clock
    spelling, one format). Parsed with ``ast``, not inspected at runtime,
    because a function-local import — precisely how a "just this once"
    connection or clock read sneaks in — never shows up in the module
    namespace (same technique as ``test_db_imports_only_stdlib``).
    """
    # Local import: audit is the module under construction; a module-level
    # import would fail collection for every other test in this file.
    import sluice.core.audit

    source_path = inspect.getsourcefile(sluice.core.audit)
    tree = ast.parse(pathlib.Path(source_path).read_text(encoding="utf-8"))

    # Collect fully-qualified module names for every import statement, and the
    # names imported from sluice.core.db. For ``from X import Y`` we record
    # the module X (absolute) or resolve the relative base against this
    # module's package, sluice.core — so ``from .db import now_iso`` counts
    # toward the db-names pin and ``from . import db`` cannot dodge the guard.
    imported_modules = set()
    names_imported_from_db = set()
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
            if resolved is not None:
                imported_modules.add(resolved)
                if resolved == "sluice.core.db":
                    names_imported_from_db.update(alias.name for alias in node.names)

    def is_allowed(module_name: str) -> bool:
        """True when ``module_name`` is an allowed module or a submodule of one."""
        return any(
            module_name == allowed or module_name.startswith(f"{allowed}.")
            for allowed in ALLOWED_AUDIT_IMPORTS
        )

    outside_the_closed_set = sorted(
        module for module in imported_modules if not is_allowed(module)
    )
    assert outside_the_closed_set == [], (
        f"{source_path} imports {outside_the_closed_set}, outside the closed "
        f"set {sorted(ALLOWED_AUDIT_IMPORTS)} (plan 07 §1): the audit writer "
        "needs json + sqlite3 + collections.abc + db.now_iso and nothing else"
    )

    forbidden_found = {
        module: reason
        for forbidden, reason in FORBIDDEN_AUDIT_IMPORTS.items()
        for module in sorted(imported_modules)
        if module == forbidden or module.startswith(f"{forbidden}.")
    }
    assert forbidden_found == {}, (
        f"{source_path} imports forbidden modules:\n"
        + "\n".join(f"  {name}: {why}" for name, why in forbidden_found.items())
    )

    assert names_imported_from_db == {"now_iso"}, (
        f"{source_path} imports {sorted(names_imported_from_db)} from "
        "sluice.core.db; exactly {'now_iso'} is allowed (plan 07 §1) — with "
        "get_connection/write_txn unreachable, the writer structurally cannot "
        "open its own connection or transaction (§9: the caller owns both)"
    )


def test_record_control_action_appends_one_row_and_returns_its_id(tmp_path):
    """One call inside the caller's ``write_txn`` appends exactly one row whose six values read back verbatim, and returns that row's ``id``.

    The §9 middle step in its native habitat: the op owns the transaction, the
    writer contributes one static INSERT with every value bound, and the
    returned ``id`` lets the op ride the row back on its Result. The args
    payload carries a hostile SQL string asserted stored *literally* — with
    ``runs`` still present — because the statement is static and every value is
    bound: data can never become SQL here (no dynamic identifiers exist, unlike
    ``update_run``, so injection rides the happy path instead of earning its
    own suite). A regression here means the writer dropped or transposed a
    column binding, wrote more (or less) than one row, or returned something
    other than the new row's id.
    """
    # Local import: the module under construction (same rationale as the
    # hygiene test's local import).
    from sluice.core.audit import record_control_action

    db_path = tmp_path / "state.db"
    sluice.core.db.initialize_database(db_path)

    injected_ts = "2026-07-28T12:00:00+00:00"
    hostile_reason = "x'); DROP TABLE runs;--"

    with sluice.core.db.write_txn(db_path) as writer_connection:
        returned_audit_id = record_control_action(
            writer_connection,
            actor="operator",
            action="hold",
            target_run="run-alpha",
            args={"reason": hostile_reason},
            result="ok",
            ts=injected_ts,
        )

    # A fresh connection after commit: what landed is what any later reader
    # sees, not a view scoped to the writer's transaction.
    reader_connection = sluice.core.db.get_connection(db_path)
    try:
        audit_rows = reader_connection.execute(
            "SELECT id, ts, actor, action, target_run, args_json, result"
            " FROM control_audit"
        ).fetchall()
        assert len(audit_rows) == 1, "one call must append exactly one row"
        landed_row = audit_rows[0]
        assert landed_row["id"] == returned_audit_id, (
            "record_control_action must return the new row's id "
            f"(row id {landed_row['id']!r}, returned {returned_audit_id!r})"
        )
        assert landed_row["ts"] == injected_ts
        assert landed_row["actor"] == "operator"
        assert landed_row["action"] == "hold"
        assert landed_row["target_run"] == "run-alpha"
        # Stored literally (a literal, not a re-dump): the hostile value is
        # data forever, never SQL.
        assert landed_row["args_json"] == '{"reason": "x\'); DROP TABLE runs;--"}'
        assert landed_row["result"] == "ok"
        runs_table_present = reader_connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'runs'"
        ).fetchall()
        assert len(runs_table_present) == 1, (
            "the runs table must survive a hostile args value — every value "
            "is bound, so data cannot become SQL"
        )
    finally:
        reader_connection.close()


def test_record_control_action_defaults_ts_to_now_iso(tmp_path):
    """Omitting ``ts`` stores a ``db.now_iso`` timestamp: ISO-8601 carrying a UTC offset.

    The one behavioral test for a delegation (plan 07 unit 1): the sluice
    decision pinned here is that the default timestamp routes through
    ``db.now_iso`` — one clock spelling, one format, always UTC — not a second
    clock site that could diverge (a naive local ``ts`` from another host would
    misorder a lexically-sorted column). The format's own properties are db's
    tested decision (``test_now_iso_returns_a_live_utc_iso_string``), not
    re-proven here.
    """
    # Local import: the module under construction (same rationale as the
    # hygiene test's local import).
    from sluice.core.audit import record_control_action

    db_path = tmp_path / "state.db"
    sluice.core.db.initialize_database(db_path)

    with sluice.core.db.write_txn(db_path) as writer_connection:
        record_control_action(writer_connection, actor="operator", action="hold")

    reader_connection = sluice.core.db.get_connection(db_path)
    try:
        stored_ts = reader_connection.execute(
            "SELECT ts FROM control_audit"
        ).fetchone()["ts"]
    finally:
        reader_connection.close()

    # fromisoformat raising (e.g. on NULL/None or a non-ISO string) is itself
    # the failure: the default was not a now_iso() value.
    parsed_default_ts = datetime.fromisoformat(stored_ts)
    assert parsed_default_ts.utcoffset() == timedelta(0), (
        f"default ts {stored_ts!r} must carry a UTC offset — the ts default "
        "routes through db.now_iso (plan 07 §1: one clock spelling, one "
        "format), never a naive or local-time stamp"
    )


def test_record_control_action_serializes_args_deterministically_and_none_as_null(
    tmp_path,
):
    r"""Scrambled-insertion-order args store one byte-identical sorted literal; ``args=None`` stores SQL NULL.

    Pins the plan-07 §2 serialization contract against a *literal*, not a
    tautological re-dump: ``sort_keys=True`` means the same mapping stores the
    same bytes whatever order the op built its dict in (two rows for the same
    action must be diffable as text), ``ensure_ascii=False`` means ``café``
    lands as readable UTF-8 (not ``caf\\u00e9`` escapes), and the no-hook
    contract means booleans/lists land as plain JSON with nothing routed
    through a coercing ``default=``. ``args=None`` must land as SQL NULL —
    absent, not ``'null'`` and not ``'{}'`` (the no-fabrication ethic) —
    asserted via ``args_json IS NULL``, which is false for both of those TEXT
    values.
    """
    # Local import: the module under construction (same rationale as the
    # hygiene test's local import).
    from sluice.core.audit import record_control_action

    db_path = tmp_path / "state.db"
    sluice.core.db.initialize_database(db_path)

    # The same mapping, two scrambled insertion orders (Python dicts preserve
    # insertion order, so without sort_keys these would serialize differently).
    args_in_one_order = {
        "waves": [1, 2],
        "note": "café",
        "force": True,
        "gate": "checksum",
    }
    args_in_another_order = {
        "gate": "checksum",
        "force": True,
        "note": "café",
        "waves": [1, 2],
    }
    expected_sorted_utf8_literal = (
        '{"force": true, "gate": "checksum", "note": "café", "waves": [1, 2]}'
    )

    with sluice.core.db.write_txn(db_path) as writer_connection:
        first_audit_id = record_control_action(
            writer_connection, actor="operator", action="hold", args=args_in_one_order
        )
        second_audit_id = record_control_action(
            writer_connection,
            actor="operator",
            action="hold",
            args=args_in_another_order,
        )
        none_args_audit_id = record_control_action(
            writer_connection, actor="operator", action="hold", args=None
        )

    reader_connection = sluice.core.db.get_connection(db_path)
    try:
        for audit_id in (first_audit_id, second_audit_id):
            stored_args_json = reader_connection.execute(
                "SELECT args_json FROM control_audit WHERE id = ?", (audit_id,)
            ).fetchone()["args_json"]
            assert stored_args_json == expected_sorted_utf8_literal, (
                f"row {audit_id} stored {stored_args_json!r}, expected the "
                f"byte-identical literal {expected_sorted_utf8_literal!r} — "
                "serialization is pinned to sort_keys=True (deterministic "
                "whatever the op's insertion order) + ensure_ascii=False "
                "(UTF-8 stored readably), plan 07 §2"
            )
        # IS NULL is false for the TEXT values 'null' and '{}' — this is the
        # distinction the plan demands (absent, not a fabricated empty payload).
        rows_with_null_args = reader_connection.execute(
            "SELECT id FROM control_audit WHERE args_json IS NULL"
        ).fetchall()
        assert [row["id"] for row in rows_with_null_args] == [none_args_audit_id], (
            "args=None must store SQL NULL — absent, not 'null' and not '{}' "
            "(plan 07 §2: the no-fabrication ethic); only the args=None row "
            "may satisfy args_json IS NULL"
        )
    finally:
        reader_connection.close()


@pytest.mark.parametrize(
    ("hostile_args", "expected_exception"),
    [
        # No default= hook: a coercing hook (default=repr) would silently
        # record a lossy representation — fabricated evidence hiding a caller
        # bug. Refusal is the contract (plan 07 §2).
        pytest.param({"p": object()}, TypeError, id="non-serializable-value"),
        # allow_nan=False: NaN would otherwise emit the non-JSON token 'NaN'.
        pytest.param({"n": float("nan")}, ValueError, id="nan-value"),
        # json.dumps would silently coerce {1: "x"} -> {"1": "x"} — a type
        # rewrite; the writer must refuse instead.
        pytest.param({1: "x"}, TypeError, id="top-level-int-key"),
        # The recursive walk, not a top-level check: the same hole one level
        # down ({"a": {1: "x"}} coerces just as silently) must also refuse.
        pytest.param({"a": {1: "x"}}, TypeError, id="nested-int-key"),
        # Mappings nested inside sequences are walked too; json.dumps would
        # coerce {True: "x"} -> {"true": "x"} silently.
        pytest.param({"a": [{True: "x"}]}, TypeError, id="bool-key-inside-list"),
    ],
)
def test_record_control_action_fails_loud_on_unserializable_args(
    tmp_path, hostile_args, expected_exception
):
    """A non-serializable, NaN, or non-``str``-keyed ``args`` payload — at any depth — raises before the connection is touched.

    Pins the plan-07 §2 fail-loud contract: no ``default=`` hook (a coercing
    hook would record fabricated evidence), ``allow_nan=False`` (NaN raises
    instead of emitting non-JSON), and the *recursive* key walk — ``json.dumps``
    silently rewrites ``{1: "x"}`` → ``{"1": "x"}`` and ``{True: "x"}`` →
    ``{"true": "x"}`` at every depth, so a top-level-only check would leave the
    identical hole one level down (the nested and nested-in-sequence params are
    the ones a shallow check fails). Validation and serialization run before
    the INSERT, so after each refusal the trail's row count is unchanged: the
    connection was never touched, and inside an op's ``write_txn`` the raise
    rolls the whole op back rather than landing a half-audited state change.
    """
    # Local import: the module under construction (same rationale as the
    # hygiene test's local import).
    from sluice.core.audit import record_control_action

    db_path = tmp_path / "state.db"
    sluice.core.db.initialize_database(db_path)

    # A plain autocommit connection, deliberately: had the writer reached its
    # INSERT before raising, the row would have autocommitted and the count
    # below would expose it — no enclosing transaction to mask the touch.
    connection = sluice.core.db.get_connection(db_path)
    try:
        with pytest.raises(expected_exception):
            record_control_action(
                connection, actor="operator", action="hold", args=hostile_args
            )
        audit_row_count = connection.execute(
            "SELECT COUNT(*) FROM control_audit"
        ).fetchone()[0]
        assert audit_row_count == 0, (
            f"a refused args payload {hostile_args!r} must never touch the "
            "connection — validation and serialization run before the INSERT "
            "(plan 07 §2), yet a row landed"
        )
    finally:
        connection.close()


# --------------------------------------------------------------------------
# Unit 2 — the trail orders by id, never ts; the writer never manages the
# transaction. Contract pins on inherited facts 2 and 3 (plan 07 §1), raw SQL,
# green against a correct unit 1 (the plan-02 unit-9 carve-out).
# --------------------------------------------------------------------------


def test_audit_trail_orders_by_id_not_ts(tmp_path):
    """A later action with an *earlier* wall-clock ``ts`` still sorts after by ``id`` — ``ORDER BY id`` is true action order; ``ORDER BY ts`` lies.

    Pins inherited fact 3 (plan 07 §1, from plan 01's migration-003 rationale):
    an NTP step can move wall-clock backwards, so the trail's ordering key is
    the AUTOINCREMENT ``id`` — strictly increasing, never recycled — and never
    ``ts``. The step-back is staged through the ``ts`` parameter (row B lands
    *after* row A but stamped one hour *earlier*), and the same test
    demonstrates the wall-clock lie: ``ORDER BY ts`` returns (B, A), inverting
    what actually happened. Any future consumer ordering this table on ``ts``
    has this test to answer to — the Plan C reader carry-forward (plan 07 §6)
    cites it by name. A regression here means the writer stopped storing the
    caller's ``ts`` verbatim (normalizing timestamps would *hide* the step-back
    instead of recording it) or stopped returning ids that reflect insert order.
    """
    # Local import: the module under construction (same rationale as the
    # hygiene test's local import).
    from sluice.core.audit import record_control_action

    db_path = tmp_path / "state.db"
    sluice.core.db.initialize_database(db_path)

    # Row A at noon; row B written AFTER it, but wall-clock stepped back an
    # hour between the two ops (the NTP lie). Two separate transactions, as
    # two real control ops would be.
    wall_clock_at_first_action = "2026-07-28T12:00:00+00:00"
    wall_clock_stepped_back_an_hour = "2026-07-28T11:00:00+00:00"

    with sluice.core.db.write_txn(db_path) as writer_connection:
        first_action_id = record_control_action(
            writer_connection,
            actor="operator",
            action="hold",
            ts=wall_clock_at_first_action,
        )
    with sluice.core.db.write_txn(db_path) as writer_connection:
        second_action_id = record_control_action(
            writer_connection,
            actor="operator",
            action="resume",
            ts=wall_clock_stepped_back_an_hour,
        )

    assert second_action_id > first_action_id, (
        f"the later action's id ({second_action_id}) must exceed the earlier "
        f"one's ({first_action_id}) even though its ts is an hour earlier — "
        "ids strictly increase in true action order (plan 01 migration-003 "
        "rationale)"
    )

    reader_connection = sluice.core.db.get_connection(db_path)
    try:
        ids_ordered_by_id = [
            row["id"]
            for row in reader_connection.execute(
                "SELECT id FROM control_audit ORDER BY id"
            )
        ]
        ids_ordered_by_ts = [
            row["id"]
            for row in reader_connection.execute(
                "SELECT id FROM control_audit ORDER BY ts"
            )
        ]
    finally:
        reader_connection.close()

    assert ids_ordered_by_id == [first_action_id, second_action_id], (
        "ORDER BY id must return (A, B) — the true action order, immune to "
        "wall-clock steps"
    )
    assert ids_ordered_by_ts == [second_action_id, first_action_id], (
        "ORDER BY ts must return (B, A) here — the wall-clock lie this test "
        "exists to demonstrate: the stored ts is the caller's verbatim, so a "
        "backwards NTP step inverts ts-order while id-order stays true. Any "
        "consumer ordering on ts answers to this test (plan 07 §6, the Plan C "
        "reader carry-forward)."
    )


def test_record_control_action_never_commits_the_callers_transaction(tmp_path):
    """An audit row written inside the caller's ``write_txn`` is invisible to a second connection, and vanishes when the caller aborts — the writer issues no COMMIT and opens no transaction of its own.

    Pins inherited fact 2 (plan 07 §1 / plan 01 F2, the composition contract
    the §9 atomicity choreography stands on): the writer contributes one INSERT
    to a transaction it does not manage. This is the probe that catches the
    plausible-wrong unit-1 writer — one that opens its own BEGIN/COMMIT passes
    every unit-1 test (all of which commit via ``write_txn``'s clean exit) and
    fails only here. Two vantage points on the one behavior: mid-transaction, a
    second connection must see zero rows (a writer-issued COMMIT would have
    published the row already); and after the caller raises before the block
    exits, a fresh connection must still see zero rows (``write_txn``'s
    rollback took the audit row with it — nothing was separately committed).
    """
    # Local import: the module under construction (same rationale as the
    # hygiene test's local import).
    from sluice.core.audit import record_control_action

    db_path = tmp_path / "state.db"
    sluice.core.db.initialize_database(db_path)

    # The sentinel the caller raises mid-op: distinct from anything the writer
    # or write_txn could raise, so the pytest.raises match is attributable.
    class CallerAbortsBeforeCommit(Exception):
        """Raised by the test to abort the op after the audit write."""

    with pytest.raises(CallerAbortsBeforeCommit):
        with sluice.core.db.write_txn(db_path) as writer_connection:
            record_control_action(
                writer_connection,
                actor="operator",
                action="hold",
                target_run="run-alpha",
                result="ok",
            )
            # Mid-transaction probe: WAL readers see only committed state, so
            # a second connection seeing this row could mean exactly one thing
            # — the writer committed it (directly, or by closing a transaction
            # it opened itself).
            mid_transaction_reader = sluice.core.db.get_connection(db_path)
            try:
                rows_visible_mid_transaction = mid_transaction_reader.execute(
                    "SELECT COUNT(*) FROM control_audit"
                ).fetchone()[0]
            finally:
                mid_transaction_reader.close()
            assert rows_visible_mid_transaction == 0, (
                "an audit row inside the caller's still-open write_txn leaked "
                "to a second connection — the writer must never COMMIT or "
                "manage a transaction of its own (plan 01 F2; plan 07 §2 "
                "choreography)"
            )
            raise CallerAbortsBeforeCommit

    # Post-rollback probe: the caller aborted, so write_txn rolled back — the
    # audit row must be gone. A row surviving here was committed by the writer.
    post_rollback_reader = sluice.core.db.get_connection(db_path)
    try:
        rows_visible_after_rollback = post_rollback_reader.execute(
            "SELECT COUNT(*) FROM control_audit"
        ).fetchone()[0]
    finally:
        post_rollback_reader.close()
    assert rows_visible_after_rollback == 0, (
        "the caller's rollback must take the audit row with it — a surviving "
        "row means the writer committed the caller's transaction (or opened "
        "and closed its own), breaking §9's state-and-audit-land-atomically "
        "choreography"
    )


@pytest.mark.parametrize("blank_value", ["", "   "], ids=["empty", "whitespace-only"])
@pytest.mark.parametrize("blank_field", ["actor", "action"])
def test_record_control_action_refuses_blank_actor_or_action(
    tmp_path, blank_field, blank_value
):
    """A blank (empty or whitespace-only) ``actor`` or ``action`` raises ``ValueError`` naming the field, and no row lands.

    Pins the plan-07 §2 blank-identity refusal: the schema's NOT NULL admits
    ``''``, and an anonymous audit row is worthless evidence — record
    integrity (like ``update_run``'s whitelist), not control policy. The
    message must name the offending field so a caller wiring actor/action from
    config or request JSON learns *which* half of its identity is missing.
    Validation runs before the INSERT, so the refused call never touches the
    connection: the count stays zero on a plain autocommit connection that
    would otherwise expose a landed row.
    """
    # Local import: the module under construction (same rationale as the
    # hygiene test's local import).
    from sluice.core.audit import record_control_action

    db_path = tmp_path / "state.db"
    sluice.core.db.initialize_database(db_path)

    # One field blanked, the other valid — so the raise is attributable to
    # exactly the parametrized field.
    call_identity = {"actor": "operator", "action": "hold"}
    call_identity[blank_field] = blank_value

    # A plain autocommit connection, deliberately (same rationale as the
    # unserializable-args test): a landed row could not hide in an
    # uncommitted transaction.
    connection = sluice.core.db.get_connection(db_path)
    try:
        with pytest.raises(ValueError, match=blank_field):
            record_control_action(connection, **call_identity)
        audit_row_count = connection.execute(
            "SELECT COUNT(*) FROM control_audit"
        ).fetchone()[0]
        assert audit_row_count == 0, (
            f"a blank {blank_field} ({blank_value!r}) must be refused before "
            "the INSERT (plan 07 §2: an anonymous audit row is worthless "
            "evidence), yet a row landed"
        )
    finally:
        connection.close()


# --------------------------------------------------------------------------
# Unit 3 — choreography integration + surface (green-on-arrival, the plan-02
# unit-9 carve-out). The composed §9 behavior of units 1–2 over the real
# schema, and the append-only module-surface promise (⚑1 decided:
# code promise only — this guard IS the enforcement).
# --------------------------------------------------------------------------


def test_control_op_choreography_state_plus_audit_land_atomically(tmp_path):
    """A control-op-shaped ``write_txn`` lands state + audit atomically in both directions, and the audit trail survives run deletion.

    The §9 choreography in miniature (plan 07 §2 — the binding contract
    subsystem 8 implements): a fake ``hold`` op batches ``update_run`` and
    ``record_control_action`` in one ``write_txn``, so a crash between the
    state write and its audit row is impossible *by construction* — they are
    one atomic commit, not two writes hoping to stay adjacent. Both deliberate
    consequences are asserted: (happy) after commit a fresh connection sees the
    run held AND exactly one audit row carrying the op's fields; (failure) when
    the audit half refuses — an unserializable ``args`` value raising before
    its INSERT — the op's raise rolls the whole transaction back, so the state
    change never lands unaudited and no orphan audit row lands either. Finally
    ``DELETE FROM runs`` (raw SQL — no public surface deletes runs) proves the
    trail outlives its run row: ``target_run`` is a name with no FK by design
    (plan 01 F1), because evidence about a deleted run is exactly when the
    trail matters.
    """
    # Local import: the module under construction (same rationale as the
    # hygiene test's local import).
    from sluice.core.audit import record_control_action

    db_path = tmp_path / "state.db"
    sluice.core.db.initialize_database(db_path)

    with sluice.core.db.write_txn(db_path) as setup_connection:
        run_was_created = sluice.core.db.insert_run(
            setup_connection, "r1", pipeline_version="v1"
        )
        assert run_was_created, "test setup: the r1 row must exist before the op"

    # A caller may pass ts so the state write and its audit row share the same
    # instant (plan 07 unit 1) — the op shape uses that affordance.
    shared_op_instant = "2026-07-28T12:00:00+00:00"

    # ---- Happy direction: mutate -> audit -> commit, one transaction. ----
    with sluice.core.db.write_txn(db_path) as op_connection:
        run_was_updated = sluice.core.db.update_run(
            op_connection,
            "r1",
            {
                "lifecycle": "held",
                "lifecycle_by": "operator",
                "lifecycle_at": shared_op_instant,
                "lifecycle_reason": "drill",
            },
        )
        assert run_was_updated, "the fake hold op's mutation must match r1"
        held_audit_id = record_control_action(
            op_connection,
            actor="operator",
            action="hold",
            target_run="r1",
            args={"reason": "drill"},
            result="ok",
            ts=shared_op_instant,
        )

    reader_connection = sluice.core.db.get_connection(db_path)
    try:
        held_run = sluice.core.db.get_run(reader_connection, "r1")
        assert held_run["lifecycle"] == "held", (
            "the state half of the op must have landed: r1 held after commit"
        )
        audit_rows = reader_connection.execute(
            "SELECT id, ts, actor, action, target_run, args_json, result"
            " FROM control_audit"
        ).fetchall()
        assert len(audit_rows) == 1, (
            "the audit half must have landed with the state half — exactly "
            "one row for the one op (§9: no state change without its audit row)"
        )
        landed_audit_row = audit_rows[0]
        assert landed_audit_row["id"] == held_audit_id
        assert landed_audit_row["ts"] == shared_op_instant
        assert landed_audit_row["actor"] == "operator"
        assert landed_audit_row["action"] == "hold"
        assert landed_audit_row["target_run"] == "r1"
        assert landed_audit_row["args_json"] == '{"reason": "drill"}'
        assert landed_audit_row["result"] == "ok"
    finally:
        reader_connection.close()

    # ---- Failure direction: the audit half refuses, the whole op rolls
    # back — the mutation below would flip r1 back to active, and must not
    # survive its audit row's refusal. ----
    with pytest.raises(TypeError):
        with sluice.core.db.write_txn(db_path) as op_connection:
            sluice.core.db.update_run(
                op_connection,
                "r1",
                {"lifecycle": "active", "lifecycle_reason": "resumed"},
            )
            record_control_action(
                op_connection,
                actor="operator",
                action="resume",
                target_run="r1",
                # A non-serializable value: the writer refuses before its
                # INSERT (no default= hook, plan 07 §2), and the raise rolls
                # the op's whole transaction back.
                args={"handle": object()},
                result="ok",
            )

    reader_connection = sluice.core.db.get_connection(db_path)
    try:
        run_after_failed_op = sluice.core.db.get_run(reader_connection, "r1")
        assert run_after_failed_op["lifecycle"] == "held", (
            "the failed op's state change must not land: an audit-side refusal "
            "rolls the mutation back too — the state change never lands "
            "unaudited (§9 atomicity, plan 07 §2)"
        )
        audit_row_count_after_failed_op = reader_connection.execute(
            "SELECT COUNT(*) FROM control_audit"
        ).fetchone()[0]
        assert audit_row_count_after_failed_op == 1, (
            "no orphan audit row from the failed op — only the successful "
            "hold's row remains (§9: no audit row without its state change)"
        )
    finally:
        reader_connection.close()

    # ---- Run deletion: the trail survives its run row (no FK, plan 01). ----
    with sluice.core.db.write_txn(db_path) as deleting_connection:
        # Raw SQL by necessity: no public surface deletes runs.
        deleting_connection.execute("DELETE FROM runs")

    reader_connection = sluice.core.db.get_connection(db_path)
    try:
        assert sluice.core.db.get_run(reader_connection, "r1") is None, (
            "test integrity: the run row must actually be gone"
        )
        surviving_audit_row = reader_connection.execute(
            "SELECT actor, action, target_run, result FROM control_audit"
        ).fetchone()
        assert surviving_audit_row is not None, (
            "the audit trail must outlive the run row it targets — "
            "target_run is a name, not an FK (plan 01 F1): deleting r1 must "
            "never cascade into the evidence about r1"
        )
        assert surviving_audit_row["target_run"] == "r1", (
            "the surviving row still names the deleted run — the trail keeps "
            "its evidence verbatim after the run is gone"
        )
    finally:
        reader_connection.close()


def test_audit_public_surface_is_exactly_the_declared_writer():
    """``__all__`` is exactly ``{"record_control_action"}`` and no public name is mutation-shaped — THE append-only enforcement.

    With ⚑1 decided as code-promise-only (no DB triggers — they could not
    coexist with the db suite's executable AUTOINCREMENT proof), the
    append-only invariant lives entirely in this module's surface, and this
    test is its enforcement (plan 07 §2/⚑1; the db unit-7 scope-guard
    precedent, ``test_db_exports_exactly_the_documented_surface``, pointed at
    audit itself). Two independent teeth: set equality on ``__all__`` catches
    any exported growth (a reader belongs to Plan C, a second writer to
    nobody), and the mutation-name regex over *all* public names catches an
    update/delete/purge/edit/amend/redact surface even if someone forgets to
    export it — an unexported ``delete_...`` is still a mutation surface.
    """
    # Local import: the module under construction (same rationale as the
    # hygiene test's local import).
    import sluice.core.audit

    assert set(sluice.core.audit.__all__) == {"record_control_action"}, (
        f"audit exports {sorted(sluice.core.audit.__all__)}; the surface is "
        "the writer alone (plan 07 ⚑1/⚑2: append-only is a module-surface "
        "promise, and the reader is deferred to Plan C with its first caller)"
    )

    mutation_surface_pattern = re.compile(r"^(update|delete|purge|edit|amend|redact)_")
    public_names = [
        name for name in vars(sluice.core.audit) if not name.startswith("_")
    ]
    mutation_shaped_public_names = sorted(
        name for name in public_names if mutation_surface_pattern.match(name)
    )
    assert mutation_shaped_public_names == [], (
        f"audit grew a mutation-shaped surface {mutation_shaped_public_names} "
        "— the control_audit trail is append-only, forever (design §5; "
        "plan 07 ⚑1: no update/delete surface, exported or not)"
    )
