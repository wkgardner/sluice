"""Behavioral tests for the ``download`` gate and the rsync transfer adapter (plan 15 increment 2).

Everything here runs offline: the ``subprocess.run``-shaped ``runner`` and the ``fetch``/xattr seams
are injected, so no rsync, no sshpass, no network, and no credential is involved. That is the point
of the adapter boundary — the world stays outside and the orchestration stays testable.
"""

import subprocess
import types

import pytest

from sluice.core.gate import GateOutcome
from sluice.gates.download import (
    DOWNLOAD,
    RAW_SEQS_DIRNAME,
    check_disk_headroom,
    download_run_files,
    run_download_gate,
)
from sluice.gates.transfer import (
    REDACTED,
    RsyncTransferClient,
    TransferError,
    is_connection_blocked_error,
)

TIB = 1024**4


def make_config(tmp_path, **overrides):
    """A config carrying every key the download gate reads, in local (no-credential) mode."""
    config = {
        "local_source_dir": str(tmp_path / "source"),
        "local_staging_dir": str(tmp_path / "staging"),
        "state_file": str(tmp_path / "state.db"),
        "disk_quota_root": str(tmp_path),
        "disk_headroom_multiplier": 2,
        "parallel_downloads": 2,
        "download_wave_size": 2,
        "download_ramp_delay_seconds": 0,
    }
    config.update(overrides)
    return config


def make_context(tmp_path, *, size_tb=1.0, name="run_A", **config_overrides):
    """A RunContext-shaped stand-in: the gate reads workdir, config, and run_row only."""
    return types.SimpleNamespace(
        workdir=tmp_path / "staging" / "4471",
        config=make_config(tmp_path, **config_overrides),
        run_row={"name": name, "experiment_id": 4471, "size_tb": size_tb},
    )


# ---------------------------------------------------------------------------
# The transfer adapter — credentials, modes, fail-closed translation.
# ---------------------------------------------------------------------------


def test_ssh_command_never_contains_the_password_in_its_rendered_form(tmp_path):
    """``describe_command`` renders the password position as ``[REDACTED]``.

    The single most important test in this file. Production logs its rsync command
    template for debugging, and the only reason that is safe is the redaction (PP
    ``poller.py:440``). If this regresses, a real credential lands in the events
    jsonl — permanently, and readable by anyone with the log (ADR-0002, errata C5).
    """
    password_file = tmp_path / "secret"
    password_file.write_text("hunter2-not-a-real-password\n", encoding="utf-8")
    client = RsyncTransferClient(
        {
            "remote_user": "svc",
            "remote_host": "seq.example.edu",
            "password_file": str(password_file),
        }
    )

    rendered = client.describe_command(
        "run_A/lane1.fastq.gz", "/staging/lane1.fastq.gz"
    )

    assert REDACTED in rendered
    assert "hunter2-not-a-real-password" not in rendered, (
        "the remote password appeared in a rendered command — it must never reach "
        "a log line or an error message (ADR-0002, errata C5)."
    )


def test_transfer_error_message_never_carries_the_password(tmp_path):
    """A failed transfer names the path and rsync's stderr, never the credential.

    The other half of the leak surface: an exception message travels into the run's
    quarantine reason, which is stored in the database and shown to operators.
    """
    password_file = tmp_path / "secret"
    password_file.write_text("hunter2-not-a-real-password\n", encoding="utf-8")

    def failing_runner(command, **kwargs):
        raise subprocess.CalledProcessError(
            returncode=23, cmd=command, stderr="rsync: permission denied"
        )

    client = RsyncTransferClient(
        {
            "remote_user": "svc",
            "remote_host": "seq.example.edu",
            "password_file": str(password_file),
        },
        runner=failing_runner,
    )

    with pytest.raises(TransferError) as raised:
        client.fetch("run_A/lane1.fastq.gz", tmp_path / "lane1.fastq.gz")

    assert "hunter2-not-a-real-password" not in str(raised.value)
    assert "permission denied" in str(raised.value)


def test_local_mode_needs_no_credentials_at_all(tmp_path):
    """With ``local_source_dir`` set, no password file is read and none is required.

    This is what makes an end-to-end rehearsal against a staged copy possible with
    no secret anywhere in the loop.
    """
    captured = {}

    def capturing_runner(command, **kwargs):
        captured["command"] = command
        return types.SimpleNamespace(stdout="", returncode=0)

    client = RsyncTransferClient(
        {"local_source_dir": str(tmp_path / "src")}, runner=capturing_runner
    )
    client.fetch("run_A/lane1.fastq.gz", tmp_path / "dest" / "lane1.fastq.gz")

    assert "sshpass" not in captured["command"]
    assert client.is_local_mode is True


def test_rsync_always_uses_the_resume_flags(tmp_path):
    """Every transfer passes ``-avP`` — the entire resume story.

    ``-P`` is what makes re-entry cheap: transferred files are skipped and partial
    ones continue. Losing it would silently turn every retry into a full
    re-download of a multi-TB run.
    """
    captured = {}

    def capturing_runner(command, **kwargs):
        captured["command"] = command
        return types.SimpleNamespace(stdout="", returncode=0)

    client = RsyncTransferClient(
        {"local_source_dir": str(tmp_path / "src")}, runner=capturing_runner
    )
    client.fetch("run_A/lane1.fastq.gz", tmp_path / "dest" / "lane1.fastq.gz")

    assert "-avP" in captured["command"]


def test_listing_drops_directories_and_keeps_files(tmp_path):
    """``list_run_files`` returns files only, prefixed with the run directory."""
    listing = (
        "drwxr-xr-x          4,096 2026/07/30 09:12:01 .\n"
        "-rw-r--r--  1,234,567,890 2026/07/30 09:12:01 lane1_001.fastq.gz\n"
        "-rw-r--r--            256 2026/07/30 09:12:02 md5s.txt\n"
        "drwxr-xr-x          4,096 2026/07/30 09:12:03 Reports\n"
    )

    client = RsyncTransferClient(
        {"local_source_dir": str(tmp_path / "src")},
        runner=lambda command, **kwargs: types.SimpleNamespace(
            stdout=listing, returncode=0
        ),
    )

    assert client.list_run_files("run_A") == [
        "run_A/lane1_001.fastq.gz",
        "run_A/md5s.txt",
    ]


def test_connection_blocked_detection_distinguishes_throttling_from_failure():
    """Server-side connection rejection is recognised, a real error is not.

    Conflating the two would either quarantine healthy runs or hammer a server
    that is asking us to back off.
    """
    assert is_connection_blocked_error("rsync: Connection closed by remote host")
    assert is_connection_blocked_error(
        "connection unexpectedly closed (0 bytes received so far)"
    )
    assert not is_connection_blocked_error("rsync: permission denied (13)")


# ---------------------------------------------------------------------------
# D1 — the disk pre-flight.
# ---------------------------------------------------------------------------


def test_disk_preflight_fails_open_when_xattrs_are_unreadable():
    """Unreadable CephFS xattrs proceed with a warning rather than blocking.

    Deliberate asymmetry (PP ``poller.py:968-973``): an unreadable xattr means the
    filesystem does not report quotas, which must never halt a healthy pipeline. A
    genuinely full quota is a different answer entirely — see the test below.
    """
    ok, reason = check_disk_headroom(
        "/quota", 1 * TIB, 2, read_xattr=lambda path, attribute: None
    )

    assert ok is True
    assert reason == ""


def test_disk_preflight_refuses_when_the_quota_lacks_multiplied_headroom():
    """Insufficient quota returns a refusal naming the shortfall in TiB.

    The reason becomes the run's quarantine text, so it has to tell the operator
    how much is needed rather than merely that something is wrong.
    """
    values = {"ceph.quota.max_bytes": 10 * TIB, "ceph.dir.rbytes": 9 * TIB}

    ok, reason = check_disk_headroom(
        "/quota", 2 * TIB, 2, read_xattr=lambda path, attribute: values[attribute]
    )

    assert ok is False
    assert "4.00 TiB" in reason and "1.00 TiB" in reason


def test_disk_preflight_passes_with_room_to_spare():
    """Ample free quota proceeds."""
    values = {"ceph.quota.max_bytes": 100 * TIB, "ceph.dir.rbytes": 10 * TIB}

    ok, reason = check_disk_headroom(
        "/quota", 1 * TIB, 2, read_xattr=lambda path, attribute: values[attribute]
    )

    assert ok is True and reason == ""


# ---------------------------------------------------------------------------
# D4 — the graduated ramp.
# ---------------------------------------------------------------------------


def test_all_files_transfer_and_land_flat_in_the_destination(tmp_path):
    """Every listed file is fetched once, flattened into the destination directory."""
    fetched = []

    def fetch(remote_path, local_dest):
        fetched.append((remote_path, local_dest))

    transferred, failures = download_run_files(
        [f"run_A/lane{n}.fastq.gz" for n in range(5)],
        tmp_path / "raw_seqs",
        fetch,
        max_workers=2,
        wave_size=2,
        ramp_delay_seconds=0,
        sleep=lambda _seconds: None,
    )

    assert transferred == 5
    assert failures == []
    assert {remote for remote, _dest in fetched} == {
        f"run_A/lane{n}.fastq.gz" for n in range(5)
    }
    assert all(dest.parent.name == "raw_seqs" for _remote, dest in fetched)


def test_ramp_stops_widening_after_the_server_rejects_a_connection(tmp_path):
    """A connection-blocked failure stops the ramp instead of quarantining outright.

    D4's whole purpose. The server saying "too many at once" is a throttling signal;
    treating it as a broken transfer would quarantine a perfectly healthy run.
    """

    def fetch(remote_path, local_dest):
        if remote_path.endswith("lane0.fastq.gz"):
            raise TransferError("connection unexpectedly closed (0 bytes received)")

    transferred, failures = download_run_files(
        [f"run_A/lane{n}.fastq.gz" for n in range(6)],
        tmp_path / "raw_seqs",
        fetch,
        max_workers=2,
        wave_size=2,
        ramp_delay_seconds=0,
        sleep=lambda _seconds: None,
    )

    # The rejected file is reported; the rest still transfer.
    assert len(failures) == 1
    assert transferred == 5


def test_a_real_transfer_failure_is_reported_not_swallowed(tmp_path):
    """A genuine rsync failure surfaces as a reason rather than a silent skip."""

    def fetch(remote_path, local_dest):
        raise TransferError("rsync: permission denied (13)")

    transferred, failures = download_run_files(
        ["run_A/lane0.fastq.gz"],
        tmp_path / "raw_seqs",
        fetch,
        max_workers=1,
        wave_size=1,
        ramp_delay_seconds=0,
        sleep=lambda _seconds: None,
    )

    assert transferred == 0
    assert len(failures) == 1 and "permission denied" in failures[0]


# ---------------------------------------------------------------------------
# The gate's orchestration.
# ---------------------------------------------------------------------------


class FakeClient:
    """A transfer client that lists a fixed set of files and records what it fetched."""

    def __init__(self, files, *, fetch_error=None, listing_error=None):
        self.files = files
        self.fetch_error = fetch_error
        self.listing_error = listing_error
        self.fetched = []

    def list_run_files(self, run_name):
        if self.listing_error is not None:
            raise self.listing_error
        return self.files

    def fetch(self, remote_path, local_dest):
        if self.fetch_error is not None:
            raise self.fetch_error
        self.fetched.append(remote_path)


def test_gate_succeeds_when_every_file_transfers(tmp_path):
    """A clean transfer of every listed file is SUCCESS, into ``workdir/raw_seqs``."""
    client = FakeClient(["run_A/lane0.fastq.gz", "run_A/md5s.txt"])
    context = make_context(tmp_path)

    result = run_download_gate(context, lambda: client)

    assert result.outcome is GateOutcome.SUCCESS
    assert (context.workdir / RAW_SEQS_DIRNAME).is_dir()
    assert len(client.fetched) == 2


def test_gate_fails_when_the_run_lists_zero_files(tmp_path):
    """An empty listing is FAILURE, never a successful download of nothing.

    The silent-success mode this gate must not have: a listing that returns nothing
    (wrong path, permissions, a server hiccup) would otherwise mark the run
    downloaded and let every later gate operate on an empty directory.
    """
    context = make_context(tmp_path)

    result = run_download_gate(context, lambda: FakeClient([]))

    assert result.outcome is GateOutcome.FAILURE
    assert "zero files" in result.reason


def test_gate_fails_closed_when_listing_raises(tmp_path):
    """A listing error quarantines rather than proceeding with a partial view."""
    context = make_context(tmp_path)
    client = FakeClient([], listing_error=TransferError("host unreachable"))

    result = run_download_gate(context, lambda: client)

    assert result.outcome is GateOutcome.FAILURE
    assert "host unreachable" in result.reason


def test_gate_fails_when_the_disk_preflight_refuses(tmp_path):
    """A full quota quarantines BEFORE any transfer starts, and before a client exists.

    Ordering matters: the pre-flight must run before ``make_client``, so a run that
    cannot possibly fit never opens a connection or reads a credential.
    """
    context = make_context(tmp_path, size_tb=100.0)
    values = {"ceph.quota.max_bytes": 10 * TIB, "ceph.dir.rbytes": 9 * TIB}

    def exploding_client():
        raise AssertionError(
            "the transfer client must not be constructed when the disk pre-flight "
            "already refused — no connection, no credential read"
        )

    import sluice.gates.download as download_module

    original = download_module.check_disk_headroom
    download_module.check_disk_headroom = lambda root, size, mult, **kw: original(
        root, size, mult, read_xattr=lambda path, attribute: values[attribute]
    )
    try:
        result = run_download_gate(context, exploding_client)
    finally:
        download_module.check_disk_headroom = original

    assert result.outcome is GateOutcome.FAILURE
    assert "insufficient disk space" in result.reason


def test_gate_metadata_declares_nothing_to_persist(tmp_path):
    """``DOWNLOAD.persists`` is empty — the filesystem is the record.

    Unlike the experiment gate, this one produces no token worth a column: whether
    the data is present is answered by the data being present. A stray ``persists``
    entry would make the daemon demand a metadata key on SUCCESS and quarantine
    when it is missing (ADR-0006 d.2).
    """
    assert dict(DOWNLOAD.persists) == {}


def test_gate_declares_its_place_in_the_pipeline():
    """Order 1, needs ``experiment``, marks ``downloaded``.

    ``needs`` is load-bearing rather than decorative: ⚑A keys staging on
    ``experiment_id``, so downloading before gate 0 would stage into the name-keyed
    fallback path, where no later gate looks.
    """
    assert DOWNLOAD.order == 1
    assert DOWNLOAD.needs == ("experiment",)
    assert DOWNLOAD.satisfied_column == "downloaded"


def test_a_transient_failure_recovers_on_a_later_pass(tmp_path):
    """A file that fails once and succeeds on retry ends the gate clean.

    The operational safety net (PP ``download_with_retry``, ``poller.py:561``). The
    sequencing server rejects excess connections under load and that rejection is
    transient — but under ADR-0002 every gate FAILURE quarantines and needs a human
    to clear. Without in-pass retries a momentary block would strand a healthy run
    until somebody noticed. Retrying costs almost nothing because ``rsync -avP``
    skips files already transferred.
    """
    attempts = {"count": 0}

    def flaky_fetch(remote_path, local_dest):
        if remote_path.endswith("lane0.fastq.gz"):
            attempts["count"] += 1
            if attempts["count"] == 1:
                raise TransferError("connection unexpectedly closed (0 bytes received)")

    transferred, failures = download_run_files(
        ["run_A/lane0.fastq.gz", "run_A/lane1.fastq.gz"],
        tmp_path / "raw_seqs",
        flaky_fetch,
        max_workers=2,
        wave_size=2,
        ramp_delay_seconds=0,
        sleep=lambda _seconds: None,
    )

    assert failures == [], (
        "a file that failed once and succeeded on retry must not leave the gate "
        "in failure — that would quarantine a healthy run for a transient block"
    )
    assert transferred == 2
    assert attempts["count"] == 2, (
        "the failed file should have been retried exactly once more"
    )


def test_a_permanent_failure_still_fails_after_every_pass(tmp_path):
    """Retries do not mask a genuinely broken transfer.

    The other half of the retry contract: passes exist to absorb transient
    throttling, not to turn a real error (permissions, corruption, a missing file)
    into a silent success.
    """

    def always_fails(remote_path, local_dest):
        raise TransferError("rsync: permission denied (13)")

    transferred, failures = download_run_files(
        ["run_A/lane0.fastq.gz"],
        tmp_path / "raw_seqs",
        always_fails,
        max_workers=1,
        wave_size=1,
        ramp_delay_seconds=0,
        passes=2,
        sleep=lambda _seconds: None,
    )

    assert transferred == 0
    assert len(failures) == 1 and "permission denied" in failures[0]


def test_the_listing_command_keeps_the_trailing_slash(tmp_path):
    """``list_run_files`` asks rsync for the directory's CONTENTS, not its entry.

    ``rsync --list-only <dir>`` prints one line describing the directory itself;
    only ``<dir>/`` lists what is inside it. The slash therefore decides whether a
    run appears to hold its files or to be empty.

    It has to be appended after path construction because pathlib strips it —
    ``Path("/a/b") / "run/"`` is ``/a/b/run``. That is exactly how it was lost,
    and no unit test caught it: this file's other listing test feeds the parser a
    canned listing, so it never exercised the command that produced it. The bug
    only surfaced on a real end-to-end run, where every run reported "listed zero
    files".
    """
    captured = {}

    def capturing_runner(command, **kwargs):
        captured["command"] = command
        return types.SimpleNamespace(stdout="", returncode=0)

    client = RsyncTransferClient(
        {"local_source_dir": str(tmp_path / "src")}, runner=capturing_runner
    )
    client.list_run_files("RUN_A")

    source_argument = captured["command"][-1]
    assert source_argument.endswith("RUN_A/"), (
        f"the listing source must end with a trailing slash, got "
        f"{source_argument!r} — without it rsync lists the directory entry "
        "rather than its contents and every run looks empty"
    )
