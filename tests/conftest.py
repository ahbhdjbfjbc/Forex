"""Shared test fixtures.

The publish gate (src/publish_gate.py) fetches higher-timeframe data, the
economic calendar and DXY over the network. Pipeline tests that are not about
the gate get a permissive gate so they stay offline and deterministic; the
gate's own tests mark themselves with `real_publish_gate` to use the real one.
"""
import os
import tempfile

import pytest

# Keep any module-level SQLite/JSON state out of the repository's data dir.
os.environ.setdefault("FOREX_DATA_DIR", tempfile.mkdtemp(prefix="forex-tests-"))


def pytest_configure(config):
    config.addinivalue_line("markers", "real_publish_gate: use the real publish gate")


@pytest.fixture(autouse=True)
def _permissive_publish_gate(request, monkeypatch):
    if request.node.get_closest_marker("real_publish_gate"):
        return
    from src import publish_gate

    monkeypatch.setattr(
        publish_gate, "check_publish_allowed",
        lambda *_args, **_kwargs: publish_gate.GateDecision(True),
    )
