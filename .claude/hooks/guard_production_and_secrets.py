#!/usr/bin/env python3
"""PreToolUse guard for the sluice repo.

Enforces two guardrails from HANDOFF.md §8 that are otherwise only prose inside
agent prompts:

1. `python-poller` is LIVE PRODUCTION and is never modified by sluice work. Its
   working tree is dirty and it is mid-flight on real runs. Reads are ALLOWED —
   Plan B ports its gates and must study the source — but any write, move,
   delete, or in-place edit is refused.

2. The secret files are never read, printed, or copied. Not by a tool call, not
   by a shell command, not "just to check the format".

Reads hook JSON on stdin; prints a PreToolUse deny decision as JSON and exits 0
when it refuses. Silence + exit 0 means allow.
"""

import json
import os
import re
import sys

# Every known location of the LIVE production orchestrator. This repo is worked on
# from two machines (HANDOFF §2) and the hook file is synced between them, so ALL
# known roots are checked on every host — a path that does not exist locally simply
# never matches, and that costs nothing. Hardcoding only the macOS path left CHTC —
# the host that actually runs production — completely unprotected.
#
# Override or extend with SLUICE_PROD_REPOS (os.pathsep-separated absolute paths).
PROD_REPOS = [
    "/Users/willgardner/Bioinformatics/mizzou_fastq_automated_download",  # macOS
    "/home/wkgardner/python-poller",                                      # CHTC (live)
]
if os.environ.get("SLUICE_PROD_REPOS"):
    PROD_REPOS += [
        p for p in os.environ["SLUICE_PROD_REPOS"].split(os.pathsep) if p.strip()
    ]

# Basenames that are secrets wherever they appear.
SECRET_BASENAMES = {"labkey_api.txt", "mizzou.txt"}

WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}

# Shell tokens that mutate the filesystem.
#
# The redirect clause deliberately requires a target that does NOT begin with
# `&`: `> out.txt` is a write, but `2>&1` and `>&2` are file-descriptor
# duplication and touch nothing on disk. Matching those blocked ordinary
# read-only inspection (`ls "$P/x" 2>&1`), which the Plan B port needs.
MUTATING = re.compile(
    r"(^|[;&|]\s*)(rm|mv|cp|dd|truncate|touch|mkdir|rmdir|chmod|chown|tee|ln)\b"
    r"|>{1,2}\s*[^&\s]"
    r"|\bsed\b[^|;&]*-i"
    r"|\bgit\b[^|;&]*\b(commit|add|checkout|reset|clean|restore|rm|mv|stash|apply)\b"
)


def deny(reason):
    json.dump(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        },
        sys.stdout,
    )
    sys.exit(0)


def real(path):
    try:
        return os.path.realpath(os.path.expanduser(path))
    except Exception:
        return path or ""


def under_prod(path):
    """True if `path` resolves inside any known production root."""
    p = real(path)
    for root in PROD_REPOS:
        r = real(root)
        if p == r or p.startswith(r + os.sep):
            return True
    return False


def prod_root_in(text):
    """The production root a shell command refers to, or None."""
    for root in PROD_REPOS:
        if root in text:
            return root
    return None


def is_secret(path):
    return os.path.basename(path or "") in SECRET_BASENAMES


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        sys.exit(0)  # never block on a malformed payload

    tool = payload.get("tool_name", "")
    ti = payload.get("tool_input") or {}

    # --- File-path tools -----------------------------------------------------
    path = ti.get("file_path") or ti.get("notebook_path") or ""
    if path:
        if tool in WRITE_TOOLS:
            if under_prod(path):
                deny(
                    f"BLOCKED: {path} is inside the LIVE production python-poller repo. "
                    f"HANDOFF.md §8: python-poller is never modified by sluice work. "
                    f"Reading it for the Plan B port is fine — writing is not."
                )
            if is_secret(path):
                deny(f"BLOCKED: {path} is a secret file. It is never written or modified.")
        if tool == "Read" and is_secret(path):
            deny(
                f"BLOCKED: {path} is a secret file (HANDOFF.md §8: never read or "
                f"displayed). If you need its shape, use the config key that points at "
                f"it, not its contents."
            )

    # --- Bash ----------------------------------------------------------------
    if tool == "Bash":
        cmd = ti.get("command", "") or ""

        for name in SECRET_BASENAMES:
            if name in cmd:
                deny(
                    f"BLOCKED: this command references the secret file {name!r}. "
                    f"HANDOFF.md §8 forbids reading, printing, or copying it."
                )

        root = prod_root_in(cmd)
        if root and MUTATING.search(cmd):
            deny(
                "BLOCKED: this command would mutate the LIVE production python-poller "
                f"repo ({root}). Read-only inspection is allowed; writes, moves, "
                "deletes, redirects, and git state changes are not."
            )

    sys.exit(0)


if __name__ == "__main__":
    main()
