"""Shared pytest configuration and fixtures."""
from __future__ import annotations

import sys
from unittest.mock import MagicMock

# dave.py is a native C extension (libdave bindings) that requires the
# platform-specific shared library. Stub it out when unavailable so the
# davey_compat tests can still run using their own per-test mocks.
try:
    import dave  # noqa: F401
except (ImportError, OSError):
    sys.modules["dave"] = MagicMock()
