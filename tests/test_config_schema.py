"""Guards and behaviour for the config schema and ``sluice setup`` (plan 15 increment 8).

The first test here is the load-bearing one: it extracts every config key the source tree actually
reads and asserts the schema declares it. Without that, the schema is a second description of the
config that drifts from the code the first time a gate adds a key — the same failure mode the
predicate-twin guard exists to prevent.
"""

import ast
import json
import pathlib

import pytest

from sluice.config.schema import CONFIG_FIELDS, FieldKind, ProbeKind
from sluice.config.setup import apply_plan, plan_setup

SOURCE_ROOT = pathlib.Path(__file__).resolve().parent.parent / "sluice"

# Any name whose subscript or .get() counts as a config read. `config` is the
# parameter name used throughout; the others are what it gets bound to locally.
_CONFIG_NAMES = {"config", "_config", "self._config"}


def _config_reads_in(tree) -> set[str]:
    """Every literal key read off a config-shaped name in one parsed module."""

    def _is_config(node) -> bool:
        if isinstance(node, ast.Name):
            return node.id in _CONFIG_NAMES
        if isinstance(node, ast.Attribute):
            return f"self.{node.attr}" in _CONFIG_NAMES
        return False

    found: set[str] = set()
    for node in ast.walk(tree):
        # config["key"]
        if (
            isinstance(node, ast.Subscript)
            and _is_config(node.value)
            and isinstance(node.slice, ast.Constant)
            and isinstance(node.slice.value, str)
        ):
            found.add(node.slice.value)
        # config.get("key")
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and _is_config(node.func.value)
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            found.add(node.args[0].value)
    return found


def keys_read_by_the_code() -> set[str]:
    """Every config key the source tree actually reads.

    Parsed with ``ast`` rather than matched with a regex, so a ``config["key"]``
    appearing inside a docstring or comment — as it does in the schema's own
    documentation — is not mistaken for a real read. The same technique the
    import guards use.
    """
    found: set[str] = set()
    for source_file in SOURCE_ROOT.rglob("*.py"):
        tree = ast.parse(source_file.read_text(encoding="utf-8"))
        found |= _config_reads_in(tree)
    return found


def test_the_schema_declares_every_key_the_code_reads():
    """No config key is read without being declared in the schema.

    THE anti-drift guard. ``sluice setup`` generates and upgrades installations
    from this schema, so a key the code needs but the schema omits produces a
    config that looks complete and fails at runtime — on the machine, not here.

    Adding a key to a gate therefore requires adding it to the schema, which is
    exactly the coupling that keeps setup honest.
    """
    declared = {field.name for field in CONFIG_FIELDS}
    undeclared = keys_read_by_the_code() - declared

    assert undeclared == set(), (
        f"these config keys are read by the code but missing from the schema: "
        f"{sorted(undeclared)} — sluice setup would generate a config without "
        "them, and the failure would surface on the deployed machine"
    )


def test_the_schema_declares_nothing_the_code_ignores():
    """The schema has no keys the code never reads.

    The other direction. A schema entry nothing reads is a prompt asking an
    operator to supply something that has no effect — worse than useless, because
    it implies the value matters.
    """
    declared = {field.name for field in CONFIG_FIELDS}
    unused = declared - keys_read_by_the_code()

    assert unused == set(), (
        f"these schema keys are never read by any code: {sorted(unused)} — either "
        "wire them up or drop them; asking an operator for a value that does "
        "nothing is worse than not asking"
    )


def test_every_config_field_declares_a_probe():
    """No config key exists without an answer to "is this checkable?".

    ``probe`` deliberately has no default (design spec D5): a defaulted field
    forgets by construction, and forgetting is the exact drift this whole
    mechanism exists to prevent. This test is belt-and-braces — a missing
    ``probe=`` is already a TypeError at import — but it states the rule where
    someone tempted to add ``= ProbeKind.NONE`` as a default will read it.
    """
    for field in CONFIG_FIELDS:
        assert isinstance(field.probe, ProbeKind), field.name


def test_tuning_values_are_not_probed_and_paths_are():
    """The classification is not uniform — it distinguishes what can be verified.

    A guard against a lazy bulk edit that stamps every key ``NONE`` (which would
    silently disable doctor) or every key ``PATH_EXISTS`` (which would make it
    fail on every install). Names one key of each kind so the assertion says
    what it means.
    """
    by_name = {field.name: field.probe for field in CONFIG_FIELDS}
    assert by_name["nvd_entropy"] is ProbeKind.NONE
    assert by_name["nvd_repo_dir"] is ProbeKind.PATH_EXISTS
    assert by_name["labkey_api_key_file"] is ProbeKind.SECRET_READABLE
    assert by_name["remote_host"] is ProbeKind.HOST_REACHABLE
    assert by_name["nvd_command"] is ProbeKind.TOOLCHAIN
    assert by_name["labkey_server"] is ProbeKind.ENDPOINT


def test_every_field_can_explain_itself():
    """Each field carries help text a non-author can act on."""
    for field in CONFIG_FIELDS:
        assert field.help.strip(), f"{field.name} has no help text"
        assert field.help.strip().endswith("."), (
            f"{field.name}'s help should read as a sentence"
        )


# ---------------------------------------------------------------------------
# Fresh install.
# ---------------------------------------------------------------------------


def test_a_fresh_install_creates_the_home_layout(tmp_path):
    """Setup builds the directories sluice owns and writes a config."""
    result = apply_plan(plan_setup(home=tmp_path / "sluice-home"))

    home = tmp_path / "sluice-home"
    assert (home / "config.json").is_file()
    for expected in ("secrets", "state", "logs", "nvd-launch"):
        assert (home / expected).is_dir(), f"{expected} was not created"
    assert result.database_initialised


def test_managed_paths_default_under_the_home(tmp_path):
    """Sluice's own state defaults into the home; the operator supplies nothing."""
    plan = plan_setup(home=tmp_path / "h")
    config = plan.merged

    assert config["state_file"].startswith(str(tmp_path / "h"))
    assert config["nvd_launch_base"].startswith(str(tmp_path / "h"))
    assert config["nvd_log_dir"].startswith(str(tmp_path / "h"))


def test_the_nvd_launch_default_is_not_inside_the_staging_tree(tmp_path):
    """The default launch base cannot trip the errata D2 guard.

    The home directory is local disk on every host sluice runs on, and the staging
    tree is shared storage supplied separately — so the default is correct rather
    than merely convenient. A default that landed on staging would make --resume
    silently do nothing.
    """
    from sluice.gates.nvd import resolve_launch_dir

    config = dict(plan_setup(home=tmp_path / "h").merged)
    config["local_staging_dir"] = str(tmp_path / "shared" / "staging")

    resolve_launch_dir(config, 4471)  # must not raise


def test_secret_files_are_created_empty_and_owner_only(tmp_path):
    """Credential files exist as empty placeholders with 0600.

    Empty rather than fabricated: the path is obviously real, and nothing can
    mistake the file for a working credential.
    """
    result = apply_plan(plan_setup(home=tmp_path / "h"))

    assert result.created_secret_placeholders
    for secret_path in result.created_secret_placeholders:
        assert secret_path.read_text() == ""
        assert oct(secret_path.stat().st_mode)[-3:] == "600"
    assert oct((tmp_path / "h" / "secrets").stat().st_mode)[-3:] == "700"


def test_the_config_file_is_owner_only(tmp_path):
    """config.json is 0600 — it names credential paths and infrastructure."""
    apply_plan(plan_setup(home=tmp_path / "h"))

    assert oct((tmp_path / "h" / "config.json").stat().st_mode)[-3:] == "600"


def test_operator_supplied_keys_are_reported_not_invented(tmp_path):
    """Keys only a human can supply are listed, never guessed at.

    Setup can create sluice's own directories, but it cannot invent where the
    sequencing server is or where the reference data lives. Inventing a plausible
    path would be worse than leaving it blank.
    """
    plan = plan_setup(home=tmp_path / "h")
    needs = {field.name for field in plan.needs_attention}

    assert "remote_host" in needs
    assert "local_staging_dir" in needs
    assert "nvd_repo_dir" in needs
    assert "state_file" not in needs, "sluice owns its own database path"


# ---------------------------------------------------------------------------
# Upgrade — the re-run case.
# ---------------------------------------------------------------------------


def test_re_running_never_overwrites_an_existing_value(tmp_path):
    """A tuned value survives an upgrade untouched.

    The rule that makes re-running safe. A setup that reset ``parallel_downloads``
    to its default on every release would be unusable exactly when it is most
    needed — during an upgrade, on a system someone has tuned.
    """
    home = tmp_path / "h"
    apply_plan(plan_setup(home=home))
    config_path = home / "config.json"
    tuned = json.loads(config_path.read_text())
    tuned["parallel_downloads"] = 16
    tuned["remote_host"] = "seq.example.edu"
    config_path.write_text(json.dumps(tuned), encoding="utf-8")

    apply_plan(plan_setup(home=home))

    after = json.loads(config_path.read_text())
    assert after["parallel_downloads"] == 16
    assert after["remote_host"] == "seq.example.edu"


def test_an_upgrade_adds_only_the_missing_keys(tmp_path):
    """A key introduced by a new release is added; everything else is left alone."""
    home = tmp_path / "h"
    apply_plan(plan_setup(home=home))
    config_path = home / "config.json"
    without_new_key = json.loads(config_path.read_text())
    del without_new_key["nvd_launch_base"]
    config_path.write_text(json.dumps(without_new_key), encoding="utf-8")

    plan = plan_setup(home=home)

    assert plan.is_upgrade
    assert set(plan.added_with_default) == {"nvd_launch_base"}


def test_keys_this_version_does_not_know_are_kept_and_reported(tmp_path):
    """An unrecognised key is preserved, not deleted, and surfaced.

    A downgrade, or a key from a feature branch, must not lose the operator's
    value. Reporting it means they find out; keeping it means nothing is
    destroyed if they were right.
    """
    home = tmp_path / "h"
    apply_plan(plan_setup(home=home))
    config_path = home / "config.json"
    config = json.loads(config_path.read_text())
    config["globus_destination_root"] = "/somewhere"
    config_path.write_text(json.dumps(config), encoding="utf-8")

    plan = plan_setup(home=home)
    apply_plan(plan)

    assert "globus_destination_root" in plan.unknown_keys
    assert (
        json.loads(config_path.read_text())["globus_destination_root"] == "/somewhere"
    )


def test_an_unparseable_config_is_refused_rather_than_clobbered(tmp_path):
    """Setup will not overwrite a config it could not read.

    The operator's values might still be in there. Clobbering the file to "fix" a
    syntax error would destroy exactly what they need recovered.
    """
    home = tmp_path / "h"
    home.mkdir()
    (home / "config.json").write_text("{ this is not json", encoding="utf-8")

    with pytest.raises(ValueError, match="Refusing to overwrite"):
        apply_plan(plan_setup(home=home))

    assert "not json" in (home / "config.json").read_text()


def test_re_running_is_idempotent(tmp_path):
    """Running setup twice in a row changes nothing the second time."""
    home = tmp_path / "h"
    apply_plan(plan_setup(home=home))
    first = (home / "config.json").read_text()

    apply_plan(plan_setup(home=home))

    assert (home / "config.json").read_text() == first


def test_a_filled_in_secret_is_not_emptied_by_a_re_run(tmp_path):
    """An upgrade never truncates a credential file someone already filled in."""
    home = tmp_path / "h"
    apply_plan(plan_setup(home=home))
    token_file = home / "secrets" / "slack_bot_token"
    token_file.write_text("xoxb-not-a-real-token", encoding="utf-8")

    apply_plan(plan_setup(home=home))

    assert token_file.read_text() == "xoxb-not-a-real-token"


def test_the_written_config_loads_through_the_normal_loader(tmp_path):
    """What setup writes is what ``load_config`` reads — no separate format."""
    from sluice.config import load_config

    apply_plan(plan_setup(home=tmp_path / "h"))

    loaded = load_config(str(tmp_path / "h" / "config.json"))
    assert loaded["state_file"].endswith("sluice_state.db")


def test_field_kinds_are_used_consistently():
    """Every managed path and secret file declares where it lives under the home."""
    for field in CONFIG_FIELDS:
        if field.kind in (FieldKind.MANAGED_PATH, FieldKind.SECRET_FILE):
            assert field.home_relative, (
                f"{field.name} is managed by sluice but declares no location "
                "under the home, so setup cannot place it"
            )
        else:
            assert field.home_relative is None, (
                f"{field.name} is operator-supplied but declares a home location"
            )


def test_the_example_config_matches_the_schema():
    """``config.example.json`` names exactly the schema's keys.

    The example is what someone reads to understand the config surface before
    they ever run setup. Before this guard it carried 34 keys nothing read —
    leftovers from the original design of record — which would have sent an
    operator configuring values that have no effect.
    """
    example_path = SOURCE_ROOT.parent / "config.example.json"
    example_keys = set(json.loads(example_path.read_text(encoding="utf-8")))

    assert example_keys == {field.name for field in CONFIG_FIELDS}


def test_setup_writes_every_key_including_the_ones_it_cannot_fill(tmp_path):
    """The written config contains EVERY schema key, not just the fillable ones.

    Setup used to list operator-supplied keys in the terminal and leave them out
    of the file, so getting an install running meant transcribing key names from
    a scrollback into JSON by hand. A typo there produces a key that is silently
    absent rather than visibly empty — and an absent key is the harder failure to
    see, both for the operator and for `sluice doctor`.

    Values are still never invented: the keys arrive empty, which is what the
    report and doctor both key off.
    """
    home = tmp_path / "h"
    apply_plan(plan_setup(home=home))

    written = json.loads((home / "config.json").read_text(encoding="utf-8"))

    missing = sorted({field.name for field in CONFIG_FIELDS} - set(written))
    assert not missing, f"setup left these keys out of the config: {missing}"

    # The keys a human must supply are present AND empty — present so they can be
    # edited in place, empty so nothing mistakes a placeholder for a real value.
    for field in CONFIG_FIELDS:
        if field.required and field.default is None and field.home_relative is None:
            assert written[field.name] == "", field.name
