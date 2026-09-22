"""Behavioral tests for the Slack notifier (plan 15 increment 5).

No token, no network, no ``slack_sdk`` required: the client is injected, so every test drives a
fake that records what would have been posted. That is the whole point of the adapter boundary —
the world stays outside.
"""

import sqlite3

import pytest

from sluice.core.db import initialize_database, insert_run, update_run, write_txn
from sluice.core.events import Notifier
from sluice.gates.slack import SlackNotifier, make_notifier, read_bot_token

CHANNEL = "C0123456789"


class FakeSlackClient:
    """Records posts; can be told to fail, the way a real outage would."""

    def __init__(self, *, fail=False, ts="1722600000.000100"):
        self.posts = []
        self.updates = []
        self.fail = fail
        self.ts = ts

    def chat_postMessage(self, *, channel, text, thread_ts=None):
        if self.fail:
            raise RuntimeError("slack is down")
        self.posts.append({"channel": channel, "text": text, "thread_ts": thread_ts})
        return {"ok": True, "ts": self.ts}

    def chat_update(self, *, channel, ts, text):
        if self.fail:
            raise RuntimeError("slack is down")
        self.updates.append({"channel": channel, "ts": ts, "text": text})
        return {"ok": True, "ts": self.ts}


@pytest.fixture
def state_db(tmp_path):
    """An initialized database holding one detected run."""
    path = tmp_path / "state.db"
    initialize_database(path)
    with write_txn(path) as connection:
        insert_run(
            connection, "SEQ_001", pipeline_version="production-v3.1", size_tb=2.5
        )
        update_run(connection, "SEQ_001", {"experiment_id": 4471})
    return path


def notifier_for(state_db, client, **kwargs):
    return SlackNotifier(client, CHANNEL, state_db, **kwargs)


# ---------------------------------------------------------------------------
# The Protocol contract.
# ---------------------------------------------------------------------------


def test_slack_notifier_satisfies_the_notifier_protocol(state_db):
    """``SlackNotifier`` is a structural ``Notifier``.

    ``safe_notify`` resolves methods by name and raises ``AttributeError`` on a
    typo — deliberately, so a mistyped method is a loud programmer error rather
    than silently disabled notifications forever. This pins the three names.
    """
    assert isinstance(notifier_for(state_db, FakeSlackClient()), Notifier)


# ---------------------------------------------------------------------------
# Threading — the A5 seam.
# ---------------------------------------------------------------------------


def test_open_run_thread_returns_the_timestamp_for_the_daemon_to_persist(state_db):
    """The parent post's ``ts`` is RETURNED, not written by the notifier.

    The A5 seam: the thread id rides the return value back to the daemon, which
    persists it. Keeping the write in the daemon preserves the single-writer rule
    over run state — a notifier writing its own column would be a second
    authority over the runs table.
    """
    client = FakeSlackClient(ts="1722600000.000100")

    returned = notifier_for(state_db, client).open_run_thread("SEQ_001")

    assert returned == "1722600000.000100"
    with sqlite3.connect(state_db) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT slack_thread_ts FROM runs WHERE name = 'SEQ_001'"
        ).fetchone()
    assert row["slack_thread_ts"] is None, (
        "the notifier must not write slack_thread_ts itself — the daemon persists "
        "what open_run_thread returns"
    )


def test_the_parent_message_carries_experiment_and_size_from_the_row(state_db):
    """The parent reads the run's experiment id and size from state, not the caller.

    The daemon calls ``open_run_thread(name)`` with no metadata, so a notifier that
    only used its arguments would post "exp pending · size unknown" for every run.
    """
    client = FakeSlackClient()

    notifier_for(state_db, client).open_run_thread("SEQ_001")

    text = client.posts[0]["text"]
    assert "SEQ_001" in text
    assert "exp 4471" in text
    assert "2.50 TB" in text


def test_reopening_an_existing_thread_posts_nothing(state_db):
    """A run that already has a thread does not get a second one.

    Detection is idempotent and re-runs every cycle; without this a re-detected
    run would sprout a fresh thread each pass.
    """
    with write_txn(state_db) as connection:
        update_run(connection, "SEQ_001", {"slack_thread_ts": "1722600000.000100"})
    client = FakeSlackClient()

    returned = notifier_for(state_db, client).open_run_thread("SEQ_001")

    assert returned is None, "nothing new to persist"
    assert client.posts == []


def test_milestones_reply_inside_the_run_thread(state_db):
    """A milestone is posted with the run's ``thread_ts``, keeping the channel quiet."""
    with write_txn(state_db) as connection:
        update_run(connection, "SEQ_001", {"slack_thread_ts": "1722600000.000100"})
    client = FakeSlackClient()

    notifier_for(state_db, client).post_milestone(
        "SEQ_001", "Download raw lanes completed"
    )

    assert client.posts[0]["thread_ts"] == "1722600000.000100"
    assert "Download raw lanes completed" in client.posts[0]["text"]


def test_a_milestone_without_a_thread_still_gets_posted(state_db):
    """With no thread yet the message goes to the channel un-threaded, not dropped.

    Happens when Slack is enabled mid-run, or the parent post failed. A milestone
    in the wrong place beats a milestone nowhere.
    """
    client = FakeSlackClient()

    notifier_for(state_db, client).post_milestone("SEQ_001", "something happened")

    assert len(client.posts) == 1
    assert client.posts[0]["thread_ts"] is None


# ---------------------------------------------------------------------------
# Terminal outcomes and mentions.
# ---------------------------------------------------------------------------


def test_a_quarantined_run_mentions_the_on_call_list(state_db, tmp_path):
    """Quarantine @-mentions people; the reason is carried verbatim.

    A quarantined run waits for a human indefinitely, so this is the message that
    has to reach someone. The reason is not abbreviated — the quarantine text is
    written to be actionable, and trimming it would force a trip to the CLI.
    """
    mention_file = tmp_path / "oncall.txt"
    mention_file.write_text("U111\nU222\n", encoding="utf-8")
    client = FakeSlackClient()

    notifier_for(state_db, client, mention_file=mention_file).finish_run_thread(
        "SEQ_001", "quarantined", "HASH_MISMATCH: 2 of 8 file(s) corrupt"
    )

    text = client.posts[0]["text"]
    assert "<@U111>" in text and "<@U222>" in text
    assert "HASH_MISMATCH: 2 of 8 file(s) corrupt" in text


def test_a_completed_run_does_not_mention_anyone(state_db, tmp_path):
    """Success is reported without pinging people.

    The distinction that keeps notifications worth reading: mentions mean "you are
    needed", so a run finishing normally must not use one.
    """
    mention_file = tmp_path / "oncall.txt"
    mention_file.write_text("U111\n", encoding="utf-8")
    client = FakeSlackClient()

    notifier_for(state_db, client, mention_file=mention_file).finish_run_thread(
        "SEQ_001", "completed"
    )

    assert "<@U111>" not in client.posts[0]["text"]
    assert "✅" in client.posts[0]["text"]


def test_an_unreadable_mention_file_still_posts_the_message(state_db, tmp_path):
    """A missing mention list degrades to an un-mentioned message, never a lost one."""
    client = FakeSlackClient()

    notifier_for(
        state_db, client, mention_file=tmp_path / "absent.txt"
    ).finish_run_thread("SEQ_001", "quarantined", "boom")

    assert len(client.posts) == 1
    assert "boom" in client.posts[0]["text"]


# ---------------------------------------------------------------------------
# Failure is never fatal.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "args"),
    [
        ("open_run_thread", ("SEQ_001",)),
        ("post_milestone", ("SEQ_001", "text")),
        ("finish_run_thread", ("SEQ_001", "quarantined", "boom")),
    ],
)
def test_a_slack_outage_never_raises(state_db, method, args):
    """Every method swallows a failing client.

    ``safe_notify`` already guards the daemon, but defending here means an outage
    degrades to "no notifications" rather than one swallowed-and-logged error per
    run per cycle.
    """
    notifier = notifier_for(state_db, FakeSlackClient(fail=True))

    assert getattr(notifier, method)(*args) is None


def test_a_missing_database_does_not_break_notification(tmp_path):
    """An unreadable state file degrades to an un-threaded post, not a crash."""
    client = FakeSlackClient()

    SlackNotifier(client, CHANNEL, tmp_path / "absent.db").post_milestone(
        "SEQ_001", "hello"
    )

    assert len(client.posts) == 1


# ---------------------------------------------------------------------------
# Configuration and secrets.
# ---------------------------------------------------------------------------


def test_the_token_is_read_from_a_file_not_from_config(tmp_path):
    """The bot token comes from a path, matching every other sluice credential.

    ``config.example.json`` is committed, so a token has no business being
    readable inline from config — the same reason ``password_file`` and
    ``labkey_api_key_file`` are paths.
    """
    token_file = tmp_path / "slack_token"
    token_file.write_text("xoxb-not-a-real-token\n", encoding="utf-8")

    assert read_bot_token({"slack_bot_token_file": str(token_file)}) == (
        "xoxb-not-a-real-token"
    )


def test_an_inline_config_token_is_ignored(tmp_path, monkeypatch):
    """A token pasted into config.json is NOT honoured.

    Deliberate: honouring it would make committing a real token to the repo an
    easy mistake with no feedback.
    """
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)

    assert read_bot_token({"slack_bot_token": "xoxb-pasted-into-config"}) is None


def test_the_token_falls_back_to_the_environment(monkeypatch):
    """``$SLACK_BOT_TOKEN`` works, matching the production poller's convention."""
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-from-env")

    assert read_bot_token({}) == "xoxb-from-env"


@pytest.mark.parametrize(
    ("config", "why"),
    [
        ({"slack_enabled": False}, "disabled outright"),
        ({"slack_enabled": True}, "no channel"),
        ({"slack_enabled": True, "slack_channel": CHANNEL}, "no token"),
    ],
)
def test_an_unconfigured_notifier_is_none_not_an_error(config, why, monkeypatch):
    """Incomplete Slack config yields ``None`` — a supported silent mode.

    ``safe_notify(None, ...)`` is explicitly a normal deployment mode, so the
    factory must not raise. Each reason is logged once at startup instead, so
    "why am I getting no notifications?" is answerable from the daemon's log.
    """
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)

    assert make_notifier(config, db_path=":memory:") is None, why


def test_update_run_header_rewrites_the_parent_with_the_experiment_id(state_db):
    """Once the experiment id exists, the parent stops saying "exp pending".

    Production request 2026-09-22: the thread parent posted at detection reads
    "exp pending" forever, even after gate 0 stamps the id — the one line
    everyone scans for the experiment number.
    """
    client = FakeSlackClient()
    notifier = notifier_for(state_db, client)
    with write_txn(state_db) as connection:
        update_run(connection, "SEQ_001", {"slack_thread_ts": "1722600000.000100"})

    notifier.update_run_header("SEQ_001")

    assert len(client.updates) == 1
    updated = client.updates[0]
    assert updated["ts"] == "1722600000.000100"
    assert "SEQ_001" in updated["text"]
    assert "exp 4471" in updated["text"]
    assert "exp pending" not in updated["text"]


def test_update_run_header_without_a_thread_is_a_no_op(state_db):
    """No parent message, nothing to rewrite — never posts, never raises."""
    client = FakeSlackClient()
    notifier_for(state_db, client).update_run_header("SEQ_001")
    assert client.updates == []
    assert client.posts == []
