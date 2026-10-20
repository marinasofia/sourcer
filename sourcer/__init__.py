"""Sourcer: a job matching agent built on LangGraph and the Claude API."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("sourcer-jobs")
except PackageNotFoundError:  # running from a checkout without an install
    __version__ = "0.0.0+local"

__all__ = ["__version__"]
