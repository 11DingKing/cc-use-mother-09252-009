"""联合教研议题决议的领域模型与纯函数式评估。

术语
----

- 提案（Proposal）：一个议题的具体版本。同一议题修订时生成新版本，
  旧版本成为历史（``superseded``）。
- 立场（Stance）：院校（机构）对某版本的表态，支持 / 反对 / 弃权，
  可附理由，并可撤回。只有院校自己可以修改自己的立场。
- 条件（Condition）：附在议题上的结构化待办项（可带责任机构与分类）。
  条件可满足、可解除（决议生效后发现不再需要时），均有时间戳与记录。
- 修订（Revision）：决议生效后再次提出文本，产生新版本并重新走表决流程。
- 签署（Signature）：院校对当前生效决议文本的正式签字确认。

生效规则（每经任何状态变化都重新评估，对“尚未生效”的议题）::

    eligible   = 未被标记利益冲突的院校（冲突院校不计入分母，也不能投票）
    quorum     = 法定人数：出席并给出“有效表态”（支持/反对，弃权不算）
                 的合格院校数达到 quorum_required
    majority   = 有效表态中“支持”严格多于“反对”
    all_met    = 所有必备条件均已满足（解除掉的条件视为不再阻塞）
    open_now   = 当前时刻未超过截止时刻（截止后不得再改变结果，
                 但已取得的生效地位保持；截止时刻本身仍可操作）
    in_effect  = 生效未被撤销（撤回表态等导致规则不再满足时撤销生效）

所有评估都是纯函数：状态对象在进出服务层时才与持久化和时钟交互。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
from typing import Optional


def utcnow() -> datetime:
    """统一的 UTC 当前时间入口（测试可替换服务层时钟）。"""
    return datetime.now(timezone.utc)


def parse_ts(value: str | datetime) -> datetime:
    """把 ISO-8601 文本解析为带时区的 datetime；无时区者按 UTC 处理。"""
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def iso(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class Vote(str, Enum):
    FAVOR = "favor"   # 支持
    OPPOSE = "oppose"  # 反对
    ABSTAIN = "abstain"  # 弃权

    @classmethod
    def parse(cls, raw: str) -> "Vote":
        try:
            return cls(raw)
        except ValueError:
            raise ValueError(f"未知投票类型: {raw!r}（可选 favor/oppose/abstain）") from None


# 议题生命周期
STATUS_OPEN = "open"          # 表决中
STATUS_EFFECTIVE = "effective"  # 决议已生效
STATUS_SUPERSEDED = "superseded"  # 被修订后的新版本取代（历史）
STATUS_CANCELLED = "cancelled"  # 撤回（作废）

# 立场状态
STANCE_ACTIVE = "active"
STANCE_WITHDRAWN = "withdrawn"

# 条件状态
COND_PENDING = "pending"
COND_MET = "met"
COND_WAIVED = "waived"

# 条件分类
COND_CLASS_REQUIRED = "required"  # 必备：未满足则阻塞生效
COND_CLASS_ADVISORY = "advisory"  # 参考：不阻塞生效


@dataclass(frozen=True)
class Institution:
    id: int
    code: str
    name: str
    created_at: datetime


@dataclass(frozen=True)
class Representative:
    """院校代表。同一院校的代表可更换，新代表自然继承院校立场。"""

    id: int
    institution_id: int
    name: str
    active: bool
    created_at: datetime
    deactivated_at: Optional[datetime] = None


@dataclass(frozen=True)
class Proposal:
    id: int                    # 议题编号（所有版本共享）
    version: int               # 版本号，从 1 开始
    title: str
    body: str
    language: str
    author_institution_id: Optional[int]
    quorum_required: int
    deadline: Optional[datetime]
    status: str
    based_on_version: Optional[int]
    created_at: datetime
    effective_at: Optional[datetime] = None
    superseded_at: Optional[datetime] = None
    cancelled_at: Optional[datetime] = None

    @property
    def is_current(self) -> bool:
        return self.status in (STATUS_OPEN, STATUS_EFFECTIVE)

    @property
    def is_live(self) -> bool:
        """还能参与表决/条件处理的版本。"""
        return self.status == STATUS_OPEN


@dataclass(frozen=True)
class Conflict:
    """利益冲突登记：被标记院校不计入法定人数，也不得表态。"""

    id: int
    proposal_id: int
    version: int
    institution_id: int
    reason: str
    created_at: datetime


@dataclass(frozen=True)
class Condition:
    id: int
    proposal_id: int
    version: int
    seq: int
    title: str
    detail: str
    kind: str                  # required / advisory
    owner_institution_id: Optional[int]
    status: str                # pending / met / waived
    created_at: datetime
    created_by_institution_id: Optional[int]
    satisfied_at: Optional[datetime] = None
    satisfied_by_institution_id: Optional[int] = None
    waiver: Optional[str] = None
    waived_at: Optional[datetime] = None
    waived_by_institution_id: Optional[int] = None

    @property
    def blocking(self) -> bool:
        """是否仍阻塞生效。"""
        return self.kind == COND_CLASS_REQUIRED and self.status == COND_PENDING

    def snapshot(self) -> "Condition":
        return replace(self)


@dataclass(frozen=True)
class Stance:
    id: int
    proposal_id: int
    version: int
    institution_id: int
    representative_id: int
    vote: Vote
    rationale: str
    status: str                # active / withdrawn
    created_at: datetime
    updated_at: datetime
    withdrawn_at: Optional[datetime] = None
    withdraw_reason: Optional[str] = None

    @property
    def active(self) -> bool:
        return self.status == STANCE_ACTIVE


@dataclass(frozen=True)
class Signature:
    id: int
    proposal_id: int
    version: int
    institution_id: int
    representative_id: int
    note: str
    created_at: datetime


@dataclass(frozen=True)
class Revision:
    id: int
    proposal_id: int
    from_version: int
    new_version: int
    reason: str
    requested_by_institution_id: Optional[int]
    created_at: datetime


@dataclass(frozen=True)
class Evaluation:
    """一次生效评估的完整结果，可直接对外解释“哪些条件尚未满足”。"""

    status: str
    at: datetime
    eligible_total: int
    conflicted: tuple[int, ...]
    active_stances: tuple[Stance, ...]
    favor: int
    oppose: int
    abstain: int
    counted: int               # 有效表态数（支持+反对）
    quorum_required: int
    quorum_reached: bool
    majority_reached: bool
    pending_required: tuple[Condition, ...]
    pending_advisory: tuple[Condition, ...]
    deadline_open: bool
    effective: bool
    reasons: tuple[str, ...]

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "evaluated_at": iso(self.at),
            "eligible_institutions": self.eligible_total,
            "conflicted_institutions": list(self.conflicted),
            "votes": {
                "favor": self.favor,
                "oppose": self.oppose,
                "abstain": self.abstain,
                "counted": self.counted,
                "quorum_required": self.quorum_required,
                "quorum_reached": self.quorum_reached,
                "majority_reached": self.majority_reached,
            },
            "pending_required_conditions": [
                {"id": c.id, "seq": c.seq, "title": c.title,
                 "owner_institution_id": c.owner_institution_id}
                for c in self.pending_required
            ],
            "pending_advisory_conditions": [
                {"id": c.id, "seq": c.seq, "title": c.title}
                for c in self.pending_advisory
            ],
            "deadline_open": self.deadline_open,
            "effective": self.effective,
            "reasons": list(self.reasons),
        }


@dataclass
class ProposalAggregate:
    """单个议题版本的完整状态，评估函数的输入。"""

    proposal: Proposal
    institutions: list[Institution] = field(default_factory=list)
    conflicts: list[Conflict] = field(default_factory=list)
    conditions: list[Condition] = field(default_factory=list)
    stances: list[Stance] = field(default_factory=list)
    signatures: list[Signature] = field(default_factory=list)

    def active_stance(self, institution_id: int) -> Optional[Stance]:
        for s in self.stances:
            if s.institution_id == institution_id and s.active:
                return s
        return None


def evaluate(agg: ProposalAggregate, at: datetime) -> Evaluation:
    """纯函数：按法定人数、利益冲突、条件与截止规则评估议题是否生效。"""
    p = agg.proposal
    at = at.astimezone(timezone.utc)

    conflicted = frozenset(c.institution_id for c in agg.conflicts)
    eligible = [i for i in agg.institutions if i.id not in conflicted]

    active = tuple(
        s for s in agg.stances
        if s.active and s.institution_id not in conflicted
    )
    favor = sum(1 for s in active if s.vote is Vote.FAVOR)
    oppose = sum(1 for s in active if s.vote is Vote.OPPOSE)
    abstain = sum(1 for s in active if s.vote is Vote.ABSTAIN)
    counted = favor + oppose

    # 法定人数是绝对下限：利益冲突使合格院校池缩水到要求数以下时，
    # 保守判定为无法达到，而不是自动降低门槛。
    quorum_required = p.quorum_required
    quorum_reached = counted >= quorum_required
    majority_reached = favor > oppose

    pending_required = tuple(c for c in agg.conditions if c.blocking)
    pending_advisory = tuple(
        c for c in agg.conditions
        if c.kind == COND_CLASS_ADVISORY and c.status == COND_PENDING
    )

    # 截止时刻本身仍可操作（at <= deadline）；未设截止期限则始终开放。
    deadline_open = True if p.deadline is None else at <= p.deadline

    qualifies = quorum_reached and majority_reached and not pending_required
    can_take_effect = qualifies and deadline_open

    # “是否生效”区分两种情形：
    # - 表决中：此刻规则全部满足且未截止，立即取得生效地位；
    # - 已生效：生效地位持续到被修订取代，越过截止时刻不使其失效。
    if p.status == STATUS_EFFECTIVE:
        effective = True
    elif p.status == STATUS_OPEN:
        effective = can_take_effect
    else:
        effective = False

    reasons: list[str] = []
    if not effective and p.status == STATUS_OPEN:
        if not quorum_reached:
            reasons.append(
                f"法定人数不足：有效表态 {counted} 所，要求至少 {quorum_required} 所"
                f"（弃权不计，利益冲突院校不计入分母）"
            )
        if not majority_reached:
            if counted == 0:
                reasons.append("尚无有效表态，未形成多数")
            else:
                reasons.append(f"未过半数：支持 {favor}，反对 {oppose}")
        if pending_required:
            names = "、".join(f"#{c.seq} {c.title}" for c in pending_required)
            reasons.append(f"尚有必备条件未满足：{names}")
        if not deadline_open:
            reasons.append("已过表决截止时刻，不能再取得生效")

    return Evaluation(
        status=p.status,
        at=at,
        eligible_total=len(eligible),
        conflicted=tuple(sorted(conflicted)),
        active_stances=active,
        favor=favor,
        oppose=oppose,
        abstain=abstain,
        counted=counted,
        quorum_required=quorum_required,
        quorum_reached=quorum_reached,
        majority_reached=majority_reached,
        pending_required=pending_required,
        pending_advisory=pending_advisory,
        deadline_open=deadline_open,
        effective=effective,
        reasons=tuple(reasons),
    )
