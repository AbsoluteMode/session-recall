"""Suite-wide environment pin.

The embedding default resolves at import time from the machine's environment,
and since the bundled-model fallback it DIFFERS by machine (key present →
voyage/1024, nothing → builtin/384). The fixtures throughout this suite were
written against the voyage geometry, so pin it — the suite must be identical
on every laptop and in CI. Individual tests that exercise the resolution
chain pass their own env dicts and stay unaffected.
"""

import os

os.environ["SESSION_RECALL_EMBED"] = "voyage"
os.environ.pop("SESSION_RECALL_LANG", None)
