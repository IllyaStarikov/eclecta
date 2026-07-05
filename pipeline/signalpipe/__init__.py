"""Signal — a local, continuously-running tech+AI feed-curation pipeline.

Ingests 1,000+ sources, dedups stories into clusters, scores them
deterministically, curates the daily finalists with Claude, and serves a
parameterized RSS feed + review dashboard on 127.0.0.1:8765.

Package is named `signalpipe` (not `signal`) to avoid shadowing the stdlib
`signal` module the worker needs for SIGTERM handling. User-facing name: Signal.
"""

__version__ = "0.1.0"
