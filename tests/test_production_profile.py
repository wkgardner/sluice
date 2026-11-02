"""The shipped production profiles — the registry entries live runs are stamped with.

Three versions ship side by side: ``production-v3.1`` (the original five gates, frozen),
``production-v3.2`` (v3.1 plus ``profiling``, frozen) and ``production-v3.3`` (v3.2 plus
``registration``). A run's ``pipeline_version`` is stamped once at detection, so a shipped version
must stay resolvable and unchanged for every run already stamped with it — adding a gate to one in
place would hand every historical run, completed ones included, a new unsatisfied gate.

Detection stamps ``runs.pipeline_version`` from ``metadata.pipeline_profile``, and the daemon
resolves that string against its assembled registry on every look. A value with no registry entry
raises ``UnknownProfileError`` and quarantines the run at the daemon's FIRST look — so this profile
existing, resolving, and naming only real schema columns is the precondition for sluice ever
running live (gate map §8 step 1 caveat).

The profile grows one gate per increment, matching the incremental-migration policy (plan 13 ⚑3):
a gate and its satisfaction column land together, and the gate joins this profile in the same
increment. Gates not yet built are simply absent — NOT listed as DISABLED, because a ``Gate`` object
has to exist to be listed at all.
"""

import sqlite3

import pytest

from sluice.core.db import initialize_database
from sluice.core.profile import (
    Applicability,
    UnknownProfileError,
    load_profile,
    required_gates,
)

PRODUCTION_VERSION = "production-v3.1"
V3_2_VERSION = "production-v3.2"
CURRENT_VERSION = "production-v3.3"


def test_production_profile_resolves_from_the_shipped_registry():
    """``load_profile("production-v3.1", PRODUCTION_PROFILES)`` returns the profile.

    The single fact that unblocks going live. Until this passes, any run detection stamps
    ``production-v3.1`` onto quarantines on ``UnknownProfileError`` at the daemon's first look,
    before a single gate runs.
    """
    from sluice.gates.profiles import PRODUCTION_PROFILES

    profile = load_profile(PRODUCTION_VERSION, PRODUCTION_PROFILES)

    assert profile.pipeline_version == PRODUCTION_VERSION


def test_production_profile_is_keyed_by_its_own_version():
    """The registry key is the profile's own ``pipeline_version``, never a hand-typed string.

    Mirrors the ``REFERENCE_PROFILES`` guarantee (plan 05 ⚑2): a key/id mismatch is rejected by
    ``load_profile`` per run, so it must be impossible to introduce here.
    """
    from sluice.gates.profiles import PRODUCTION_PROFILES

    for registry_key, profile in PRODUCTION_PROFILES.items():
        assert registry_key == profile.pipeline_version, (
            f"registry key {registry_key!r} does not match its profile's "
            f"pipeline_version {profile.pipeline_version!r} — every entry must be "
            "keyed by the profile's own attribute."
        )


def test_experiment_gate_is_in_the_contract_at_order_zero():
    """The ``experiment`` gate is REQUIRED and runs first.

    Gate 0 creates the LabKey experiment and persists ``experiment_id``, the key a human uses to
    find a run. Every later gate depends on it having happened.
    """
    from sluice.gates.profiles import PRODUCTION_PROFILES

    profile = load_profile(PRODUCTION_VERSION, PRODUCTION_PROFILES)
    contract = required_gates(profile)

    assert contract, "the production contract must not be empty"
    assert contract[0].id == "experiment"
    assert contract[0].order == 0
    assert profile.applicability["experiment"] is Applicability.REQUIRED


def test_every_profile_gate_column_exists_in_the_schema(tmp_path):
    """Every gate's ``satisfied_column`` is a real column on ``runs`` at migration head.

    The drift guard that matters most in production. ``reconcile`` raises ``KeyError`` naming any
    profile gate whose column is absent from the run row, and it does so on EVERY look — so a
    profile naming a column no migration created would fail every run, continuously, with a
    schema-drift error rather than anything a gate could recover from.

    This is exactly the mistake the incremental-migration policy invites: adding a gate to the
    profile while forgetting the migration that adds its column.
    """
    from sluice.gates.profiles import PRODUCTION_PROFILES

    database_path = tmp_path / "state.db"
    initialize_database(database_path)
    connection = sqlite3.connect(database_path)
    try:
        runs_columns = {row[1] for row in connection.execute("PRAGMA table_info(runs)")}
    finally:
        connection.close()

    for profile in PRODUCTION_PROFILES.values():
        for gate in profile.gates:
            assert gate.satisfied_column in runs_columns, (
                f"gate {gate.id!r} of profile {profile.pipeline_version!r} names "
                f"satisfied_column {gate.satisfied_column!r}, which is not a column "
                "of runs at migration head — reconcile would raise KeyError on every "
                "look. The gate's migration is missing."
            )


def test_deferred_gates_are_absent_rather_than_silently_required():
    """Gates not yet built are absent from the contract, so a run completes at the last built gate.

    ADR-0004 read forward: the contract is exactly the profile's non-disabled gates, so a run whose
    built gates all succeed completes **deliberately** — never a silent "final gate lies" (C4).
    This is what makes shipping a partial pipeline legitimate rather than a hack, and it is the
    property the whole MVP sequencing rests on (plan 15 §3a).

    Concretely: while ``download`` is unbuilt it is not in the contract, so a run that finishes
    ``experiment`` is complete and says so.
    """
    from sluice.gates.profiles import PRODUCTION_PROFILES

    profile = load_profile(CURRENT_VERSION, PRODUCTION_PROFILES)
    contract_ids = {gate.id for gate in required_gates(profile)}

    assert "globus_submit" not in contract_ids, (
        "globus_submit is deferred (plan 15 §3a) and must not block completion"
    )
    assert "globus_verify" not in contract_ids, (
        "globus_verify is deferred (plan 15 §3a) and must not block completion"
    )


def test_an_unknown_production_version_still_raises():
    """A typo'd version does not silently resolve to the production profile.

    Guards against a registry built with a permissive default. The loud
    ``UnknownProfileError`` is what turns a mis-stamped run into one visible quarantine instead of
    a run silently processed under the wrong contract.
    """
    from sluice.gates.profiles import PRODUCTION_PROFILES

    with pytest.raises(UnknownProfileError):
        load_profile("production-v9.9", PRODUCTION_PROFILES)


def test_v3_1_is_frozen_without_the_profiling_gate():
    """``production-v3.1`` keeps exactly its original five gates, in order.

    Runs are stamped once; changing this contract would reach back into every run already
    stamped v3.1 — and completed runs stay daemon-eligible (F4), so they would re-run the new
    gate.
    """
    from sluice.gates.profiles import PRODUCTION_PROFILES

    profile = load_profile(PRODUCTION_VERSION, PRODUCTION_PROFILES)

    assert [gate.id for gate in profile.gates] == [
        "experiment",
        "download",
        "checksum",
        "samplesheet",
        "nvd",
    ]


def test_v3_2_is_frozen_without_the_registration_gate():
    """``production-v3.2`` is v3.1 plus ``profiling`` (order 4), REQUIRED, before ``nvd`` — no more."""
    from sluice.gates.profiles import PRODUCTION_PROFILES

    profile = load_profile(V3_2_VERSION, PRODUCTION_PROFILES)
    contract_ids = [gate.id for gate in required_gates(profile)]

    assert contract_ids == [
        "experiment",
        "download",
        "checksum",
        "samplesheet",
        "profiling",
        "nvd",
    ]
    assert profile.applicability["profiling"] is Applicability.REQUIRED
    assert [gate.order for gate in profile.gates] == [0, 1, 2, 3, 4, 5]


def test_v3_3_registers_samples_after_samplesheet_and_before_profiling():
    """``production-v3.3`` is v3.2 plus ``registration``, every gate REQUIRED.

    Registration sits right after samplesheet because it reads the samplesheet, and it comes
    before the slow compute so a registration problem surfaces before hours of pipeline work.
    """
    from sluice.gates.profiles import PRODUCTION_PROFILES

    profile = load_profile(CURRENT_VERSION, PRODUCTION_PROFILES)

    assert [gate.id for gate in required_gates(profile)] == [
        "experiment",
        "download",
        "checksum",
        "samplesheet",
        "registration",
        "profiling",
        "nvd",
    ]
    assert [gate.order for gate in profile.gates] == [0, 1, 2, 3, 4, 5, 6]


def test_renumbering_for_v3_3_leaves_the_shipped_gate_objects_alone():
    """v3.3 renumbers COPIES of profiling and nvd; only ``order`` differs from the originals.

    ``NVD`` and ``PROFILING`` are also listed in the frozen versions, so changing them in place
    would quietly edit those contracts.
    """
    from dataclasses import replace

    from sluice.gates.nvd import NVD
    from sluice.gates.profiles import PRODUCTION_PROFILES
    from sluice.gates.profiling import PROFILING

    gates_by_id = {
        gate.id: gate
        for gate in load_profile(CURRENT_VERSION, PRODUCTION_PROFILES).gates
    }

    assert (PROFILING.order, NVD.order) == (4, 5)
    assert gates_by_id["profiling"] == replace(PROFILING, order=5)
    assert gates_by_id["nvd"] == replace(NVD, order=6)


def test_the_default_for_fresh_databases_is_the_newest_profile():
    """``PRODUCTION_VERSION`` (what a fresh DB is seeded with) is v3.3.

    An existing DB keeps its stored default until an operator runs
    ``sluice set-default-profile`` — ``prepare_database`` never overwrites it.
    """
    from sluice.gates.profiles import PRODUCTION_PROFILE, PRODUCTION_VERSION as SHIPPED

    assert SHIPPED == CURRENT_VERSION
    assert PRODUCTION_PROFILE.pipeline_version == CURRENT_VERSION
