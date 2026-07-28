"""Behavioral tests for ``sluice.core.reconcile`` — the pure decision engine (plan 06).

Reconcile is the pure core of the loop (design §7): given a run's state row and
its resolved ``Profile``, it computes the next contract gate to run
(``next_gate``) and derives completion (``is_complete``). It is PURE — state
in, decision out (plan 06 §2 fact 1): no I/O, no DB, no clock, no network, and
it writes nothing (ADR-0002 — reconcile decides, the daemon persists). Contracts
are binary (ADR-0004): every contract gate is mandatory, excluded gates are
absent, and there is no runtime optional tier.
"""

import ast
import inspect
import itertools
import pathlib
import sqlite3
import sys

import pytest

from sluice.core.gate import Gate, GateOutcome, GateResult
from sluice.core.profile import (
    REFERENCE_PROFILE,
    REFERENCE_PROFILE_ALPHA_DISABLED,
    Applicability,
    Profile,
    build_profile,
)


def make_gate(id, order, *, needs=(), satisfied_column=None) -> Gate:
    """Build a minimal ``Gate`` for reconcile tests — a no-op task with a fake column.

    Reconcile never calls ``gate.run`` (plan 06 §1 — that is the daemon's), so
    the behavior functions are inert no-ops; what matters is the *shape* —
    ``id``/``order``/``needs``/``satisfied_column`` — which is everything the
    decision engine reads. ``satisfied_column`` defaults to ``f"{id}_done"``
    and need not exist in any real schema: reconcile reads columns off the
    plain-``dict`` row it is handed (a benefit of its purity, plan 06 §4).
    """
    return Gate(
        id=id,
        name=id,
        satisfied_column=satisfied_column
        if satisfied_column is not None
        else f"{id}_done",
        order=order,
        needs=needs,
        produces=(),
        run=lambda run_context: GateResult(GateOutcome.SUCCESS),
        invalidate=lambda run_context: None,
    )


def make_profile(entries) -> Profile:
    """Build a throwaway ``Profile`` over ``(gate, Applicability)`` pairs (plan 06 §4).

    A thin wrapper over ``build_profile("test-v", entries)`` so tests can state
    arbitrary contract shapes inline; all validation (unique ids/columns/orders,
    needs-closure, ascending order) still runs in ``Profile.__post_init__``.
    """
    return build_profile("test-v", entries)


def test_reconcile_is_pure_by_import():
    """``reconcile.py`` imports only stdlib + ``sluice.core.gate``/``profile``; DB/SDK/clock/network names forbidden.

    This one test carries DECIDED FACT 1 structurally (plan 06 §1/§2): purity
    is a property of the import surface, not a promise. A module that cannot
    name ``sqlite3``/``sluice.core.db`` cannot read or write the DB — so it
    can never re-implement eligibility, read ``metadata.pipeline_profile``
    behind the freeze, or persist a decision (ADR-0002) — and one that cannot
    name ``time``/``datetime``/``random``/``os``/``socket``/``urllib``/
    ``http``/``subprocess`` cannot read a clock, entropy, the environment, or
    the network, so "``next_gate`` is a pure function of ``(run_row,
    profile)``" holds by what the module can even name. "A hidden read is a
    defect" becomes a compile surface, not a review note. Parsed with ``ast``,
    not inspected at runtime, because a function-local import — precisely how
    a "just this once" query or clock read sneaks in — never shows up in the
    module namespace (same technique as ``test_profile_is_core_clean_by_import``).
    """
    # Local import: reconcile is the module under construction; a module-level
    # import would fail collection for every other test in this file.
    import sluice.core.reconcile

    source_path = inspect.getsourcefile(sluice.core.reconcile)
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
    # (plan 06 unit 1): each name is a distinct policy with its own reason.
    forbidden_by_name = {
        "sqlite3": (
            "reconcile must not reach a SQL client — a module that cannot "
            "open a connection cannot do a hidden read or write; reconcile "
            "decides, the daemon persists (plan 06 fact 1, ADR-0002)"
        ),
        "sluice.core.db": (
            "reconcile must not reach the db primitives — it reads only the "
            "(run_row, profile) it is handed; eligibility lives in "
            "db.select_active_runs, not here (plan 06 facts 1 and 5)"
        ),
        "structlog": (
            "reconcile emits nothing — the wide event is the daemon's, fired "
            "after the persist commits (plan 06 §1; plan 03 §7 persist-then-emit)"
        ),
        "slack_sdk": (
            "core never imports a service SDK (ADR-0003 decision 3) — "
            "reconcile notifies no one"
        ),
        "time": (
            "no clock: a pure decision cannot depend on when it is asked "
            "(plan 06 fact 1 — state in, decision out)"
        ),
        "datetime": (
            "no clock: with the optional tier gone (ADR-0004) nothing is "
            "'due' and no timestamp enters the decision (plan 06 fact 1)"
        ),
        "random": (
            "no entropy: the same (run_row, profile) must always yield the "
            "same decision (plan 06 fact 1)"
        ),
        "os": (
            "no environment/filesystem read: reconcile's whole input is the "
            "(run_row, profile) arguments (plan 06 §3.4)"
        ),
        "socket": (
            "no network: a pure decision engine cannot reach another host "
            "(plan 06 fact 1)"
        ),
        "urllib": ("no network: same policy as socket (plan 06 fact 1)"),
        "http": ("no network: same policy as socket (plan 06 fact 1)"),
        "subprocess": (
            "no process spawning: reconcile picks WHICH gate; it never runs "
            "one (plan 06 §1 — gate.run is the daemon's call)"
        ),
        "sluice.gates": (
            "the dependency arrow is gates → core; core reconcile importing "
            "a gates module would reverse it (plan 06 §1)"
        ),
        "sluice.api": (
            "the dependency arrow is api → core; core reconcile importing "
            "api would reverse it (plan 06 §1)"
        ),
        "sluice.cli": (
            "the dependency arrow is cli → core; core reconcile importing "
            "cli would reverse it (plan 06 §1)"
        ),
    }
    for forbidden_module, reason in forbidden_by_name.items():
        matches = sorted(
            module
            for module in imported_modules
            if module == forbidden_module or module.startswith(f"{forbidden_module}.")
        )
        assert matches == [], f"{source_path} imports {matches}: {reason}"

    # The closed sluice-internal set (plan 06 §1): gate supplies the Gate
    # return type; profile supplies Applicability/required_gates — and
    # nothing else sluice-internal.
    allowed_sluice_imports = {"sluice.core.gate", "sluice.core.profile"}
    stray_sluice_imports = sorted(
        module
        for module in imported_modules
        if (module == "sluice" or module.startswith("sluice."))
        and module not in allowed_sluice_imports
    )
    assert stray_sluice_imports == [], (
        f"{source_path} imports {stray_sluice_imports} — reconcile's only "
        "allowed sluice-internal imports are sluice.core.gate and "
        "sluice.core.profile (plan 06 §1 closed set)"
    )

    # Everything else must be stdlib: no third-party dependency at all
    # (ADR-0003 — layering yes, but core stays lean).
    non_stdlib_roots = sorted(
        {module.split(".")[0] for module in imported_modules}
        - set(sys.stdlib_module_names)
        - {"sluice"}
    )
    assert non_stdlib_roots == [], (
        f"{source_path} imports non-stdlib modules: {non_stdlib_roots} — "
        "reconcile's allowed import set is stdlib + sluice.core.gate + "
        "sluice.core.profile only (plan 06 §1)"
    )


def test_is_complete_signature_is_row_and_profile_only():
    """``is_complete`` takes exactly ``(run_row, profile)`` — purity as a signature fact.

    No connection, no db path, no clock parameter (plan 06 fact 1): reconcile
    is a pure function of the state row and the resolved ``Profile`` the
    daemon hands it. The parameter list IS the input surface — a "helpful"
    extra parameter (a connection to re-check eligibility, a registry to
    re-resolve the profile, a now() for anything "due") is exactly the hidden
    read the design forbids, so editing this signature must be a conscious,
    tested purity change, never a drive-by.
    """
    # Local import: is_complete is the symbol under construction; a
    # module-level import would fail collection for every other test here.
    from sluice.core.reconcile import is_complete

    assert list(inspect.signature(is_complete).parameters) == ["run_row", "profile"]


@pytest.mark.parametrize(
    ("run_row", "expected_complete"),
    [
        pytest.param(
            {"mock_alpha_done": 0, "mock_beta_done": 0}, False, id="none-satisfied"
        ),
        pytest.param(
            {"mock_alpha_done": 1, "mock_beta_done": 0}, False, id="alpha-only"
        ),
        # Order-inconsistent (beta done before its need alpha) but state-legal:
        # is_complete is a total function of the columns — it judges, never
        # raises, on any {0,1} assignment.
        pytest.param(
            {"mock_alpha_done": 0, "mock_beta_done": 1},
            False,
            id="beta-only-order-inconsistent",
        ),
        pytest.param(
            {"mock_alpha_done": 1, "mock_beta_done": 1}, True, id="all-satisfied"
        ),
    ],
)
def test_is_complete_true_iff_all_contract_columns_truthy(run_row, expected_complete):
    """``is_complete`` is True iff EVERY contract gate's ``satisfied_column`` is truthy.

    The §7 / ``is_folder_completed`` reframing (plan 06 §2): complete means
    every gate of the run's own contract — here ``REFERENCE_PROFILE``, both
    gates in-contract — has a truthy column, nothing more and nothing less.
    Any single false column, in either position, yields False (a partial run
    is never complete), and only the all-truthy row yields True. This is the
    predicate the daemon's ``completed_at`` stamp hangs off, so a wrong
    truth table here becomes a falsely-completed or never-completed run.
    """
    from sluice.core.reconcile import is_complete

    assert is_complete(run_row, REFERENCE_PROFILE) is expected_complete


def test_is_complete_false_while_any_contract_column_is_false():
    """Even ONE unsatisfied contract column blocks completion — the C4 reconcile half.

    Errata C4 (central; ADR-0004 d.4): completion is never stamped over a gate
    that has not genuinely succeeded. python-poller stamps ``completed_at``
    unconditionally at the end (``poller.py:1614``, outside any Globus guard)
    while the NVD path already guards against exactly this
    (``poller.py:1127-1134``, the 2026-06-22 incident). Sluice's reconcile half
    of the fix: ``is_complete`` reads gate-satisfaction COLUMNS only, so a
    contract gate whose column is still ``0`` — here beta, unsatisfied while
    alpha is done — forces ``False``, and completion can never be *derived*
    over a not-genuinely-done contract gate. (The daemon half — a column is set
    truthy only on genuine SUCCESS, SKIPPED quarantines — is subsystem 9's.)
    An implementation completing on "any column truthy" or "the first contract
    gate truthy" fails here.
    """
    from sluice.core.reconcile import is_complete

    assert (
        is_complete({"mock_alpha_done": 1, "mock_beta_done": 0}, REFERENCE_PROFILE)
        is False
    )


def test_is_complete_ignores_disabled_gates():
    """A DISABLED gate is out of the contract — it never blocks completion.

    The excluded-never-blocks half of ADR-0004 d.2 / plan 06 fact 4 (the B1
    ``nvd_auto_run=false`` loop-forever fix): under
    ``REFERENCE_PROFILE_ALPHA_DISABLED`` alpha is excluded from the contract,
    so its column staying ``0`` (a disabled gate's column is NEVER set — it is
    satisfied by absence, not by a fabricated write) must not block: with beta
    satisfied the run IS complete. An implementation that counts every
    ``profile.gates`` column instead of the contract (``required_gates``)
    leaves such a run incomplete forever — exactly PP's ``poller.py:1229-1237``
    bug. The second row proves "ignores" is not "ignores everything": beta,
    the contract, still gates completion.
    """
    from sluice.core.reconcile import is_complete

    # Alpha excluded, column 0; beta (the whole contract) satisfied → complete.
    assert (
        is_complete(
            {"mock_alpha_done": 0, "mock_beta_done": 1},
            REFERENCE_PROFILE_ALPHA_DISABLED,
        )
        is True
    )
    # Beta — the contract — unsatisfied → not complete (alpha still ignored).
    assert (
        is_complete(
            {"mock_alpha_done": 0, "mock_beta_done": 0},
            REFERENCE_PROFILE_ALPHA_DISABLED,
        )
        is False
    )


def test_is_complete_is_vacuously_true_for_an_all_disabled_profile():
    """An all-disabled profile has an empty contract — complete at detection.

    The vacuous-completion edge (plan 05 unit 4, carried to reconcile by plan
    06 §2 B1): ``required_gates`` of an all-DISABLED profile is ``()``, so
    ``is_complete`` must be ``all([]) == True`` — an empty contract is complete
    the moment the run is detected, with every column still ``0`` and nothing
    ever run. This is a real, legitimate state the daemon stamps
    ``completed_at`` on, not an error; a "defensive" implementation that
    treats an empty contract as incomplete would recreate PP's
    ``nvd_auto_run=false`` loop-forever bug (poller.py:1229-1237) in its purest
    form — a run no gate can ever advance and no predicate will ever complete.
    """
    from sluice.core.reconcile import is_complete

    all_disabled_profile = make_profile(
        [
            (make_gate("vac_alpha", 1), Applicability.DISABLED),
            (make_gate("vac_beta", 2), Applicability.DISABLED),
        ]
    )
    assert (
        is_complete({"vac_alpha_done": 0, "vac_beta_done": 0}, all_disabled_profile)
        is True
    )


def test_is_complete_reads_only_contract_gate_columns_and_ignores_completed_at():
    """``is_complete`` reads ONLY the profile's gate columns; ``completed_at`` is never an input.

    Two halves of plan 06 §3.4 / §5 in one behavior. (1) A minimal row carrying
    nothing but the two gate columns — no ``lifecycle``, ``errored_at``,
    ``completed_at``, or ``pipeline_version`` — must decide cleanly, proving
    there is no hidden read of any other run column (a hidden read is a defect,
    fact 1). (2) ``completed_at`` is a derived cache, never an input: a row with
    a contract column cleared (the ``reset_gate``-after-complete scenario) but a
    stale ``completed_at`` still set must re-derive as NOT complete, so the run
    correctly re-enters the pipeline. This is why ``db.select_active_runs``
    deliberately omits ``completed_at IS NULL`` — reconcile owns completion,
    re-derived from state every look. An implementation that short-circuits on a
    present-and-truthy ``completed_at`` fails the second assertion.
    """
    from sluice.core.reconcile import is_complete

    # (1) Minimal row: only the gate columns exist — decides True, no hidden read.
    assert (
        is_complete({"mock_alpha_done": 1, "mock_beta_done": 1}, REFERENCE_PROFILE)
        is True
    )
    # (2) Stale cache: beta's column cleared after completion, completed_at not
    # cleared — completion is re-derived from the columns, so NOT complete.
    assert (
        is_complete(
            {
                "mock_alpha_done": 1,
                "mock_beta_done": 0,
                "completed_at": "2026-07-27T00:00:00+00:00",
            },
            REFERENCE_PROFILE,
        )
        is False
    )


def _as_sqlite3_row(column_values) -> sqlite3.Row:
    """Build a real ``sqlite3.Row`` carrying exactly ``column_values`` — no schema needed.

    A one-row SELECT of aliased literals gives the genuine ``sqlite3.Row`` type
    (value-membership ``in``, ``IndexError`` on a missing column — the §3.1
    traps) without any table, so fail-loud tests can exercise the row type the
    daemon actually hands reconcile. Values are test-controlled small ints, so
    inlining them as SQL literals is safe here.
    """
    connection = sqlite3.connect(":memory:")
    try:
        connection.row_factory = sqlite3.Row
        select_list = ", ".join(
            f"{value} AS {name}" for name, value in column_values.items()
        )
        return connection.execute(f"SELECT {select_list}").fetchone()
    finally:
        connection.close()


@pytest.mark.parametrize("row_kind", ["dict", "sqlite3.Row"])
@pytest.mark.parametrize(
    ("profile_entries", "present_column_values"),
    [
        pytest.param(
            [
                (
                    make_gate("ghost", 1, satisfied_column="ghost_done"),
                    Applicability.REQUIRED,
                )
            ],
            {"unrelated_column": 1},
            id="single-contract-gate",
        ),
        # The short-circuit trap (plan 06 §3.2 option (a)): R1 is present and
        # unsatisfied (0), R2's column is absent. A naive
        # ``all(row[col] ...)`` returns False after R1 and never reads R2, so
        # the drift would pass silently as "not complete" — only an up-front
        # presence pre-check makes this case raise.
        pytest.param(
            [
                (make_gate("r1", 1), Applicability.REQUIRED),
                (
                    make_gate("r2", 2, satisfied_column="ghost_done"),
                    Applicability.REQUIRED,
                ),
            ],
            {"r1_done": 0},
            id="short-circuit-trap",
        ),
    ],
)
def test_is_complete_fails_loud_on_a_missing_gate_column(
    row_kind, profile_entries, present_column_values
):
    """A profile-gate column absent from the row raises ``KeyError`` naming it — never a silent "not complete".

    The fail-loud boundary (plan 06 §3.2 option (a); guardrail §8): a
    registry/schema drift — a profile naming a ``satisfied_column`` the row
    does not carry — must raise at the decision boundary, not be read as
    "unsatisfied". Two implementation shortcuts would each pass a weaker test:
    (1) deferring detection to the column read lets ``all()`` short-circuit
    past the drifted column whenever an earlier contract gate is unsatisfied
    (the ``short-circuit-trap`` case); (2) a naive ``row[col]`` /
    ``col in row`` spelling type-checks on ``sqlite3.Row`` but raises
    ``IndexError`` (or tests VALUE membership) instead of the uniform
    ``KeyError`` a caller can catch (the §3.1 trap, the ``sqlite3.Row``
    cases). Every (row type × profile shape) cell must raise ``KeyError``
    naming ``ghost_done``.
    """
    from sluice.core.reconcile import is_complete

    profile = make_profile(profile_entries)
    run_row = (
        dict(present_column_values)
        if row_kind == "dict"
        else _as_sqlite3_row(present_column_values)
    )

    with pytest.raises(KeyError) as raised:
        is_complete(run_row, profile)
    assert "ghost_done" in str(raised.value)


def test_next_gate_signature_is_row_and_profile_only():
    """``next_gate`` takes exactly ``(run_row, profile)`` — purity as a signature fact.

    Plan 06 fact 1 and the §2 pointer decision (⚑3, decided 2026-07-27): the
    selection engine is a pure function of the state row and the resolved
    ``Profile`` — no connection, no db path, no clock, and expressly NO
    ``run_from_step``/``run_from_fresh`` pointer parameter. The parameter list
    IS the input surface: any biasing of the selection (or a "helpful"
    eligibility/registry/now() argument) must arrive as a conscious, tested
    signature change, never a quiet extra parameter.
    """
    # Local import: next_gate is the symbol under construction; a module-level
    # import would fail collection for every other test in this file.
    from sluice.core.reconcile import next_gate

    assert list(inspect.signature(next_gate).parameters) == ["run_row", "profile"]


def test_next_gate_returns_lowest_order_runnable_contract_gate():
    """With nothing satisfied, ``next_gate`` returns the lowest-``order`` runnable contract gate.

    The selection rule (design §7 read through ADR-0004; plan 06 fact 2):
    the first contract gate whose needs are satisfied and whose
    ``satisfied_column`` is false, lowest ``Gate.order`` first —
    ``Gate.order`` is the sole ordering authority (errata B3). Under
    ``REFERENCE_PROFILE`` with both columns ``0``, alpha (order 1, no needs)
    is the contract's next task; beta's column is ALSO false, yet beta is not
    returned — its need on alpha is unmet. The two assertions together pin
    need-blocking and lowest-order-first: an implementation that ignores
    ``needs``, or returns any/the-last unsatisfied gate, hands the daemon a
    gate whose inputs do not exist yet.
    """
    from sluice.core.mock_gates import MOCK_ALPHA, MOCK_BETA
    from sluice.core.reconcile import next_gate

    selected = next_gate({"mock_alpha_done": 0, "mock_beta_done": 0}, REFERENCE_PROFILE)
    assert selected is MOCK_ALPHA
    # Beta's column is also false, but it must not be selected: its need
    # (mock_alpha) is unsatisfied, and its order is higher.
    assert selected is not MOCK_BETA


def test_next_gate_advances_when_the_predecessor_is_satisfied():
    """Once alpha's column is truthy, ``next_gate`` advances to beta — held by STATE, not position.

    The advancing half of the selection rule (plan 06 §3.2 steps (b)/(c)):
    under ``REFERENCE_PROFILE`` with ``{alpha 1, beta 0}``, alpha is skipped
    (already satisfied) and beta's need on alpha is now met, so beta is
    returned. Combined with
    ``test_next_gate_returns_lowest_order_runnable_contract_gate`` (same
    profile, all-zero row → alpha), this proves beta was previously held BY
    STATE, not by list position: the identical profile yields a different gate
    purely because the row changed. An implementation that never re-reads the
    satisfied column (returning alpha forever) or reads needs from anything
    but the row fails here — that regression is the loop livelocking on its
    first gate.
    """
    from sluice.core.mock_gates import MOCK_BETA
    from sluice.core.reconcile import next_gate

    assert (
        next_gate({"mock_alpha_done": 1, "mock_beta_done": 0}, REFERENCE_PROFILE)
        is MOCK_BETA
    )


def test_next_gate_returns_none_when_all_contract_gates_satisfied():
    """With every contract column truthy, ``next_gate`` returns ``None`` — the completion signal.

    ``None`` is the one signal the daemon stamps ``completed_at`` on (plan 06
    §3.2 / §7 carry-forward): under ``REFERENCE_PROFILE`` with
    ``{alpha 1, beta 1}`` there is no runnable contract gate left, so the
    selection loop must exhaust and say so. An implementation that keeps
    handing back an already-satisfied gate (livelock — the loop re-runs a done
    task forever) or returns some sentinel gate instead of ``None`` breaks the
    ``next_gate is None ⟺ is_complete`` contract the daemon's completion
    stamp hangs off (the §3.3 theorem, pinned exhaustively in unit 3).
    """
    from sluice.core.reconcile import next_gate

    assert (
        next_gate({"mock_alpha_done": 1, "mock_beta_done": 1}, REFERENCE_PROFILE)
        is None
    )


def test_next_gate_selection_identity_lowest_wins_and_need_blocked_not_returned():
    """Among MULTIPLE runnable contract gates, the lowest-``order`` one is returned.

    The selection-*identity* test the None-only theorem cannot catch (plan 06
    unit 2): a wrong-but-non-``None`` choice satisfies ``next_gate is None ⟺
    is_complete`` perfectly, so *which* gate comes back must be pinned
    directly. A 3-gate contract, all REQUIRED — A(order 1, needs=()),
    B(order 2, needs=(A,)), C(order 3, needs=()) — puts TWO gates in the
    runnable set at once, which ``REFERENCE_PROFILE``'s linear chain never
    does: (1) on the all-zero row A and C are both runnable (empty needs,
    false columns) and A must win by order — an implementation returning any
    runnable gate, or the last one (e.g. iterating reversed), passes every
    linear-chain test in this file and fails only here; (2) once A's column
    is truthy, B's need clears and B (order 2) must beat the still-runnable
    C (order 3) — the need-blocked gate from sub-case 1 re-enters the
    selection at its rightful position the moment its need clears
    (``Gate.order`` is the sole ordering authority, errata B3).
    """
    from sluice.core.reconcile import next_gate

    gate_a = make_gate("sel_a", 1)
    gate_b = make_gate("sel_b", 2, needs=("sel_a",))
    gate_c = make_gate("sel_c", 3)
    profile = make_profile(
        [
            (gate_a, Applicability.REQUIRED),
            (gate_b, Applicability.REQUIRED),
            (gate_c, Applicability.REQUIRED),
        ]
    )

    # (1) All zero: A and C are BOTH runnable; the lowest order wins.
    selected_from_scratch = next_gate(
        {"sel_a_done": 0, "sel_b_done": 0, "sel_c_done": 0}, profile
    )
    assert selected_from_scratch is gate_a
    assert selected_from_scratch is not gate_c

    # (2) A satisfied: B's need clears, and B (order 2) beats the
    # also-runnable C (order 3).
    selected_after_need_clears = next_gate(
        {"sel_a_done": 1, "sel_b_done": 0, "sel_c_done": 0}, profile
    )
    assert selected_after_need_clears is gate_b
    assert selected_after_need_clears is not gate_c


def test_next_gate_treats_a_disabled_gate_as_out_of_the_contract():
    """A DISABLED gate is never selected, and it satisfies its dependents' needs by absence.

    The satisfied-by-absence test (plan 06 fact 4; ADR-0004 d.2; the plan 05
    ⚑2 exemplar profile) — the selection half of the fix for PP's
    ``nvd_auto_run=false`` loop-forever bug (``poller.py:1229-1237``). Under
    ``REFERENCE_PROFILE_ALPHA_DISABLED`` on the all-zero row, alpha's column is
    false yet alpha must NEVER be selected (step (a): out of the contract), and
    beta — whose ``needs=("mock_alpha",)`` — must be returned even though
    alpha's column stays ``0``: the need is satisfied from APPLICABILITY, not
    the column (§3.3 disabled-need arm). This is the test that catches a
    wrong, column-only needs check, which would block every dependent of an
    excluded gate forever — ``next_gate`` returning ``None`` on an incomplete
    run, the deadlock ADR-0004 d.2 exists to prevent. The second row proves
    exclusion holds to the end: with beta (the whole contract) satisfied,
    ``next_gate`` is ``None`` — alpha is not selected even as the last
    column-false gate standing.
    """
    from sluice.core.mock_gates import MOCK_BETA
    from sluice.core.reconcile import next_gate

    # All-zero row: alpha (column 0) is excluded, never selected; beta is
    # unblocked by alpha's ABSENCE though alpha's column is still 0.
    assert (
        next_gate(
            {"mock_alpha_done": 0, "mock_beta_done": 0},
            REFERENCE_PROFILE_ALPHA_DISABLED,
        )
        is MOCK_BETA
    )
    # Contract satisfied: beta done → None, with alpha's column STILL 0 —
    # the excluded gate is never selected, even as the only column-false gate.
    assert (
        next_gate(
            {"mock_alpha_done": 0, "mock_beta_done": 1},
            REFERENCE_PROFILE_ALPHA_DISABLED,
        )
        is None
    )


def test_next_gate_is_none_for_an_all_disabled_profile():
    """An all-disabled profile has an empty contract — ``next_gate`` is ``None`` at detection.

    The selection half of the vacuous-completion edge (plan 06 unit 2; §2 B1;
    ADR-0004 d.2), matching unit 1's
    ``test_is_complete_is_vacuously_true_for_an_all_disabled_profile``: with
    every gate DISABLED there is nothing in the contract to run, so on the
    all-zero row — every column false, nothing ever executed — ``next_gate``
    must exhaust and return ``None``, keeping the ``next_gate is None ⟺
    is_complete`` theorem true on the empty contract. This is a legitimate
    state the daemon stamps ``completed_at`` on, not an error. The regression
    it catches is distinct from the mixed disabled-variant test above: a
    "defensive" implementation that special-cases an empty contract as
    "everything still pending" (returning a column-false excluded gate, or
    raising) would recreate PP's ``nvd_auto_run=false`` loop-forever bug
    (``poller.py:1229-1237``) in its purest form — a run no gate can ever
    advance and no signal will ever complete.
    """
    from sluice.core.reconcile import next_gate

    all_disabled_profile = make_profile(
        [
            (make_gate("vac_alpha", 1), Applicability.DISABLED),
            (make_gate("vac_beta", 2), Applicability.DISABLED),
        ]
    )
    assert (
        next_gate({"vac_alpha_done": 0, "vac_beta_done": 0}, all_disabled_profile)
        is None
    )


def test_next_gate_reads_only_the_profile_gate_columns():
    """``next_gate`` decides off a row carrying ONLY the gate columns — no hidden read of any other run column.

    The purity/scope guard (plan 06 §3.4; fact 1): everything ``next_gate``
    reads from the row is the ``satisfied_column`` of each gate in
    ``profile.gates`` — never ``lifecycle``, ``errored_at``, ``completed_at``,
    ``pipeline_version``, ``run_from_step``/``run_from_fresh``, or any other
    run column. The row here carries ``REFERENCE_PROFILE``'s two gate columns
    and NOTHING else, so any hidden read — a "helpful" eligibility re-check
    creeping in (fact 5), a ``completed_at`` cache peek (§5), a quiet
    ``run_from_step`` bias (the §2 pointer decision) — hits an absent key and
    raises instead of returning alpha. "A hidden read is a defect" (fact 1)
    is thereby a behavioral fact about the row surface, complementing the
    structural import test: an import-clean implementation can still overreach
    into the row it was handed, and only a minimal row catches that.
    """
    from sluice.core.mock_gates import MOCK_ALPHA
    from sluice.core.reconcile import next_gate

    minimal_row = {"mock_alpha_done": 0, "mock_beta_done": 0}
    assert next_gate(minimal_row, REFERENCE_PROFILE) is MOCK_ALPHA


def test_next_gate_ignores_lifecycle_and_error_state():
    """``next_gate`` decides identically on a held/errored/completed-marked row — eligibility is NOT re-implemented.

    Fact 5 (plan 06 §2): the eligibility predicate (``lifecycle='active' AND
    errored_at IS NULL``) lives in ``db.select_active_runs``
    (``ACTIVE_RUNS_WHERE``) and ONLY there — the daemon filters, then calls
    reconcile solely on eligible runs. So a row carrying ``lifecycle="held"``,
    a set ``errored_at``/``error_reason``, and a set ``completed_at`` must
    yield the SAME decision as the active row: ``MOCK_ALPHA``. The regression
    this catches is the "helpful" eligibility check creeping into the pure
    decision — a ``next_gate`` that peeks at ``lifecycle``/``errored_at`` and
    returns ``None`` (or a special value) would silently duplicate the
    daemon's filter, split the eligibility policy across two modules, and
    break the ``next_gate is None ⟺ is_complete`` theorem (``None`` while a
    contract column is false). Complements
    ``test_next_gate_reads_only_the_profile_gate_columns``: that minimal row
    proves absent keys are never read; this over-full row proves PRESENT
    eligibility keys are never consulted either.
    """
    from sluice.core.mock_gates import MOCK_ALPHA
    from sluice.core.reconcile import next_gate

    held_and_errored_row = {
        "mock_alpha_done": 0,
        "mock_beta_done": 0,
        "lifecycle": "held",
        "errored_at": "2026-07-27T00:00:00+00:00",
        "error_reason": "x",
        "completed_at": "2026-07-27T00:00:00+00:00",
    }
    active_row = {"mock_alpha_done": 0, "mock_beta_done": 0}
    # Identical decision to the active row: reconcile never reads
    # lifecycle/errored_at/completed_at (§3.4); the daemon owns eligibility.
    assert next_gate(held_and_errored_row, REFERENCE_PROFILE) is MOCK_ALPHA
    assert next_gate(held_and_errored_row, REFERENCE_PROFILE) is next_gate(
        active_row, REFERENCE_PROFILE
    )


@pytest.mark.parametrize("row_kind", ["dict", "sqlite3.Row"])
@pytest.mark.parametrize(
    ("profile_entries", "present_column_values"),
    [
        pytest.param(
            [
                (
                    make_gate("ghost", 1, satisfied_column="ghost_done"),
                    Applicability.REQUIRED,
                )
            ],
            {"unrelated_column": 1},
            id="single-contract-gate",
        ),
        # The short-circuit trap, next_gate edition (plan 06 §3.2 option (a)):
        # R1 is present and unsatisfied (0), R2's column is absent. A lazy
        # implementation reads r1_done, finds a runnable gate, and returns R1
        # — a plausible-looking decision that silently masks the drift on R2
        # — only an up-front presence pre-check makes this case raise.
        pytest.param(
            [
                (make_gate("r1", 1), Applicability.REQUIRED),
                (
                    make_gate("r2", 2, satisfied_column="ghost_done"),
                    Applicability.REQUIRED,
                ),
            ],
            {"r1_done": 0},
            id="short-circuit-trap",
        ),
        # The need-column drift (plan 06 unit 2): the absent column belongs to
        # a NEED of a selectable gate. The need gate is DISABLED — the only
        # shape in which its dependent is genuinely selectable (need satisfied
        # by absence, §3.3) while the need's column is missing — so a lazy
        # implementation never reads ghost_done on ANY path (step (a) skips
        # the disabled gate; the absence arm decides the need) and happily
        # returns the dependent. The pre-check covers every profile gate's
        # column — selection column and need column, contract or excluded —
        # in one place, so the drift raises anyway.
        pytest.param(
            [
                (
                    make_gate("drifted_need", 1, satisfied_column="ghost_done"),
                    Applicability.DISABLED,
                ),
                (
                    make_gate("dep", 2, needs=("drifted_need",)),
                    Applicability.REQUIRED,
                ),
            ],
            {"dep_done": 0},
            id="need-column-drifted",
        ),
    ],
)
def test_next_gate_fails_loud_on_a_missing_gate_column(
    row_kind, profile_entries, present_column_values
):
    """A profile-gate column absent from the row raises ``KeyError`` naming it — never a silent selection.

    ``next_gate``'s half of the fail-loud boundary (plan 06 §3.2 option (a);
    guardrail §8), covering every read path — selection column AND need column
    — through the one up-front pre-check. Three lazy shortcuts would each pass
    a weaker test: (1) deferring detection to the column read returns R1 as a
    plausible next gate in the ``short-circuit-trap`` case, silently masking
    the drift on R2 behind a valid-looking decision; (2) in the
    ``need-column-drifted`` case even a CORRECT lazy needs check never reads
    the excluded need's column at all (satisfied by absence), so only a
    pre-check spanning every profile gate — contract or excluded — raises;
    (3) a naive ``row[col]`` / ``col in row`` spelling type-checks on
    ``sqlite3.Row`` but raises ``IndexError`` (or tests VALUE membership)
    instead of the uniform ``KeyError`` a caller can catch (the §3.1 trap).
    Every (row type × profile shape) cell must raise ``KeyError`` naming
    ``ghost_done``.
    """
    from sluice.core.reconcile import next_gate

    profile = make_profile(profile_entries)
    run_row = (
        dict(present_column_values)
        if row_kind == "dict"
        else _as_sqlite3_row(present_column_values)
    )

    with pytest.raises(KeyError) as raised:
        next_gate(run_row, profile)
    assert "ghost_done" in str(raised.value)


def test_reconcile_exports_exactly_the_documented_surface():
    """``reconcile.__all__`` is exactly ``{"is_complete", "next_gate"}`` — the scope guard.

    The db unit-7 precedent (plan 06 §1/unit 3): set EQUALITY, not subset, so
    the documented surface can neither shrink (a consumer's import breaks
    silently) nor grow (a "helpful" extra export — the pre-check helper, a
    selection-biasing variant, an eligibility shim — becomes de-facto public
    without a conscious edit here). Subsystems 8 and 9 build against exactly
    these two names (§7 carry-forwards); anything else in the module is
    private by declaration.
    """
    # Local import: file convention — a module-level import of the module
    # under construction would fail collection for every other test here.
    import sluice.core.reconcile

    assert set(sluice.core.reconcile.__all__) == {"is_complete", "next_gate"}


# The disabled-middle-gate DAG (plan 06 unit 3): Z's needs name BOTH a
# satisfied-by-column gate (A) and an excluded gate (M) mid-DAG, so the theorem
# sweep exercises the absence-satisfaction arm in composition with a real
# column need — the shape the shipped two-gate profiles cannot produce.
_THEOREM_DISABLED_MIDDLE_PROFILE = make_profile(
    [
        (make_gate("thm_a", 1), Applicability.REQUIRED),
        (make_gate("thm_m", 2), Applicability.DISABLED),
        (make_gate("thm_z", 3, needs=("thm_a", "thm_m")), Applicability.REQUIRED),
    ]
)

# The empty-contract edge: every gate excluded, so the theorem must hold as
# "None ⟺ vacuously complete" on every (all four) column assignments.
_THEOREM_ALL_DISABLED_PROFILE = make_profile(
    [
        (make_gate("thm_da", 1), Applicability.DISABLED),
        (make_gate("thm_db", 2), Applicability.DISABLED),
    ]
)


def _theorem_state_space_cases():
    """Return ``(profile, run_row)`` params covering EVERY ``{0,1}^n`` column state.

    Exhaustive by construction (plan 06 unit 3): for each contract shape, every
    possible assignment of its gates' columns — including order-inconsistent
    rows a healthy daemon would never write (a dependent satisfied before its
    need) — because reconcile must be a total function of state, robust to
    operator writes (``set_gate``/``reset_gate``) that can produce any row.
    """
    profiles = [
        ("reference", REFERENCE_PROFILE),
        ("alpha-disabled", REFERENCE_PROFILE_ALPHA_DISABLED),
        ("disabled-middle", _THEOREM_DISABLED_MIDDLE_PROFILE),
        ("all-disabled", _THEOREM_ALL_DISABLED_PROFILE),
    ]
    cases = []
    for label, profile in profiles:
        columns = [gate.satisfied_column for gate in profile.gates]
        for bits in itertools.product((0, 1), repeat=len(columns)):
            run_row = dict(zip(columns, bits))
            bits_id = "".join(str(bit) for bit in bits)
            cases.append(pytest.param(profile, run_row, id=f"{label}-{bits_id}"))
    return cases


@pytest.mark.parametrize(("profile", "run_row"), _theorem_state_space_cases())
def test_next_gate_none_iff_is_complete_across_the_state_space(profile, run_row):
    """The central theorem (§3.3): ``next_gate(row, p) is None ⟺ is_complete(row, p)`` — exhaustively.

    The equivalence the daemon's completion stamp hangs off (``if gate is
    None: mark complete``), verified as a PROPERTY over every ``{0,1}^n``
    column state of four contract shapes rather than trusted as an implicit
    coupling of two functions. Each direction is a distinct disaster if it
    breaks: ``None`` while incomplete is a FALSE COMPLETE or a DEADLOCK —
    e.g. a column-only needs check blocking every dependent of an excluded
    gate (the disabled-middle DAG and alpha-disabled shapes catch exactly
    that, ADR-0004 d.2), or an "empty contract is pending" special case (the
    all-disabled shape); non-``None`` while complete is a LIVELOCK — the loop
    re-running a done contract forever. Together with C4's reconcile half
    (never ``None`` while a contract column is false) this is plan 06's
    no-false-complete/no-deadlock guarantee, on order-inconsistent rows too.
    It pins only None-ness; WHICH non-``None`` gate comes back is the unit-2
    identity tests' job — a wrong-but-non-``None`` choice passes here by
    design.
    """
    from sluice.core.reconcile import is_complete, next_gate

    assert (next_gate(run_row, profile) is None) == is_complete(run_row, profile)


def test_reconcile_drives_the_reference_profile_to_completion_off_real_rows(tmp_path):
    """The pure decisions drive a REAL ``runs`` row through ``reference-v1`` to completion.

    The load-bearing seam (plan 06 unit 3): every prior test hands reconcile a
    plain ``dict``, so a drift between reconcile's column reads and the real
    schema (migration 005's ``mock_alpha_done``/``mock_beta_done``), or between
    ``runs.pipeline_version`` and the shipped registry keys, would survive the
    whole file and surface in the daemon at 2am. This test plays the daemon's
    loop verbatim off real ``sqlite3.Row``s: resolve the profile from the
    STAMPED version (the freeze — never the current default, fact 3), ask
    ``next_gate``, persist the daemon's ``satisfied_column = 1`` via
    ``update_run``, re-read, repeat — alpha then beta by identity, then the
    ``None``/``is_complete`` completion signal. The test imports
    ``sluice.core.db``; the module under test still cannot (unit 1's
    import-hygiene test) — reconcile stays pure while the STATE is real.
    """
    import contextlib

    # Local imports: this test alone may import sluice.core.db — the module
    # under test must not (unit 1's import-hygiene test enforces that).
    import sluice.core.db
    from sluice.core.mock_gates import MOCK_ALPHA, MOCK_BETA
    from sluice.core.profile import REFERENCE_PROFILES, load_profile
    from sluice.core.reconcile import is_complete, next_gate

    db_path = tmp_path / "state.db"
    sluice.core.db.initialize_database(db_path)

    with contextlib.closing(sluice.core.db.get_connection(db_path)) as connection:
        sluice.core.db.insert_run(connection, "r1", pipeline_version="reference-v1")

        # The daemon's resolution: the profile comes from the version STAMPED
        # into the row at detection, off a real sqlite3.Row — identity, because
        # the run must decide under THE shipped definition (fact 3).
        profile = load_profile(
            sluice.core.db.get_run(connection, "r1")["pipeline_version"],
            REFERENCE_PROFILES,
        )
        assert profile is REFERENCE_PROFILE

        # Fresh row: both columns default 0 (migration 005) → alpha first.
        assert (
            next_gate(sluice.core.db.get_run(connection, "r1"), profile) is MOCK_ALPHA
        )

        # Daemon persists alpha's SUCCESS; the re-read row advances to beta.
        sluice.core.db.update_run(connection, "r1", {"mock_alpha_done": 1})
        assert next_gate(sluice.core.db.get_run(connection, "r1"), profile) is MOCK_BETA

        # Daemon persists beta's SUCCESS; the contract is exhausted — the
        # None signal and the completion predicate agree on the real row.
        sluice.core.db.update_run(connection, "r1", {"mock_beta_done": 1})
        completed_row = sluice.core.db.get_run(connection, "r1")
        assert next_gate(completed_row, profile) is None
        assert is_complete(completed_row, profile) is True


def test_reconcile_disabled_variant_completes_without_running_the_excluded_gate(
    tmp_path,
):
    """A run on the shipped disabled variant completes with the excluded gate never run and its column still ``0``.

    The full-loop satisfied-by-absence proof off a shipped profile (plan 06
    unit 3; ADR-0004 d.2; the B1 ``nvd_auto_run=false`` loop-forever fix,
    PP ``poller.py:1229-1237``): under ``reference-v1-alpha-disabled`` —
    resolved from the STAMPED version off a real row — alpha is out of the
    contract, so ``next_gate`` selects beta immediately (unblocked by alpha's
    ABSENCE, its column untouched at the schema default ``0``), one persist
    completes the run, and the final row still carries ``mock_alpha_done = 0``:
    completion derived with the excluded gate never executed and never
    fabricated-satisfied. This is also the pure-decision half of
    remediation-is-exclusion (ADR-0004 d.3): a run whose contract no longer
    contains a step resumes past it with no daemon restart and no
    special-casing — the subsystem-8 exclusion op relies on exactly this.
    """
    import contextlib

    # Local imports: this test alone may import sluice.core.db — the module
    # under test must not (unit 1's import-hygiene test enforces that).
    import sluice.core.db
    from sluice.core.mock_gates import MOCK_BETA
    from sluice.core.profile import REFERENCE_PROFILES, load_profile
    from sluice.core.reconcile import is_complete, next_gate

    db_path = tmp_path / "state.db"
    sluice.core.db.initialize_database(db_path)

    with contextlib.closing(sluice.core.db.get_connection(db_path)) as connection:
        sluice.core.db.insert_run(
            connection, "r2", pipeline_version="reference-v1-alpha-disabled"
        )
        profile = load_profile(
            sluice.core.db.get_run(connection, "r2")["pipeline_version"],
            REFERENCE_PROFILES,
        )
        assert profile is REFERENCE_PROFILE_ALPHA_DISABLED

        # Fresh row, both columns 0: alpha is excluded — NEVER selected — and
        # beta is unblocked by absence, so the contract's first task is beta.
        assert next_gate(sluice.core.db.get_run(connection, "r2"), profile) is MOCK_BETA

        # One persist completes the one-gate contract.
        sluice.core.db.update_run(connection, "r2", {"mock_beta_done": 1})
        final_row = sluice.core.db.get_run(connection, "r2")
        assert next_gate(final_row, profile) is None
        assert is_complete(final_row, profile) is True
        # The load-bearing detail: the excluded gate's column is STILL 0 on
        # the real completed row — satisfied by absence, not by a write.
        assert final_row["mock_alpha_done"] == 0


def test_reconcile_re_derives_completion_ignoring_a_stale_completed_at(tmp_path):
    """A contract column cleared after completion re-derives as NOT complete despite a stale ``completed_at``.

    The C4-adjacent reset scenario (plan 06 §2/§5): ``completed_at`` is a
    derived CACHE, never an input — reconcile re-derives completion from the
    contract columns on every look. A real run is driven to completion and
    stamped (as the daemon would), then ``mock_alpha_done`` is cleared
    (simulating subsystem 7's ``reset_gate``) WITHOUT clearing the stamp —
    the stale-cache state ``select_active_runs`` deliberately still returns
    (its WHERE omits ``completed_at IS NULL`` for exactly this reason). The
    re-read row must decide NOT complete with alpha selected again, so the run
    re-enters the pipeline at the reset gate. An implementation that
    short-circuits on the present-and-truthy ``completed_at`` column would
    return complete/``None`` here and strand the reset forever.
    """
    import contextlib

    # Local imports: this test alone may import sluice.core.db — the module
    # under test must not (unit 1's import-hygiene test enforces that).
    import sluice.core.db
    from sluice.core.mock_gates import MOCK_ALPHA
    from sluice.core.profile import REFERENCE_PROFILES, load_profile
    from sluice.core.reconcile import is_complete, next_gate

    db_path = tmp_path / "state.db"
    sluice.core.db.initialize_database(db_path)

    with contextlib.closing(sluice.core.db.get_connection(db_path)) as connection:
        sluice.core.db.insert_run(connection, "r1", pipeline_version="reference-v1")
        profile = load_profile(
            sluice.core.db.get_run(connection, "r1")["pipeline_version"],
            REFERENCE_PROFILES,
        )

        # Drive to completion and stamp completed_at, as the daemon would on
        # the None signal (persist-then-emit; the value's shape is now_iso's).
        sluice.core.db.update_run(
            connection,
            "r1",
            {
                "mock_alpha_done": 1,
                "mock_beta_done": 1,
                "completed_at": "2026-07-27T00:00:00+00:00",
            },
        )
        completed_row = sluice.core.db.get_run(connection, "r1")
        assert is_complete(completed_row, profile) is True
        assert next_gate(completed_row, profile) is None

        # reset_gate's effect: clear the contract column, completed_at NOT
        # cleared — the cache is now stale against the columns.
        sluice.core.db.update_run(connection, "r1", {"mock_alpha_done": 0})
        stale_row = sluice.core.db.get_run(connection, "r1")
        # Precondition, so a passing test cannot be hiding a cleared cache.
        assert stale_row["completed_at"] is not None

        # Re-derived from columns, the stale stamp ignored: not complete, and
        # the reset gate is selected again — the run re-enters the pipeline.
        assert is_complete(stale_row, profile) is False
        assert next_gate(stale_row, profile) is MOCK_ALPHA
