"""Core service surface for PowerRail.

The frozen baseline only reports process health. Later work adds the real
capabilities described in README.md behind this module; keep the public
surface here backward compatible.
"""

from __future__ import annotations

from . import __version__


class Service:
    """Placeholder service. Only health reporting is implemented."""

    name = "powerrail"
    version = __version__

    def health(self) -> dict[str, str]:
        return {"status": "ok", "service": self.name, "version": self.version}
