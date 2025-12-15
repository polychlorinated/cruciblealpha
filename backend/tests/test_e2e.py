import asyncio
import json
import os
import sys
import pytest
from datetime import datetime
import httpx
from httpx import ASGITransport
from fastapi import FastAPI

# Ensure backend package path is importable when running tests from repo root
ROOT = os.path.dirname(os.path.dirname(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import main as app_main

# Tests will create an AsyncClient with ASGI transport per-test
ASGI_TRANSPORT = ASGITransport(app=app_main.app)


def make_dummy_table():
    class Dummy:
        def __init__(self):
            self._data = []
        def update(self, obj):
            return self
        def insert(self, obj):
            # capture insert payload for assertions if needed
            self._last_insert = obj
            # persist into table data so select() can return it
            self._data.insert(0, obj)
            class Exec:
                def __init__(self, data):
                    self.data = [data]
                def execute(self):
                    return self
            return Exec(obj)
        def select(self, *args, **kwargs):
            outer = self
            class Exec:
                def __init__(self):
                    # return the table data (most recent first)
                    self.data = outer._data
                def eq(self, *a, **k):
                    return self
                def order(self, *a, **k):
                    return self
                def limit(self, *a, **k):
                    return self
                def execute(self):
                    return self
            return Exec()
        def eq(self, *a, **k):
            return self
        def execute(self):
            class R:
                data = []
            return R()

    return Dummy()


@pytest.fixture(autouse=True)
def patch_supabase(monkeypatch):
    # Patch supabase.table to avoid external calls
    # Provide stable table instances per table name so tests can inspect inserts
    class FakeSupabase:
        def __init__(self):
            self._tables = {}
        def table(self, name):
            if name not in self._tables:
                self._tables[name] = make_dummy_table()
            return self._tables[name]

    monkeypatch.setattr(app_main, "supabase", FakeSupabase())


@pytest.mark.asyncio
async def test_scan_preview_guest():
    payload = {
        "job_description": "Remote-first deep work protected role",
        "trauma": {
            "safety_baseline": 5,
            "adhd_wiring": 3,
            "capability": 5,
            "co_regulation": 5,
            "financial": 5
        },
        "consume_credit": False
    }

    async with httpx.AsyncClient(transport=ASGI_TRANSPORT, base_url="http://test") as client:
        r = await client.post("/api/scan-job", json=payload)
        assert r.status_code == 200
        data = r.json()
        assert data["preview_mode"] is True
        assert "overall_score" in data


@pytest.mark.asyncio
async def test_authenticated_scan_creates_module_and_initialize(monkeypatch):
    # Patch get_clerk_user to simulate an authenticated user
    async def fake_get_clerk_user(*args, **kwargs):
        return "test-user-123"

    monkeypatch.setattr(app_main, "get_clerk_user", fake_get_clerk_user)

    # Patch deduct_credit to always return True
    async def fake_deduct_credit(uid):
        return True
    monkeypatch.setattr(app_main, "deduct_credit", fake_deduct_credit)

    payload = {
        "job_description": "Remote-first deep work protected role",
        "trauma": {
            "safety_baseline": 5,
            "adhd_wiring": 3,
            "capability": 5,
            "co_regulation": 5,
            "financial": 5
        },
        "consume_credit": True
    }

    # Make the request with an Authorization header (value ignored by fake_get_clerk_user)
    async with httpx.AsyncClient(transport=ASGI_TRANSPORT, base_url="http://test") as client:
        r = await client.post("/api/scan-job", json=payload, headers={"Authorization": "Bearer fake"})
        assert r.status_code == 200
        data = r.json()
        assert data["preview_mode"] is False
        assert "overall_score" in data


@pytest.mark.asyncio
async def test_initialize_user_grants_credits(monkeypatch):
    async def fake_get_clerk_user(authorization=None):
        return "init-user-1"

    monkeypatch.setattr(app_main, "get_clerk_user", fake_get_clerk_user)

    async with httpx.AsyncClient(transport=ASGI_TRANSPORT, base_url="http://test") as client:
        r = await client.post("/api/user/initialize", headers={"Authorization": "Bearer fake"})
        assert r.status_code == 200
        data = r.json()
        assert data.get("credits") == 5

    # Verify a user record was inserted with 5 credits
    users_table = app_main.supabase.table("users")
    assert hasattr(users_table, "_last_insert")
    assert users_table._last_insert["user_id"] == "init-user-1"
    assert users_table._last_insert["credits_remaining"] == 5


@pytest.mark.asyncio
async def test_transfer_and_profile_visible(monkeypatch):
    async def fake_get_clerk_user(*args, **kwargs):
        return "transfer-user-1"

    monkeypatch.setattr(app_main, "get_clerk_user", fake_get_clerk_user)

    payload = {
        "survey": {"safety_baseline": 6},
        "job_description": "Remote-first role",
        "scan_results": {"overall_score": 88.5},
        "created_at": datetime.utcnow().isoformat()
    }

    async with httpx.AsyncClient(transport=ASGI_TRANSPORT, base_url="http://test") as client:
        r = await client.post("/api/user/transfer-pending-module", json=payload, headers={"Authorization": "Bearer fake"})
        assert r.status_code == 200
        assert r.json().get("success") is True

        # Now fetch profile
        r2 = await client.get("/api/user/profile", headers={"Authorization": "Bearer fake"})
        assert r2.status_code == 200
        profile = r2.json().get("profile")
        assert profile is not None
        assert profile.get("overall_score") == 88.5

    modules_table = app_main.supabase.table("modules")
    assert hasattr(modules_table, "_last_insert")
    assert modules_table._last_insert["user_id"] == "transfer-user-1"


@pytest.mark.asyncio
async def test_debug_whoami_endpoint(monkeypatch):
    # No auth header -> should return user_id null
    async with httpx.AsyncClient(transport=ASGI_TRANSPORT, base_url="http://test") as client:
        r = await client.get("/api/debug/whoami")
        assert r.status_code == 200
        assert r.json().get("user_id") is None

    # With auth (monkeypatch get_clerk_user), should return id
    async def fake_get_clerk(*args, **kwargs):
        return "whoami-user"
    monkeypatch.setattr(app_main, "get_clerk_user", fake_get_clerk)

    async with httpx.AsyncClient(transport=ASGI_TRANSPORT, base_url="http://test") as client:
        r = await client.get("/api/debug/whoami", headers={"Authorization": "Bearer fake"})
        assert r.status_code == 200
        assert r.json().get("user_id") == "whoami-user"


@pytest.mark.asyncio
async def test_dev_bypass_header(monkeypatch):
    # Enable dev bypass and call with X-DEV-USER header
    monkeypatch.setattr(app_main, "CLERK_DEV_BYPASS", True)

    async with httpx.AsyncClient(transport=ASGI_TRANSPORT, base_url="http://test") as client:
        r = await client.get("/api/debug/whoami", headers={"X-DEV-USER": "dev-user-1"})
        assert r.status_code == 200
        assert r.json().get("user_id") == "dev-user-1"


@pytest.mark.asyncio
async def test_dev_signup_flow(monkeypatch):
    # Use CLERK_DEV_BYPASS and the X-DEV-USER header to simulate a sign-up flow
    monkeypatch.setattr(app_main, "CLERK_DEV_BYPASS", True)

    async with httpx.AsyncClient(transport=ASGI_TRANSPORT, base_url="http://test") as client:
        # Step 1: initialize user via dev-bypass header
        r = await client.post("/api/user/initialize", headers={"X-DEV-USER": "devflow-user-1"})
        assert r.status_code == 200
        assert r.json().get("credits") == 5

        # Step 2: transfer a pending guest module
        payload = {
            "survey": {"safety_baseline": 6},
            "job_description": "Remote-first role",
            "scan_results": {"overall_score": 88.5},
            "created_at": datetime.utcnow().isoformat()
        }
        r2 = await client.post("/api/user/transfer-pending-module", headers={"X-DEV-USER": "devflow-user-1"}, json=payload)
        assert r2.status_code == 200

        # Step 3: fetch profile — should be visible
        r3 = await client.get("/api/user/profile", headers={"X-DEV-USER": "devflow-user-1"})
        assert r3.status_code == 200
        profile = r3.json().get("profile")
        assert profile is not None
        assert profile.get("overall_score") == 88.5
