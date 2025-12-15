import asyncio
import httpx
from httpx import ASGITransport
import main as app_main

# Enable dev bypass and replace supabase with in-memory fake
app_main.CLERK_DEV_BYPASS = True

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

async def run():
    transport = ASGITransport(app=app_main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        headers = {"X-DEV-USER": "sim-user-42"}
        r = await client.post("/api/user/initialize", headers=headers)
        print("status:", r.status_code)
        print("body:", r.json())

        users = app_main.supabase.table("users")
        print("inserted:", getattr(users, "_last_insert", None))

if __name__ == '__main__':
    asyncio.run(run())
