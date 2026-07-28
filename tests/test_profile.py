"""Behavioral tests for ``sluice.core.profile`` — the pipeline-profile model (plan 05).

A profile is the ordered gate set plus per-gate applicability for a
``pipeline_version`` (design §8). Applicability is policy and lives here, never
on the gate (design §4); the vocabulary is deliberately closed at two, because
a run's contract is binary — in and mandatory, or out and absent (ADR-0004
decision 2) — and the closed set is itself the B1/B4 surface: no further
applicability member and no retry member may creep in (plan 05 §2 B4,
ADR-0002).
"""

import ast
import dataclasses
import inspect
import pathlib
import sys

import pytest


def test_applicability_vocabulary_is_the_binary_two():
    """``Applicability`` is exactly REQUIRED/DISABLED with the §8 string values (ADR-0004).

    Equality, not subset: a run's contract is binary — a gate is in the
    contract and mandatory, or out and absent (ADR-0004 decision 2), so the
    closed two-member set *is* the decision's surface. A third tier (optional,
    retry, a "paused" state) can only return via a conscious edit of this test
    plus a new ADR. The string values are what profiles and reconcile speak,
    so they are pinned alongside the names.
    """
    # Local import: profile is the module under construction; a module-level
    # import would fail collection for every other test in this file.
    from sluice.core.profile import Applicability

    assert {member.name: member.value for member in Applicability} == {
        "REQUIRED": "required",
        "DISABLED": "disabled",
    }


def test_profile_is_core_clean_by_import():
    """``profile.py`` imports only stdlib + ``sluice.core.gate``/``mock_gates``; db/SDK/outer-layer names forbidden.

    This one test carries the freeze's structural backstop (plan 05 unit 1,
    §2 fact 1): a module that cannot name ``sqlite3`` or ``sluice.core.db``
    cannot read ``metadata.pipeline_profile``, so it can never silently
    re-resolve an in-flight run's frozen ``pipeline_version`` against the
    active default (design §8). The same closed set keeps the dependency
    arrows honest (gates/api/cli → core, never the reverse) and keeps service
    SDKs (``slack_sdk``, ``structlog``) out — a profile is pure data plus pure
    functions over its inputs (plan 05 §1). Parsed with ``ast``, not inspected
    at runtime, because a function-local import — precisely how a "just this
    once" query sneaks in — never shows up in the module namespace (same
    technique as ``test_db_imports_only_stdlib`` /
    ``test_events_is_core_clean_by_import`` /
    ``test_context_is_stdlib_only_and_db_free``).
    """
    # Local import: profile is the module under construction; a module-level
    # import would fail collection for every other test in this file.
    import sluice.core.profile

    source_path = inspect.getsourcefile(sluice.core.profile)
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
    # (plan 05 unit 1): each name is a distinct policy with its own reason.
    forbidden_by_name = {
        "sqlite3": (
            "profile must not reach a SQL client — a module that cannot open "
            "a connection cannot read metadata.pipeline_profile, so it cannot "
            "re-resolve an in-flight run's frozen pipeline_version against "
            "the active default (the freeze, plan 05 §2 fact 1 / design §8)"
        ),
        "sluice.core.db": (
            "profile must not reach the db primitives — same freeze backstop "
            "as sqlite3 (plan 05 §2 fact 1); profiles are read-only pure data "
            "and hold no connection"
        ),
        "slack_sdk": (
            "core never imports a service SDK (ADR-0003 decision 3) — a "
            "profile emits no notification and holds no notifier"
        ),
        "structlog": (
            "profile's allowed set is stdlib + sluice.core.gate + "
            "sluice.core.mock_gates ONLY (plan 05 §1) — a profile emits no "
            "event and holds no logger; structlog stays in events"
        ),
        "sluice.gates": (
            "the dependency arrow is gates → core; core profile importing a "
            "gates module would reverse it — and would let production "
            "profiles leak into Plan A (plan 05 §1, fact 4 / B1)"
        ),
        "sluice.api": (
            "the dependency arrow is api → core; core profile importing api "
            "would reverse it (plan 05 §1)"
        ),
        "sluice.cli": (
            "the dependency arrow is cli → core; core profile importing cli "
            "would reverse it (plan 05 §1)"
        ),
    }
    for forbidden_module, reason in forbidden_by_name.items():
        matches = sorted(
            module
            for module in imported_modules
            if module == forbidden_module or module.startswith(f"{forbidden_module}.")
        )
        assert matches == [], f"{source_path} imports {matches}: {reason}"

    # The closed sluice-internal set (plan 05 §1): profile consumes the gate
    # contract (validate_gates/Gate) and references the mock gates to build
    # the reference profiles — and nothing else sluice-internal.
    allowed_sluice_imports = {"sluice.core.gate", "sluice.core.mock_gates"}
    stray_sluice_imports = sorted(
        module
        for module in imported_modules
        if (module == "sluice" or module.startswith("sluice."))
        and module not in allowed_sluice_imports
    )
    assert stray_sluice_imports == [], (
        f"{source_path} imports {stray_sluice_imports} — profile's only "
        "allowed sluice-internal imports are sluice.core.gate and "
        "sluice.core.mock_gates (plan 05 §1 closed set)"
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
        "profile's allowed import set is stdlib + sluice.core.gate + "
        "sluice.core.mock_gates only (plan 05 §1)"
    )


def test_profile_fields_are_exactly_pipeline_version_gates_applicability():
    """``Profile`` carries exactly ``pipeline_version``, ``gates``, ``applicability`` — all required.

    Equality is the point (plan 05 unit 2): the record is deliberately LEAN.
    No ``config`` field (profiles are code-defined, fact 4), no run-state
    handle (ADR-0002 — gates never write state, and neither do the profiles
    that describe them), and no ``retryable``/``max_attempts`` (B4 — every
    failure quarantines; retry arrives only as a new ADR, never as a quiet
    profile field). All three fields are required with no default of either
    kind: a defaulted ``applicability`` would let an author ship a profile
    whose gates reconcile cannot classify. Editing this test is a conscious
    contract change.
    """
    # Local import: Profile is the unit under construction; a module-level
    # import would fail collection for the already-green unit-1 tests.
    from sluice.core.profile import Profile

    profile_fields = dataclasses.fields(Profile)
    assert [field.name for field in profile_fields] == [
        "pipeline_version",
        "gates",
        "applicability",
    ]

    # No default and no default_factory on any field: every profile author
    # must state all three, so nothing can be omitted silently.
    for field in profile_fields:
        assert field.default is dataclasses.MISSING, (
            f"Profile.{field.name} must have no default (plan 05 unit 2)"
        )
        assert field.default_factory is dataclasses.MISSING, (
            f"Profile.{field.name} must have no default_factory (plan 05 unit 2)"
        )


def test_profile_is_frozen():
    """Assigning to a field on a ``Profile`` instance raises ``FrozenInstanceError``.

    The DB and the profile registry are the truth (plan 05 unit 2 / the
    spine): a profile that could be reassigned after construction would become
    a second, mutable in-memory authority over which gates apply to an
    in-flight run — and would let a holder invalidate the ``__post_init__``
    guarantees (validated order, applicability coverage) that were checked
    only at construction time.
    """
    # Local imports: profile is the module under construction; a module-level
    # import would fail collection for every other test in this file.
    from sluice.core.mock_gates import MOCK_GATES
    from sluice.core.profile import Applicability, Profile

    profile = Profile(
        pipeline_version="reference-v1",
        gates=MOCK_GATES,
        applicability={
            "mock_alpha": Applicability.REQUIRED,
            "mock_beta": Applicability.REQUIRED,
        },
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        profile.gates = ()


def test_profile_runs_gates_through_validate_gates():
    """Constructing a ``Profile`` with a B3-inverted gate set raises ``ValueError`` naming both gates.

    The B3 extension (plan 05 unit 2 / errata B3): python-poller's
    ``PIPELINE_GATE_COLUMNS`` (``poller_state_db.py:32-42``) lists download
    before the registration that must execute first, and transcribing that
    column order into ``Gate.order`` yields a needer ordered *before* its need
    — a profile that deadlocks the daemon, because the need can never be
    satisfied in time. ``Profile.__post_init__`` must run its gate set through
    ``validate_gates`` so this shape fails at profile construction, with a
    message naming both ends of the inverted edge, not at 2am in the daemon.
    The applicability map covers both gates completely, so the ordering
    violation is the only construction error this profile can raise.
    """
    # Local imports: profile is the module under construction; a module-level
    # import would fail collection for every other test in this file.
    from sluice.core.gate import Gate, GateOutcome, GateResult
    from sluice.core.profile import Applicability, Profile

    # The PP trap shape: the needer's order is LOWER than its need's order.
    download_like = Gate(
        id="download",
        name="Download-shaped gate",
        satisfied_column="download_done",
        order=1,
        needs=("registration",),
        produces=(),
        run=lambda context: GateResult(GateOutcome.SUCCESS),
        invalidate=lambda context: None,
    )
    registration_like = Gate(
        id="registration",
        name="Registration-shaped gate",
        satisfied_column="registration_done",
        order=2,
        needs=(),
        produces=(),
        run=lambda context: GateResult(GateOutcome.SUCCESS),
        invalidate=lambda context: None,
    )

    with pytest.raises(ValueError) as raised:
        Profile(
            pipeline_version="deadlock-v1",
            gates=(download_like, registration_like),
            applicability={
                "download": Applicability.REQUIRED,
                "registration": Applicability.REQUIRED,
            },
        )

    # The message must name both sides of the inverted edge so a profile
    # author can locate the transcription error (propagated from
    # validate_gates, plan 02 unit 4).
    message = str(raised.value)
    assert "download" in message
    assert "registration" in message


def test_profile_stores_gates_in_execution_order():
    """A ``Profile`` built with its gates listed out of order stores them ascending by ``Gate.order``.

    Reconcile consumes ``profile.gates`` as the execution sequence (plan 05
    unit 2 / §3.3), so a registry author's *listing* order must never leak
    into scheduling. The regression this catches: an implementer who calls
    ``validate_gates`` for its checks but discards its sorted return — the
    profile would validate clean yet hand reconcile the author's arbitrary
    ordering.
    """
    # Local imports: profile is the module under construction; a module-level
    # import would fail collection for every other test in this file.
    from sluice.core.mock_gates import MOCK_ALPHA, MOCK_BETA
    from sluice.core.profile import Applicability, Profile

    profile = Profile(
        pipeline_version="reference-v1",
        # Deliberately reversed: beta (order=2) listed before alpha (order=1).
        gates=(MOCK_BETA, MOCK_ALPHA),
        applicability={
            "mock_alpha": Applicability.REQUIRED,
            "mock_beta": Applicability.REQUIRED,
        },
    )
    assert profile.gates == (MOCK_ALPHA, MOCK_BETA)


@pytest.mark.parametrize(
    ("applicability_gate_ids", "offending_gate_id"),
    [
        # (a) mock_beta is in gates but has NO applicability entry — a gate
        # reconcile could not classify at selection time.
        pytest.param(
            ("mock_alpha",),
            "mock_beta",
            id="gate-without-applicability-entry",
        ),
        # (b) mock_gamma has an applicability entry but is NOT in gates — a
        # typo'd gate id that would otherwise be silently ignored.
        pytest.param(
            ("mock_alpha", "mock_beta", "mock_gamma"),
            "mock_gamma",
            id="stray-applicability-key",
        ),
    ],
)
def test_profile_applicability_must_cover_exactly_the_gates(
    applicability_gate_ids, offending_gate_id
):
    """Constructing a ``Profile`` whose applicability keys ≠ its gate ids raises ``ValueError`` naming the offender.

    ``set(applicability) == {g.id for g in gates}`` exactly, both directions
    (plan 05 unit 2 / §3.1 step 2). A gate with no applicability entry is a
    gate reconcile cannot classify — its ``profile.applicability[gate.id]``
    lookup would ``KeyError`` mid-selection (§3.3 promises that lookup never
    misses). A stray applicability key is almost always a typo'd gate id: the
    author believed they classified a real gate, and silently ignoring the
    stray would let that belief stand. Both must fail loud at construction,
    with the offending id in the message so the author can locate it.
    """
    # Local imports: profile is the module under construction; a module-level
    # import would fail collection for every other test in this file.
    from sluice.core.mock_gates import MOCK_GATES
    from sluice.core.profile import Applicability, Profile

    applicability = {
        gate_id: Applicability.REQUIRED for gate_id in applicability_gate_ids
    }
    with pytest.raises(ValueError) as raised:
        Profile(
            pipeline_version="reference-v1",
            gates=MOCK_GATES,
            applicability=applicability,
        )
    assert offending_gate_id in str(raised.value)


def test_profile_rejects_a_non_applicability_value():
    """Constructing a ``Profile`` with a bare-string applicability value raises ``TypeError``.

    §3.1 step 3 (plan 05 unit 2): every ``applicability`` value must be an
    ``Applicability`` member, not the member's string value. The regression
    this catches: reconcile branches with ``is`` on the enum (``applicability
    is Applicability.DISABLED``), so a bare ``"required"`` that slipped
    through construction would compare unequal to every member and
    mis-branch silently at selection time — the profile must fail loud at
    construction instead (mirror of ``GateResult``'s outcome type check,
    plan 02 unit 2).
    """
    # Local imports: profile is the module under construction; a module-level
    # import would fail collection for every other test in this file.
    from sluice.core.mock_gates import MOCK_GATES
    from sluice.core.profile import Applicability, Profile

    with pytest.raises(TypeError):
        Profile(
            pipeline_version="reference-v1",
            gates=MOCK_GATES,
            applicability={
                # The trap: the enum member's VALUE, not the member itself.
                "mock_alpha": "required",
                "mock_beta": Applicability.REQUIRED,
            },
        )


def test_profile_applicability_is_read_only():
    """``profile.applicability`` rejects mutation and is a snapshot decoupled from the caller's dict.

    §3.1 step 4 (plan 05 unit 2): the constructor stores
    ``MappingProxyType(dict(applicability))`` — a read-only view over a private
    copy. The regression this catches: a live shared applicability map would
    let any one holder silently re-classify a gate (say, flip ``mock_alpha``
    to DISABLED) for every other holder — a second mutable in-memory authority
    over which gates apply, which the spine forbids (mirror of
    ``RunContext.config``, plan 04 unit 1). Both halves matter: the proxy
    blocks writes *through* the profile, and the ``dict(...)`` snapshot blocks
    writes *around* it via the constructor argument the author still holds.
    """
    # Local imports: profile is the module under construction; a module-level
    # import would fail collection for every other test in this file.
    from sluice.core.mock_gates import MOCK_GATES
    from sluice.core.profile import Applicability, Profile

    author_held_applicability = {
        "mock_alpha": Applicability.REQUIRED,
        "mock_beta": Applicability.REQUIRED,
    }
    profile = Profile(
        pipeline_version="reference-v1",
        gates=MOCK_GATES,
        applicability=author_held_applicability,
    )

    # Writes THROUGH the profile: assignment and deletion both refused.
    with pytest.raises(TypeError):
        profile.applicability["mock_alpha"] = Applicability.DISABLED
    with pytest.raises(TypeError):
        del profile.applicability["mock_alpha"]

    # Writes AROUND the profile: mutating the author's original dict must not
    # show through — the profile snapshotted at construction.
    author_held_applicability["mock_alpha"] = Applicability.DISABLED
    assert profile.applicability["mock_alpha"] is Applicability.REQUIRED


def test_build_profile_splits_pairs_into_gates_and_applicability():
    """``build_profile`` splits ``(gate, applicability)`` pairs into ``Profile``'s two parallel collections.

    The pairs form is the authoring convenience (plan 05 unit 3): keeping a
    gate and its applicability *adjacent* prevents the "gates list and
    applicability map drift out of sync" bug that two separate arguments
    invite — an author cannot classify a gate they did not list, nor list a
    gate they did not classify. The factory is a thin splitter that delegates
    every check to ``Profile.__post_init__`` (§3.1 layering, mirror of
    ``Gate.__post_init__`` + ``validate_gates``, plan 02), so the resulting
    record carries exactly the version string, the gates in execution order,
    and each gate's own applicability keyed by its id.
    """
    # Local imports: build_profile is the unit under construction; a
    # module-level import would fail collection for every other test here.
    from sluice.core.mock_gates import MOCK_ALPHA, MOCK_BETA
    from sluice.core.profile import Applicability, build_profile

    profile = build_profile(
        "reference-v1",
        [(MOCK_ALPHA, Applicability.REQUIRED), (MOCK_BETA, Applicability.DISABLED)],
    )
    assert profile.pipeline_version == "reference-v1"
    assert profile.gates == (MOCK_ALPHA, MOCK_BETA)
    # Distinct applicability per gate proves each pair's value landed on its
    # OWN gate id — a splitter that zipped them wrong would swap the two.
    assert dict(profile.applicability) == {
        "mock_alpha": Applicability.REQUIRED,
        "mock_beta": Applicability.DISABLED,
    }


def test_build_profile_rejects_a_duplicate_gate():
    """``build_profile`` with two pairs naming the same gate raises ``ValueError`` naming that gate.

    The load-bearing part (plan 05 unit 3): the naive ``{g.id: a for g, a in
    entries}`` applicability dict would silently drop one of the duplicate
    entries — the collapsed one-gate set would then construct clean, hiding
    the authoring bug (and, when the two pairs disagree, silently letting one
    classification win). ``build_profile`` must keep every entry in the gates
    tuple it hands to ``Profile``, so ``validate_gates``' duplicate-id check
    fires at construction and the collapsed set cannot be smuggled past it.
    """
    # Local imports: build_profile is the unit under construction; a
    # module-level import would fail collection for every other test here.
    from sluice.core.mock_gates import MOCK_ALPHA
    from sluice.core.profile import Applicability, build_profile

    with pytest.raises(ValueError) as raised:
        build_profile(
            "duplicate-v1",
            [
                (MOCK_ALPHA, Applicability.REQUIRED),
                # The same gate listed twice, with a DISAGREEING applicability:
                # a silent collapse would also pick one side of this conflict.
                (MOCK_ALPHA, Applicability.DISABLED),
            ],
        )
    assert "mock_alpha" in str(raised.value)


@pytest.mark.parametrize(
    ("alpha_applicability_name", "beta_applicability_name", "expected_gate_ids"),
    [
        # Both required → both returned, in EXECUTION order (the order
        # validate_gates stored, not any authoring order).
        pytest.param(
            "REQUIRED", "REQUIRED", ("mock_alpha", "mock_beta"), id="both-required"
        ),
        # A DISABLED gate is out of the contract — satisfied by absence
        # (ADR-0004 d.2), so it can never block completion — the B1 fix for
        # the nvd_auto_run=false loop-forever bug (PP poller.py:1229-1237).
        pytest.param("DISABLED", "REQUIRED", ("mock_beta",), id="disabled-excluded"),
    ],
)
def test_required_gates_returns_only_required_in_order(
    alpha_applicability_name, beta_applicability_name, expected_gate_ids
):
    """``required_gates`` returns exactly the contract — the REQUIRED subset — in execution order.

    The completion-predicate seam (plan 05 unit 4 / §3.3, amended by
    ADR-0004): reconcile's "complete" is ``all(row[g.satisfied_column] for g
    in required_gates(profile))``, so what this function returns *is* the set
    of gates that can block completion. The regressions this catches: an
    implementation that includes DISABLED gates would resurrect the B1
    loop-forever bug (a disabled gate's column stays 0 forever, so the run
    never completes); and one that loses execution order would hand reconcile
    an unordered seam.
    """
    # Local imports: required_gates is the unit under construction; a
    # module-level import would fail collection for every other test here.
    from sluice.core.mock_gates import MOCK_ALPHA, MOCK_BETA
    from sluice.core.profile import Applicability, build_profile, required_gates

    profile = build_profile(
        "reference-v1",
        [
            (MOCK_ALPHA, Applicability[alpha_applicability_name]),
            (MOCK_BETA, Applicability[beta_applicability_name]),
        ],
    )
    assert tuple(gate.id for gate in required_gates(profile)) == expected_gate_ids


@pytest.mark.parametrize(
    "uniform_applicability_name",
    [
        pytest.param("DISABLED", id="all-disabled"),
    ],
)
def test_required_gates_is_empty_for_an_all_disabled_profile(
    uniform_applicability_name,
):
    """A profile with no REQUIRED gate yields ``required_gates(profile) == ()`` — the vacuous-completion edge.

    Plan 05 unit 4 / §3.3, amended by ADR-0004: an all-disabled profile has an
    empty contract, so reconcile's completion predicate
    ``all(row[g.satisfied_column] for g in required_gates(profile))`` is
    ``all([])`` — vacuously true, the run complete at detection. That edge is
    real and legitimate, and this test pins it as a *fact reconcile must
    handle* (§7 carry-forward), not an accident. The regression it catches: a
    defensive implementation that treats an empty contract as an authoring
    error — falling back to every gate, or raising — would turn a
    deliberately all-disabled profile into one whose gates silently block
    completion after all.
    """
    # Local imports: required_gates is the unit under construction; a
    # module-level import would fail collection for every other test here.
    from sluice.core.mock_gates import MOCK_ALPHA, MOCK_BETA
    from sluice.core.profile import Applicability, build_profile, required_gates

    uniform_applicability = Applicability[uniform_applicability_name]
    profile = build_profile(
        "reference-v1",
        [(MOCK_ALPHA, uniform_applicability), (MOCK_BETA, uniform_applicability)],
    )
    assert required_gates(profile) == ()


def test_load_profile_resolves_by_version_string():
    """``load_profile`` returns the registry's profile for exactly the requested ``pipeline_version``.

    The freeze's resolution seam (plan 05 unit 5 / §2 fact 1): a run always
    completes under the definition it started with, so ``load_profile``
    resolves the version string already stamped into ``runs.pipeline_version``
    against an explicitly-injected registry — nothing else. With TWO profiles
    registered, asserting identity (``is``) on the one whose key was asked for
    proves the lookup keys on the *argument*, not on some favored/first/default
    entry — the regression where a hardcoded reference profile (or the first
    registry entry) is returned regardless of the stamped version would
    silently reinterpret every in-flight run against the wrong definition
    (design §8). Registries are built inline with ``build_profile`` over the
    mock gates (available since unit 3), so a failure here is a behavioral RED
    for ``load_profile`` itself, not a missing unit-6 constant.
    """
    # Local imports: load_profile is the unit under construction; a
    # module-level import would fail collection for every other test here.
    from sluice.core.mock_gates import MOCK_ALPHA, MOCK_BETA
    from sluice.core.profile import Applicability, build_profile, load_profile

    reference = build_profile(
        "reference-v1",
        [(MOCK_ALPHA, Applicability.REQUIRED), (MOCK_BETA, Applicability.REQUIRED)],
    )
    other = build_profile(
        "other-v1",
        [(MOCK_ALPHA, Applicability.DISABLED), (MOCK_BETA, Applicability.REQUIRED)],
    )
    registry = {"reference-v1": reference, "other-v1": other}

    assert load_profile("reference-v1", registry) is reference


def test_load_profile_fails_loud_on_an_unknown_version():
    """``load_profile`` on an unregistered version raises ``UnknownProfileError`` (a ``KeyError``) naming it — never a fallback.

    THE freeze (plan 05 unit 5 / §2 fact 1 / design §8): a run frozen to a
    ``pipeline_version`` the injected registry does not carry is an error to
    surface, not a run to silently reinterpret against the active default —
    so the regression this catches is any fallback branch (return the sole
    registered profile, a favored ``"reference-v1"``, a ``.get`` default)
    that would quietly complete an in-flight run under a definition it never
    started with (guardrail §8 no-silent-fallback). The exception type is
    itself contract: a dedicated ``UnknownProfileError`` lets the daemon
    catch it explicitly and quarantine that one run (§7 carry-forward), and
    ``isinstance(exc, KeyError)`` pins the mirror of ``UnknownColumnError`` —
    plus the caller hazard that a bare ``except KeyError`` swallows it. The
    message carries the requested version and the known ids so the 2am
    operator sees *what* was asked and *what* exists.
    """
    # Local imports: load_profile is the unit under construction; a
    # module-level import would fail collection for every other test here.
    from sluice.core.mock_gates import MOCK_ALPHA, MOCK_BETA
    from sluice.core.profile import (
        Applicability,
        UnknownProfileError,
        build_profile,
        load_profile,
    )

    registry = {
        "reference-v1": build_profile(
            "reference-v1",
            [
                (MOCK_ALPHA, Applicability.REQUIRED),
                (MOCK_BETA, Applicability.REQUIRED),
            ],
        )
    }

    with pytest.raises(UnknownProfileError) as raised:
        load_profile("ghost-v9", registry)

    # Mirror UnknownColumnError: the dedicated type IS a KeyError, so callers
    # can catch it precisely — and must, because a bare `except KeyError`
    # would swallow it (the daemon's quarantine obligation, plan 05 §7).
    assert isinstance(raised.value, KeyError)

    # The message teaches: the version that was asked for, and the ids the
    # registry actually carries.
    message = str(raised.value)
    assert "ghost-v9" in message
    assert "reference-v1" in message


def test_load_profile_rejects_a_key_id_mismatch():
    """``load_profile`` on a registry key that ≠ the resolved profile's own ``pipeline_version`` raises ``ValueError`` naming both.

    Registry coherence (plan 05 unit 5 / §3.2): ``load_profile`` is not a bare
    ``registry[key]`` — after resolving, it must verify the resolved profile's
    own ``pipeline_version`` equals the lookup key. The regression this
    catches: without the check, an injected entry whose key does not equal its
    profile's id silently resolves every run stamped with that key against the
    *wrong definition* — exactly the class of bug the detection-time freeze
    exists to prevent (§2 fact 1 / design §8). The message must name BOTH the
    lookup key and the profile's id so the mis-assembled entry can be located.
    This is the per-run backstop only: the duplicate-key half (a production
    profile shadowing ``"reference-v1"`` at merge time) is the daemon's
    assembly-time guard (§7), which this check cannot see.
    """
    # Local imports: load_profile is the unit under construction; a
    # module-level import would fail collection for every other test here.
    from sluice.core.mock_gates import MOCK_ALPHA, MOCK_BETA
    from sluice.core.profile import Applicability, build_profile, load_profile

    # The mis-assembled registry: the key "mislabeled" does not equal the
    # profile's own pipeline_version "reference-v1".
    registry = {
        "mislabeled": build_profile(
            "reference-v1",
            [
                (MOCK_ALPHA, Applicability.REQUIRED),
                (MOCK_BETA, Applicability.REQUIRED),
            ],
        )
    }

    with pytest.raises(ValueError) as raised:
        load_profile("mislabeled", registry)

    # Both halves of the mismatch in the message: the key the run was stamped
    # with, and the id the resolved profile actually claims to define.
    message = str(raised.value)
    assert "mislabeled" in message
    assert "reference-v1" in message


def test_load_profile_requires_an_explicit_registry():
    """``load_profile``'s signature is exactly ``(pipeline_version, registry)`` with no default registry.

    No production default baked in (plan 05 unit 5 / guardrail §8): the
    explicit registry is the injection seam — core cannot import
    ``sluice.gates``, so the daemon must assemble the full registry
    (reference + production profiles) and pass it in (§7 carry-forward). The
    regression this catches: a defaulted ``registry`` (say
    ``= REFERENCE_PROFILES``) would let a Plan B daemon call
    ``load_profile(version)`` and silently resolve only reference profiles —
    every production-stamped run would fail (or, worse, misresolve) without
    anyone having consciously chosen that. Editing this signature to add a
    default is a conscious contract change.
    """
    # Local import: load_profile is the unit under construction; a
    # module-level import would fail collection for every other test here.
    from sluice.core.profile import load_profile

    load_profile_parameters = inspect.signature(load_profile).parameters
    assert list(load_profile_parameters) == ["pipeline_version", "registry"]
    assert load_profile_parameters["registry"].default is inspect.Parameter.empty, (
        "registry must have no default — the daemon injects it (plan 05 unit 5)"
    )


def test_reference_profile_covers_the_two_mock_gates_both_required():
    """``REFERENCE_PROFILE`` is ``"reference-v1"`` over both mock gates, both REQUIRED.

    Decided fact 3 / design §12 (plan 05 unit 6): the shipped reference
    profile is what lets the daemon and the control ops be exercised
    end-to-end with no external services — so it must carry exactly the two
    mock gates in execution order, and BOTH must be required, or the
    full-loop test would "complete" without proving both gates run. The id
    ``"reference-v1"`` is the established cross-subsystem constant (the db
    plan stamps it into ``runs.pipeline_version`` and seeds
    ``metadata.pipeline_profile`` with it, §3.2), so it is pinned exactly.
    """
    # Local imports: REFERENCE_PROFILE is the unit under construction; a
    # module-level import would fail collection for every other test here.
    from sluice.core.mock_gates import MOCK_GATES
    from sluice.core.profile import REFERENCE_PROFILE, required_gates

    assert REFERENCE_PROFILE.pipeline_version == "reference-v1"
    assert REFERENCE_PROFILE.gates == MOCK_GATES
    # Both mock gates REQUIRED: the reference run completes only when both
    # have actually run to success (§12) — a disabled slip here would let
    # the end-to-end proof pass with a gate never executed.
    assert required_gates(REFERENCE_PROFILE) == MOCK_GATES


def test_reference_disabled_variant_keeps_the_disabled_gate_in_the_set():
    """``REFERENCE_PROFILE_ALPHA_DISABLED`` disables alpha yet keeps it in the gate set — disabling ≠ omitting.

    ⚑2's exemplar (plan 05 unit 6, decided 2026-07-23: two profiles ship;
    §2 B1's corollary): turning a step off means keeping the gate in the
    ordered set with ``Applicability.DISABLED``, never dropping its id — beta's
    ``needs=("mock_alpha",)`` must still resolve (needs-closure in
    ``validate_gates`` would raise if alpha were omitted). The variant disables
    the *needed* gate deliberately, so the full loop proves BOTH halves of
    disabled-is-satisfied: reconcile must unblock beta from *applicability*
    (alpha's column stays 0 forever), and completion is beta-only —
    ``required_gates`` excluding the disabled gate is the B1 fix for the
    nvd_auto_run=false loop-forever bug (PP ``poller.py:1229-1237``). The
    regression this catches: a variant that omits alpha, or one that lets it
    leak into the required subset (which would resurrect loop-forever).
    """
    # Local imports: REFERENCE_PROFILE_ALPHA_DISABLED is the unit under
    # construction; a module-level import would fail collection for every
    # other test in this file.
    from sluice.core.mock_gates import MOCK_BETA, MOCK_GATES
    from sluice.core.profile import (
        REFERENCE_PROFILE_ALPHA_DISABLED,
        Applicability,
        required_gates,
    )

    assert (
        REFERENCE_PROFILE_ALPHA_DISABLED.pipeline_version
        == "reference-v1-alpha-disabled"
    )
    # Alpha is STILL IN the gate set: disabling ≠ omitting (§2 B1). Dropping
    # it would break beta's needs-closure at construction — and, worse, would
    # leave nothing for reconcile's disabled-is-satisfied treatment to prove.
    assert REFERENCE_PROFILE_ALPHA_DISABLED.gates == MOCK_GATES
    assert (
        REFERENCE_PROFILE_ALPHA_DISABLED.applicability["mock_alpha"]
        is Applicability.DISABLED
    )
    assert (
        REFERENCE_PROFILE_ALPHA_DISABLED.applicability["mock_beta"]
        is Applicability.REQUIRED
    )
    # The disabled gate is excluded from the completion predicate while
    # remaining in the profile's gate set: completion is beta-only.
    assert required_gates(REFERENCE_PROFILE_ALPHA_DISABLED) == (MOCK_BETA,)


def test_reference_registry_is_keyed_by_pipeline_version():
    """``REFERENCE_PROFILES`` maps each shipped profile's own ``pipeline_version`` to that profile — both entries, distinct keys.

    ⚑2 (plan 05 unit 6, decided 2026-07-23: two profiles ship): the registry
    is the seam ``load_profile`` keys on and the daemon extends (§3.2), so it
    must carry BOTH shipped profiles coexisting under their own ids — exact
    dict equality pins that nothing is missing, nothing extra is smuggled in,
    and neither entry shadows the other. The key==pipeline_version half is
    registry coherence at the source: every entry must pass ``load_profile``'s
    key-equals-id check (which rejects a mismatch with ``ValueError``), so a
    run stamped from this registry can never resolve to a profile that is not
    its own definition. The regression this catches: a one-entry registry
    (dropping the ⚑2 variant), or an entry keyed by anything other than its
    profile's own id — either would make the daemon's assembled registry
    incoherent before a single production profile is merged.
    """
    # Local imports: REFERENCE_PROFILES is the unit under construction; a
    # module-level import would fail collection for every other test here.
    from sluice.core.profile import (
        REFERENCE_PROFILE,
        REFERENCE_PROFILE_ALPHA_DISABLED,
        REFERENCE_PROFILES,
    )

    assert dict(REFERENCE_PROFILES) == {
        "reference-v1": REFERENCE_PROFILE,
        "reference-v1-alpha-disabled": REFERENCE_PROFILE_ALPHA_DISABLED,
    }
    # Every entry keyed by its profile's OWN pipeline_version: a key/id
    # mismatch (which load_profile rejects per run, §3.2) cannot originate
    # from the reference registry.
    assert all(
        registry_key == profile.pipeline_version
        for registry_key, profile in REFERENCE_PROFILES.items()
    )


# --------------------------------------------------------------------------
# Unit 7 — subsystem-closing surface + real-``sqlite3.Row`` integration.
#
# Green-on-arrival by design (plan 05 §4; the plan-02 unit-9 carve-out
# convention): no production code beyond the final ``__all__``, which units 1-6
# grew as names landed. These tests import ``sluice.core.db`` — the *module
# under test* still does not (unit 1's import-hygiene test stands).
# --------------------------------------------------------------------------


def test_profile_exports_exactly_the_documented_surface():
    """``sluice.core.profile.__all__`` names exactly the plan §1 nine-name surface.

    Makes §1's "``__all__`` names exactly the surface" executable, catching
    future scope creep in either direction — a production profile leaking into
    Plan A (fact 4 / B1), a retry knob (B4), a config hook (fact 4), or a name
    quietly dropped that a downstream subsystem (reconcile, daemon) was
    promised (§6 done criterion 7). Set equality, per the db unit-7 precedent
    (``test_db_exports_exactly_the_documented_surface``).
    """
    # Local import: keeps the whole file collectable even if the module
    # regresses, matching every other test here.
    import sluice.core.profile

    documented_surface = {
        # The applicability vocabulary + the profile record.
        "Applicability",
        "Profile",
        # Authoring factory, completion-predicate seam, registry resolver.
        "build_profile",
        "required_gates",
        "load_profile",
        # The resolver's fail-loud error.
        "UnknownProfileError",
        # The two shipped Plan A reference profiles (⚑2) + their registry.
        "REFERENCE_PROFILE",
        "REFERENCE_PROFILE_ALPHA_DISABLED",
        "REFERENCE_PROFILES",
    }
    assert set(sluice.core.profile.__all__) == documented_surface


def test_reference_profile_resolves_from_a_real_run_row(tmp_path):
    """A ``pipeline_version`` frozen into a real ``runs`` row resolves each run to its own shipped profile; an unregistered version fails loud.

    The load-bearing seam (plan 05 unit 7): the daemon's loop is
    ``load_profile(row["pipeline_version"], registry)`` off a real
    ``sqlite3.Row``, so this test drives the string through the real schema —
    ``initialize_database`` + ``insert_run`` + ``get_run``, a real WAL file
    under ``tmp_path`` — and a drift in either the column name or the shipped
    profile ids fails here, not in the daemon at 2am. Three legs, one DB:
    a run stamped ``"reference-v1"`` resolves to ``REFERENCE_PROFILE``; the ⚑2
    variant leg, a run stamped ``"reference-v1-alpha-disabled"``, resolves to
    ``REFERENCE_PROFILE_ALPHA_DISABLED`` — two runs frozen to *different*
    shipped definitions coexist in one DB, each resolving to its own (§8
    freeze-at-detection is per run, not per DB); and the freeze leg, a run
    stamped ``"drifted-v9"``, raises ``UnknownProfileError`` — never a silent
    reinterpretation against ``"reference-v1"`` (§2 fact 1).
    """
    import contextlib

    # Local imports: this test alone may import sluice.core.db — the module
    # under test must not (unit 1's import-hygiene test enforces that).
    import sluice.core.db
    from sluice.core.profile import (
        REFERENCE_PROFILE,
        REFERENCE_PROFILE_ALPHA_DISABLED,
        REFERENCE_PROFILES,
        UnknownProfileError,
        load_profile,
    )

    db_path = tmp_path / "state.db"
    sluice.core.db.initialize_database(db_path)

    with contextlib.closing(sluice.core.db.get_connection(db_path)) as connection:
        # Three runs frozen at detection, coexisting in the one runs table:
        # two to the two shipped definitions, one to a version the registry
        # does not carry.
        sluice.core.db.insert_run(connection, "r1", pipeline_version="reference-v1")
        sluice.core.db.insert_run(
            connection, "r3", pipeline_version="reference-v1-alpha-disabled"
        )
        sluice.core.db.insert_run(connection, "r2", pipeline_version="drifted-v9")

        # Leg 1: the stamped string, read back off a real sqlite3.Row through
        # the real column name, resolves to the reference profile — identity,
        # not equality, because the run must get THE shipped definition.
        reference_row = sluice.core.db.get_run(connection, "r1")
        assert (
            load_profile(reference_row["pipeline_version"], REFERENCE_PROFILES)
            is REFERENCE_PROFILE
        )

        # Leg 2 (⚑2 variant): a second run in the same DB, frozen to the other
        # shipped definition, resolves to ITS profile — each run completes
        # under the definition it started with, side by side.
        variant_row = sluice.core.db.get_run(connection, "r3")
        assert (
            load_profile(variant_row["pipeline_version"], REFERENCE_PROFILES)
            is REFERENCE_PROFILE_ALPHA_DISABLED
        )

        # Leg 3 (the freeze): a run frozen to a version the registry does not
        # carry fails loud — never silently reinterpreted against
        # "reference-v1" (§8; guardrail §8 no-silent-fallback).
        drifted_row = sluice.core.db.get_run(connection, "r2")
        with pytest.raises(UnknownProfileError):
            load_profile(drifted_row["pipeline_version"], REFERENCE_PROFILES)
