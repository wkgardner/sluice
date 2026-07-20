"""Config schema + loader (Plan A populates the loader).

Real infrastructure values live only in a gitignored ``config.json``; the
committed ``config.example.json`` is placeholders only. A missing required key is
a load-time error, never a silent production fallback.
"""
