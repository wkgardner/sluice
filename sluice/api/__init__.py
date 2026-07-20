"""API layer — the FastAPI control-write path (Plan C).

Each endpoint is a thin wrapper over one ``sluice.core.control`` function:
parse request -> call the pure op -> return its Result. The service adds policy
(authz, one audit choke point, validation) and holds no authoritative state.
"""
