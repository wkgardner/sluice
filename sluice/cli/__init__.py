"""CLI — the ``sluice`` command (Plan C).

An HTTP client to the api (``sluice pause``, ``sluice hold RUN``,
``sluice status``, ...), built with Typer + rich. The CLI never writes the DB
directly; it goes through the api, which is the authorized control-write path.
"""
