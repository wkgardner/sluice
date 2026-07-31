"""LabKey experiment world adapter — the fail-closed identity oracle (plan 13 Unit 2).

A **world adapter** derived from config and injected into the ``experiment`` gate;
it is **NOT a ``Gate``** and appears in no profile (the ``gates -> core`` arrow is
auto-guarded by ``tests/test_dependency_arrows.py`` because it lives on the gates
side). It imports **nothing** from ``sluice.core`` and imports the ``labkey``
package **lazily** — only inside the default api factory's returned closure — so the
whole test suite imports and exercises this module with ``labkey`` absent, by
injecting a fake api through ``api_factory``.

Central hazard C1: LabKey experiment creation is non-idempotent, so a naive retry
mints a duplicate real experiment. This adapter's half of the fix is
lookup-before-create keyed on the ``Description`` the gate itself writes — **never**
python-poller's racy "most-recent-by-user" query (``labkey_user_id`` is dropped
from the config surface entirely). Every load-bearing world failure — unreachable,
auth, insert rejected, id unresolvable, or an **ambiguous** match — raises
``LabKeyError``; the gate turns that into a quarantine (fail closed, C5). An
erroring or ambiguous lookup **never** falls through to create.

No secret ever appears in a ``LabKeyError`` message: the api key is read from the
``labkey_api_key_file`` path lazily inside the factory closure and is never passed
to ``find``/``create`` or formatted into any message.
"""

from collections.abc import Callable
from pathlib import Path
from typing import Any, NamedTuple

__all__ = ["ExperimentClient", "LabKeyError", "make_experiment_client"]

# The trailing token the gate writes into every experiment's ``Description`` and
# matches on. Written LAST (nothing follows it) so ``find``'s exact-suffix
# ``endswith`` check distinguishes run ``"A"`` from run ``"AB"`` (F3, C1).
_FOLDER_NAME_PREFIX = "Folder name: "


class LabKeyError(Exception):
    """The single fail-closed signal the ``experiment`` gate catches.

    Raised by both ``ExperimentClient`` methods for every load-bearing LabKey
    failure (unreachable, auth, insert rejected, id unresolvable, ambiguous
    match). Its ``str`` **never** carries the api key (C5/no-secrets): wrapping
    code formats only the run name and the underlying exception's *type*, never
    the api key or an api-error body that might echo it.
    """


class _DescriptionContainsFilter(NamedTuple):
    """A package-neutral ``select_rows`` filter: ``column`` CONTAINS ``value``.

    The adapter builds its ``filter_array`` out of these — plain, inspectable,
    and constructible without the ``labkey`` package — so the suite runs with
    ``labkey`` absent and a test can read ``filter.column`` directly. The default
    api translates each one into a real ``labkey.query.QueryFilter`` at the
    boundary; a fake api simply records it. There is deliberately no
    ``modifiedby``/user filter type — the C1 "most-recent-by-user" query is not
    ported.
    """

    column: str
    value: str


class _LabKeyApi:
    """Package-neutral wrapper over a live ``labkey`` ``APIWrapper`` (default only).

    Owns every ``labkey`` specific: it lazily imports ``QueryFilter`` to translate
    the adapter's neutral ``filter_array`` into the real CONTAINS filter, and
    forwards ``select_rows``/``insert_rows`` to ``api.query``. Only the default
    factory builds this; tests inject a fake with the same two-method surface, so
    the ``ExperimentClient`` logic never touches ``labkey`` directly.
    """

    def __init__(self, api_wrapper: Any) -> None:
        self._api = api_wrapper

    def select_rows(
        self, *, schema_name: str, query_name: str, filter_array: list
    ) -> dict:
        """Translate the neutral filters to real ``QueryFilter``s and select."""
        from labkey.query import QueryFilter  # lazy: only the live path needs it

        real_filters = [
            QueryFilter(spec.column, spec.value, QueryFilter.Types.CONTAINS)
            for spec in filter_array
        ]
        return self._api.query.select_rows(
            schema_name=schema_name,
            query_name=query_name,
            filter_array=real_filters,
        )

    def insert_rows(self, *, schema_name: str, query_name: str, rows: list) -> Any:
        """Forward an insert to ``api.query.insert_rows`` unchanged."""
        return self._api.query.insert_rows(
            schema_name=schema_name, query_name=query_name, rows=rows
        )


def _default_api_factory(
    server: str, project: str, api_key_file: str
) -> Callable[[], _LabKeyApi]:
    """Build the lazy api factory used when the gate is on the live LabKey path.

    Returns a **closure**: nothing external is touched here. The api key file is
    read and ``APIWrapper`` is constructed only when the closure is *called* —
    i.e. only when ``find``/``create`` actually run — so a resuming gate that
    never calls a method never reads the secret and never imports ``labkey``.
    The ``labkey`` import and the key read both live inside the closure.
    """

    def factory() -> _LabKeyApi:
        from labkey.api_wrapper import APIWrapper  # lazy: keeps the suite import-free

        api_key = Path(api_key_file).read_text(encoding="utf-8").strip()
        wrapper = APIWrapper(server, project, api_key=api_key, use_ssl=True)
        return _LabKeyApi(wrapper)

    return factory


class ExperimentClient:
    """Resolve and create LabKey experiments, fail-closed, keyed on ``Description``.

    Holds the ``schema``/``query`` and a **callable** ``api_factory`` (a plain
    seam, not a Protocol). Constructing this touches nothing external; the api is
    built on the first ``find``/``create`` call. Both methods raise ``LabKeyError``
    for every world failure — the gate's sole catch (ADR-0002 quarantine).
    """

    def __init__(
        self, *, api_factory: Callable[[], Any], schema: str, query: str
    ) -> None:
        """Capture the api factory and the schema/query; touch nothing external."""
        self._api_factory = api_factory
        self._schema = schema
        self._query = query

    def find_experiment_by_run_name(self, run_name: str) -> int | None:
        """Return the experiment number whose ``Description`` ends with the token, or ``None``.

        Resolves identity by the ``"Folder name: <run_name>"`` token the gate
        itself writes — **not** by "most-recent-by-user" (C1: a concurrent
        same-user experiment would poison that). A server-side CONTAINS narrows
        the rows; the authoritative match is an **exact-suffix ``endswith``** on
        the *trailing* token, so run ``"RUN_A"`` never matches run ``"RUN_AB"``'s
        experiment (F3 substring-collision guard).

        Returns ``None`` on zero exact matches; the ``int`` ``ExperimentNumber``
        on exactly one. **More than one exact match raises ``LabKeyError``** —
        ambiguous, never guessed (the C1 poison). Any api/network exception is
        wrapped in ``LabKeyError`` (fail closed); the message carries the run
        name and the underlying exception *type*, never the api key.

        Raises:
            LabKeyError: on an ambiguous (>1 exact) match, or any api/network
                failure.
        """
        token = _FOLDER_NAME_PREFIX + run_name
        api = self._api_factory()
        filter_array = [_DescriptionContainsFilter("Description", token)]
        try:
            result = api.select_rows(
                schema_name=self._schema,
                query_name=self._query,
                filter_array=filter_array,
            )
        except LabKeyError:
            raise
        except Exception as api_error:  # noqa: BLE001 — every world failure fails closed
            raise LabKeyError(
                f"LabKey experiment lookup failed for run {run_name!r} "
                f"({type(api_error).__name__})"
            ) from api_error

        rows = result.get("rows", [])
        exact = [row for row in rows if str(row.get("Description", "")).endswith(token)]
        if not exact:
            return None
        if len(exact) > 1:
            raise LabKeyError(
                f"ambiguous LabKey lookup for run {run_name!r}: "
                f"{len(exact)} experiments match {token!r} exactly — refusing "
                "to guess (C1: a wrong reuse or a duplicate create)"
            )
        # The matched row's ExperimentNumber is the id — but a missing, None, or
        # non-numeric value must fail through the SAME LabKeyError door as every
        # other adapter failure (C5: "id unresolvable → LabKeyError"), not a bare
        # KeyError/TypeError/ValueError. The message carries only the run name and
        # the exception type — never the row contents (no-secrets discipline: a
        # row could echo anything).
        try:
            return int(exact[0]["ExperimentNumber"])
        except (KeyError, TypeError, ValueError) as extract_error:
            raise LabKeyError(
                f"LabKey experiment matched for run {run_name!r} but its "
                f"ExperimentNumber was unusable ({type(extract_error).__name__})"
            ) from extract_error

    def create_experiment(self, run_name: str) -> int:
        """Write the experiment (trailing folder-name token) and resolve its number.

        The ``Description`` is a fixed-format string ending with the
        ``"Folder name: <run_name>"`` token and **nothing after it** (F3) — exactly
        what ``find``'s ``endswith`` matches. Deliberately simplified from
        python-poller's format: the NVD-version and date decorations are dropped
        (pulling the NVD version at gate 0 would couple it to the ``nvd`` tool at
        gate 5; nothing downstream parses the ``Description`` beyond the trailing
        folder name).

        LabKey's ``insert_rows`` does not return the ``ExperimentNumber``, so the
        id is resolved by re-running the SAME ``find`` query. If that returns
        ``None`` the experiment exists but its number is unresolvable →
        ``LabKeyError`` (fail loud, never a fabricated id).

        **Single-writer resolve note (F2):** resolving the just-inserted id by
        ``Description`` trusts sluice's single-daemon writer — one look creates one
        experiment. A hypothetical *concurrent* same-folder insert would make the
        resolve return ``>1`` and surface as an ambiguous ``LabKeyError``
        (fail-loud, quarantine) — never a silent duplicate. A stated trust
        boundary, not an unhandled race.

        Raises:
            LabKeyError: if the insert fails, or the id is unresolvable after a
                successful insert.
        """
        description = _build_description(run_name)
        api = self._api_factory()
        try:
            api.insert_rows(
                schema_name=self._schema,
                query_name=self._query,
                rows=[{"Description": description}],
            )
        except LabKeyError:
            raise
        except Exception as api_error:  # noqa: BLE001 — every world failure fails closed
            raise LabKeyError(
                f"LabKey experiment creation failed for run {run_name!r} "
                f"({type(api_error).__name__})"
            ) from api_error

        experiment_number = self.find_experiment_by_run_name(run_name)
        if experiment_number is None:
            raise LabKeyError(
                f"LabKey experiment created for run {run_name!r} but its "
                "ExperimentNumber was unresolvable"
            )
        return experiment_number


def _build_description(run_name: str) -> str:
    """A fixed ``Description`` ending with the trailing folder-name token (F3).

    The token is the LAST segment — nothing follows it — so ``find``'s
    ``endswith`` guard is exact and substring-safe.
    """
    return (
        "Automated sequencing experiment created by sluice. "
        f"{_FOLDER_NAME_PREFIX}{run_name}"
    )


def make_experiment_client(
    config, *, api_factory: Callable[[], Any] | None = None
) -> ExperimentClient:
    """Build an ``ExperimentClient`` from config; construct nothing external.

    Reads its five keys by **subscript** (no ``.get`` default — a missing key is a
    loud ``KeyError``, a deployment bug, never a baked-in fallback):
    ``labkey_server``, ``labkey_experiments_project`` (C2 — the real value lives
    only in the gitignored config, never in code), ``labkey_schema``,
    ``labkey_query``, ``labkey_api_key_file`` (a **path** to a gitignored secret).
    ``labkey_user_id`` is deliberately NOT read (C1). When ``api_factory`` is
    omitted, a lazy default is captured that reads the key file and builds
    ``APIWrapper`` only on first use — so construction is network- and
    secret-free (a resuming gate that never calls a method never reads the key).

    Raises:
        KeyError: if any of the five required keys is absent from ``config``.
    """
    server = config["labkey_server"]
    project = config["labkey_experiments_project"]
    schema = config["labkey_schema"]
    query = config["labkey_query"]
    api_key_file = config["labkey_api_key_file"]

    if api_factory is None:
        api_factory = _default_api_factory(server, project, api_key_file)

    return ExperimentClient(api_factory=api_factory, schema=schema, query=query)
