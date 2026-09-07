"""Notes store + brain REST surface + notification feed."""

from __future__ import annotations

from pathlib import Path

import pytest
from starlette.testclient import TestClient

from prana.brain.api import create_app
from prana.brain.config import BrainConfig
from prana.notes import store


@pytest.fixture(autouse=True)
def temp_home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    yield tmp_path


def test_store_crud_roundtrip():
    n = store.create_note("Shopping", kind="checklist",
                          items=["milk", "eggs", " "])
    assert [i["text"] for i in n["items"]] == ["milk", "eggs"]
    got = store.get_note(n["id"])
    assert got is not None and got["title"] == "Shopping"

    item_id = n["items"][0]["id"]
    updated = store.update_note(n["id"], check={item_id: True},
                                add_items=["bread"])
    assert updated["items"][0]["done"] is True
    assert updated["items"][-1]["text"] == "bread"

    updated = store.update_note(n["id"], remove_item_ids=[item_id])
    assert all(i["id"] != item_id for i in updated["items"])

    assert store.delete_note(n["id"]) is True
    assert store.delete_note(n["id"]) is False


def test_store_validation():
    with pytest.raises(ValueError):
        store.create_note("  ")
    with pytest.raises(ValueError):
        store.create_note("x", kind="drawing")
    with pytest.raises(KeyError):
        store.update_note("nope", title="y")


def test_list_orders_by_recency():
    import time
    a = store.create_note("first")
    time.sleep(1.1)  # updated_at has second resolution
    store.create_note("second")
    time.sleep(1.1)
    store.update_note(a["id"], text="bumped")
    assert [n["title"] for n in store.list_notes()][:2] == ["first", "second"]


# ── REST surface ────────────────────────────────────────────────────────


class _NoBackend:
    def __init__(self, **kw):  # pragma: no cover
        raise AssertionError("notes endpoints must not touch the backend")


@pytest.fixture
def client(tmp_path, monkeypatch):
    wake = tmp_path / "wake-context.md"
    wake.write_text("ctx", encoding="utf-8")
    config = BrainConfig(sessions_root=tmp_path / "sessions",
                        wake_context=wake)
    monkeypatch.setattr("prana.brain.api.load_brain_tokens",
                        lambda: {"app": "tok-app"})
    app = create_app(config, _NoBackend)
    with TestClient(app) as c:
        yield c


HEADERS = {"Authorization": "Bearer tok-app"}


def test_rest_roundtrip(client):
    assert client.get("/api/notes").status_code == 401
    res = client.post("/api/notes", headers=HEADERS,
                      json={"title": "Shopping", "kind": "checklist",
                            "items": ["milk"]})
    assert res.status_code == 200
    note = res.json()
    item_id = note["items"][0]["id"]

    res = client.patch(f"/api/notes/{note['id']}", headers=HEADERS,
                       json={"check": {item_id: True},
                             "add_items": ["butter"]})
    assert res.status_code == 200
    assert res.json()["items"][0]["done"] is True

    listing = client.get("/api/notes", headers=HEADERS).json()
    assert listing[0]["title"] == "Shopping"

    assert client.delete(f"/api/notes/{note['id']}",
                         headers=HEADERS).status_code == 200
    assert client.patch("/api/notes/nope", headers=HEADERS,
                        json={"title": "x"}).status_code == 404


def test_notification_feed(client):
    from prana.brain import push

    push._log_notification("Narada", "morning message")
    push._log_notification("Narada", "deploy finished")
    feed = client.get("/api/notifications", headers=HEADERS).json()
    assert [n["body"] for n in feed] == ["morning message", "deploy finished"]
    assert client.get("/api/notifications").status_code == 401
