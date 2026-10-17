# -*- coding: utf-8 -*-
"""SQLite 持久化：多店铺会话、消息、回复、风控日志。

设计要点：
    - 所有业务表都带 ``shop_id``，同一张表存多店铺数据，面板可按店铺聚合；
    - 会话主键为 (shop_id, uid)，同一买家在不同店铺互不影响；
    - 旧版单店铺数据库会自动迁移（旧数据标记为 ``default`` 店铺，旧表保留备份）。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from .utils import iso, now_beijing

DEFAULT_SHOP = "default"

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    shop_id             TEXT NOT NULL DEFAULT 'default',
    uid                 TEXT NOT NULL,
    nickname            TEXT DEFAULT '',
    first_seen          TEXT,
    last_in_at          TEXT,
    last_reply_at       TEXT,
    inbound_count       INTEGER DEFAULT 0,
    outbound_count      INTEGER DEFAULT 0,
    human_takeover_until TEXT,
    last_result         TEXT DEFAULT '',
    last_order          TEXT,
    updated_at          TEXT,
    PRIMARY KEY (shop_id, uid)
);
CREATE TABLE IF NOT EXISTS messages (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    shop_id      TEXT NOT NULL DEFAULT 'default',
    uid          TEXT NOT NULL,
    direction    TEXT NOT NULL,
    role         TEXT,
    context_type TEXT,
    content      TEXT,
    msg_id       TEXT,
    platform_time REAL,
    created_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_messages_shop_uid ON messages(shop_id, uid, id);
CREATE INDEX IF NOT EXISTS idx_messages_created ON messages(created_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_msgid ON messages(shop_id, uid, msg_id)
    WHERE msg_id IS NOT NULL AND msg_id <> '';
CREATE TABLE IF NOT EXISTS replies (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    shop_id     TEXT NOT NULL DEFAULT 'default',
    uid         TEXT NOT NULL,
    content     TEXT,
    action      TEXT,
    result      TEXT,
    status      TEXT,
    error       TEXT,
    created_at  TEXT,
    latency_ms  INTEGER,
    rule_id     TEXT DEFAULT '',
    reason      TEXT DEFAULT '',
    question    TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_replies_created ON replies(created_at);
CREATE INDEX IF NOT EXISTS idx_replies_shop ON replies(shop_id, created_at);
CREATE TABLE IF NOT EXISTS risk_logs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    shop_id    TEXT NOT NULL DEFAULT 'default',
    uid        TEXT,
    risk_type  TEXT,
    detail     TEXT,
    created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_risk_created ON risk_logs(created_at);
CREATE INDEX IF NOT EXISTS idx_risk_shop ON risk_logs(shop_id, created_at);
CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

_LEGACY_TABLES = ("conversations", "messages", "replies", "risk_logs")


@dataclass
class StoredMessage:
    uid: str
    direction: str
    role: str
    content: str
    context_type: str
    created_at: str
    msg_id: str = ""
    platform_time: float = 0.0
    shop_id: str = DEFAULT_SHOP


class Store:
    """线程安全的 SQLite 存储（多店铺）。"""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False, timeout=10)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._migrate_if_needed()
            self._conn.executescript(SCHEMA)
            try:
                self._conn.execute("PRAGMA journal_mode=WAL")
            except sqlite3.DatabaseError:
                pass
            self._conn.commit()

    # ------------------------------------------------------------------
    # 迁移：旧版单店铺表 → 多店铺表（旧表保留 legacy 备份）
    # ------------------------------------------------------------------
    def _table_columns(self, table: str) -> list[str]:
        try:
            rows = self._conn.execute(f"PRAGMA table_info({table})").fetchall()
        except sqlite3.DatabaseError:
            return []
        return [str(row[1]) for row in rows]

    def _migrate_if_needed(self) -> None:
        columns = self._table_columns("conversations")
        if not columns or "shop_id" in columns:
            return
        # 旧库：先改名保留备份，再按新结构重建并回填 default 店铺
        stamp = now_beijing().strftime("%Y%m%d%H%M%S")
        for table in _LEGACY_TABLES:
            if self._table_columns(table):
                self._conn.execute(f"ALTER TABLE {table} RENAME TO {table}_legacy_{stamp}")
        self._conn.commit()
        self._conn.executescript(SCHEMA)
        cols_conv = self._table_columns(f"conversations_legacy_{stamp}")
        if cols_conv:
            def _col(name: str, default: str = "''") -> str:
                return name if name in cols_conv else default

            self._conn.execute(
                "INSERT INTO conversations (shop_id, uid, nickname, first_seen, last_in_at,"
                " last_reply_at, inbound_count, outbound_count, human_takeover_until,"
                " last_result, updated_at)"
                f" SELECT 'default', uid, {_col('nickname')}, {_col('first_seen')},"
                f" {_col('last_in_at')}, {_col('last_reply_at')},"
                f" {_col('inbound_count', '0')}, {_col('outbound_count', '0')},"
                f" {_col('human_takeover_until')}, {_col('last_result')}, {_col('updated_at')}"
                f" FROM conversations_legacy_{stamp}"
            )
        for table, target_cols in (
            ("messages", "shop_id, uid, direction, role, context_type, content, msg_id, platform_time, created_at"),
            ("replies", "shop_id, uid, content, action, result, status, error, created_at, latency_ms"),
            ("risk_logs", "shop_id, uid, risk_type, detail, created_at"),
        ):
            legacy = f"{table}_legacy_{stamp}"
            if not self._table_columns(legacy):
                continue
            source_cols = [c.strip() for c in target_cols.split(",")][1:]  # 去掉 shop_id
            select_cols = ", ".join(source_cols)
            self._conn.execute(
                f"INSERT INTO {table} ({target_cols}) SELECT 'default', {select_cols} FROM {legacy}"
            )
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.commit()
                self._conn.close()
            except sqlite3.Error:
                pass

    # ------------------------------------------------------------------
    def _execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cursor = self._conn.execute(sql, params)
            self._conn.commit()
            return cursor

    def _query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(sql, params).fetchall())

    @staticmethod
    def _shop_clause(shop_id: Optional[str], column: str = "shop_id") -> tuple[str, tuple]:
        if shop_id:
            return f" AND {column}=?", (shop_id,)
        return "", ()

    # ------------------------------------------------------------------
    # 消息 / 会话
    # ------------------------------------------------------------------
    def is_duplicate_message(self, uid: str, msg_id: str, shop_id: str = DEFAULT_SHOP) -> bool:
        if not msg_id:
            return False
        rows = self._query(
            "SELECT 1 FROM messages WHERE shop_id=? AND uid=? AND msg_id=? LIMIT 1",
            (shop_id, uid, msg_id),
        )
        return bool(rows)

    def record_inbound(
        self,
        *,
        uid: str,
        nickname: str,
        role: str,
        context_type: str,
        content: str,
        msg_id: str = "",
        platform_time: float = 0.0,
        created_at: Optional[datetime] = None,
        shop_id: str = DEFAULT_SHOP,
    ) -> None:
        created_at = created_at or now_beijing()
        with self._lock:
            self._conn.execute(
                "INSERT INTO messages (shop_id, uid, direction, role, context_type, content,"
                " msg_id, platform_time, created_at) VALUES (?, ?, 'in', ?, ?, ?, ?, ?, ?)",
                (shop_id, uid, role, context_type, content, msg_id, platform_time, iso(created_at)),
            )
            self._conn.execute(
                """
                INSERT INTO conversations (shop_id, uid, nickname, first_seen, last_in_at,
                                           inbound_count, updated_at)
                VALUES (?, ?, ?, ?, ?, 1, ?)
                ON CONFLICT(shop_id, uid) DO UPDATE SET
                    nickname = CASE WHEN excluded.nickname <> '' THEN excluded.nickname
                                    ELSE conversations.nickname END,
                    last_in_at = excluded.last_in_at,
                    inbound_count = conversations.inbound_count + 1,
                    updated_at = excluded.updated_at
                """,
                (shop_id, uid, nickname, iso(created_at), iso(created_at), iso(created_at)),
            )
            self._conn.commit()

    def record_outbound(
        self,
        *,
        uid: str,
        content: str,
        action: str = "",
        result: str = "",
        status: str = "sent",
        error: str = "",
        latency_ms: int = 0,
        created_at: Optional[datetime] = None,
        shop_id: str = DEFAULT_SHOP,
        rule_id: str = "",
        reason: str = "",
        question: str = "",
    ) -> None:
        created_at = created_at or now_beijing()
        with self._lock:
            self._conn.execute(
                "INSERT INTO messages (shop_id, uid, direction, role, context_type, content,"
                " msg_id, platform_time, created_at)"
                " VALUES (?, ?, 'out', 'SELF', 'text', ?, '', 0, ?)",
                (shop_id, uid, content, iso(created_at)),
            )
            self._conn.execute(
                "INSERT INTO replies (shop_id, uid, content, action, result, status, error,"
                " created_at, latency_ms, rule_id, reason, question)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    shop_id, uid, content, action, result, status, error, iso(created_at),
                    latency_ms, rule_id, reason, question,
                ),
            )
            self._conn.execute(
                """
                INSERT INTO conversations (shop_id, uid, last_reply_at, outbound_count,
                                           last_result, updated_at)
                VALUES (?, ?, ?, 1, ?, ?)
                ON CONFLICT(shop_id, uid) DO UPDATE SET
                    last_reply_at = excluded.last_reply_at,
                    outbound_count = conversations.outbound_count + 1,
                    last_result = excluded.last_result,
                    updated_at = excluded.updated_at
                """,
                (shop_id, uid, iso(created_at), result, iso(created_at)),
            )
            self._conn.commit()

    def mark_result(self, uid: str, result: str, shop_id: str = DEFAULT_SHOP) -> None:
        self._execute(
            "UPDATE conversations SET last_result=?, updated_at=? WHERE shop_id=? AND uid=?",
            (result, iso(now_beijing()), shop_id, uid),
        )

    def set_last_order(self, uid: str, order: dict[str, Any], shop_id: str = DEFAULT_SHOP) -> None:
        """把最近一次订单卡片信息挂到会话上（客服后续可直接看到）。"""
        self._execute(
            "UPDATE conversations SET last_order=?, updated_at=? WHERE shop_id=? AND uid=?",
            (json.dumps(order, ensure_ascii=False), iso(now_beijing()), shop_id, uid),
        )

    def get_conversation(self, uid: str, shop_id: str = DEFAULT_SHOP) -> Optional[dict[str, Any]]:
        rows = self._query(
            "SELECT * FROM conversations WHERE shop_id=? AND uid=?", (shop_id, uid)
        )
        if not rows:
            return None
        conv = dict(rows[0])
        if conv.get("last_order"):
            try:
                conv["last_order"] = json.loads(conv["last_order"])
            except (json.JSONDecodeError, TypeError):
                conv["last_order"] = None
        return conv

    def list_conversations(
        self, limit: int = 50, *, shop_id: Optional[str] = None, search: str = ""
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM conversations WHERE 1=1"
        params: list[Any] = []
        if shop_id:
            sql += " AND shop_id=?"
            params.append(shop_id)
        if search:
            sql += " AND (uid LIKE ? OR nickname LIKE ?)"
            params.extend([f"%{search}%", f"%{search}%"])
        sql += " ORDER BY COALESCE(last_in_at, updated_at) DESC LIMIT ?"
        params.append(max(1, limit))
        rows = self._query(sql, tuple(params))
        result = []
        for row in rows:
            conv = dict(row)
            if conv.get("last_order"):
                try:
                    conv["last_order"] = json.loads(conv["last_order"])
                except (json.JSONDecodeError, TypeError):
                    conv["last_order"] = None
            result.append(conv)
        return result

    def get_messages(
        self, uid: str, limit: int = 30, shop_id: str = DEFAULT_SHOP
    ) -> list[dict[str, Any]]:
        rows = self._query(
            "SELECT * FROM messages WHERE shop_id=? AND uid=? ORDER BY id DESC LIMIT ?",
            (shop_id, uid, max(1, limit)),
        )
        return [dict(row) for row in reversed(rows)]

    def recent_replies(self, uid: str, limit: int = 6, shop_id: str = DEFAULT_SHOP) -> list[str]:
        rows = self._query(
            "SELECT content FROM replies WHERE shop_id=? AND uid=? AND status='sent'"
            " ORDER BY id DESC LIMIT ?",
            (shop_id, uid, max(1, limit)),
        )
        return [str(row["content"]) for row in reversed(rows)]

    def reply_times(
        self,
        *,
        uid: Optional[str] = None,
        since: Optional[datetime] = None,
        shop_id: Optional[str] = None,
    ) -> list[datetime]:
        sql = "SELECT created_at FROM replies WHERE status='sent'"
        params: list[Any] = []
        if shop_id:
            sql += " AND shop_id=?"
            params.append(shop_id)
        if uid:
            sql += " AND uid=?"
            params.append(uid)
        if since:
            sql += " AND created_at >= ?"
            params.append(iso(since))
        rows = self._query(sql, tuple(params))
        times: list[datetime] = []
        for row in rows:
            try:
                times.append(
                    datetime.strptime(row["created_at"], "%Y-%m-%d %H:%M:%S").replace(
                        tzinfo=now_beijing().tzinfo
                    )
                )
            except (ValueError, TypeError):
                continue
        return times

    def history_for_ai(
        self, uid: str, limit: int = 12, shop_id: str = DEFAULT_SHOP
    ) -> list[dict[str, str]]:
        messages = self.get_messages(uid, limit=limit, shop_id=shop_id)
        history: list[dict[str, str]] = []
        for msg in messages:
            role = "user" if msg.get("direction") == "in" else "assistant"
            content = str(msg.get("content") or "").strip()
            if content:
                history.append({"role": role, "content": content})
        return history

    def count_repeat_asks(
        self, uid: str, text: str, minutes: int = 10, limit: int = 8, shop_id: str = DEFAULT_SHOP
    ) -> int:
        since = iso(now_beijing() - timedelta(minutes=max(1, minutes)))
        rows = self._query(
            "SELECT content FROM messages WHERE shop_id=? AND uid=? AND direction='in'"
            " AND created_at>=? ORDER BY id DESC LIMIT ?",
            (shop_id, uid, since, max(2, limit)),
        )
        from .utils import similarity

        return sum(1 for row in rows if similarity(text, str(row["content"] or "")) >= 0.75)

    def search_conversations(
        self, term: str, limit: int = 30, *, shop_id: Optional[str] = None
    ) -> list[dict[str, Any]]:
        """按 uid / 昵称 / 消息内容 / 订单号搜索会话。"""
        term = (term or "").strip()
        if not term:
            return []
        like = f"%{term}%"
        sql = (
            "SELECT c.* FROM conversations c LEFT JOIN messages m"
            " ON m.shop_id=c.shop_id AND m.uid=c.uid"
            " WHERE (c.uid LIKE ? OR c.nickname LIKE ? OR m.content LIKE ?"
            " OR c.last_order LIKE ?)"
        )
        params: list[Any] = [like, like, like, like]
        if shop_id:
            sql += " AND c.shop_id=?"
            params.append(shop_id)
        sql += " GROUP BY c.shop_id, c.uid ORDER BY COALESCE(c.last_in_at, c.updated_at) DESC LIMIT ?"
        params.append(max(1, limit))
        return [dict(row) for row in self._query(sql, tuple(params))]

    # ------------------------------------------------------------------
    # 人工接管
    # ------------------------------------------------------------------
    def set_human_takeover(
        self, uid: str, until: Optional[datetime], shop_id: str = DEFAULT_SHOP
    ) -> None:
        self._execute(
            "UPDATE conversations SET human_takeover_until=?, updated_at=? WHERE shop_id=? AND uid=?",
            (iso(until) if until else None, iso(now_beijing()), shop_id, uid),
        )

    def get_human_takeover(self, uid: str, shop_id: str = DEFAULT_SHOP) -> Optional[datetime]:
        conv = self.get_conversation(uid, shop_id=shop_id)
        if not conv or not conv.get("human_takeover_until"):
            return None
        try:
            return datetime.strptime(
                conv["human_takeover_until"], "%Y-%m-%d %H:%M:%S"
            ).replace(tzinfo=now_beijing().tzinfo)
        except (ValueError, TypeError):
            return None

    def expire_human_takeovers(self) -> int:
        cursor = self._execute(
            "UPDATE conversations SET human_takeover_until=NULL WHERE human_takeover_until IS NOT NULL"
            " AND human_takeover_until <= ?",
            (iso(now_beijing()),),
        )
        return cursor.rowcount or 0

    # ------------------------------------------------------------------
    # 风控日志
    # ------------------------------------------------------------------
    def log_risk(
        self, uid: str, risk_type: str, detail: str, shop_id: str = DEFAULT_SHOP
    ) -> None:
        self._execute(
            "INSERT INTO risk_logs (shop_id, uid, risk_type, detail, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (shop_id, uid, risk_type, detail, iso(now_beijing())),
        )

    def list_risk_logs(
        self, limit: int = 50, *, shop_id: Optional[str] = None
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM risk_logs WHERE 1=1"
        params: list[Any] = []
        if shop_id:
            sql += " AND shop_id=?"
            params.append(shop_id)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(max(1, limit))
        return [dict(row) for row in self._query(sql, tuple(params))]

    # ------------------------------------------------------------------
    # 统计 / 洞察
    # ------------------------------------------------------------------
    def today_stats(self, shop_id: Optional[str] = None) -> dict[str, Any]:
        today = now_beijing().strftime("%Y-%m-%d")
        start = f"{today} 00:00:00"
        clause, params = self._shop_clause(shop_id)

        def count(sql: str, extra: tuple = ()) -> int:
            rows = self._query(sql + clause, extra + params)
            return int(rows[0]["n"]) if rows else 0

        return {
            "date": today,
            "shop_id": shop_id or "all",
            "inbound_messages": count(
                "SELECT COUNT(*) AS n FROM messages WHERE direction='in' AND created_at>=?", (start,)
            ),
            "conversations": count(
                "SELECT COUNT(*) AS n FROM conversations WHERE last_in_at>=?", (start,)
            ),
            "auto_replies": count(
                "SELECT COUNT(*) AS n FROM replies WHERE status='sent' AND created_at>=?", (start,)
            ),
            "transfers": count(
                "SELECT COUNT(*) AS n FROM replies WHERE result='transfer' AND status='sent'"
                " AND created_at>=?",
                (start,),
            ),
            "failed_replies": count(
                "SELECT COUNT(*) AS n FROM replies WHERE status IN ('rejected','failed')"
                " AND created_at>=?",
                (start,),
            ),
            "risk_events": count(
                "SELECT COUNT(*) AS n FROM risk_logs WHERE created_at>=?", (start,)
            ),
        }

    def shop_today_summary(self) -> list[dict[str, Any]]:
        """按店铺汇总今日数据（面板「店铺状态」用）。"""
        rows = self._query(
            "SELECT shop_id, COUNT(*) AS n FROM conversations WHERE last_in_at>=?"
            " GROUP BY shop_id",
            (now_beijing().strftime("%Y-%m-%d 00:00:00"),),
        )
        shops = [str(row["shop_id"]) for row in rows]
        return [self.today_stats(shop_id) for shop_id in shops]

    def reply_result_breakdown(self, shop_id: Optional[str] = None) -> dict[str, int]:
        sql = "SELECT result, COUNT(*) AS n FROM replies WHERE result <> ''"
        params: list[Any] = []
        if shop_id:
            sql += " AND shop_id=?"
            params.append(shop_id)
        rows = self._query(sql + " GROUP BY result", tuple(params))
        return {str(row["result"]): int(row["n"]) for row in rows}

    def hourly_counts(self, *, shop_id: Optional[str] = None, days: int = 1) -> list[dict[str, Any]]:
        """按小时统计进线量（最近 N 天，最多 48 小时窗口）。"""
        since = iso(now_beijing() - timedelta(days=max(1, days)))
        clause, params = self._shop_clause(shop_id)
        rows = self._query(
            "SELECT substr(created_at, 1, 13) AS hour, COUNT(*) AS n FROM messages"
            " WHERE direction='in' AND created_at>=?" + clause + " GROUP BY hour ORDER BY hour",
            (since,) + params,
        )
        return [{"hour": str(row["hour"]), "count": int(row["n"])} for row in rows]

    def transfer_reasons(
        self, *, shop_id: Optional[str] = None, days: int = 7, limit: int = 10
    ) -> list[dict[str, Any]]:
        since = iso(now_beijing() - timedelta(days=max(1, days)))
        clause, params = self._shop_clause(shop_id)
        rows = self._query(
            "SELECT reason, COUNT(*) AS n FROM replies WHERE result='transfer' AND status='sent'"
            " AND created_at>=? AND reason <> ''" + clause + " GROUP BY reason ORDER BY n DESC LIMIT ?",
            (since,) + params + (max(1, limit),),
        )
        return [{"reason": str(row["reason"]), "count": int(row["n"])} for row in rows]

    def top_rules(
        self, *, shop_id: Optional[str] = None, days: int = 7, limit: int = 10
    ) -> list[dict[str, Any]]:
        since = iso(now_beijing() - timedelta(days=max(1, days)))
        clause, params = self._shop_clause(shop_id)
        rows = self._query(
            "SELECT rule_id, COUNT(*) AS n FROM replies WHERE status='sent' AND rule_id <> ''"
            " AND created_at>=?" + clause + " GROUP BY rule_id ORDER BY n DESC LIMIT ?",
            (since,) + params + (max(1, limit),),
        )
        return [{"rule_id": str(row["rule_id"]), "count": int(row["n"])} for row in rows]

    def knowledge_gaps(
        self, *, shop_id: Optional[str] = None, days: int = 7, limit: int = 10
    ) -> list[dict[str, Any]]:
        """知识缺口：没有自动答好（no_match / 转人工）的客户原话。"""
        since = iso(now_beijing() - timedelta(days=max(1, days)))
        clause, params = self._shop_clause(shop_id)
        rows = self._query(
            "SELECT question, COUNT(*) AS n FROM replies WHERE question <> ''"
            " AND (result='no_match' OR (result='transfer' AND"
            " (reason LIKE '%无匹配%' OR reason LIKE '%no_match%')))"
            " AND created_at>=?" + clause
            + " GROUP BY question ORDER BY n DESC LIMIT ?",
            (since,) + params + (max(1, limit),),
        )
        return [{"question": str(row["question"])[:120], "count": int(row["n"])} for row in rows]

    def dialogue_rows(
        self, *, shop_id: Optional[str] = None, days: int = 7, limit: int = 300
    ) -> list[dict[str, Any]]:
        """质检用：返回最近 N 天有消息的会话及其消息（按会话聚合）。"""
        since = iso(now_beijing() - timedelta(days=max(1, days)))
        clause, params = self._shop_clause(shop_id)
        convs = self._query(
            "SELECT shop_id, uid, nickname, last_in_at, last_result FROM conversations"
            " WHERE last_in_at>=?" + clause + " ORDER BY last_in_at DESC LIMIT ?",
            (since,) + params + (max(1, limit),),
        )
        result: list[dict[str, Any]] = []
        for conv in convs:
            messages = self._query(
                "SELECT direction, role, content, created_at FROM messages"
                " WHERE shop_id=? AND uid=? AND created_at>=? ORDER BY id",
                (conv["shop_id"], conv["uid"], since),
            )
            result.append(
                {
                    "shop_id": conv["shop_id"],
                    "uid": conv["uid"],
                    "nickname": conv["nickname"],
                    "last_in_at": conv["last_in_at"],
                    "last_result": conv["last_result"],
                    "messages": [dict(row) for row in messages],
                }
            )
        return result

    # ------------------------------------------------------------------
    # 待处理队列
    # ------------------------------------------------------------------
    def pending_conversations(
        self,
        *,
        shop_id: Optional[str] = None,
        overdue_seconds: int = 120,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """需要人工关注的会话：等待超时 / 转接失败 / 人工接管中。"""
        now = now_beijing()
        threshold = iso(now - timedelta(seconds=max(30, overdue_seconds)))
        clause, params = self._shop_clause(shop_id)
        rows = self._query(
            "SELECT * FROM conversations WHERE ("
            " (last_in_at IS NOT NULL AND last_in_at >= ? AND (last_reply_at IS NULL OR last_reply_at < last_in_at))"
            " OR last_result='transfer' OR last_result='transfer_failed'"
            " OR (human_takeover_until IS NOT NULL AND human_takeover_until > ?)"
            ")" + clause + " ORDER BY COALESCE(last_in_at, updated_at) DESC LIMIT ?",
            (threshold, iso(now)) + params + (max(1, limit),),
        )
        result = []
        for row in rows:
            conv = dict(row)
            waiting = None
            if conv.get("last_in_at") and (
                not conv.get("last_reply_at") or conv["last_reply_at"] < conv["last_in_at"]
            ):
                try:
                    start = datetime.strptime(conv["last_in_at"], "%Y-%m-%d %H:%M:%S").replace(
                        tzinfo=now.tzinfo
                    )
                    waiting = max(0, int((now - start).total_seconds()))
                except (ValueError, TypeError):
                    waiting = None
            conv["waiting_seconds"] = waiting
            if conv.get("human_takeover_until"):
                conv["human_active"] = conv["human_takeover_until"] > iso(now)
            result.append(conv)
        return result

    # ------------------------------------------------------------------
    # 导出
    # ------------------------------------------------------------------
    def export(
        self, path: Path, *, days: int = 30, shop_id: Optional[str] = None
    ) -> int:
        since = iso(now_beijing() - timedelta(days=max(1, days)))
        clause, params = self._shop_clause(shop_id)

        def rows(table: str) -> list[dict[str, Any]]:
            return [
                dict(r)
                for r in self._query(
                    f"SELECT * FROM {table} WHERE created_at>=?" + clause + " ORDER BY id",
                    (since,) + params,
                )
            ]

        conversations = self.list_conversations(limit=10000, shop_id=shop_id)
        data = {
            "exported_at": iso(now_beijing()),
            "since": since,
            "shop_id": shop_id or "all",
            "conversations": conversations,
            "messages": rows("messages"),
            "replies": rows("replies"),
            "risk_logs": rows("risk_logs"),
        }
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return len(data["messages"])

    # ------------------------------------------------------------------
    # KV
    # ------------------------------------------------------------------
    def set_kv(self, key: str, value: Any) -> None:
        self._execute(
            "INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value, ensure_ascii=False)),
        )

    def get_kv(self, key: str, default: Any = None) -> Any:
        rows = self._query("SELECT value FROM kv WHERE key=?", (key,))
        if not rows:
            return default
        try:
            return json.loads(rows[0]["value"])
        except json.JSONDecodeError:
            return default


def stored_message_from_row(row: dict[str, Any]) -> StoredMessage:
    return StoredMessage(
        uid=str(row.get("uid", "")),
        direction=str(row.get("direction", "")),
        role=str(row.get("role", "")),
        content=str(row.get("content", "")),
        context_type=str(row.get("context_type", "")),
        created_at=str(row.get("created_at", "")),
        msg_id=str(row.get("msg_id", "")),
        platform_time=float(row.get("platform_time") or 0.0),
        shop_id=str(row.get("shop_id") or DEFAULT_SHOP),
    )


__all__ = ["Store", "StoredMessage", "stored_message_from_row", "DEFAULT_SHOP"]
