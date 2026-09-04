"""The ``download`` gate — order 1, the first heavy-I/O gate (plan 15 increment 2).

Transfers a run's raw lanes from the sequencing server into ``{workdir}/raw_seqs`` with rsync.
Ports python-poller's download stage (PP ``poller.py:314-560``) and carries errata **D1** (CephFS
disk pre-flight), **D4** (the graduated connection ramp), and **D6** (the threadpool contextvar
landmine).

**Resume-first for free.** ``rsync -avP`` skips already-transferred files and continues partial
ones, so re-entering after a crash or a hold costs only the remainder. The gate keeps no transfer
bookkeeping of its own — the filesystem is the record, which is why ``persists`` is empty and
migration 008 adds only the ``downloaded`` checkmark.

**D1 — the disk pre-flight fails OPEN.** Before transferring, the gate checks that the CephFS quota
has ``disk_headroom_multiplier ×`` the run's size free. If either xattr cannot be read, it proceeds
with a warning rather than blocking (PP ``poller.py:968-973``). That asymmetry is deliberate: a
missing xattr means "this filesystem does not report quotas", which must never halt a healthy
pipeline, whereas a genuinely full quota is a real quarantine.

**D6 — the threadpool contextvar landmine, closed structurally.** ``structlog``'s contextvars do
not propagate into worker threads, so run-scoped log context silently vanishes inside a pool. Rather
than re-binding per worker and hoping, the workers here do **pure I/O and return a result** — every
log line is emitted from the submitting thread, where the context is intact. There is no logging
inside a worker to lose context from.
"""

import concurrent.futures
import functools
import pathlib
import subprocess
import time
from typing import Any

import structlog

from sluice.core.gate import Gate, GateOutcome, GateResult, no_op_invalidate
from sluice.core.stop import SHUTDOWN
from sluice.gates.transfer import (
    TransferError,
    is_connection_blocked_error,
    make_transfer_client,
)

__all__ = [
    "DOWNLOAD",
    "RAW_SEQS_DIRNAME",
    "check_disk_headroom",
    "download_run_files",
    "run_download_gate",
]

log = structlog.get_logger("sluice.gates.download")

# The subdirectory of workdir raw lanes land in, matching production's layout
# ({local_staging_dir}/{experiment_id}/raw_seqs — ⚑A, PP ``_experiment_staging_dir``).
RAW_SEQS_DIRNAME = "raw_seqs"

TIB = 1024**4


def _read_ceph_xattr_int(target_path, attribute_name: str, runner=subprocess.run):
    """Read one CephFS extended attribute as an int, or ``None`` if unavailable.

    Shells out to ``getfattr`` rather than using ``os.getxattr`` because CephFS
    quota attributes are virtual and are not always exposed through the syscall on
    every client/mount combination, while ``getfattr --only-values`` is what
    production relies on. Any failure — missing binary, missing attribute,
    unparseable value — is reported as ``None``, which the caller treats as
    "cannot tell" and fails open on.
    """
    try:
        completed = runner(
            ["getfattr", "--only-values", "-n", attribute_name, str(target_path)],
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    try:
        return int(completed.stdout.strip())
    except ValueError:
        return None


def check_disk_headroom(
    quota_root, folder_size_bytes: int, multiplier: int, *, read_xattr=None
) -> tuple[bool, str]:
    """Return ``(ok, reason)`` for whether the quota has room for this run.

    D1, ported from PP ``poller.py:943-991``. Requires ``multiplier ×`` the run's
    size free within the CephFS quota, all arithmetic in bytes to avoid TB/TiB
    ambiguity.

    **Fails OPEN.** If either xattr is unreadable the answer is ``(True, "")`` with
    a warning line — "this filesystem does not report quotas" must never halt a
    healthy pipeline. A quota that genuinely lacks room returns ``(False, reason)``
    and the gate quarantines with that reason.

    Args:
        quota_root: the directory the CephFS quota is set on
            (``config["disk_quota_root"]``).
        folder_size_bytes: the run's raw size in bytes.
        multiplier: headroom factor (``config["disk_headroom_multiplier"]``).
        read_xattr: the ``(path, attribute) -> int | None`` seam; defaults to the
            real ``getfattr`` reader.

    Returns:
        ``(True, "")`` to proceed, or ``(False, human_readable_reason)``.
    """
    read_xattr = read_xattr if read_xattr is not None else _read_ceph_xattr_int

    quota_bytes = read_xattr(quota_root, "ceph.quota.max_bytes")
    used_bytes = read_xattr(quota_root, "ceph.dir.rbytes")

    if quota_bytes is None or used_bytes is None:
        log.warning(
            "download.disk_preflight_skipped",
            **{
                "disk.quota_root": str(quota_root),
                "disk.reason": "CephFS quota/usage xattrs unreadable — failing open",
            },
        )
        return True, ""

    available_bytes = quota_bytes - used_bytes
    required_bytes = multiplier * folder_size_bytes
    if available_bytes >= required_bytes:
        log.info(
            "download.disk_preflight_ok",
            **{
                "disk.required_tib": round(required_bytes / TIB, 2),
                "disk.available_tib": round(available_bytes / TIB, 2),
            },
        )
        return True, ""

    return False, (
        f"insufficient disk space: need {required_bytes / TIB:.2f} TiB "
        f"({multiplier}x run size), only {available_bytes / TIB:.2f} TiB free "
        "in the quota"
    )


def _download_one_pass(
    file_paths,
    destination_dir,
    fetch,
    *,
    max_workers: int,
    wave_size: int,
    ramp_delay_seconds: float,
    sleep,
) -> tuple[int, list[tuple[str, str]]]:
    """Transfer ``file_paths`` into ``destination_dir`` in parallel, ramping up gently.

    D4, ported from PP ``download_files_graduated`` (``poller.py:391``). Files are
    submitted in waves rather than all at once, and only while the pool has spare
    capacity — without the capacity check the executor's queue would absorb every
    future immediately and the ramp delay would do nothing.

    On the first *connection-blocked* failure the ramp **stops widening** for the
    rest of the run: the sequencing server's sshd is saying "too many at once",
    which is a throttling signal rather than a broken transfer. Already-submitted
    work drains normally, and remaining files continue in the narrower regime.

    D6 is closed by construction: workers call ``fetch`` and return: every log line
    here is emitted from the calling thread, so no run-scoped log context can be
    lost crossing into the pool.

    Args:
        file_paths: remote paths to transfer.
        destination_dir: local directory; every file lands flat inside it.
        fetch: ``(remote_path, local_dest) -> Any``, raising ``TransferError``.
        max_workers: pool width.
        wave_size: files submitted per wave.
        ramp_delay_seconds: pause between waves.
        sleep: the delay seam, so tests do not really wait.

    Returns:
        ``(transferred_count, failure_reasons)`` — an empty reason list means all
        files transferred.
    """
    destination_dir = pathlib.Path(destination_dir)
    destination_dir.mkdir(parents=True, exist_ok=True)

    remaining = list(file_paths)
    total = len(remaining)
    transferred = 0
    failures: list[tuple[str, str]] = []
    ramp_paused = False

    def _transfer(remote_path):
        """Worker: pure I/O, returns an outcome. NEVER logs (D6)."""
        try:
            fetch(remote_path, destination_dir / pathlib.Path(remote_path).name)
        except TransferError as transfer_error:
            return remote_path, False, str(transfer_error)
        return remote_path, True, ""

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        in_flight: dict[Any, str] = {}
        while remaining or in_flight:
            # Cooperative stop (Bug B, HANDOFF 3ac): once a signal sets the
            # flag, submit nothing more — in-flight transfers drain, the rest
            # are reported as failures so the gate can never claim success.
            if remaining and SHUTDOWN.is_set():
                log.warning(
                    "download.shutdown_stop",
                    **{"download.not_submitted": len(remaining)},
                )
                failures.extend(
                    (path, "shutdown requested — file not submitted")
                    for path in remaining
                )
                remaining = []
            # Submit the next wave only when the pool has room, so the delay is
            # a real ramp rather than a queue that drains instantly.
            if remaining and not ramp_paused and len(in_flight) < max_workers:
                wave, remaining = remaining[:wave_size], remaining[wave_size:]
                for remote_path in wave:
                    in_flight[executor.submit(_transfer, remote_path)] = remote_path
                log.info(
                    "download.ramp",
                    **{
                        "download.submitted": total - len(remaining),
                        "download.total": total,
                        "download.in_flight": len(in_flight),
                    },
                )
                if remaining:
                    sleep(ramp_delay_seconds)

            done = [future for future in in_flight if future.done()]
            if not done and in_flight:
                # Nothing finished yet; block on the pool rather than spinning.
                # Guarded on in_flight because wait([]) returns instantly, which
                # would busy-loop while the ramp is paused.
                concurrent.futures.wait(
                    list(in_flight), return_when=concurrent.futures.FIRST_COMPLETED
                )
                done = [future for future in in_flight if future.done()]

            for future in done:
                in_flight.pop(future)
                remote_path, succeeded, error_message = future.result()
                if succeeded:
                    transferred += 1
                    continue
                failures.append((remote_path, error_message))
                if not ramp_paused and is_connection_blocked_error(error_message):
                    ramp_paused = True
                    log.warning(
                        "download.ramp_paused",
                        **{
                            "download.in_flight": len(in_flight),
                            "download.reason": (
                                "server rejected an excess connection — pausing "
                                "submission until the pool drains"
                            ),
                        },
                    )

            # PAUSE, not stop (PP poller.py:544). Once the in-flight work has
            # drained, resume submitting the remainder at the same width. Omitting
            # this deadlocks: with the ramp latched off and the pool empty, nothing
            # is ever submitted and nothing ever completes.
            if ramp_paused and not in_flight and remaining:
                ramp_paused = False
                log.info(
                    "download.ramp_resumed",
                    **{"download.remaining": len(remaining)},
                )

    return transferred, failures


def download_run_files(
    file_paths,
    destination_dir,
    fetch,
    *,
    max_workers: int,
    wave_size: int,
    ramp_delay_seconds: float,
    passes: int = 3,
    sleep=time.sleep,
) -> tuple[int, list[str]]:
    """Transfer ``file_paths`` into ``destination_dir``, retrying failures over N passes.

    Wraps the ramped pass above with production's multi-pass strategy (PP
    ``download_with_retry``, ``poller.py:561``). Retrying is nearly free because
    ``rsync -avP`` skips files already transferred, so a second pass costs only the
    files that actually failed.

    This matters operationally more than it looks. The sequencing server rejects
    excess connections under load, and that rejection is transient — but under
    ADR-0002 every gate FAILURE quarantines, which needs a human to clear. Without
    in-pass retries a momentary connection block would strand a healthy run until
    someone noticed. Retrying inside one gate invocation is not a "retry policy" in
    the ADR-0002 sense (no counter, no state, nothing persisted); it is simply how
    this gate does its work.

    Args:
        passes: how many times to attempt the outstanding files. Only the files
            still failing after the last pass are reported.

    Returns:
        ``(transferred_count, failure_reasons)`` — an empty reason list means every
        file is present.
    """
    outstanding = list(file_paths)
    transferred_total = 0
    failures: list[tuple[str, str]] = []

    for pass_number in range(1, passes + 1):
        transferred, failures = _download_one_pass(
            outstanding,
            destination_dir,
            fetch,
            max_workers=max_workers,
            wave_size=wave_size,
            ramp_delay_seconds=ramp_delay_seconds,
            sleep=sleep,
        )
        transferred_total += transferred
        if not failures:
            break
        outstanding = [remote_path for remote_path, _reason in failures]
        if pass_number < passes:
            log.info(
                "download.pass_retry",
                **{
                    "download.pass": pass_number,
                    "download.outstanding": len(outstanding),
                },
            )

    return transferred_total, [
        f"{remote_path}: {reason}" for remote_path, reason in failures
    ]


def run_download_gate(context: Any, make_client) -> GateResult:
    """Transfer this run's raw lanes, after a disk pre-flight. The gate's orchestration.

    ``make_client`` is the zero-arg seam (the ``make_experiment_client`` precedent)
    so nothing is constructed and no credential is read until a transfer is actually
    needed.

    Order of operations, each with its own failure meaning:

    1. **Disk pre-flight (D1)** — insufficient quota is a FAILURE with the shortfall
       named, so the operator sees how much is needed. Unreadable xattrs proceed.
    2. **List the run's files** — a ``TransferError`` here is FAILURE; an empty
       listing is ALSO failure, because "transferred zero files successfully" is
       the silent-success mode this gate must never have.
    3. **Transfer** — any file failing means FAILURE with the reasons, and the
       daemon quarantines. Re-entry resumes rather than restarting.

    Returns ``SUCCESS`` only when every listed file transferred.
    """
    run_row = context.run_row
    run_name = run_row["name"]
    config = context.config
    raw_seqs_dir = pathlib.Path(context.workdir) / RAW_SEQS_DIRNAME

    # 1. Disk pre-flight (D1). size_tb is stamped by detection; a NULL size means
    # detection could not size the run, so there is nothing to check against and
    # the pre-flight is skipped in the same fail-open spirit.
    size_tb = run_row["size_tb"]
    if size_tb is not None:
        headroom_ok, headroom_reason = check_disk_headroom(
            config["disk_quota_root"],
            int(size_tb * TIB),
            config["disk_headroom_multiplier"],
        )
        if not headroom_ok:
            return GateResult(
                GateOutcome.FAILURE,
                reason=f"download gate: {headroom_reason} (run {run_name!r})",
            )

    client = make_client()

    # 2. List. Fail closed — an empty list must never read as a clean download.
    try:
        file_paths = client.list_run_files(run_name)
    except TransferError as listing_error:
        return GateResult(
            GateOutcome.FAILURE,
            reason=f"download gate: could not list run {run_name!r}: {listing_error}",
        )
    if not file_paths:
        return GateResult(
            GateOutcome.FAILURE,
            reason=(
                f"download gate: run {run_name!r} listed zero files — refusing to "
                "report a successful download of nothing"
            ),
        )

    # 3. Transfer.
    transferred, failures = download_run_files(
        file_paths,
        raw_seqs_dir,
        client.fetch,
        max_workers=config["parallel_downloads"],
        wave_size=config["download_wave_size"],
        ramp_delay_seconds=config["download_ramp_delay_seconds"],
    )
    if failures:
        shown = "; ".join(failures[:3])
        more = f" (+{len(failures) - 3} more)" if len(failures) > 3 else ""
        return GateResult(
            GateOutcome.FAILURE,
            reason=(
                f"download gate: {len(failures)} of {len(file_paths)} files failed "
                f"for run {run_name!r}: {shown}{more}"
            ),
        )
    return GateResult(
        GateOutcome.SUCCESS,
        metadata={"downloaded_files": transferred},
    )


def _run_download(context: Any) -> GateResult:
    """Production wiring: defer client construction behind the zero-arg seam."""
    return run_download_gate(
        context, functools.partial(make_transfer_client, context.config)
    )


DOWNLOAD = Gate(
    id="download",
    name="Download raw lanes (rsync)",
    satisfied_column="downloaded",  # migration 008
    order=1,  # TRUE execution order — after experiment (order 0)
    needs=("experiment",),  # staging is keyed by experiment_id (⚑A)
    produces=("raw_seqs/",),
    run=_run_download,
    # ADR-0005: the honest undo would delete a multi-TB transfer, which sluice
    # never does automatically. Re-running is safe anyway — ``rsync -avP``
    # re-verifies and resumes rather than re-downloading.
    invalidate=no_op_invalidate,
    terminate=None,  # rsync resumes; interruption is handled by re-entry (⚑E)
    persists={},  # the filesystem is the record — no token to persist
)
"""The ``download`` gate — Plan B gate 1, the first heavy-I/O gate.

Depends on ``experiment`` because ⚑A keys staging on ``experiment_id``: running before gate 0 would
stage into the name-keyed fallback path and the data would land somewhere later gates do not look.
"""
