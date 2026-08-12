"""Shared test doubles and fixture loaders.

Nothing here touches the network: the FFBad site and the Strapi API are both
reached through an ``httpx.AsyncClient``-shaped object injected into
``InterclubUpdate.__init__`` via ``client_factory``, and this module supplies
the fake for it.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

FIXTURES = Path(__file__).parent / "fixtures"


def fixture_text(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


def fixture_json(name):
    return json.loads(fixture_text(name))


class FakeResponse:
    """Stand-in for httpx.Response covering only what the function reads."""

    def __init__(self, text="", json_body=None, status_code=200):
        self.text = text
        self._json_body = json_body
        self.status_code = status_code

    def json(self):
        return self._json_body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise AssertionError(f"HTTP {self.status_code}")
        return self


class FakeClient:
    """Async stub for httpx.AsyncClient; responses consumed in call order."""

    def __init__(self, get_responses=None, put_responses=None, post_responses=None):
        self.get_responses = list(get_responses or [])
        self.put_responses = list(put_responses or [])
        self.post_responses = list(post_responses or [])
        self.get_calls = []
        self.put_calls = []
        self.post_calls = []
        self.closed = False

    async def get(self, url, params=None, headers=None):
        self.get_calls.append({"url": url, "params": params, "headers": headers})
        if not self.get_responses:
            raise AssertionError(f"Unexpected GET call to {url} params={params}")
        return self.get_responses.pop(0)

    async def put(self, url, json=None, headers=None):
        self.put_calls.append({"url": url, "json": json, "headers": headers})
        if not self.put_responses:
            raise AssertionError(f"Unexpected PUT call to {url}")
        return self.put_responses.pop(0)

    async def post(self, url, json=None, headers=None):
        self.post_calls.append({"url": url, "json": json, "headers": headers})
        if not self.post_responses:
            raise AssertionError(f"Unexpected POST call to {url}")
        return self.post_responses.pop(0)

    async def aclose(self):
        self.closed = True


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Fail loudly if any test opens a real socket.

    The suite is meant to be hermetic; a missing injected factory would
    otherwise quietly reach icbad.ffbad.org or the Strapi API.
    """
    import socket

    def blocked(*args, **kwargs):
        raise AssertionError(
            "Test attempted a real network connection — inject a fake factory instead"
        )

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
