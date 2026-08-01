#!/usr/bin/env python3
"""PostToolUse architecture guard for the sluice repo.

Guards the structural invariants that do not fail loudly on their own. Each is
a fast test file that runs on any edit under `sluice/core/`, so a break is
reported straight back to the model rather than waiting for a build-verifier
pass. Together they run in well under a second.

1. The one-way dependency `gates -> core` is what makes sluice a framework
   rather than a tangle: delete every production gate and the core must still
   import and pass its tests (design of record §2.5, §5).
2. The eligibility rule exists twice by necessity — as SQL in
   `db.ACTIVE_RUNS_WHERE` and as Python in `db.is_daemon_eligible`, because
   SQLite cannot call a Python function. Silent drift between the halves is the
   exact failure plan 14 set out to make impossible.

Adding a guard is one entry in `GUARD_TESTS`; the headline beside it is what
the model sees, so it should say what broke and why it matters.

Exit 0 = fine (or not applicable). Exit 2 = an invariant broke.
"""

import json
import os
import subprocess
import sys

# (test file, failure headline). Ordered cheapest-first; every entry runs on a
# core edit and the first failure reported wins.
GUARD_TESTS = (
    (
        "tests/test_dependency_arrows.py",
        "ARCHITECTURE GUARD FAILED — the one-way dependency `gates -> core` is "
        "broken.\n"
        "A module under sluice/core/ now imports sluice.gates, sluice.api, or "
        "sluice.cli. Core must import with all three absent (design of record "
        "§2.5, §5).",
    ),
    (
        "tests/test_predicate_twins.py",
        "PREDICATE TWIN GUARD FAILED — the two halves of the daemon-eligibility "
        "rule no longer agree.\n"
        "db.ACTIVE_RUNS_WHERE (SQL) and db.is_daemon_eligible (Python) encode "
        "the same rule and cannot be merged, so they must be edited together "
        "(plan 14). Change both, or neither.",
    ),
)


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        sys.exit(0)

    ti = payload.get("tool_input") or {}
    tr = payload.get("tool_response") or {}
    path = tr.get("filePath") or ti.get("file_path") or ""

    # Only care about core. Normalize separators so this works if a path arrives
    # relative or absolute.
    if "sluice/core/" not in path.replace(os.sep, "/"):
        sys.exit(0)

    project = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()

    for guard_test, headline in GUARD_TESTS:
        try:
            result = subprocess.run(
                ["pixi", "run", "--", "pytest", guard_test, "-q"],
                cwd=project,
                capture_output=True,
                text=True,
                timeout=180,
            )
        except FileNotFoundError:
            sys.exit(0)  # pixi not on PATH — stay silent rather than cry wolf
        except subprocess.TimeoutExpired:
            print(f"arch guard: timed out running {guard_test}", file=sys.stderr)
            sys.exit(2)

        if result.returncode != 0:
            tail = (result.stdout + result.stderr).strip().splitlines()[-20:]
            print(headline + "\n\n" + "\n".join(tail), file=sys.stderr)
            sys.exit(2)

    sys.exit(0)


if __name__ == "__main__":
    main()
