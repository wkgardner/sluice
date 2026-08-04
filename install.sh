#!/usr/bin/env bash
#
# install.sh — put `sluice` on your PATH.
#
# sluice runs inside a pixi environment, so the real command is
# `pixi run --manifest-path <repo>/pixi.toml sluice ...`. Typing that is not
# reasonable, and every command in docs/OPERATING.md is written as plain
# `sluice ...`. This script installs a small wrapper that makes the documented
# form actually work, from any directory.
#
# Safe to re-run: it overwrites its own wrapper and nothing else. It never uses
# sudo, never writes outside the bin directory, and never touches your shell
# profile — if PATH needs changing it tells you the line to add and stops short
# of adding it.
#
#   ./install.sh                     install to ~/.local/bin
#   ./install.sh --bin-dir ~/bin     install somewhere else
#   ./install.sh --uninstall         remove the wrapper
#
set -euo pipefail

# The marker line identifies wrappers this script wrote, so a re-run can safely
# replace one while refusing to clobber an unrelated `sluice` someone else put
# there.
MARKER="# installed by sluice install.sh — safe to delete"

# Absolute path to this repo, derived from where THIS script lives rather than
# from the working directory, so `bash /path/to/sluice/install.sh` works from
# anywhere and the wrapper it writes points at the right checkout.
REPO_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

BIN_DIR="${SLUICE_BIN_DIR:-$HOME/.local/bin}"
UNINSTALL=0

while [ $# -gt 0 ]; do
    case "$1" in
        --bin-dir)
            [ $# -ge 2 ] || { echo "install.sh: --bin-dir needs a path" >&2; exit 2; }
            BIN_DIR="$2"
            shift 2
            ;;
        --uninstall)
            UNINSTALL=1
            shift
            ;;
        -h|--help)
            sed -n '3,20p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *)
            echo "install.sh: unknown option $1 (try --help)" >&2
            exit 2
            ;;
    esac
done

TARGET="$BIN_DIR/sluice"

if [ "$UNINSTALL" -eq 1 ]; then
    if [ ! -e "$TARGET" ]; then
        echo "Nothing to remove — no file at $TARGET"
        exit 0
    fi
    if ! grep -qF "$MARKER" "$TARGET" 2>/dev/null; then
        echo "install.sh: $TARGET was not written by this script — leaving it alone" >&2
        exit 1
    fi
    rm "$TARGET"
    echo "Removed $TARGET"
    exit 0
fi

# pixi is the whole mechanism; without it the wrapper would be a broken command
# that fails only when someone tries to use it.
if ! command -v pixi >/dev/null 2>&1; then
    cat >&2 <<'EOF'
install.sh: pixi is not installed, or not on your PATH.

sluice runs inside a pixi environment, so pixi has to be there first:

    curl -fsSL https://pixi.sh/install.sh | bash

Then open a new shell and run this script again.
EOF
    exit 1
fi

if [ ! -f "$REPO_ROOT/pixi.toml" ]; then
    echo "install.sh: no pixi.toml in $REPO_ROOT — is this the sluice repo?" >&2
    exit 1
fi

# Refuse to overwrite a `sluice` this script did not write. Somebody else's
# command silently replaced is a worse outcome than this script failing.
if [ -e "$TARGET" ] && ! grep -qF "$MARKER" "$TARGET" 2>/dev/null; then
    echo "install.sh: $TARGET already exists and was not written by this script." >&2
    echo "Move it aside, or choose another location with --bin-dir." >&2
    exit 1
fi

echo "Resolving the sluice environment (this can take a minute the first time)..."
pixi install --manifest-path "$REPO_ROOT/pixi.toml"

mkdir -p "$BIN_DIR"

cat > "$TARGET" <<EOF
#!/usr/bin/env bash
$MARKER
#
# Runs sluice from the checkout at:
#   $REPO_ROOT
# Delete this file to uninstall, or re-run that repo's install.sh to update it.
exec pixi run --manifest-path "$REPO_ROOT/pixi.toml" sluice "\$@"
EOF
chmod +x "$TARGET"

echo "Installed: $TARGET -> $REPO_ROOT"

# Verify the wrapper actually runs, rather than reporting success and leaving the
# first real failure for the operator to discover.
if ! "$TARGET" --help >/dev/null 2>&1; then
    echo >&2
    echo "install.sh: the wrapper was written but does not run correctly." >&2
    echo "Try it directly to see the error:" >&2
    echo "    pixi run --manifest-path \"$REPO_ROOT/pixi.toml\" sluice --help" >&2
    exit 1
fi
echo "Verified: the wrapper runs."

case ":$PATH:" in
    *":$BIN_DIR:"*)
        echo
        echo "You can now run sluice from anywhere. Next step:"
        echo
        echo "    sluice setup"
        echo
        ;;
    *)
        # Name the likely profile file, but do not edit it — editing someone's
        # shell profile without asking is not this script's business.
        case "${SHELL:-}" in
            */zsh)  PROFILE="~/.zshrc" ;;
            */bash) PROFILE="~/.bashrc" ;;
            *)      PROFILE="your shell profile" ;;
        esac
        echo
        echo "One more step — $BIN_DIR is not on your PATH."
        echo "Add this line to $PROFILE, then open a new terminal:"
        echo
        echo "    export PATH=\"$BIN_DIR:\$PATH\""
        echo
        echo "Until then, sluice still works by full path:"
        echo
        echo "    $TARGET setup"
        echo
        ;;
esac
