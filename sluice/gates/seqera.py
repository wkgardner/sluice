"""Find a Nextflow run's Seqera Platform (Tower) watch link, so Slack can say where to monitor it.

Both Nextflow gates (``nvd`` and ``profiling``) run Tower-enabled pipelines, and Nextflow records
the run's link a second or two after launch. Where it records it depends on the Nextflow version:

* NVD's (older) Nextflow prints ``Monitor the execution with Seqera Platform using this URL:
  https://cloud.seqera.io/orgs/<org>/workspaces/<ws>/watch/<id>`` to the console and its log.
* REMV's Nextflow 26.10 writes only a DEBUG line to ``.nextflow.log``: ``Platform workflow id:
  <id>; workflow url: https://cloud.seqera.io/.../watch/<id>``.

Both end in ``/watch/<id>``, so one pattern finds either, in a console line or in the
``.nextflow.log`` Nextflow keeps in its launch directory.
"""

import pathlib
import re

__all__ = ["LINES_TO_WAIT_FOR_WATCH_URL", "find_watch_url"]

# Any https URL ending in /watch/<run id> — the shape of every Seqera Platform run link.
WATCH_URL_PATTERN = re.compile(r"https://\S+/watch/[A-Za-z0-9]+")

# How many console lines a gate waits for the link before announcing its start without one
# (Tower switched off, or a Nextflow that logs it somewhere else). The link appears within the
# first few lines when it appears at all, so this only delays the no-link announcement.
LINES_TO_WAIT_FOR_WATCH_URL = 200

# File mtimes come from the kernel's coarse clock, so a log written just after launch can read a
# few milliseconds older than ``time.time()`` taken before it. A stale log from an earlier attempt
# is minutes or hours old, so one second of slack cannot let one through.
MTIME_SLACK_SECONDS = 1.0


def find_watch_url(console_line, nextflow_log_path, launched_at) -> str | None:
    """Return the run's watch link if it is in ``console_line`` or this launch's Nextflow log.

    Args:
        console_line: one line of the pipeline's console output.
        nextflow_log_path: ``<launch dir>/.nextflow.log``.
        launched_at: ``time.time()`` just before the launch. A log last modified
            before then belongs to an earlier attempt, and its link points at
            that attempt's run, so it is ignored.

    Returns:
        The first watch link found, or ``None``.
    """
    match = WATCH_URL_PATTERN.search(console_line)
    if match:
        return match.group(0)
    nextflow_log_path = pathlib.Path(nextflow_log_path)
    try:
        if nextflow_log_path.stat().st_mtime < launched_at - MTIME_SLACK_SECONDS:
            return None
        match = WATCH_URL_PATTERN.search(
            nextflow_log_path.read_text(encoding="utf-8", errors="replace")
        )
    except OSError:
        return None  # Not written yet: Nextflow has only just started.
    return match.group(0) if match else None
