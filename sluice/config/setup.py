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
      config.toml          the config, 0600
      secrets/             credential files, 0700 dir / 0600 files
      state/               the SQLite database
      logs/                events.jsonl, per-run logs, per-experiment NVD logs
      nvd-launch/          Nextflow launch dirs — local disk on purpose (errata D2)

Putting ``nvd-launch`` here is not incidental. It has to be on local disk rather than the shared
staging tree, and a home directory is local on every host sluice runs on, so the default is correct
rather than merely convenient.

## The file is regenerated, not edited

``render_config_toml`` writes the whole file from the schema every run: one table per group, each
key preceded by its ``ConfigField.help``. That is why no comment-preserving TOML library is needed
— comments are re-derived rather than round-tripped — and why the file cannot describe a key
differently from ``sluice doctor``, which reads the same help text.

The cost is that a comment an operator adds by hand does not survive the next setup. Values do,
which is the half that matters; the backup and the confirmation prompt exist for the other half.

## Migrating a pre-TOML install

An install with a ``config.json`` and no ``config.toml`` is carried across automatically: setup
reads the JSON, writes the TOML, and **leaves the JSON exactly where it is**. Nothing is
overwritten, so the migration needs no confirmation and no backup, and undoing it means deleting
one file.
"""

import dataclasses
import pathlib
import textwrap

from sluice.config import (
    DEFAULT_CONFIG_FILENAME,
    LEGACY_CONFIG_FILENAME,
    ConfigError,
    default_home,
    parse_config_text,
)
from sluice.config.schema import CONFIG_FIELDS, ConfigField, FieldKind, fields_by_group

__all__ = [
    "SetupPlan",
    "SetupResult",
    "apply_plan",
    "managed_directory_for",
    "plan_setup",
    "render_config_toml",
]

# Directories sluice creates and owns. secrets/ is deliberately first and gets
# tighter permissions.
_MANAGED_SUBDIRECTORIES = ("secrets", "state", "logs", "logs/runs", "logs/nvd")

# Where keys from an older release land — present in the file, no longer in the
# schema. Named rather than dropped: setup never deletes an operator's value, and
# a key sitting under a table that says "nothing reads this" is a clearer report
# than the same key silently mixed in with live ones.
_UNRECOGNISED_TABLE = "unrecognised"

# Comment text wraps here. Narrow enough to stay readable beside a value in a
# split editor pane.
_COMMENT_WIDTH = 76


def managed_directory_for(value) -> pathlib.Path:
    """The directory setup creates for a ``MANAGED_PATH`` value.

    A value with a suffix names a *file* sluice writes (``logs/events.jsonl``),
    so the directory is its parent and the file itself appears only on first
    write; a value without one names the directory itself (``logs/runs``).
    Exported because doctor must judge a managed path by the same rule — warning
    that ``events.jsonl`` is absent on a correctly set-up install is the
    cry-wolf failure that trains operators to ignore the report.

    The suffix test is a heuristic and misreads a *directory* whose name
    contains a dot (``logs/2026.runs`` is judged the file ``runs`` under
    ``logs/``). No shipped ``home_relative`` value has that shape, and the schema
    carries no file-vs-directory flag to ask instead.
    """
    target = pathlib.Path(value)
    return target.parent if target.suffix else target


def _toml_table_name(group: str) -> str:
    """A schema group as a bare TOML table name (``nvd analysis`` → ``nvd_analysis``).

    Bare rather than quoted because ``["nvd analysis"]`` is legal TOML that reads
    like a mistake. The name is cosmetic in any case — ``flatten_tables`` discards
    it, so this never has to round-trip back to the group.
    """
    return group.replace(" ", "_")


def _toml_value(value) -> str:
    """One config value as a TOML scalar.

    Bools before ints deliberately: ``bool`` is a subclass of ``int`` in Python,
    so checking ``int`` first would write ``slack_enabled = 0`` and quietly change
    the type an operator reads.

    Raises:
        TypeError: a value that is not a string, bool or number. sluice has no
            such config key today, and a silent ``str()`` of one would produce a
            file that parses back as something else.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        escaped = escaped.replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
        return f'"{escaped}"'
    raise TypeError(
        f"cannot write {type(value).__name__} to the config: sluice config "
        "values are strings, booleans and numbers"
    )


def _comment_lines(text: str) -> list[str]:
    """``text`` as wrapped ``#`` comment lines."""
    return [f"# {line}" for line in textwrap.wrap(text, width=_COMMENT_WIDTH)]


def render_config_toml(values: dict, unknown_keys=()) -> str:
    """Render the config file: one table per schema group, every key explained.

    The whole point of the TOML move. Section headers and the per-key help come
    from the schema, so the file an operator edits explains itself and cannot
    describe a key differently from ``sluice doctor`` — both read
    ``ConfigField.help``.

    Setup regenerates this file in full on every run rather than editing it in
    place, which is why no comment-preserving TOML library is needed: the
    comments are not preserved, they are re-derived. What *is* preserved is every
    value, which is the part that matters.

    Args:
        values: the flat key → value mapping to write. Keys absent from the
            schema are written under the unrecognised table if listed in
            ``unknown_keys``, and otherwise skipped.
        unknown_keys: keys to carry forward that the schema no longer declares.

    Returns:
        The file's full text, ending in a newline.
    """
    lines = [
        "# sluice configuration.",
        "#",
        "# Section headers group keys for reading only — sluice ignores them, so a",
        "# key works wherever you put it. Every key's own name is what matters.",
        "#",
        "# Regenerated by `sluice setup`: your values are kept, these comments are",
        "# rewritten. Real infrastructure values live here and nowhere else.",
    ]

    for group, fields in fields_by_group().items():
        present = [field for field in fields if field.name in values]
        if not present:
            continue
        lines.append("")
        lines.append(f"[{_toml_table_name(group)}]")
        for field in present:
            lines.append("")
            lines.extend(_comment_lines(field.help))
            if not field.required:
                lines.append("# Optional — leave empty to switch it off.")
            lines.append(f"{field.name} = {_toml_value(values[field.name])}")

    carried = [key for key in unknown_keys if key in values]
    if carried:
        lines.append("")
        lines.append(f"[{_UNRECOGNISED_TABLE}]")
        lines.append("")
        lines.extend(
            _comment_lines(
                "Nothing in this version of sluice reads these. They are kept "
                "because setup never deletes a value you set — delete them "
                "yourself once you are sure."
            )
        )
        for key in carried:
            lines.append(f"{key} = {_toml_value(values[key])}")

    return "\n".join(lines) + "\n"


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
    # key -> "", for keys setup cannot supply a value for. Written anyway so the
    # operator edits a value instead of transcribing a key name from a terminal
    # into JSON — a mistyped key is silently absent, which is a worse failure
    # than an empty one.
    added_blank: dict
    # Fields that need a human: no value yet and no default setup can invent.
    # A subset of `added_blank` — the required ones, which drive the report.
    needs_attention: tuple[ConfigField, ...]
    # Keys present in the config file that no longer appear in the schema.
    unknown_keys: tuple[str, ...]
    # The pre-TOML `config.json` these values came from, if this run is carrying
    # an older install across. `None` for a fresh install and for one already on
    # TOML. Distinct from `is_upgrade`, which is about the TOML file itself:
    # a migration writes a config.toml that does not exist yet, so it overwrites
    # nothing and needs neither a confirmation nor a backup.
    migrated_from: pathlib.Path | None = None

    @property
    def merged(self) -> dict:
        """The config this plan would write."""
        return {**self.existing, **self.added_with_default, **self.added_blank}

    @property
    def source_path(self) -> pathlib.Path | None:
        """The file existing values were read from, or ``None`` if there was none."""
        if self.migrated_from is not None:
            return self.migrated_from
        return self.config_path if self.is_upgrade else None


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

    An install that predates the TOML move is carried across here: when there is
    no ``config.toml`` but a ``config.json`` sits beside where one would go, its
    values become the existing ones. The JSON is only read — ``apply_plan``
    writes the TOML and leaves the old file alone, so a migration is reversible
    by deleting one file.

    Args:
        home: the sluice home; defaults to ``$SLUICE_HOME`` or ``~/.sluice``.
        config_path: override the config location; defaults to
            ``<home>/config.toml``.

    Returns:
        The plan.
    """
    home = pathlib.Path(home).expanduser() if home else default_home()
    config_path = (
        pathlib.Path(config_path).expanduser()
        if config_path
        else home / DEFAULT_CONFIG_FILENAME
    )

    is_upgrade = config_path.is_file()
    migrated_from = None
    if not is_upgrade and config_path.suffix == ".toml":
        legacy = config_path.with_name(LEGACY_CONFIG_FILENAME)
        if legacy.is_file():
            migrated_from = legacy

    existing: dict = {}
    read_from = config_path if is_upgrade else migrated_from
    if read_from is not None:
        try:
            existing = parse_config_text(
                read_from.read_text(encoding="utf-8"),
                str(read_from),
                is_json=read_from.suffix == ".json",
            )
        except (OSError, ConfigError):
            # A corrupt config is treated as "nothing known yet" for planning, so
            # setup can still report what is needed. apply_plan refuses to write
            # over it (see below), so nothing is lost.
            existing = {}

    added: dict = {}
    blank: dict = {}
    needs_attention: list[ConfigField] = []
    for field in CONFIG_FIELDS:
        if field.name in existing:
            continue
        default = _default_for(field, home)
        if default is not None:
            added[field.name] = default
            continue
        # No default, so a human has to supply it. Write the key anyway, empty:
        # every key the operator must fill is then already in the file, in place,
        # spelled correctly. The alternative — listing key names in the terminal
        # and asking someone to type them into JSON — turns one typo into a key
        # that is silently missing rather than visibly empty.
        blank[field.name] = ""
        if field.required:
            needs_attention.append(field)

    known = {field.name for field in CONFIG_FIELDS}
    unknown = tuple(sorted(key for key in existing if key not in known))

    return SetupPlan(
        home=home,
        config_path=config_path,
        is_upgrade=is_upgrade,
        existing=existing,
        added_with_default=added,
        added_blank=blank,
        needs_attention=tuple(needs_attention),
        unknown_keys=unknown,
        migrated_from=migrated_from,
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
    source_path = plan.source_path
    if source_path is not None and not plan.existing:
        # Distinguishes "empty config" (fine) from "unparseable" (not). Checked
        # against whichever file the values were read from, so a migration is
        # refused on a broken config.json rather than starting the new TOML from
        # an empty dict and calling the old values gone.
        try:
            parse_config_text(
                source_path.read_text(encoding="utf-8"),
                str(source_path),
                is_json=source_path.suffix == ".json",
            )
        except (OSError, ConfigError) as unreadable:
            raise ValueError(
                f"{source_path} exists but could not be parsed "
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
        render_config_toml(merged, plan.unknown_keys), encoding="utf-8"
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
