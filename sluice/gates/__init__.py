"""Production gates implementing ``sluice.core.gate.Gate`` (Plan B).

download, checksum, merge, nvd, globus, registration — a real wastewater
metagenomic-sequencing pipeline. All infrastructure specifics live in gitignored
config; nothing here is a secret or a baked-in production default.

Gates depend on core; core never imports gates.
"""
