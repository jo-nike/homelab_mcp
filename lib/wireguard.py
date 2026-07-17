"""Shared WireGuard (wg-easy) peer connection logic.

``tools/wireguard`` and ``lib/refresh`` both decided whether a peer is connected
from its last-handshake time; refresh used a literal 180 and skipped the
never-connected sentinel. This owns the single implementation.
"""

from datetime import UTC, datetime

# 3 minutes -- peers with a handshake more recent than this are connected.
CONNECTED_THRESHOLD_SECONDS = 180

# wg-easy sentinel value for "never connected".
WG_NEVER_CONNECTED = "0001-01-01T00:00:00.000Z"


def is_connected(handshake_at: str | None) -> bool:
    """True if the last handshake was within CONNECTED_THRESHOLD_SECONDS.

    Handles None and the wg-easy 'never connected' sentinel.
    """
    if not handshake_at or handshake_at == WG_NEVER_CONNECTED:
        return False
    try:
        handshake_time = datetime.fromisoformat(handshake_at.replace("Z", "+00:00"))
        elapsed = (datetime.now(UTC) - handshake_time).total_seconds()
        return elapsed < CONNECTED_THRESHOLD_SECONDS
    except (ValueError, TypeError):
        return False
