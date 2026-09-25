"""SQLite 持久化。

写方法接收事务内连接以支持服务层组合多语句原子操作；
读方法自行打开短事务。重启后依赖 INTEGER PRIMARY KEY AUTOINCREMENT
保持审计顺序，所有时间以 UTC 文本保存。
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from .models import (
    AuditEvent,
    Condition,
    ConditionStatus,
    ConflictDeclaration,
    Institution,
    Issue,
    IssueStatus,
    Proposal,
    RevisionRequest,
    Signature,
    Stance,
    Vote,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS institutions (
    code        TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    delegate    TEXT,
    is_member   INTEGER NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS issues (
    id               TEXT PRIMARY KEY,
    title            TEXT NOT NULL,
    status           TEXT NOT NULL,
    current_seq      INTEGER NOT NULL,
    adopted_seq      INTEGER,
    adopted_at       TEXT,
    deadline_utc     TEXT NOT NULL,
    tzname           TEXT NOT NULL,
    parent_issue_id  TEXT,
    quorum_ratio     REAL NOT NULL,
    signature_ratio  REAL NOT NULL,
    created_at       TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS proposals (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_id         TEXT NOT NULL REFERENCES issues(id),
    seq              INTEGER NOT NULL,
    title            TEXT NOT NULL,
    body             TEXT NOT NULL,
    language         TEXT NOT NULL,
    summary          TEXT NOT NULL,
    supersedes_seq   INTEGER,
    author_institution TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    UNIQUE(issue_id, seq)
);
CREATE TABLE IF NOT EXISTS conditions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_id     TEXT NOT NULL REFERENCES issues(id),
    proposal_seq INTEGER NOT NULL,
    code         TEXT NOT NULL,
    raised_by    TEXT NOT NULL,
    description  TEXT NOT NULL,
    status       TEXT NOT NULL,
    resolved_by  TEXT,
    note         TEXT,
    created_at   TEXT NOT NULL,
    resolved_at  TEXT,
    UNIQUE(issue_id, code)
);
CREATE TABLE IF NOT EXISTS votes (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_id     TEXT NOT NULL REFERENCES issues(id),
    proposal_seq INTEGER NOT NULL,
    institution  TEXT NOT NULL,
    delegate     TEXT NOT NULL,
    stance       TEXT NOT NULL,
    rationale    TEXT NOT NULL,
    active       INTEGER NOT NULL,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS votes_active_one
    ON votes(issue_id, proposal_seq, institution) WHERE active = 1;
CREATE TABLE IF NOT EXISTS signatures (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_id     TEXT NOT NULL REFERENCES issues(id),
    proposal_seq INTEGER NOT NULL,
    institution  TEXT NOT NULL,
    signer       TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    UNIQUE(issue_id, proposal_seq, institution)
);
CREATE TABLE IF NOT EXISTS conflicts (
    issue_id    TEXT NOT NULL REFERENCES issues(id),
    institution TEXT NOT NULL,
    declared    INTEGER NOT NULL,
    reason      TEXT,
    updated_at  TEXT NOT NULL,
    PRIMARY KEY(issue_id, institution)
);
CREATE TABLE IF NOT EXISTS revision_requests (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_id           TEXT NOT NULL REFERENCES issues(id),
    institution        TEXT NOT NULL,
    reason             TEXT NOT NULL,
    created_at         TEXT NOT NULL,
    successor_issue_id TEXT
);
CREATE TABLE IF NOT EXISTS audit_log (
    seq         INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id    TEXT NOT NULL UNIQUE,
    at          TEXT NOT NULL,
    event_type  TEXT NOT NULL,
    issue_id    TEXT,
    institution TEXT,
    actor       TEXT NOT NULL,
    payload     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS idempotency_keys (
    key        TEXT PRIMARY KEY,
    event_seq  INTEGER,
    result_ref TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


def _ts(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value)


class SQLiteRepository:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 15000")
        return conn

    def _initialize(self) -> None:
        with self.transaction(write=True) as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def transaction(self, write: bool = False) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # 机构
    # ------------------------------------------------------------------
    def insert_institution(self, conn: sqlite3.Connection, inst: Institution) -> None:
        conn.execute(
            "INSERT INTO institutions(code, name, delegate, is_member, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (inst.code, inst.name, inst.delegate, 1 if inst.is_member else 0, _ts(inst.created_at)),
        )

    def update_delegate(self, conn: sqlite3.Connection, code: str, delegate: str) -> None:
        conn.execute("UPDATE institutions SET delegate = ? WHERE code = ?", (delegate, code))

    def get_institution(self, code: str) -> Institution | None:
        with self.transaction() as conn:
            row = conn.execute("SELECT * FROM institutions WHERE code = ?", (code,)).fetchone()
        return _row_to_institution(row) if row else None

    def list_institutions(self) -> list[Institution]:
        with self.transaction() as conn:
            rows = conn.execute("SELECT * FROM institutions ORDER BY created_at, code").fetchall()
        return [_row_to_institution(r) for r in rows]

    # ------------------------------------------------------------------
    # 议题与提案
    # ------------------------------------------------------------------
    def insert_issue(self, conn: sqlite3.Connection, issue: Issue) -> None:
        conn.execute(
            "INSERT INTO issues(id, title, status, current_seq, adopted_seq, adopted_at,"
            " deadline_utc, tzname, parent_issue_id, quorum_ratio, signature_ratio, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                issue.id, issue.title, issue.status.value, issue.current_seq,
                issue.adopted_seq, _ts(issue.adopted_at) if issue.adopted_at else None,
                _ts(issue.deadline_utc), issue.tzname, issue.parent_issue_id,
                issue.quorum_ratio, issue.signature_ratio, _ts(issue.created_at),
            ),
        )

    def update_issue_state(
        self,
        conn: sqlite3.Connection,
        issue_id: str,
        status: IssueStatus,
        current_seq: int | None = None,
        adopted_seq: int | None = None,
        adopted_at: datetime | None = None,
    ) -> None:
        conn.execute(
            "UPDATE issues SET status = ?, current_seq = COALESCE(?, current_seq),"
            " adopted_seq = ?, adopted_at = ? WHERE id = ?",
            (
                status.value, current_seq, adopted_seq,
                _ts(adopted_at) if adopted_at else None, issue_id,
            ),
        )

    def get_issue(self, issue_id: str) -> Issue | None:
        with self.transaction() as conn:
            row = conn.execute("SELECT * FROM issues WHERE id = ?", (issue_id,)).fetchone()
        return _row_to_issue(row) if row else None

    def list_issues(self) -> list[Issue]:
        with self.transaction() as conn:
            rows = conn.execute("SELECT * FROM issues ORDER BY created_at, id").fetchall()
        return [_row_to_issue(r) for r in rows]

    def insert_proposal(self, conn: sqlite3.Connection, p: Proposal) -> None:
        conn.execute(
            "INSERT INTO proposals(issue_id, seq, title, body, language, summary,"
            " supersedes_seq, author_institution, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (p.issue_id, p.seq, p.title, p.body, p.language, p.summary,
             p.supersedes_seq, p.author_institution, _ts(p.created_at)),
        )

    def get_proposal(self, issue_id: str, seq: int) -> Proposal | None:
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM proposals WHERE issue_id = ? AND seq = ?", (issue_id, seq)
            ).fetchone()
        return _row_to_proposal(row) if row else None

    def list_proposals(self, issue_id: str) -> list[Proposal]:
        with self.transaction() as conn:
            rows = conn.execute(
                "SELECT * FROM proposals WHERE issue_id = ? ORDER BY seq", (issue_id,)
            ).fetchall()
        return [_row_to_proposal(r) for r in rows]

    # ------------------------------------------------------------------
    # 条件
    # ------------------------------------------------------------------
    def insert_condition(self, conn: sqlite3.Connection, c: Condition) -> None:
        conn.execute(
            "INSERT INTO conditions(issue_id, proposal_seq, code, raised_by, description,"
            " status, resolved_by, note, created_at, resolved_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (c.issue_id, c.proposal_seq, c.code, c.raised_by, c.description,
             c.status.value, c.resolved_by, c.note, _ts(c.created_at),
             _ts(c.resolved_at) if c.resolved_at else None),
        )

    def get_condition(self, conn: sqlite3.Connection, issue_id: str, code: str) -> Condition | None:
        row = conn.execute(
            "SELECT * FROM conditions WHERE issue_id = ? AND code = ?", (issue_id, code)
        ).fetchone()
        return _row_to_condition(row) if row else None

    def list_conditions(self, issue_id: str) -> list[Condition]:
        with self.transaction() as conn:
            rows = conn.execute(
                "SELECT * FROM conditions WHERE issue_id = ? ORDER BY created_at, id", (issue_id,)
            ).fetchall()
        return [_row_to_condition(r) for r in rows]

    def update_condition(
        self,
        conn: sqlite3.Connection,
        condition_id: int,
        status: ConditionStatus,
        resolved_by: str | None,
        note: str | None,
        resolved_at: datetime | None,
    ) -> None:
        conn.execute(
            "UPDATE conditions SET status = ?, resolved_by = ?, note = ?, resolved_at = ?"
            " WHERE id = ?",
            (status.value, resolved_by, note, _ts(resolved_at) if resolved_at else None, condition_id),
        )

    def supersede_conditions(self, conn: sqlite3.Connection, issue_id: str, seq: int) -> int:
        """新版本提案产生后，旧版本上未决的条件自动归为 superseded。"""
        cur = conn.execute(
            "UPDATE conditions SET status = 'superseded'"
            " WHERE issue_id = ? AND proposal_seq < ? AND status = 'outstanding'",
            (issue_id, seq),
        )
        return cur.rowcount

    # ------------------------------------------------------------------
    # 利益冲突
    # ------------------------------------------------------------------
    def upsert_conflict(
        self,
        conn: sqlite3.Connection,
        issue_id: str,
        institution: str,
        declared: bool,
        reason: str | None,
        now: datetime,
    ) -> None:
        conn.execute(
            "INSERT INTO conflicts(issue_id, institution, declared, reason, updated_at)"
            " VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT(issue_id, institution) DO UPDATE SET"
            " declared = excluded.declared, reason = excluded.reason, updated_at = excluded.updated_at",
            (issue_id, institution, 1 if declared else 0, reason, _ts(now)),
        )

    def list_conflicts(self, conn: sqlite3.Connection, issue_id: str) -> list[ConflictDeclaration]:
        rows = conn.execute(
            "SELECT * FROM conflicts WHERE issue_id = ? ORDER BY institution", (issue_id,)
        ).fetchall()
        return [_row_to_conflict(r) for r in rows]

    # ------------------------------------------------------------------
    # 投票与签署
    # ------------------------------------------------------------------
    def insert_vote(self, conn: sqlite3.Connection, v: Vote) -> int:
        cur = conn.execute(
            "INSERT INTO votes(issue_id, proposal_seq, institution, delegate, stance,"
            " rationale, active, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (v.issue_id, v.proposal_seq, v.institution, v.delegate, v.stance.value,
             v.rationale, 1 if v.active else 0, _ts(v.created_at), _ts(v.updated_at)),
        )
        return int(cur.lastrowid)

    def deactivate_active_vote(
        self, conn: sqlite3.Connection, issue_id: str, seq: int, institution: str,
        now: datetime,
    ) -> int:
        cur = conn.execute(
            "UPDATE votes SET active = 0, updated_at = ?"
            " WHERE issue_id = ? AND proposal_seq = ? AND institution = ? AND active = 1",
            (_ts(now), issue_id, seq, institution),
        )
        return cur.rowcount

    def get_vote(
        self, conn: sqlite3.Connection, issue_id: str, seq: int, institution: str
    ) -> Vote | None:
        row = conn.execute(
            "SELECT * FROM votes WHERE issue_id = ? AND proposal_seq = ? AND institution = ?",
            (issue_id, seq, institution),
        ).fetchone()
        return _row_to_vote(row) if row else None

    def list_active_votes(self, conn: sqlite3.Connection, issue_id: str, seq: int) -> list[Vote]:
        rows = conn.execute(
            "SELECT * FROM votes WHERE issue_id = ? AND proposal_seq = ? AND active = 1"
            " ORDER BY updated_at, institution",
            (issue_id, seq),
        ).fetchall()
        return [_row_to_vote(r) for r in rows]

    def list_vote_history(self, issue_id: str) -> list[Vote]:
        with self.transaction() as conn:
            rows = conn.execute(
                "SELECT * FROM votes WHERE issue_id = ? ORDER BY updated_at, id", (issue_id,)
            ).fetchall()
        return [_row_to_vote(r) for r in rows]

    def deactivate_vote(self, conn: sqlite3.Connection, vote_id: int, now: datetime) -> None:
        conn.execute(
            "UPDATE votes SET active = 0, updated_at = ? WHERE id = ?", (_ts(now), vote_id)
        )

    def insert_signature(self, conn: sqlite3.Connection, s: Signature) -> None:
        conn.execute(
            "INSERT OR IGNORE INTO signatures(issue_id, proposal_seq, institution, signer, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (s.issue_id, s.proposal_seq, s.institution, s.signer, _ts(s.created_at)),
        )

    def get_signature(
        self, conn: sqlite3.Connection, issue_id: str, seq: int, institution: str
    ) -> Signature | None:
        row = conn.execute(
            "SELECT * FROM signatures WHERE issue_id = ? AND proposal_seq = ? AND institution = ?",
            (issue_id, seq, institution),
        ).fetchone()
        return _row_to_signature(row) if row else None

    def list_signatures(self, conn: sqlite3.Connection, issue_id: str, seq: int) -> list[Signature]:
        rows = conn.execute(
            "SELECT * FROM signatures WHERE issue_id = ? AND proposal_seq = ? ORDER BY created_at, id",
            (issue_id, seq),
        ).fetchall()
        return [_row_to_signature(r) for r in rows]

    # ------------------------------------------------------------------
    # 修订
    # ------------------------------------------------------------------
    def insert_revision_request(self, conn: sqlite3.Connection, r: RevisionRequest) -> int:
        cur = conn.execute(
            "INSERT INTO revision_requests(issue_id, institution, reason, created_at,"
            " successor_issue_id) VALUES (?, ?, ?, ?, ?)",
            (r.issue_id, r.institution, r.reason, _ts(r.created_at), r.successor_issue_id),
        )
        return int(cur.lastrowid)

    def list_revision_requests(self, issue_id: str) -> list[RevisionRequest]:
        with self.transaction() as conn:
            rows = conn.execute(
                "SELECT * FROM revision_requests WHERE issue_id = ? ORDER BY created_at, id",
                (issue_id,),
            ).fetchall()
        return [_row_to_revision(r) for r in rows]

    # ------------------------------------------------------------------
    # 审计与幂等
    # ------------------------------------------------------------------
    def insert_audit(self, conn: sqlite3.Connection, event: AuditEvent) -> int:
        cur = conn.execute(
            "INSERT INTO audit_log(event_id, at, event_type, issue_id, institution, actor, payload)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (event.event_id, _ts(event.at), event.event_type, event.issue_id,
             event.institution, event.actor, event.payload),
        )
        return int(cur.lastrowid)

    def list_audit(self, issue_id: str | None = None) -> list[AuditEvent]:
        with self.transaction() as conn:
            if issue_id is None:
                rows = conn.execute("SELECT * FROM audit_log ORDER BY seq").fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM audit_log WHERE issue_id = ? ORDER BY seq", (issue_id,)
                ).fetchall()
        return [_row_to_audit(r) for r in rows]

    def store_idempotency(
        self, conn: sqlite3.Connection, key: str, event_seq: int, result_ref: str, now: datetime
    ) -> None:
        conn.execute(
            "INSERT INTO idempotency_keys(key, event_seq, result_ref, created_at)"
            " VALUES (?, ?, ?, ?)",
            (key, event_seq, result_ref, _ts(now)),
        )

    def get_idempotency(self, key: str) -> tuple[str, int | None] | None:
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT result_ref, event_seq FROM idempotency_keys WHERE key = ?", (key,)
            ).fetchone()
        return (row["result_ref"], row["event_seq"]) if row else None


# ----------------------------------------------------------------------
# 行映射
# ----------------------------------------------------------------------
def _row_to_institution(row: sqlite3.Row) -> Institution:
    return Institution(
        code=row["code"], name=row["name"], delegate=row["delegate"],
        is_member=bool(row["is_member"]), created_at=_parse_ts(row["created_at"]),
    )


def _row_to_issue(row: sqlite3.Row) -> Issue:
    return Issue(
        id=row["id"], title=row["title"], status=IssueStatus(row["status"]),
        current_seq=row["current_seq"], adopted_seq=row["adopted_seq"],
        adopted_at=_parse_ts(row["adopted_at"]) if row["adopted_at"] else None,
        deadline_utc=_parse_ts(row["deadline_utc"]), tzname=row["tzname"],
        parent_issue_id=row["parent_issue_id"], quorum_ratio=row["quorum_ratio"],
        signature_ratio=row["signature_ratio"], created_at=_parse_ts(row["created_at"]),
    )


def _row_to_proposal(row: sqlite3.Row) -> Proposal:
    return Proposal(
        id=row["id"], issue_id=row["issue_id"], seq=row["seq"], title=row["title"],
        body=row["body"], language=row["language"], summary=row["summary"],
        supersedes_seq=row["supersedes_seq"], author_institution=row["author_institution"],
        created_at=_parse_ts(row["created_at"]),
    )


def _row_to_condition(row: sqlite3.Row) -> Condition:
    return Condition(
        id=row["id"], issue_id=row["issue_id"], proposal_seq=row["proposal_seq"],
        code=row["code"], raised_by=row["raised_by"], description=row["description"],
        status=ConditionStatus(row["status"]), resolved_by=row["resolved_by"],
        note=row["note"], created_at=_parse_ts(row["created_at"]),
        resolved_at=_parse_ts(row["resolved_at"]) if row["resolved_at"] else None,
    )


def _row_to_vote(row: sqlite3.Row) -> Vote:
    return Vote(
        id=row["id"], issue_id=row["issue_id"], proposal_seq=row["proposal_seq"],
        institution=row["institution"], delegate=row["delegate"],
        stance=Stance(row["stance"]), rationale=row["rationale"],
        active=bool(row["active"]), created_at=_parse_ts(row["created_at"]),
        updated_at=_parse_ts(row["updated_at"]),
    )


def _row_to_signature(row: sqlite3.Row) -> Signature:
    return Signature(
        id=row["id"], issue_id=row["issue_id"], proposal_seq=row["proposal_seq"],
        institution=row["institution"], signer=row["signer"],
        created_at=_parse_ts(row["created_at"]),
    )


def _row_to_conflict(row: sqlite3.Row) -> ConflictDeclaration:
    return ConflictDeclaration(
        issue_id=row["issue_id"], institution=row["institution"],
        declared=bool(row["declared"]), reason=row["reason"],
        updated_at=_parse_ts(row["updated_at"]),
    )


def _row_to_revision(row: sqlite3.Row) -> RevisionRequest:
    return RevisionRequest(
        id=row["id"], issue_id=row["issue_id"], institution=row["institution"],
        reason=row["reason"], created_at=_parse_ts(row["created_at"]),
        successor_issue_id=row["successor_issue_id"],
    )


def _row_to_audit(row: sqlite3.Row) -> AuditEvent:
    return AuditEvent(
        seq=row["seq"], event_id=row["event_id"], at=_parse_ts(row["at"]),
        event_type=row["event_type"], issue_id=row["issue_id"],
        institution=row["institution"], actor=row["actor"], payload=row["payload"],
    )
