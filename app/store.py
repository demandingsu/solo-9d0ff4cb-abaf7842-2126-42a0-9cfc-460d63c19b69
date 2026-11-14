"""快照存储：核查结果整版入库，成功后原子替换现役版本，失败保留旧版。

- 每个快照自包含（源清单两版 + 依赖 + 报告），查询只读取 state='active' 的唯一版本，
  因此任何查询都不会混用两版数据。
- 新版本的解析/评估全部在写事务之外完成；替换仅一条事务内的
  UPDATE(archived) + INSERT(active)，失败回滚则旧版原样保留。
- WAL 模式保证核查（写入）进行时，旧版仍可被并发读取。
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    created_at TEXT NOT NULL,
    state      TEXT NOT NULL CHECK (state IN ('active', 'archived', 'failed')),
    error      TEXT,
    payload    TEXT
);
CREATE INDEX IF NOT EXISTS idx_snapshots_state ON snapshots(state, id);
"""


class SnapshotStore:
    def __init__(self, path: str):
        self.path = path
        parent = os.path.dirname(os.path.abspath(path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._lock = threading.Lock()
        conn = self._conn()
        try:
            conn.executescript(SCHEMA)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.commit()
        finally:
            conn.close()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")

    # -- 写 ---------------------------------------------------------------

    def publish(self, name: str, payload: dict[str, Any]) -> int:
        """整版替换：新快照置为 active，旧 active 置为 archived。事务失败则旧版保留。"""
        blob = json.dumps(payload, ensure_ascii=False)
        with self._lock:
            conn = self._conn()
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute("UPDATE snapshots SET state='archived' WHERE state='active'")
                cur = conn.execute(
                    "INSERT INTO snapshots(name, created_at, state, payload) VALUES (?,?, 'active', ?)",
                    (name, self._now(), blob))
                conn.commit()
                return int(cur.lastrowid)
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    def record_failed(self, name: str, error: str) -> int:
        """核查失败留痕，不影响现役版本。"""
        with self._lock:
            conn = self._conn()
            try:
                cur = conn.execute(
                    "INSERT INTO snapshots(name, created_at, state, error) VALUES (?,?, 'failed', ?)",
                    (name or "(未命名)", self._now(), error))
                conn.commit()
                return int(cur.lastrowid)
            finally:
                conn.close()

    # -- 读（只读现役版本，绝不混版） --------------------------------------

    def current(self) -> Optional[dict[str, Any]]:
        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT id, name, created_at, payload FROM snapshots "
                "WHERE state='active' ORDER BY id DESC LIMIT 1").fetchone()
            if not row:
                return None
            payload = json.loads(row["payload"])
            return {"id": row["id"], "name": row["name"],
                    "created_at": row["created_at"], "payload": payload}
        finally:
            conn.close()

    def history(self, limit: int = 20) -> list[dict[str, Any]]:
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT id, name, created_at, state, error FROM snapshots "
                "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()
