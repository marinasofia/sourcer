"""Logging setup and the package version fallback."""

from __future__ import annotations

import importlib
import logging
from importlib.metadata import PackageNotFoundError
from unittest.mock import patch

import sourcer
from sourcer import log


def reset_package_logger():
    root = logging.getLogger(log.PACKAGE_LOGGER)
    for h in list(root.handlers):
        root.removeHandler(h)
    root.propagate = True
    root.setLevel(logging.NOTSET)


def test_configure_attaches_one_stderr_handler_and_is_idempotent(monkeypatch):
    monkeypatch.delenv("SOURCER_LOG_LEVEL", raising=False)
    reset_package_logger()
    try:
        log.configure()
        log.configure()
        root = logging.getLogger(log.PACKAGE_LOGGER)
        assert len(root.handlers) == 1
        assert isinstance(root.handlers[0], logging.StreamHandler)
        assert root.level == logging.INFO
        assert root.propagate is False
    finally:
        reset_package_logger()


def test_configure_reads_level_from_env(monkeypatch):
    monkeypatch.setenv("SOURCER_LOG_LEVEL", "debug")
    reset_package_logger()
    try:
        log.configure()
        assert logging.getLogger(log.PACKAGE_LOGGER).level == logging.DEBUG
        log.configure("WARNING")  # explicit argument wins and re-levels the same handler
        root = logging.getLogger(log.PACKAGE_LOGGER)
        assert root.level == logging.WARNING and len(root.handlers) == 1
    finally:
        reset_package_logger()


def test_get_logger_is_a_child_of_the_package_logger():
    child = log.get_logger("sourcer.pipeline.source")
    assert child.parent is not None
    assert child.name.startswith(log.PACKAGE_LOGGER)


def test_version_is_a_string():
    assert isinstance(sourcer.__version__, str) and sourcer.__version__


def test_version_falls_back_without_installed_metadata():
    with patch("importlib.metadata.version", side_effect=PackageNotFoundError("sourcer-jobs")):
        reloaded = importlib.reload(sourcer)
        assert reloaded.__version__ == "0.0.0+local"
    importlib.reload(sourcer)  # restore the real value for later tests
