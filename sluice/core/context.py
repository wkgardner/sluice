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
__all__ = ["RunContext", "resolve_context"]


@dataclasses.dataclass(frozen=True)
class RunContext:
    """Everything a gate needs to do its work, derived fresh from state (spec §6).

    The field set is LEAN and closed (plan 04 facts 1–2): no notifier (events
    plan ⚑5, daemon-only caller model) and no destination/remote path fields
    (Plan B). Carrying ``db_path`` does not license gate-side run-state
    writes — ADR-0002, reachable ≠ permitted.
    """

    workdir: pathlib.Path
    db_path: pathlib.Path
    config: Mapping[str, object]

    def __post_init__(self) -> None:
        """Seal ``config`` as a read-only snapshot of the mapping passed in.

        ``frozen=True`` only stops field rebinding; a live shared dict would
        let one gate rewrite config for every other gate — a second mutable
        in-memory authority the spine forbids (plan 04 §3.1). The ``dict``
        copy detaches from the caller's mapping; the ``MappingProxyType``
        wrapper makes item writes and deletes raise ``TypeError``. Rebinding
        goes through ``object.__setattr__`` because the instance is frozen.
        """
        object.__setattr__(self, "config", types.MappingProxyType(dict(self.config)))


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
    if "/" in run_name or "\\" in run_name or run_name in ("", ".", ".."):
        raise ValueError(
            f'run name "{run_name}" is not a single safe path component: '
            'it must not contain "/" or "\\" and must not equal "", ".", '
            'or ".." (plan 04 §3.3)'
        )
    return RunContext(
        workdir=pathlib.Path(config["local_staging_dir"]) / run_name,
        db_path=pathlib.Path(config["state_file"]),
        config=config,
    )
