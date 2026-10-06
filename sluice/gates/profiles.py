"""The shipped production profile registry — what live runs are stamped with.

Lives under ``sluice.gates`` because it names production ``Gate`` objects, which import world
adapters; ``sluice.core.profile`` holds only the reference/mock registry, so the ``gates -> core``
arrow is untouched (ADR-0003 d.1). The daemon's composition root unions the two with
``assemble_registry`` (plan 05 §7), which raises on a duplicate key rather than shadowing one
definition with another.

**Why this file is load-bearing.** Detection stamps ``runs.pipeline_version`` from
``metadata.pipeline_profile``, and the daemon resolves that string against its registry on EVERY
look. A stamped version with no registry entry raises ``UnknownProfileError`` and quarantines the
run before a single gate runs. So every version ever stamped onto a run must stay here.

**Each new gate ships a new profile version.** This follows from the incremental-migration policy
(plan 13 ⚑3): a gate and the migration adding its satisfaction column land together, and the gate
joins the pipeline in that same increment. Before go-live the one profile simply grew in place;
since go-live (v3.1 was live when ``profiling`` arrived), a shipped version is frozen and the gate
joins a NEW version instead, because runs are stamped once and completed runs stay daemon-eligible.
A gate not yet built is simply **absent** — it cannot be listed as ``DISABLED``, because a ``Gate``
object must exist to be listed at all.

That absence is safe rather than a compromise, and the whole MVP sequencing rests on it (plan 15
§3a). Under ADR-0004 the contract is exactly the profile's non-disabled gates, so a run whose built
gates all succeed **completes deliberately** — never a silent "final gate lies" (C4, resolved
structurally in gate map §4). Shipping a partial pipeline into production is therefore a legitimate
configuration, not a hack: each new gate increment extends the pipeline with nothing behind it
reworked.

Gate map §3 fixes the full eight-gate target in TRUE execution order:

===== =================== ====================== ==========================================
order gate id             satisfied_column       status
===== =================== ====================== ==========================================
0     ``experiment``      ``labkey_registered``  **built** (migration 007)
1     ``download``        ``downloaded``         **built** (migration 008)
2     ``checksum``        ``checksummed``        **built** (migration 009)
3     ``samplesheet``     ``samplesheet_created`` **built** (migration 010)
4     ``profiling``       ``profiling_completed`` **built** (migration 012) — v3.2 only
5     ``nvd``             ``nvd_completed``      **built** (migration 011)
6     ``globus_submit``   ``globus_submitted``   DEFERRED (plan 15 §3a)
7     ``globus_verify``   ``clumped_data_backed_up`` DEFERRED (plan 15 §3a)
===== =================== ====================== ==========================================

``registration`` (``authority_registered``, formerly listed at order 4) stays out permanently until
C3 (``fetch_registered_labels`` must fail **closed**) is resolved; it was never built as a ``Gate``,
so ``profiling`` (silly-fast-bfx, a sluice-only gate) took the free order-4 slot. If registration is
ever built it joins a *new* profile version, where orders can be renumbered freely. The Globus pair is deferred by scope decision, not by design: a site that does not want
Globus runs exactly this shape and completes at ``nvd`` on purpose (gate map §7).
"""

import types
from collections.abc import Mapping

from sluice.core.profile import Applicability, Profile, build_profile
from sluice.gates.checksum import CHECKSUM
from sluice.gates.download import DOWNLOAD
from sluice.gates.experiment import EXPERIMENT
from sluice.gates.nvd import NVD
from sluice.gates.profiling import PROFILING
from sluice.gates.samplesheet import SAMPLESHEET

__all__ = [
    "PRODUCTION_PROFILE",
    "PRODUCTION_PROFILES",
    "PRODUCTION_V3_1_PROFILE",
    "PRODUCTION_VERSION",
]

# The version a FRESH database is seeded with (``prepare_database``); detection stamps the DB's
# stored default onto each new run, and the daemon resolves it every look. A constant rather than a
# literal at each use, so a typo is an ImportError instead of a per-run quarantine. An existing DB
# keeps its stored default until an operator runs ``sluice set-default-profile``.
PRODUCTION_VERSION = "production-v3.2"

PRODUCTION_V3_1_PROFILE: Profile = build_profile(
    "production-v3.1",
    [
        (EXPERIMENT, Applicability.REQUIRED),
        (DOWNLOAD, Applicability.REQUIRED),
        (CHECKSUM, Applicability.REQUIRED),
        (SAMPLESHEET, Applicability.REQUIRED),
        (NVD, Applicability.REQUIRED),
    ],
)
"""The original five-gate contract — FROZEN.

Kept resolvable and unchanged because every run detected before v3.2 is stamped with it, and a
run's ``pipeline_version`` never changes (stamp-once). Adding a gate here would give every such run
— completed ones included, since they stay daemon-eligible (F4) — a new unsatisfied gate to run.

Five gates: ``experiment`` (order 0) creates the run's LabKey experiment and persists
``experiment_id``; ``download`` (order 1) transfers the raw lanes into the experiment-keyed staging
tree; ``checksum`` (order 2) verifies them against the delivered manifest; ``samplesheet`` (order 3)
generates the by-lane samplesheet NVD consumes; ``nvd`` (order 5) runs the preprocessing pipeline.
This is the MVP target scope (plan 15 §3a) — the Globus pair is deferred, so a run completing at
``nvd`` is complete, deliberately. A run reaching the end of this contract is genuinely complete under it — the daemon stamps
``completed_at`` and says so, which is correct behaviour for a partial pipeline (ADR-0004 / C4), not
a bug to work around.

"""

PRODUCTION_PROFILE: Profile = build_profile(
    PRODUCTION_VERSION,
    [
        (EXPERIMENT, Applicability.REQUIRED),
        (DOWNLOAD, Applicability.REQUIRED),
        (CHECKSUM, Applicability.REQUIRED),
        (SAMPLESHEET, Applicability.REQUIRED),
        (PROFILING, Applicability.REQUIRED),
        (NVD, Applicability.REQUIRED),
    ],
)
"""The current live contract (``production-v3.2``): v3.1 plus ``profiling`` at order 4.

``profiling`` runs silly-fast-bfx (Deacon + Sylph) over the samplesheet before NVD. A profiling
failure quarantines the run, so NVD waits: the operator fixes and ``clear_error``s. (Excluding
``profiling`` for one run would let it go on to NVD, but ``control.exclude_gate`` is not on the CLI
yet.)

A new gate means a new version, never an edit to a shipped one: copy the newest list, add the gate,
bump ``PRODUCTION_VERSION``, register it below, and switch the live DB with
``sluice set-default-profile``.
"""

PRODUCTION_PROFILES: Mapping[str, Profile] = types.MappingProxyType(
    {
        profile.pipeline_version: profile
        for profile in (PRODUCTION_V3_1_PROFILE, PRODUCTION_PROFILE)
    }
)
"""The shipped production registry, keyed by each profile's OWN ``pipeline_version``.

Never a hand-typed key (the ``REFERENCE_PROFILES`` precedent, plan 05 ⚑2): ``load_profile`` rejects
a key/id mismatch per run, so the mismatch must be impossible to introduce here. A read-only proxy,
because a mutable module-level dict would be a second in-memory authority over which versions
resolve.
"""
