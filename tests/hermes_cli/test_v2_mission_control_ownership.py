"""Mission Control requests against disposable stores and independent principals."""

import asyncio
import time
from types import SimpleNamespace

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
import httpx
import pytest

from gateway.owner_identity import host_owner_subject
from gateway.runtime_bootstrap import TicketStore
from gateway.session_identity import authenticated_subject
from hermes_cli.dashboard_auth.base import Session
from hermes_state import SessionDB


ALICE_ID = "alice-disposable-session"
BOB_ID = "bob-disposable-session"
ALICE_TEXT = "ownershipfilter alpha transcript"
BOB_TEXT = "ownershipfilter beta private failure"
ISSUER = "https://identity.fixture.invalid"
PROVIDER = "mission-fixture"


def subject(user):
    return authenticated_subject({"user_id": user, "provider": PROVIDER, "issuer": ISSUER})


@pytest.fixture
def state(tmp_path, monkeypatch):
    home = tmp_path / "user-home"
    state = tmp_path / "hermes-state"
    home.mkdir()
    state.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HERMES_HOME", str(state))
    monkeypatch.setattr("hermes_state.DEFAULT_DB_PATH", state / "state.db")
    with SessionDB(db_path=state / "state.db") as db:
        for user, sid, text in (("alice", ALICE_ID, ALICE_TEXT), ("bob", BOB_ID, BOB_TEXT)):
            db.create_session(sid, source="cli", user_id=subject(user), profile_name="default")
            db.append_message(sid, "assistant", text)
            db.append_message(sid, "assistant", f"response for {user}")
    return state


@pytest.fixture
def native_dashboard(state, monkeypatch):
    from hermes_cli import web_server

    instance = "v2-06-disposable-daemon"
    profile = str(state)
    authority = SimpleNamespace(profile_id=profile, instance_id=instance,
                                db=SimpleNamespace(db_path=state / "state.db"))
    store = TicketStore(instance, frozenset({profile}))
    runner = SimpleNamespace(session_authority=authority, session_ticket_store=store, _draining=False,
                             session_runtime_descriptor={"state": "ready", "instance_id": instance,
                                                         "served_profiles": [{"home": profile, "profile_id": profile}]})
    monkeypatch.setattr(web_server.app.state, "gateway_runner", runner, raising=False)
    monkeypatch.setattr(web_server.app.state, "auth_required", False, raising=False)
    monkeypatch.setattr(web_server.app.state, "bound_host", "127.0.0.1", raising=False)
    monkeypatch.setattr(web_server, "_SESSION_TOKEN", "hud-host-fixture")

    async def app(scope, receive, send):
        # A native HTTP request reaches the app through the verified loopback
        # gateway listener. ASGITransport has no listener to supply its peer fact.
        await web_server.app(dict(scope, **{"hermes.gateway_socket_peer": ("127.0.0.1", 49152)}),
                             receive, send)

    def headers(user):
        return {"X-Hermes-Gateway-Ticket": store.mint(
            profile_id=profile, subject=subject(user), purpose="native-http")}

    client = TestClient(app, base_url="http://127.0.0.1")
    yield client, headers
    client.close()


def test_native_owner_can_browse_search_and_export_only_their_session(native_dashboard):
    client, headers = native_dashboard
    assert client.get("/api/sessions").status_code == 401
    for user, own, foreign, own_text, foreign_text in (
        ("alice", ALICE_ID, BOB_ID, ALICE_TEXT, BOB_TEXT),
        ("bob", BOB_ID, ALICE_ID, BOB_TEXT, ALICE_TEXT),
    ):
        listing = client.get("/api/sessions?limit=1&archived=include&order=recent", headers=headers(user))
        assert listing.status_code == 200, listing.text
        assert listing.json()["total"] == 1
        assert [item["id"] for item in listing.json()["sessions"]] == [own]
        aggregate = client.get("/api/profiles/sessions?profile=default", headers=headers(user))
        assert [item["id"] for item in aggregate.json()["sessions"]] == [own]
        stats = client.get("/api/sessions/stats", headers=headers(user)).json()
        assert stats["total"] == 1 and stats["messages"] == 2
        result = client.get("/api/sessions/search?q=ownershipfilter", headers=headers(user)).json()
        assert [item["session_id"] for item in result["results"]] == [own], result
        for suffix in ("", "/messages", "/timeline", "/messages/around?row_id=1", "/latest-descendant", "/export"):
            denied = client.get(f"/api/sessions/{foreign}{suffix}", headers=headers(user))
            missing = client.get(f"/api/sessions/no-such-session{suffix}", headers=headers(user))
            assert denied.status_code == missing.status_code == 404, denied.text
            assert denied.json() == missing.json()
        assert client.get(f"/api/sessions/{own}", headers=headers(user)).json()["id"] == own
        messages = client.get(f"/api/sessions/{own}/messages", headers=headers(user))
        assert messages.status_code == 200 and own_text in messages.text and foreign_text not in messages.text
        exported = client.get(f"/api/sessions/{own}/export", headers=headers(user))
        assert exported.status_code == 200 and own_text in exported.text and foreign_text not in exported.text
    hud = client.get("/api/sessions", headers={"Authorization": "Bearer hud-host-fixture"})
    assert hud.status_code == 200 and hud.json()["total"] == 2
    grant = headers("alice")
    assert client.get(f"/api/sessions/{ALICE_ID}", headers=grant).status_code == 200
    assert client.get(f"/api/sessions/{ALICE_ID}", headers=grant).status_code == 401


def test_sidebar_cache_cannot_reuse_another_principals_projection(native_dashboard):
    client, headers = native_dashboard
    endpoint = "/api/profiles/sessions/sidebar?recents_profile=default"
    for user, own, foreign in (("alice", ALICE_ID, BOB_ID), ("bob", BOB_ID, ALICE_ID), ("alice", ALICE_ID, BOB_ID)):
        sidebar = client.get(endpoint, headers=headers(user))
        assert sidebar.status_code == 200, sidebar.text
        assert own in sidebar.text and foreign not in sidebar.text


@pytest.fixture
def standalone_app(state, monkeypatch):
    from hermes_cli import backend_retirement
    from hermes_cli.web_routers.sessions import list_router, manage_router
    from hermes_cli.web_routers.status import router as status_router
    from hermes_cli import web_server_idle_proof

    monkeypatch.setattr(backend_retirement, "retirement", backend_retirement.RetirementFence())
    monkeypatch.setattr(web_server_idle_proof, "idle_proof", lambda: {"idle": True})
    app = FastAPI()
    app.state.auth_required = True
    app.include_router(list_router)
    app.include_router(manage_router)
    app.include_router(status_router)

    # Distinct, predetermined fixture identities at the verified OAuth session
    # seam. The client can present a credential only, never a claimed subject.
    def login(user, provider):
        return Session(user, "", "", "", provider, int(time.time()) + 300, "", "", ISSUER)
    verified = {"fixture-a": login("alice", PROVIDER), "fixture-b": login("bob", PROVIDER),
                "fixture-host": login(host_owner_subject(), "session-token")}

    @app.middleware("http")
    async def verify_fixture_identity(request: Request, call_next):
        request.state.session = verified.get(request.headers.get("authorization"))
        return await call_next(request)

    return app


def test_standalone_mutation_checks_owner_before_revision_and_preserves_state(standalone_app, state):
    alice = {"Authorization": "fixture-a"}
    bob = {"Authorization": "fixture-b"}
    with TestClient(standalone_app) as client:
        write = {"request_id": "rename-fixture", "expected_revision": 0, "title": "Alice renamed"}
        denied = client.patch(f"/api/sessions/{ALICE_ID}", headers=bob, json=write | {"expected_revision": 44})
        assert denied.status_code == 404, denied.text
        snap = client.get(f"/api/sessions/{ALICE_ID}/mutation-snapshot", headers=bob)
        assert snap.status_code == 200 and not snap.json()["exists"]
        assert client.post("/api/sessions/bulk-delete", headers=bob, json={"ids": [ALICE_ID]}).status_code == 403
        renamed = client.patch(f"/api/sessions/{ALICE_ID}", headers=alice, json=write)
        assert renamed.status_code == 200, renamed.text
        assert client.patch(f"/api/sessions/{ALICE_ID}", headers=alice, json=write).json() == renamed.json()
        stale = client.patch(f"/api/sessions/{ALICE_ID}", headers=alice,
                             json=write | {"request_id": "stale-fixture"})
        assert stale.status_code == 409 and stale.json()["detail"] == "revision_conflict"
    with SessionDB(db_path=state / "state.db", read_only=True) as db:
        assert db.get_session(ALICE_ID)["title"] == "Alice renamed"
        assert db.get_session(BOB_ID)["runtime_revision"] == 0


def test_retirement_fences_concurrent_clients_without_hiding_owner_reads(standalone_app, monkeypatch):
    asyncio.run(_exercise_retirement(standalone_app, monkeypatch))


async def _exercise_retirement(standalone_app, monkeypatch):
    from hermes_cli import web_server_sessions

    started, release = asyncio.Event(), asyncio.Event()
    original = web_server_sessions._mutate_via_gateway_owner

    async def wait_then_write(*args, **kwargs):
        started.set()
        await release.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(web_server_sessions, "_mutate_via_gateway_owner", wait_then_write)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=standalone_app),
                                 base_url="http://dashboard.fixture") as client:
        alice = {"Authorization": "fixture-a"}
        host = {"Authorization": "fixture-host"}
        write = {"request_id": "retirement-race", "expected_revision": 0, "title": "Saved before retirement"}
        task = asyncio.create_task(client.patch(f"/api/sessions/{ALICE_ID}", headers=alice, json=write))
        try:
            await asyncio.wait_for(started.wait(), 5)
            prepare = {"action": "prepare"}
            assert (await client.post("/api/health/retirement", headers=alice, json=prepare)).status_code == 403
            blocked = await client.post("/api/health/retirement", headers=host, json=prepare)
            assert blocked.json() == {"ok": False, "idle": False}
        finally:
            release.set()
        assert (await asyncio.wait_for(task, 10)).status_code == 200
        prepared = await client.post("/api/health/retirement", headers=host, json=prepare)
        permit = prepared.json()
        assert permit["ok"] and permit["idle"]
        refused = await client.patch(f"/api/sessions/{ALICE_ID}", headers=alice, json=write)
        assert refused.status_code == 503 and refused.json()["detail"] == "runtime_draining"
        still_readable = await client.get("/api/sessions", headers=alice)
        assert [row["id"] for row in still_readable.json()["sessions"]] == [ALICE_ID]
        assert (await client.post("/api/health/retirement", headers=host,
                                  json={"action": "cancel", "token": permit["token"]})).json() == {"ok": True}
