"""browser-daemon: resolve a browser-level CDP WebSocket URL from any local Chrome.

v0.1 is Mode A only — a one-shot CLI resolver. Mode B (socket proxy) lands in v0.2.
The package surface is the `browser-daemon` console script; importing this module
directly is not part of the public contract (Skill talks via subprocess only).
"""

__version__ = "0.5.3"
