"""Behavioral tests for the ``checksum`` gate (plan 15 increment 3).

Real files under ``tmp_path`` and real hashing throughout — the gate has no world adapter and no
seam, because it only reads local files. Nothing here needs mocking.
"""

import hashlib
import types

from sluice.core.gate import GateOutcome
from sluice.gates.checksum import (
    CHECKSUM,
    HASH_MISMATCH,
    MANIFEST_MISSING,
    find_manifests,
    parse_manifests,
    run_checksum_gate,
    verify_directory,
)
from sluice.gates.download import RAW_SEQS_DIRNAME


def make_run(tmp_path, name="run_A"):
    """Create the raw_seqs tree the download gate would have produced."""
    workdir = tmp_path / "staging" / "4471"
    raw_seqs = workdir / RAW_SEQS_DIRNAME
    raw_seqs.mkdir(parents=True)
    context = types.SimpleNamespace(
        workdir=workdir,
        config={"parallel_downloads": 4},
        run_row={"name": name, "experiment_id": 4471},
    )
    return context, raw_seqs


def write_lane(raw_seqs, filename, content=b"ACGT" * 64):
    """Write a lane file and return its md5 and sha256 digests."""
    (raw_seqs / filename).write_bytes(content)
    return (
        hashlib.md5(content).hexdigest(),
        hashlib.sha256(content).hexdigest(),
    )


# ---------------------------------------------------------------------------
# Manifest discovery and parsing.
# ---------------------------------------------------------------------------


def test_hidden_sha256_manifests_are_found(tmp_path):
    """A dotfile manifest like ``.verified-copy.sha256`` is discovered.

    The reason discovery walks instead of globbing (PP ``poller.py:642``):
    ``pathlib.glob("*")`` skips leading-dot names, so a glob-based implementation
    would silently find no manifest and report a perfectly healthy delivery as
    unverifiable.
    """
    _context, raw_seqs = make_run(tmp_path)
    (raw_seqs / ".verified-copy.sha256").write_text("abc  x_001.fastq.gz\n")

    found = find_manifests(raw_seqs)

    assert [path.name for path in found] == [".verified-copy.sha256"]


def test_manifest_parsing_keeps_lane_files_and_drops_the_rest(tmp_path):
    """Only ``001.fastq.gz`` entries are verified; trimmed/derived files are skipped.

    Manifests routinely list files that were never part of the delivery. Treating
    those as missing would fail every healthy run (PP ``parse_md5_files``, :688).
    """
    manifest = tmp_path / "md5s.txt"
    manifest.write_text(
        "aaa  sample_L001_R1_001.fastq.gz\n"
        "bbb  sample_trimmed.fastq.gz\n"
        "ccc *sample_L002_R1_001.fastq.gz\n"
    )

    expected = parse_manifests([manifest])

    assert expected == {
        "sample_L001_R1_001.fastq.gz": "aaa",
        "sample_L002_R1_001.fastq.gz": "ccc",
    }


def test_manifest_paths_are_reduced_to_basenames(tmp_path):
    """A manifest entry with a directory prefix matches the flattened transfer.

    Manifests are written relative to the delivery root, while the download gate
    flattens every file into one directory — so ``Data/lane1_001.fastq.gz`` in the
    manifest has to find ``lane1_001.fastq.gz`` on disk.
    """
    manifest = tmp_path / "md5s.txt"
    manifest.write_text("aaa  Data/Intensities/sample_L001_R1_001.fastq.gz\n")

    assert parse_manifests([manifest]) == {"sample_L001_R1_001.fastq.gz": "aaa"}


# ---------------------------------------------------------------------------
# The two distinguishable failure modes (plan 15 §8).
# ---------------------------------------------------------------------------


def test_no_manifest_fails_as_manifest_missing(tmp_path):
    """A delivery with no manifest fails as ``MANIFEST_MISSING``, not a generic error.

    The distinction a future auto-rewind policy needs (plan 15 §8): re-running the
    download can never conjure a manifest that was never delivered, so this failure
    must be visibly different from a mismatch.
    """
    context, _raw_seqs = make_run(tmp_path)

    result = run_checksum_gate(context)

    assert result.outcome is GateOutcome.FAILURE
    assert result.reason.startswith(MANIFEST_MISSING)


def test_empty_manifest_fails_as_manifest_missing(tmp_path):
    """A manifest with no verifiable entries is also ``MANIFEST_MISSING``.

    A present-but-useless manifest is the same operational situation as an absent
    one — nothing to verify against, and re-transferring will not change that.
    """
    context, raw_seqs = make_run(tmp_path)
    (raw_seqs / "md5s.txt").write_text("# nothing verifiable here\n")

    result = run_checksum_gate(context)

    assert result.outcome is GateOutcome.FAILURE
    assert result.reason.startswith(MANIFEST_MISSING)


def test_corrupted_file_fails_as_hash_mismatch(tmp_path):
    """A file whose contents disagree with the manifest fails as ``HASH_MISMATCH``.

    The re-transferable case. This is what a truncated or corrupted transfer looks
    like, and it is the one where rewinding to ``download`` could plausibly help.
    """
    context, raw_seqs = make_run(tmp_path)
    md5_digest, _sha = write_lane(raw_seqs, "sample_L001_R1_001.fastq.gz")
    (raw_seqs / "md5s.txt").write_text(f"{md5_digest}  sample_L001_R1_001.fastq.gz\n")
    # Corrupt the file after writing its correct digest into the manifest.
    (raw_seqs / "sample_L001_R1_001.fastq.gz").write_bytes(b"TRUNCATED")

    result = run_checksum_gate(context)

    assert result.outcome is GateOutcome.FAILURE
    assert result.reason.startswith(HASH_MISMATCH)
    assert "sample_L001_R1_001.fastq.gz" in result.reason


def test_missing_file_fails_as_hash_mismatch_naming_the_file(tmp_path):
    """A manifest entry with no file on disk is reported, naming the file.

    Grouped with mismatch rather than manifest-missing on purpose: the manifest is
    fine, the *data* is incomplete, which is exactly the re-transferable case.
    """
    context, raw_seqs = make_run(tmp_path)
    (raw_seqs / "md5s.txt").write_text("aaa  absent_L001_R1_001.fastq.gz\n")

    result = run_checksum_gate(context)

    assert result.outcome is GateOutcome.FAILURE
    assert result.reason.startswith(HASH_MISMATCH)
    assert "absent_L001_R1_001.fastq.gz" in result.reason


# ---------------------------------------------------------------------------
# The success path, both algorithms.
# ---------------------------------------------------------------------------


def test_intact_md5_delivery_succeeds(tmp_path):
    """Every file matching its MD5 digest is SUCCESS, counting what was verified."""
    context, raw_seqs = make_run(tmp_path)
    md5_digest, _sha = write_lane(raw_seqs, "sample_L001_R1_001.fastq.gz")
    (raw_seqs / "md5s.txt").write_text(f"{md5_digest}  sample_L001_R1_001.fastq.gz\n")

    result = run_checksum_gate(context)

    assert result.outcome is GateOutcome.SUCCESS
    assert result.metadata == {"verified_files": 1}


def test_sha256_is_inferred_from_digest_length(tmp_path):
    """A 64-character digest is verified as SHA-256, not MD5.

    The algorithm is never declared in the manifest — it is inferred from digest
    length (PP ``_checksum_algorithm``, :710). Getting this wrong would fail every
    SHA-256 delivery, which is the format the newer deliveries ship.
    """
    context, raw_seqs = make_run(tmp_path)
    _md5, sha_digest = write_lane(raw_seqs, "sample_L001_R1_001.fastq.gz")
    (raw_seqs / ".verified-copy.sha256").write_text(
        f"{sha_digest}  sample_L001_R1_001.fastq.gz\n"
    )

    result = run_checksum_gate(context)

    assert result.outcome is GateOutcome.SUCCESS


def test_verification_is_idempotent(tmp_path):
    """Running the gate twice gives the same answer and changes nothing on disk.

    Re-entry after a crash or a hold simply re-verifies — the gate reads only, so
    there is no partial state to reconcile.
    """
    context, raw_seqs = make_run(tmp_path)
    md5_digest, _sha = write_lane(raw_seqs, "sample_L001_R1_001.fastq.gz")
    (raw_seqs / "md5s.txt").write_text(f"{md5_digest}  sample_L001_R1_001.fastq.gz\n")

    before = sorted(path.name for path in raw_seqs.iterdir())
    first = run_checksum_gate(context)
    second = run_checksum_gate(context)
    after = sorted(path.name for path in raw_seqs.iterdir())

    assert first.outcome is second.outcome is GateOutcome.SUCCESS
    assert before == after


def test_gate_declares_its_place_and_produces_nothing():
    """Order 2, needs ``download``, produces nothing, persists nothing.

    ``produces=()`` is the honest statement that verification creates no artifact —
    its only output is the checkmark. A non-empty ``persists`` would make the
    daemon demand a metadata key on SUCCESS and quarantine without it (ADR-0006).
    """
    assert CHECKSUM.order == 2
    assert CHECKSUM.needs == ("download",)
    assert CHECKSUM.satisfied_column == "checksummed"
    assert CHECKSUM.produces == ()
    assert dict(CHECKSUM.persists) == {}


def test_verify_directory_stops_on_shutdown(tmp_path):
    """A set shutdown flag stops verification fail-closed (the 4-hour lesson).

    Every unverified file becomes a problem, so the gate can never report a
    partially-verified run as checksummed.
    """
    from sluice.core import stop

    payload = b"lane data"
    (tmp_path / "lane1_001.fastq.gz").write_bytes(payload)
    expected = {"lane1_001.fastq.gz": hashlib.md5(payload).hexdigest()}

    stop.SHUTDOWN.set()
    try:
        problems = verify_directory(tmp_path, expected)
    finally:
        stop.SHUTDOWN.clear()

    assert len(problems) == 1
    assert "shutdown requested" in problems[0]


def test_verify_directory_emits_per_file_progress(tmp_path):
    """Each verified file emits a progress event — hours of silence, never again."""
    import structlog.testing

    payloads = {f"lane{n}_001.fastq.gz": f"data{n}".encode() for n in range(3)}
    expected = {}
    for filename, payload in payloads.items():
        (tmp_path / filename).write_bytes(payload)
        expected[filename] = hashlib.md5(payload).hexdigest()

    with structlog.testing.capture_logs() as captured:
        problems = verify_directory(tmp_path, expected, run_name="RUN_A")

    assert problems == []
    progress = [e for e in captured if e["event"] == "checksum.progress"]
    assert len(progress) == 3
    assert progress[-1]["checksum.verified"] == 3
    assert progress[-1]["checksum.total"] == 3
    assert progress[-1]["run.name"] == "RUN_A"


def test_verify_directory_hashes_files_in_parallel(tmp_path, monkeypatch):
    """Three files with three workers hash CONCURRENTLY, poller-style.

    The barrier only releases when all three digest computations are in flight
    at once — a sequential implementation deadlocks it and times out. This is
    the poller's verify_checksums shape (ThreadPoolExecutor + as_completed);
    the 4-hour sequential checksum of 3aa/3af was the cost of losing it.
    """
    import threading

    barrier = threading.Barrier(3, timeout=10)

    def digest_after_rendezvous(file_path, algorithm):
        barrier.wait()
        return "match"

    monkeypatch.setattr("sluice.gates.checksum.compute_digest", digest_after_rendezvous)

    expected = {}
    for n in range(3):
        filename = f"lane{n}_001.fastq.gz"
        (tmp_path / filename).write_bytes(b"data")
        expected[filename] = "match"

    problems = verify_directory(tmp_path, expected, max_workers=3)
    assert problems == []


def test_verify_directory_reports_problems_in_filename_order(tmp_path):
    """Problems are deterministic by filename, not by thread completion order."""
    (tmp_path / "b_lane_001.fastq.gz").write_bytes(b"wrong")
    expected = {
        "b_lane_001.fastq.gz": hashlib.md5(b"right").hexdigest(),
        "a_missing_001.fastq.gz": hashlib.md5(b"gone").hexdigest(),
    }

    problems = verify_directory(tmp_path, expected, max_workers=4)

    assert len(problems) == 2
    assert problems[0].startswith("a_missing_001.fastq.gz:")
    assert problems[1].startswith("b_lane_001.fastq.gz:")


def test_shutdown_announces_draining_once(tmp_path):
    """The moment the flag is seen, one ``checksum.draining`` line is emitted.

    Without it, an operator's Ctrl-C is followed only by MORE progress lines
    (the in-flight hashes draining), which reads as "signal ignored" — the
    exact confusion of 2026-09-11. One warning says: acknowledged, finishing
    what's in flight, then stopping.
    """
    import structlog.testing

    from sluice.core import stop

    payloads = {f"lane{n}_001.fastq.gz": f"data{n}".encode() for n in range(3)}
    expected = {}
    for filename, payload in payloads.items():
        (tmp_path / filename).write_bytes(payload)
        expected[filename] = hashlib.md5(payload).hexdigest()

    import sluice.gates.checksum as checksum_module

    real = checksum_module.compute_digest

    def digest_then_request_shutdown(file_path, algorithm):
        digest = real(file_path, algorithm)
        stop.SHUTDOWN.set()
        return digest

    try:
        with structlog.testing.capture_logs() as captured:
            checksum_module.compute_digest = digest_then_request_shutdown
            try:
                problems = verify_directory(tmp_path, expected, max_workers=1)
            finally:
                checksum_module.compute_digest = real
    finally:
        stop.SHUTDOWN.clear()

    draining = [e for e in captured if e["event"] == "checksum.draining"]
    assert len(draining) == 1
    assert any("shutdown requested" in problem for problem in problems)
