"""Netguard allows only slackcache-daemon schtasks operations."""

from __future__ import annotations

import pytest

from slack_cache_mcp import netguard


def test_schtasks_create_requires_xml_and_tn() -> None:
    netguard.install()
    with pytest.raises(netguard.NetworkGuardError):
        netguard._check_subprocess(  # noqa: SLF001
            ["schtasks", "/Create", "/TN", "other-task", "/XML", "x.xml", "/F"]
        )
    with pytest.raises(netguard.NetworkGuardError):
        netguard._check_subprocess(  # noqa: SLF001
            ["schtasks", "/Create", "/TN", "slackcache-daemon", "/F"]
        )
    # Allowed shape
    netguard._check_subprocess(  # noqa: SLF001
        ["schtasks", "/Create", "/TN", "slackcache-daemon", "/XML", "x.xml", "/F"]
    )
    netguard._check_subprocess(  # noqa: SLF001
        ["schtasks", "/Delete", "/TN", "slackcache-daemon", "/F"]
    )
    netguard._check_subprocess(["schtasks", "/Query", "/TN", "slackcache-daemon"])  # noqa: SLF001
