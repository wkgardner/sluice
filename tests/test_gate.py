"""Behavioral tests for ``sluice.core.gate`` — the gate contract.

The Gate record is the narrowly-defined task object the whole system hangs off
(design of record §4), so its vocabulary and field lists are pinned by equality:
adding or renaming a member is a visible contract change, never a quiet drift.
"""

import ast
import dataclasses
import inspect
import pathlib
import sys
import types

import pytest

import sluice.core.gate
from sluice.core.gate import Gate, GateOutcome, GateResult


def make_gate(**overrides) -> Gate:
    """One-line factory for ad-hoc ``Gate`` construction (plan 02 §4).

    Defaults are the smallest honest gate — a no-op ``run`` returning SUCCESS,
    a no-op ``invalidate``, empty ``needs``/``produces`` — so each test names
    only the fields its behavior is about.
    """
    fields = dict(
        id="gate_under_test",
        name="Gate under test",
        satisfied_column="gate_under_test_done",
        order=1,
        needs=(),
        produces=(),
        run=lambda context: GateResult(GateOutcome.SUCCESS),
        invalidate=lambda context: None,
    )
    fields.update(overrides)
    return Gate(**fields)


def test_gate_outcome_serializes_to_the_spec_strings():
    """The outcome vocabulary is exactly the four spec strings (design §4).

    Equality, not subset: the values are the persisted/logged vocabulary, and
    the *closed* set is the surface of the B4 resolution (ADR-0002: every
    failure quarantines) — a creeping ``TRANSIENT`` or ``RETRY`` member would
    reopen retry policy without an ADR, so it must fail here.
    """
    assert {outcome.name: outcome.value for outcome in GateOutcome} == {
        "SUCCESS": "success",
        "FAILURE": "failure",
        "HELD": "held",
        "SKIPPED": "skipped",
    }


def test_gate_imports_only_stdlib():
    """``sluice/core/gate.py`` imports nothing outside the standard library.

    Guards the ``gates -> core`` arrow at the file level (plan 02 unit 1,
    invariant table): the contract module every gate depends on may depend on
    nothing of ours and no third-party code. Parsed with ``ast``, not
    inspected at runtime, because a conditional or function-local import —
    precisely how a dependency inversion gets smuggled in — would not show up
    in the module namespace (same technique as
    ``test_db.py::test_db_imports_only_stdlib``).
    """
    source_path = inspect.getsourcefile(sluice.core.gate)
    tree = ast.parse(pathlib.Path(source_path).read_text(encoding="utf-8"))

    imported_roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            # A relative import (level > 0) is by definition inside sluice.
            if node.level:
                imported_roots.add("sluice")
            elif node.module:
                imported_roots.add(node.module.split(".")[0])

    non_stdlib = sorted(imported_roots - sys.stdlib_module_names)

    assert non_stdlib == [], f"{source_path} imports non-stdlib modules: {non_stdlib}"


def test_gate_result_fields_are_exactly_outcome_reason_metadata():
    """``GateResult`` carries exactly ``outcome``, ``reason``, ``metadata``.

    Equality is the point (plan 02 unit 2): the Plan A contract carries no
    ``retryable`` and no ``attempts`` — ADR-0002 decision 1 says every failure
    quarantines, so a retry field creeping into the result record would reopen
    that policy without an ADR. ``reason`` and ``metadata`` default to ``None``
    so a bare ``GateResult(GateOutcome.SUCCESS)`` is a complete, honest result.
    """
    # Local import: GateResult is the unit under construction; a module-level
    # import would fail collection for the already-green unit-1 tests.
    from sluice.core.gate import GateResult

    result_fields = dataclasses.fields(GateResult)
    assert [field.name for field in result_fields] == [
        "outcome",
        "reason",
        "metadata",
    ]

    defaults_by_name = {field.name: field.default for field in result_fields}
    assert defaults_by_name["reason"] is None
    assert defaults_by_name["metadata"] is None


def test_gate_result_failure_requires_a_reason():
    """A FAILURE ``GateResult`` refuses to construct without a real ``reason``.

    Plan 02 §3.2: the schema CHECK ``(error_reason IS NULL) = (errored_at IS
    NULL)`` makes a reasonless FAILURE unpersistable — at quarantine time the
    loop would have to fabricate a reason or violate the CHECK. Rejecting at
    construction (including whitespace-only, which would satisfy the CHECK
    while telling the operator nothing) surfaces the bug at the gate author's
    desk instead of inside the daemon's write transaction.
    """
    from sluice.core.gate import GateResult

    with pytest.raises(ValueError, match="reason"):
        GateResult(GateOutcome.FAILURE)

    with pytest.raises(ValueError, match="reason"):
        GateResult(GateOutcome.FAILURE, reason="  ")

    # The guard must not over-reject: a genuine reason constructs fine.
    assert GateResult(GateOutcome.FAILURE, reason="boom").reason == "boom"


def test_gate_result_reason_is_optional_for_every_non_failure_outcome():
    """A bare ``GateResult`` is valid, with ``reason`` None, except for FAILURE.

    The positive half of the §3.2 reason contract: only FAILURE demands a
    reason, because only the quarantine write needs one to satisfy the schema
    CHECK. The mock gates return a bare ``GateResult(GateOutcome.SUCCESS)``
    (plan 02 §3.3), so a well-meaning "tightening" that widens the reason
    requirement to every outcome would break every reference gate — this test
    catches that regression at the contract, not in a distant mock-gate test.
    """
    assert GateResult(GateOutcome.SUCCESS).reason is None
    assert GateResult(GateOutcome.HELD).reason is None
    assert GateResult(GateOutcome.SKIPPED).reason is None


def test_gate_contract_fields_are_exactly_the_spec_ten():
    """The ``Gate`` record carries exactly the ten contract fields, in order.

    This one equality carries three errata (plan 02 unit 3): no
    ``retryable``/``max_attempts`` (B4 — ADR-0002 quarantines every failure),
    no applicability field (§4 — required/disabled lives in the profile,
    ADR-0004, subsystem 5), and no ``timeout_seconds`` (B6 — deferred until the
    change that builds its enforcement). ``needs`` and ``produces`` have NO
    default: a gate author must state dependencies explicitly, because a
    silently-defaulted ``needs=()`` is the B3 deadlock's cousin. Editing this
    test is a conscious contract change requiring an ADR or plan revision.
    """
    # Local import: Gate is the unit under construction; a module-level import
    # would fail collection for the already-green unit-1/2 tests.
    from sluice.core.gate import Gate

    gate_fields = dataclasses.fields(Gate)
    assert [field.name for field in gate_fields] == [
        "id",
        "name",
        "satisfied_column",
        "order",
        "needs",
        "produces",
        "run",
        "invalidate",
        "terminate",
        "control_point",
    ]

    fields_by_name = {field.name: field for field in gate_fields}
    assert fields_by_name["terminate"].default is None
    assert fields_by_name["control_point"].default is True
    # No default of either kind: a default_factory would also let an author
    # omit needs/produces, so both slots must be MISSING.
    for required_field_name in ("needs", "produces"):
        assert fields_by_name[required_field_name].default is dataclasses.MISSING
        assert (
            fields_by_name[required_field_name].default_factory is dataclasses.MISSING
        )


def test_gate_result_rejects_a_non_outcome():
    """``GateResult`` refuses an ``outcome`` that is not a ``GateOutcome``.

    The loop branches on the outcome with ``is`` against the enum members
    (plan 02 unit 2), so a stray string — ``"success"`` is the sneakiest,
    being a valid enum *value* but not a member — would match no branch and
    silently mis-branch at persist time. Fail at construction instead, where
    the gate author sees it.
    """
    from sluice.core.gate import GateResult

    with pytest.raises(TypeError):
        GateResult("success")


def test_gate_records_are_frozen():
    """Both contract records refuse attribute assignment after construction.

    Pins the spec §4 ``frozen=True`` choice (plan 02 unit 3): gates are shared
    long-lived registry values and results are in flight to the loop's persist
    step — mutating either in place would change "pure data" behind every
    reader's back, and nothing but this test fails if someone removes the flag.
    """
    gate = make_gate()
    with pytest.raises(dataclasses.FrozenInstanceError):
        gate.order = 99

    result = GateResult(GateOutcome.SUCCESS)
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.outcome = GateOutcome.FAILURE


def test_gate_coerces_needs_and_produces_to_tuples():
    """``needs`` and ``produces`` are stored as tuples even when passed lists.

    A frozen record is only as immutable as its weakest field (plan 02 unit 3):
    a list passed at construction and kept by reference could be mutated later,
    changing a "frozen" Gate's dependency graph from the outside. Coercion at
    construction makes the freeze real for the two collection fields.
    """
    gate = make_gate(needs=["a"], produces=["p"])

    assert isinstance(gate.needs, tuple)
    assert gate.needs == ("a",)
    assert isinstance(gate.produces, tuple)
    assert gate.produces == ("p",)


def test_validate_gates_returns_execution_order():
    """``validate_gates`` returns the gate set sorted ascending by ``order``.

    Plan 02 unit 4 (B3 made mechanical): ``order`` is TRUE execution order, and
    the tuple ``validate_gates`` returns is the execution sequence subsystems 5
    and 6 consume. Passing gates in the wrong order — exactly how PP's
    ``PIPELINE_GATE_COLUMNS`` lists them — must come back re-sorted, so a
    registry author's listing order can never leak into scheduling.
    """
    # Local import: validate_gates is the unit under construction; a
    # module-level import would fail collection for the already-green tests.
    from sluice.core.gate import validate_gates

    alpha_like = make_gate(id="alpha_like", satisfied_column="alpha_like_done", order=1)
    beta_like = make_gate(id="beta_like", satisfied_column="beta_like_done", order=2)

    # Deliberately passed out of execution order (column-listing order).
    assert validate_gates([beta_like, alpha_like]) == (alpha_like, beta_like)


def test_validate_gates_rejects_duplicate_ids():
    """``validate_gates`` refuses a set where two gates share an ``id``.

    Plan 02 unit 4: gate ids are the keys ``needs`` resolves against and the
    keys every control op (set_gate, reset_gate) is addressed by, so a
    duplicate id makes dependency resolution and operator commands ambiguous.
    The error names the offending id so a registry author can find the clash.
    """
    from sluice.core.gate import validate_gates

    # Distinct satisfied_column and order on purpose: only the id collides,
    # so this test cannot pass by tripping a different uniqueness rule.
    first = make_gate(id="dup", satisfied_column="first_done", order=1)
    second = make_gate(id="dup", satisfied_column="second_done", order=2)

    with pytest.raises(ValueError, match="dup"):
        validate_gates([first, second])


def test_validate_gates_rejects_duplicate_satisfied_columns():
    """``validate_gates`` refuses a set where two gates share a ``satisfied_column``.

    Plan 02 unit 4: the satisfaction column is how the loop records completion,
    so if two gates share one, the loop's write for one gate's SUCCESS would
    silently mark the other satisfied too — ambiguous completion that reconcile
    can never distinguish. The error names the offending gate(s) so a registry
    author can find the clash.
    """
    from sluice.core.gate import validate_gates

    # Distinct ids and orders on purpose: only the satisfied_column collides,
    # so this test cannot pass by tripping a different uniqueness rule.
    first = make_gate(id="first_gate", satisfied_column="done", order=1)
    second = make_gate(id="second_gate", satisfied_column="done", order=2)

    with pytest.raises(ValueError, match="first_gate|second_gate"):
        validate_gates([first, second])


def test_validate_gates_rejects_duplicate_orders():
    """``validate_gates`` refuses a set where two gates share an ``order``.

    Plan 02 unit 4: the loop schedules "lowest order first" (spec §7), so equal
    orders make that pick nondeterministic — which gate runs first would depend
    on sort stability and listing order, the very ambiguity B3's "order is TRUE
    execution order" exists to forbid. The error names the offending gate(s) so
    a registry author can find the clash.
    """
    from sluice.core.gate import validate_gates

    # Distinct ids and satisfied_columns on purpose: only the order collides,
    # so this test cannot pass by tripping a different uniqueness rule.
    first = make_gate(id="first_gate", satisfied_column="first_done", order=1)
    second = make_gate(id="second_gate", satisfied_column="second_done", order=1)

    with pytest.raises(ValueError, match="first_gate|second_gate"):
        validate_gates([first, second])


def test_validate_gates_rejects_an_unknown_need():
    """``validate_gates`` refuses a set where a ``need`` names no gate in the set.

    Plan 02 unit 4: a need that resolves to nothing can never be satisfied, so
    reconcile would wait on it forever — a deadlock that only surfaces at
    daemon runtime. Failing at registry validation, with a message naming both
    the phantom dependency and the gate that declared it, turns a typo'd or
    forgotten gate id into an immediate, locatable test failure instead.
    """
    from sluice.core.gate import validate_gates

    needer = make_gate(id="needer_gate", needs=("ghost",))

    with pytest.raises(ValueError) as raised:
        validate_gates([needer])

    # The message must name both sides of the broken edge: the phantom need
    # ("ghost") and the gate that declared it ("needer_gate").
    message = str(raised.value)
    assert "ghost" in message
    assert "needer_gate" in message


def test_validate_gates_rejects_a_need_that_does_not_precede():
    """``validate_gates`` refuses a ``need`` whose ``order`` is not strictly lower.

    THE B3 test (plan 02 unit 4 / errata B3): python-poller's
    ``PIPELINE_GATE_COLUMNS`` (``poller_state_db.py:32-42``) lists download
    before the registration that must execute first — download needs the
    ``experiment_id`` registration produces. Transcribing that column list into
    ``order`` yields exactly this shape: the needer ordered *before* its need,
    a profile that deadlocks the daemon (the need can never be satisfied in
    time). This test makes the naive transcription fail at registry
    construction, with a message naming both ends of the inverted edge, instead
    of wedging reconcile at runtime.
    """
    from sluice.core.gate import validate_gates

    # The PP trap shape: the needer's order is LOWER than its need's order.
    download_like = make_gate(
        id="download",
        satisfied_column="download_done",
        order=1,
        needs=("registration",),
    )
    registration_like = make_gate(
        id="registration",
        satisfied_column="registration_done",
        order=2,
    )

    with pytest.raises(ValueError) as raised:
        validate_gates([download_like, registration_like])

    # The message must name both sides of the inverted edge so a registry
    # author can locate the transcription error.
    message = str(raised.value)
    assert "download" in message
    assert "registration" in message


def test_gate_exports_exactly_the_documented_surface():
    """``gate.__all__`` is exactly the four documented names (plan 02 unit 4).

    The scope guard, db unit-7 precedent: equality — not subset — so the public
    surface cannot silently grow (a helper leaking into the contract becomes an
    API someone depends on) or shrink (dropping a name breaks every downstream
    ``from sluice.core.gate import ...``). Widening this set is a conscious
    plan revision, made visible by editing this test.
    """
    assert set(sluice.core.gate.__all__) == {
        "Gate",
        "GateResult",
        "GateOutcome",
        "validate_gates",
    }


def test_gate_rejects_a_self_need():
    """A ``Gate`` whose ``needs`` names its own ``id`` refuses to construct.

    Plan 02 unit 3: a self-need can never be satisfied — reconcile would wait
    forever on a dependency that only completes after itself, an undetectable
    deadlock at daemon runtime. Construction is the only place the mistake is
    locally checkable (``validate_gates`` sees whole sets, but a lone record
    already carries the contradiction), so it fails here, naming the id.
    """
    with pytest.raises(ValueError, match="x"):
        make_gate(id="x", needs=("x",))


def test_mock_alpha_run_touches_its_marker_and_reports_success(tmp_path):
    """``MOCK_ALPHA.run`` writes its marker file and returns SUCCESS (plan 02 §3.3).

    The touch-file reference gate (design §12): ``run`` does its work purely on
    the filesystem under ``context.workdir`` and *returns* its result — it never
    writes run state (ADR-0002), which is why this test asserts on the returned
    ``GateResult`` and the filesystem only. The metadata carries the marker path
    so the loop's persist step has something real to record; the context is a
    bare ``SimpleNamespace(workdir=...)`` because ``workdir`` is the *entire*
    context contract the mocks may rely on (§3.4).
    """
    # Local import: mock_gates is the module under construction; a module-level
    # import would fail collection for the already-green unit-1..4 tests.
    from sluice.core.mock_gates import MOCK_ALPHA

    ctx = types.SimpleNamespace(workdir=tmp_path)

    result = MOCK_ALPHA.run(ctx)

    assert result.outcome is GateOutcome.SUCCESS
    assert (tmp_path / "mock_alpha.marker").exists()
    assert result.metadata["marker"] == str(tmp_path / "mock_alpha.marker")


def test_mock_alpha_run_is_reentrant(tmp_path):
    """``MOCK_ALPHA.run`` succeeds again when its marker already exists (plan 02 §3.3).

    Design §4's "idempotent enough to re-enter", pinned on the reference
    implementation: after a crash-and-restart, or a ``clear_error`` recovery,
    the daemon re-runs a gate whose artifacts may already be on disk. A run
    that raised (or reported FAILURE) on finding its own marker would turn
    every re-entry into a fresh quarantine — so the second call must return
    SUCCESS just like the first, not merely avoid crashing.
    """
    from sluice.core.mock_gates import MOCK_ALPHA

    ctx = types.SimpleNamespace(workdir=tmp_path)

    first_result = MOCK_ALPHA.run(ctx)
    assert first_result.outcome is GateOutcome.SUCCESS

    # Re-entry: the marker is already present; the gate must not care.
    second_result = MOCK_ALPHA.run(ctx)
    assert second_result.outcome is GateOutcome.SUCCESS
    assert (tmp_path / "mock_alpha.marker").exists()


def test_mock_alpha_invalidate_is_the_inverse_and_idempotent(tmp_path):
    """``MOCK_ALPHA.invalidate`` removes the marker; a clean directory is fine (plan 02 §3.3).

    The two halves of the invalidate contract on the reference gate: it undoes
    exactly what ``produces`` declares (the marker — B2 honesty), and it is
    ``missing_ok`` because ``reset_gate`` may be issued against a gate that
    never ran — an invalidate that crashed on a clean directory would turn an
    operator's no-op reset into an error. Per ADR-0002 decision 2, invalidate
    touches only the filesystem; the *caller* clears the satisfaction column,
    which is why this test asserts on files alone.
    """
    from sluice.core.mock_gates import MOCK_ALPHA

    ctx = types.SimpleNamespace(workdir=tmp_path)

    run_result = MOCK_ALPHA.run(ctx)
    assert run_result.outcome is GateOutcome.SUCCESS
    marker_path = tmp_path / "mock_alpha.marker"
    assert marker_path.exists()

    # Inverse: invalidate removes exactly the artifact run produced.
    MOCK_ALPHA.invalidate(ctx)
    assert not marker_path.exists()

    # Idempotent: a second invalidate on the now-clean directory must not raise
    # (a reset_gate on a never-run gate takes this same path).
    MOCK_ALPHA.invalidate(ctx)
    assert not marker_path.exists()


def test_mock_alpha_record():
    """``MOCK_ALPHA``'s record fields match the §3.3 spec exactly (plan 02 unit 5).

    These four values are load-bearing seams, not decoration:
    ``satisfied_column`` is the name migration 005 adds to ``runs`` (a drift
    here surfaces as ``UnknownColumnError`` at daemon runtime — unit 8's
    cross-binding test guards the other side); ``needs == ()`` is what lets
    reconcile schedule alpha first; ``terminate is None`` is the
    not-interruptible branch of the contract (the daemon must not call a
    terminator that does not exist); and ``produces`` naming exactly the
    marker ``invalidate`` clears is the B2 honesty rule ``set_gate``/
    ``reset_gate`` policy (subsystem 8) will be keyed on.
    """
    from sluice.core.mock_gates import MOCK_ALPHA

    assert MOCK_ALPHA.satisfied_column == "mock_alpha_done"
    assert MOCK_ALPHA.needs == ()
    assert MOCK_ALPHA.terminate is None
    assert MOCK_ALPHA.produces == ("mock_alpha.marker",)


# ``None`` = no instruction file at all — the daemon's ordinary path, where
# nobody scripted an outcome and the gate must simply succeed (plan 02 §3.3).
# An EMPTY (or whitespace-only, which strips to empty) file is the opposite
# case, named distinctly by §3.3: the scripting seam exists but says nothing,
# so it must fail loud, never fall back to the unscripted SUCCESS path.
@pytest.mark.parametrize(
    ("instruction", "expected_outcome", "marker_expected"),
    [
        (None, GateOutcome.SUCCESS, True),
        ("success", GateOutcome.SUCCESS, True),
        ("failure", GateOutcome.FAILURE, False),
        ("held", GateOutcome.HELD, False),
        ("skipped", GateOutcome.SKIPPED, False),
        ("", GateOutcome.FAILURE, False),
        (" \n\t ", GateOutcome.FAILURE, False),
    ],
    ids=["absent", "success", "failure", "held", "skipped", "empty", "whitespace-only"],
)
def test_mock_beta_follows_its_instruction_file(
    tmp_path, instruction, expected_outcome, marker_expected
):
    """``MOCK_BETA.run`` dispatches on its instruction file's content (plan 02 §3.3).

    The scriptable reference gate: daemon/control integration tests (subsystems
    8-9) script outcomes by writing ``mock_beta.instruction`` through the
    filesystem — no monkeypatching a running daemon — so each of the four
    outcomes must be reachable by content, and an absent file must mean plain
    SUCCESS. An empty or whitespace-only file is NOT absent: §3.3 makes it a
    distinct fail-loud FAILURE, because the sneakiest wrong implementation —
    ``if not instruction:`` treating a falsy read as "nobody scripted this" —
    would silently turn a half-written scripted test into SUCCESS, exactly the
    no-silent-defaults violation the fail-loud branch exists to forbid. The
    marker is written on SUCCESS only: a FAILURE/HELD/SKIPPED run leaving a
    marker behind would let a later invalidate-then-rerun cycle mistake
    scripted non-success for completed work. FAILURE must carry a real reason
    because the loop persists it into ``error_reason`` (§3.2, the schema
    CHECK). Asserts on the returned ``GateResult`` and the filesystem only,
    never the DB (ADR-0002).
    """
    # Local import: MOCK_BETA is the unit under construction; a module-level
    # import would fail collection for the already-green unit-1..5 tests.
    from sluice.core.mock_gates import MOCK_BETA

    if instruction is not None:
        (tmp_path / "mock_beta.instruction").write_text(instruction, encoding="utf-8")

    ctx = types.SimpleNamespace(workdir=tmp_path)

    result = MOCK_BETA.run(ctx)

    assert result.outcome is expected_outcome
    assert (tmp_path / "mock_beta.marker").exists() is marker_expected
    if expected_outcome is GateOutcome.FAILURE:
        # The loop will persist this into error_reason; it must be real text.
        assert isinstance(result.reason, str)
        assert result.reason.strip() != ""


def test_mock_beta_fails_loud_on_an_unknown_instruction(tmp_path):
    """``MOCK_BETA.run`` reports FAILURE naming an unrecognized instruction (plan 02 §3.3).

    The no-silent-defaults posture (guardrail §8) applied to the reference
    implementation: an unrecognized script — here ``"retry"``, exactly the word
    a future test author might type expecting a retry semantics that ADR-0002
    abolished — must be an error, never a quiet fallback to success. The
    literal bad instruction must appear in ``reason`` so the quarantined run's
    ``error_reason`` tells the operator *what* was typo'd; without this, a
    typo'd daemon integration test would pass vacuously on a silent SUCCESS.
    """
    from sluice.core.mock_gates import MOCK_BETA

    (tmp_path / "mock_beta.instruction").write_text("retry", encoding="utf-8")
    ctx = types.SimpleNamespace(workdir=tmp_path)

    result = MOCK_BETA.run(ctx)

    assert result.outcome is GateOutcome.FAILURE
    assert "retry" in result.reason


def test_mock_beta_terminate_leaves_a_visible_trace(tmp_path):
    """``MOCK_BETA.terminate`` writes its trace file and reports nothing (plan 02 §3.3).

    The interruptible branch of the contract (spec §4, decision A): terminate
    stops in-flight work and returns ``None``, never a ``GateResult`` — the
    loop already knows the run held (``lifecycle='held'`` was the operator's
    write), so a terminator that reported an outcome would invite a second,
    conflicting persist. The visible trace (``mock_beta.terminated``) is what
    lets daemon/control integration tests (subsystems 8-9) assert termination
    happened by reading state alone; and terminate must not fabricate the
    success marker, because a marker appearing on a *hold* would let a later
    rerun mistake interruption for completed work.
    """
    from sluice.core.mock_gates import MOCK_BETA

    ctx = types.SimpleNamespace(workdir=tmp_path)

    returned = MOCK_BETA.terminate(ctx)

    assert returned is None
    assert (tmp_path / "mock_beta.terminated").exists()
    assert not (tmp_path / "mock_beta.marker").exists()


def test_mock_beta_invalidate_removes_all_its_artifacts(tmp_path):
    """``MOCK_BETA.invalidate`` removes the marker AND the terminated trace (plan 02 §3.3).

    Beta produces two artifacts — ``mock_beta.marker`` (success) and
    ``mock_beta.terminated`` (the terminate trace) — and invalidate must undo
    *both* (B2 honesty: ``produces`` names exactly what invalidate clears). A
    stale terminated flag surviving a reset would pollute a re-run's forensics:
    a daemon/control test reading state afterward would conclude the fresh run
    was interrupted when it never was. Idempotent (``missing_ok``) for the same
    reason as alpha's: a ``reset_gate`` on a never-run gate must not crash. Per
    ADR-0002 decision 2, only the filesystem is touched; the caller clears the
    satisfaction column, so this test asserts on files alone.
    """
    from sluice.core.mock_gates import MOCK_BETA

    ctx = types.SimpleNamespace(workdir=tmp_path)

    # A successful (unscripted) run plus a terminate call lays down both
    # artifacts beta can ever produce.
    run_result = MOCK_BETA.run(ctx)
    assert run_result.outcome is GateOutcome.SUCCESS
    MOCK_BETA.terminate(ctx)
    marker_path = tmp_path / "mock_beta.marker"
    terminated_path = tmp_path / "mock_beta.terminated"
    assert marker_path.exists()
    assert terminated_path.exists()

    # Inverse: invalidate clears everything beta produced.
    MOCK_BETA.invalidate(ctx)
    assert not marker_path.exists()
    assert not terminated_path.exists()

    # Idempotent: a second invalidate on the now-clean directory must not raise.
    MOCK_BETA.invalidate(ctx)
    assert not marker_path.exists()
    assert not terminated_path.exists()


def test_mock_beta_record():
    """``MOCK_BETA``'s record fields match the §3.3 spec exactly (plan 02 unit 6).

    These four values are load-bearing seams, not decoration:
    ``needs == ("mock_alpha",)`` is the one dependency edge in the mock
    registry — drop it and reconcile could schedule beta before alpha, the
    exact B3 failure shape the registry exists to demonstrate;
    ``satisfied_column`` is the name migration 005 adds to ``runs`` (unit 8's
    cross-binding test guards the other side of that seam); ``terminate is not
    None`` is the interruptible branch of the contract — the daemon only calls
    a terminator on hold if one exists, so a ``None`` here would silently make
    the reference interruptible gate uninterruptible; and ``produces`` naming
    exactly the two files ``invalidate`` clears is the B2 honesty rule
    ``set_gate``/``reset_gate`` policy (subsystem 8) will be keyed on.
    """
    from sluice.core.mock_gates import MOCK_BETA

    assert MOCK_BETA.needs == ("mock_alpha",)
    assert MOCK_BETA.satisfied_column == "mock_beta_done"
    assert MOCK_BETA.terminate is not None
    assert MOCK_BETA.produces == ("mock_beta.marker", "mock_beta.terminated")


def test_mock_gates_is_a_valid_ordered_registry():
    """``MOCK_GATES`` passes ``validate_gates`` unchanged, in execution order (plan 02 unit 7).

    The registry is what downstream subsystems consume (5's profiles, 6's
    reconcile), and Plan B's production registry must pass through
    ``validate_gates`` too — so the reference registry demonstrates the pattern:
    already listed in TRUE execution order (B3) and clean under every rule,
    including needs-precede, ``validate_gates`` returns it *equal to itself*.
    If someone reorders the tuple, breaks a dependency edge, or lets the
    registry drift from the two records it aggregates, this is where it fails —
    at registry construction, not inside a wedged reconcile.
    """
    # Local import: MOCK_GATES is the unit under construction; a module-level
    # import would fail collection for the already-green unit-1..6 tests.
    from sluice.core.gate import validate_gates
    from sluice.core.mock_gates import MOCK_GATES

    assert validate_gates(MOCK_GATES) == MOCK_GATES
    assert [gate.id for gate in MOCK_GATES] == ["mock_alpha", "mock_beta"]


def test_mock_gates_import_only_gate_and_stdlib():
    """``mock_gates.py`` imports only stdlib + ``sluice.core.gate`` (plan 02 unit 7).

    The STRUCTURAL ADR-0002 guard: a gate module that cannot reach a SQL
    client (``sqlite3``) or the db primitives (``sluice.core.db``) cannot
    write run state, so decision 2 — gates never touch ``runs`` columns — is
    enforced by what the module *can even name*, not just by unit 9's
    behavioral check. It also forecloses a ``db <-> mock_gates`` import cycle
    (db's migration 005 names the mock columns; the reverse arrow would knot
    the foundation to a leaf). Parsed with ``ast``, not inspected at runtime,
    because a function-local ``import sqlite3`` — precisely how a "just this
    once" write sneaks in — never shows up in the module namespace (same
    technique as ``test_gate_imports_only_stdlib``).
    """
    # Local import: mock_gates is the module under construction; a
    # module-level import would fail collection for the already-green tests.
    import sluice.core.mock_gates

    source_path = inspect.getsourcefile(sluice.core.mock_gates)
    tree = ast.parse(pathlib.Path(source_path).read_text(encoding="utf-8"))

    # Collect fully-qualified module names for every import statement. For
    # ``from X import Y`` we record the module X (absolute) or resolve the
    # relative base against this module's package, sluice.core — so
    # ``from .db import connect`` cannot dodge the guard by being relative.
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

    # Asserted by name so the failure message teaches (plan 02 unit 7).
    sql_client_imports = sorted(
        module
        for module in imported_modules
        if module == "sqlite3" or module.startswith("sqlite3.")
    )
    assert sql_client_imports == [], (
        f"{source_path} imports {sql_client_imports}: a gate module must not "
        "reach a SQL client — gates never write run state (ADR-0002 decision 2)"
    )
    db_module_imports = sorted(
        module
        for module in imported_modules
        if module == "sluice.core.db" or module.startswith("sluice.core.db.")
    )
    assert db_module_imports == [], (
        f"{source_path} imports {db_module_imports}: a gate module must not "
        "reach the db primitives — gates never write run state (ADR-0002 "
        "decision 2), and this arrow would also close a db <-> mock_gates cycle"
    )

    # Every sluice-internal import is exactly the gate contract module.
    sluice_module_imports = sorted(
        module
        for module in imported_modules
        if module == "sluice" or module.startswith("sluice.")
    )
    non_gate_sluice_imports = [
        module for module in sluice_module_imports if module != "sluice.core.gate"
    ]
    assert non_gate_sluice_imports == [], (
        f"{source_path} imports sluice modules beyond the gate contract: "
        f"{non_gate_sluice_imports}"
    )

    # Everything else must be standard library — no third-party code in the
    # reference gates (the gates -> core arrow, invariant table).
    non_stdlib_roots = sorted(
        {module.split(".")[0] for module in imported_modules}
        - sys.stdlib_module_names
        - {"sluice"}
    )
    assert non_stdlib_roots == [], (
        f"{source_path} imports non-stdlib modules: {non_stdlib_roots}"
    )


def test_mock_gates_exports_exactly_the_documented_surface():
    """``mock_gates.__all__`` is exactly the three documented names (plan 02 unit 7).

    The scope guard, db unit-7 precedent: equality — not subset — so the public
    surface cannot silently grow (a ``_run_mock_alpha`` helper leaking into the
    export list becomes an API a downstream subsystem depends on, bypassing the
    Gate records that are the only supported handles) or shrink (dropping a
    name breaks every ``from sluice.core.mock_gates import ...`` downstream).
    Widening this set is a conscious plan revision, made visible by editing
    this test.
    """
    # Local import: mock_gates is the module under construction; a
    # module-level import would fail collection for the already-green tests.
    import sluice.core.mock_gates

    assert set(sluice.core.mock_gates.__all__) == {
        "MOCK_ALPHA",
        "MOCK_BETA",
        "MOCK_GATES",
    }


def test_migration_005_adds_the_mock_satisfaction_columns(tmp_path):
    """Migration 005 adds ``mock_alpha_done``/``mock_beta_done`` to ``runs`` (§3.5).

    A fresh row reads back ``0`` for both (plan 02 unit 8). The satisfaction
    columns are the loop's completion record for the two reference gates, and
    both halves of this assertion are load-bearing: the columns must exist
    after a plain ``initialize_database`` (the ladder now ends at 005 —
    subsystem 2's one edit to core ``db.py``, F3-revision), and a
    newly-inserted run must read back exactly ``0``, not ``None`` — a nullable
    satisfaction column would give reconcile three-valued logic, where
    ``WHERE col = 0`` silently misses NULL rows and a run that never ran a
    gate quietly falls out of scheduling. New runs start unsatisfied by
    ``DEFAULT 0``, never by any code writing the column.
    """
    # db is the shipped substrate (subsystem 1), imported locally only to
    # match this file's import style — it is green on arrival, not under
    # construction here.
    import contextlib

    from sluice.core.db import get_connection, get_run, initialize_database, insert_run

    db_path = tmp_path / "state.db"
    initialize_database(db_path)

    with contextlib.closing(get_connection(db_path)) as connection:
        live_runs_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(runs)")
        }
        assert {"mock_alpha_done", "mock_beta_done"} <= live_runs_columns, (
            "migration 005 must add both mock satisfaction columns to runs; "
            f"missing: {sorted({'mock_alpha_done', 'mock_beta_done'} - live_runs_columns)}"
        )

        # A minimal row inserted through the shipped primitive: both columns
        # must land as exactly 0 (== 0 also excludes None — NULL would be the
        # three-valued-logic bug §3.5 forbids).
        assert insert_run(connection, "r1", pipeline_version="reference-v1")
        row = get_run(connection, "r1")
        assert row["mock_alpha_done"] == 0
        assert row["mock_beta_done"] == 0


def test_migration_005_lands_on_a_populated_database(tmp_path, monkeypatch):
    """Migration 005 applies over a populated 004-level DB; old rows read ``0`` (§3.5).

    The greenfield-drain story (plan 02 unit 8): a real deployment upgrades a
    database that already holds runs, so the ALTERs must be legal on a
    populated table (``DEFAULT 0`` is a constant — the db plan §4 note) and
    every pre-existing row must land as exactly ``0`` for both mock columns:
    unsatisfied, because those runs never ran the gates. ``NULL`` here would be
    the three-valued-logic bug (``WHERE col = 0`` silently misses NULL, so
    reconcile would never schedule the drained runs); any truthy value would
    spuriously mark never-run gates satisfied. Truncation technique per
    ``test_db.py::test_index_migration_applies_to_a_pre_existing_database``.
    """
    import contextlib

    import sluice.core.db

    full_migrations = sluice.core.db.MIGRATIONS
    db_path = tmp_path / "state.db"

    # A populated pre-005 database: ladder truncated to the db subsystem's
    # shipped 001-004, with one run already living in it.
    monkeypatch.setattr(sluice.core.db, "MIGRATIONS", full_migrations[:4])
    sluice.core.db.initialize_database(db_path)
    with contextlib.closing(sluice.core.db.get_connection(db_path)) as connection:
        assert sluice.core.db.insert_run(
            connection, "preexisting", pipeline_version="reference-v1"
        )

    # The upgrade: restore the full ladder and apply 005 over live data.
    monkeypatch.setattr(sluice.core.db, "MIGRATIONS", full_migrations)
    with contextlib.closing(sluice.core.db.get_connection(db_path)) as connection:
        sluice.core.db.apply_migrations(connection)

        # The pre-existing row survives, untouched but for the two new columns,
        # which read exactly 0 — unsatisfied, never NULL, never satisfied.
        row = sluice.core.db.get_run(connection, "preexisting")
        assert row is not None
        assert row["pipeline_version"] == "reference-v1"
        assert row["mock_alpha_done"] == 0
        assert row["mock_beta_done"] == 0


def test_mock_columns_arrive_only_in_migration_005(tmp_path, monkeypatch):
    """Migrations 001-004 alone yield no ``runs`` column naming a mock gate.

    The relocated F3 guard (plan 02 unit 8, succeeding
    ``test_db.py::test_no_mock_gate_columns_in_db_subsystem``): the db
    subsystem's own migrations name no gates — core "knows the contract, not
    the gates" (design §4) — so the mock columns exist only because subsystem
    2's migration 005 adds them. Truncation technique per
    ``test_db.py::test_index_migration_applies_to_a_pre_existing_database``.
    """
    import contextlib

    import sluice.core.db

    db_path = tmp_path / "state.db"
    # Truncate the ladder to the db subsystem's shipped migrations 001-004.
    monkeypatch.setattr(sluice.core.db, "MIGRATIONS", sluice.core.db.MIGRATIONS[:4])
    sluice.core.db.initialize_database(db_path)

    with contextlib.closing(sluice.core.db.get_connection(db_path)) as connection:
        runs_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(runs)")
        }

    assert "name" in runs_columns, (
        "runs table must exist for this guard to be meaningful"
    )
    mock_named = {column for column in runs_columns if "mock" in column}
    assert mock_named == set(), (
        f"a db-subsystem migration named a gate column: {sorted(mock_named)}"
    )


def test_every_mock_gate_column_is_live_in_the_schema(tmp_path):
    """Every ``MOCK_GATES`` ``satisfied_column`` exists on ``runs`` after init.

    THE cross-binding test (plan 02 unit 8) for the one seam this subsystem
    creates: the mock column names appear in two places that nothing else ties
    together — ``db.py``'s migration-005 ALTERs and ``mock_gates.py``'s Gate
    records. ``Gate`` deliberately does not validate that ``satisfied_column``
    is schema-live (§3.1's stated non-check), so a rename on *either* side
    would otherwise surface only at daemon runtime, as ``UnknownColumnError``
    inside the loop's persist step. Asserting through the records themselves —
    not hardcoded strings — makes this test fail for any drift between the
    registry and the live schema, whichever side moved.
    """
    import contextlib

    # db is the shipped substrate (subsystem 1); MOCK_GATES is this
    # subsystem's registry — imported locally per this file's style.
    from sluice.core.db import get_connection, initialize_database
    from sluice.core.mock_gates import MOCK_GATES

    db_path = tmp_path / "state.db"
    initialize_database(db_path)

    with contextlib.closing(get_connection(db_path)) as connection:
        live_runs_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(runs)")
        }

    declared_satisfaction_columns = {gate.satisfied_column for gate in MOCK_GATES}
    assert declared_satisfaction_columns <= live_runs_columns, (
        "mock gate satisfaction columns missing from the runs schema "
        "(registry/migration drift): "
        f"{sorted(declared_satisfaction_columns - live_runs_columns)}"
    )


def test_gate_functions_leave_the_database_untouched(tmp_path):
    """Every class of gate function runs to completion without one DB write.

    THE behavioral ADR-0002 decision-2 test (plan 02 unit 9), complementing
    unit 7's structural import guard: run/terminate/invalidate are exercised —
    including a scripted FAILURE, the outcome most tempting to "helpfully"
    persist from inside the gate — against a real initialized database holding
    a real run, and the database must come back byte-identical (row dict,
    metadata store, schema version). The context deliberately carries only
    ``workdir`` (§3.4): a gate has no handle to the DB at all, so the loop —
    subsystems 8/9, played by the choreography test below — is provably the
    sole writer of run state.
    """
    import contextlib

    from sluice.core.db import (
        all_metadata,
        get_connection,
        get_run,
        initialize_database,
        insert_run,
        schema_version,
    )
    from sluice.core.mock_gates import MOCK_ALPHA, MOCK_BETA

    db_path = tmp_path / "state.db"
    initialize_database(db_path)

    # The gates' workdir is a separate directory: the context contract is
    # workdir alone, so nothing hands the gates even the DB file's location.
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    ctx = types.SimpleNamespace(workdir=workdir)

    with contextlib.closing(get_connection(db_path)) as connection:
        assert insert_run(connection, "r1", pipeline_version="reference-v1")
        row_snapshot = dict(get_run(connection, "r1"))
        metadata_snapshot = all_metadata(connection)
        # Captured, not hardcoded (plan 08 unit 9's ladder-agnostic reword):
        # the point is that GATES change no schema, which must keep holding
        # however tall the migration ladder grows.
        schema_version_snapshot = schema_version(connection)

        # One call from every gate-function class, across both mock gates,
        # including the quarantine-shaped outcome (scripted FAILURE).
        alpha_result = MOCK_ALPHA.run(ctx)
        assert alpha_result.outcome is GateOutcome.SUCCESS
        (workdir / "mock_beta.instruction").write_text("failure", encoding="utf-8")
        beta_result = MOCK_BETA.run(ctx)
        assert beta_result.outcome is GateOutcome.FAILURE
        MOCK_BETA.terminate(ctx)
        MOCK_ALPHA.invalidate(ctx)

        # Byte-identical: no satisfaction write, no error write, no metadata,
        # no schema change — gates returned results and touched the filesystem.
        assert dict(get_run(connection, "r1")) == row_snapshot
        assert all_metadata(connection) == metadata_snapshot
        assert schema_version(connection) == schema_version_snapshot


def test_loop_choreography_run_persist_quarantine_reset(tmp_path):
    """The test-as-loop persists every outcome the gates only ever *return*.

    The executable statement of §3.2's who-writes-what table (plan 02 unit 9),
    which subsystems 6, 8 and 9 will implement and cite: gates return results;
    the loop writes satisfaction in its own transaction (SUCCESS), writes both
    error columns together (FAILURE — the quarantine leg of ADR-0002 decision
    1, observable as the run leaving ``select_active_runs`` with no retry path
    in sight); ``clear_error`` is the *only* recovery; and reset is the §2
    invalidate-choreography split — the gate undoes its artifacts, the caller
    clears the column. Uses autocommit primitives only (short-transactions
    invariant); each step is commented with the subsystem that will own the
    write.
    """
    import contextlib

    from sluice.core.db import (
        get_connection,
        get_run,
        initialize_database,
        insert_run,
        now_iso,
        select_active_runs,
        update_run,
    )
    from sluice.core.mock_gates import MOCK_ALPHA, MOCK_BETA

    db_path = tmp_path / "state.db"
    initialize_database(db_path)
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    ctx = types.SimpleNamespace(workdir=workdir)

    with contextlib.closing(get_connection(db_path)) as connection:
        assert insert_run(connection, "r1", pipeline_version="reference-v1")

        # Step 1 — SUCCESS: the gate returns it; *as the daemon (subsystem 9)*
        # the loop sets the satisfaction column in its own write.
        alpha_result = MOCK_ALPHA.run(ctx)
        assert alpha_result.outcome is GateOutcome.SUCCESS
        assert update_run(connection, "r1", {"mock_alpha_done": 1})
        assert get_run(connection, "r1")["mock_alpha_done"] == 1

        # Step 2 — FAILURE quarantines, always (ADR-0002 decision 1): *as the
        # daemon* the loop writes BOTH error columns together (the schema CHECK
        # binds them), and the run leaves eligibility. No retry leg exists.
        (workdir / "mock_beta.instruction").write_text("failure", encoding="utf-8")
        beta_result = MOCK_BETA.run(ctx)
        assert beta_result.outcome is GateOutcome.FAILURE
        assert update_run(
            connection,
            "r1",
            {"error_reason": beta_result.reason, "errored_at": now_iso()},
        )
        assert "r1" not in [row["name"] for row in select_active_runs(connection)]

        # Step 3 — recovery is ONLY the operator's clear_error (subsystem 8):
        # both error columns cleared together; the run is eligible again.
        assert update_run(connection, "r1", {"error_reason": None, "errored_at": None})
        assert "r1" in [row["name"] for row in select_active_runs(connection)]

        # Step 4 — reset choreography (subsystem 8's reset_gate, per §2's
        # ADR-0002 split): the gate's invalidate undoes the on-disk artifact,
        # THEN the caller clears the satisfaction column in its own write.
        MOCK_ALPHA.invalidate(ctx)
        assert not (workdir / "mock_alpha.marker").exists()
        assert update_run(connection, "r1", {"mock_alpha_done": 0})
        assert get_run(connection, "r1")["mock_alpha_done"] == 0

        # Re-runnable: the same gate runs again cleanly after the reset.
        rerun_result = MOCK_ALPHA.run(ctx)
        assert rerun_result.outcome is GateOutcome.SUCCESS
        assert (workdir / "mock_alpha.marker").exists()
