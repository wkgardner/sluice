"""Drift guard: the SQL and Python halves of the eligibility rule must agree.

The daemon-eligibility rule — ``lifecycle = 'active' AND errored_at IS NULL``
(F4) — has to exist twice. ``db.ACTIVE_RUNS_WHERE`` is the SQL half, because
``select_active_runs`` filters inside SQLite; ``db.is_daemon_eligible`` is the
Python half, because ``daemon.process_run`` and
``control._quiescence_refusal_why`` re-check a fresh row in Python. SQLite
cannot call a Python function, so the pair is irreducible.

Plan 14 collapsed every *avoidable* copy of this rule and left exactly this one
seam. A docstring cannot fail; this file can. It runs both halves over the same
rows and asserts they select the same runs, so editing one without the other
turns it red immediately — the `.claude/hooks/arch_guard.py` PostToolUse hook
runs it on any edit under ``sluice/core/``.

Deliberately self-contained (its own row inserter, no import from
``tests/test_db.py``) so the hook can run this file alone and stay fast.
"""

import contextlib
import sqlite3

import pytest

import sluice.core.db
from sluice.core.db import is_daemon_eligible, now_iso, select_active_runs

# Every lifecycle the schema admits, crossed with both error states below.
LIFECYCLES = ("active", "held", "skipped")


@contextlib.contextmanager
def initialized_connection(db_path):
    """Migrate a fresh ``db_path`` to head and yield a row-keyed connection."""
    sluice.core.db.initialize_database(db_path)
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        yield connection
    finally:
        connection.close()


def insert_row(connection, name, *, lifecycle, errored, completed=False):
    """Insert one ``runs`` row with lifecycle, error, and completion set explicitly.

    ``errored`` sets ``errored_at`` and ``error_reason`` together, as the paired
    schema CHECK requires — a half-set row would be rejected by the database
    before either predicate ever saw it.
    """
    connection.execute(
        "INSERT INTO runs "
        "(name, detected_at, pipeline_version, lifecycle, errored_at, error_reason, "
        " completed_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            name,
            now_iso(),
            "reference-v1",
            lifecycle,
            now_iso() if errored else None,
            "boom" if errored else None,
            now_iso() if completed else None,
        ),
    )


def populate_full_cross_product(connection):
    """Insert one run for every (lifecycle × errored) combination, plus a completed one.

    The completed-but-active row is load-bearing, not filler: ``completed_at``
    is deliberately absent from the rule (F4), so this row is what catches a
    future attempt to "obviously" tighten either half with
    ``completed_at IS NULL``. Without it, that mutation would pass both halves
    identically and the twins would drift together, undetected.
    """
    for lifecycle in LIFECYCLES:
        for errored in (False, True):
            insert_row(
                connection,
                f"{lifecycle}_{'errored' if errored else 'clear'}",
                lifecycle=lifecycle,
                errored=errored,
            )
    insert_row(
        connection,
        "active_completed",
        lifecycle="active",
        errored=False,
        completed=True,
    )
    connection.commit()


def test_sql_and_python_eligibility_twins_select_the_same_runs(tmp_path):
    """``ACTIVE_RUNS_WHERE`` and ``is_daemon_eligible`` agree on every row.

    The whole point of this file. The SQL half filters in the database; the
    Python half is handed every row and filters in memory. Over the same
    population the two must produce the identical set of names. Change one
    half's rule and the sets diverge here.
    """
    with initialized_connection(tmp_path / "state.db") as connection:
        populate_full_cross_product(connection)

        sql_selected = {row["name"] for row in select_active_runs(connection)}
        every_row = connection.execute("SELECT * FROM runs").fetchall()
        python_selected = {row["name"] for row in every_row if is_daemon_eligible(row)}

    assert sql_selected == python_selected, (
        "the SQL and Python halves of the eligibility rule disagree — "
        "db.ACTIVE_RUNS_WHERE and db.is_daemon_eligible are twins and must be "
        f"edited together (plan 14). SQL selected {sorted(sql_selected)}, "
        f"Python selected {sorted(python_selected)}."
    )


def test_the_twins_agree_on_a_non_empty_and_non_total_set(tmp_path):
    """The agreed set is neither everything nor nothing.

    Guards the way this file could rot into a vacuous pass: two predicates that
    both selected every row, or both selected none, would agree perfectly while
    testing nothing. Pinning that the agreement is a genuine partition keeps the
    twin test honest.
    """
    with initialized_connection(tmp_path / "state.db") as connection:
        populate_full_cross_product(connection)

        selected = {row["name"] for row in select_active_runs(connection)}
        total = connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0]

    assert selected, "the eligibility rule selected no runs at all — vacuous agreement"
    assert len(selected) < total, (
        "the eligibility rule selected every run — it is filtering nothing, so "
        "agreement between the twins proves nothing"
    )


@pytest.mark.parametrize(
    ("name", "expected_eligible"),
    [
        ("active_clear", True),
        ("active_completed", True),
        ("active_errored", False),
        ("held_clear", False),
        ("held_errored", False),
        ("skipped_clear", False),
        ("skipped_errored", False),
    ],
)
def test_each_row_lands_where_the_rule_says(tmp_path, name, expected_eligible):
    """Both halves place each individual row on the expected side of the rule.

    The set comparison above proves the twins agree; this proves they agree on
    the *right* answer. Without it, two halves that had drifted identically
    would still pass.
    """
    with initialized_connection(tmp_path / "state.db") as connection:
        populate_full_cross_product(connection)

        sql_selected = {row["name"] for row in select_active_runs(connection)}
        run_row = sluice.core.db.get_run(connection, name)

    assert (name in sql_selected) is expected_eligible, (
        f"SQL half: {name} must be "
        f"{'eligible' if expected_eligible else 'ineligible'} (F4)."
    )
    assert is_daemon_eligible(run_row) is expected_eligible, (
        f"Python half: {name} must be "
        f"{'eligible' if expected_eligible else 'ineligible'} (F4)."
    )
