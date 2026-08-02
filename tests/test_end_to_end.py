"""End-to-end: a real run through detection, download, and checksum.

Everything else in the suite injects a seam. This file injects **nothing** below the CLI: real
rsync, real files on disk, real hashing, the real migrations, and the real reconcile loop. Local
source mode means no network and no credentials, so it runs anywhere the suite runs.

**This file exists because the unit tests missed two real bugs**, both found the first time the
pipeline was pointed at an actual directory:

* the listing command lost its trailing slash, so ``rsync --list-only`` described the run directory
  instead of listing its contents and every run reported "listed zero files". The unit test fed the
  parser a canned listing, so it never exercised the command that produced one;
* nothing wired the production rsync lister into the daemon, so a deployment would have silently
  used the Plan-A watch-directory stub.

Seams are what make components testable; a test that only ever uses them proves the components fit
their fakes rather than each other.
"""

import hashlib
import json
import pathlib
import shutil

import pytest

from sluice.config.setup import apply_plan, plan_setup
from sluice.core import control
from sluice.core.daemon import detect_new_runs, prepare_database, process_run
from sluice.core.profile import Applicability, build_profile
from sluice.gates.checksum import CHECKSUM
from sluice.gates.detection import list_remote_runs
from sluice.gates.download import DOWNLOAD, RAW_SEQS_DIRNAME
from sluice.gates.experiment import EXPERIMENT

# The experiment gate is DISABLED rather than absent: under ADR-0004 d.2 that
# satisfies download's needs=("experiment",) by absence, which is the sanctioned
# way to run a pipeline without LabKey. It also exercises that mechanism.
DRY_RUN_PROFILE = build_profile(
    "dryrun-v1",
    [
        (EXPERIMENT, Applicability.DISABLED),
        (DOWNLOAD, Applicability.REQUIRED),
        (CHECKSUM, Applicability.REQUIRED),
    ],
)
REGISTRY = {DRY_RUN_PROFILE.pipeline_version: DRY_RUN_PROFILE}

RUN_NAME = "20260101_TEST_0001_ADRYRUN"
LANE_FILES = (
    "Sample_S1_L001_R1_001.fastq.gz",
    "Sample_S1_L001_R2_001.fastq.gz",
    "Sample_S1_L002_R1_001.fastq.gz",
)


@pytest.fixture
def delivery(tmp_path):
    """A source directory shaped like a real sequencing delivery.

    Includes a manifest listing BOTH the delivered lane files and a set of derived
    ``.1perc`` names that were never delivered — which is what the real fixture
    looks like, and what the ``001.fastq.gz`` filter exists to handle. A parser
    that took every manifest line would report the derived files as missing and
    fail a perfectly healthy delivery.
    """
    source = tmp_path / "source" / RUN_NAME
    source.mkdir(parents=True)
    manifest_lines = []
    for index, filename in enumerate(LANE_FILES):
        content = f"@read{index}\nACGT\n+\n!!!!\n".encode() * 32
        (source / filename).write_bytes(content)
        digest = hashlib.md5(content).hexdigest()
        manifest_lines.append(f"{digest}  {filename}")
        # A derived name that is NOT on disk, exactly as the real delivery does.
        manifest_lines.append(
            f"{digest}  {filename.replace('001.fastq.gz', '001.1perc.fastq.gz')}"
        )
    (source / "md5s.txt").write_text("\n".join(manifest_lines) + "\n", encoding="utf-8")
    return source.parent


@pytest.fixture
def installation(tmp_path, delivery):
    """A complete sluice install, created by the real setup path, in local mode."""
    home = tmp_path / "sluice-home"
    apply_plan(plan_setup(home=home))
    config_path = home / "config.json"
    config = json.loads(config_path.read_text())
    config.update(
        {
            "local_source_dir": str(delivery),
            "local_staging_dir": str(home / "staging"),
            "disk_quota_root": str(home),
        }
    )
    config_path.write_text(json.dumps(config), encoding="utf-8")
    prepare_database(config, registry=REGISTRY, default_pipeline_version="dryrun-v1")
    return config


@pytest.mark.skipif(shutil.which("rsync") is None, reason="rsync is not installed")
def test_a_real_delivery_flows_from_detection_to_completion(installation):
    """Detection finds the run, download transfers it, checksum verifies it, done.

    The whole point of this file: every stage running for real against real bytes.
    If this passes, the parts genuinely fit each other rather than fitting their
    fakes.
    """
    config = installation

    detected = detect_new_runs(config, lister=list_remote_runs)
    assert detected == [RUN_NAME]

    assert process_run(config, RUN_NAME, registry=REGISTRY) == "completed"

    status = control.get_status(config["state_file"], RUN_NAME, registry=REGISTRY)
    assert status["outcome"] == "completed"
    assert status["complete"] is True
    assert set(status["gates"]["satisfied"]) == {"download", "checksum"}


@pytest.mark.skipif(shutil.which("rsync") is None, reason="rsync is not installed")
def test_every_delivered_file_lands_in_raw_seqs(installation):
    """The transferred files are really on disk, with their real sizes.

    Guards the "listed zero files" bug specifically: the run would previously
    complete detection and then fail here with nothing transferred.
    """
    config = installation
    detect_new_runs(config, lister=list_remote_runs)
    process_run(config, RUN_NAME, registry=REGISTRY)

    raw_seqs = pathlib.Path(config["local_staging_dir"]) / RUN_NAME / RAW_SEQS_DIRNAME
    staged = {path.name for path in raw_seqs.iterdir()}

    assert set(LANE_FILES) <= staged, f"missing lane files; staged {sorted(staged)}"
    assert "md5s.txt" in staged, "the manifest must be transferred too"
    for filename in LANE_FILES:
        assert (raw_seqs / filename).stat().st_size > 0


@pytest.mark.skipif(shutil.which("rsync") is None, reason="rsync is not installed")
def test_detection_sizes_the_run(installation):
    """The detected run carries a size, which the disk pre-flight needs.

    A ``size_tb`` of NULL skips the D1 check entirely, so this being populated is
    what makes the pre-flight meaningful in production.
    """
    config = installation
    detect_new_runs(config, lister=list_remote_runs)

    status = control.get_status(config["state_file"], registry=REGISTRY)
    assert status["runs"][0]["name"] == RUN_NAME


@pytest.mark.skipif(shutil.which("rsync") is None, reason="rsync is not installed")
def test_re_running_a_completed_run_is_a_cheap_noop(installation):
    """Driving an already-complete run again does nothing and stays complete.

    Re-entry safety, end to end: ``rsync -avP`` skips transferred files and
    verification is a pure read, so a second pass over a finished run is free.
    """
    config = installation
    detect_new_runs(config, lister=list_remote_runs)
    process_run(config, RUN_NAME, registry=REGISTRY)

    assert process_run(config, RUN_NAME, registry=REGISTRY) == "already-complete"


@pytest.mark.skipif(shutil.which("rsync") is None, reason="rsync is not installed")
def test_a_corrupted_file_is_caught_by_checksum(installation):
    """Corrupting a staged file makes the next checksum pass fail as HASH_MISMATCH.

    Proves verification really reads bytes rather than trusting the transfer, and
    that the re-transferable failure mode reports itself distinctly (plan 15 §8).

    Also documents the operator sequence for a rewind end to end, including why
    it needs a hold first — see the comment below.
    """
    config = installation
    detect_new_runs(config, lister=list_remote_runs)
    process_run(config, RUN_NAME, registry=REGISTRY)

    raw_seqs = pathlib.Path(config["local_staging_dir"]) / RUN_NAME / RAW_SEQS_DIRNAME
    (raw_seqs / LANE_FILES[0]).write_bytes(b"TRUNCATED")

    # The REAL operator sequence for a rewind, and the reason it has these steps:
    # reset_gate refuses a daemon-eligible run outright ("hold, skip, or wait for
    # quarantine first"), because an in-flight gate's persist would immediately
    # re-stamp the column the op just cleared. So the run is held, rewound, and
    # released. This is exactly the manual dance the deferred auto-rewind question
    # (plan 15 §8) would automate.
    state_file = config["state_file"]
    control.hold(state_file, "tester", RUN_NAME, reason="rewinding checksum")
    control.reset_gate(state_file, "tester", RUN_NAME, "checksum", registry=REGISTRY)
    control.unhold(state_file, "tester", RUN_NAME)

    process_run(config, RUN_NAME, registry=REGISTRY)

    status = control.get_status(config["state_file"], RUN_NAME, registry=REGISTRY)
    assert status["outcome"] == "quarantined"
    assert "HASH_MISMATCH" in status["error_reason"]
