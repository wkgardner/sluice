"""Tests for the ``registration`` gate (``sluice/gates/registration.py``).

Most tests aim at layer 1 — the plain functions ported from python-poller's
``authority_registration.py`` — and the rest drive ``run_registration_gate`` with a
fake client, so nothing here touches LabKey or the API key file.
"""

import types

import pytest

from sluice.core.gate import GateOutcome
from sluice.gates.registration import (
    ALREADY_REGISTERED,
    INSERT,
    REASON_INVALID_DATE,
    REASON_MALFORMED,
    REASON_UNKNOWN_PREFIX,
    REGISTRATION,
    SKIPPED,
    build_row,
    classify,
    format_report,
    parse_sample_id,
    read_samplesheet_ids,
    run_registration_gate,
)
from sluice.gates.sample_registry import SampleRegistryError
from sluice.gates.samplesheet import SAMPLESHEET_FILENAME

AUTHORITY = {
    "WI_Madison_MMSD": ("MMSD", "Madison, WI"),
    "CA_Riverside_RWQCP": ("RWQCP", "Riverside, CA"),
}

# ---- layer 1: parse_sample_id (poller parity) ------------------------------


def test_parse_splits_prefix_date_and_well():
    """Prefix is everything before the date and well tokens; the date becomes ISO."""
    sample = parse_sample_id("CA_Riverside_RWQCP_20261008_S27")

    assert sample.prefix == "CA_Riverside_RWQCP"
    assert sample.date_iso == "2026-10-08"
    assert sample.well == "S27"
    assert sample.status == INSERT
    assert sample.reason is None


@pytest.mark.parametrize(
    "sample_id",
    [
        "Water_S53",  # too few tokens
        "WI_Madison_MMSD_20261006_X7",  # last token is not S<digits>
        "WI_Madison_MMSD_2026106_S7",  # date is not 8 digits
        "WI_Madison_MMSD_2026100a_S7",  # date is not all digits
    ],
)
def test_parse_marks_malformed_ids(sample_id):
    """Ids that do not end ``_<YYYYMMDD>_S<n>`` are skipped as malformed."""
    sample = parse_sample_id(sample_id)

    assert sample.status == SKIPPED
    assert sample.reason == REASON_MALFORMED


def test_parse_marks_an_impossible_date_invalid():
    """Eight digits that are not a real date get their own reason."""
    sample = parse_sample_id("WI_Madison_MMSD_20261340_S7")

    assert sample.status == SKIPPED
    assert sample.reason == REASON_INVALID_DATE


# ---- layer 1: classify -------------------------------------------------------


def test_classify_sorts_samples_into_insert_known_and_skipped():
    """Valid + known prefix -> insert; registered -> already; unknown prefix -> skipped."""
    parsed = [
        parse_sample_id("WI_Madison_MMSD_20261006_S7"),
        parse_sample_id("CA_Riverside_RWQCP_20261008_S27"),
        parse_sample_id("TX_Nowhere_20261008_S3"),
    ]

    classified = classify(parsed, AUTHORITY, {"CA_Riverside_RWQCP_20261008_S27"})

    assert [s.status for s in classified] == [INSERT, ALREADY_REGISTERED, SKIPPED]
    assert classified[2].reason == REASON_UNKNOWN_PREFIX


def test_classify_counts_a_registered_sample_as_registered_whatever_its_name():
    """Deviation from the poller: a hand-registered sample is not reported missing.

    The poller decided "unknown prefix" / "malformed" before looking at the
    registry, so a sample someone had registered by hand still raised an alert.
    The question the report answers is "which samples have no metadata row?".
    """
    parsed = [parse_sample_id("Water_20261015_S53")]  # prefix not on the list

    classified = classify(parsed, AUTHORITY, {"Water_20261015_S53"})

    assert classified[0].status == ALREADY_REGISTERED


def test_classify_does_not_mutate_its_input():
    """Classification returns new samples; the parsed list is left as it was."""
    parsed = [parse_sample_id("TX_Nowhere_20261008_S3")]

    classify(parsed, AUTHORITY, set())

    assert parsed[0].status == INSERT


# ---- layer 1: build_row ------------------------------------------------------


def test_build_row_uses_the_poller_field_names():
    """The insert row matches the poller's locked field names and fixed values."""
    sample = parse_sample_id("WI_Madison_MMSD_20261006_S7")

    assert build_row(sample, AUTHORITY) == {
        "sequence_sample_label": "WI_Madison_MMSD_20261006_S7",
        "sample_type": "ww",
        "sequencing_strategy": "PEG unbiased",
        "sewershed_name": "MMSD",
        "sewershed_location": "Madison, WI",
        "sample_date": "2026-10-06",
    }


# ---- layer 1: read_samplesheet_ids -----------------------------------------


def test_read_samplesheet_ids_keeps_order_and_drops_duplicates(tmp_path):
    """One id per sample, in samplesheet order, blanks ignored."""
    samplesheet = tmp_path / SAMPLESHEET_FILENAME
    samplesheet.write_text(
        "sample_id,fastq1\nB_20261006_S2,x\nA_20261006_S1,y\nB_20261006_S2,z\n,w\n",
        encoding="utf-8",
    )

    assert read_samplesheet_ids(samplesheet) == ["B_20261006_S2", "A_20261006_S1"]


# ---- layer 1: format_report --------------------------------------------------


def test_report_lists_every_sample_with_its_status():
    """The Slack report is the poller's manifest: header plus one line per sample."""
    samples = classify(
        [
            parse_sample_id("WI_Madison_MMSD_20261006_S7"),
            parse_sample_id("TX_Nowhere_20261008_S3"),
        ],
        AUTHORITY,
        set(),
    )

    text = format_report(samples, dry_run=False, experiment_id=32743)

    first_line = text.splitlines()[0]
    assert first_line.startswith("Sample registration (experiment `32743` — 2 samples)")
    assert "1 inserted, 0 already registered, 1 skipped" in first_line
    assert "WI_Madison_MMSD_20261006_S7" in text
    assert "skipped: unknown_prefix" in text


def test_dry_run_report_says_so():
    """A dry run is never silent: the report is labelled and says "would insert"."""
    samples = classify(
        [parse_sample_id("WI_Madison_MMSD_20261006_S7")], AUTHORITY, set()
    )

    text = format_report(samples, dry_run=True, experiment_id=1)

    assert text.startswith("[DRY-RUN] Sample registration")
    assert "1 would insert" in text
    assert "would insert" in text.splitlines()[-2]


# ---- layer 2: run_registration_gate -----------------------------------------


class FakeClient:
    """A ``SampleRegistryClient`` stand-in: canned answers, recorded inserts."""

    def __init__(self, *, registered=(), error=None, insert_error=None):
        self.registered = set(registered)
        self.error = error
        self.insert_error = insert_error
        self.inserted_rows = []

    def fetch_authority_map(self):
        if self.error is not None:
            raise self.error
        return dict(AUTHORITY)

    def fetch_registered_labels(self, labels):
        return {label for label in labels if label in self.registered}

    def insert_rows(self, rows):
        if self.insert_error is not None:
            raise self.insert_error
        self.inserted_rows.extend(rows)
        # A real insert makes the rows registered — what a re-run then sees.
        self.registered.update(row["sequence_sample_label"] for row in rows)


def _context(tmp_path, sample_ids, *, dry_run=False, with_samplesheet=True):
    """A gate context with a samplesheet listing ``sample_ids``."""
    workdir = tmp_path / "staging" / "32743"
    workdir.mkdir(parents=True, exist_ok=True)
    if with_samplesheet:
        lines = ["sample_id,fastq1"] + [f"{sid},x" for sid in sample_ids]
        (workdir / SAMPLESHEET_FILENAME).write_text(
            "\n".join(lines) + "\n", encoding="utf-8"
        )
    return types.SimpleNamespace(
        workdir=workdir,
        config={"labkey_register_dry_run": dry_run},
        run_row={"name": "run_A", "experiment_id": 32743},
    )


def test_gate_inserts_new_samples_and_reports_quietly(tmp_path):
    """All samples registrable: they are inserted, and the report has no @-mention."""
    client = FakeClient(registered={"CA_Riverside_RWQCP_20261008_S27"})
    context = _context(
        tmp_path, ["WI_Madison_MMSD_20261006_S7", "CA_Riverside_RWQCP_20261008_S27"]
    )

    result = run_registration_gate(context, lambda: client)

    assert result.outcome is GateOutcome.SUCCESS
    assert [row["sequence_sample_label"] for row in client.inserted_rows] == [
        "WI_Madison_MMSD_20261006_S7"
    ]
    assert result.metadata["inserted"] == 1
    assert result.metadata["already_registered"] == 1
    assert result.metadata["skipped"] == 0
    assert result.metadata["dry_run"] is False
    assert result.metadata["notice"].startswith("Sample registration")
    assert result.metadata["notice_mention"] is False


def test_gate_succeeds_but_mentions_when_samples_are_skipped(tmp_path):
    """Unregistrable samples never block the run; they @-mention the on-call."""
    client = FakeClient()
    context = _context(
        tmp_path, ["WI_Madison_MMSD_20261006_S7", "TX_Nowhere_20261008_S3"]
    )

    result = run_registration_gate(context, lambda: client)

    assert result.outcome is GateOutcome.SUCCESS
    assert result.metadata["skipped"] == 1
    assert result.metadata["notice_mention"] is True
    assert "TX_Nowhere_20261008_S3" in result.metadata["notice"]


def test_dry_run_inserts_nothing_and_labels_everything(tmp_path):
    """Dry run reads LabKey but writes nothing, and says so in the counts and report."""
    client = FakeClient()
    context = _context(tmp_path, ["WI_Madison_MMSD_20261006_S7"], dry_run=True)

    result = run_registration_gate(context, lambda: client)

    assert result.outcome is GateOutcome.SUCCESS
    assert client.inserted_rows == []
    assert "inserted" not in result.metadata
    assert result.metadata["would_insert"] == 1
    assert result.metadata["dry_run"] is True
    assert "[DRY-RUN]" in result.metadata["notice"]


def test_rerunning_after_a_successful_insert_inserts_nothing_new(tmp_path):
    """Resume safety: a second run sees the first run's rows and skips them."""
    client = FakeClient()
    context = _context(tmp_path, ["WI_Madison_MMSD_20261006_S7"])

    run_registration_gate(context, lambda: client)
    second = run_registration_gate(context, lambda: client)

    assert len(client.inserted_rows) == 1
    assert second.metadata["inserted"] == 0
    assert second.metadata["already_registered"] == 1


def test_labkey_read_failure_quarantines(tmp_path):
    """C3: a LabKey read problem is a FAILURE naming the run, never a guess."""
    client = FakeClient(error=SampleRegistryError("authority list unreadable"))
    context = _context(tmp_path, ["WI_Madison_MMSD_20261006_S7"])

    result = run_registration_gate(context, lambda: client)

    assert result.outcome is GateOutcome.FAILURE
    assert "run_A" in result.reason
    assert "authority list unreadable" in result.reason


def test_insert_failure_quarantines(tmp_path):
    """A rejected insert stops the run; the re-run after clear-error is safe."""
    client = FakeClient(insert_error=SampleRegistryError("insert rejected"))
    context = _context(tmp_path, ["WI_Madison_MMSD_20261006_S7"])

    result = run_registration_gate(context, lambda: client)

    assert result.outcome is GateOutcome.FAILURE
    assert "insert rejected" in result.reason


def test_missing_samplesheet_quarantines_without_contacting_labkey(tmp_path):
    """No samplesheet means nothing to register — fail before building a client."""
    context = _context(tmp_path, [], with_samplesheet=False)

    def never_called():
        raise AssertionError("the client must not be built")

    result = run_registration_gate(context, never_called)

    assert result.outcome is GateOutcome.FAILURE
    assert SAMPLESHEET_FILENAME in result.reason


def test_empty_samplesheet_quarantines(tmp_path):
    """A samplesheet with no sample ids is a broken samplesheet, not "0 registered"."""
    context = _context(tmp_path, [])

    result = run_registration_gate(context, lambda: FakeClient())

    assert result.outcome is GateOutcome.FAILURE
    assert "no sample" in result.reason


# ---- layer 3: the Gate record ------------------------------------------------


def test_gate_record_runs_after_samplesheet_and_owns_its_column():
    """Order 4, needs the samplesheet, satisfies migration 013's column."""
    assert REGISTRATION.id == "registration"
    assert REGISTRATION.order == 4
    assert REGISTRATION.needs == ("samplesheet",)
    assert REGISTRATION.satisfied_column == "authority_registered"
    assert REGISTRATION.persists == {}
    # Non-empty so `sluice set-gate` refuses to mark it done by hand without
    # --force: that would skip the inserts silently (errata B2).
    assert REGISTRATION.produces
