"""Mode B (v0.2) — long-running daemon process.

This package is loaded only by `browserwright-daemon serve` and friends. v0.1 Mode A
subcommands never import from here, keeping the import graph + cold-start cost
of the CLI minimal.
"""
