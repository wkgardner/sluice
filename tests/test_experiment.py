"""Behavioral tests for the ``experiment`` gate and its LabKey adapter (plan 13).

Two halves, both network-free and ``labkey``-package-free:

* Unit 2 — ``sluice.gates.labkey``: the fail-closed world adapter, exercised with
  a **fake api** injected via ``api_factory``. The pins that matter: keys read by
  subscript (loud ``KeyError``), construction touches nothing external, ``find``
  resolves by the ``Description`` trailing-token ``endswith`` (never by user),
  disambiguates substrings, fails **closed** on ambiguity/error, and no api key
  ever reaches a ``LabKeyError`` message.
* Unit 3 — ``sluice.gates.experiment``: the gate orchestration, exercised with a
  **fake whole-client**. The make-or-break C1 branches: resume reuses the
  persisted id with ZERO LabKey contact; first run does lookup-before-create; the
  crash window adopts an existing experiment without a duplicate; every LabKey
  failure quarantines fail-closed and never falls through to create.

No fixture contains a real LabKey host, project path, or api key — the config
placeholders below are obvious fakes.
"""

import types

import pytest

from sluice.core.context import RunContext
from sluice.core.gate import GateOutcome, validate_gates
from sluice.gates.experiment import EXPERIMENT, run_experiment_gate
from sluice.gates.labkey import (
    ExperimentClient,
    LabKeyError,
    make_experiment_client,
)

# --------------------------------------------------------------------------
# Fakes and helpers.
# --------------------------------------------------------------------------


def _config(tmp_path, **overrides):
    """A complete, obviously-fake LabKey config; the key file need not exist.

    No real host, project path, or key appears here — every value is a
    placeholder (``.test`` server, ``/example/...`` project). The api key file
    path points under ``tmp_path`` and is intentionally NOT created, so tests can
    prove construction never reads it.
    """
    config = {
        "labkey_server": "labkey.example.test",
        "labkey_experiments_project": "/example/experiments",
        "labkey_schema": "ExampleExperiments",
        "labkey_query": "Experiments",
        "labkey_api_key_file": str(tmp_path / "labkey_api_key.txt"),
    }
    config.update(overrides)
    return config


class FakeApi:
    """Records ``select_rows``/``insert_rows`` calls; returns/raises as configured.

    Mirrors the two-method surface ``ExperimentClient`` calls on the object its
    ``api_factory`` returns. ``calls`` preserves order as ``(kind, payload)`` so a
    test can assert insert precedes the resolve select.
    """

    def __init__(self, *, select_result=None, select_error=None, insert_error=None):
        self.calls = []
        self._select_result = {"rows": []} if select_result is None else select_result
        self._select_error = select_error
        self._insert_error = insert_error

    def select_rows(self, *, schema_name, query_name, filter_array):
        self.calls.append(("select", filter_array))
        if self._select_error is not None:
            raise self._select_error
        return self._select_result

    def insert_rows(self, *, schema_name, query_name, rows):
        self.calls.append(("insert", rows))
        if self._insert_error is not None:
            raise self._insert_error
        return {"rowsAffected": len(rows)}


def _client(tmp_path, api):
    """An ``ExperimentClient`` whose api factory returns the given fake api."""
    return make_experiment_client(_config(tmp_path), api_factory=lambda: api)


def _row(description, experiment_number):
    """A LabKey ``select_rows`` row dict with a ``Description`` and number."""
    return {"Description": description, "ExperimentNumber": experiment_number}


class FakeClient:
    """A fake whole ``ExperimentClient`` for the gate orchestration tests.

    Records call order in ``order`` and per-method counts, and returns/raises as
    configured so each C1 branch is driven deterministically.
    """

    def __init__(
        self,
        *,
        find_result=None,
        find_error=None,
        create_result=None,
        create_error=None,
    ):
        self.order = []
        self.find_calls = 0
        self.create_calls = 0
        self._find_result = find_result
        self._find_error = find_error
        self._create_result = create_result
        self._create_error = create_error

    def find_experiment_by_run_name(self, run_name):
        self.find_calls += 1
        self.order.append("find")
        if self._find_error is not None:
            raise self._find_error
        return self._find_result

    def create_experiment(self, run_name):
        self.create_calls += 1
        self.order.append("create")
        if self._create_error is not None:
            raise self._create_error
        return self._create_result


def _context(run_row, config=None):
    """A minimal context carrying only what the gate reads: ``run_row``.

    ``types.SimpleNamespace`` is enough for ``run_experiment_gate`` (it reads
    ``context.run_row`` only); ``config`` is attached for the ``_run_experiment``
    wrapper path.
    """
    return types.SimpleNamespace(run_row=run_row, config=config or {})


# ==========================================================================
# Unit 2 — the LabKey adapter (fake api, no network, no ``labkey`` package).
# ==========================================================================


def test_make_experiment_client_missing_required_key_fails_loud(tmp_path):
    """A missing config key raises ``KeyError`` (subscript, no baked default)."""
    config = _config(tmp_path)
    del config["labkey_experiments_project"]
    with pytest.raises(KeyError, match="labkey_experiments_project"):
        make_experiment_client(config)
    # The other four keys are read the same way (subscript); one is pinned here.


def test_client_construction_is_network_and_secret_free(tmp_path):
    """Construction reads no secret and never invokes the api factory.

    The default-factory path succeeds even though the api key file does not
    exist (the read is deferred into the factory closure); and an injected
    factory is never called during ``make_experiment_client``.
    """
    # Default factory: nonexistent key file, construction still succeeds.
    client = make_experiment_client(_config(tmp_path))
    assert isinstance(client, ExperimentClient)

    # Injected factory: never invoked at construction time.
    invocations = []

    def spy_factory():
        invocations.append(1)
        return FakeApi()

    make_experiment_client(_config(tmp_path), api_factory=spy_factory)
    assert invocations == []


def test_find_returns_none_when_no_match(tmp_path):
    """Zero rows → ``None`` (nothing to reuse)."""
    client = _client(tmp_path, FakeApi(select_result={"rows": []}))
    assert client.find_experiment_by_run_name("RUN_A") is None


def test_find_returns_experiment_number_for_single_exact_match(tmp_path):
    """One row ending with the trailing token → its ``int`` ``ExperimentNumber``."""
    api = FakeApi(select_result={"rows": [_row("run on Folder name: RUN_A", 5150)]})
    client = _client(tmp_path, api)
    assert client.find_experiment_by_run_name("RUN_A") == 5150


def test_find_disambiguates_substring_run_names(tmp_path):
    """A row ending ``Folder name: RUN_AB`` is excluded for run ``RUN_A`` (C1).

    The server-side CONTAINS would return the substring collision; the exact
    ``endswith`` guard drops it, so ``RUN_A`` never adopts ``RUN_AB``'s experiment.
    """
    api = FakeApi(select_result={"rows": [_row("run on Folder name: RUN_AB", 99)]})
    client = _client(tmp_path, api)
    assert client.find_experiment_by_run_name("RUN_A") is None


def test_find_raises_on_multiple_exact_matches(tmp_path):
    """Two exact-suffix matches → ``LabKeyError`` (ambiguous, never guessed)."""
    api = FakeApi(
        select_result={
            "rows": [
                _row("a Folder name: RUN_A", 1),
                _row("b Folder name: RUN_A", 2),
            ]
        }
    )
    client = _client(tmp_path, api)
    with pytest.raises(LabKeyError):
        client.find_experiment_by_run_name("RUN_A")


def test_find_never_queries_by_user_id(tmp_path):
    """The recorded ``filter_array`` filters on ``Description``, never a user id.

    The C1 fix: the "most-recent-by-user" query is not ported. The only filter
    is a ``Description`` CONTAINS carrying the folder token — no ``modifiedby``.
    """
    api = FakeApi(select_result={"rows": []})
    client = _client(tmp_path, api)
    client.find_experiment_by_run_name("RUN_A")

    kind, filter_array = api.calls[0]
    assert kind == "select"
    assert [f.column for f in filter_array] == ["Description"]
    assert filter_array[0].value == "Folder name: RUN_A"


def test_find_wraps_api_error_as_labkey_error(tmp_path):
    """An api exception → ``LabKeyError`` whose message omits the api key."""
    api = FakeApi(select_error=RuntimeError("network down key=SUPERSECRETKEY"))
    client = _client(tmp_path, api)
    with pytest.raises(LabKeyError) as excinfo:
        client.find_experiment_by_run_name("RUN_A")
    assert "SUPERSECRETKEY" not in str(excinfo.value)


@pytest.mark.parametrize(
    "bad_number",
    [None, "not-a-number", "12x"],
    ids=["none", "non-numeric", "partly-numeric"],
)
def test_find_raises_labkey_error_on_unusable_experiment_number(tmp_path, bad_number):
    """A matched row with a missing/None/non-numeric ``ExperimentNumber`` → ``LabKeyError``.

    The id extraction must fail through the SAME door as every other adapter
    failure (C5: "id unresolvable → LabKeyError"), never a bare
    ``KeyError``/``TypeError``/``ValueError``. The message stays clean of any row
    contents — only the run name and the exception type.
    """
    row = {"Description": "x Folder name: RUN_A"}
    if bad_number is not None:
        # Include a bad ExperimentNumber; the ``None`` id-case omits the key
        # entirely to exercise the missing-key path.
        row["ExperimentNumber"] = bad_number
    api = FakeApi(select_result={"rows": [row]})
    client = _client(tmp_path, api)

    with pytest.raises(LabKeyError) as excinfo:
        client.find_experiment_by_run_name("RUN_A")

    message = str(excinfo.value)
    assert "RUN_A" in message
    # No row contents leak — neither the Description nor the bad value.
    assert "Folder name" not in message
    if isinstance(bad_number, str):
        assert bad_number not in message


def test_create_writes_trailing_folder_name_then_resolves(tmp_path):
    """``create`` inserts a trailing-token ``Description``, THEN resolves the id.

    The insert's ``Description`` ends with ``Folder name: RUN_A`` (F3), and the
    resolve select runs after the insert (insert precedes select in ``calls``).
    """
    api = FakeApi(select_result={"rows": [_row("x Folder name: RUN_A", 5150)]})
    client = _client(tmp_path, api)

    assert client.create_experiment("RUN_A") == 5150

    kinds = [kind for kind, _payload in api.calls]
    assert kinds == ["insert", "select"]
    insert_rows = api.calls[0][1]
    assert insert_rows[0]["Description"].endswith("Folder name: RUN_A")


def test_create_raises_when_id_unresolvable_after_insert(tmp_path):
    """Insert succeeds but the resolve finds nothing → ``LabKeyError`` (fail loud)."""
    api = FakeApi(select_result={"rows": []})
    client = _client(tmp_path, api)
    with pytest.raises(LabKeyError):
        client.create_experiment("RUN_A")


def test_create_wraps_insert_error_as_labkey_error(tmp_path):
    """An insert exception → ``LabKeyError`` (C5 fail-loud)."""
    api = FakeApi(insert_error=RuntimeError("insert rejected"))
    client = _client(tmp_path, api)
    with pytest.raises(LabKeyError):
        client.create_experiment("RUN_A")


# ==========================================================================
# Unit 3 — the gate orchestration (fake whole-client).
# ==========================================================================


def test_resume_reuses_persisted_experiment_id_without_touching_labkey():
    """A set ``experiment_id`` → SUCCESS reusing it; ``make_client`` never called.

    The resume-first milestone: the gate succeeds with ZERO LabKey contact, so a
    restart survives LabKey being down or its config being absent.
    """
    context = _context({"experiment_id": 4242, "name": "RUN_A"})
    invocations = []

    def make_client():
        invocations.append(1)
        raise AssertionError("resume path must not build a client")

    result = run_experiment_gate(context, make_client)

    assert result.outcome is GateOutcome.SUCCESS
    assert result.metadata == {"experiment_id": 4242}
    assert invocations == []


def test_first_run_finds_none_then_creates():
    """NULL id → ``find`` (None) THEN ``create``; SUCCESS with the new id."""
    context = _context({"experiment_id": None, "name": "RUN_A"})
    client = FakeClient(find_result=None, create_result=5150)

    result = run_experiment_gate(context, lambda: client)

    assert result.outcome is GateOutcome.SUCCESS
    assert result.metadata == {"experiment_id": 5150}
    assert client.order == ["find", "create"]


def test_crash_window_adopts_existing_experiment_without_creating():
    """NULL id but ``find`` returns an id → adopt it, ``create`` never called (⚑1/C1)."""
    context = _context({"experiment_id": None, "name": "RUN_A"})
    client = FakeClient(find_result=5150)

    result = run_experiment_gate(context, lambda: client)

    assert result.outcome is GateOutcome.SUCCESS
    assert result.metadata == {"experiment_id": 5150}
    assert client.create_calls == 0


def test_lookup_error_quarantines_and_never_creates():
    """A ``find`` ``LabKeyError`` → FAILURE naming the run; ``create`` never called.

    Fail closed: a flaky lookup must not fall through to a duplicate create.
    """
    context = _context({"experiment_id": None, "name": "RUN_A"})
    client = FakeClient(find_error=LabKeyError("labkey unreachable"))

    result = run_experiment_gate(context, lambda: client)

    assert result.outcome is GateOutcome.FAILURE
    assert "RUN_A" in result.reason
    assert client.create_calls == 0


def test_create_error_quarantines():
    """A ``create`` ``LabKeyError`` → FAILURE naming the run (C5).

    Also covers "id unresolvable" — the adapter surfaces that as ``LabKeyError``.
    """
    context = _context({"experiment_id": None, "name": "RUN_A"})
    client = FakeClient(find_result=None, create_error=LabKeyError("insert rejected"))

    result = run_experiment_gate(context, lambda: client)

    assert result.outcome is GateOutcome.FAILURE
    assert "RUN_A" in result.reason


# --------------------------------------------------------------------------
# Unit 3 — the Gate contract and the thin production wrapper.
# --------------------------------------------------------------------------


def test_experiment_gate_contract():
    """``EXPERIMENT`` carries the exact contract the already-built daemon acts on."""
    assert EXPERIMENT.id == "experiment"
    assert EXPERIMENT.satisfied_column == "labkey_registered"
    assert EXPERIMENT.order == 0
    assert EXPERIMENT.needs == ()
    assert EXPERIMENT.produces == ("experiment_id",)  # non-empty — arms control B2
    assert EXPERIMENT.terminate is None
    assert dict(EXPERIMENT.persists) == {"experiment_id": "experiment_id"}
    # The persist target differs from the satisfaction column, so the ADR-0006
    # self-clobber guard in validate_gates passes.
    assert validate_gates((EXPERIMENT,)) == (EXPERIMENT,)


def test_experiment_invalidate_is_noop():
    """``invalidate`` returns ``None``, raises nothing, touches no client (⚑C)."""
    context = _context({})
    assert EXPERIMENT.invalidate(context) is None


def test_gate_run_resume_path_is_end_to_end_labkey_free(tmp_path):
    """``EXPERIMENT.run`` reuses a set id with NO labkey config keys present.

    Proves the ``_run_experiment`` wrapper delegates and that resume needs no
    LabKey config at all (the partial is never invoked).
    """
    context = RunContext(
        workdir=tmp_path,
        db_path=tmp_path / "state.db",
        config={},  # deliberately no labkey_* keys
        run_row={"experiment_id": 99, "name": "RUN_A"},
    )
    result = EXPERIMENT.run(context)
    assert result.outcome is GateOutcome.SUCCESS
    assert result.metadata == {"experiment_id": 99}


def test_gate_run_create_path_delegates_to_client(tmp_path, monkeypatch):
    """``EXPERIMENT.run`` on a NULL id wires config into the adapter and returns its id.

    Monkeypatching ``make_experiment_client`` is acceptable for the thin wrapper
    (the risk logic is covered above); it proves the ``functools.partial`` feeds
    ``context.config`` into ``run_experiment_gate``.
    """
    fake = FakeClient(find_result=None, create_result=7777)
    monkeypatch.setattr(
        "sluice.gates.experiment.make_experiment_client",
        lambda config, **kwargs: fake,
    )
    context = RunContext(
        workdir=tmp_path,
        db_path=tmp_path / "state.db",
        config={"labkey_server": "labkey.example.test"},
        run_row={"experiment_id": None, "name": "RUN_A"},
    )
    result = EXPERIMENT.run(context)
    assert result.outcome is GateOutcome.SUCCESS
    assert result.metadata == {"experiment_id": 7777}
