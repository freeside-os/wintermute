"""Unified SQLite persistence and hybrid FTS5 + vector knowledge base adapter."""

from __future__ import annotations

import json
import math
import re
import struct
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import aiosqlite

from app.application.knowledge_service import compute_deterministic_embedding
from app.application.ports import StoragePort
from app.domain.cost import CostLedgerEntry, ModelPricing, TokenUsage
from app.domain.models import (
    AgentRole,
    KnowledgeEntry,
    SubagentRun,
    Task,
    TaskEvent,
    TaskMessage,
    TaskStatus,
    WorkUnit,
    utcnow_iso,
)

_FTS_TOKEN_RE = re.compile(r"[A-Za-z0-9_./:-]+")


def sanitize_fts5_query(query: str) -> str:
    """Convert raw compiler/error text into a safe FTS5 OR MATCH query."""
    tokens: list[str] = []
    seen: set[str] = set()
    for raw in _FTS_TOKEN_RE.findall(query):
        # Split paths/colons as sub-tokens too while preserving exact flag/file tokens
        cleaned = raw.strip('":()[]{}')
        if len(cleaned) < 2:
            continue
        for candidate in [cleaned, cleaned.split(":", 1)[0], *re.split(r"[:/]", cleaned)]:
            c = candidate.strip()
            if len(c) >= 2 and c not in seen:
                seen.add(c)
                escaped = c.replace('"', '""')
                tokens.append(f'"{escaped}"')
    if not tokens:
        return ""
    return " OR ".join(tokens[:24])


def _cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def _pack_f32(vec: Sequence[float]) -> bytes:
    return struct.pack(f"<{len(vec)}f", *vec)


@dataclass(slots=True)
class SQLiteStorageAdapter(StoragePort):
    db_path: Path
    embedding_dimensions: int = 768
    _vec_enabled: bool = False

    @asynccontextmanager
    async def _connect(self) -> AsyncIterator[aiosqlite.Connection]:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(str(self.db_path), timeout=10.0) as conn:
            conn.row_factory = aiosqlite.Row
            await conn.execute("PRAGMA busy_timeout=5000;")
            await conn.execute("PRAGMA foreign_keys=ON;")
            await self._try_load_sqlite_vec(conn)
            yield conn

    async def _try_load_sqlite_vec(self, conn: aiosqlite.Connection) -> None:
        try:
            import sqlite_vec  # type: ignore[import-not-found]

            raw_conn = conn._conn
            if hasattr(raw_conn, "enable_load_extension"):
                raw_conn.enable_load_extension(True)
                sqlite_vec.load(raw_conn)
                raw_conn.enable_load_extension(False)
                self._vec_enabled = True
        except Exception:
            self._vec_enabled = False

    async def initialize(self) -> None:
        async with self._connect() as conn:
            await conn.execute("PRAGMA journal_mode=WAL;")
            await conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    prompt TEXT NOT NULL,
                    status TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    domain TEXT,
                    target TEXT,
                    pipeline_template TEXT,
                    conversation_id TEXT,
                    worktree_path TEXT,
                    branch_name TEXT,
                    touched_repos_json TEXT NOT NULL DEFAULT '[]',
                    max_usd REAL,
                    extra_usd_granted REAL NOT NULL DEFAULT 0.0,
                    pending_question TEXT,
                    review_summary TEXT,
                    error_message TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS work_units (
                    id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    domain TEXT NOT NULL,
                    target TEXT,
                    pipeline_template TEXT NOT NULL,
                    complexity TEXT NOT NULL,
                    skills_json TEXT NOT NULL DEFAULT '[]',
                    prompt TEXT NOT NULL,
                    current_stage_idx INTEGER NOT NULL DEFAULT 0,
                    current_stage_name TEXT NOT NULL DEFAULT '',
                    gate_attempts_json TEXT NOT NULL DEFAULT '{}',
                    last_file_modified_at REAL NOT NULL DEFAULT 0.0,
                    last_gate_passed_at REAL NOT NULL DEFAULT 0.0,
                    commit_sha TEXT,
                    status TEXT NOT NULL,
                    summary TEXT NOT NULL DEFAULT '',
                    FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS subagent_runs (
                    id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    work_unit_id TEXT,
                    role TEXT NOT NULL,
                    model_alias TEXT NOT NULL,
                    model_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    prompt TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    prompt_tokens INTEGER NOT NULL DEFAULT 0,
                    cached_tokens INTEGER NOT NULL DEFAULT 0,
                    candidate_tokens INTEGER NOT NULL DEFAULT 0,
                    thoughts_tokens INTEGER NOT NULL DEFAULT 0,
                    total_tokens INTEGER NOT NULL DEFAULT 0,
                    usd_cost REAL NOT NULL DEFAULT 0.0,
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS task_events (
                    id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    source TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    content TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    timestamp TEXT NOT NULL,
                    FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS task_messages (
                    id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    sender TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS cost_ledger (
                    id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    subagent_run_id TEXT,
                    role TEXT NOT NULL,
                    model_id TEXT NOT NULL,
                    prompt_tokens INTEGER NOT NULL,
                    cached_tokens INTEGER NOT NULL,
                    candidate_tokens INTEGER NOT NULL,
                    thoughts_tokens INTEGER NOT NULL,
                    total_tokens INTEGER NOT NULL,
                    usd_cost REAL NOT NULL,
                    timestamp TEXT NOT NULL,
                    FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS model_pricing_cache (
                    model_id TEXT PRIMARY KEY,
                    input_cost_per_1m REAL NOT NULL,
                    cached_input_cost_per_1m REAL NOT NULL,
                    output_cost_per_1m REAL NOT NULL,
                    thinking_cost_per_1m REAL NOT NULL,
                    source TEXT NOT NULL,
                    fetched_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS knowledge_entries (
                    id TEXT PRIMARY KEY,
                    domain TEXT NOT NULL,
                    target TEXT NOT NULL,
                    issue TEXT NOT NULL,
                    resolution TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    capture_type TEXT NOT NULL,
                    embedding_json TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS runtime_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts USING fts5(
                    entry_id UNINDEXED,
                    domain,
                    target,
                    issue,
                    resolution
                );
                """
            )
            if self._vec_enabled:
                await conn.execute(
                    f"""
                    CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_vec USING vec0(
                        entry_id TEXT PRIMARY KEY,
                        embedding float[{self.embedding_dimensions}]
                    );
                    """
                )
            cursor = await conn.execute("PRAGMA table_info(work_units)")
            cols = {row[1] for row in await cursor.fetchall()}
            if "skills_json" not in cols:
                await conn.execute(
                    "ALTER TABLE work_units ADD COLUMN skills_json TEXT NOT NULL DEFAULT '[]'"
                )
            await conn.commit()

    def _row_to_task(self, row: aiosqlite.Row) -> Task:
        return Task(
            id=row["id"],
            title=row["title"],
            prompt=row["prompt"],
            status=TaskStatus(row["status"]),
            mode=row["mode"],
            domain=row["domain"],
            target=row["target"],
            pipeline_template=row["pipeline_template"],
            conversation_id=row["conversation_id"],
            worktree_path=row["worktree_path"],
            branch_name=row["branch_name"],
            touched_repos=json.loads(row["touched_repos_json"]),
            max_usd=row["max_usd"],
            extra_usd_granted=float(row["extra_usd_granted"]),
            pending_question=row["pending_question"],
            review_summary=row["review_summary"],
            error_message=row["error_message"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    async def upsert_task(self, task: Task) -> None:
        async with self._connect() as conn:
            await conn.execute(
                """
                INSERT INTO tasks (
                    id, title, prompt, status, mode, domain, target, pipeline_template,
                    conversation_id, worktree_path, branch_name, touched_repos_json,
                    max_usd, extra_usd_granted, pending_question, review_summary,
                    error_message, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    title=excluded.title,
                    prompt=excluded.prompt,
                    status=excluded.status,
                    mode=excluded.mode,
                    domain=excluded.domain,
                    target=excluded.target,
                    pipeline_template=excluded.pipeline_template,
                    conversation_id=excluded.conversation_id,
                    worktree_path=excluded.worktree_path,
                    branch_name=excluded.branch_name,
                    touched_repos_json=excluded.touched_repos_json,
                    max_usd=excluded.max_usd,
                    extra_usd_granted=excluded.extra_usd_granted,
                    pending_question=excluded.pending_question,
                    review_summary=excluded.review_summary,
                    error_message=excluded.error_message,
                    updated_at=excluded.updated_at
                """,
                (
                    task.id,
                    task.title,
                    task.prompt,
                    task.status.value,
                    task.mode,
                    task.domain,
                    task.target,
                    task.pipeline_template,
                    task.conversation_id,
                    task.worktree_path,
                    task.branch_name,
                    json.dumps(task.touched_repos),
                    task.max_usd,
                    task.extra_usd_granted,
                    task.pending_question,
                    task.review_summary,
                    task.error_message,
                    task.created_at,
                    task.updated_at,
                ),
            )
            await conn.commit()

    async def get_task(self, task_id: str) -> Task | None:
        async with self._connect() as conn:
            cursor = await conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,))
            row = await cursor.fetchone()
            return self._row_to_task(row) if row else None

    async def list_tasks(
        self, status: TaskStatus | None = None, *, limit: int | None = None
    ) -> list[Task]:
        async with self._connect() as conn:
            clauses: list[str] = []
            params: list[object] = []
            if status is not None:
                clauses.append("status = ?")
                params.append(status.value)
            where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
            limit_sql = ""
            if limit is not None:
                limit_sql = " LIMIT ?"
                params.append(limit)
            cursor = await conn.execute(
                f"SELECT * FROM tasks {where} ORDER BY updated_at DESC, created_at DESC{limit_sql}",
                tuple(params),
            )
            rows = await cursor.fetchall()
            return [self._row_to_task(r) for r in rows]

    async def delete_task(self, task_id: str) -> bool:
        async with self._connect() as conn:
            cursor = await conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
            await conn.commit()
            return cursor.rowcount > 0

    async def get_setting(self, key: str) -> str | None:
        async with self._connect() as conn:
            cursor = await conn.execute(
                "SELECT value FROM runtime_settings WHERE key = ?", (key,)
            )
            row = await cursor.fetchone()
            return str(row["value"]) if row else None

    async def set_setting(self, key: str, value: str) -> None:
        async with self._connect() as conn:
            await conn.execute(
                """
                INSERT INTO runtime_settings (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value=excluded.value,
                    updated_at=excluded.updated_at
                """,
                (key, value, utcnow_iso()),
            )
            await conn.commit()

    async def append_task_message(self, message: TaskMessage) -> None:
        async with self._connect() as conn:
            await conn.execute(
                """
                INSERT INTO task_messages (id, task_id, sender, content, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    message.id,
                    message.task_id,
                    message.sender,
                    message.content,
                    message.created_at,
                ),
            )
            await conn.commit()

    async def list_task_messages(
        self, task_id: str, limit: int = 200
    ) -> list[TaskMessage]:
        async with self._connect() as conn:
            cursor = await conn.execute(
                "SELECT * FROM task_messages WHERE task_id = ? ORDER BY rowid ASC LIMIT ?",
                (task_id, limit),
            )
            rows = await cursor.fetchall()
            return [
                TaskMessage(
                    id=r["id"],
                    task_id=r["task_id"],
                    sender=r["sender"],
                    content=r["content"],
                    created_at=r["created_at"],
                )
                for r in rows
            ]

    async def get_session_stats(
        self, task_ids: Sequence[str]
    ) -> dict[str, tuple[int, int, float]]:
        if not task_ids:
            return {}
        placeholders = ", ".join("?" for _ in task_ids)
        params = tuple(task_ids)
        msg_counts: dict[str, int] = {}
        wu_counts: dict[str, int] = {}
        usd_totals: dict[str, float] = {}
        async with self._connect() as conn:
            cur_msg = await conn.execute(
                f"SELECT task_id, COUNT(*) AS cnt FROM task_messages WHERE task_id IN ({placeholders}) GROUP BY task_id",
                params,
            )
            for r in await cur_msg.fetchall():
                msg_counts[r["task_id"]] = int(r["cnt"])
            cur_wu = await conn.execute(
                f"SELECT task_id, COUNT(*) AS cnt FROM work_units WHERE task_id IN ({placeholders}) GROUP BY task_id",
                params,
            )
            for r in await cur_wu.fetchall():
                wu_counts[r["task_id"]] = int(r["cnt"])
            cur_cost = await conn.execute(
                f"SELECT task_id, COALESCE(SUM(usd_cost), 0.0) AS total FROM cost_ledger WHERE task_id IN ({placeholders}) GROUP BY task_id",
                params,
            )
            for r in await cur_cost.fetchall():
                usd_totals[r["task_id"]] = float(r["total"])
        return {
            tid: (msg_counts.get(tid, 0), wu_counts.get(tid, 0), usd_totals.get(tid, 0.0))
            for tid in task_ids
        }

    def _row_to_work_unit(self, row: aiosqlite.Row) -> WorkUnit:
        raw_keys = row.keys()
        skills = json.loads(row["skills_json"]) if "skills_json" in raw_keys else []
        return WorkUnit(
            id=row["id"],
            task_id=row["task_id"],
            title=row["title"],
            domain=row["domain"],
            target=row["target"],
            pipeline_template=row["pipeline_template"],
            complexity=row["complexity"],
            skills=list(skills),
            prompt=row["prompt"],
            current_stage_idx=int(row["current_stage_idx"]),
            current_stage_name=row["current_stage_name"],
            gate_attempts=json.loads(row["gate_attempts_json"]),
            last_file_modified_at=float(row["last_file_modified_at"]),
            last_gate_passed_at=float(row["last_gate_passed_at"]),
            commit_sha=row["commit_sha"],
            status=row["status"],
            summary=row["summary"],
        )

    async def upsert_work_unit(self, work_unit: WorkUnit) -> None:
        async with self._connect() as conn:
            await conn.execute(
                """
                INSERT INTO work_units (
                    id, task_id, title, domain, target, pipeline_template, complexity,
                    skills_json, prompt, current_stage_idx, current_stage_name, gate_attempts_json,
                    last_file_modified_at, last_gate_passed_at, commit_sha, status, summary
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    title=excluded.title,
                    domain=excluded.domain,
                    target=excluded.target,
                    pipeline_template=excluded.pipeline_template,
                    complexity=excluded.complexity,
                    skills_json=excluded.skills_json,
                    prompt=excluded.prompt,
                    current_stage_idx=excluded.current_stage_idx,
                    current_stage_name=excluded.current_stage_name,
                    gate_attempts_json=excluded.gate_attempts_json,
                    last_file_modified_at=excluded.last_file_modified_at,
                    last_gate_passed_at=excluded.last_gate_passed_at,
                    commit_sha=excluded.commit_sha,
                    status=excluded.status,
                    summary=excluded.summary
                """,
                (
                    work_unit.id,
                    work_unit.task_id,
                    work_unit.title,
                    work_unit.domain,
                    work_unit.target,
                    work_unit.pipeline_template,
                    work_unit.complexity,
                    json.dumps(work_unit.skills),
                    work_unit.prompt,
                    work_unit.current_stage_idx,
                    work_unit.current_stage_name,
                    json.dumps(work_unit.gate_attempts),
                    work_unit.last_file_modified_at,
                    work_unit.last_gate_passed_at,
                    work_unit.commit_sha,
                    work_unit.status,
                    work_unit.summary,
                ),
            )
            await conn.commit()

    async def get_work_unit(self, work_unit_id: str) -> WorkUnit | None:
        async with self._connect() as conn:
            cursor = await conn.execute(
                "SELECT * FROM work_units WHERE id = ?", (work_unit_id,)
            )
            row = await cursor.fetchone()
            return self._row_to_work_unit(row) if row else None

    async def list_work_units(self, task_id: str) -> list[WorkUnit]:
        async with self._connect() as conn:
            cursor = await conn.execute(
                "SELECT * FROM work_units WHERE task_id = ? ORDER BY rowid ASC",
                (task_id,),
            )
            return [self._row_to_work_unit(r) for r in await cursor.fetchall()]

    async def upsert_subagent_run(self, run: SubagentRun) -> None:
        async with self._connect() as conn:
            await conn.execute(
                """
                INSERT INTO subagent_runs (
                    id, task_id, work_unit_id, role, model_alias, model_id, status,
                    prompt, summary, prompt_tokens, cached_tokens, candidate_tokens,
                    thoughts_tokens, total_tokens, usd_cost, started_at, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    status=excluded.status,
                    summary=excluded.summary,
                    prompt_tokens=excluded.prompt_tokens,
                    cached_tokens=excluded.cached_tokens,
                    candidate_tokens=excluded.candidate_tokens,
                    thoughts_tokens=excluded.thoughts_tokens,
                    total_tokens=excluded.total_tokens,
                    usd_cost=excluded.usd_cost,
                    completed_at=excluded.completed_at
                """,
                (
                    run.id,
                    run.task_id,
                    run.work_unit_id,
                    run.role.value,
                    run.model_alias,
                    run.model_id,
                    run.status,
                    run.prompt,
                    run.summary,
                    run.prompt_tokens,
                    run.cached_tokens,
                    run.candidate_tokens,
                    run.thoughts_tokens,
                    run.total_tokens,
                    run.usd_cost,
                    run.started_at,
                    run.completed_at,
                ),
            )
            await conn.commit()

    async def list_subagent_runs(self, task_id: str) -> list[SubagentRun]:
        async with self._connect() as conn:
            cursor = await conn.execute(
                "SELECT * FROM subagent_runs WHERE task_id = ? ORDER BY started_at ASC",
                (task_id,),
            )
            rows = await cursor.fetchall()
            return [
                SubagentRun(
                    id=r["id"],
                    task_id=r["task_id"],
                    work_unit_id=r["work_unit_id"],
                    role=AgentRole(r["role"]),
                    model_alias=r["model_alias"],
                    model_id=r["model_id"],
                    status=r["status"],
                    prompt=r["prompt"],
                    summary=r["summary"],
                    prompt_tokens=int(r["prompt_tokens"]),
                    cached_tokens=int(r["cached_tokens"]),
                    candidate_tokens=int(r["candidate_tokens"]),
                    thoughts_tokens=int(r["thoughts_tokens"]),
                    total_tokens=int(r["total_tokens"]),
                    usd_cost=float(r["usd_cost"]),
                    started_at=r["started_at"],
                    completed_at=r["completed_at"],
                )
                for r in rows
            ]

    async def append_event(self, event: TaskEvent) -> None:
        async with self._connect() as conn:
            await conn.execute(
                """
                INSERT INTO task_events (id, task_id, source, event_type, content, metadata_json, timestamp)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.id,
                    event.task_id,
                    event.source,
                    event.event_type,
                    event.content,
                    json.dumps(event.metadata),
                    event.timestamp,
                ),
            )
            await conn.commit()

    async def list_events(self, task_id: str, limit: int = 200) -> list[TaskEvent]:
        async with self._connect() as conn:
            cursor = await conn.execute(
                "SELECT * FROM task_events WHERE task_id = ? ORDER BY rowid ASC LIMIT ?",
                (task_id, limit),
            )
            rows = await cursor.fetchall()
            return [
                TaskEvent(
                    id=r["id"],
                    task_id=r["task_id"],
                    source=r["source"],
                    event_type=r["event_type"],
                    content=r["content"],
                    metadata=json.loads(r["metadata_json"]),
                    timestamp=r["timestamp"],
                )
                for r in rows
            ]

    async def record_cost_entry(self, entry: CostLedgerEntry) -> None:
        async with self._connect() as conn:
            await conn.execute(
                """
                INSERT INTO cost_ledger (
                    id, task_id, subagent_run_id, role, model_id,
                    prompt_tokens, cached_tokens, candidate_tokens,
                    thoughts_tokens, total_tokens, usd_cost, timestamp
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    entry.id,
                    entry.task_id,
                    entry.subagent_run_id,
                    entry.role,
                    entry.model_id,
                    entry.usage.prompt_tokens,
                    entry.usage.cached_tokens,
                    entry.usage.candidate_tokens,
                    entry.usage.thoughts_tokens,
                    entry.usage.total_tokens,
                    entry.usd_cost,
                    entry.timestamp,
                ),
            )
            await conn.commit()

    async def get_task_usd_spent(self, task_id: str) -> float:
        async with self._connect() as conn:
            cursor = await conn.execute(
                "SELECT COALESCE(SUM(usd_cost), 0.0) AS total FROM cost_ledger WHERE task_id = ?",
                (task_id,),
            )
            row = await cursor.fetchone()
            return float(row["total"]) if row else 0.0

    async def get_daily_usd_spent(self, day_prefix: str | None = None) -> float:
        prefix = day_prefix or utcnow_iso()[:10]
        async with self._connect() as conn:
            cursor = await conn.execute(
                "SELECT COALESCE(SUM(usd_cost), 0.0) AS total FROM cost_ledger WHERE timestamp LIKE ?",
                (f"{prefix}%",),
            )
            row = await cursor.fetchone()
            return float(row["total"]) if row else 0.0

    async def get_task_token_usage(self, task_id: str) -> TokenUsage:
        async with self._connect() as conn:
            cursor = await conn.execute(
                """
                SELECT
                    COALESCE(SUM(prompt_tokens), 0) AS pt,
                    COALESCE(SUM(cached_tokens), 0) AS ct,
                    COALESCE(SUM(candidate_tokens), 0) AS cdt,
                    COALESCE(SUM(thoughts_tokens), 0) AS tt,
                    COALESCE(SUM(total_tokens), 0) AS tot
                FROM cost_ledger WHERE task_id = ?
                """,
                (task_id,),
            )
            row = await cursor.fetchone()
            if not row:
                return TokenUsage()
            return TokenUsage(
                prompt_tokens=int(row["pt"]),
                cached_tokens=int(row["ct"]),
                candidate_tokens=int(row["cdt"]),
                thoughts_tokens=int(row["tt"]),
                total_tokens=int(row["tot"]),
            )

    async def get_daily_token_usage(self, day_prefix: str | None = None) -> TokenUsage:
        prefix = day_prefix or utcnow_iso()[:10]
        async with self._connect() as conn:
            cursor = await conn.execute(
                """
                SELECT
                    COALESCE(SUM(prompt_tokens), 0) AS pt,
                    COALESCE(SUM(cached_tokens), 0) AS ct,
                    COALESCE(SUM(candidate_tokens), 0) AS cdt,
                    COALESCE(SUM(thoughts_tokens), 0) AS tt,
                    COALESCE(SUM(total_tokens), 0) AS tot
                FROM cost_ledger WHERE timestamp LIKE ?
                """,
                (f"{prefix}%",),
            )
            row = await cursor.fetchone()
            if not row:
                return TokenUsage()
            return TokenUsage(
                prompt_tokens=int(row["pt"]),
                cached_tokens=int(row["ct"]),
                candidate_tokens=int(row["cdt"]),
                thoughts_tokens=int(row["tt"]),
                total_tokens=int(row["tot"]),
            )

    async def get_cached_pricing(
        self, model_id: str, *, max_age_hours: int = 24
    ) -> ModelPricing | None:
        async with self._connect() as conn:
            cursor = await conn.execute(
                "SELECT * FROM model_pricing_cache WHERE model_id = ?", (model_id,)
            )
            row = await cursor.fetchone()
            if not row:
                return None
            fetched_at = datetime.fromisoformat(row["fetched_at"])
            if fetched_at.tzinfo is None:
                fetched_at = fetched_at.replace(tzinfo=UTC)
            if datetime.now(UTC) - fetched_at > timedelta(hours=max_age_hours):
                return None
            return ModelPricing(
                model_id=row["model_id"],
                input_cost_per_1m=float(row["input_cost_per_1m"]),
                cached_input_cost_per_1m=float(row["cached_input_cost_per_1m"]),
                output_cost_per_1m=float(row["output_cost_per_1m"]),
                thinking_cost_per_1m=float(row["thinking_cost_per_1m"]),
                source=row["source"],
                fetched_at=row["fetched_at"],
            )

    async def upsert_cached_pricing(self, pricing: ModelPricing) -> None:
        async with self._connect() as conn:
            await conn.execute(
                """
                INSERT INTO model_pricing_cache (
                    model_id, input_cost_per_1m, cached_input_cost_per_1m,
                    output_cost_per_1m, thinking_cost_per_1m, source, fetched_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(model_id) DO UPDATE SET
                    input_cost_per_1m=excluded.input_cost_per_1m,
                    cached_input_cost_per_1m=excluded.cached_input_cost_per_1m,
                    output_cost_per_1m=excluded.output_cost_per_1m,
                    thinking_cost_per_1m=excluded.thinking_cost_per_1m,
                    source=excluded.source,
                    fetched_at=excluded.fetched_at
                """,
                (
                    pricing.model_id,
                    pricing.input_cost_per_1m,
                    pricing.cached_input_cost_per_1m,
                    pricing.output_cost_per_1m,
                    pricing.thinking_cost_per_1m,
                    pricing.source,
                    pricing.fetched_at,
                ),
            )
            await conn.commit()

    def _row_to_knowledge(self, row: aiosqlite.Row) -> KnowledgeEntry:
        emb_raw = json.loads(row["embedding_json"]) if row["embedding_json"] else []
        return KnowledgeEntry(
            id=row["id"],
            domain=row["domain"],
            target=row["target"],
            issue=row["issue"],
            resolution=row["resolution"],
            outcome=row["outcome"],
            capture_type=row["capture_type"],
            embedding=emb_raw if emb_raw else None,
            created_at=row["created_at"],
        )

    async def upsert_knowledge(self, entry: KnowledgeEntry) -> None:
        embedding = entry.embedding or compute_deterministic_embedding(
            f"{entry.domain} {entry.target} {entry.issue}\n{entry.resolution}",
            dimensions=self.embedding_dimensions,
        )
        async with self._connect() as conn:
            await conn.execute(
                """
                INSERT INTO knowledge_entries (
                    id, domain, target, issue, resolution, outcome,
                    capture_type, embedding_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    domain=excluded.domain,
                    target=excluded.target,
                    issue=excluded.issue,
                    resolution=excluded.resolution,
                    outcome=excluded.outcome,
                    capture_type=excluded.capture_type,
                    embedding_json=excluded.embedding_json
                """,
                (
                    entry.id,
                    entry.domain,
                    entry.target,
                    entry.issue,
                    entry.resolution,
                    entry.outcome,
                    entry.capture_type,
                    json.dumps(list(embedding)),
                    entry.created_at,
                ),
            )
            await conn.execute(
                "DELETE FROM knowledge_fts WHERE entry_id = ?", (entry.id,)
            )
            await conn.execute(
                "INSERT INTO knowledge_fts (entry_id, domain, target, issue, resolution) VALUES (?, ?, ?, ?, ?)",
                (entry.id, entry.domain, entry.target, entry.issue, entry.resolution),
            )
            if self._vec_enabled and len(embedding) == self.embedding_dimensions:
                await conn.execute(
                    "DELETE FROM knowledge_vec WHERE entry_id = ?", (entry.id,)
                )
                await conn.execute(
                    "INSERT INTO knowledge_vec (entry_id, embedding) VALUES (?, ?)",
                    (entry.id, _pack_f32(embedding)),
                )
            await conn.commit()

    async def list_knowledge(
        self, *, domain: str | None = None, target: str | None = None, limit: int = 50
    ) -> list[KnowledgeEntry]:
        clauses: list[str] = []
        params: list[object] = []
        if domain:
            clauses.append("domain = ?")
            params.append(domain)
        if target:
            clauses.append("target = ?")
            params.append(target)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        async with self._connect() as conn:
            cursor = await conn.execute(
                f"SELECT * FROM knowledge_entries {where} ORDER BY created_at DESC LIMIT ?",
                tuple(params),
            )
            return [self._row_to_knowledge(r) for r in await cursor.fetchall()]

    async def replace_target_knowledge(
        self, domain: str, target: str, consolidated: KnowledgeEntry
    ) -> None:
        embedding = consolidated.embedding or compute_deterministic_embedding(
            f"{consolidated.domain} {consolidated.target} {consolidated.issue}\n{consolidated.resolution}",
            dimensions=self.embedding_dimensions,
        )
        async with self._connect() as conn:
            cursor = await conn.execute(
                "SELECT id FROM knowledge_entries WHERE domain = ? AND target = ?",
                (domain, target),
            )
            old_ids = [r["id"] for r in await cursor.fetchall()]
            for old_id in old_ids:
                await conn.execute(
                    "DELETE FROM knowledge_fts WHERE entry_id = ?", (old_id,)
                )
                if self._vec_enabled:
                    await conn.execute(
                        "DELETE FROM knowledge_vec WHERE entry_id = ?", (old_id,)
                    )
            await conn.execute(
                "DELETE FROM knowledge_entries WHERE domain = ? AND target = ?",
                (domain, target),
            )
            await conn.execute(
                """
                INSERT INTO knowledge_entries (
                    id, domain, target, issue, resolution, outcome,
                    capture_type, embedding_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    consolidated.id,
                    consolidated.domain,
                    consolidated.target,
                    consolidated.issue,
                    consolidated.resolution,
                    consolidated.outcome,
                    consolidated.capture_type,
                    json.dumps(list(embedding)),
                    consolidated.created_at,
                ),
            )
            await conn.execute(
                "INSERT INTO knowledge_fts (entry_id, domain, target, issue, resolution) VALUES (?, ?, ?, ?, ?)",
                (
                    consolidated.id,
                    consolidated.domain,
                    consolidated.target,
                    consolidated.issue,
                    consolidated.resolution,
                ),
            )
            if self._vec_enabled and len(embedding) == self.embedding_dimensions:
                await conn.execute(
                    "INSERT INTO knowledge_vec (entry_id, embedding) VALUES (?, ?)",
                    (consolidated.id, _pack_f32(embedding)),
                )
            await conn.commit()

    async def search_knowledge(
        self,
        query: str,
        *,
        query_embedding: Sequence[float] | None = None,
        domain: str | None = None,
        target: str | None = None,
        limit: int = 5,
    ) -> list[KnowledgeEntry]:
        q_emb = list(
            query_embedding
            or compute_deterministic_embedding(
                query, dimensions=self.embedding_dimensions
            )
        )
        fts_query = sanitize_fts5_query(query)
        rrf_k = 60.0

        async with self._connect() as conn:
            clauses: list[str] = []
            params: list[object] = []
            if domain:
                clauses.append("domain = ?")
                params.append(domain)
            if target:
                clauses.append("target = ?")
                params.append(target)
            where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
            cursor = await conn.execute(
                f"SELECT * FROM knowledge_entries {where}", tuple(params)
            )
            entries = {r["id"]: self._row_to_knowledge(r) for r in await cursor.fetchall()}
            if not entries:
                return []

            bm25_ranks: dict[str, int] = {}
            if fts_query:
                try:
                    fts_cursor = await conn.execute(
                        """
                        SELECT entry_id, bm25(knowledge_fts) AS rank_score
                        FROM knowledge_fts
                        WHERE knowledge_fts MATCH ?
                        ORDER BY rank_score ASC
                        LIMIT ?
                        """,
                        (fts_query, max(limit * 4, 20)),
                    )
                    rank = 1
                    for r in await fts_cursor.fetchall():
                        eid = r["entry_id"]
                        if eid in entries:
                            bm25_ranks[eid] = rank
                            rank += 1
                except Exception:
                    pass

            vec_ranks: dict[str, int] = {}
            if self._vec_enabled and len(q_emb) == self.embedding_dimensions:
                try:
                    vec_cursor = await conn.execute(
                        """
                        SELECT entry_id, distance
                        FROM knowledge_vec
                        WHERE embedding MATCH ? AND k = ?
                        ORDER BY distance ASC
                        """,
                        (_pack_f32(q_emb), max(limit * 4, 20)),
                    )
                    rank = 1
                    for r in await vec_cursor.fetchall():
                        eid = r["entry_id"]
                        if eid in entries:
                            vec_ranks[eid] = rank
                            rank += 1
                except Exception:
                    pass

            if not vec_ranks:
                scored_vecs: list[tuple[float, str]] = []
                for eid, entry in entries.items():
                    if entry.embedding:
                        sim = _cosine_similarity(q_emb, entry.embedding)
                        if sim > 0.0:
                            scored_vecs.append((sim, eid))
                scored_vecs.sort(reverse=True)
                for rank_idx, (_, eid) in enumerate(scored_vecs, start=1):
                    vec_ranks[eid] = rank_idx

            lowered_q = query.lower().strip()
            fused: list[tuple[float, str]] = []
            for eid, entry in entries.items():
                score = 0.0
                if eid in bm25_ranks:
                    score += 1.0 / (rrf_k + bm25_ranks[eid])
                if eid in vec_ranks:
                    score += 1.0 / (rrf_k + vec_ranks[eid])
                haystack = f"{entry.target} {entry.issue} {entry.resolution}".lower()
                if lowered_q and lowered_q in haystack:
                    score += 0.05
                if target and entry.target.lower() == target.lower():
                    score += 0.02
                if score > 0.0:
                    fused.append((score, eid))

            fused.sort(reverse=True)
            return [entries[eid] for _, eid in fused[:limit]]
