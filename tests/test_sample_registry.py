"""Tests for the sample-registry LabKey adapter (``sluice/gates/sample_registry.py``).

Every test injects a fake api through ``api_factory``, so the suite never imports
``labkey`` and never touches a real server or the API key file. The adapter's job
is to fail CLOSED (errata C3): any read it cannot trust raises
``SampleRegistryError`` instead of returning an empty answer that would make every
sample look unregistered.
"""

import pytest

from sluice.gates.sample_registry import (
    SampleRegistryClient,
    SampleRegistryError,
    make_sample_registry_client,
)

AUTHORITY_LIST = "authority-list-under-test"
REGISTRY_LIST = "registry-list-under-test"


class FakeApi:
    """A stand-in for the live api: canned select results, recorded calls."""

    def __init__(self, select_results=None, select_error=None, insert_error=None):
        # query_name -> the dict select_rows returns for it
        self.select_results = select_results or {}
        self.select_error = select_error
        self.insert_error = insert_error
        self.select_calls = []
        self.insert_calls = []

    def select_rows(self, *, schema_name, query_name, columns, filter_array=None):
        """Record the call; raise if told to; else return the canned result."""
        self.select_calls.append(
            {
                "schema_name": schema_name,
                "query_name": query_name,
                "columns": columns,
                "filter_array": filter_array,
            }
        )
        if self.select_error is not None:
            raise self.select_error
        return self.select_results[query_name]

    def insert_rows(self, *, schema_name, query_name, rows):
        """Record the insert; raise if told to."""
        self.insert_calls.append(
            {"schema_name": schema_name, "query_name": query_name, "rows": rows}
        )
        if self.insert_error is not None:
            raise self.insert_error


def _client(fake_api):
    """A client wired to ``fake_api`` and the two test list names."""
    return SampleRegistryClient(
        api_factory=lambda: fake_api,
        registry_list=REGISTRY_LIST,
        authority_list=AUTHORITY_LIST,
    )


def _rows(rows, row_count=None):
    """A select_rows result: the rows plus LabKey's total ``rowCount``."""
    return {"rows": rows, "rowCount": len(rows) if row_count is None else row_count}


# ---- fetch_authority_map --------------------------------------------------


def test_authority_map_maps_prefix_to_sewershed_name_and_location():
    """Each authority row becomes ``prefix -> (sewershed_name, sewershed_location)``."""
    fake_api = FakeApi(
        {
            AUTHORITY_LIST: _rows(
                [
                    {
                        "sample_prefix_code": "WI_Madison_MMSD",
                        "sewershed_name": "MMSD",
                        "sewershed_location": "Madison, WI",
                    },
                    {"sample_prefix_code": None, "sewershed_name": "ignored"},
                ]
            )
        }
    )

    authority_map = _client(fake_api).fetch_authority_map()

    assert authority_map == {"WI_Madison_MMSD": ("MMSD", "Madison, WI")}
    assert fake_api.select_calls[0]["schema_name"] == "lists"
    assert fake_api.select_calls[0]["query_name"] == AUTHORITY_LIST


def test_authority_map_read_failure_raises():
    """An unreadable authority list fails closed — never an empty map."""
    fake_api = FakeApi(select_error=ConnectionError("server down"))

    with pytest.raises(SampleRegistryError, match="authority list"):
        _client(fake_api).fetch_authority_map()


def test_empty_authority_list_raises():
    """An empty authority list is a wrong list name, not "every prefix is unknown"."""
    fake_api = FakeApi({AUTHORITY_LIST: _rows([])})

    with pytest.raises(SampleRegistryError, match="empty"):
        _client(fake_api).fetch_authority_map()


def test_truncated_authority_read_raises():
    """LabKey reporting more rows than it returned means the read was capped."""
    fake_api = FakeApi(
        {
            AUTHORITY_LIST: _rows(
                [{"sample_prefix_code": "A", "sewershed_name": "n"}], row_count=5
            )
        }
    )

    with pytest.raises(SampleRegistryError, match="truncated"):
        _client(fake_api).fetch_authority_map()


# ---- fetch_registered_labels ----------------------------------------------


def test_registered_labels_filters_to_the_labels_asked_about():
    """The lookup is limited to this run's labels, so a big list is never capped."""
    fake_api = FakeApi(
        {
            REGISTRY_LIST: _rows(
                [{"sequence_sample_label": "WI_Madison_MMSD_20261006_S7"}]
            )
        }
    )

    found = _client(fake_api).fetch_registered_labels(
        ["WI_Madison_MMSD_20261006_S7", "CA_Riverside_RWQCP_20261008_S27"]
    )

    assert found == {"WI_Madison_MMSD_20261006_S7"}
    (in_filter,) = fake_api.select_calls[0]["filter_array"]
    assert in_filter.column == "sequence_sample_label"
    assert set(in_filter.values) == {
        "WI_Madison_MMSD_20261006_S7",
        "CA_Riverside_RWQCP_20261008_S27",
    }


def test_registered_labels_with_no_labels_makes_no_call():
    """Nothing to look up means no network at all."""
    fake_api = FakeApi()

    assert _client(fake_api).fetch_registered_labels([]) == set()
    assert fake_api.select_calls == []


def test_registered_labels_read_failure_raises():
    """The C3 fix: a failed read raises — the poller returned set() and re-inserted everything."""
    fake_api = FakeApi(select_error=TimeoutError("slow"))

    with pytest.raises(SampleRegistryError, match="registered"):
        _client(fake_api).fetch_registered_labels(["A_20261006_S1"])


def test_truncated_registered_labels_read_raises():
    """A capped read could hide an existing row and cause a duplicate insert."""
    fake_api = FakeApi({REGISTRY_LIST: _rows([], row_count=3)})

    with pytest.raises(SampleRegistryError, match="truncated"):
        _client(fake_api).fetch_registered_labels(["A_20261006_S1"])


# ---- insert_rows ------------------------------------------------------------


def test_insert_rows_writes_to_the_registry_list():
    """Rows go to ``lists.<registry_list>`` unchanged."""
    fake_api = FakeApi()
    rows = [{"sequence_sample_label": "A_20261006_S1"}]

    _client(fake_api).insert_rows(rows)

    assert fake_api.insert_calls == [
        {"schema_name": "lists", "query_name": REGISTRY_LIST, "rows": rows}
    ]


def test_insert_failure_raises_without_leaking_the_error_body():
    """A rejected insert raises; the message names the error type, never its text."""
    fake_api = FakeApi(insert_error=RuntimeError("secret-looking body"))

    with pytest.raises(SampleRegistryError) as raised:
        _client(fake_api).insert_rows([{"sequence_sample_label": "A_20261006_S1"}])

    assert "RuntimeError" in str(raised.value)
    assert "secret-looking body" not in str(raised.value)


# ---- make_sample_registry_client ------------------------------------------


def _config():
    """The config keys the factory reads (placeholder values only)."""
    return {
        "labkey_server": "labkey.example.org",
        "labkey_sample_registry_project": "some/project",
        "labkey_sample_registry_list": REGISTRY_LIST,
        "labkey_sample_authority_list": AUTHORITY_LIST,
        "labkey_api_key_file": "/nonexistent/key",
    }


def test_factory_builds_a_client_without_touching_the_world():
    """Construction reads no key file and imports nothing — the key path does not exist."""
    client = make_sample_registry_client(_config())

    assert isinstance(client, SampleRegistryClient)


def test_factory_requires_every_key():
    """A missing key is a loud KeyError naming it, never a baked-in default."""
    config = _config()
    del config["labkey_sample_authority_list"]

    with pytest.raises(KeyError, match="labkey_sample_authority_list"):
        make_sample_registry_client(config)
