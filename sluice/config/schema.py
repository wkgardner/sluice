"""The declared config surface — what ``sluice setup`` knows how to create and upgrade.

**This is not load-time validation.** ``load_config`` deliberately does not check the key set;
every consumer reads what it needs by subscript at the point of use, so a missing key fails loudly
inside the component that needed it. This schema serves a different job: it is what lets setup
*generate* a config, *upgrade* one across a release, and explain each key to whoever is filling it
in.

**It cannot drift.** ``tests/test_config_schema.py`` extracts every ``config["key"]`` and
``config.get("key")`` from the source tree and asserts this schema covers them. A new key added to
a gate without a schema entry fails that test, so the two cannot silently diverge — the same
technique as the predicate-twin guard.

**Keys arrive with their feature.** Only what the code reads today is declared. Configuration for
unbuilt gates (the Globus pair, registration) lands with those gates, exactly as their migrations
and profile entries do. That is why this list is shorter than ``config.example.json``, which still
carries entries from the original design of record that nothing reads.
"""

import dataclasses
import enum

__all__ = [
    "CONFIG_FIELDS",
    "ConfigField",
    "FieldKind",
    "ProbeKind",
    "fields_by_group",
]


class FieldKind(enum.Enum):
    """How ``sluice setup`` should treat a field.

    The distinction that matters is who owns the path. ``MANAGED_PATH`` is
    sluice's own working state, so setup picks a sensible location under the
    sluice home and creates it. ``EXTERNAL_PATH`` points at something that already
    exists in the world — a mount, a reference database, a watch directory — so
    setup can only ask, never invent.
    """

    MANAGED_PATH = "managed_path"
    EXTERNAL_PATH = "external_path"
    SECRET_FILE = "secret_file"
    VALUE = "value"


class ProbeKind(enum.Enum):
    """How ``sluice doctor`` can verify a config key — its *verifiability*.

    Distinct from ``FieldKind``, which answers who supplies the value and whether
    ``sluice setup`` can invent it; the two deliberately do not line up (design
    spec §5). ``NONE`` means "nothing to verify **beyond** presence" — doctor
    still requires the key to be set, because nothing else does: ``load_config``
    declines to validate the key set and the schema guard only checks the source
    tree. That is why ``ConfigField.probe`` has no default.
    """

    PATH_EXISTS = "path_exists"  # a directory or file that must be there
    SECRET_READABLE = "secret_readable"  # a credential file: present and non-empty
    HOST_REACHABLE = "host_reachable"  # part of a connection to another machine
    ENDPOINT = "endpoint"  # names a service or a container within one
    TOOLCHAIN = "toolchain"  # a command run through a pinned environment
    NONE = "none"  # a tuning value; presence is the whole check


@dataclasses.dataclass(frozen=True)
class ConfigField:
    """One config key: what it is, who supplies it, and what it defaults to."""

    name: str
    kind: FieldKind
    group: str
    help: str
    # No default, deliberately (design spec D5): adding a config key must be
    # impossible without answering "can doctor check this?". Declared before the
    # defaulted fields below because a non-default field cannot follow a
    # defaulted one in a dataclass.
    probe: ProbeKind
    default: object = None
    # Relative to the sluice home, for MANAGED_PATH and SECRET_FILE fields.
    home_relative: str | None = None
    required: bool = True

    @property
    def is_operator_supplied(self) -> bool:
        """Whether a human has to provide this — setup cannot invent it."""
        return self.kind in (FieldKind.EXTERNAL_PATH, FieldKind.SECRET_FILE) or (
            self.kind is FieldKind.VALUE and self.default is None
        )


CONFIG_FIELDS: tuple[ConfigField, ...] = (
    # -- sluice's own state -------------------------------------------------
    ConfigField(
        "state_file",
        FieldKind.MANAGED_PATH,
        "sluice state",
        "The SQLite database holding all run state. sluice's source of truth.",
        probe=ProbeKind.PATH_EXISTS,
        home_relative="state/sluice_state.db",
    ),
    ConfigField(
        "events_file",
        FieldKind.MANAGED_PATH,
        "sluice state",
        "The rotating structured event log (one JSON object per line).",
        probe=ProbeKind.PATH_EXISTS,
        home_relative="logs/events.jsonl",
    ),
    ConfigField(
        "run_events_dir",
        FieldKind.MANAGED_PATH,
        "sluice state",
        "Per-run event logs, one file per run — the fastest way to read one run's history.",
        probe=ProbeKind.PATH_EXISTS,
        home_relative="logs/runs",
    ),
    ConfigField(
        "events_max_mb",
        FieldKind.VALUE,
        "sluice state",
        "Rotate the event log once it reaches this many megabytes.",
        probe=ProbeKind.NONE,
        default=50,
    ),
    ConfigField(
        "events_backup_count",
        FieldKind.VALUE,
        "sluice state",
        "How many rotated event logs to keep.",
        probe=ProbeKind.NONE,
        default=10,
    ),
    ConfigField(
        "poll_interval_minutes",
        FieldKind.VALUE,
        "sluice state",
        "How long the daemon sleeps between looks. Also the worst-case delay "
        "before an operator command takes effect.",
        probe=ProbeKind.NONE,
        default=10,
    ),
    # -- where the data comes from -----------------------------------------
    ConfigField(
        "watch_dir",
        FieldKind.EXTERNAL_PATH,
        "detection",
        "Local directory scanned for new runs. Used only in local-source mode.",
        probe=ProbeKind.PATH_EXISTS,
        required=False,
    ),
    ConfigField(
        "remote_user",
        FieldKind.VALUE,
        "detection",
        "SSH user on the sequencing server.",
        probe=ProbeKind.HOST_REACHABLE,
    ),
    ConfigField(
        "remote_host",
        FieldKind.VALUE,
        "detection",
        "Hostname of the sequencing server.",
        probe=ProbeKind.HOST_REACHABLE,
    ),
    ConfigField(
        "remote_source_path",
        FieldKind.VALUE,
        "detection",
        "Path on the sequencing server holding run directories. MUST END WITH A "
        "SLASH, and must point at the folder CONTAINING the run directories, not "
        "at a run or a delivery inside one. Relative paths are relative to the "
        "SSH user's home.",
        probe=ProbeKind.HOST_REACHABLE,
    ),
    ConfigField(
        "password_file",
        FieldKind.SECRET_FILE,
        "detection",
        "File containing the SSH password. Never stored in config itself.",
        probe=ProbeKind.SECRET_READABLE,
        home_relative="secrets/remote_password",
    ),
    ConfigField(
        "local_source_dir",
        FieldKind.EXTERNAL_PATH,
        "detection",
        "Local directory to transfer FROM instead of over SSH. Set this to "
        "rehearse the whole pipeline with no credentials at all; leave empty for "
        "normal operation.",
        probe=ProbeKind.PATH_EXISTS,
        required=False,
    ),
    # -- staging and disk ---------------------------------------------------
    ConfigField(
        "local_staging_dir",
        FieldKind.EXTERNAL_PATH,
        "storage",
        "Where run data is staged, keyed by experiment id. Usually the shared "
        "CephFS tree — large, and NOT where NVD launches from.",
        probe=ProbeKind.PATH_EXISTS,
    ),
    ConfigField(
        "disk_quota_root",
        FieldKind.EXTERNAL_PATH,
        "storage",
        "Directory the CephFS quota is set on. Read for free space before a "
        "download starts.",
        probe=ProbeKind.PATH_EXISTS,
    ),
    ConfigField(
        "disk_headroom_multiplier",
        FieldKind.VALUE,
        "storage",
        "Require this many times a run's size free before downloading it.",
        probe=ProbeKind.NONE,
        default=2,
    ),
    # -- download behaviour -------------------------------------------------
    ConfigField(
        "parallel_downloads",
        FieldKind.VALUE,
        "download",
        "Maximum simultaneous file transfers.",
        probe=ProbeKind.NONE,
        default=4,
    ),
    ConfigField(
        "download_wave_size",
        FieldKind.VALUE,
        "download",
        "How many transfers to add at a time while ramping up.",
        probe=ProbeKind.NONE,
        default=4,
    ),
    ConfigField(
        "download_ramp_delay_seconds",
        FieldKind.VALUE,
        "download",
        "Pause between ramp waves, to avoid tripping the server's connection limit.",
        probe=ProbeKind.NONE,
        default=2,
    ),
    # -- LabKey -------------------------------------------------------------
    ConfigField(
        "labkey_server",
        FieldKind.VALUE,
        "labkey",
        "LabKey server hostname.",
        probe=ProbeKind.ENDPOINT,
    ),
    ConfigField(
        "labkey_experiments_project",
        FieldKind.VALUE,
        "labkey",
        "LabKey project path that experiments are created under.",
        probe=ProbeKind.ENDPOINT,
    ),
    ConfigField(
        "labkey_schema",
        FieldKind.VALUE,
        "labkey",
        "LabKey schema holding the experiments list.",
        probe=ProbeKind.ENDPOINT,
        default="lists",
    ),
    ConfigField(
        "labkey_query",
        FieldKind.VALUE,
        "labkey",
        "LabKey query (list) name for experiments.",
        probe=ProbeKind.ENDPOINT,
    ),
    ConfigField(
        "labkey_api_key_file",
        FieldKind.SECRET_FILE,
        "labkey",
        "File containing the LabKey API key. Never stored in config itself.",
        probe=ProbeKind.SECRET_READABLE,
        home_relative="secrets/labkey_api_key",
    ),
    # -- NVD ----------------------------------------------------------------
    ConfigField(
        "nvd_repo_dir",
        FieldKind.EXTERNAL_PATH,
        "nvd",
        "The NVD repository checkout. Its pyproject.toml pins the tool version "
        "sluice runs, so keeping this on the intended ref is a deployment step.",
        probe=ProbeKind.PATH_EXISTS,
    ),
    ConfigField(
        "nvd_command",
        FieldKind.VALUE,
        "nvd",
        "The nvd executable name inside that environment.",
        probe=ProbeKind.TOOLCHAIN,
        default="nvd",
    ),
    ConfigField(
        "nvd_launch_base",
        FieldKind.MANAGED_PATH,
        "nvd",
        "Where Nextflow launches from. MUST be local disk, never the shared "
        "staging tree — on shared storage --resume silently does nothing and an "
        "interrupted run restarts from zero (errata D2).",
        probe=ProbeKind.PATH_EXISTS,
        home_relative="nvd-launch",
    ),
    ConfigField(
        "nvd_log_dir",
        FieldKind.MANAGED_PATH,
        "nvd",
        "Per-experiment NVD pipeline logs, appended across retries.",
        probe=ProbeKind.PATH_EXISTS,
        home_relative="logs/nvd",
    ),
    ConfigField(
        "nvd_results_base",
        FieldKind.EXTERNAL_PATH,
        "nvd",
        "Base directory for NVD results and work trees. Large.",
        probe=ProbeKind.PATH_EXISTS,
    ),
    ConfigField(
        "nvd_profile",
        FieldKind.VALUE,
        "nvd",
        "Nextflow profile to run under, if any. Leave empty for the default.",
        probe=ProbeKind.NONE,
        required=False,
    ),
    ConfigField(
        "nvd_blast_db",
        FieldKind.EXTERNAL_PATH,
        "nvd reference data",
        "Directory holding the BLAST database.",
        probe=ProbeKind.PATH_EXISTS,
    ),
    ConfigField(
        "nvd_blast_db_prefix",
        FieldKind.VALUE,
        "nvd reference data",
        "BLAST database file prefix.",
        probe=ProbeKind.NONE,
    ),
    ConfigField(
        "nvd_blast_db_version",
        FieldKind.VALUE,
        "nvd reference data",
        "BLAST database version, recorded in results for provenance.",
        probe=ProbeKind.NONE,
    ),
    ConfigField(
        "nvd_taxonomy_dir",
        FieldKind.EXTERNAL_PATH,
        "nvd reference data",
        "Directory holding the NCBI taxonomy dump.",
        probe=ProbeKind.PATH_EXISTS,
    ),
    ConfigField(
        "nvd_virus_index",
        FieldKind.EXTERNAL_PATH,
        "nvd reference data",
        "The virus index file.",
        probe=ProbeKind.PATH_EXISTS,
    ),
    ConfigField(
        "nvd_virus_index_version",
        FieldKind.VALUE,
        "nvd reference data",
        "Virus index version, recorded in results for provenance.",
        probe=ProbeKind.NONE,
    ),
    ConfigField(
        "nvd_cutoff_percent",
        FieldKind.VALUE,
        "nvd analysis",
        "Abundance cutoff below which hits are discarded.",
        probe=ProbeKind.NONE,
        default=0.001,
    ),
    ConfigField(
        "nvd_entropy",
        FieldKind.VALUE,
        "nvd analysis",
        "Entropy filter threshold.",
        probe=ProbeKind.NONE,
        default=0.9,
    ),
    ConfigField(
        "nvd_tax_stringency",
        FieldKind.VALUE,
        "nvd analysis",
        "Taxonomic assignment stringency.",
        probe=ProbeKind.NONE,
        default=0.7,
    ),
    ConfigField(
        "nvd_max_blast_targets",
        FieldKind.VALUE,
        "nvd analysis",
        "Maximum BLAST targets per query.",
        probe=ProbeKind.NONE,
        default=100,
    ),
    ConfigField(
        "nvd_dedup",
        FieldKind.VALUE,
        "nvd analysis",
        "Whether to deduplicate reads.",
        probe=ProbeKind.NONE,
        default=True,
    ),
    # -- Slack --------------------------------------------------------------
    ConfigField(
        "slack_enabled",
        FieldKind.VALUE,
        "slack",
        "Whether to post run notifications to Slack.",
        probe=ProbeKind.NONE,
        default=False,
    ),
    ConfigField(
        "slack_channel",
        FieldKind.VALUE,
        "slack",
        "Channel id to post into.",
        probe=ProbeKind.ENDPOINT,
        required=False,
    ),
    ConfigField(
        "slack_bot_token_file",
        FieldKind.SECRET_FILE,
        "slack",
        "File containing the Slack bot token. Never stored in config itself.",
        probe=ProbeKind.SECRET_READABLE,
        home_relative="secrets/slack_bot_token",
        required=False,
    ),
    ConfigField(
        "slack_user_id_file",
        FieldKind.SECRET_FILE,
        "slack",
        "File listing Slack user ids to @-mention when a run needs a human, one "
        "per line.",
        probe=ProbeKind.SECRET_READABLE,
        home_relative="secrets/slack_mentions",
        required=False,
    ),
)


def fields_by_group() -> dict[str, list[ConfigField]]:
    """The fields grouped for display, preserving declaration order within a group."""
    grouped: dict[str, list[ConfigField]] = {}
    for field in CONFIG_FIELDS:
        grouped.setdefault(field.group, []).append(field)
    return grouped
