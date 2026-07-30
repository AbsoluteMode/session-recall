"""P2P recall sharing — pairing, trust and envelopes.

Spec: docs/decisions/2026-07-30-p2p-sharing-v1-security-gate.md
"""

from pathlib import Path

from .. import config


def share_dir() -> Path:
    """Resolved lazily so tests (and a relocated XDG home) can repoint
    config.DATA_DIR without re-importing the package."""
    return config.DATA_DIR / "share"
