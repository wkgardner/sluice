"""The Slack world adapter — one notification thread per run.

Implements the ``Notifier`` Protocol that ``sluice.core.events`` has reserved since Plan A
(``events.py``: *"the concrete Slack implementation arrives with Plan B"*). Lives under
``sluice.gates`` because it imports ``slack_sdk``: ADR-0003 d.3 keeps every service SDK out of
core, reached only through an injected Protocol.

## Why threads

A run generates several messages over hours or days — detected, each gate finishing, a terminal
outcome. Flat posting turns a busy week into noise nobody reads. So each run gets **one parent
message** and everything else is a reply in its thread; the channel shows one line per run, and the
detail is one click away.

Threading needs the parent's timestamp (``thread_ts``), which must survive daemon restarts. It
lives in ``runs.slack_thread_ts``, a column present since migration 001 for exactly this.

**This adapter never writes it.** ``open_run_thread`` *returns* the timestamp and the daemon
persists it — the "A5 seam" ``safe_notify``'s docstring describes, where the thread id rides the
return value back to the daemon. Keeping the write in the daemon preserves the single-writer rule
that makes run state easy to reason about; a notifier that wrote run rows would be a second
authority over them. Reads are fine and are how replies find their thread.

## Failure is never fatal

Every method returns rather than raises. ``safe_notify`` already swallows exceptions so a Slack
outage cannot fail a gate, but defending here too means a missing token or an unreachable API
degrades to "no notifications" rather than an error line per run per cycle. An unconfigured
notifier is a supported deployment mode, not a broken one.

## Secrets

The bot token is read from ``slack_bot_token_file`` (sluice's convention for every credential:
``password_file``, ``labkey_api_key_file``) or from ``$SLACK_BOT_TOKEN``. It is deliberately NOT
readable from ``config.toml`` inline — that file is templated in the repo and a token has no
business in it. The token is never logged, never returned, and never formatted into a message
(ADR-0002, errata C5).
"""

import os
import pathlib
import sqlite3
from datetime import datetime
from typing import Any

import structlog

from sluice.core.events import format_duration

__all__ = [
    "TOKEN_ENV_VAR",
    "SlackNotifier",
    "make_notifier",
    "read_bot_token",
]

log = structlog.get_logger("sluice.gates.slack")

# The fallback the token is read from when no token file is configured. Named
# because doctor must judge "is a token available?" by the same rule, and a
# transcribed literal there would let an env-only install look unconfigured.
TOKEN_ENV_VAR = "SLACK_BOT_TOKEN"


def _total_duration_text(row) -> str | None:
    """Wall-clock from detection to completion, human-scaled, or None if unknowable."""
    detected, completed = row["detected_at"], row["completed_at"]
    if not detected or not completed:
        return None
    try:
        elapsed = datetime.fromisoformat(completed) - datetime.fromisoformat(detected)
    except (ValueError, TypeError):
        return None
    return format_duration(int(elapsed.total_seconds() * 1000))


def read_bot_token(config) -> str | None:
    """Return the Slack bot token, or ``None`` if none is configured.

    Sources, in order: ``config["slack_bot_token_file"]`` (a path, sluice's
    convention for every credential), then ``$SLACK_BOT_TOKEN``.

    Deliberately NOT read inline from config: ``config.example.json`` is committed
    and a token has no business in a templated file. Returns ``None`` rather than
    raising, because "no token" means "notifications off", which is a supported
    deployment mode.
    """
    token_file = config.get("slack_bot_token_file")
    if token_file:
        try:
            token = pathlib.Path(token_file).read_text(encoding="utf-8").strip()
        except OSError as unreadable:
            # Name the path, never the contents.
            log.error(
                "slack.token_file_unreadable",
                **{
                    "slack.token_file": str(token_file),
                    "error.type": type(unreadable).__name__,
                },
            )
            return None
        if token:
            return token
        log.error("slack.token_file_empty", **{"slack.token_file": str(token_file)})
        return None
    return os.environ.get(TOKEN_ENV_VAR) or None


def _format_metadata(experiment_id, size_tb) -> str:
    """The ``exp N · X.XX TB`` line shared by the parent message's every state.

    Kept identical across processing and terminal renders so finishing a run never
    drops the metadata — production hit exactly that bug, where completing a run
    overwrote the parent with a bare one-liner (PP ``_terminal_title``).
    """
    experiment = f"exp {experiment_id}" if experiment_id else "exp pending"
    size = f"{size_tb:.2f} TB" if isinstance(size_tb, (int, float)) else "size unknown"
    return f"{experiment} · {size}"


class SlackNotifier:
    """Posts run milestones to one Slack channel, one thread per run.

    Constructed by ``make_notifier``; the daemon holds one and reaches it only
    through ``safe_notify``.
    """

    def __init__(self, client, channel: str, db_path, *, mention_file=None):
        """Store the collaborators. Does no I/O.

        Args:
            client: a ``slack_sdk.WebClient``-shaped object exposing
                ``chat_postMessage`` and ``chat_update``. Injected so tests drive a
                fake and no test ever needs a token.
            channel: the channel id to post into.
            db_path: the state database, read (never written) for a run's
                ``slack_thread_ts`` and its experiment id / size.
            mention_file: optional path listing user ids to @-mention on the
                messages that ask for a human.
        """
        self._client = client
        self._channel = channel
        self._db_path = db_path
        self._mention_file = mention_file

    # -- reads ------------------------------------------------------------

    def _run_row(self, run_name: str):
        """Read one run row, or ``None``. Read-only, on its own short connection."""
        try:
            connection = sqlite3.connect(self._db_path)
        except sqlite3.Error:
            return None
        try:
            connection.row_factory = sqlite3.Row
            return connection.execute(
                "SELECT * FROM runs WHERE name = ?", (run_name,)
            ).fetchone()
        except sqlite3.Error:
            return None
        finally:
            connection.close()

    def _thread_ts(self, run_name: str) -> str | None:
        """The run's parent-message timestamp, if a thread has been opened."""
        row = self._run_row(run_name)
        return row["slack_thread_ts"] if row is not None else None

    def _mention_text(self) -> str:
        """Render the @-mentions, re-read on demand so edits take effect live.

        Returns an empty string on any problem — an unreadable mention list must
        degrade to an un-mentioned message, never to a lost one.
        """
        if not self._mention_file:
            return ""
        try:
            raw = pathlib.Path(self._mention_file).read_text(encoding="utf-8")
        except OSError:
            log.error(
                "slack.mention_file_unreadable",
                **{"slack.mention_file": str(self._mention_file)},
            )
            return ""
        user_ids = [line.strip() for line in raw.splitlines() if line.strip()]
        return " ".join(f"<@{user_id}>" for user_id in user_ids)

    # -- the Notifier Protocol --------------------------------------------

    def open_run_thread(
        self,
        run_name: str,
        experiment_id: int | None = None,
        size_tb: float | None = None,
    ) -> str | None:
        """Post the run's parent message and RETURN its timestamp for the daemon to persist.

        Idempotent: if the run already has a ``slack_thread_ts``, that thread is
        reused and nothing is posted, so a re-detected run does not sprout a second
        thread.

        Returns:
            The parent message timestamp, or ``None`` if a thread already existed
            or the post failed. ``None`` on the already-exists path is deliberate:
            it means "nothing new to persist".
        """
        row = self._run_row(run_name)
        if row is not None and row["slack_thread_ts"]:
            return None
        if row is not None:
            experiment_id = experiment_id or row["experiment_id"]
            size_tb = size_tb if size_tb is not None else row["size_tb"]
        text = (
            f"*{run_name}*\n{_format_metadata(experiment_id, size_tb)}\n"
            "Status: processing"
        )
        response = self._post(text)
        return response.get("ts") if response else None

    def update_run_header(self, run_name: str) -> None:
        """Rewrite the parent message from the run's current row (exp id, size).

        No thread yet (or no row) means nothing to rewrite — a silent no-op,
        matching ``open_run_thread``'s idempotence.
        """
        row = self._run_row(run_name)
        if row is None or not row["slack_thread_ts"]:
            return
        self._rewrite_parent(run_name, row, "processing")

    def _rewrite_parent(self, run_name: str, row, status: str) -> None:
        """``chat_update`` the parent to ``status``, preserving the metadata line.

        The template mirrors ``open_run_thread`` exactly so a rewrite never
        drops metadata (the ``_format_metadata`` lesson, PP ``_terminal_title``).
        Same failure posture as ``_post``: a Slack failure never fails work.
        """
        text = (
            f"*{run_name}*\n"
            f"{_format_metadata(row['experiment_id'], row['size_tb'])}\n"
            f"Status: {status}"
        )
        try:
            self._client.chat_update(
                channel=self._channel, ts=row["slack_thread_ts"], text=text
            )
        except Exception as update_error:
            log.error(
                "slack.update_failed",
                **{
                    "slack.channel": self._channel,
                    "error.type": type(update_error).__name__,
                },
            )

    def post_milestone(self, run_name: str, text: str, mention: bool = False) -> None:
        """Post a milestone as a reply in the run's thread.

        With no thread yet (notifications enabled mid-run, or the parent post
        failed) the message still goes to the channel un-threaded rather than being
        dropped — a milestone in the wrong place beats a milestone nowhere.
        """
        body = f"*{run_name}* — {text}"
        if mention:
            mentions = self._mention_text()
            if mentions:
                body = f"{body}\n{mentions}"
        self._post(body, thread_ts=self._thread_ts(run_name))

    def finish_run_thread(
        self, run_name: str, outcome: str, reason: str | None = None
    ) -> None:
        """Close the run's thread with its terminal outcome, @-mentioning on trouble.

        A quarantined run needs a human, so that outcome mentions the on-call list;
        a completed one does not. The reply carries the reason verbatim, because the
        quarantine text is written to be actionable and abbreviating it here would
        force a trip to the CLI.
        """
        # Plain words, no emoji (request 2026-11-13).
        body = f"*{run_name}* — {outcome}"
        row = self._run_row(run_name)
        if row is not None and outcome == "completed":
            # The numbers anyone opening a finished thread wants (sweep
            # 2026-09-25): experiment, data size, total flight time.
            metrics = _format_metadata(row["experiment_id"], row["size_tb"])
            total_duration = _total_duration_text(row)
            if total_duration:
                metrics = f"{metrics} \u00b7 {total_duration} total"
            body = f"{body}\n{metrics}"
        if reason:
            body = f"{body}\n{reason}"
        needs_a_human = outcome == "quarantined"
        if needs_a_human:
            mentions = self._mention_text()
            if mentions:
                body = f"{body}\n{mentions}"
        self._post(body, thread_ts=self._thread_ts(run_name))
        # The parent is the channel's one line per run — a terminal outcome
        # must not leave it saying "processing".
        if row is not None and row["slack_thread_ts"]:
            self._rewrite_parent(run_name, row, outcome)

    # -- the one place that talks to Slack --------------------------------

    def _post(self, text: str, thread_ts: str | None = None) -> Any:
        """Post one message, swallowing every failure into one log line.

        The single egress point, so there is exactly one place a Slack outage can
        surface and exactly one place to look when messages stop arriving. Returns
        the API response, or ``None`` if the post failed.
        """
        try:
            return self._client.chat_postMessage(
                channel=self._channel, text=text, thread_ts=thread_ts
            )
        except Exception as post_error:
            # Never re-raise: a Slack outage must not fail a gate or the daemon.
            # The exception type only — a Slack error can echo request context.
            log.error(
                "slack.post_failed",
                **{
                    "slack.channel": self._channel,
                    "error.type": type(post_error).__name__,
                },
            )
            return None


def make_notifier(config, db_path=None):
    """Build the notifier from config, or return ``None`` if Slack is not configured.

    Returns ``None`` — a supported, silent deployment mode that ``safe_notify``
    already understands — when ``slack_enabled`` is false, no token is available,
    no channel is set, or ``slack_sdk`` is not installed. Each reason is logged
    once at startup so "why am I getting no notifications?" is answerable from the
    daemon's own log rather than by reading this code.

    Args:
        config: the loaded config.
        db_path: the state database; defaults to ``config["state_file"]``.

    Returns:
        A ``SlackNotifier``, or ``None``.
    """
    if not config.get("slack_enabled"):
        return None

    channel = config.get("slack_channel")
    if not channel:
        log.error("slack.disabled", **{"slack.reason": "slack_channel is not set"})
        return None

    token = read_bot_token(config)
    if not token:
        log.error(
            "slack.disabled",
            **{
                "slack.reason": (
                    "no bot token — set slack_bot_token_file or $SLACK_BOT_TOKEN"
                )
            },
        )
        return None

    try:
        # Imported lazily and inside the factory so the whole test suite, and any
        # deployment with Slack off, runs with slack_sdk absent.
        from slack_sdk import WebClient
    except ImportError:
        log.error("slack.disabled", **{"slack.reason": "slack_sdk is not installed"})
        return None

    log.info("slack.enabled", **{"slack.channel": channel})
    return SlackNotifier(
        WebClient(token=token),
        channel,
        db_path or config["state_file"],
        mention_file=config.get("slack_user_id_file"),
    )
