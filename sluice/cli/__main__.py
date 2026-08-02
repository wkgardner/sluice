"""``python -m sluice.cli`` — the process entry point.

Kept separate from ``__init__`` so importing the CLI (for tests, or to reuse
``build_parser``) never has the side effect of running it.
"""

import sys

from sluice.cli import main

if __name__ == "__main__":
    sys.exit(main())
