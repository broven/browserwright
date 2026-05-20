"""REPL entry point: inline heredoc execution (in-process).

The cross-process REPL daemon (server/client/_proto) was removed in P3 — it
was the silent cross-talk vector. Only the in-process heredoc runtime remains.
"""
from . import inline  # noqa: F401
