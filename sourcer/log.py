"""Logging for the package.

Library modules call ``get_logger(__name__)`` and never print. Entry points
(CLIs, the HTTP service, the Lambda handler) call ``configure()`` once so
diagnostics go to stderr in a single, greppable format while results keep
going to stdout. ``SOURCER_LOG_LEVEL`` sets the level (default INFO).
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Any

FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
DATE_FORMAT = "%Y-%m-%dT%H:%M:%S"
PACKAGE_LOGGER = "sourcer"


class _StderrHandler(logging.StreamHandler):
    """Writes to whatever ``sys.stderr`` is at emit time, not at creation.

    Test runners and some hosts swap ``sys.stderr``; binding the stream once
    would leave the handler writing to a closed file.
    """

    def __init__(self) -> None:
        super().__init__()

    @property
    def stream(self) -> Any:
        return sys.stderr

    @stream.setter
    def stream(self, value: Any) -> None:
        del value  # always resolved dynamically


def configure(level: str | None = None) -> None:
    """Attach one stderr handler to the package logger. Idempotent."""
    resolved = (level or os.environ.get("SOURCER_LOG_LEVEL", "INFO")).upper()
    root = logging.getLogger(PACKAGE_LOGGER)
    if not root.handlers:
        handler = _StderrHandler()
        handler.setFormatter(logging.Formatter(FORMAT, DATE_FORMAT))
        root.addHandler(handler)
        root.propagate = False
    root.setLevel(resolved)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
