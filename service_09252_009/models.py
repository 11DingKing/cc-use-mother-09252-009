"""领域模型：议题、版本化提案、结构化条件、立场与审计。"""
from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import datetime


class Stance(enum.Enum):
    """机构对某一版提案的表态。"""

    FOR = "for"
    AGAINST = "against"
    ABSTAIN = "abstain"


class ConditionStatus(enum.Enum):
    """条件的结构化状态。"""

    OUTSTANDING = "outstanding"
    FULFILLED = "fulfilled"
    WAIVED = "waived"
    SUPERSEDED = "superseded"


class IssueStatus(enum.Enum):
    """议题生命周期状态。

    OPEN: 协商中，决议尚未生效；
    ADOPTED: 决议已生效；
    UNDER_REVISION: 已生效决议进入修订流程，原决议仍然有效。
    """

    OPEN = "open"
    ADOPTED = "adopted"
    UNDER_REVISION = "under_revision"


@dataclass(frozen=True)
class Institution:
    code: str
    name: str
    delegate: str | None
    is_member: bool
    created_at: datetime


@dataclass(frozen=True)
class Proposal:
    id: int
    issue_id: str
    seq: int
    title: str
    body: str
    language: str
    summary: str
    supersedes_seq: int | None
    author_institution: str
    created_at: datetime


@dataclass(frozen=True)
class Condition:
    id: int
    issue_id: str
    proposal_seq: int
    code: str
    raised_by: str
    description: str
    status: ConditionStatus
    resolved_by: str | None
    note: str | None
    created_at: datetime
    resolved_at: datetime | None


@dataclass(frozen=True)
class Vote:
    id: int
    issue_id: str
    proposal_seq: int
    institution: str
    delegate: str
    stance: Stance
    rationale: str
    active: bool
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class Signature:
    id: int
    issue_id: str
    proposal_seq: int
    institution: str
    signer: str
    created_at: datetime


@dataclass(frozen=True)
class ConflictDeclaration:
    issue_id: str
    institution: str
    declared: bool
    reason: str | None
    updated_at: datetime


@dataclass(frozen=True)
class RevisionRequest:
    id: int
    issue_id: str
    institution: str
    reason: str
    created_at: datetime
    successor_issue_id: str | None


@dataclass(frozen=True)
class Issue:
    id: str
    title: str
    status: IssueStatus
    current_seq: int
    adopted_seq: int | None
    adopted_at: datetime | None
    deadline_utc: datetime
    tzname: str
    parent_issue_id: str | None
    quorum_ratio: float
    signature_ratio: float
    created_at: datetime


@dataclass(frozen=True)
class AuditEvent:
    seq: int
    event_id: str
    at: datetime
    event_type: str
    issue_id: str | None
    institution: str | None
    actor: str
    payload: str
