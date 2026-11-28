"""Multi-device sync (stage 1.5 local bundles; stage 2 HTTP lives here later).

Stage 1.5 never contacts Slack or any remote server — only local folder I/O
and age/Ed25519 crypto. HTTP clients for stage 2 must stay confined to this
package (import-linter / AST scan allowlist).
"""

from slack_cache_mcp.sync.bundle import (
    ensure_local_device,
    export_bundle,
    import_bundles,
    register_peer,
)
from slack_cache_mcp.sync.device import DeviceIdentity, DevicePublicInfo, load_or_create_device
from slack_cache_mcp.sync.onedrive import (
    EXCHANGE_DIR_ENV,
    default_exchange_dir,
    ensure_exchange_dir,
    exchange_status,
)
from slack_cache_mcp.sync.state_hash import compute_state_hash

__all__ = [
    "EXCHANGE_DIR_ENV",
    "DeviceIdentity",
    "DevicePublicInfo",
    "compute_state_hash",
    "default_exchange_dir",
    "ensure_exchange_dir",
    "ensure_local_device",
    "exchange_status",
    "export_bundle",
    "import_bundles",
    "load_or_create_device",
    "register_peer",
]
