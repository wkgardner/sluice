"""The pipeline-profile model — per-gate applicability for a ``pipeline_version``.

Applicability is policy and lives here, never on the gate (design §4): the gate
is intrinsic, the profile says which gates apply to a given ``pipeline_version``
(design §8). This module's imports are a closed set — stdlib plus
``sluice.core.gate``/``sluice.core.mock_gates`` only (plan 05 unit 1) — so it
structurally cannot reach the DB and cannot re-resolve an in-flight run's frozen
version against the active default (``tests/test_dependency_arrows.py``
enforces the layering).
"""

import dataclasses
import enum
import types
from collections.abc import Iterable, Mapping

from sluice.core.gate import Gate, validate_gates
from sluice.core.mock_gates import MOCK_ALPHA, MOCK_BETA

__all__ = [
    "Applicability",
    "Profile",
    "REFERENCE_PROFILE",
    "REFERENCE_PROFILES",
    "REFERENCE_PROFILE_ALPHA_DISABLED",
    "UnknownProfileError",
    "build_profile",
    "load_profile",
    "required_gates",
]


class Applicability(enum.Enum):
    """Closed vocabulary for how a gate applies within a profile.

    Exactly these two members, by design (ADR-0004 decision 2): a run's
    contract is binary — a gate is in the contract and mandatory, or out and
    absent. The values are the strings profiles and reconcile speak, and the
    closed set is the B1/B4 surface — no further applicability and no retry
    member may be added without a new ADR (ADR-0002).
    """

    REQUIRED = "required"  # in the contract — mandatory (ADR-0004 d.1)
    DISABLED = "disabled"  # out of the contract — satisfied by absence (ADR-0004 d.2)


@dataclasses.dataclass(frozen=True)
class Profile:
    """The pipeline-version definition — the ordered gate set plus per-gate applicability.

    Exactly these three fields, all required (plan 05 unit 2): no ``config``
    (profiles are code-defined, plan 05 fact 4), no run-state handle
    (ADR-0002), and no retry field (B4 — every failure quarantines; retry
    arrives only as a new ADR). ``pipeline_version`` is the id that
    ``runs.pipeline_version`` freezes at detection and references thereafter
    (design §8).
    """

    pipeline_version: str  # the id runs.pipeline_version references (§8)
    gates: tuple[Gate, ...]  # execution order (B3)
    applicability: Mapping[str, Applicability]  # gate.id -> applicability

    def __post_init__(self) -> None:
        """Validate the gate set, store it in execution order, require exact applicability coverage, and store applicability as a read-only snapshot.

        Every profile is validated at the record boundary (plan 05 §3.1), so a
        B3 order transcription — python-poller's ``PIPELINE_GATE_COLUMNS``
        lists download before the registration that must execute first
        (errata B3) — fails at profile construction, never as a deadlocked run
        at daemon runtime; and ``applicability`` must key exactly the gate ids
        (§3.1 step 2), because reconcile's ``profile.applicability[gate.id]``
        lookup is promised never to miss (§3.3) and a stray key is almost
        always a typo'd gate id; and every value must be an ``Applicability``
        member (§3.1 step 3), because reconcile branches with ``is`` on the
        enum and a bare string would mis-branch silently at selection time.

        Raises:
            ValueError: propagated from ``validate_gates`` — duplicate
                ``id``/``satisfied_column``/``order`` values, a ``need``
                naming a gate not in the set, or a ``need`` that does not
                strictly precede its needer; or ``applicability`` keys that
                do not match the gate ids exactly, naming each missing and
                stray id.
            TypeError: an ``applicability`` value that is not an
                ``Applicability`` member (for example the member's bare
                string value), naming each offending gate id.
        """
        # object.__setattr__ is the one sanctioned route past the freeze
        # during construction.
        object.__setattr__(self, "gates", validate_gates(self.gates))

        # §3.1 step 2 — coverage must be exact in BOTH directions: a gate
        # with no entry is one reconcile cannot classify; a stray key is a
        # classification the author believed applied to a real gate.
        gate_ids = {gate.id for gate in self.gates}
        applicability_ids = set(self.applicability)
        missing_gate_ids = sorted(gate_ids - applicability_ids)
        stray_applicability_ids = sorted(applicability_ids - gate_ids)
        if missing_gate_ids or stray_applicability_ids:
            raise ValueError(
                f"Profile {self.pipeline_version!r}: applicability must cover "
                f"exactly the gate ids — missing entries for gates "
                f"{missing_gate_ids}, stray keys {stray_applicability_ids}"
            )

        # §3.1 step 3 — reconcile branches with `is` on the enum, so a bare
        # string value would compare unequal to every member and mis-branch
        # silently at selection time; fail loud at construction instead.
        non_member_values = sorted(
            (gate_id, value)
            for gate_id, value in self.applicability.items()
            if not isinstance(value, Applicability)
        )
        if non_member_values:
            raise TypeError(
                f"Profile {self.pipeline_version!r}: every applicability value "
                f"must be an Applicability member; got {non_member_values}"
            )

        # §3.1 step 4 — a read-only view over a private snapshot: the proxy
        # refuses writes through the profile, and dict(...) decouples it from
        # the author's dict so later mutation there cannot show through — a
        # live shared map would be a second mutable authority over which gates
        # apply (the spine; mirror RunContext.config, plan 04).
        object.__setattr__(
            self, "applicability", types.MappingProxyType(dict(self.applicability))
        )


def build_profile(
    pipeline_version: str, entries: Iterable[tuple[Gate, Applicability]]
) -> Profile:
    """Build a ``Profile`` from ``(gate, applicability)`` pairs and return it.

    The pairs form is the authoring convenience (plan 05 unit 3): a gate and
    its applicability stay adjacent, so the gates tuple and the applicability
    map cannot drift out of sync. The factory is a thin splitter — every check
    lives in ``Profile.__post_init__`` (§3.1), and the gates tuple keeps every
    entry (duplicates included) so ``validate_gates`` sees a duplicate gate
    that the keyed applicability dict alone would silently collapse.

    Raises:
        ValueError: propagated from ``Profile.__post_init__`` — an invalid
            gate set (duplicate ``id``/``satisfied_column``/``order``, an
            unknown or non-preceding ``need``).
        TypeError: propagated from ``Profile.__post_init__`` — an
            ``applicability`` value that is not an ``Applicability`` member.
    """
    # Materialize once: entries may be a one-shot iterable (a generator), and
    # the splitter reads it for both parallel collections.
    entry_pairs = tuple(entries)
    gates = tuple(gate for gate, _ in entry_pairs)
    applicability = {
        gate.id: gate_applicability for gate, gate_applicability in entry_pairs
    }
    return Profile(
        pipeline_version=pipeline_version,
        gates=gates,
        applicability=applicability,
    )


class UnknownProfileError(KeyError):
    """``load_profile`` was asked for a ``pipeline_version`` the registry does not carry.

    A ``KeyError`` subclass (mirror ``UnknownColumnError``) so callers can
    catch it precisely — and must, because a bare ``except KeyError`` around
    ``load_profile`` swallows it; the daemon catches this type explicitly to
    quarantine the one affected run (plan 05 §7).
    """


def load_profile(pipeline_version: str, registry: Mapping[str, Profile]) -> Profile:
    """Resolve ``pipeline_version`` against the injected registry and return its ``Profile``.

    The freeze's resolution seam (plan 05 §2 fact 1 / design §8): a run always
    completes under the definition stamped into ``runs.pipeline_version`` at
    detection, so resolution keys on that string against an explicitly-passed
    ``registry`` — there is no default registry, and an unknown version is an
    error to surface, never a fallback to the active default. Registry
    coherence is checked, not assumed (§3.2): this is the per-run backstop
    only — the duplicate-key half (a production profile shadowing a reference
    key at merge time) is the daemon's assembly-time guard (plan 05 §7), which
    this check cannot see.

    Raises:
        UnknownProfileError: ``pipeline_version`` is not a key of ``registry``;
            the message names the requested version and the registry's known
            ids.
        ValueError: the resolved profile's own ``pipeline_version`` does not
            equal the lookup key — a mis-assembled registry entry that would
            resolve every run stamped with that key against the wrong
            definition; the message names both.
    """
    if pipeline_version not in registry:
        raise UnknownProfileError(
            f"unknown pipeline_version {pipeline_version!r}; "
            f"the registry carries {sorted(registry)}"
        )
    resolved_profile = registry[pipeline_version]

    # §3.2 registry coherence — a key that does not equal its profile's own id
    # would silently resolve every run stamped with that key against a
    # definition it never started with, the class of bug the detection-time
    # freeze exists to prevent.
    if resolved_profile.pipeline_version != pipeline_version:
        raise ValueError(
            f"registry key {pipeline_version!r} resolves to a profile whose "
            f"own pipeline_version is {resolved_profile.pipeline_version!r} — "
            f"a mis-assembled registry entry"
        )
    return resolved_profile


def required_gates(profile: Profile) -> tuple[Gate, ...]:
    """Return the contract — every non-disabled gate, in execution order.

    The completion-predicate seam (plan 05 §3.3, amended by ADR-0004):
    reconcile's "complete" is ``all(row[g.satisfied_column] for g in
    required_gates(profile))``, so this subset is exactly the gates that can
    block completion — DISABLED is out of the contract, satisfied by absence
    (ADR-0004 d.2 / B1, the fix for the nvd_auto_run=false loop-forever bug).
    A pure function of the profile alone, no run row; ``profile.gates`` is
    already ascending by ``Gate.order`` (``validate_gates`` in
    ``Profile.__post_init__``), so the filtered tuple preserves execution
    order, and the ``applicability`` lookup never misses for a gate in
    ``profile.gates`` (§3.1 exact coverage).
    """
    return tuple(
        gate
        for gate in profile.gates
        if profile.applicability[gate.id] is Applicability.REQUIRED
    )


# The Plan A reference profile (plan 05 fact 3 / design §12): both mock gates
# REQUIRED, so the daemon and control ops can be proven end-to-end with no
# external services — the run completes only when both gates actually ran.
# "reference-v1" is the cross-subsystem constant the db plan stamps into
# ``runs.pipeline_version`` and seeds into ``metadata.pipeline_profile`` (§3.2).
REFERENCE_PROFILE = build_profile(
    "reference-v1",
    [(MOCK_ALPHA, Applicability.REQUIRED), (MOCK_BETA, Applicability.REQUIRED)],
)

# The shipped disabled variant (⚑2, decided 2026-07-23): alpha DISABLED yet
# still in the gate set — disabling ≠ omitting (§2 B1) — so beta's
# needs=("mock_alpha",) resolves through validate_gates while completion is
# beta-only. Disabling the *needed* gate is deliberate: the full loop proves
# both halves of disabled-is-satisfied (needs-unblocking from applicability,
# since alpha's column stays 0, AND exclusion from the completion predicate).
REFERENCE_PROFILE_ALPHA_DISABLED = build_profile(
    "reference-v1-alpha-disabled",
    [(MOCK_ALPHA, Applicability.DISABLED), (MOCK_BETA, Applicability.REQUIRED)],
)

# The shipped registry (⚑2, decided 2026-07-23: both profiles, distinct keys):
# the seam load_profile keys on and the daemon extends with production
# profiles (§3.2/§7). Each entry is keyed by its profile's OWN
# pipeline_version attribute — never a hand-typed string — so a key/id
# mismatch (which load_profile rejects per run) cannot originate here; and
# it is a read-only proxy because a mutable module-level dict would be a
# second in-memory authority over which versions resolve (the spine).
REFERENCE_PROFILES: Mapping[str, Profile] = types.MappingProxyType(
    {
        REFERENCE_PROFILE.pipeline_version: REFERENCE_PROFILE,
        REFERENCE_PROFILE_ALPHA_DISABLED.pipeline_version: (
            REFERENCE_PROFILE_ALPHA_DISABLED
        ),
    }
)
