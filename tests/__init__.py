"""No test downloads a release asset unless it says so.

The CI job sets RETROBIOS_OFFLINE, but a local run of the documented command
did not, and e2e fixtures queried the large-files release and wrote its
answers into the shared .cache/large. A test that serves its own assets
clears the variable for itself.
"""

from __future__ import annotations

import os

os.environ.setdefault("RETROBIOS_OFFLINE", "1")
