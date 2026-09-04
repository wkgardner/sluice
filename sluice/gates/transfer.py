"""The rsync world adapter — the boundary to the sequencing server's filesystem.

A **world adapter** in the `CONTEXT.md` sense: it owns the subprocess calls, the credentials, and
the fail-closed translation of every transfer failure into one error type (``TransferError``). Lives
under ``sluice.gates`` because it shells out and reads secrets; core never imports it.

**Credential handling is the load-bearing part.** The remote password is read lazily from
``config["password_file"]`` at the moment a command is built, and it is never returned, never
stored on the adapter, never logged, and never formatted into an exception message. Production does
exactly this and logs its command template with the password replaced by a literal ``[REDACTED]``
(PP ``poller.py:440``); ``describe_command`` below is that redaction, made the ONLY way a caller can
render a command for logs. ADR-0002's no-secrets rule and errata C5 both land here.

**Two modes**, matching production (PP ``poller.py:314``): if ``local_source_dir`` is set the
transfer is a plain local ``rsync`` with no credentials at all — the mode used for testing against
a staged copy; otherwise it is ``sshpass -p <password> rsync`` against
``{remote_user}@{remote_host}``.

``rsync -avP`` is what makes the download gate resume-first for free: it skips files already
transferred and continues partial ones, so re-running the gate after a crash costs only the
un-transferred remainder (PP ``:337``).
"""

import pathlib
import subprocess

__all__ = [
    "REDACTED",
    "TransferError",
    "RsyncTransferClient",
    "is_connection_blocked_error",
    "make_transfer_client",
]

# The literal that stands in for the password anywhere a command is rendered.
REDACTED = "[REDACTED]"

# rsync flags, fixed: archive + verbose + partial/progress. -P is the resume half
# (partial files continue, transferred files are skipped), which is why the gate
# above needs no resume bookkeeping of its own.
RSYNC_FLAGS = ("-avP",)

# Substrings identifying the sequencing server's sshd closing an excess connection
# rather than a genuine transfer failure (PP ``poller.py:374``). Distinguishing
# them is what lets the ramp back off instead of quarantining a healthy run.
_CONNECTION_BLOCKED_MARKERS = (
    "connection closed by",
    "connection reset by",
    "connection unexpectedly closed (0 bytes received",
)


class TransferError(RuntimeError):
    """A transfer failed, with every world failure funnelled through this one type.

    Carries the remote path and the underlying stderr, and **never** the password —
    the adapter builds the credential into the argv list only, so it cannot reach a
    message by construction rather than by discipline.
    """


def is_connection_blocked_error(error_message: str) -> bool:
    """Return whether ``error_message`` is the server rejecting an excess connection.

    The sequencing server's sshd closes connections beyond its limit with zero bytes
    transferred, which looks like a failure but means "too many at once, back off"
    — materially different from a corrupt or missing file. The download ramp uses
    this to stop widening instead of treating a healthy run as broken.

    Case-insensitive because the exact casing varies across ssh/rsync versions.
    """
    lowered = error_message.lower()
    return any(marker in lowered for marker in _CONNECTION_BLOCKED_MARKERS)


# The transfer scope, matching the poller's rsync include filters: raw lane
# fastqs and checksum manifests ONLY. Production folders also carry
# ``*_trimmed.fastq.gz`` / ``*.stats.txt`` / ``*fastp.json``; NVD does its own
# trimming, so fetching those is terabytes of derivable data per run.
_PIPELINE_INPUT_SUFFIXES = ("001.fastq.gz", "md5s.txt", ".sha256")


def _parse_listing_filenames(listing_output: str, run_name: str) -> list[str]:
    """Extract pipeline-input file paths from ``rsync --list-only`` output.

    rsync lines look like ``-rw-r--r--  1,234,567 2026/07/30 09:12:01 file.fastq.gz``:
    permissions, size, date, time, then the name — so the name is everything after
    the fourth whitespace-split field, and a leading ``d`` marks a directory. The
    name is split off with ``maxsplit=4`` rather than ``[-1]`` because filenames can
    contain spaces, and ``.``/``..`` self-entries are dropped the same way any other
    non-file is. Files outside ``_PIPELINE_INPUT_SUFFIXES`` are dropped — the
    same scope the poller's ``--include`` filters enforced, so "listed zero
    files" means "zero pipeline inputs".
    """
    filenames = []
    for line in listing_output.splitlines():
        fields = line.split(maxsplit=4)
        if len(fields) < 5:
            continue
        permissions, name = fields[0], fields[4].strip()
        if permissions.startswith("d") or name in ("", ".", ".."):
            continue
        if not name.lower().endswith(_PIPELINE_INPUT_SUFFIXES):
            continue
        filenames.append(f"{run_name}/{name}")
    return filenames


def _load_password(password_file) -> str:
    """Read the remote password from its file, stripped of the trailing newline.

    Read at command-build time and immediately dropped into the argv list — never
    returned to a caller, cached on an object, or held longer than the call.
    """
    return pathlib.Path(password_file).read_text(encoding="utf-8").strip()


class RsyncTransferClient:
    """Fetches remote files with ``rsync``, in either local or ssh mode.

    Built from config by ``make_transfer_client``; the download gate holds one and
    calls ``fetch`` per file. Stateless apart from the config mapping — in
    particular it does NOT hold the password, which is re-read per command so it is
    never resident on a long-lived object.
    """

    def __init__(self, config, runner=subprocess.run):
        """Store config and the subprocess runner seam.

        Args:
            config: the loaded config; read by subscript for the mode's required
                keys, so a missing key fails loud at first use (guardrail §8).
            runner: the ``subprocess.run``-shaped callable actually invoked. The
                seam every test drives instead of shelling out.
        """
        self._config = config
        self._runner = runner

    @property
    def is_local_mode(self) -> bool:
        """Whether this client transfers from a local directory rather than over ssh.

        Local mode needs no credentials at all, which is what makes an end-to-end
        test against a staged copy possible with no secret anywhere in the loop.
        """
        return bool(self._config.get("local_source_dir"))

    def _remote_source(self, remote_path: str) -> str:
        """Render the rsync source argument for ``remote_path`` in the active mode."""
        if self.is_local_mode:
            return str(pathlib.Path(self._config["local_source_dir"]) / remote_path)
        return (
            f"{self._config['remote_user']}@{self._config['remote_host']}:{remote_path}"
        )

    def describe_command(self, remote_path: str, local_dest) -> str:
        """Render this transfer's command for a log line, with the password REDACTED.

        The only sanctioned way to put a transfer command into a log or an error.
        In ssh mode the password position holds the literal ``[REDACTED]`` — never
        the value, not even truncated (PP ``poller.py:440``; ADR-0002, errata C5).
        """
        source = self._remote_source(remote_path)
        if self.is_local_mode:
            return f"rsync {' '.join(RSYNC_FLAGS)} {source} {local_dest}"
        return (
            f"sshpass -p {REDACTED} rsync {' '.join(RSYNC_FLAGS)} {source} {local_dest}"
        )

    def _build_command(self, remote_path: str, local_dest) -> list[str]:
        """Build the argv list, reading the password only if ssh mode needs it.

        The password enters as its own argv element, so it is never substring-
        searchable in a rendered command string this module produces.
        """
        source = self._remote_source(remote_path)
        if self.is_local_mode:
            return ["rsync", *RSYNC_FLAGS, source, str(local_dest)]
        password = _load_password(self._config["password_file"])
        return [
            "sshpass",
            "-p",
            password,
            "rsync",
            *RSYNC_FLAGS,
            source,
            str(local_dest),
        ]

    def list_run_files(self, run_name: str) -> list[str]:
        """Return the run's file paths, relative to the transfer root.

        Lists ``<root>/<run_name>/`` first. If that yields no files and the name
        contains files at any depth, they are all returned: **the folder is one
        umbrella run** (decision 2026-09-04, revised same day — deliveries roll
        up into the folder's single experiment, mirroring detection; the brief
        per-delivery split and its name-to-path fallback are reverted).
        Directory entries are dropped (leading ``d`` in rsync's permission
        column) so only real files reach the transfer loop, and each surviving
        name is returned prefixed with the run directory so it can be handed
        straight back to ``fetch``.

        Raises:
            TransferError: the listing command failed. Fail-closed on purpose —
                an empty list would otherwise read as "this run has no files"
                and let the gate report a successful download of nothing.
        """
        return self._list_directory(run_name)

    def _list_directory(self, remote_directory: str) -> list[str]:
        """One recursive ``rsync --list-only -r`` of ``remote_directory``'s contents.

        Recursive on purpose (review finding 1): detection certifies a
        candidate from files at any depth inside its delivery unit, so a
        non-recursive listing here would fetch the manifest, miss the nested
        data, and let the gate report success having downloaded nothing real.

        Raises:
            TransferError: the listing command failed or a binary is absent.
        """
        # The trailing slash is LOAD-BEARING: `rsync --list-only <dir>` lists the
        # directory ENTRY (one line), while `<dir>/` lists its CONTENTS. It has to
        # be appended AFTER path construction, because pathlib strips it —
        # Path("/a/b") / "run/" is "/a/b/run". Losing it makes every run look
        # empty, which the gate then correctly refuses as "listed zero files".
        source = self._remote_source(remote_directory) + "/"
        if self.is_local_mode:
            command = ["rsync", "--list-only", "-r", source]
        else:
            password = _load_password(self._config["password_file"])
            command = [
                "sshpass",
                "-p",
                password,
                "rsync",
                "--list-only",
                "-r",
                source,
            ]
        try:
            completed = self._runner(
                command, capture_output=True, text=True, check=True
            )
        except FileNotFoundError as missing_binary:
            raise TransferError(
                f"listing {remote_directory!r} failed: required binary not found "
                f"({missing_binary})"
            ) from missing_binary
        except subprocess.CalledProcessError as failed:
            raise TransferError(
                f"listing {remote_directory!r} failed: {failed.stderr}"
            ) from failed
        return _parse_listing_filenames(completed.stdout or "", remote_directory)

    def fetch(self, remote_path: str, local_dest) -> str:
        """Transfer one file, creating its parent directory. Returns rsync's stdout.

        Resume-safe by virtue of ``-avP``: an already-transferred file is skipped and
        a partial one continues, so calling this again after a crash is cheap and
        correct rather than a re-download.

        Raises:
            TransferError: rsync exited non-zero, or the binary is absent. The
                message carries the remote path and rsync's stderr — never the
                password, which exists only inside the argv list.
        """
        local_dest = pathlib.Path(local_dest)
        local_dest.parent.mkdir(parents=True, exist_ok=True)
        command = self._build_command(remote_path, local_dest)
        try:
            completed = self._runner(
                command, capture_output=True, text=True, check=True
            )
        except FileNotFoundError as missing_binary:
            # rsync/sshpass absent: a deployment fault, surfaced as the same
            # fail-closed error so the daemon quarantines rather than continuing.
            raise TransferError(
                f"transfer of {remote_path!r} failed: required binary not found "
                f"({missing_binary})"
            ) from missing_binary
        except subprocess.CalledProcessError as failed:
            raise TransferError(
                f"transfer of {remote_path!r} failed: {failed.stderr}"
            ) from failed
        return completed.stdout or ""


def make_transfer_client(config) -> RsyncTransferClient:
    """Build the production transfer client from config.

    The factory the download gate defers behind a zero-arg callable, matching the
    ``make_experiment_client`` precedent — so a gate that resumes without needing a
    transfer never constructs a client and never touches the password file.
    """
    return RsyncTransferClient(config)
