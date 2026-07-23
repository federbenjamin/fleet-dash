"""fleet-dash core package: engine, provider adapters, notifications, search.

The repo root keeps the runtime entrypoint (server.py) and web assets
(dashboard.html, static/); this package holds everything imported by them.
`search_index.py` and `codex_launcher.py` are stdlib-only and are also spawned
directly as scripts by their absolute file path.
"""
