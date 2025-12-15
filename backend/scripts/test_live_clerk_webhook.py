import asyncio
import json
from httpx import ASGITransport, AsyncClient
import main as app_main

# Use in-process fake Supabase
class InMemoryTable:
    def __init__(self):
        self._data = []
    def update(self, obj):
        return self
    def insert(self, obj):
        self._last_insert = obj
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

class FakeSupabase:
    def __init__(self):
        self._tables = {}
    def table(self, name):
        if name not in self._tables:
            self._tables[name] = InMemoryTable()
        return self._tables[name]

app_main.supabase = FakeSupabase()
app_main.CLERK_DEV_BYPASS = True

async def run():
    transport = ASGITransport(app=app_main.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        payload = {
            "type": "user.created",
            "data": {
                "id": "clerk-live-1",
                "email_addresses": [{"email_address": "alice@example.com"}]
            }
        }

        r = await client.post("/webhooks/clerk", json=payload)
        print("webhook status", r.status_code, r.text)

        # Check users table
        users = app_main.supabase.table("users")
        print("users._last_insert =", getattr(users, "_last_insert", None))

        # Use dev-bypass header to check credits via API
        r2 = await client.get("/api/user/credits", headers={"X-DEV-USER": "clerk-live-1"})
        print("credits API ->", r2.status_code, r2.json())

if __name__ == '__main__':
    asyncio.run(run())
