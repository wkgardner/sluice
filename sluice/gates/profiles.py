"""The shipped production profile registry — what live runs are stamped with.

Lives under ``sluice.gates`` because it names production ``Gate`` objects, which import world
adapters; ``sluice.core.profile`` holds only the reference/mock registry, so the ``gates -> core``
arrow is untouched (ADR-0003 d.1). The daemon's composition root unions the two with
``assemble_registry`` (plan 05 §7), which raises on a duplicate key rather than shadowing one
definition with another.

**Why this file is load-bearing.** Detection stamps ``runs.pipeline_version`` from
``metadata.pipeline_profile``, and the daemon resolves that string against its registry on EVERY
look. A stamped version with no registry entry raises ``UnknownProfileError`` and quarantines the
run before a single gate runs. Nothing can go live until ``production-v3.1`` resolves here.

**The profile grows one gate per increment.** This follows directly from the incremental-migration
policy (plan 13 ⚑3): a gate and the migration adding its satisfaction column land together, and the
gate joins this profile in that same increment. A gate not yet built is simply **absent** — it
cannot be listed as ``DISABLED``, because a ``Gate`` object must exist to be listed at all.

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
3     ``samplesheet``     ``samplesheet_created`` pending (migration 010)
4     ``registration``    ``authority_registered`` DISABLED by ⚑D — C3 fail-closed first
5     ``nvd``             ``nvd_completed``      pending (migration 011)
6     ``globus_submit``   ``globus_submitted``   DEFERRED (plan 15 §3a)
7     ``globus_verify``   ``clumped_data_backed_up`` DEFERRED (plan 15 §3a)
===== =================== ====================== ==========================================

``registration`` stays out permanently until C3 (``fetch_registered_labels`` must fail **closed**)
is resolved. The Globus pair is deferred by scope decision, not by design: a site that does not want
Globus runs exactly this shape and completes at ``nvd`` on purpose (gate map §7).
"""

import types
from collections.abc import Mapping

from sluice.core.profile import Applicability, Profile, build_profile
from sluice.gates.checksum import CHECKSUM
from sluice.gates.download import DOWNLOAD
from sluice.gates.experiment import EXPERIMENT

__all__ = ["PRODUCTION_PROFILE", "PRODUCTION_PROFILES", "PRODUCTION_VERSION"]

# The version string detection stamps onto every run and the daemon resolves each look. A constant
# rather than a literal at each use, so a typo is an ImportError instead of a per-run quarantine.
PRODUCTION_VERSION = "production-v3.1"

PRODUCTION_PROFILE: Profile = build_profile(
    PRODUCTION_VERSION,
    [
        (EXPERIMENT, Applicability.REQUIRED),
        (DOWNLOAD, Applicability.REQUIRED),
        (CHECKSUM, Applicability.REQUIRED),
    ],
)
"""The live pipeline contract, as far as it is built.

Currently three gates: ``experiment`` (order 0) creates the run's LabKey experiment and persists
``experiment_id``; ``download`` (order 1) transfers the raw lanes into the experiment-keyed staging
tree; ``checksum`` (order 2) verifies them against the delivered manifest. A run reaching the end of this contract is genuinely complete under it — the daemon stamps
``completed_at`` and says so, which is correct behaviour for a partial pipeline (ADR-0004 / C4), not
a bug to work around.

Each pending gate above joins this list in its own increment, after its migration lands.
"""

PRODUCTION_PROFILES: Mapping[str, Profile] = types.MappingProxyType(
    {PRODUCTION_PROFILE.pipeline_version: PRODUCTION_PROFILE}
)
"""The shipped production registry, keyed by each profile's OWN ``pipeline_version``.

Never a hand-typed key (the ``REFERENCE_PROFILES`` precedent, plan 05 ⚑2): ``load_profile`` rejects
a key/id mismatch per run, so the mismatch must be impossible to introduce here. A read-only proxy,
because a mutable module-level dict would be a second in-memory authority over which versions
resolve.
"""
