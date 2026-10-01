import socket
from pathlib import Path

import pytest

from pdf_notion_mvp.contracts import FixtureInput


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("network is forbidden in offline tests")
    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)


@pytest.fixture
def source():
    return FixtureInput.model_validate_json((Path(__file__).parents[1] / "fixtures/synthetic.json").read_text())
