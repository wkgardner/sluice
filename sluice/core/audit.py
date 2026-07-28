"""Append-only writer for the ``control_audit`` trail — state, never a log line.

Imports are a closed set: ``json`` + ``sqlite3`` + ``collections.abc`` +
exactly ``now_iso`` from ``sluice.core.db`` (plan 07 §1). With
``get_connection``/``write_txn`` unreachable, this module structurally cannot
open its own connection or transaction — the caller owns both (design §9), and
the module exposes no update/delete surface, ever (append-only is a
module-surface promise; plan 07 ⚑1).
"""

import json
import sqlite3
from collections.abc import Mapping, Sequence

from sluice.core.db import now_iso

__all__ = ["record_control_action"]


def _refuse_non_string_mapping_keys(value: object) -> None:
    """Raise if any mapping at any depth inside ``value`` carries a non-``str`` key.

    ``json.dumps`` silently coerces ``{1: "x"}`` → ``{"1": "x"}`` and
    ``{True: "x"}`` → ``{"true": "x"}`` — a type rewrite that would record
    fabricated evidence (plan 07 §2). The rule is total, not top-level:
    mappings nested inside lists/tuples are walked too; ``str``/``bytes``
    values are leaves, never descended into.

    Raises:
        TypeError: a mapping at any depth carries a non-``str`` key.
    """
    if isinstance(value, Mapping):
        for key, nested_value in value.items():
            if not isinstance(key, str):
                raise TypeError(
                    f"args mapping key {key!r} ({type(key).__name__}) is not "
                    "str — json.dumps would silently rewrite it; audit "
                    "refuses to record a coerced payload (plan 07 §2)"
                )
            _refuse_non_string_mapping_keys(nested_value)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for element in value:
            _refuse_non_string_mapping_keys(element)


def record_control_action(
    conn: sqlite3.Connection,
    *,
    actor: str,
    action: str,
    target_run: str | None = None,
    args: Mapping[str, object] | None = None,
    result: str | None = None,
    ts: str | None = None,
) -> int:
    """Append one ``control_audit`` row on the caller's connection and return its ``id``.

    The §9 middle step's primitive (plan 07): validation and serialization run
    before exactly one static ``INSERT`` with every value bound, so a refused
    call never touches the connection. The writer never opens, commits, or
    rolls back a transaction (plan 01 F2) — batched inside an op's
    ``write_txn``, state and audit land atomically or not at all. ``args`` is
    never coerced or truncated: a non-serializable payload is refused loudly,
    not recorded lossily.

    Args:
        actor: must be non-blank — an anonymous audit row is worthless
            evidence.
        action: must be non-blank; the action vocabulary is the calling op's
            (subsystem 8), not audit's.
        target_run: the run *name* (plan 01 F1), never validated against
            ``runs`` — the trail must survive run deletion.
        args: the op's own parameters as JSON-shaped primitives with ``str``
            keys at every depth; ``None`` stores SQL NULL, not ``'null'``.
        result: caller-chosen summary string (e.g. ``"ok"``,
            ``"refused: <why>"``); the structured Result dict rides the wide
            event, not this column.
        ts: defaults to ``now_iso()``; pass one so a state write and its
            audit row can share the same instant.

    Raises:
        ValueError: ``actor`` or ``action`` is blank (the message names the
            field); or ``args`` contains NaN/Infinity (``allow_nan=False``).
        TypeError: a mapping in ``args`` has a non-``str`` key at any depth;
            or an ``args`` value is not JSON-serializable (no ``default=``
            hook — a coercing hook would record fabricated evidence).
        sqlite3.ProgrammingError: ``result`` is neither ``str`` nor ``None``
            (sqlite3's own unsupported-type refusal; documented, not wrapped).
        sqlite3.IntegrityError: propagated from the INSERT.
    """
    for field_name, field_value in (("actor", actor), ("action", action)):
        if not field_value.strip():
            raise ValueError(
                f"{field_name} must be non-blank: the schema's NOT NULL "
                "admits '', but an anonymous audit row is worthless evidence "
                "(plan 07 §2)"
            )
    if args is None:
        serialized_args_json = None
    else:
        _refuse_non_string_mapping_keys(args)
        serialized_args_json = json.dumps(
            dict(args), sort_keys=True, ensure_ascii=False, allow_nan=False
        )
    if ts is None:
        ts = now_iso()
    insert_cursor = conn.execute(
        "INSERT INTO control_audit (ts, actor, action, target_run, args_json,"
        " result) VALUES (?, ?, ?, ?, ?, ?)",
        (ts, actor, action, target_run, serialized_args_json, result),
    )
    # lastrowid is the new row's AUTOINCREMENT id — the trail's true ordering
    # key (never ts: wall-clock can step backwards; plan 01 migration 003).
    return insert_cursor.lastrowid
