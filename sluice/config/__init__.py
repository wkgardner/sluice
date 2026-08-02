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
    "ConfigError",
    "load_config",
    "resolve_config_path",
]

# Point every sluice command at one config without repeating --config.
CONFIG_ENV_VAR = "SLUICE_CONFIG"

# Looked for in the working directory when neither --config nor the env var is
# given. A named constant, never derived from the package's install location —
# deriving paths from where the code happens to live is the errata A7/D2 trap.
DEFAULT_CONFIG_FILENAME = "config.json"


class ConfigError(RuntimeError):
    """The config could not be found, read, or parsed.

    One error type for every load-time failure, so the CLI can turn any of them
    into a single clear sentence rather than leaking a traceback at an operator.
    """


def resolve_config_path(explicit_path=None) -> pathlib.Path:
    """Decide which config file to load, in precedence order.

    ``--config`` beats ``$SLUICE_CONFIG`` beats ``./config.json``.

    The chosen path is returned whether or not it exists, so the caller can report
    *which* path was tried. "No config at /etc/sluice/config.json" is actionable in
    a way that "config not found" is not, and loading a different config than
    intended is the most common real-world config failure.

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
    return pathlib.Path(DEFAULT_CONFIG_FILENAME)


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
            f"no config file at {config_path} — pass --config PATH, set "
            f"${CONFIG_ENV_VAR}, or run from a directory containing "
            f"{DEFAULT_CONFIG_FILENAME}"
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
