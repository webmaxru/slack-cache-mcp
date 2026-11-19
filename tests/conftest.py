"""Shared fixtures — synthetic data only."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.fixture_gen.build_profile import build_synthetic_profile


@pytest.fixture(scope="session")
def synthetic_profile(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("synth-profile")
    return build_synthetic_profile(root)


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    d = tmp_path / "slackcache-data"
    d.mkdir()
    return d
