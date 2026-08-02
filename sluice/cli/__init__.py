"""``sluice`` — the operator command line.

A thin client over the control ops (``sluice.core.control``). It adds no policy of its own: every
command is one control op plus formatting, so anything the CLI can do is something the audited
control layer already sanctioned, and anything it refuses is a refusal that came from there.

**DEVIATION from the original Plan C shape, recorded on purpose (plan 15 §9).** This module's
first docstring said the CLI would be an HTTP client to ``sluice.api`` because "the api is the
authorized control-write path". This version talks to ``control`` **directly**. Two reasons:

1. *The property that mattered is preserved.* Auditing lives in ``control`` — every state-changing
   op writes its ``control_audit`` row inside its own transaction — not in the API. Going direct
   keeps every write attributed and audited exactly as before. What the API adds is **remote**
   access, not authorization.
2. *Remote access is not what is needed first.* The API means FastAPI, a lockfile change, a service
   to supervise, and a port to secure — before anyone can unstick a single run. An operator on the
   host the daemon runs on needs none of that.

The API remains the right answer for operating sluice from a laptop, and this CLI is structured so
that swapping its transport later touches only ``_run_op`` and ``_cmd_status``. Nothing else here
knows how the ops are reached.

**stdlib ``argparse``, deliberately.** Typer and rich would be prettier, but neither is a project
dependency yet and adding them means regenerating the lockfile. The CLI is the one thing that must
work on whatever host the daemon runs on, on the day it is needed, so it depends on nothing that is
not already installed.

## The mental model an operator needs

sluice is a **reconcile loop**, not a queue. Every command here changes *state* and returns
immediately; nothing is dispatched or executed by the CLI. The daemon notices on its next look — up
to one poll interval later — and acts then. So:

* ``sluice hold`` does not stop an in-flight gate. It stops the *next* one from starting.
* ``sluice clear-error`` does not retry anything. It makes the run eligible again, and the daemon
  picks it up on its next pass.
* Nothing here ever deletes a file (ADR-0005). Rewinds clear columns; operators own destructive
  filesystem actions.

## Exit codes

``0`` the op succeeded or was a no-op; ``1`` the op was refused, or the run or config was not
found; ``2`` a usage error (argparse's own convention). Refusals exit non-zero on purpose, so a
wrapper script cannot mistake "protected, refused" for "done".

## Attribution

Every state-changing op writes an audit row naming an **actor**, defaulting to the OS user and
overridable with ``--actor``. There is no anonymous path: the audit trail is the only record of who
did what, and an unattributed row is worthless evidence.
"""

import argparse
import contextlib
import getpass
import json
import sys

import structlog

from sluice.config import ConfigError, load_config
from sluice.core import control
from sluice.core.daemon import assemble_registry
from sluice.core.profile import REFERENCE_PROFILES
from sluice.gates.profiles import PRODUCTION_PROFILES

__all__ = ["build_parser", "main"]

EXIT_OK = 0
EXIT_REFUSED = 1

# Shared by every state-changing subcommand. The reconcile-loop model is the one
# thing an operator must understand, and --help is where they will look for it.
_STATE_ONLY_EPILOG = (
    "This changes stored state only. The daemon acts on its next look (up to one "
    "poll interval later); nothing is executed immediately."
)


@contextlib.contextmanager
def _logs_on_stderr():
    """Route structlog output to stderr for the duration of one command, then restore.

    The control ops emit their canonical ``control.finished`` event through
    structlog, whose default logger factory writes to **stdout**. In a daemon that
    is fine; in a CLI it corrupts the data channel — ``sluice --json status | jq``
    fails because a log line is interleaved with the JSON. Unix convention and
    practical need agree: stdout is the answer, stderr is the narration.

    **Restoring matters.** ``structlog.configure`` is process-global, so a ``main()``
    that configured and walked away would silently change logging for everything
    else in the process — the daemon, another library, the rest of a test session.
    A function callable in-process must leave global state as it found it, so the
    previous configuration is captured and put back unconditionally.
    """
    previous_configuration = structlog.get_config().copy()
    structlog.configure(logger_factory=structlog.PrintLoggerFactory(file=sys.stderr))
    try:
        yield
    finally:
        structlog.configure(**previous_configuration)


def _registry():
    """The full profile registry: reference profiles plus the production ones.

    Mirrors what the daemon assembles, so ``status`` resolves exactly the profiles
    the daemon would — a status board reporting against a different registry than
    the daemon uses would be worse than none. ``assemble_registry`` raises on a
    duplicate key rather than letting one definition silently shadow another.
    """
    return assemble_registry(REFERENCE_PROFILES, PRODUCTION_PROFILES)


def _default_actor() -> str:
    """The OS user, for the audit trail.

    Falls back to ``"unknown"`` only if the OS cannot say, which the audit writer
    then rejects loudly rather than writing an unattributed row.
    """
    try:
        return getpass.getuser()
    except Exception:
        return "unknown"


def _format_run_line(entry) -> str:
    """One status line for a run: name, experiment, outcome, lifecycle, any error.

    ``experiment_id`` sits next to the name because it is the key a human uses —
    it is what they read in LabKey and what names the run's directory on disk.
    """
    experiment = entry.get("experiment_id")
    experiment_label = f"exp {experiment}" if experiment is not None else "exp —"
    parts = [
        f"{entry['name']:<28}",
        f"{experiment_label:<10}",
        f"{entry.get('outcome', '?'):<12}",
        f"{entry.get('lifecycle', '?'):<8}",
    ]
    if entry.get("protected"):
        parts.append("PROTECTED")
    line = "  ".join(parts).rstrip()
    if entry.get("error_reason"):
        line += f"\n    error: {entry['error_reason']}"
    return line


def _print_overview(status) -> None:
    """Render the fleet overview: global flags first, then one line per run."""
    print(f"pipeline profile : {status.get('pipeline_profile')}")
    print(
        "daemon paused    : "
        f"{'YES — no run will advance' if status.get('paused') else 'no'}"
    )
    runs = status.get("runs", [])
    print(f"runs             : {len(runs)}")
    if not runs:
        print("\n(no runs detected yet)")
        return
    print()
    for entry in runs:
        print(_format_run_line(entry))
    quarantined = [entry for entry in runs if entry.get("error_reason")]
    if quarantined:
        # The one actionable summary: a quarantined run waits forever by design,
        # so the board must say so rather than leaving it to be noticed.
        print(
            f"\n{len(quarantined)} run(s) quarantined and waiting for an operator. "
            "Fix the cause, then: sluice clear-error <run>"
        )


def _print_run_detail(entry) -> None:
    """Render one run in full, including which gates are done and which are not."""
    print(f"run            : {entry['name']}")
    print(f"experiment id  : {entry.get('experiment_id')}")
    print(f"outcome        : {entry.get('outcome')}")
    print(f"lifecycle      : {entry.get('lifecycle')}")
    print(f"pipeline       : {entry.get('pipeline_version')}")
    print(f"detected       : {entry.get('detected_at')}")
    print(f"completed      : {entry.get('completed_at') or '—'}")
    print(f"protected      : {'yes' if entry.get('protected') else 'no'}")
    if entry.get("error_reason"):
        print(f"error          : {entry['error_reason']}")
    # Per-run anomalies are fields, not exceptions (status is total), so they have
    # to be printed or they are invisible.
    for anomaly in ("profile_error", "exclusions_error"):
        if entry.get(anomaly):
            print(f"{anomaly:<15}: {entry[anomaly]}")
    if entry.get("excluded_gates"):
        print(f"excluded gates : {', '.join(entry['excluded_gates'])}")
    gates = entry.get("gates")
    if gates:
        print(f"gates done     : {', '.join(gates.get('satisfied', [])) or '—'}")
        print(f"gates pending  : {', '.join(gates.get('unsatisfied', [])) or '—'}")


def _emit_result(result, as_json: bool) -> int:
    """Print a control op's outcome and return the process exit code."""
    if as_json:
        print(
            json.dumps(
                {
                    "action": result.action,
                    "actor": result.actor,
                    "run": result.target_run,
                    "status": result.status,
                    "message": result.message,
                    "changes": dict(result.changes),
                    "audit_id": result.audit_id,
                },
                indent=2,
                default=str,
            )
        )
        return EXIT_OK
    target = f" {result.target_run}" if result.target_run else ""
    if result.status == "noop":
        print(f"{result.action}{target}: nothing to do ({result.message})")
    else:
        print(f"{result.action}{target}: ok")
        for column, value in result.changes.items():
            print(f"    {column} -> {value}")
    print(f"    audited as #{result.audit_id}")
    return EXIT_OK


def _run_op(operation, args, *, needs_run: bool, **extra) -> int:
    """Call one control op and render it.

    The single place that knows how a control op is reached. Swapping this to an
    HTTP call is the whole of the future API migration.
    """
    config = load_config(args.config)
    positional = [config["state_file"], args.actor]
    if needs_run:
        positional.append(args.run)
    result = operation(*positional, reason=args.reason, **extra)
    return _emit_result(result, args.json)


def _cmd_status(args) -> int:
    """``sluice status`` — the fleet overview, or one run with ``--run``."""
    config = load_config(args.config)
    status = control.get_status(config["state_file"], args.run, registry=_registry())
    if args.json:
        print(json.dumps(status, indent=2, default=str))
        return EXIT_OK
    if args.run:
        _print_run_detail(status)
    else:
        _print_overview(status)
    return EXIT_OK


def _cmd_show(args) -> int:
    """``sluice show RUN`` — one run in full."""
    config = load_config(args.config)
    entry = control.get_status(config["state_file"], args.run, registry=_registry())
    if args.json:
        print(json.dumps(entry, indent=2, default=str))
        return EXIT_OK
    _print_run_detail(entry)
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser. Separate from ``main`` so tests can inspect it."""
    parser = argparse.ArgumentParser(
        prog="sluice",
        description=(
            "Operator control for the sluice sequencing pipeline. Commands change "
            "stored state; the daemon acts on its next look."
        ),
        epilog=(
            "Config is found via --config, then $SLUICE_CONFIG, then ./config.json. "
            "Every state change is audited against --actor (default: your OS user)."
        ),
    )
    parser.add_argument(
        "--config",
        help="path to config.json (default: $SLUICE_CONFIG, else ./config.json)",
    )
    parser.add_argument(
        "--actor",
        default=_default_actor(),
        help="who to record in the audit trail (default: your OS user)",
    )
    parser.add_argument(
        "--json", action="store_true", help="emit machine-readable JSON instead of text"
    )
    subcommands = parser.add_subparsers(dest="command", required=True)

    status_parser = subcommands.add_parser(
        "status",
        help="show every run and whether the daemon is paused",
        description=(
            "The board. Shows the active pipeline profile, whether the daemon is "
            "paused, and one line per run with its experiment id, outcome, and any "
            "quarantine reason."
        ),
    )
    status_parser.add_argument("--run", help="show just this run, in full")
    status_parser.set_defaults(handler=_cmd_status)

    show_parser = subcommands.add_parser(
        "show",
        help="show one run in full, including gate progress",
        description=(
            "Everything known about one run: lifecycle, quarantine reason, which "
            "gates are satisfied, which are still pending, and which are excluded."
        ),
    )
    show_parser.add_argument("run", help="the run name")
    show_parser.set_defaults(handler=_cmd_show)

    def _add_run_op(name, operation, help_text, description):
        """Register a subcommand targeting one run, backed by one control op."""
        subparser = subcommands.add_parser(
            name, help=help_text, description=description, epilog=_STATE_ONLY_EPILOG
        )
        subparser.add_argument("run", help="the run name")
        subparser.add_argument(
            "--reason",
            help="recorded in the audit trail — say why, for whoever reads it later",
        )
        subparser.set_defaults(
            handler=lambda args, operation=operation: _run_op(
                operation, args, needs_run=True
            )
        )
        return subparser

    _add_run_op(
        "hold",
        control.hold,
        "stop a run from advancing",
        (
            "Marks the run held, so the daemon stops advancing it. An in-flight "
            "gate finishes; the next one never starts. Reverse with 'unhold'."
        ),
    )
    _add_run_op(
        "unhold",
        control.unhold,
        "let a held run advance again",
        "Returns a held run to active. The daemon resumes it on its next look.",
    )
    _add_run_op(
        "unskip",
        control.unskip,
        "return a skipped run to active",
        "Reverses 'skip'. The daemon resumes the run on its next look.",
    )
    _add_run_op(
        "clear-error",
        control.clear_error,
        "forgive a quarantined run so it can retry",
        (
            "THE recovery command. A gate failure quarantines its run, and it stays "
            "quarantined until you run this — there is no automatic retry, by "
            "design. Fix the underlying cause FIRST, then clear: the daemon re-runs "
            "the failed gate on its next look, and gates resume rather than restart "
            "(a partly-downloaded run continues where it stopped). A run that is "
            "also held needs 'unhold' as well — the two are independent."
        ),
    )

    skip_parser = subcommands.add_parser(
        "skip",
        help="skip a run entirely",
        description=(
            "Marks the run skipped: the daemon will not advance it at all. For a "
            "run that should never be processed. Reverse with 'unskip'."
        ),
        epilog=_STATE_ONLY_EPILOG,
    )
    skip_parser.add_argument("run", help="the run name")
    skip_parser.add_argument("--reason", help="recorded in the audit trail")
    skip_parser.add_argument(
        "--force", action="store_true", help="skip even a protected run"
    )
    skip_parser.set_defaults(
        handler=lambda args: _run_op(
            control.skip, args, needs_run=True, force=args.force
        )
    )

    for name, operation, help_text, description in (
        (
            "pause",
            control.pause,
            "stop the daemon advancing ANY run",
            (
                "Global stop. No run advances until 'resume'; in-flight gates "
                "finish. Use when something is wrong fleet-wide — bad config, a "
                "full disk, an unreachable server."
            ),
        ),
        (
            "resume",
            control.resume,
            "let the daemon advance runs again",
            "Clears a global pause. Runs resume on the daemon's next look.",
        ),
    ):
        global_parser = subcommands.add_parser(
            name, help=help_text, description=description, epilog=_STATE_ONLY_EPILOG
        )
        global_parser.add_argument("--reason", help="recorded in the audit trail")
        global_parser.set_defaults(
            handler=lambda args, operation=operation: _run_op(
                operation, args, needs_run=False
            )
        )

    return parser


def main(argv=None) -> int:
    """Entry point. Returns a process exit code; never shows an operator a traceback.

    Expected failures — a refused op, an unknown run, an unreadable config, a
    missing config key — each become one clear line on stderr and exit 1. Anything
    else propagates, loudly and with its traceback, because an unexpected error is
    a bug that should be reported rather than dressed up as a diagnostic.
    """
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        with _logs_on_stderr():
            return args.handler(args)
    except ConfigError as config_error:
        print(f"sluice: {config_error}", file=sys.stderr)
        return EXIT_REFUSED
    except control.UnknownRunError as unknown_run:
        print(f"sluice: {unknown_run}", file=sys.stderr)
        return EXIT_REFUSED
    except control.ControlError as refusal:
        # A refusal is the control layer doing its job — a protected run, an active
        # run that must be held first. Report the reason and exit non-zero, so a
        # wrapper script cannot mistake a refusal for success.
        print(f"sluice: refused — {refusal}", file=sys.stderr)
        return EXIT_REFUSED
    except KeyError as missing_key:
        print(f"sluice: config is missing required key {missing_key}", file=sys.stderr)
        return EXIT_REFUSED


if __name__ == "__main__":  # pragma: no cover - process entry point
    sys.exit(main())
