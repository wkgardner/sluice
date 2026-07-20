#!/usr/bin/env python3
"""PostToolUse architecture guard for the sluice repo.

The one-way dependency `gates -> core` is what makes sluice a framework rather
than a tangle: delete every production gate and the core must still import and
pass its tests (design of record §2.5, §5).

`tests/test_dependency_arrows.py` runs in ~0.02s, so there is no reason to wait
for a build-verifier pass to learn you broke it. This fires on any edit under
`sluice/core/` and reports failure straight back to the model.

Exit 0 = fine (or not applicable). Exit 2 = the invariant broke.
"""

import json
import os
import subprocess
import sys

GUARD_TEST = "tests/test_dependency_arrows.py"


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

    try:
        result = subprocess.run(
            ["pixi", "run", "--", "pytest", GUARD_TEST, "-q"],
            cwd=project,
            capture_output=True,
            text=True,
            timeout=180,
        )
    except FileNotFoundError:
        sys.exit(0)  # pixi not on PATH — stay silent rather than cry wolf
    except subprocess.TimeoutExpired:
        print("arch guard: timed out running the dependency-arrows test", file=sys.stderr)
        sys.exit(2)

    if result.returncode != 0:
        tail = (result.stdout + result.stderr).strip().splitlines()[-20:]
        print(
            "ARCHITECTURE GUARD FAILED — the one-way dependency `gates -> core` is "
            "broken.\n"
            f"A module under sluice/core/ now imports sluice.gates, sluice.api, or "
            f"sluice.cli. Core must import with all three absent (design of record "
            f"§2.5, §5).\n\n" + "\n".join(tail),
            file=sys.stderr,
        )
        sys.exit(2)

    sys.exit(0)


if __name__ == "__main__":
    main()
