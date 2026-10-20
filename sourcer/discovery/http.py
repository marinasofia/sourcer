"""Rate-limited JSON and HTML client for discovery sources."""

from __future__ import annotations

import json
import time
from typing import Any

from ..net import SafeSession


class DiscoveryClient:
    def __init__(self, interval: float = 1.0, session: Any | None = None) -> None:
        self.interval = interval
        self.session = session or SafeSession()
        self._last_request: float | None = None

    def get(self, url: str, **kwargs: Any) -> Any:
        now = time.monotonic()
        if self._last_request is not None:
            remaining = self.interval - (now - self._last_request)
            if remaining > 0:
                time.sleep(remaining)
        self._last_request = time.monotonic()
        headers = {"User-Agent": "Sourcer discovery/0.3", **kwargs.pop("headers", {})}
        response = self.session.get(
            url,
            timeout=kwargs.pop("timeout", 30),
            headers=headers,
            **kwargs,
        )
        response.raise_for_status()
        return response

    def get_json(self, url: str, **kwargs: Any) -> Any:
        response = self.get(url, **kwargs)
        try:
            return response.json()
        except AttributeError:
            return json.loads(response.text)
