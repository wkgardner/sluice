"""Tests for finding a Nextflow run's Seqera watch link (``sluice/gates/seqera.py``).

The two log shapes are copied from real runs: NVD 32824's console line and REMV
32824's ``.nextflow.log`` DEBUG line.
"""

import os
import time

from sluice.gates.seqera import find_watch_url

NVD_CONSOLE_LINE = (
    "Monitor the execution with Seqera Platform using this URL: "
    "https://cloud.seqera.io/orgs/OConnor_lab/workspaces/nvd/watch/5YRGhiTUzwZcD6\n"
)
REMV_LOG_LINE = (
    "Nov-11 17:01:13.804 [main] DEBUG i.seqera.tower.plugin.TowerObserver - Platform "
    "workflow id: 4dHTL5OQTO4mWW; workflow url: "
    "https://cloud.seqera.io/orgs/OConnor_lab/workspaces/nvd/watch/4dHTL5OQTO4mWW\n"
)


def test_the_link_is_found_in_a_console_line(tmp_path):
    """NVD's Nextflow prints the link to the console."""
    assert find_watch_url(NVD_CONSOLE_LINE, tmp_path / ".nextflow.log", 0) == (
        "https://cloud.seqera.io/orgs/OConnor_lab/workspaces/nvd/watch/5YRGhiTUzwZcD6"
    )


def test_the_link_is_found_in_this_launchs_nextflow_log(tmp_path):
    """REMV's Nextflow writes it only to .nextflow.log, as a DEBUG line."""
    nextflow_log = tmp_path / ".nextflow.log"
    launched_at = time.time() - 5
    nextflow_log.write_text("startup\n" + REMV_LOG_LINE, encoding="utf-8")

    assert find_watch_url("[PROCESS ab/cdef12] X\n", nextflow_log, launched_at) == (
        "https://cloud.seqera.io/orgs/OConnor_lab/workspaces/nvd/watch/4dHTL5OQTO4mWW"
    )


def test_a_log_from_an_earlier_attempt_is_ignored(tmp_path):
    """A .nextflow.log older than this launch holds the previous run's link."""
    nextflow_log = tmp_path / ".nextflow.log"
    nextflow_log.write_text(REMV_LOG_LINE, encoding="utf-8")
    an_hour_ago = time.time() - 3600
    os.utime(nextflow_log, (an_hour_ago, an_hour_ago))

    assert find_watch_url("plain line\n", nextflow_log, time.time()) is None


def test_no_link_and_no_log_yet_is_none(tmp_path):
    """Nothing to find yet is not an error."""
    assert find_watch_url("plain line\n", tmp_path / ".nextflow.log", 0) is None
