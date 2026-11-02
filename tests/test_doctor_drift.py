"""THE anti-drift guard for ``sluice doctor``.

A gate that starts reading a new path, secret, host or executable must not be
able to do so without doctor learning to check it. This is the mechanism that
was chosen over a twelfth ``Gate`` field (design spec D2): the coupling is a
test rather than the type, so the test has to be real.

Fails loudly and specifically, naming the gate, the key and the group, because
a guard whose message does not say what to do gets suppressed.
"""

import ast
import pathlib

from sluice.config.schema import CONFIG_FIELDS, ProbeKind
from sluice.doctor import (
    GATE_CONFIG_GROUPS,
    LOCAL_SOURCE_KEYS,
    REMOTE_SOURCE_KEYS,
)
from sluice.gates.profiles import PRODUCTION_PROFILE
from tests.test_config_schema import _config_reads_in

# Module paths below are relative to the repository root, resolved from this
# file rather than the working directory so the guard runs the same whether it
# is invoked from the repo root or anywhere else.
REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

# Gate id -> the module implementing it. Adapters shared by several gates are
# attributed to the gate that owns the configuration they read.
_GATE_MODULES = {
    "experiment": ("sluice/gates/experiment.py", "sluice/gates/labkey.py"),
    "download": (
        "sluice/gates/download.py",
        "sluice/gates/transfer.py",
        "sluice/gates/detection.py",
    ),
    "checksum": ("sluice/gates/checksum.py",),
    "samplesheet": ("sluice/gates/samplesheet.py", "sluice/gates/nvd_tool.py"),
    "nvd": ("sluice/gates/nvd.py",),
    "profiling": ("sluice/gates/profiling.py",),
    "registration": ("sluice/gates/registration.py", "sluice/gates/sample_registry.py"),
}


def _probe_by_key():
    return {field.name: field.probe for field in CONFIG_FIELDS}


def _group_by_key():
    return {field.name: field.group for field in CONFIG_FIELDS}


# Reads a gate makes for DECORATION, not operation: the value is informative
# (degrades to a placeholder on absence) and must not make doctor demand its
# group for profiles that do not contain the owning gate. Each entry names
# why. Keep this list short — a read that can fail a gate belongs in
# GATE_CONFIG_GROUPS, not here.
OPTIONAL_DECORATIVE_READS = {
    # The experiment description quotes the NVD version (poller parity,
    # 2026-10-05); _nvd_version_from_pyproject degrades to "unknown".
    ("experiment", "nvd_repo_dir"),
}


def _uncovered_probe_bearing_reads(sources_by_gate, groups_by_gate) -> list[str]:
    """Probe-bearing config keys a gate reads that doctor's map does not cover.

    Takes the sources and the coverage map as arguments rather than reading the
    module-level ones, so the comparison can be driven with a synthetic gate
    whose read is deliberately uncovered — see the witness test below. A guard
    that has never been seen to fail is not a guard.

    Args:
        sources_by_gate: gate id -> the Python sources implementing it, as text.
        groups_by_gate: gate id -> the schema groups doctor checks for it. Every
            gate id in ``sources_by_gate`` must be a key; a defaulted lookup is
            what made this guard vacuous for a newly added gate.
    """
    probe = _probe_by_key()
    group = _group_by_key()
    problems = []
    for gate_id, sources in sources_by_gate.items():
        covered_groups = set(groups_by_gate[gate_id])
        for source in sources:
            for key in sorted(_config_reads_in(ast.parse(source))):
                if probe.get(key, ProbeKind.NONE) is ProbeKind.NONE:
                    continue
                if (gate_id, key) in OPTIONAL_DECORATIVE_READS:
                    continue
                if group.get(key) not in covered_groups:
                    problems.append(
                        f"gate {gate_id!r} reads {key!r} (group "
                        f"{group.get(key)!r}, probe {probe[key].value}) but "
                        f"doctor's GATE_CONFIG_GROUPS[{gate_id!r}] does not "
                        f"include that group"
                    )
    return problems


def test_every_gate_in_the_profile_is_known_to_doctor_and_to_this_guard():
    """A new gate must appear in both maps, or nothing below can see it.

    Both lookups used to default to empty, so a gate added to the production
    profile with no entry contributed no modules to scan and no groups to check:
    the guard iterated nothing and passed on precisely the change it exists to
    police. Three gates are already scheduled, so this is the next change rather
    than a hypothetical one. An explicit empty tuple is how to say "this gate
    reads no configuration" — ``checksum`` already does.
    """
    profile_gate_ids = {gate.id for gate in PRODUCTION_PROFILE.gates}

    assert profile_gate_ids <= set(_GATE_MODULES), (
        f"add {sorted(profile_gate_ids - set(_GATE_MODULES))} to _GATE_MODULES "
        f"in tests/test_doctor_drift.py, naming the module(s) implementing it"
    )
    assert profile_gate_ids <= set(GATE_CONFIG_GROUPS), (
        f"add {sorted(profile_gate_ids - set(GATE_CONFIG_GROUPS))} to "
        f"GATE_CONFIG_GROUPS in sluice/doctor.py, naming the schema groups "
        f"doctor should check for it (() if it reads no config)"
    )


def test_every_probe_bearing_key_a_gate_reads_is_covered_by_doctor():
    """No gate reads a checkable config key that doctor does not check.

    Scoped to the gates in the shipped production profile: a gate that is not
    in the contract cannot break an installation, and demanding coverage for
    unbuilt gates would block the very increments that add them.
    """
    sources_by_gate = {
        gate.id: [
            (REPO_ROOT / module_path).read_text(encoding="utf-8")
            for module_path in _GATE_MODULES[gate.id]
        ]
        for gate in PRODUCTION_PROFILE.gates
    }

    problems = _uncovered_probe_bearing_reads(sources_by_gate, GATE_CONFIG_GROUPS)

    assert not problems, (
        "doctor has drifted from the gates — add the group to "
        "GATE_CONFIG_GROUPS in sluice/doctor.py:\n  " + "\n  ".join(problems)
    )


def test_the_guard_reports_a_probe_bearing_read_nobody_checks():
    """The guard's teeth, exercised on every run (design spec §10).

    A gate that starts reading a new path is the change this whole module exists
    to catch, so the comparison is run here against a source that does exactly
    that and asserted to complain. Without this the guard could be silently
    defanged — by a defaulted lookup, a swallowed key, a narrowed filter — and
    the suite would stay green.
    """
    reads_an_unchecked_path = "def run(config):\n    return config['nvd_blast_db']\n"

    problems = _uncovered_probe_bearing_reads(
        {"invented_gate": [reads_an_unchecked_path]}, {"invented_gate": ()}
    )

    assert problems, "the guard did not notice an entirely unchecked path read"
    assert "nvd_blast_db" in problems[0]
    assert "invented_gate" in problems[0]

    # ...and stays quiet when the group *is* covered, so it is not simply
    # complaining about everything.
    assert not _uncovered_probe_bearing_reads(
        {"invented_gate": [reads_an_unchecked_path]},
        {"invented_gate": ("nvd reference data",)},
    )


def test_every_detection_key_belongs_to_exactly_one_source_mode():
    """The source-mode partition is exhaustive, or a key is demanded in both.

    ``_source_mode_skip`` asks which mode a detection key belongs to. A key in
    neither set is silently demanded in *both* modes, which fails every
    correctly configured install; a key in both is checked in neither. The two
    sets are a hand-written partition of a group the code owns, so this is what
    keeps them from going stale.
    """
    detection_keys = {
        field.name for field in CONFIG_FIELDS if field.group == "detection"
    }

    assert not LOCAL_SOURCE_KEYS & REMOTE_SOURCE_KEYS, (
        f"{sorted(LOCAL_SOURCE_KEYS & REMOTE_SOURCE_KEYS)} is claimed by both "
        f"source modes, so doctor would check it in neither"
    )
    assert detection_keys == LOCAL_SOURCE_KEYS | REMOTE_SOURCE_KEYS, (
        f"detection keys {sorted(detection_keys ^ (LOCAL_SOURCE_KEYS | REMOTE_SOURCE_KEYS))} "
        f"belong to neither source mode — add each to LOCAL_SOURCE_KEYS or "
        f"REMOTE_SOURCE_KEYS in sluice/doctor.py"
    )


def test_the_guard_names_a_group_that_actually_exists():
    """A typo in GATE_CONFIG_GROUPS would silently cover nothing.

    Without this, `"nvd reference"` instead of `"nvd reference data"` would pass
    the guard above by covering no keys at all — the exact failure the guard
    exists to prevent, hidden inside the guard itself.
    """
    real_groups = {field.group for field in CONFIG_FIELDS}
    for gate_id, groups in GATE_CONFIG_GROUPS.items():
        for group in groups:
            assert group in real_groups, (
                f"GATE_CONFIG_GROUPS[{gate_id!r}] names {group!r}, which is not "
                f"a group any config field declares"
            )
