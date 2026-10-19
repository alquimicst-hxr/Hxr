"""Persistência SQLite: conexões por operação, WAL, transações explícitas.

Todas as consultas são parametrizadas. Nenhuma chave de API é armazenada.
A classe é síncrona; o bot a chama via asyncio.to_thread.
"""
from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT,
  custom_instructions TEXT, created_at REAL NOT NULL, last_seen REAL NOT NULL);
CREATE TABLE IF NOT EXISTS admins(
  user_id INTEGER PRIMARY KEY, role TEXT NOT NULL CHECK(role IN ('owner','admin')),
  added_by INTEGER, added_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS plans(
  name TEXT PRIMARY KEY, quota INTEGER NOT NULL, period_days INTEGER NOT NULL,
  model TEXT NOT NULL DEFAULT '', is_paid INTEGER NOT NULL, active INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS subscriptions(
  id INTEGER PRIMARY KEY AUTOINCREMENT, subject_id INTEGER NOT NULL, plan TEXT NOT NULL REFERENCES plans(name),
  started_at REAL NOT NULL, expires_at REAL NOT NULL, period_days INTEGER NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('active','expired','cancelled','replaced')),
  granted_by INTEGER, source TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_sub_subject ON subscriptions(subject_id, status, expires_at);
CREATE TABLE IF NOT EXISTS subscription_events(
  id INTEGER PRIMARY KEY AUTOINCREMENT, subject_id INTEGER NOT NULL, action TEXT NOT NULL,
  plan TEXT, actor INTEGER, detail TEXT, ts REAL NOT NULL);
CREATE INDEX IF NOT EXISTS idx_subev_subject ON subscription_events(subject_id, ts);
CREATE TABLE IF NOT EXISTS usage_counters(
  subject_id INTEGER NOT NULL, period_key TEXT NOT NULL, used INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(subject_id, period_key));
CREATE TABLE IF NOT EXISTS messages(
  id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL, thread_id INTEGER NOT NULL DEFAULT 0,
  user_id INTEGER, role TEXT NOT NULL CHECK(role IN ('user','assistant')),
  content TEXT NOT NULL, created_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS idx_msg_ctx ON messages(chat_id, thread_id, id);
CREATE INDEX IF NOT EXISTS idx_msg_created ON messages(created_at);
CREATE TABLE IF NOT EXISTS groups(
  chat_id INTEGER PRIMARY KEY, title TEXT, enabled INTEGER NOT NULL DEFAULT 0,
  default_thread_id INTEGER, premium_enabled INTEGER NOT NULL DEFAULT 0,
  member_count INTEGER, member_count_checked_at REAL, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS group_topics(
  chat_id INTEGER NOT NULL, thread_id INTEGER NOT NULL, title TEXT, enabled INTEGER NOT NULL DEFAULT 1,
  PRIMARY KEY(chat_id, thread_id));
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS admin_events(
  id INTEGER PRIMARY KEY AUTOINCREMENT, actor INTEGER NOT NULL, action TEXT NOT NULL,
  target TEXT, detail TEXT, ts REAL NOT NULL);
CREATE TABLE IF NOT EXISTS audit_log(
  id INTEGER PRIMARY KEY AUTOINCREMENT, actor INTEGER, action TEXT NOT NULL,
  chat_id INTEGER, ok INTEGER NOT NULL, detail TEXT, ts REAL NOT NULL);
CREATE TABLE IF NOT EXISTS user_blocks(
  user_id INTEGER PRIMARY KEY, reason TEXT, blocked_by INTEGER, blocked_at REAL NOT NULL);
"""


class Database:
    def __init__(self, path: str, busy_timeout_ms: int = 10000, clock=time.time) -> None:
        self.path = str(path)
        self.busy_timeout_ms = busy_timeout_ms
        self.clock = clock

    # ---- infraestrutura ----------------------------------------------
    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=self.busy_timeout_ms / 1000, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute(f"PRAGMA busy_timeout={int(self.busy_timeout_ms)}")
            conn.execute("PRAGMA foreign_keys=ON")
            yield conn
        finally:
            conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Transação com lock de escrita imediato (serializa consumo de cota)."""
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            else:
                conn.execute("COMMIT")

    def init(self) -> None:
        """Criação idempotente do esquema."""
        if self.path != ":memory:":
            Path(self.path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.executescript(SCHEMA)

    def _one(self, sql: str, args: tuple = ()) -> Optional[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(sql, args).fetchone()

    def _all(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(sql, args).fetchall()

    def _run(self, sql: str, args: tuple = ()) -> int:
        with self.transaction() as conn:
            return conn.execute(sql, args).rowcount

    # ---- usuários ------------------------------------------------------
    def upsert_user(self, user_id: int, username: str | None, first_name: str | None) -> None:
        now = self.clock()
        self._run(
            "INSERT INTO users(user_id,username,first_name,created_at,last_seen) VALUES(?,?,?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET username=excluded.username, first_name=excluded.first_name, last_seen=excluded.last_seen",
            (user_id, (username or "")[:64], (first_name or "")[:64], now, now))

    def get_user(self, user_id: int):
        return self._one("SELECT * FROM users WHERE user_id=?", (user_id,))

    def list_users(self, limit: int, offset: int):
        return self._all("SELECT * FROM users ORDER BY created_at DESC, user_id LIMIT ? OFFSET ?", (limit, offset))

    def count_users(self) -> int:
        return self._one("SELECT COUNT(*) c FROM users")["c"]

    def set_custom_instructions(self, user_id: int, text: str | None) -> None:
        self._run("UPDATE users SET custom_instructions=? WHERE user_id=?", (text, user_id))

    def get_custom_instructions(self, user_id: int) -> str:
        row = self.get_user(user_id)
        return (row["custom_instructions"] or "") if row else ""

    # ---- bloqueios -----------------------------------------------------
    def block_user(self, user_id: int, reason: str, by: int) -> None:
        self._run("INSERT OR REPLACE INTO user_blocks(user_id,reason,blocked_by,blocked_at) VALUES(?,?,?,?)",
                  (user_id, reason[:200], by, self.clock()))

    def unblock_user(self, user_id: int) -> bool:
        return self._run("DELETE FROM user_blocks WHERE user_id=?", (user_id,)) > 0

    def is_blocked(self, user_id: int) -> bool:
        return self._one("SELECT 1 FROM user_blocks WHERE user_id=?", (user_id,)) is not None

    def count_blocks(self) -> int:
        return self._one("SELECT COUNT(*) c FROM user_blocks")["c"]

    # ---- administradores -----------------------------------------------
    def list_admins(self):
        return self._all("SELECT * FROM admins ORDER BY role DESC, added_at")

    def get_admin(self, user_id: int):
        return self._one("SELECT * FROM admins WHERE user_id=?", (user_id,))

    def add_admin(self, user_id: int, role: str, added_by: int | None) -> None:
        self._run("INSERT OR IGNORE INTO admins(user_id,role,added_by,added_at) VALUES(?,?,?,?)",
                  (user_id, role, added_by, self.clock()))

    def remove_admin(self, user_id: int) -> bool:
        return self._run("DELETE FROM admins WHERE user_id=? AND role!='owner'", (user_id,)) > 0

    # ---- planos --------------------------------------------------------
    def upsert_plan(self, name: str, quota: int, period_days: int, model: str, is_paid: bool) -> None:
        self._run(
            "INSERT INTO plans(name,quota,period_days,model,is_paid,active) VALUES(?,?,?,?,?,1) "
            "ON CONFLICT(name) DO UPDATE SET quota=excluded.quota, period_days=excluded.period_days, model=excluded.model, is_paid=excluded.is_paid",
            (name, quota, period_days, model, 1 if is_paid else 0))

    def get_plan(self, name: str):
        return self._one("SELECT * FROM plans WHERE name=?", (name,))

    def list_plans(self):
        return self._all("SELECT * FROM plans ORDER BY is_paid, quota")

    def sync_plans(self, config) -> None:
        """Grava no banco os planos definidos na configuração."""
        self.upsert_plan("free", config.plan_quotas["free"], 0, config.plan_models.get("free", ""), False)
        for p in ("standard", "pro", "ultra"):
            self.upsert_plan(p, config.plan_quotas[p], config.plan_period_days, config.plan_models.get(p, ""), True)

    # ---- configurações -------------------------------------------------
    def get_setting(self, key: str, default: str = "") -> str:
        row = self._one("SELECT value FROM settings WHERE key=?", (key,))
        return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        self._run("INSERT INTO settings(key,value,updated_at) VALUES(?,?,?) "
                  "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                  (key, value, self.clock()))

    def all_settings(self) -> dict:
        return {r["key"]: r["value"] for r in self._all("SELECT key,value FROM settings ORDER BY key")}

    # ---- mensagens / histórico ----------------------------------------
    def add_messages(self, chat_id: int, thread_id: int, user_id: int | None, pairs: list[tuple[str, str]]) -> None:
        """Insere várias mensagens (role, content) numa única transação."""
        now = self.clock()
        with self.transaction() as conn:
            for role, content in pairs:
                conn.execute("INSERT INTO messages(chat_id,thread_id,user_id,role,content,created_at) VALUES(?,?,?,?,?,?)",
                             (chat_id, thread_id, user_id if role == "user" else None, role, content, now))

    def recent_messages(self, chat_id: int, thread_id: int, limit: int) -> list[sqlite3.Row]:
        if limit <= 0:
            return []
        rows = self._all("SELECT role,content,user_id,created_at FROM messages WHERE chat_id=? AND thread_id=? "
                         "ORDER BY id DESC LIMIT ?", (chat_id, thread_id, limit))
        return list(reversed(rows))

    def count_messages(self, chat_id: int, thread_id: int) -> int:
        return self._one("SELECT COUNT(*) c FROM messages WHERE chat_id=? AND thread_id=?", (chat_id, thread_id))["c"]

    def clear_history(self, chat_id: int, thread_id: int) -> int:
        return self._run("DELETE FROM messages WHERE chat_id=? AND thread_id=?", (chat_id, thread_id))

    def prune_history(self, chat_id: int, thread_id: int, keep: int) -> int:
        return self._run(
            "DELETE FROM messages WHERE chat_id=? AND thread_id=? AND id NOT IN "
            "(SELECT id FROM messages WHERE chat_id=? AND thread_id=? ORDER BY id DESC LIMIT ?)",
            (chat_id, thread_id, chat_id, thread_id, max(keep, 0)))

    def purge_older_than(self, days: int) -> int:
        """Política de retenção: apaga mensagens mais antigas que `days`."""
        return self._run("DELETE FROM messages WHERE created_at < ?", (self.clock() - days * 86400,))

    def delete_user_messages(self, user_id: int) -> int:
        """Apaga mensagens do usuário (não zera cotas: contadores ficam)."""
        return self._run("DELETE FROM messages WHERE user_id=? OR (chat_id=? AND thread_id=0)", (user_id, user_id))

    # ---- grupos e tópicos ----------------------------------------------
    def upsert_group(self, chat_id: int, title: str | None, enabled_default: bool = False) -> None:
        self._run("INSERT INTO groups(chat_id,title,enabled,created_at) VALUES(?,?,?,?) "
                  "ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title",
                  (chat_id, (title or "")[:128], 1 if enabled_default else 0, self.clock()))

    def get_group(self, chat_id: int):
        return self._one("SELECT * FROM groups WHERE chat_id=?", (chat_id,))

    def list_groups(self, limit: int = 50, offset: int = 0):
        return self._all("SELECT * FROM groups ORDER BY created_at DESC LIMIT ? OFFSET ?", (limit, offset))

    def set_group_field(self, chat_id: int, field: str, value) -> bool:
        if field not in ("enabled", "default_thread_id", "premium_enabled"):
            raise ValueError("campo não permitido")
        return self._run(f"UPDATE groups SET {field}=? WHERE chat_id=?", (value, chat_id)) > 0  # campo validado acima

    def set_group_member_count(self, chat_id: int, count: int | None) -> None:
        self._run("UPDATE groups SET member_count=?, member_count_checked_at=? WHERE chat_id=?",
                  (count, self.clock() if count is not None else None, chat_id))

    def upsert_topic(self, chat_id: int, thread_id: int, title: str | None = None, enabled: bool | None = None) -> None:
        with self.transaction() as conn:
            conn.execute("INSERT OR IGNORE INTO group_topics(chat_id,thread_id,title,enabled) VALUES(?,?,?,1)",
                         (chat_id, thread_id, (title or "")[:128]))
            if enabled is not None:
                conn.execute("UPDATE group_topics SET enabled=? WHERE chat_id=? AND thread_id=?",
                             (1 if enabled else 0, chat_id, thread_id))

    def get_topic(self, chat_id: int, thread_id: int):
        return self._one("SELECT * FROM group_topics WHERE chat_id=? AND thread_id=?", (chat_id, thread_id))

    # ---- eventos administrativos e auditoria ---------------------------
    def add_admin_event(self, actor: int, action: str, target: str = "", detail: str = "") -> None:
        self._run("INSERT INTO admin_events(actor,action,target,detail,ts) VALUES(?,?,?,?,?)",
                  (actor, action, target[:64], detail[:300], self.clock()))

    def list_admin_events(self, limit: int, offset: int):
        return self._all("SELECT * FROM admin_events ORDER BY id DESC LIMIT ? OFFSET ?", (limit, offset))

    def count_admin_events(self) -> int:
        return self._one("SELECT COUNT(*) c FROM admin_events")["c"]

    def add_audit(self, actor: int | None, action: str, chat_id: int | None, ok: bool, detail: str = "") -> None:
        """Auditoria técnica. NUNCA gravar conteúdo de mensagens em `detail`."""
        self._run("INSERT INTO audit_log(actor,action,chat_id,ok,detail,ts) VALUES(?,?,?,?,?,?)",
                  (actor, action, chat_id, 1 if ok else 0, detail[:300], self.clock()))

    def stats(self) -> dict:
        one = lambda sql: self._one(sql)["c"]  # noqa: E731
        now = self.clock()
        return {
            "users": one("SELECT COUNT(*) c FROM users"),
            "blocked": one("SELECT COUNT(*) c FROM user_blocks"),
            "groups": one("SELECT COUNT(*) c FROM groups"),
            "groups_enabled": one("SELECT COUNT(*) c FROM groups WHERE enabled=1"),
            "messages_stored": one("SELECT COUNT(*) c FROM messages"),
            "active_subscriptions": self._one(
                "SELECT COUNT(*) c FROM subscriptions WHERE status='active' AND expires_at>?", (now,))["c"],
            "admins": one("SELECT COUNT(*) c FROM admins"),
        }
