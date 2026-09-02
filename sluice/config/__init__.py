"""Config loading.

Real infrastructure values live only in a gitignored ``config.toml``; the committed
``config.example.toml`` is placeholders only. A missing required key is a load-time error, never a
silent production fallback.

**This loader deliberately does not validate the key set.** It finds, reads, and parses — that is
all. Every consumer reads the keys it needs by subscript at the point of use: ``resolve_context``
reads ``local_staging_dir``, the download gate reads ``disk_quota_root``, and so on. A missing key
therefore fails loudly, naming itself, inside the component that actually needed it.

A central required-keys list here would be a second authority over which keys matter, and it would
drift from the code that reads them the first time a gate is added. Validating that a whole
deployment is *workable* is a different job — it belongs in ``sluice doctor`` (errata E2), which
checks that configured values resolve and are reachable, not merely that they are present.

## TOML, and why tables are flattened away

The config is TOML so it can carry section headers and a one-line explanation above every key —
the file an operator edits should say what each key is for, and strict JSON cannot hold a comment.
Reading is ``tomllib`` from the standard library; writing is ``sluice.config.setup``'s own emitter,
so neither direction costs a dependency.

**Tables are organisation, not structure.** ``[nvd]`` exists to group keys visually and is
discarded at load: ``nvd_repo_dir`` under ``[nvd]`` arrives as ``config["nvd_repo_dir"]``, exactly
the name every gate already subscripts and exactly the name ``sluice doctor`` prints. The
alternative — nesting for real, or stripping the group prefix — would mean doctor naming a key the
operator cannot find by searching the file for it. A key is therefore free to sit in any table, or
none; only its own name matters.

## The JSON predecessor

Installs created before the TOML move have a ``config.json``. ``resolve_config_path`` still finds
one when no ``config.toml`` sits beside it, and ``load_config`` parses either by suffix, so an
upgrade does not break every command before the operator gets to ``sluice setup``. Setup writes the
TOML and leaves the JSON where it is.
"""

import json
import os
import pathlib
import tomllib

__all__ = [
    "CONFIG_ENV_VAR",
    "DEFAULT_CONFIG_FILENAME",
    "DEFAULT_HOME",
    "HOME_ENV_VAR",
    "LEGACY_CONFIG_FILENAME",
    "ConfigError",
    "default_home",
    "flatten_tables",
    "is_unfilled_placeholder",
    "load_config",
    "parse_config_text",
    "resolve_config_path",
]

# Point every sluice command at one config without repeating --config.
CONFIG_ENV_VAR = "SLUICE_CONFIG"

# Looked for in the working directory when neither --config nor the env var is
# given. A named constant, never derived from the package's install location —
# deriving paths from where the code happens to live is the errata A7/D2 trap.
DEFAULT_CONFIG_FILENAME = "config.toml"

# What installs created before the TOML move are called. Still found and still
# parsed, so upgrading sluice does not break every command at once.
LEGACY_CONFIG_FILENAME = "config.json"

# The directory `sluice setup` creates and owns. It lives here rather than in
# `setup`, because it is not setup's private business: `resolve_config_path` has
# to find the config setup wrote, and a second copy of this rule would let the
# writer and the reader disagree about where the config lives — which is exactly
# the bug that shipped when only setup knew.
HOME_ENV_VAR = "SLUICE_HOME"
DEFAULT_HOME = "~/.sluice"


def default_home() -> pathlib.Path:
    """The sluice home, honouring ``$SLUICE_HOME`` then falling back to ``~/.sluice``."""
    from_environment = os.environ.get(HOME_ENV_VAR)
    if from_environment:
        return pathlib.Path(from_environment).expanduser()
    return pathlib.Path(DEFAULT_HOME).expanduser()


class ConfigError(RuntimeError):
    """The config could not be found, read, or parsed.

    One error type for every load-time failure, so the CLI can turn any of them
    into a single clear sentence rather than leaking a traceback at an operator.
    """


def is_unfilled_placeholder(value) -> bool:
    """Whether ``value`` is a ``<key_name>`` marker nobody has filled in.

    ``config.example.json`` carries one per operator-supplied key, so an install
    that started from the example rather than from ``sluice setup`` arrives with
    them intact. A placeholder is a **live, broken value, not an absent one** —
    every consumer here tests truthiness, so ``<nvd_profile>`` is passed to the
    NVD tool as ``--profile '<nvd_profile>'`` and ``<local_source_dir>`` is swept
    as a directory of that literal name. One home, so a caller asking "is this
    configured?" cannot answer it with ``bool(value)`` and be quietly wrong.
    """
    return isinstance(value, str) and value.startswith("<") and value.endswith(">")


def _config_in(directory: pathlib.Path):
    """An existing config in ``directory``, TOML first, or ``None``.

    The legacy fallback is deliberately per-directory rather than global: an
    operator who has run setup has a ``config.toml`` in the home and possibly a
    stale ``config.json`` beside it, and the TOML must win there without a
    ``config.json`` in the *working directory* being ignored.
    """
    for filename in (DEFAULT_CONFIG_FILENAME, LEGACY_CONFIG_FILENAME):
        candidate = directory / filename
        if candidate.is_file():
            return candidate
    return None


def resolve_config_path(explicit_path=None) -> pathlib.Path:
    """Decide which config file to load, in precedence order.

    ``--config`` beats ``$SLUICE_CONFIG`` beats ``./config.toml`` beats
    ``<sluice home>/config.toml``, with a ``config.json`` accepted in either
    directory when no ``config.toml`` sits beside it — an install that predates
    the TOML move keeps working until its next ``sluice setup``.

    The home step is what makes a normal install work: ``sluice setup`` writes
    the config into the sluice home, so a resolver that stopped at the working
    directory sent every command looking somewhere setup never writes. The
    working directory still wins when a config is actually there, which keeps a
    repo checkout working without ``--config``.

    When nothing exists anywhere, the home's ``config.toml`` is returned even
    though it is absent, so the caller can report *which* path was tried. "No
    config at /etc/sluice/config.toml" is actionable in a way that "config not
    found" is not, and loading a different config than intended is the most
    common real-world config failure.

    Args:
        explicit_path: the ``--config`` value, or ``None``.

    Returns:
        The path that would be loaded.
    """
    if explicit_path:
        return pathlib.Path(explicit_path)
    from_environment = os.environ.get(CONFIG_ENV_VAR)
    if from_environment:
        return pathlib.Path(from_environment)
    in_working_directory = _config_in(pathlib.Path())
    if in_working_directory is not None:
        return in_working_directory
    home = default_home()
    return _config_in(home) or home / DEFAULT_CONFIG_FILENAME


def flatten_tables(parsed: dict, source="the config") -> dict:
    """Merge every TOML table's keys up to the top level, discarding table names.

    Tables group keys for a human reading the file; the code reads flat names.
    ``[nvd] nvd_repo_dir`` and a bare top-level ``nvd_repo_dir`` are therefore the
    same key, which is what lets an operator move a key between sections — or
    ignore sections entirely — without breaking anything.

    Raises:
        ConfigError: the same key appears in two places. Silently letting one win
            would make the file say two things and the loader believe one, with
            no way to tell which from the outside.
    """
    flattened: dict = {}
    homes: dict[str, str] = {}

    def absorb(mapping: dict, table: str) -> None:
        for key, value in mapping.items():
            if isinstance(value, dict):
                absorb(value, key)
                continue
            if key in flattened:
                raise ConfigError(
                    f"{source} sets '{key}' twice — once under "
                    f"[{homes[key]}] and again under [{table}]. Delete one; "
                    f"table names are ignored, so both are the same setting."
                )
            flattened[key] = value
            homes[key] = table

    absorb(parsed, "the top level")
    return flattened


def parse_config_text(raw_text: str, source="the config", *, is_json=False) -> dict:
    """Parse config text as TOML (or JSON), returning flat keys.

    Split out from ``load_config`` because ``sluice setup`` reads an existing
    config through the same two parsers, and a second copy of "which suffix means
    which parser" is exactly how a writer and a reader come to disagree.

    Args:
        raw_text: the file's contents.
        source: what to name in an error message — a path, usually.
        is_json: parse as JSON rather than TOML. For pre-TOML installs.

    Raises:
        ConfigError: the text does not parse, is not a table/object, or sets one
            key twice.
    """
    if is_json:
        try:
            parsed = json.loads(raw_text)
        except json.JSONDecodeError as invalid:
            raise ConfigError(f"{source} is not valid JSON: {invalid}") from invalid
        if not isinstance(parsed, dict):
            raise ConfigError(
                f"{source} must contain a JSON object of config keys, got "
                f"{type(parsed).__name__}"
            )
        return parsed

    try:
        parsed = tomllib.loads(raw_text)
    except tomllib.TOMLDecodeError as invalid:
        raise ConfigError(f"{source} is not valid TOML: {invalid}") from invalid
    return flatten_tables(parsed, source)


def load_config(explicit_path=None) -> dict:
    """Load and parse the config file, or raise ``ConfigError`` naming the path.

    TOML, unless the path ends ``.json`` — an install that predates the TOML move
    keeps working until its next ``sluice setup``. Either way the result is flat:
    see ``flatten_tables``.

    Args:
        explicit_path: an explicit path to load; otherwise resolved by
            ``resolve_config_path``.

    Returns:
        The parsed config as a plain dict of flat keys.

    Raises:
        ConfigError: the file is absent, unreadable, does not parse, or sets a
            key twice. Every message names the path.
    """
    config_path = resolve_config_path(explicit_path)
    try:
        raw_text = config_path.read_text(encoding="utf-8")
    except FileNotFoundError as missing:
        raise ConfigError(
            f"no config file at {config_path} — run `sluice setup` to create "
            f"one, or point at an existing config with --config PATH or "
            f"${CONFIG_ENV_VAR}"
        ) from missing
    except OSError as unreadable:
        raise ConfigError(f"cannot read {config_path}: {unreadable}") from unreadable

    return parse_config_text(
        raw_text, str(config_path), is_json=config_path.suffix == ".json"
    )
