import json
from typing import Any, Optional

from sqler.adapter.asynchronous import AsyncSQLiteAdapter


class AsyncSQLerDB:
    """Async document store for JSON blobs on SQLite."""

    @classmethod
    def in_memory(cls, shared: bool = True) -> "AsyncSQLerDB":
        adapter = AsyncSQLiteAdapter.in_memory(shared=shared)
        return cls(adapter)

    @classmethod
    def on_disk(cls, path: str = "sqler.db") -> "AsyncSQLerDB":
        adapter = AsyncSQLiteAdapter.on_disk(path)
        return cls(adapter)

    def __init__(self, adapter: AsyncSQLiteAdapter):
        self.adapter = adapter

    async def connect(self) -> None:
        await self.adapter.connect()

    async def close(self) -> None:
        await self.adapter.close()

    async def _ensure_table(self, table: str) -> None:
        ddl = f"""
        CREATE TABLE IF NOT EXISTS {table} (
            _id INTEGER PRIMARY KEY AUTOINCREMENT,
            data JSON NOT NULL
        );
        """
        await self.adapter.execute(ddl)
        await self.adapter.commit()

    async def insert_document(self, table: str, doc: dict[str, Any]) -> int:
        await self._ensure_table(table)
        payload = json.dumps(doc)
        cur = await self.adapter.execute(f"INSERT INTO {table} (data) VALUES (json(?));", [payload])
        await self.adapter.commit()
        last_id = cur.lastrowid  # type: ignore[attr-defined]
        await cur.close()
        return last_id

    async def upsert_document(self, table: str, _id: Optional[int], doc: dict[str, Any]) -> int:
        await self._ensure_table(table)
        payload = json.dumps(doc)
        if _id is None:
            return await self.insert_document(table, doc)
        cur = await self.adapter.execute(
            f"UPDATE {table} SET data = json(?) WHERE _id = ?;", [payload, _id]
        )
        await self.adapter.commit()
        await cur.close()
        return _id

    async def find_document(self, table: str, _id: int) -> Optional[dict[str, Any]]:
        await self._ensure_table(table)
        cur = await self.adapter.execute(f"SELECT _id, data FROM {table} WHERE _id = ?;", [_id])
        row = await cur.fetchone()
        await cur.close()
        if not row:
            return None
        obj = json.loads(row[1])
        obj["_id"] = row[0]
        return obj

    # ---- versioned (optimistic locking) helpers ----
    async def _ensure_versioned_table(self, table: str) -> None:
        await self._ensure_table(table)
        cur = await self.adapter.execute(f"PRAGMA table_info({table});")
        cols = [row[1] for row in await cur.fetchall()]
        await cur.close()
        if "_version" not in cols:
            cur2 = await self.adapter.execute(
                f"ALTER TABLE {table} ADD COLUMN _version INTEGER NOT NULL DEFAULT 0;"
            )
            await self.adapter.commit()
            await cur2.close()

    async def upsert_with_version(
        self, table: str, _id: Optional[int], doc: dict[str, Any], expected_version: Optional[int]
    ) -> tuple[int, int]:
        await self._ensure_versioned_table(table)
        payload = json.dumps(doc)
        if _id is None:
            cur = await self.adapter.execute(
                f"INSERT INTO {table} (data, _version) VALUES (json(?), 0);",
                [payload],
            )
            await self.adapter.commit()
            last_id = cur.lastrowid  # type: ignore[attr-defined]
            await cur.close()
            return last_id, 0
        if expected_version is None:
            raise ValueError("expected_version required for update")
        cur = await self.adapter.execute(
            f"UPDATE {table} SET data = json(?), _version = _version + 1 WHERE _id = ? AND _version = ?;",
            [payload, _id, expected_version],
        )
        await self.adapter.commit()
        await cur.close()
        # Check changes() to confirm update actually happened
        ch = await self.adapter.execute("SELECT changes();")
        row = await ch.fetchone()
        await ch.close()
        if not row or int(row[0]) == 0:
            raise RuntimeError("Stale version: update rejected")
        return _id, expected_version + 1

    async def find_document_with_version(self, table: str, _id: int) -> Optional[dict[str, Any]]:
        await self._ensure_versioned_table(table)
        cur = await self.adapter.execute(
            f"SELECT _id, data, _version FROM {table} WHERE _id = ?;",
            [_id],
        )
        row = await cur.fetchone()
        await cur.close()
        if not row:
            return None
        obj = json.loads(row[1])
        obj["_id"] = row[0]
        obj["_version"] = row[2]
        return obj

    async def query(self, table: str):
        """Convenience: return an AsyncSQLerQuery bound to this adapter."""
        from sqler.query.async_query import AsyncSQLerQuery

        await self._ensure_table(table)
        return AsyncSQLerQuery(table=table, adapter=self.adapter)
