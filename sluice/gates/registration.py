"""The ``registration`` gate — order 4: register this run's samples in LabKey's sample-metadata list.

Reads the samplesheet the ``samplesheet`` gate wrote, works out which sample ids are
new, inserts a metadata row for each new sample whose name prefix is on the
authority list, and reports every sample's outcome to the run's Slack thread. A
sample it cannot register (prefix not on the authority list, malformed id,
impossible date) does NOT block the run: the gate succeeds and the report
@-mentions the on-call, who fixes the authority list or registers it by hand.

Ported from python-poller ``authority_registration.py`` (``parse_sample_id``,
``classify``, ``build_row``, ``read_samplesheet_ids``, ``register_samples``,
``RegistrationResult.manifest_text``), which ran at ``poller.py:1248-1272`` between
samplesheet and NVD. The poller's separate pre-NVD metadata check
(``labkey_client.check_sample_metadata``) is not ported as its own step: a sample
is missing metadata exactly when this report lists it as skipped. Deliberate
deviations:

- **Fail closed (errata C3).** Every LabKey read or write problem is a FAILURE
  (quarantine). The poller logged and carried on, and its "registered labels" read
  returned an empty set on error, so a LabKey blip re-inserted every sample. A
  re-run after ``clear-error`` is always safe: rows that landed are seen as
  already registered and skipped.
- **Already-registered wins.** The poller judged the name (malformed / unknown
  prefix) before checking the registry, so a sample registered by hand still
  raised an alert. Here a sample with a row is "already registered", whatever its
  name.
- **No insert-error status.** A failed insert quarantines instead of being
  recorded per sample and ignored.
- **Dry run is kept, at the user's request** (``labkey_register_dry_run``). It
  reads LabKey and reports, but inserts nothing — and the gate still counts as
  satisfied, which ``docs/ADDING-A-GATE.md`` normally forbids. To stop that being
  silent, the counts say ``would_insert`` instead of ``inserted``, the report is
  labelled ``[DRY-RUN]``, and the gate event records ``dry_run``. A run registered
  in dry-run mode is re-run for real with ``sluice reset-gate <run> registration``.

The gate only RETURNS a result. The Slack report rides back as the ``notice`` /
``notice_mention`` metadata keys, which the daemon posts (gates never hold the
notifier). ``persists`` is empty: the rows live in LabKey, not in ``runs``.
"""

import csv
import dataclasses
import functools
import pathlib
import re
from datetime import datetime
from typing import Any

from sluice.core.gate import Gate, GateOutcome, GateResult, no_op_invalidate
from sluice.gates.sample_registry import (
    SampleRegistryError,
    make_sample_registry_client,
)
from sluice.gates.samplesheet import SAMPLESHEET_FILENAME

__all__ = [
    "ALREADY_REGISTERED",
    "INSERT",
    "REASON_INVALID_DATE",
    "REASON_MALFORMED",
    "REASON_UNKNOWN_PREFIX",
    "REGISTRATION",
    "SKIPPED",
    "ParsedSample",
    "build_row",
    "classify",
    "format_report",
    "parse_sample_id",
    "read_samplesheet_ids",
    "run_registration_gate",
]

# A sample's status after classification (poller values, unchanged).
INSERT = "insert"
ALREADY_REGISTERED = "already_registered"
SKIPPED = "skipped"

# Why a sample was skipped (poller values, unchanged — operators know them).
REASON_UNKNOWN_PREFIX = "unknown_prefix"
REASON_INVALID_DATE = "invalid_date"
REASON_MALFORMED = "malformed_structure"

# The well token ends every sample id: S followed by digits (e.g. S27).
_WELL_PATTERN = re.compile(r"^S\d+$")


# ---- Layer 1: plain functions. No context, no database, no LabKey. ----------


@dataclasses.dataclass(frozen=True)
class ParsedSample:
    """One sample id split into its parts, plus what the gate will do with it.

    ``prefix`` / ``date_iso`` / ``well`` are ``None`` when the id could not be
    parsed. ``reason`` is set only for ``SKIPPED`` samples.
    """

    sample_id: str
    prefix: str | None = None
    date_iso: str | None = None
    well: str | None = None
    status: str = INSERT
    reason: str | None = None


def parse_sample_id(sample_id: str) -> ParsedSample:
    """Split ``<prefix>_<YYYYMMDD>_S<n>`` into prefix, ISO date and well.

    The well is the last ``_`` token and must look like ``S<digits>``; the date is
    the second-to-last and must be eight digits forming a real date; the prefix is
    everything before. Whether the prefix is *known* is decided later by
    ``classify``. Poller parity, including the two skip reasons.
    """
    tokens = sample_id.split("_")
    if len(tokens) < 3 or not _WELL_PATTERN.match(tokens[-1]):
        return ParsedSample(sample_id, status=SKIPPED, reason=REASON_MALFORMED)
    date_token = tokens[-2]
    if len(date_token) != 8 or not date_token.isdigit():
        return ParsedSample(sample_id, status=SKIPPED, reason=REASON_MALFORMED)
    try:
        sample_date = datetime.strptime(date_token, "%Y%m%d")
    except ValueError:
        return ParsedSample(sample_id, status=SKIPPED, reason=REASON_INVALID_DATE)
    return ParsedSample(
        sample_id,
        prefix="_".join(tokens[:-2]),
        date_iso=sample_date.strftime("%Y-%m-%d"),
        well=tokens[-1],
    )


def classify(parsed_samples, authority_map, registered_labels) -> list[ParsedSample]:
    """Decide each sample's final status. Returns new samples; the input is untouched.

    In order: a sample that already has a registry row is ``ALREADY_REGISTERED``
    (whatever its name — see the module docstring); a sample that failed to parse
    keeps its skip; a parsed sample whose prefix is not on the authority list is
    skipped as ``unknown_prefix``; everything else is to be inserted.
    """
    classified = []
    for sample in parsed_samples:
        if sample.sample_id in registered_labels:
            classified.append(
                dataclasses.replace(sample, status=ALREADY_REGISTERED, reason=None)
            )
        elif sample.status == SKIPPED:
            classified.append(sample)
        elif sample.prefix not in authority_map:
            classified.append(
                dataclasses.replace(
                    sample, status=SKIPPED, reason=REASON_UNKNOWN_PREFIX
                )
            )
        else:
            classified.append(sample)
    return classified


def build_row(sample: ParsedSample, authority_map) -> dict:
    """The registry-list row for one insertable sample (poller's locked field names)."""
    sewershed_name, sewershed_location = authority_map[sample.prefix]
    return {
        "sequence_sample_label": sample.sample_id,
        "sample_type": "ww",
        "sequencing_strategy": "PEG unbiased",
        "sewershed_name": sewershed_name,
        "sewershed_location": sewershed_location,
        "sample_date": sample.date_iso,
    }


def read_samplesheet_ids(samplesheet_path) -> list[str]:
    """The samplesheet's ``sample_id`` values, in order, without blanks or repeats."""
    sample_ids = []
    with open(samplesheet_path, newline="", encoding="utf-8") as samplesheet_file:
        for row in csv.DictReader(samplesheet_file):
            sample_id = (row.get("sample_id") or "").strip()
            if sample_id and sample_id not in sample_ids:
                sample_ids.append(sample_id)
    return sample_ids


def _count(samples, status) -> int:
    """How many samples have ``status``."""
    return sum(1 for sample in samples if sample.status == status)


def _status_label(sample: ParsedSample, *, dry_run: bool) -> str:
    """One sample's status as the report shows it."""
    if sample.status == INSERT:
        return "would insert" if dry_run else "inserted"
    if sample.status == ALREADY_REGISTERED:
        return "already registered"
    return f"skipped: {sample.reason}"


def format_report(samples, *, dry_run: bool, experiment_id) -> str:
    """The Slack report: a one-line summary, then every sample and its status.

    Byte-format parity with the poller's ``manifest_text`` so the channel reads as it
    always has, minus its emoji (removed 2026-11-13): ``[DRY-RUN]`` and "would
    insert" in dry-run mode; a monospace table of every sample. A skip still
    @-mentions the on-call (``notice_mention``), which is the real alert.
    """
    skipped = _count(samples, SKIPPED)
    dry_run_label = "[DRY-RUN] " if dry_run else ""
    insert_verb = "would insert" if dry_run else "inserted"
    header = (
        f"{dry_run_label}Sample registration (experiment `{experiment_id}` — "
        f"{len(samples)} samples): {_count(samples, INSERT)} {insert_verb}, "
        f"{_count(samples, ALREADY_REGISTERED)} already registered, {skipped} skipped"
    )
    lines = [header, "```", f"{'sample_id':<36} status"]
    lines += [
        f"{sample.sample_id:<36} {_status_label(sample, dry_run=dry_run)}"
        for sample in samples
    ]
    lines.append("```")
    return "\n".join(lines)


# ---- Layer 2: the orchestrator. Reads the context, returns a result. --------


def run_registration_gate(context: Any, make_client) -> GateResult:
    """Register this run's new samples, fail-closed, and return the report.

    ``make_client`` is a zero-argument callable returning a
    ``SampleRegistryClient`` (the ``experiment`` gate's seam); it is called only once
    the samplesheet has been read, so a broken samplesheet never touches LabKey.

    Reads ``context.run_row["name"]`` / ``["experiment_id"]``, ``context.workdir``
    and ``context.config["labkey_register_dry_run"]``. Returns; never writes run
    state.
    """
    run_name = context.run_row["name"]
    experiment_id = context.run_row["experiment_id"]
    config = context.config  # bound by name so the schema guard sees the read
    dry_run = bool(config["labkey_register_dry_run"])

    samplesheet_path = pathlib.Path(context.workdir) / SAMPLESHEET_FILENAME
    if not samplesheet_path.is_file():
        return GateResult(
            GateOutcome.FAILURE,
            reason=(
                f"registration gate: no {SAMPLESHEET_FILENAME} at {samplesheet_path} "
                f"for run {run_name!r} — the samplesheet gate's output is missing"
            ),
        )
    sample_ids = read_samplesheet_ids(samplesheet_path)
    if not sample_ids:
        return GateResult(
            GateOutcome.FAILURE,
            reason=(
                f"registration gate: {samplesheet_path} for run {run_name!r} has no "
                "sample ids — nothing to register, so the samplesheet is broken"
            ),
        )

    client = make_client()  # only now — construct the adapter
    try:
        authority_map = client.fetch_authority_map()
        registered_labels = client.fetch_registered_labels(sample_ids)
        samples = classify(
            [parse_sample_id(sample_id) for sample_id in sample_ids],
            authority_map,
            registered_labels,
        )
        to_insert = [sample for sample in samples if sample.status == INSERT]
        if to_insert and not dry_run:
            client.insert_rows(
                [build_row(sample, authority_map) for sample in to_insert]
            )
    except SampleRegistryError as registry_error:
        # Fail closed (C3): quarantine; a re-run after clear-error is safe because
        # anything that did land is then seen as already registered.
        return GateResult(
            GateOutcome.FAILURE,
            reason=f"registration gate: run {run_name!r}: {registry_error}",
        )

    skipped = _count(samples, SKIPPED)
    # "would_insert" in dry-run so the Slack milestone can never read as real inserts.
    insert_key = "would_insert" if dry_run else "inserted"
    return GateResult(
        GateOutcome.SUCCESS,
        metadata={
            insert_key: len(to_insert),
            "already_registered": _count(samples, ALREADY_REGISTERED),
            "skipped": skipped,
            "dry_run": dry_run,
            "notice": format_report(
                samples, dry_run=dry_run, experiment_id=experiment_id
            ),
            "notice_mention": skipped > 0,
        },
    )


def _run_registration(context: Any) -> GateResult:
    """Production wiring: defer client construction behind the zero-arg seam."""
    return run_registration_gate(
        context, functools.partial(make_sample_registry_client, context.config)
    )


# ---- Layer 3: the task object. ---------------------------------------------

REGISTRATION = Gate(
    id="registration",
    name="Sample registration",
    satisfied_column="authority_registered",  # migration 013
    order=4,  # TRUE execution order — after samplesheet, before profiling and NVD
    needs=("samplesheet",),  # the sample ids come from the samplesheet
    # Non-empty on purpose: `sluice set-gate` then refuses to mark this done by hand
    # without --force, because that would skip the inserts silently (errata B2).
    produces=("LabKey sample-metadata rows",),
    run=_run_registration,
    # Never deletes LabKey rows (ADR-0005): a reset re-runs the gate, and rows
    # already registered are simply skipped.
    invalidate=no_op_invalidate,
    terminate=None,  # seconds-long; nothing to interrupt
    persists={},  # the rows live in LabKey; counts go to the event log and Slack
)
