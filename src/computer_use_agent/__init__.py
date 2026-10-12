"""ComputerUseAgent: privacy-reduced interaction-trace recording and analysis toolkit.

This module must stay import-light (standard library only, no jsonschema): the
X11 guest bundle ships ``computer_use_agent/__init__.py`` together with the
trace core and runs it under the guest's system Python.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("computer-use-agent")
except PackageNotFoundError:  # running from a source tree or a zipapp bundle
    __version__ = "0.5.0"

__all__ = ["__version__"]
