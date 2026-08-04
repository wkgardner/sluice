"""Config loading.

Real infrastructure values live only in a gitignored ``config.json``; the committed
``config.example.json`` is placeholders only. A missing required key is a load-time error, never a
silent production fallback.

**This loader deliberately does not validate the key set.** It finds, reads, and parses — that is
all. Every consumer reads the keys it needs by subscript at the point of use: ``resolve_context``
reads ``local_staging_dir``, the download gate reads ``disk_quota_root``, and so on. A missing key
therefore fails loudly, naming itself, inside the component that actually needed it.

A central required-keys list here would be a second authority over which keys matter, and it would
drift from the code that reads them the first time a gate is added. Validating that a whole
deployment is *workable* is a different job — it belongs in ``sluice doctor`` (errata E2), which
checks that configured values resolve and are reachable, not merely that they are present.
"""

import json
import os
import pathlib

__all__ = [
    "CONFIG_ENV_VAR",
    "DEFAULT_CONFIG_FILENAME",
    "DEFAULT_HOME",
    "HOME_ENV_VAR",
    "ConfigError",
    "default_home",
    "is_unfilled_placeholder",
    "load_config",
    "resolve_config_path",
]

# Point every sluice command at one config without repeating --config.
CONFIG_ENV_VAR = "SLUICE_CONFIG"

# Looked for in the working directory when neither --config nor the env var is
# given. A named constant, never derived from the package's install location —
# deriving paths from where the code happens to live is the errata A7/D2 trap.
DEFAULT_CONFIG_FILENAME = "config.json"

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


def resolve_config_path(explicit_path=None) -> pathlib.Path:
    """Decide which config file to load, in precedence order.

    ``--config`` beats ``$SLUICE_CONFIG`` beats ``./config.json`` beats
    ``<sluice home>/config.json``.

    The last step is what makes a normal install work: ``sluice setup`` writes
    the config into the sluice home, so a resolver that stopped at the working
    directory sent every command looking somewhere setup never writes. The
    working directory still wins when a ``config.json`` is actually there, which
    keeps a repo checkout working without ``--config``; that step is therefore
    the one place here that tests for existence, since an unconditional return
    would make the home unreachable.

    Otherwise the chosen path is returned whether or not it exists, so the caller
    can report *which* path was tried. "No config at /etc/sluice/config.json" is
    actionable in a way that "config not found" is not, and loading a different
    config than intended is the most common real-world config failure.

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
    in_working_directory = pathlib.Path(DEFAULT_CONFIG_FILENAME)
    if in_working_directory.is_file():
        return in_working_directory
    return default_home() / DEFAULT_CONFIG_FILENAME


def load_config(explicit_path=None) -> dict:
    """Load and parse the config file, or raise ``ConfigError`` naming the path.

    Args:
        explicit_path: an explicit path to load; otherwise resolved by
            ``resolve_config_path``.

    Returns:
        The parsed config as a plain dict.

    Raises:
        ConfigError: the file is absent, unreadable, not valid JSON, or does not
            contain a JSON object. Every message names the path.
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

    try:
        parsed = json.loads(raw_text)
    except json.JSONDecodeError as invalid:
        raise ConfigError(f"{config_path} is not valid JSON: {invalid}") from invalid

    if not isinstance(parsed, dict):
        raise ConfigError(
            f"{config_path} must contain a JSON object of config keys, got "
            f"{type(parsed).__name__}"
        )
    return parsed
