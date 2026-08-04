"""Initialise or upgrade a sluice installation under ``~/.sluice``.

Backs ``sluice setup``, and is written to be run **repeatedly** rather than once. The same command
that creates a fresh install also upgrades an existing one after a sluice release: it adds keys the
new version introduced, leaves every existing value alone, and reports exactly what changed.

## The rule that makes re-running safe

**An existing value is never overwritten.** Not by a default, not by a new release's opinion, not
by a re-run. Setup only ever *adds* what is missing and *reports* what needs attention. The only
destructive thing it can do is create a directory, and it stops short of that where a wrong path
would matter.

That rule is what lets an upgrade be run without reading the diff first. A setup that silently
reset a tuned ``parallel_downloads`` back to its default on every release would be unusable exactly
when it is most needed.

## Layout

``~/.sluice`` holds everything sluice owns; the operator supplies only paths to things that already
exist in the world (mounts, reference data, the sequencing server)::

    ~/.sluice/
      config.json          the config, 0600
      secrets/             credential files, 0700 dir / 0600 files
      state/               the SQLite database
      logs/                events.jsonl, per-run logs, per-experiment NVD logs
      nvd-launch/          Nextflow launch dirs — local disk on purpose (errata D2)

Putting ``nvd-launch`` here is not incidental. It has to be on local disk rather than the shared
staging tree, and a home directory is local on every host sluice runs on, so the default is correct
rather than merely convenient.
"""

import dataclasses
import json
import pathlib

from sluice.config import default_home
from sluice.config.schema import CONFIG_FIELDS, ConfigField, FieldKind

__all__ = [
    "SetupPlan",
    "SetupResult",
    "apply_plan",
    "plan_setup",
]

# Directories sluice creates and owns. secrets/ is deliberately first and gets
# tighter permissions.
_MANAGED_SUBDIRECTORIES = ("secrets", "state", "logs", "logs/runs", "logs/nvd")


@dataclasses.dataclass(frozen=True)
class SetupPlan:
    """What setup intends to do, computed before anything is written.

    Separating the plan from its application is what makes ``--dry-run`` honest
    and the whole operation reviewable: the same computation produces both the
    preview and the change.
    """

    home: pathlib.Path
    config_path: pathlib.Path
    is_upgrade: bool
    # key -> value, for keys that already have one. Carried forward untouched.
    existing: dict
    # key -> value, for keys this run will add with a usable default.
    added_with_default: dict
    # Fields that need a human: no value yet and no default setup can invent.
    needs_attention: tuple[ConfigField, ...]
    # Keys present in the config file that no longer appear in the schema.
    unknown_keys: tuple[str, ...]

    @property
    def merged(self) -> dict:
        """The config this plan would write."""
        return {**self.existing, **self.added_with_default}


@dataclasses.dataclass(frozen=True)
class SetupResult:
    """What setup actually did."""

    plan: SetupPlan
    created_directories: tuple[pathlib.Path, ...]
    created_secret_placeholders: tuple[pathlib.Path, ...]
    database_initialised: bool


def _default_for(field: ConfigField, home: pathlib.Path):
    """The value setup would use for ``field``, or ``None`` if a human must supply it."""
    if field.home_relative is not None:
        return str(home / field.home_relative)
    return field.default


def plan_setup(home=None, config_path=None) -> SetupPlan:
    """Work out what setup would do, without touching anything.

    Reads any existing config and diffs it against the schema. Every existing
    value is carried forward verbatim — this function has no opinion about values
    that are already set, which is what makes re-running safe.

    Args:
        home: the sluice home; defaults to ``$SLUICE_HOME`` or ``~/.sluice``.
        config_path: override the config location; defaults to
            ``<home>/config.json``.

    Returns:
        The plan.
    """
    home = pathlib.Path(home).expanduser() if home else default_home()
    config_path = (
        pathlib.Path(config_path).expanduser() if config_path else home / "config.json"
    )

    existing: dict = {}
    is_upgrade = config_path.is_file()
    if is_upgrade:
        try:
            loaded = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # A corrupt config is treated as "nothing known yet" for planning, so
            # setup can still report what is needed. apply_plan refuses to write
            # over it (see below), so nothing is lost.
            loaded = {}
        if isinstance(loaded, dict):
            existing = loaded

    added: dict = {}
    needs_attention: list[ConfigField] = []
    for field in CONFIG_FIELDS:
        if field.name in existing:
            continue
        default = _default_for(field, home)
        if default is not None:
            added[field.name] = default
        elif field.required:
            needs_attention.append(field)
        else:
            # Optional with no default: write it empty so the key is visible and
            # discoverable rather than silently absent.
            added[field.name] = ""

    known = {field.name for field in CONFIG_FIELDS}
    unknown = tuple(sorted(key for key in existing if key not in known))

    return SetupPlan(
        home=home,
        config_path=config_path,
        is_upgrade=is_upgrade,
        existing=existing,
        added_with_default=added,
        needs_attention=tuple(needs_attention),
        unknown_keys=unknown,
    )


def apply_plan(plan: SetupPlan, *, initialise_database=True) -> SetupResult:
    """Create the directories, write the config, and migrate the database.

    Raises:
        ValueError: the existing config file could not be parsed. Setup refuses to
            overwrite a file it could not read — the operator's values might still
            be in there, and clobbering them to "fix" a syntax error would be the
            worst possible outcome.
        OSError: a directory or file could not be created.
    """
    if plan.is_upgrade and not plan.existing:
        # Distinguishes "empty JSON object" (fine) from "unparseable" (not).
        try:
            json.loads(plan.config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as unreadable:
            raise ValueError(
                f"{plan.config_path} exists but could not be parsed "
                f"({unreadable}). Refusing to overwrite it — fix or move the file, "
                "then run setup again."
            ) from unreadable

    created_directories = []
    plan.home.mkdir(parents=True, exist_ok=True)
    created_directories.append(plan.home)
    for relative in _MANAGED_SUBDIRECTORIES:
        directory = plan.home / relative
        directory.mkdir(parents=True, exist_ok=True)
        created_directories.append(directory)
    # Credentials live here; keep it owner-only regardless of umask.
    (plan.home / "secrets").chmod(0o700)

    # Managed paths may point outside the standard subdirectories if an operator
    # moved them, so create each one's parent explicitly.
    merged = plan.merged
    for field in CONFIG_FIELDS:
        if field.kind is not FieldKind.MANAGED_PATH:
            continue
        value = merged.get(field.name)
        if not value:
            continue
        target = pathlib.Path(value)
        directory = target.parent if target.suffix else target
        directory.mkdir(parents=True, exist_ok=True)

    created_placeholders = []
    for field in CONFIG_FIELDS:
        if field.kind is not FieldKind.SECRET_FILE:
            continue
        value = merged.get(field.name)
        if not value:
            continue
        secret_path = pathlib.Path(value)
        secret_path.parent.mkdir(parents=True, exist_ok=True)
        if not secret_path.exists():
            # An empty placeholder, not a fabricated credential: the file exists so
            # the path is obviously real, and it is empty so nothing can mistake it
            # for a working secret.
            secret_path.touch()
            created_placeholders.append(secret_path)
        secret_path.chmod(0o600)

    plan.config_path.parent.mkdir(parents=True, exist_ok=True)
    plan.config_path.write_text(
        json.dumps(merged, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    plan.config_path.chmod(0o600)

    database_initialised = False
    state_file = merged.get("state_file")
    if initialise_database and state_file:
        # Imported here rather than at module scope: setup is also the thing an
        # operator runs to diagnose a broken install, and it should not fail to
        # start because some unrelated core import broke.
        from sluice.core.db import initialize_database

        pathlib.Path(state_file).parent.mkdir(parents=True, exist_ok=True)
        initialize_database(state_file)
        database_initialised = True

    return SetupResult(
        plan=plan,
        created_directories=tuple(created_directories),
        created_secret_placeholders=tuple(created_placeholders),
        database_initialised=database_initialised,
    )
