"""LabKey sample-registry world adapter — the fail-closed half of the ``registration`` gate.

A **world adapter** injected into the ``registration`` gate; it is **NOT a ``Gate``**
and appears in no profile. Like ``labkey.py`` it imports nothing from
``sluice.core`` and imports the ``labkey`` package **lazily** — only inside the
default api factory's closure — so the suite exercises it with ``labkey`` absent by
injecting a fake api through ``api_factory``.

Two LabKey lists are involved, both in the ``lists`` schema of one project:

- the **authority list** — which sample-name prefixes are real sewersheds, and each
  one's ``sewershed_name`` / ``sewershed_location``;
- the **registry list** — the sample-metadata list itself, one row per registered
  ``sequence_sample_label``.

Ported from python-poller ``authority_registration.py`` (``fetch_authority_map``,
``fetch_registered_labels``, ``insert_metagenomics_rows``). Deliberate deviations,
all for errata **C3** (the poller fails OPEN, which mints duplicate rows):

- every read or write failure raises ``SampleRegistryError`` — the poller returned
  ``None`` / ``set()`` and carried on, so a LabKey blip made every sample look new;
- an **empty** authority list raises — it is a wrong list name, not "no prefix is
  valid";
- a **truncated** read raises (LabKey's ``rowCount`` larger than the rows returned);
- a successful reply with no real result raises — the client hands back a status dict
  for a non-JSON 2xx body (a proxy's maintenance page), which must not read as "0 rows";
- an insert is **read back** before it counts, because a reply can look accepted
  without the rows having landed;
- the registered-labels lookup is filtered to the labels being asked about
  (an ``IN`` filter) instead of reading the whole list, so a large list can never be
  silently capped.

No secret ever appears in a ``SampleRegistryError`` message: the key file is read
lazily inside the factory closure, and wrapped errors carry only the exception's
*type*, never its text (an api error body could echo anything).
"""

from collections.abc import Callable
from pathlib import Path
from typing import Any, NamedTuple

__all__ = [
    "SampleRegistryClient",
    "SampleRegistryError",
    "make_sample_registry_client",
]

# Both lists live in LabKey's ``lists`` schema (python-poller hard-codes the same).
_LISTS_SCHEMA = "lists"

# Column names, fixed by the lists themselves (poller ``authority_registration.py``).
_AUTHORITY_COLUMNS = "sample_prefix_code,sewershed_name,sewershed_location"
_LABEL_COLUMN = "sequence_sample_label"


class SampleRegistryError(Exception):
    """The single fail-closed signal the ``registration`` gate catches.

    Raised for every LabKey problem the gate cannot safely carry on past. Its ``str``
    never carries the API key or an api error body.
    """


class _InFilter(NamedTuple):
    """A package-neutral ``select_rows`` filter: ``column`` IN ``values``.

    The adapter builds these without the ``labkey`` package so tests can read
    ``filter.column`` / ``filter.values`` directly; the live api translates each into
    a real ``QueryFilter`` at the boundary (the ``labkey.py`` precedent).
    """

    column: str
    values: tuple[str, ...]


class _RegistryApi:
    """Package-neutral wrapper over a live ``labkey`` ``APIWrapper`` (default only)."""

    def __init__(self, api_wrapper: Any) -> None:
        self._api = api_wrapper

    def select_rows(
        self, *, schema_name: str, query_name: str, columns: str, filter_array=None
    ) -> dict:
        """Translate neutral ``_InFilter``s to real IN filters, then select."""
        from labkey.query import QueryFilter  # lazy: only the live path needs it

        real_filters = [
            QueryFilter(spec.column, ";".join(spec.values), QueryFilter.Types.IN)
            for spec in (filter_array or [])
        ]
        return self._api.query.select_rows(
            schema_name=schema_name,
            query_name=query_name,
            columns=columns,
            filter_array=real_filters or None,
        )

    def insert_rows(self, *, schema_name: str, query_name: str, rows: list) -> Any:
        """Forward an insert to ``api.query.insert_rows`` unchanged."""
        return self._api.query.insert_rows(
            schema_name=schema_name, query_name=query_name, rows=rows
        )


def _default_api_factory(
    server: str, project: str, api_key_file: str
) -> Callable[[], _RegistryApi]:
    """A closure that reads the key and builds the api only when first called.

    Nothing external is touched here, so building a client is network- and
    secret-free (the ``labkey.py`` ``_default_api_factory`` precedent).
    """

    def factory() -> _RegistryApi:
        from labkey.api_wrapper import APIWrapper  # lazy: keeps the suite import-free

        api_key = Path(api_key_file).read_text(encoding="utf-8").strip()
        wrapper = APIWrapper(server, project, api_key=api_key, use_ssl=True)
        return _RegistryApi(wrapper)

    return factory


def _select_complete(
    api_factory, *, query_name: str, columns: str, filter_array, what: str
):
    """Run one select and return its rows, raising on any failure or truncation.

    ``what`` names the read in the error message ("authority list", "registered
    labels"). LabKey's ``rowCount`` is the total matching rows; fewer rows returned
    than that means the server capped the read, which would hide real rows.
    """
    try:
        result = api_factory().select_rows(
            schema_name=_LISTS_SCHEMA,
            query_name=query_name,
            columns=columns,
            filter_array=filter_array,
        )
    except Exception as api_error:  # noqa: BLE001 — every world failure fails closed
        raise SampleRegistryError(
            f"could not read the {what} from LabKey list {query_name!r} "
            f"({type(api_error).__name__})"
        ) from api_error

    rows = result.get("rows") if isinstance(result, dict) else None
    total = result.get("rowCount") if isinstance(result, dict) else None
    if (
        not isinstance(rows, list)
        or not isinstance(total, int)
        or not all(isinstance(row, dict) for row in rows)
    ):
        raise SampleRegistryError(
            f"the {what} read from LabKey list {query_name!r} did not return a usable "
            "result — refusing to treat that as an empty list"
        )
    if total > len(rows):
        raise SampleRegistryError(
            f"the {what} read from LabKey list {query_name!r} was truncated "
            f"({len(rows)} of {total} rows) — refusing to act on a partial list"
        )
    return rows


class SampleRegistryClient:
    """Read the authority list, look up registered labels, insert new rows — fail-closed.

    Construction touches nothing external; the api is built on each method call
    through ``api_factory``. Every method raises ``SampleRegistryError`` for every
    world failure.
    """

    def __init__(
        self,
        *,
        api_factory: Callable[[], Any],
        registry_list: str,
        authority_list: str,
    ) -> None:
        """Capture the api factory and the two list names."""
        self._api_factory = api_factory
        self._registry_list = registry_list
        self._authority_list = authority_list

    def fetch_authority_map(self) -> dict[str, tuple[Any, Any]]:
        """Return ``{sample_prefix_code: (sewershed_name, sewershed_location)}``.

        Rows with no prefix are ignored (poller parity).

        Raises:
            SampleRegistryError: the read failed, was truncated, or the list is
                empty (an empty authority list is a misconfigured list name).
        """
        rows = _select_complete(
            self._api_factory,
            query_name=self._authority_list,
            columns=_AUTHORITY_COLUMNS,
            filter_array=None,
            what="authority list",
        )
        authority_map = {
            row["sample_prefix_code"]: (
                row.get("sewershed_name"),
                row.get("sewershed_location"),
            )
            for row in rows
            if row.get("sample_prefix_code")
        }
        if not authority_map:
            raise SampleRegistryError(
                f"the authority list {self._authority_list!r} is empty — check "
                "labkey_sample_authority_list in the config"
            )
        return authority_map

    def fetch_registered_labels(self, labels) -> set[str]:
        """Return which of ``labels`` already have a row in the registry list.

        An empty ``labels`` returns an empty set without any network call.

        Raises:
            SampleRegistryError: the read failed, was truncated, or returned a row
                without its label. Never returns an empty set in place of an answer
                (the C3 fix).
        """
        wanted = tuple(labels)
        if not wanted:
            return set()
        rows = _select_complete(
            self._api_factory,
            query_name=self._registry_list,
            columns=_LABEL_COLUMN,
            filter_array=[_InFilter(_LABEL_COLUMN, wanted)],
            what="registered labels",
        )
        found_labels = {row.get(_LABEL_COLUMN) for row in rows}
        if not all(found_labels):
            raise SampleRegistryError(
                f"the registered labels read from LabKey list {self._registry_list!r} "
                "did not return a usable result — a matched row has no label"
            )
        return found_labels

    def insert_rows(self, rows: list[dict]) -> None:
        """Insert ``rows`` into the registry list in one call, then read them back.

        Raises:
            SampleRegistryError: LabKey rejected or could not take the insert, or the
                rows are not all there afterwards.
        """
        try:
            self._api_factory().insert_rows(
                schema_name=_LISTS_SCHEMA, query_name=self._registry_list, rows=rows
            )
        except Exception as api_error:  # noqa: BLE001 — every world failure fails closed
            raise SampleRegistryError(
                f"LabKey rejected the insert of {len(rows)} row(s) into list "
                f"{self._registry_list!r} ({type(api_error).__name__})"
            ) from api_error

        inserted_labels = {row[_LABEL_COLUMN] for row in rows}
        missing_labels = inserted_labels - self.fetch_registered_labels(inserted_labels)
        if missing_labels:
            raise SampleRegistryError(
                f"LabKey accepted the insert into list {self._registry_list!r}, but "
                f"{len(missing_labels)} of {len(inserted_labels)} row(s) are not there"
            )


def make_sample_registry_client(
    config, *, api_factory: Callable[[], Any] | None = None
) -> SampleRegistryClient:
    """Build a ``SampleRegistryClient`` from config; construct nothing external.

    Reads its five keys by **subscript** — a missing key is a loud ``KeyError``, never
    a baked-in production default: ``labkey_server``,
    ``labkey_sample_registry_project``, ``labkey_sample_registry_list``,
    ``labkey_sample_authority_list``, ``labkey_api_key_file`` (a path to the secret,
    read only when a method first runs).

    Raises:
        KeyError: if any required key is absent.
    """
    server = config["labkey_server"]
    project = config["labkey_sample_registry_project"]
    registry_list = config["labkey_sample_registry_list"]
    authority_list = config["labkey_sample_authority_list"]
    api_key_file = config["labkey_api_key_file"]

    if api_factory is None:
        api_factory = _default_api_factory(server, project, api_key_file)

    return SampleRegistryClient(
        api_factory=api_factory,
        registry_list=registry_list,
        authority_list=authority_list,
    )
