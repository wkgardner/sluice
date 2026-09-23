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

Every state-changing op writes an audit row naming an **actor**: the OS user, and nothing else.
There is no anonymous path and no way to choose a name — the audit trail is the only record of who
did what, and a row naming whoever the caller *said* they were is worse than no row, because it
reads as authoritative.

``--actor`` used to accept an arbitrary string. That was harmless while one person could write to
the database; it became fake evidence the moment a shared installation let several people write.
The flag is gone rather than ignored, so a script still passing it fails loudly instead of
recording something other than what it says.
"""

import contextlib
import dataclasses
import getpass
import json
import socket
import sys

import structlog
import typer
from rich.console import Console
from rich.table import Table
from rich.text import Text

from sluice.config import ConfigError, load_config, resolve_config_path
from sluice.config.setup import apply_plan, plan_setup
from sluice.core import control
from sluice.core.daemon import (
    DaemonHostError,
    assemble_registry,
    assert_daemon_host,
    configure_logging,
    is_daemon_running,
    read_last_look,
    run_forever,
)
from sluice.core.profile import REFERENCE_PROFILES, UnknownProfileError
from sluice.doctor import Severity, run_doctor, worst
from sluice.gates.detection import list_remote_runs
from sluice.gates.profiles import PRODUCTION_PROFILES, PRODUCTION_VERSION
from sluice.gates.slack import make_notifier

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


def _current_actor() -> str:
    """The OS user, for the audit trail. Not overridable, by design.

    An OS that cannot name its own user is a refusal, not a fallback. The old
    ``"unknown"`` placeholder was exactly the unattributed row the design calls
    worthless evidence — and worse than refusing, because a command that
    succeeded looks like one that was accounted for.

    Raises:
        ConfigError: the OS cannot name the current user. Reusing that type
            keeps this on the ``_guard`` path, so it exits with a sentence
            rather than a traceback.
    """
    try:
        user = getpass.getuser()
    except Exception as no_user:
        raise ConfigError(
            "cannot determine the current OS user, so this action could not be "
            "attributed to anyone. sluice refuses to write an unattributed "
            "audit row."
        ) from no_user
    if not user.strip():
        raise ConfigError(
            "the current OS user is empty, so this action could not be "
            "attributed to anyone. sluice refuses to write an unattributed "
            "audit row."
        )
    return user


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
    except DaemonHostError as wrong_host:
        # Not a bug and not a refusal by the control layer — the operator is
        # simply standing on the wrong machine, and the message says which.
        _err.print(f"[red]sluice:[/red] {wrong_host}")
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
    except UnknownProfileError as unknown_profile:
        # Before the KeyError clause: UnknownProfileError subclasses KeyError,
        # and "config is missing required key" would be the wrong story.
        _err.print(f"[red]sluice:[/red] {unknown_profile}")
        raise typer.Exit(EXIT_REFUSED) from None
    except KeyError as missing_key:
        _err.print(f"[red]sluice:[/red] config is missing required key {missing_key}")
        raise typer.Exit(EXIT_REFUSED) from None


def _settings(context: typer.Context) -> Settings:
    """The resolved global options for this invocation."""
    return context.obj


def _state_file(settings: Settings) -> str:
    """The database path from config, guarded against use from the wrong machine.

    Every command that touches the database comes through here, which is why the
    cross-host check lives here rather than in each op: one choke point cannot be
    forgotten by the next command someone adds.

    It runs *before* the path is handed to anything that would open it. That
    ordering is the whole point — on a cluster the state file sits on shared
    storage, and a second machine opening a WAL database is the corruption case
    design §5 forbids. Reading the claim costs one ``read_text`` of a sibling
    file and touches no SQLite.
    """
    state_file = load_config(settings.config)["state_file"]
    assert_daemon_host(state_file, socket.gethostname())
    return state_file


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
        "[cyan]./config.toml[/cyan], else the one [cyan]sluice setup[/cyan] wrote "
        "under [cyan]~/.sluice[/cyan].  Full guide: [cyan]docs/OPERATING.md[/cyan]"
    ),
)


@app.callback()
def _global_options(
    context: typer.Context,
    config: str = typer.Option(
        None, "--config", "-c", help="Path to config.toml.", metavar="PATH"
    ),
    as_json: bool = typer.Option(
        False, "--json", help="Emit machine-readable JSON instead of a table."
    ),
) -> None:
    """Resolve the options every command shares."""
    # The actor is resolved lazily by `_call_op`, not here: `status` and
    # `doctor` need no attribution, and refusing them because the OS cannot name
    # a user would break the two commands you most want during an outage.
    context.obj = Settings(config=config, as_json=as_json)


# ---------------------------------------------------------------------------
# Readers.
# ---------------------------------------------------------------------------


def _outcome_text(outcome) -> Text:
    """The outcome, styled so the states needing a human stand out."""
    label = str(outcome) if outcome is not None else "?"
    return Text(label, style=_OUTCOME_STYLES.get(label, "cyan"))


def _daemon_health(status) -> Text:
    """One line answering "is it working?" — liveness first, then progress.

    Two facts, because either alone misleads. The lock says whether a daemon
    process exists; the heartbeat says when it last began a look. A live daemon
    with an old heartbeat is inside a long gate, which is normal for NVD and
    must not read as a fault. A dead daemon is the case that matters, and
    nothing else in sluice would tell an operator about it — nothing supervises
    it (errata E1), so after a reboot it simply is not there.
    """
    if not status.get("daemon_running"):
        return Text("NOT RUNNING — start it: sluice daemon", style="bold red")
    last_look = status.get("daemon_last_look")
    if not last_look:
        return Text("running (no look recorded yet)", style="green")
    return Text(f"running, last look {last_look}", style="green")


def _render_overview(status) -> None:
    """The fleet board: global flags, a table of runs, then what to do next."""
    paused = status.get("paused")
    header = Table.grid(padding=(0, 2))
    header.add_column(style="dim")
    header.add_column()
    header.add_row("pipeline profile", str(status.get("pipeline_profile")))
    header.add_row("daemon", _daemon_health(status))
    if paused:
        header.add_row("", Text("PAUSED — no run will advance", style="bold red"))
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


# Reading order for the report: least urgent first, so failures finish the table
# next to the summary rather than scrolling off the top. Presentation only —
# `doctor.worst` ranks severity for the exit code and is not this.
_DOCTOR_DISPLAY_ORDER = {
    Severity.SKIP: 0,
    Severity.OK: 1,
    Severity.WARN: 2,
    Severity.FAIL: 3,
}

_DOCTOR_STYLES = {
    Severity.OK: ("[green]ok[/green]", "green"),
    Severity.WARN: ("[yellow]warn[/yellow]", "yellow"),
    Severity.FAIL: ("[red]FAIL[/red]", "red"),
    Severity.SKIP: ("[dim]skip[/dim]", "dim"),
}


def _render_doctor(results, config_path) -> None:
    """One row per check, least urgent first, then a one-line summary.

    Ordered so the failures land at the bottom, directly above the summary and
    wherever the operator's cursor already is. A full config runs to forty-odd
    rows, and in run order the failures scatter through them and scroll off the
    top — leaving the reader to hunt upward for the thing they have to act on.

    The sort is stable, so within a severity the checks keep the order they ran
    and related keys stay together rather than being shuffled into alphabetical
    order.

    The config path is printed above the table because "doctor still says X
    after I fixed X" is almost always a second config file, and nothing else in
    the output says which one was read.

    Skipped checks are shown rather than hidden: "not used in this source mode"
    is information an operator wants when they are wondering why doctor said
    nothing about the host they just configured.
    """
    _out.print(f"[dim]config:[/dim] {config_path}")
    table = Table(title="sluice doctor", show_lines=False)
    table.add_column("")
    table.add_column("area", style="cyan")
    table.add_column("check")
    table.add_column("detail", overflow="fold")
    for result in sorted(results, key=lambda r: _DOCTOR_DISPLAY_ORDER[r.severity]):
        marker, _style = _DOCTOR_STYLES[result.severity]
        table.add_row(marker, result.area, result.check_id, result.detail)
    _out.print(table)

    failures = [r for r in results if r.severity is Severity.FAIL]
    warnings = [r for r in results if r.severity is Severity.WARN]
    if failures:
        _out.print(
            f"\n[red]{len(failures)} check(s) failed[/red] — sluice cannot run "
            f"correctly until these are fixed."
        )
    elif warnings:
        _out.print(f"\n[yellow]{len(warnings)} warning(s)[/yellow] — sluice will run.")
    else:
        _out.print("\n[green]All checks passed.[/green]")


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

    Lists each run with its experiment id, outcome, and any quarantine reason,
    and calls out the runs that are waiting for a human.
    """
    settings = _settings(context)
    with _guard():
        payload = control.get_status(_state_file(settings), run, registry=_registry())
        if not run:
            # Liveness and progress are read here, not in `control`: `daemon`
            # imports `control`, so control cannot import daemon back, and the
            # CLI is already the composition root that sees both.
            state_file = _state_file(settings)
            payload["daemon_running"] = is_daemon_running(state_file)
            payload["daemon_last_look"] = read_last_look(state_file)
    if settings.as_json:
        _emit_json(payload)
        return
    if run:
        _render_run_detail(payload)
    else:
        _render_overview(payload)


@app.command()
def doctor(context: typer.Context) -> None:
    """Check that this installation can actually work.

    [bold]setup proves your config is complete; doctor proves it works.[/bold]
    Reads everything, changes nothing — no directory is created, no config
    written, no repository checked out.

    Exits 1 if any check failed, 0 otherwise. Warnings do not change the exit
    code, so an install that works but wants attention stays scriptable.
    """
    settings = _settings(context)
    config_path = resolve_config_path(settings.config)
    try:
        config = load_config(settings.config)
    except ConfigError as no_config:
        # Not the shared `_guard` message: doctor is the first command a new
        # operator runs, and "no config file at ..." leaves them nowhere to go.
        _err.print(
            f"[red]sluice:[/red] {no_config}\n"
            f"If sluice has not been installed on this machine yet, run "
            f"[bold]sluice setup[/bold] — it writes the config and creates the "
            f"database."
        )
        raise typer.Exit(EXIT_REFUSED) from None
    with _guard():
        results = run_doctor(config, _registry(), PRODUCTION_VERSION)

    if settings.as_json:
        _emit_json(
            [
                {
                    "check": result.check_id,
                    "area": result.area,
                    "severity": result.severity.value,
                    "detail": result.detail,
                }
                for result in results
            ]
        )
    else:
        _render_doctor(results, config_path)

    if worst(results) is Severity.FAIL:
        raise typer.Exit(EXIT_REFUSED)


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
            _state_file(settings), _current_actor(), *positional, **keyword
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
    """Skip a run — the daemon will never advance it.

    For a run that should not be processed at all. Reverse with
    [cyan]unskip[/cyan].
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
    this. There is no automatic retry.

    [bold]Fix the underlying cause first[/bold], then clear. The daemon re-runs the
    failed gate on its next look, and gates resume rather than restart: a
    partly-downloaded run continues where it stopped.

    A run that is also held needs [cyan]unhold[/cyan] too — the two are independent.
    """
    _call_op(_settings(context), control.clear_error, run, reason=reason)


@app.command(epilog=_STATE_ONLY_EPILOG)
def pause(context: typer.Context, reason: str = _REASON_OPTION) -> None:
    """Stop the daemon advancing [bold]any[/bold] run.

    Use it when something is wrong for every run at once: a full disk, an
    unreachable server, bad config. Gates already in flight still finish.
    """
    _call_op(_settings(context), control.pause, reason=reason)


@app.command(epilog=_STATE_ONLY_EPILOG)
def resume(context: typer.Context, reason: str = _REASON_OPTION) -> None:
    """Let the daemon advance runs again, clearing a global pause."""
    _call_op(_settings(context), control.resume, reason=reason)


@app.command("set-default-profile", epilog=_STATE_ONLY_EPILOG)
def set_default_profile(
    context: typer.Context,
    pipeline_version: str = typer.Argument(
        ..., help="A registered profile version, e.g. production-v3.2."
    ),
) -> None:
    """Choose which profile [bold]newly detected[/bold] runs are stamped with.

    The rollout step for a new gate: shipped profiles are frozen, so a gate joins
    a new version, and this switches the default to it. Runs already detected
    keep the version they were stamped with — nothing re-runs. An unknown
    version is refused and never stored.
    """
    _call_op(
        _settings(context),
        control.set_default_profile,
        pipeline_version,
        registry=_registry(),
    )


@app.command()
def daemon(
    context: typer.Context,
    once: bool = typer.Option(
        False, "--once", help="Run a single cycle and exit, instead of looping."
    ),
) -> None:
    """Start the reconcile loop — the process that actually does the work.

    Nothing else in sluice moves data. The other commands change stored state;
    this is what notices and acts on it.

    Runs until interrupted. Only one instance can run at a time — a second start
    fails on the lock rather than racing the first.
    """
    settings = _settings(context)
    with _guard():
        config = load_config(settings.config)
        # Wire the file sinks BEFORE any work: the rotating events.jsonl and
        # the per-run logs are the only record of what happened once the
        # console scrolls away (3af — this call existed for six weeks with no
        # caller, and a 4-hour checksum ran invisibly because of it).
        configure_logging(config)
        registry = _registry()
        notifier = make_notifier(config)
        _out.print(
            f"[green]Starting daemon[/green] — profile "
            f"[cyan]{PRODUCTION_VERSION}[/cyan], notifications "
            f"{'[green]on[/green]' if notifier else '[dim]off[/dim]'}"
        )
        run_forever(
            config,
            registry=registry,
            default_pipeline_version=PRODUCTION_VERSION,
            lister=list_remote_runs,
            notifier=notifier,
            max_cycles=1 if once else None,
            # The composition root supplies the host: core stays socket-free,
            # and starting here is what claims the DB for this machine.
            hostname=socket.gethostname(),
        )


@app.command()
def setup(
    home: str = typer.Option(
        None, "--home", help="Where sluice keeps its files.", metavar="PATH"
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Show what would change without writing anything."
    ),
    assume_yes: bool = typer.Option(
        False, "--yes", "-y", help="Do not ask before rewriting an existing config."
    ),
    shared_group: str = typer.Option(
        None,
        "--shared-group",
        help="Let this Unix group operate sluice too. Credentials stay yours.",
        metavar="GROUP",
    ),
) -> None:
    """Create or upgrade this installation — safe to re-run after every release.

    On a fresh machine this creates [cyan]~/.sluice[/cyan], migrates the database,
    and writes a config with every key the current version knows about.

    On an existing one it [bold]adds only what is missing[/bold]. A value you
    have already set is never overwritten, by a default or by anything else.

    Rewriting an existing config asks first, and always copies the old file to
    [cyan]config.toml.bak[/cyan] before writing. Your values survive either way.
    The backup is there because the file is regenerated from the schema, which
    loses any comments or ordering you arranged by hand, and loses unsaved
    changes if an editor has the file open.

    An install still on the older [cyan]config.json[/cyan] is migrated on the
    next run: your values move into [cyan]config.toml[/cyan] and the JSON is
    left exactly where it is.

    [bold]--shared-group GROUP[/bold] lets everyone in that Unix group run
    sluice against this installation — they can see state, hold, and clear
    errors. It widens the config and the state directory only;
    [cyan]secrets/[/cyan] stays yours alone, because operating sluice never
    needs the credentials the daemon uses.

    Everyone must run from the machine the daemon is on. sluice enforces that
    rather than trusting it: the state file is claimed by whichever host the
    daemon starts on, and commands from anywhere else are refused.
    """
    plan = plan_setup(home=home)

    _out.print()
    if plan.migrated_from is not None:
        action = "Migrating"
    elif plan.is_upgrade:
        action = "Upgrading"
    else:
        action = "Creating"
    _out.print(f"[bold]{action}[/bold] sluice installation at [cyan]{plan.home}[/cyan]")
    if plan.migrated_from is not None:
        _out.print(
            f"\nCarrying your values from [cyan]{plan.migrated_from}[/cyan] into "
            f"[cyan]{plan.config_path}[/cyan]. The old file is left in place — "
            "delete it once you are happy."
        )

    if plan.added_with_default:
        table = Table(box=None, pad_edge=False, header_style="bold dim")
        table.add_column("NEW KEY")
        table.add_column("VALUE", overflow="fold")
        for key in sorted(plan.added_with_default):
            value = plan.added_with_default[key]
            table.add_row(key, str(value) if value != "" else "[dim](empty)[/dim]")
        _out.print()
        _out.print(table)
    elif plan.is_upgrade:
        _out.print("\n[green]No new keys[/green] — this install is already current.")

    if plan.unknown_keys:
        _out.print(
            f"\n[yellow]{len(plan.unknown_keys)} key(s) in your config are not "
            "used by this version[/yellow] (left untouched): "
            + ", ".join(plan.unknown_keys)
        )

    if plan.needs_attention:
        _out.print(
            "\n[bold red]These are written into the config as empty values — "
            "fill them in before sluice can run:[/bold red]"
        )
        needs = Table(box=None, pad_edge=False, header_style="bold dim")
        needs.add_column("KEY")
        needs.add_column("WHAT IT IS", overflow="fold")
        for field in plan.needs_attention:
            needs.add_row(Text(field.name, style="red"), field.help)
        _out.print(needs)

    if dry_run:
        _out.print("\n[dim]--dry-run: nothing was written.[/dim]")
        return

    backup_path = None
    if plan.is_upgrade:
        # Confirm before touching a config that already exists. Every value in it
        # survives, but the file is rewritten sorted and reformatted, and an
        # editor holding it open is a real way to lose work.
        if not assume_yes:
            if not sys.stdin.isatty():
                _err.print(
                    f"[red]sluice setup:[/red] {plan.config_path} already exists "
                    "and this is not an interactive terminal. Re-run with "
                    "[cyan]--yes[/cyan] to rewrite it, or [cyan]--dry-run[/cyan] "
                    "to see what would change."
                )
                raise typer.Exit(EXIT_REFUSED)
            _out.print(
                f"\n[yellow]This rewrites[/yellow] [cyan]{plan.config_path}[/cyan]. "
                "Your values are kept, but the file is regenerated — close it in "
                "your editor first if you have unsaved changes."
            )
            if not typer.confirm("Continue?", default=False):
                _out.print("[dim]Nothing was written.[/dim]")
                raise typer.Exit(EXIT_OK)

        # Back up before writing, not after: a backup taken afterwards would be a
        # copy of the new file, which is worth nothing.
        backup_path = plan.config_path.with_suffix(".toml.bak")
        try:
            backup_path.write_bytes(plan.config_path.read_bytes())
            backup_path.chmod(0o600)
        except OSError as backup_failure:
            _err.print(
                f"[red]sluice setup:[/red] could not back up "
                f"{plan.config_path} to {backup_path}: {backup_failure}"
            )
            raise typer.Exit(EXIT_REFUSED) from None

    with _guard():
        try:
            result = apply_plan(plan, shared_group=shared_group)
        except (ValueError, OSError) as setup_failure:
            _err.print(f"[red]sluice setup:[/red] {setup_failure}")
            raise typer.Exit(EXIT_REFUSED) from None

    if backup_path is not None:
        _out.print(f"\n[dim]Previous config saved to {backup_path}[/dim]")
    _out.print(f"\n[green]Wrote[/green] {plan.config_path}")
    if shared_group:
        _out.print(
            f"[green]Shared[/green] with the [cyan]{shared_group}[/cyan] group — "
            "they can operate sluice from this machine. "
            "[dim]secrets/ is unchanged and still yours only.[/dim]"
        )
    if result.database_initialised:
        _out.print("[green]Database[/green] migrated to the current schema.")
    if result.created_secret_placeholders:
        _out.print("\n[bold]Empty credential files created — fill these in:[/bold]")
        for secret_path in result.created_secret_placeholders:
            _out.print(f"    [cyan]{secret_path}[/cyan]")

    remaining = [field.name for field in plan.needs_attention]
    if remaining:
        _out.print(
            f"\n[yellow]Next:[/yellow] open [cyan]{plan.config_path}[/cyan] and fill "
            f"in the {len(remaining)} empty value(s) listed above — every key is "
            "already there, so you are editing values, not adding keys. Then run "
            "[cyan]sluice doctor[/cyan] to check it."
        )
    else:
        _out.print("\n[green]Ready.[/green] Try: [cyan]sluice doctor[/cyan]")


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
