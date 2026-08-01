"""Run context — the frozen record of everything a gate needs, derived from state.

Imports are a closed set: stdlib ONLY (plan 04 §1). ``sqlite3``,
``sluice.core.db``, ``slack_sdk``, and any ``sluice.gates``/``api``/``cli``
are forbidden by name, so this module structurally cannot query the database
(fact 3), construct a notifier (fact 1), or reverse the ``gates → core``
arrow — ``tests/test_context.py`` enforces it by ast parse.
"""

import dataclasses
import pathlib
import types
from collections.abc import Mapping

# The declared public surface (db unit-7 precedent), grown as names land
# (plan 04 §4).
__all__ = ["RunContext", "is_safe_path_component", "resolve_context"]


@dataclasses.dataclass(frozen=True)
class RunContext:
    """Everything a gate needs to do its work, derived fresh from state (spec §6).

    The field set is LEAN and closed (plan 04 facts 1–2): no notifier (events
    plan ⚑5, daemon-only caller model) and no destination/remote path fields
    (Plan B). Carrying ``db_path`` does not license gate-side run-state
    writes — ADR-0002, reachable ≠ permitted.

    ``run_row`` is the ADR-0006 d.3 gate read path: a sealed read-only snapshot
    of the run row, so a resuming gate reads its own prior tokens (experiment
    id, globus task id) from ``context.run_row`` without a DB touch — carrying
    ``db_path`` still does not license a gate write (ADR-0002). The field is
    OPTIONAL with a sealed empty default: direct constructions that omit it
    stay valid, and ``resolve_context`` fills it on the production path.
    """

    workdir: pathlib.Path
    db_path: pathlib.Path
    config: Mapping[str, object]
    run_row: Mapping[str, object] = types.MappingProxyType({})

    def __post_init__(self) -> None:
        """Seal ``config`` and ``run_row`` as read-only snapshots of the mappings passed in.

        ``frozen=True`` only stops field rebinding; a live shared dict would
        let one gate rewrite config (or the run-row snapshot) for every other
        gate — a second mutable in-memory authority the spine forbids (plan 04
        §3.1). The ``dict`` copy detaches from the caller's mapping; the
        ``MappingProxyType`` wrapper makes item writes and deletes raise
        ``TypeError``. Rebinding goes through ``object.__setattr__`` because the
        instance is frozen.

        The ``run_row`` seal only ever receives a PLAIN dict, never a raw
        ``sqlite3.Row`` (``dict(<Row>)`` raises — a ``Row`` iterates values,
        not items): a direct construction passes a plain dict (or omits it →
        the empty-proxy default), and ``resolve_context`` pre-converts the
        ``Row`` with a keyed comprehension before it ever reaches here.
        """
        object.__setattr__(self, "config", types.MappingProxyType(dict(self.config)))
        object.__setattr__(self, "run_row", types.MappingProxyType(dict(self.run_row)))


def is_safe_path_component(run_name: str) -> bool:
    r"""Return whether ``run_name`` is a single safe path component.

    The traversal defense in one place (plan 14 unit 1). ``context`` owns the
    identity→path boundary, so it owns the rule: a name containing ``/`` or
    ``\``, or equal to ``""``/``"."``/``".."``, must never become a path.
    Without it, ``Path("/staging") / "../../etc"`` escapes the staging root.

    Exported because it is needed away from this boundary too: the daemon's
    per-run jsonl sink (plan 03 §3.11) turns the same identity into a path and
    used to carry a hand-copied twin. One definition, two callers.

    Args:
        run_name: the ``run.name`` field value to validate.

    Returns:
        ``True`` iff the name is a single safe path component.
    """
    return not ("/" in run_name or "\\" in run_name or run_name in ("", ".", ".."))


def resolve_context(run_row, config: Mapping[str, object]) -> RunContext:
    r"""Derive a ``RunContext`` from an already-fetched run row and loaded config.

    Pure path arithmetic over its inputs (fact 3): ``workdir`` is
    ``{config["local_staging_dir"]}/{run_row["name"]}`` — name-keyed, B2 ⚑1
    approved, and on no other root (§3.3: never ``/scratch``/system-temp) —
    and ``db_path`` is ``config["state_file"]``. No ``.resolve()``, no
    ``mkdir``, no DB access: the daemon creates ``workdir`` and fetches the
    row before calling (plan 04 §1, §7). Required keys are read by subscript,
    never ``.get`` with a default (guardrail §8), and the name is validated as
    a single safe path component before it becomes a path (§3.3) — this is
    the boundary where a stored string identity turns into a filesystem path.

    The run row is snapshotted onto the context (ADR-0006 d.3): a resuming gate
    reads its prior tokens (experiment id, globus task id) off
    ``context.run_row`` with no DB touch. The snapshot is built here by a keyed
    comprehension — ``dict(run_row)`` is WRONG for a ``sqlite3.Row`` (it
    iterates values, not items, and raises), so keys are read explicitly — so
    the ``RunContext`` seal only ever receives a plain dict.

    Raises:
        KeyError: If ``config`` lacks ``local_staging_dir`` or ``state_file``.
            (A ``sqlite3.Row`` lacking ``name`` would raise ``IndexError``
            instead — unreachable in practice, since ``name`` is the ``runs``
            primary key.)
        ValueError: If ``run_row["name"]`` is not a single safe path
            component — it contains ``/`` or ``\``, or equals ``""``,
            ``"."``, or ``".."`` (§3.3 injection defense).
    """
    run_name = run_row["name"]
    # Injection defense at the identity→path boundary (§3.3): without this,
    # Path("/staging") / "../../etc" escapes the staging root. The message
    # carries the raw offending name so the daemon's quarantine-grade log
    # (§7) says which stored name was refused.
    if not is_safe_path_component(run_name):
        raise ValueError(
            f'run name "{run_name}" is not a single safe path component: '
            'it must not contain "/" or "\\" and must not equal "", ".", '
            'or ".." (plan 04 §3.3)'
        )
    # Keyed comprehension, not dict(run_row): a sqlite3.Row iterates values,
    # not items, so dict(<Row>) raises — build the plain-dict snapshot the
    # RunContext seal expects (ADR-0006 d.3).
    run_snapshot = {key: run_row[key] for key in run_row.keys()}
    return RunContext(
        workdir=pathlib.Path(config["local_staging_dir"]) / run_name,
        db_path=pathlib.Path(config["state_file"]),
        config=config,
        run_row=run_snapshot,
    )
