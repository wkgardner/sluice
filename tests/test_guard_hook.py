"""The PreToolUse guardrail hook must keep protecting live production.

`.claude/hooks/guard_production_and_secrets.py` is the mechanical enforcement of
two HANDOFF.md §8 guardrails: python-poller is never modified, and the secret
files are never read. A silent regression here does not fail loudly — it just
quietly stops protecting a live system, which is the worst failure mode
available. So the hook gets a real test suite.

Both production roots are asserted on every host. The hook file is synced
between the macOS and CHTC working copies (HANDOFF §2), so it must protect
whichever machine it lands on — CI runs linux and therefore covers the CHTC
path even though these tests also run on macOS.

The guard is deliberately asymmetric: **reads of python-poller are ALLOWED**
(the Plan B port depends on studying the source) while **any write is DENIED**.
Both halves are tested; losing the allow half would block the port, losing the
deny half would endanger production.
"""

import json
import pathlib
import subprocess
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
GUARD = REPO_ROOT / ".claude" / "hooks" / "guard_production_and_secrets.py"

MAC_PROD = "/Users/willgardner/Bioinformatics/mizzou_fastq_automated_download"
CHTC_PROD = "/home/wkgardner/python-poller"
PROD_ROOTS = [MAC_PROD, CHTC_PROD]

# Assembled at runtime so the literal secret filenames never sit in a shell
# command line — the live guard blocks its own test harness otherwise.
SECRET_A = "labkey_api" + ".txt"
SECRET_B = "mizzou" + ".txt"


def verdict(payload):
    """Run the hook and report DENY (it emitted a decision) or ALLOW (silence)."""
    result = subprocess.run(
        [sys.executable, str(GUARD)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"guard crashed: {result.stderr}"
    if not result.stdout.strip():
        return "ALLOW"
    decision = json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"]
    return "DENY" if decision == "deny" else "ALLOW"


def bash(command):
    return {"tool_name": "Bash", "tool_input": {"command": command}}


def file_tool(name, path):
    return {"tool_name": name, "tool_input": {"file_path": path}}


def test_guard_exists():
    assert GUARD.is_file(), f"guardrail hook missing at {GUARD}"


# --- writes into production are refused, on every known root -----------------


@pytest.mark.parametrize("prod", PROD_ROOTS)
@pytest.mark.parametrize("tool", ["Write", "Edit", "NotebookEdit"])
def test_writes_into_production_are_denied(prod, tool):
    key = "notebook_path" if tool == "NotebookEdit" else "file_path"
    assert (
        verdict({"tool_name": tool, "tool_input": {key: f"{prod}/poller.py"}}) == "DENY"
    )


@pytest.mark.parametrize("prod", PROD_ROOTS)
@pytest.mark.parametrize(
    "template",
    [
        "rm -rf {p}/data",
        "mv {p}/a {p}/b",
        "cp /tmp/x {p}/x",
        "echo x > {p}/poller.py",
        "echo x >> {p}/poller.py",
        "sed -i s/a/b/ {p}/poller.py",
        "git -C {p} commit -m x",
        "git -C {p} checkout main",
        "touch {p}/newfile",
        "chmod 777 {p}/poller.py",
    ],
)
def test_mutating_shell_commands_against_production_are_denied(prod, template):
    assert verdict(bash(template.format(p=prod))) == "DENY"


# --- but reading production stays possible: Plan B ports from it -------------


@pytest.mark.parametrize("prod", PROD_ROOTS)
@pytest.mark.parametrize(
    "template",
    [
        "ls -d {p} 2>&1",  # the `2>&1` false positive that once blocked this
        "cat {p}/poller.py 2>&1",
        "grep -n def {p}/poller.py",
        "find {p} -name '*.py'",
        "wc -l {p}/poller.py",
        "head -50 {p}/poller_state_db.py",
    ],
)
def test_read_only_inspection_of_production_is_allowed(prod, template):
    assert verdict(bash(template.format(p=prod))) == "ALLOW"


@pytest.mark.parametrize("prod", PROD_ROOTS)
def test_reading_production_source_is_allowed(prod):
    assert verdict(file_tool("Read", f"{prod}/poller.py")) == "ALLOW"


# --- secrets are refused wherever they live ---------------------------------


@pytest.mark.parametrize("secret", [SECRET_A, SECRET_B])
def test_reading_a_secret_file_is_denied(secret):
    assert verdict(file_tool("Read", f"{MAC_PROD}/data/{secret}")) == "DENY"
    assert verdict(file_tool("Read", f"/tmp/{secret}")) == "DENY"


@pytest.mark.parametrize("secret", [SECRET_A, SECRET_B])
def test_shell_commands_naming_a_secret_are_denied(secret):
    assert verdict(bash(f"wc -l ./data/{secret}")) == "DENY"


@pytest.mark.parametrize("secret", [SECRET_A, SECRET_B])
def test_writing_a_secret_file_is_denied(secret):
    assert verdict(file_tool("Write", f"./data/{secret}")) == "DENY"


# --- ordinary sluice work is never impeded ----------------------------------


@pytest.mark.parametrize(
    "sluice_root",
    ["/Users/willgardner/Bioinformatics/sluice", "/home/wkgardner/sluice"],
)
def test_ordinary_sluice_edits_are_allowed(sluice_root):
    assert verdict(file_tool("Edit", f"{sluice_root}/sluice/core/db.py")) == "ALLOW"
    assert verdict(file_tool("Write", f"{sluice_root}/tests/test_db.py")) == "ALLOW"


@pytest.mark.parametrize(
    "command",
    ["pixi run test", "git status", "echo hi > /tmp/scratch.txt", "ls -la"],
)
def test_ordinary_commands_are_allowed(command):
    assert verdict(bash(command)) == "ALLOW"


def test_malformed_payload_does_not_block():
    """A broken payload must never wedge the session."""
    result = subprocess.run(
        [sys.executable, str(GUARD)], input="not json", capture_output=True, text=True
    )
    assert result.returncode == 0
    assert not result.stdout.strip()
