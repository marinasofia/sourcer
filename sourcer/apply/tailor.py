"""Resume tailoring. Reserved: the working implementation is private.

``sourcer_private.tailor`` in the private repository proposes per-bullet
changes for a posting and writes a tailored copy next to the operator's
resumes. It stays there because it reads and writes personal files. This
module is the public seat for a data-free version.
"""

from __future__ import annotations

from typing import Any


def tailor(posting: dict[str, Any]) -> None:
    """Propose and apply resume changes for one posting."""
    raise NotImplementedError(f"tailor is not implemented ({posting.get('title')})")
