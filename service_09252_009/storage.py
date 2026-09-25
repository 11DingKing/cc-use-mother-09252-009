"""SQLite 持久化端口。

设计要点
--------

- 所有写操作在调用方给定的事务中执行（``BEGIN IMMEDIATE``），
  SQLite 的库级写锁与 ``busy_timeout`` 共同保证并发写串行化，
  天然适配“读到的状态-评估-写回”的表决事务。
- 编号与序号统一使用 ``INTEGER PRIMARY KEY AUTOINCREMENT`` / 单调计数器，
  重启后严格保持创建顺序，不复用已删除行的编号。
- 时间全部以 UTC ISO-8601 文本存储（带偏移，词法序即时间序）。
- 幂等键（``idempotency`` 表）记录每个客户端键的处理结果，
  重试请求返回首次结果，且不产生重复业务记录。
- 审计日志（``audit_log``）只增不改，记录每一次写操作的主体、动作与参数。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator, Optional

from .models import (
    COND_CLASS_ADVISORY,
    COND_CLASS_REQUIRED,
    COND_MET,
    COND_PENDING,
    COND_WAIVED,
    STANCE_ACTIVE,
    STANCE_WITHDRAWN,
    STATUS_CANCELLED,
    STATUS_EFFECTIVE,
    STATUS_OPEN,
    STATUS_SUPERSEDED,
    Condition,
    Conflict,
    Institution,
    Proposal,
    Representative,
    Revision,
    Signature,
    Stance,
    Vote,
    iso,
    parse_ts,
)

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS institutions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS representatives (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    institution_id INTEGER NOT NULL REFERENCES institutions(id),
    name TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    deactivated_at TEXT
);

CREATE TABLE IF NOT EXISTS proposals (
    id INTEGER NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    language TEXT NOT NULL DEFAULT 'zh',
    author_institution_id INTEGER REFERENCES institutions(id),
    quorum_required INTEGER NOT NULL,
    deadline TEXT,
    status TEXT NOT NULL DEFAULT 'open',
    based_on_version INTEGER,
    created_at TEXT NOT NULL,
    effective_at TEXT,
    superseded_at TEXT,
    cancelled_at TEXT,
    PRIMARY KEY (id, version)
);

-- 单议题版本序列，保证修订版本号在并发与重启后仍单调递增。
CREATE TABLE IF NOT EXISTS proposal_seq (
    proposal_id INTEGER PRIMARY KEY,
    next_version INTEGER NOT NULL
);

-- 全局议题编号簿：INTEGER 主键自增仅在单表内可靠，
-- 议题编号独立于此，保证跨重启单调、不复用。
CREATE TABLE IF NOT EXISTS proposal_id_book (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    next_id INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS conflicts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    proposal_id INTEGER NOT NULL,
    version INTEGER NOT NULL,
    institution_id INTEGER NOT NULL REFERENCES institutions(id),
    reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE (proposal_id, version, institution_id)
);

CREATE TABLE IF NOT EXISTS conditions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    proposal_id INTEGER NOT NULL,
    version INTEGER NOT NULL,
    seq INTEGER NOT NULL,
    title TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL DEFAULT 'required',
    owner_institution_id INTEGER REFERENCES institutions(id),
    created_by_institution_id INTEGER REFERENCES institutions(id),
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    satisfied_at TEXT,
    satisfied_by_institution_id INTEGER,
    waiver TEXT,
    waived_at TEXT,
    waived_by_institution_id INTEGER,
    UNIQUE (proposal_id, version, seq)
);

CREATE TABLE IF NOT EXISTS stances (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    proposal_id INTEGER NOT NULL,
    version INTEGER NOT NULL,
    institution_id INTEGER NOT NULL REFERENCES institutions(id),
    representative_id INTEGER NOT NULL REFERENCES representatives(id),
    vote TEXT NOT NULL,
    rationale TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    withdrawn_at TEXT,
    withdraw_reason TEXT,
    UNIQUE (proposal_id, version, institution_id)
);

CREATE TABLE IF NOT EXISTS signatures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    proposal_id INTEGER NOT NULL,
    version INTEGER NOT NULL,
    institution_id INTEGER NOT NULL REFERENCES institutions(id),
    representative_id INTEGER NOT NULL REFERENCES representatives(id),
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE (proposal_id, version, institution_id)
);

CREATE TABLE IF NOT EXISTS revisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    proposal_id INTEGER NOT NULL,
    from_version INTEGER NOT NULL,
    new_version INTEGER NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    requested_by_institution_id INTEGER,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS idempotency (
    key TEXT PRIMARY KEY,
    scope TEXT NOT NULL,
    created_at TEXT NOT NULL,
    response TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    actor_institution_id INTEGER,
    actor_representative_id INTEGER,
    action TEXT NOT NULL,
    target_type TEXT NOT NULL,
    target_id TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '',
    result TEXT NOT NULL
);
"""


class SQLiteStore:
    """线程安全的 SQLite 存储。每个事务使用独立连接。"""

    def __init__(self, path: str | Path = ":memory:"):
        self.path = str(path)
        # check_same_thread=False：连接在事务期间仅被一个线程持有。
        self._master = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._master.row_factory = sqlite3.Row
        self._master.execute("PRAGMA busy_timeout=5000")
        if self.path != ":memory:":
            self._master.execute("PRAGMA journal_mode=WAL")
        self._master.execute("PRAGMA foreign_keys=ON")
        self._lock = threading.RLock()
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            self._master.executescript(SCHEMA)

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """开启一个串行化写事务（BEGIN IMMEDIATE）。

        内存库使用共享主连接（并加锁）；文件库为每事务开独立连接，
        WAL 下读不阻塞、写全局串行，满足并发投票测试。
        """
        if self.path == ":memory:":
            conn = self._master
            self._lock.acquire()
            should_close = False
        else:
            conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("PRAGMA foreign_keys=ON")
            should_close = True
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            if should_close:
                conn.close()
            else:
                self._lock.release()

    # ---- 审计与幂等 ----------------------------------------------------

    def write_audit(
        self,
        conn: sqlite3.Connection,
        *,
        at: datetime,
        action: str,
        target_type: str,
        target_id: str,
        payload: dict | None = None,
        result: str = "ok",
        actor_institution_id: Optional[int] = None,
        actor_representative_id: Optional[int] = None,
    ) -> None:
        conn.execute(
            """INSERT INTO audit_log
               (at, actor_institution_id, actor_representative_id, action,
                target_type, target_id, payload, result)
               VALUES (?,?,?,?,?,?,?,?)""",
            (
                iso(at), actor_institution_id, actor_representative_id, action,
                target_type, target_id,
                json.dumps(payload or {}, ensure_ascii=False, sort_keys=True), result,
            ),
        )

    def get_idempotent(self, conn: sqlite3.Connection, key: str) -> Optional[dict]:
        row = conn.execute(
            "SELECT response FROM idempotency WHERE key=?", (key,)
        ).fetchone()
        return json.loads(row["response"]) if row else None

    def put_idempotent(self, conn: sqlite3.Connection, key: str, scope: str,
                       at: datetime, response: dict) -> None:
        conn.execute(
            """INSERT OR IGNORE INTO idempotency (key, scope, created_at, response)
               VALUES (?,?,?,?)""",
            (key, scope, iso(at), json.dumps(response, ensure_ascii=False, sort_keys=True)),
        )

    # ---- 机构与代表 ----------------------------------------------------

    def create_institution(self, conn, code: str, name: str, at: datetime) -> Institution:
        cur = conn.execute(
            "INSERT INTO institutions (code, name, created_at) VALUES (?,?,?)",
            (code, name, iso(at)),
        )
        return self.get_institution(conn, cur.lastrowid)

    def get_institution(self, conn, institution_id: int) -> Institution:
        row = conn.execute(
            "SELECT * FROM institutions WHERE id=?", (institution_id,)
        ).fetchone()
        if row is None:
            raise LookupError(f"机构不存在: {institution_id}")
        return _institution(row)

    def find_institution_by_code(self, conn, code: str) -> Optional[Institution]:
        row = conn.execute("SELECT * FROM institutions WHERE code=?", (code,)).fetchone()
        return _institution(row) if row else None

    def list_institutions(self, conn) -> list[Institution]:
        return [_institution(r) for r in conn.execute(
            "SELECT * FROM institutions ORDER BY id")]

    def add_representative(self, conn, institution_id: int, name: str,
                           at: datetime) -> Representative:
        cur = conn.execute(
            "INSERT INTO representatives (institution_id, name, created_at) VALUES (?,?,?)",
            (institution_id, name, iso(at)),
        )
        return self.get_representative(conn, cur.lastrowid)

    def replace_representative(self, conn, institution_id: int, name: str,
                               at: datetime) -> Representative:
        """更换代表：停用该院校当前全部在任代表，再任命新代表。"""
        conn.execute(
            "UPDATE representatives SET active=0, deactivated_at=? "
            "WHERE institution_id=? AND active=1",
            (iso(at), institution_id),
        )
        return self.add_representative(conn, institution_id, name, at)

    def get_representative(self, conn, representative_id: int) -> Representative:
        row = conn.execute(
            "SELECT * FROM representatives WHERE id=?", (representative_id,)
        ).fetchone()
        if row is None:
            raise LookupError(f"代表不存在: {representative_id}")
        return _representative(row)

    # ---- 提案 ----------------------------------------------------------

    def insert_proposal(self, conn, *, proposal_id: int, version: int, title: str,
                        body: str, language: str, author_institution_id: Optional[int],
                        quorum_required: int, deadline: Optional[datetime],
                        status: str, based_on_version: Optional[int],
                        at: datetime) -> Proposal:
        conn.execute(
            """INSERT INTO proposals
               (id, version, title, body, language, author_institution_id,
                quorum_required, deadline, status, based_on_version, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (proposal_id, version, title, body, language, author_institution_id,
             quorum_required, iso(deadline) if deadline else None, status,
             based_on_version, iso(at)),
        )
        # next_version 永远指向“下一可用版本号”。
        conn.execute(
            "INSERT OR IGNORE INTO proposal_seq (proposal_id, next_version) VALUES (?,?)",
            (proposal_id, version + 1),
        )
        return self.get_proposal(conn, proposal_id, version)

    def allocate_proposal_id(self, conn) -> int:
        """分配跨重启单调递增的议题编号。"""
        conn.execute(
            "INSERT OR IGNORE INTO proposal_id_book (id, next_id) VALUES (1,1)")
        row = conn.execute(
            "SELECT next_id FROM proposal_id_book WHERE id=1").fetchone()
        next_id = row["next_id"]
        conn.execute(
            "UPDATE proposal_id_book SET next_id=? WHERE id=1", (next_id + 1,))
        return next_id

    def next_version(self, conn, proposal_id: int) -> int:
        conn.execute(
            "INSERT OR IGNORE INTO proposal_seq (proposal_id, next_version) VALUES (?,2)",
            (proposal_id,),
        )
        row = conn.execute(
            "SELECT next_version FROM proposal_seq WHERE proposal_id=?",
            (proposal_id,),
        ).fetchone()
        next_version = row["next_version"]
        conn.execute(
            "UPDATE proposal_seq SET next_version=? WHERE proposal_id=?",
            (next_version + 1, proposal_id),
        )
        return next_version

    def get_proposal(self, conn, proposal_id: int, version: Optional[int] = None) -> Proposal:
        if version is None:
            row = conn.execute(
                "SELECT * FROM proposals WHERE id=? ORDER BY version DESC LIMIT 1",
                (proposal_id,),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM proposals WHERE id=? AND version=?",
                (proposal_id, version),
            ).fetchone()
        if row is None:
            raise LookupError(f"提案不存在: {proposal_id}" +
                              (f" v{version}" if version else ""))
        return _proposal(row)

    def list_proposals(self, conn, *, status: Optional[str] = None) -> list[Proposal]:
        if status:
            rows = conn.execute(
                "SELECT * FROM proposals WHERE status=? ORDER BY id, version",
                (status,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM proposals ORDER BY id, version").fetchall()
        return [_proposal(r) for r in rows]

    def update_proposal_status(self, conn, proposal_id: int, version: int, *,
                               status: str, at: datetime) -> None:
        sets = ["status=?"]
        params: list[Any] = [status]
        if status == STATUS_EFFECTIVE:
            sets.append("effective_at=COALESCE(effective_at,?)")
            params.append(iso(at))
        elif status == STATUS_OPEN:
            sets.append("effective_at=NULL")
        elif status == STATUS_SUPERSEDED:
            sets.append("superseded_at=?")
            params.append(iso(at))
        elif status == STATUS_CANCELLED:
            sets.append("cancelled_at=?")
            params.append(iso(at))
        params.extend([proposal_id, version])
        conn.execute(
            f"UPDATE proposals SET {', '.join(sets)} WHERE id=? AND version=?",
            params,
        )

    # ---- 冲突 / 条件 / 立场 / 签署 / 修订 ------------------------------

    def add_conflict(self, conn, proposal_id: int, version: int, institution_id: int,
                     reason: str, at: datetime) -> Conflict:
        cur = conn.execute(
            """INSERT INTO conflicts
               (proposal_id, version, institution_id, reason, created_at)
               VALUES (?,?,?,?,?)""",
            (proposal_id, version, institution_id, reason, iso(at)),
        )
        row = conn.execute("SELECT * FROM conflicts WHERE id=?", (cur.lastrowid,)).fetchone()
        return _conflict(row)

    def list_conflicts(self, conn, proposal_id: int, version: int) -> list[Conflict]:
        return [_conflict(r) for r in conn.execute(
            "SELECT * FROM conflicts WHERE proposal_id=? AND version=? ORDER BY id",
            (proposal_id, version))]

    def add_condition(self, conn, *, proposal_id: int, version: int, seq: int,
                      title: str, detail: str, kind: str,
                      owner_institution_id: Optional[int],
                      created_by_institution_id: Optional[int],
                      at: datetime) -> Condition:
        conn.execute(
            """INSERT INTO conditions
               (proposal_id, version, seq, title, detail, kind,
                owner_institution_id, created_by_institution_id, status, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (proposal_id, version, seq, title, detail, kind, owner_institution_id,
             created_by_institution_id, COND_PENDING, iso(at)),
        )
        return self.get_condition_by_seq(conn, proposal_id, version, seq)

    def get_condition_by_seq(self, conn, proposal_id: int, version: int,
                             seq: int) -> Condition:
        row = conn.execute(
            "SELECT * FROM conditions WHERE proposal_id=? AND version=? AND seq=?",
            (proposal_id, version, seq),
        ).fetchone()
        if row is None:
            raise LookupError(f"条件不存在: 议题{proposal_id} v{version} 条件#{seq}")
        return _condition(row)

    def get_condition(self, conn, condition_id: int) -> Condition:
        row = conn.execute(
            "SELECT * FROM conditions WHERE id=?", (condition_id,)).fetchone()
        if row is None:
            raise LookupError(f"条件不存在: {condition_id}")
        return _condition(row)

    def list_conditions(self, conn, proposal_id: int, version: int) -> list[Condition]:
        return [_condition(r) for r in conn.execute(
            "SELECT * FROM conditions WHERE proposal_id=? AND version=? ORDER BY seq",
            (proposal_id, version))]

    def set_condition_status(self, conn, condition_id: int, *, status: str, at: datetime,
                             actor_institution_id: int, waiver: Optional[str] = None) -> None:
        if status == COND_MET:
            conn.execute(
                """UPDATE conditions SET status=?, satisfied_at=?,
                   satisfied_by_institution_id=? WHERE id=?""",
                (status, iso(at), actor_institution_id, condition_id),
            )
        elif status == COND_PENDING:
            # 重新打开（撤销满足/解除）
            conn.execute(
                """UPDATE conditions SET status=?, satisfied_at=NULL,
                   satisfied_by_institution_id=NULL, waiver=NULL,
                   waived_at=NULL, waived_by_institution_id=NULL WHERE id=?""",
                (status, condition_id),
            )
        elif status == COND_WAIVED:
            conn.execute(
                """UPDATE conditions SET status=?, waiver=?, waived_at=?,
                   waived_by_institution_id=? WHERE id=?""",
                (status, waiver or "", iso(at), actor_institution_id, condition_id),
            )
        else:
            raise ValueError(f"未知条件状态: {status}")

    def upsert_stance(self, conn, *, proposal_id: int, version: int,
                      institution_id: int, representative_id: int, vote: Vote,
                      rationale: str, at: datetime) -> Stance:
        conn.execute(
            """INSERT INTO stances
               (proposal_id, version, institution_id, representative_id, vote,
                rationale, status, created_at, updated_at)
               VALUES (?,?,?,?,?,?, 'active', ?, ?)
               ON CONFLICT (proposal_id, version, institution_id) DO UPDATE SET
                 representative_id=excluded.representative_id,
                 vote=excluded.vote,
                 rationale=excluded.rationale,
                 status='active',
                 updated_at=excluded.updated_at,
                 withdrawn_at=NULL,
                 withdraw_reason=NULL""",
            (proposal_id, version, institution_id, representative_id, vote.value,
             rationale, iso(at), iso(at)),
        )
        row = conn.execute(
            "SELECT * FROM stances WHERE proposal_id=? AND version=? AND institution_id=?",
            (proposal_id, version, institution_id),
        ).fetchone()
        return _stance(row)

    def withdraw_stance(self, conn, proposal_id: int, version: int,
                        institution_id: int, reason: Optional[str],
                        at: datetime) -> None:
        conn.execute(
            """UPDATE stances SET status='withdrawn', withdrawn_at=?,
               withdraw_reason=?, updated_at=?
               WHERE proposal_id=? AND version=? AND institution_id=? AND status='active'""",
            (iso(at), reason, iso(at), proposal_id, version, institution_id),
        )

    def list_stances(self, conn, proposal_id: int, version: int) -> list[Stance]:
        return [_stance(r) for r in conn.execute(
            "SELECT * FROM stances WHERE proposal_id=? AND version=? ORDER BY id",
            (proposal_id, version))]

    def add_signature(self, conn, *, proposal_id: int, version: int,
                      institution_id: int, representative_id: int, note: str,
                      at: datetime) -> Signature:
        conn.execute(
            """INSERT INTO signatures
               (proposal_id, version, institution_id, representative_id, note, created_at)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT (proposal_id, version, institution_id) DO UPDATE SET
                 representative_id=excluded.representative_id,
                 note=excluded.note,
                 created_at=excluded.created_at""",
            (proposal_id, version, institution_id, representative_id, note, iso(at)),
        )
        row = conn.execute(
            "SELECT * FROM signatures WHERE proposal_id=? AND version=? AND institution_id=?",
            (proposal_id, version, institution_id),
        ).fetchone()
        return _signature(row)

    def list_signatures(self, conn, proposal_id: int, version: int) -> list[Signature]:
        return [_signature(r) for r in conn.execute(
            "SELECT * FROM signatures WHERE proposal_id=? AND version=? ORDER BY id",
            (proposal_id, version))]

    def add_revision(self, conn, *, proposal_id: int, from_version: int,
                     new_version: int, reason: str,
                     requested_by_institution_id: Optional[int],
                     at: datetime) -> Revision:
        cur = conn.execute(
            """INSERT INTO revisions
               (proposal_id, from_version, new_version, reason,
                requested_by_institution_id, created_at)
               VALUES (?,?,?,?,?,?)""",
            (proposal_id, from_version, new_version, reason,
             requested_by_institution_id, iso(at)),
        )
        row = conn.execute("SELECT * FROM revisions WHERE id=?", (cur.lastrowid,)).fetchone()
        return _revision(row)

    def list_revisions(self, conn, proposal_id: int) -> list[Revision]:
        return [_revision(r) for r in conn.execute(
            "SELECT * FROM revisions WHERE proposal_id=? ORDER BY id",
            (proposal_id,))]

    def list_audit(self, conn, *, proposal_id: Optional[int] = None,
                   limit: int = 100) -> list[dict]:
        if proposal_id is not None:
            rows = conn.execute(
                """SELECT * FROM audit_log WHERE target_id LIKE ? OR target_id=?
                   ORDER BY seq DESC LIMIT ?""",
                (f"{proposal_id}:%", str(proposal_id), limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM audit_log ORDER BY seq DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def close(self) -> None:
        self._master.close()


# ---- row -> dataclass ----------------------------------------------------

def _institution(row: sqlite3.Row) -> Institution:
    return Institution(
        id=row["id"], code=row["code"], name=row["name"],
        created_at=parse_ts(row["created_at"]),
    )


def _representative(row: sqlite3.Row) -> Representative:
    return Representative(
        id=row["id"], institution_id=row["institution_id"], name=row["name"],
        active=bool(row["active"]), created_at=parse_ts(row["created_at"]),
        deactivated_at=parse_ts(row["deactivated_at"]) if row["deactivated_at"] else None,
    )


def _proposal(row: sqlite3.Row) -> Proposal:
    return Proposal(
        id=row["id"], version=row["version"], title=row["title"], body=row["body"],
        language=row["language"], author_institution_id=row["author_institution_id"],
        quorum_required=row["quorum_required"],
        deadline=parse_ts(row["deadline"]) if row["deadline"] else None,
        status=row["status"], based_on_version=row["based_on_version"],
        created_at=parse_ts(row["created_at"]),
        effective_at=parse_ts(row["effective_at"]) if row["effective_at"] else None,
        superseded_at=parse_ts(row["superseded_at"]) if row["superseded_at"] else None,
        cancelled_at=parse_ts(row["cancelled_at"]) if row["cancelled_at"] else None,
    )


def _conflict(row: sqlite3.Row) -> Conflict:
    return Conflict(
        id=row["id"], proposal_id=row["proposal_id"], version=row["version"],
        institution_id=row["institution_id"], reason=row["reason"],
        created_at=parse_ts(row["created_at"]),
    )


def _condition(row: sqlite3.Row) -> Condition:
    return Condition(
        id=row["id"], proposal_id=row["proposal_id"], version=row["version"],
        seq=row["seq"], title=row["title"], detail=row["detail"],
        kind=row["kind"], owner_institution_id=row["owner_institution_id"],
        status=row["status"], created_at=parse_ts(row["created_at"]),
        created_by_institution_id=row["created_by_institution_id"],
        satisfied_at=parse_ts(row["satisfied_at"]) if row["satisfied_at"] else None,
        satisfied_by_institution_id=row["satisfied_by_institution_id"],
        waiver=row["waiver"], waived_at=parse_ts(row["waived_at"]) if row["waived_at"] else None,
        waived_by_institution_id=row["waived_by_institution_id"],
    )


def _stance(row: sqlite3.Row) -> Stance:
    return Stance(
        id=row["id"], proposal_id=row["proposal_id"], version=row["version"],
        institution_id=row["institution_id"], representative_id=row["representative_id"],
        vote=Vote(row["vote"]), rationale=row["rationale"], status=row["status"],
        created_at=parse_ts(row["created_at"]), updated_at=parse_ts(row["updated_at"]),
        withdrawn_at=parse_ts(row["withdrawn_at"]) if row["withdrawn_at"] else None,
        withdraw_reason=row["withdraw_reason"],
    )


def _signature(row: sqlite3.Row) -> Signature:
    return Signature(
        id=row["id"], proposal_id=row["proposal_id"], version=row["version"],
        institution_id=row["institution_id"], representative_id=row["representative_id"],
        note=row["note"], created_at=parse_ts(row["created_at"]),
    )


def _revision(row: sqlite3.Row) -> Revision:
    return Revision(
        id=row["id"], proposal_id=row["proposal_id"], from_version=row["from_version"],
        new_version=row["new_version"], reason=row["reason"],
        requested_by_institution_id=row["requested_by_institution_id"],
        created_at=parse_ts(row["created_at"]),
    )
