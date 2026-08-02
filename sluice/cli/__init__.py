"""``sluice`` — the operator command line.

A thin client over the control ops (``sluice.core.control``). It adds no policy of its own: every
command is one control op plus formatting, so anything the CLI can do is something the audited
control layer already sanctioned, and anything it refuses is a refusal that came from there.

Built with **Typer + rich**: colour-coded status, aligned tables, and rich-formatted help. Both
arrived as project dependencies with this CLI (plan 15 increment 4).

**DEVIATION from the original Plan C shape, recorded on purpose (plan 15 §9).** This module's
first docstring said the CLI would be an HTTP client to ``sluice.api`` because "the api is the
authorized control-write path". This version talks to ``control`` **directly**. Two reasons:

1. *The property that mattered is preserved.* Auditing lives in ``control`` — every state-changing
   op writes its ``control_audit`` row inside its own transaction — not in the API. Going direct
   keeps every write attributed and audited exactly as before. What the API adds is **remote**
   access, not authorization.
2. *Remote access is not what is needed first.* The API means FastAPI, a service to supervise, and
   a port to secure — before anyone can unstick a single run. An operator on the host the daemon
   runs on needs none of that.

The API remains the right answer for operating sluice from a laptop, and this CLI is structured so
that swapping its transport later touches only ``_call_op`` and the two readers. Nothing else here
knows how the ops are reached.

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
found; ``2`` a usage error (Click's own convention). Refusals exit non-zero on purpose, so a
wrapper script cannot mistake "protected, refused" for "done".

## Output channels

The answer goes to **stdout**; everything else — log events, error messages, refusals — goes to
**stderr**. That is what makes ``sluice --json status | jq`` work: a ``control.finished`` log line
on stdout would corrupt the JSON.

## Attribution

Every state-changing op writes an audit row naming an **actor**, defaulting to the OS user and
overridable with ``--actor``. There is no anonymous path: the audit trail is the only record of who
did what, and an unattributed row is worthless evidence.
"""

import contextlib
import dataclasses
import getpass
import json
import sys

import structlog
import typer
from rich.console import Console
from rich.table import Table
from rich.text import Text

from sluice.config import ConfigError, load_config
from sluice.core import control
from sluice.core.daemon import assemble_registry
from sluice.core.profile import REFERENCE_PROFILES
from sluice.gates.profiles import PRODUCTION_PROFILES

__all__ = ["app", "main"]

EXIT_OK = 0
EXIT_REFUSED = 1

# Shared by every state-changing subcommand. The reconcile-loop model is the one
# thing an operator must understand, and --help is where they will look for it.
_STATE_ONLY_EPILOG = (
    "[dim]Changes stored state only. The daemon acts on its next look "
    "(up to one poll interval later); nothing runs immediately.[/dim]"
)

# Outcome -> colour, keyed on the EXACT vocabulary ``events.run_outcome`` returns
# ({quarantined, completed, held, skipped, active}) — a near-miss like "complete"
# silently falls through to the default and the colour quietly stops meaning
# anything. Chosen so quarantined is the only red thing on a healthy screen.
_OUTCOME_STYLES = {
    "completed": "green",
    "quarantined": "bold red",
    "held": "yellow",
    "skipped": "dim",
    "active": "cyan",
}

# stdout carries the answer, stderr the narration. Two consoles, never crossed.
_out = Console()
_err = Console(stderr=True)


@dataclasses.dataclass(frozen=True)
class Settings:
    """The global options, resolved once and carried on the Click context."""

    config: str | None
    actor: str
    as_json: bool


@contextlib.contextmanager
def _logs_on_stderr():
    """Route structlog output to stderr for one command, then restore it.

    The control ops emit their canonical ``control.finished`` event through
    structlog, whose default logger factory writes to **stdout**. In a daemon that
    is fine; in a CLI it corrupts the data channel.

    **Restoring matters.** ``structlog.configure`` is process-global, so a ``main()``
    that configured and walked away would silently change logging for everything
    else in the process — the daemon, another library, the rest of a test session.
    A function callable in-process must leave global state as it found it.
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
    the daemon uses would be worse than none.
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


@contextlib.contextmanager
def _guard():
    """Turn every expected failure into one stderr line and exit code 1.

    Errors are handled where they happen rather than in ``main``, so Typer owns
    the exit path and this module never depends on Click internals (Typer vendors
    Click privately as ``typer._click``; importing that would be fragile).

    Unexpected exceptions are deliberately NOT caught — a traceback for a genuine
    bug is a report worth having, while a traceback for a missing config file is
    just noise at an operator.
    """
    try:
        yield
    except ConfigError as config_error:
        _err.print(f"[red]sluice:[/red] {config_error}")
        raise typer.Exit(EXIT_REFUSED) from None
    except control.UnknownRunError as unknown_run:
        _err.print(f"[red]sluice:[/red] {unknown_run}")
        raise typer.Exit(EXIT_REFUSED) from None
    except control.ControlError as refusal:
        # A refusal is the control layer doing its job — a protected run, an
        # active run that must be held first. Report it and exit non-zero, so a
        # wrapper script cannot mistake a refusal for success.
        _err.print(f"[red]sluice: refused[/red] — {refusal}")
        raise typer.Exit(EXIT_REFUSED) from None
    except KeyError as missing_key:
        _err.print(f"[red]sluice:[/red] config is missing required key {missing_key}")
        raise typer.Exit(EXIT_REFUSED) from None


def _settings(context: typer.Context) -> Settings:
    """The resolved global options for this invocation."""
    return context.obj


def _state_file(settings: Settings) -> str:
    """The database path from config, or a clear failure naming what is missing."""
    return load_config(settings.config)["state_file"]


def _emit_json(payload) -> None:
    """Write JSON to stdout with nothing else on the channel."""
    # Deliberately print(), not the rich console: rich would wrap and style it,
    # and this output exists to be parsed.
    print(json.dumps(payload, indent=2, default=str))


app = typer.Typer(
    name="sluice",
    no_args_is_help=True,
    add_completion=False,
    rich_markup_mode="rich",
    help=(
        "Operator control for the [bold]sluice[/bold] sequencing pipeline.\n\n"
        "sluice is a [italic]reconcile loop, not a queue[/italic]: these commands "
        "change stored state and return immediately. The daemon notices on its "
        "next look and acts then — so a command that appears to do nothing is "
        "working correctly."
    ),
    epilog=(
        "Config: [cyan]--config[/cyan], else [cyan]$SLUICE_CONFIG[/cyan], else "
        "[cyan]./config.json[/cyan].  Full guide: [cyan]docs/OPERATING.md[/cyan]"
    ),
)


@app.callback()
def _global_options(
    context: typer.Context,
    config: str = typer.Option(
        None, "--config", "-c", help="Path to config.json.", metavar="PATH"
    ),
    actor: str = typer.Option(
        None, "--actor", help="Who to record in the audit trail.", metavar="NAME"
    ),
    as_json: bool = typer.Option(
        False, "--json", help="Emit machine-readable JSON instead of a table."
    ),
) -> None:
    """Resolve the options every command shares."""
    context.obj = Settings(
        config=config, actor=actor or _default_actor(), as_json=as_json
    )


# ---------------------------------------------------------------------------
# Readers.
# ---------------------------------------------------------------------------


def _outcome_text(outcome) -> Text:
    """The outcome, styled so the states needing a human stand out."""
    label = str(outcome) if outcome is not None else "?"
    return Text(label, style=_OUTCOME_STYLES.get(label, "cyan"))


def _render_overview(status) -> None:
    """The fleet board: global flags, a table of runs, then what to do next."""
    paused = status.get("paused")
    header = Table.grid(padding=(0, 2))
    header.add_column(style="dim")
    header.add_column()
    header.add_row("pipeline profile", str(status.get("pipeline_profile")))
    header.add_row(
        "daemon",
        Text("PAUSED — no run will advance", style="bold red")
        if paused
        else Text("running", style="green"),
    )
    _out.print(header)

    runs = status.get("runs", [])
    if not runs:
        _out.print("\n[dim]No runs detected yet.[/dim]")
        return

    table = Table(box=None, pad_edge=False, header_style="bold dim")
    table.add_column("RUN", overflow="fold")
    table.add_column("EXPERIMENT")
    table.add_column("OUTCOME")
    table.add_column("LIFECYCLE")
    table.add_column("NOTE", overflow="fold")
    for entry in runs:
        experiment = entry.get("experiment_id")
        note = entry.get("error_reason") or ""
        if entry.get("protected"):
            note = f"PROTECTED  {note}".strip()
        table.add_row(
            entry["name"],
            str(experiment) if experiment is not None else "—",
            _outcome_text(entry.get("outcome")),
            str(entry.get("lifecycle", "?")),
            Text(note, style="red" if entry.get("error_reason") else "dim"),
        )
    _out.print()
    _out.print(table)

    quarantined = [entry for entry in runs if entry.get("error_reason")]
    if quarantined:
        # A quarantined run waits forever by design — there is no automatic
        # retry — so the board states it rather than leaving it to be noticed.
        _out.print(
            f"\n[bold red]{len(quarantined)} run(s) quarantined[/bold red] and "
            "waiting for an operator.\nFix the cause, then: "
            "[cyan]sluice clear-error <run>[/cyan]"
        )


def _render_run_detail(entry) -> None:
    """One run in full, including which gates are done and which are pending."""
    detail = Table.grid(padding=(0, 2))
    detail.add_column(style="dim", justify="right")
    detail.add_column()
    detail.add_row("run", Text(entry["name"], style="bold"))
    detail.add_row("experiment id", str(entry.get("experiment_id")))
    detail.add_row("outcome", _outcome_text(entry.get("outcome")))
    detail.add_row("lifecycle", str(entry.get("lifecycle")))
    detail.add_row("pipeline", str(entry.get("pipeline_version")))
    detail.add_row("detected", str(entry.get("detected_at")))
    detail.add_row("completed", str(entry.get("completed_at") or "—"))
    detail.add_row("protected", "yes" if entry.get("protected") else "no")
    if entry.get("error_reason"):
        detail.add_row("error", Text(entry["error_reason"], style="bold red"))
    # Per-run anomalies are fields, not exceptions (status is total), so they
    # have to be rendered or they are invisible.
    for anomaly in ("profile_error", "exclusions_error"):
        if entry.get(anomaly):
            detail.add_row(anomaly, Text(str(entry[anomaly]), style="red"))
    if entry.get("excluded_gates"):
        detail.add_row("excluded gates", ", ".join(entry["excluded_gates"]))
    gates = entry.get("gates")
    if gates:
        detail.add_row(
            "gates done",
            Text(", ".join(gates.get("satisfied", [])) or "—", style="green"),
        )
        detail.add_row(
            "gates pending",
            Text(", ".join(gates.get("unsatisfied", [])) or "—", style="yellow"),
        )
    _out.print(detail)


@app.command()
def status(
    context: typer.Context,
    run: str = typer.Option(None, "--run", "-r", help="Show just this run, in full."),
) -> None:
    """Show every run, and whether the daemon is paused.

    The board. Lists each run with its experiment id, outcome, and any quarantine
    reason, and calls out runs that are waiting for a human.
    """
    settings = _settings(context)
    with _guard():
        payload = control.get_status(_state_file(settings), run, registry=_registry())
    if settings.as_json:
        _emit_json(payload)
        return
    if run:
        _render_run_detail(payload)
    else:
        _render_overview(payload)


@app.command()
def show(context: typer.Context, run: str = typer.Argument(..., help="Run name.")):
    """Show one run in full, including which gates remain.

    "Which gate is it stuck on?" is answered by the first entry under
    [bold]gates pending[/bold].
    """
    settings = _settings(context)
    with _guard():
        entry = control.get_status(_state_file(settings), run, registry=_registry())
    if settings.as_json:
        _emit_json(entry)
        return
    _render_run_detail(entry)


# ---------------------------------------------------------------------------
# State-changing ops. Each is exactly one control op plus rendering.
# ---------------------------------------------------------------------------


def _render_result(result) -> None:
    """Show a control op's outcome, including the audit row it wrote."""
    target = f" [bold]{result.target_run}[/bold]" if result.target_run else ""
    if result.status == "noop":
        _out.print(
            f"[yellow]{result.action}[/yellow]{target}: nothing to do "
            f"([dim]{result.message}[/dim])"
        )
    else:
        _out.print(f"[green]{result.action}[/green]{target}: ok")
        for column, value in result.changes.items():
            _out.print(f"    [dim]{column}[/dim] → {value}")
    _out.print(f"    [dim]audited as #{result.audit_id}[/dim]")


def _call_op(settings: Settings, operation, *positional, **keyword) -> None:
    """Call one control op and render it.

    The single place that knows how a control op is reached. Swapping this for an
    HTTP call is the whole of the future API migration.
    """
    with _guard():
        result = operation(
            _state_file(settings), settings.actor, *positional, **keyword
        )
    if settings.as_json:
        _emit_json(
            {
                "action": result.action,
                "actor": result.actor,
                "run": result.target_run,
                "status": result.status,
                "message": result.message,
                "changes": dict(result.changes),
                "audit_id": result.audit_id,
            }
        )
        return
    _render_result(result)


_RUN_ARGUMENT = typer.Argument(..., help="Run name.")
_REASON_OPTION = typer.Option(
    None,
    "--reason",
    help="Recorded in the audit trail — say why, for whoever reads it later.",
)


@app.command(epilog=_STATE_ONLY_EPILOG)
def hold(
    context: typer.Context, run: str = _RUN_ARGUMENT, reason: str = _REASON_OPTION
) -> None:
    """Stop a run from advancing.

    An in-flight gate finishes; the [italic]next[/italic] one never starts.
    Reverse with [cyan]unhold[/cyan].
    """
    _call_op(_settings(context), control.hold, run, reason=reason)


@app.command(epilog=_STATE_ONLY_EPILOG)
def unhold(
    context: typer.Context, run: str = _RUN_ARGUMENT, reason: str = _REASON_OPTION
) -> None:
    """Let a held run advance again."""
    _call_op(_settings(context), control.unhold, run, reason=reason)


@app.command(epilog=_STATE_ONLY_EPILOG)
def skip(
    context: typer.Context,
    run: str = _RUN_ARGUMENT,
    reason: str = _REASON_OPTION,
    force: bool = typer.Option(False, "--force", help="Skip even a protected run."),
) -> None:
    """Skip a run entirely — the daemon will not advance it at all.

    For a run that should never be processed. Reverse with [cyan]unskip[/cyan].
    """
    _call_op(_settings(context), control.skip, run, reason=reason, force=force)


@app.command(epilog=_STATE_ONLY_EPILOG)
def unskip(
    context: typer.Context, run: str = _RUN_ARGUMENT, reason: str = _REASON_OPTION
) -> None:
    """Return a skipped run to active."""
    _call_op(_settings(context), control.unskip, run, reason=reason)


@app.command("clear-error", epilog=_STATE_ONLY_EPILOG)
def clear_error(
    context: typer.Context, run: str = _RUN_ARGUMENT, reason: str = _REASON_OPTION
) -> None:
    """Forgive a quarantined run so it can try again — [bold]the recovery command[/bold].

    A gate failure quarantines its run, and it stays quarantined until you run
    this — there is no automatic retry, by design.

    [bold]Fix the underlying cause first[/bold], then clear. The daemon re-runs the
    failed gate on its next look, and gates resume rather than restart: a
    partly-downloaded run continues where it stopped.

    A run that is also held needs [cyan]unhold[/cyan] too — the two are independent.
    """
    _call_op(_settings(context), control.clear_error, run, reason=reason)


@app.command(epilog=_STATE_ONLY_EPILOG)
def pause(context: typer.Context, reason: str = _REASON_OPTION) -> None:
    """Stop the daemon advancing [bold]any[/bold] run.

    Global stop, for when something is wrong fleet-wide — a full disk, an
    unreachable server, bad config. In-flight gates still finish.
    """
    _call_op(_settings(context), control.pause, reason=reason)


@app.command(epilog=_STATE_ONLY_EPILOG)
def resume(context: typer.Context, reason: str = _REASON_OPTION) -> None:
    """Let the daemon advance runs again, clearing a global pause."""
    _call_op(_settings(context), control.resume, reason=reason)


def main(argv=None) -> int:
    """Entry point. Returns a process exit code rather than exiting.

    Keeps ``main`` callable in-process — tests, and any future embedding — instead
    of only as a process. Typer runs in its normal standalone mode and raises
    ``SystemExit``; that is translated back into a return value here.

    Expected failures are already handled at their source by ``_guard`` and arrive
    as exit code 1. ``--help`` arrives as 0, a usage error as 2.
    """
    try:
        with _logs_on_stderr():
            # prog_name pinned: without it the usage line reflects how the
            # process was launched ("python -m sluice.cli"), which is not what
            # an operator types.
            app(args=argv, prog_name="sluice", standalone_mode=True)
    except SystemExit as exit_request:
        code = exit_request.code
        return EXIT_OK if code is None else int(code)
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover - process entry point
    sys.exit(main())
