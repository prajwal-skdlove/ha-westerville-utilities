"""Makes `client/` importable as a standalone top-level package.

`client/` was deliberately built with zero Home Assistant dependency (see
its own docstrings), but importing it the normal way --
`custom_components.westerville_utilities.client...` -- executes the parent
package's `__init__.py` first, which *does* import `homeassistant` (it's
the real integration entry point). That would drag a full HA install into
what's supposed to be an HA-independent test group. Importing `client` from
its own directory sidesteps the parent entirely.
"""

from __future__ import annotations

from pathlib import Path
import sys

_CLIENT_PARENT = Path(__file__).parents[2] / "custom_components" / "westerville_utilities"
if str(_CLIENT_PARENT) not in sys.path:
    sys.path.insert(0, str(_CLIENT_PARENT))
