"""纯领域规则：截止时间、法定人数、利益冲突与生效评估。

本模块不触碰数据库，输入输出均为不可变模型，便于单测与推理。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from .errors import DeadlinePassedError, ValidationError
from .models import (
    Condition,
    ConditionStatus,
    ConflictDeclaration,
    Institution,
    Issue,
    Stance,
    Vote,
)


def parse_deadline(local_text: str, tzname: str) -> tuple[datetime, str]:
    """把 "YYYY-MM-DD HH:MM" + IANA 时区解析为 UTC datetime。

    返回 (UTC 时间, 规范化后的时区名)。
    """
    try:
        tz = ZoneInfo(tzname)
    except Exception as exc:  # ZoneInfoNotFoundError 等
        raise ValidationError(f"未知时区: {tzname}") from exc
    text = local_text.strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            naive = datetime.strptime(text, fmt)
            break
        except ValueError:
            naive = None  # type: ignore[assignment]
    if naive is None:
        raise ValidationError(f"无法解析截止时间: {local_text!r}")
    aware = naive.replace(tzinfo=tz)
    return aware.astimezone(ZoneInfo("UTC")), tzname


def deadline_local_text(issue: Issue) -> str:
    """以议题登记的时区展示截止时间。"""
    tz = ZoneInfo(issue.tzname)
    return issue.deadline_utc.astimezone(tz).strftime("%Y-%m-%d %H:%M:%S %Z")


def assert_before_deadline(issue: Issue, now: datetime) -> None:
    if now.astimezone(issue.deadline_utc.tzinfo) > issue.deadline_utc:
        raise DeadlinePassedError(
            f"议题 {issue.id} 已于 {deadline_local_text(issue)}（{issue.tzname}）截止"
        )


def conflict_map(declarations: list[ConflictDeclaration]) -> dict[str, ConflictDeclaration]:
    return {d.institution: d for d in declarations if d.declared}


def is_conflicted(institution: str, declarations: list[ConflictDeclaration]) -> bool:
    decl = conflict_map(declarations).get(institution)
    return decl is not None and decl.declared


@dataclass(frozen=True)
class Eligibility:
    """某一时刻参与计数的机构集合。"""

    members: list[Institution]
    conflicted: frozenset[str]

    @property
    def eligible(self) -> list[Institution]:
        return [m for m in self.members if m.code not in self.conflicted]

    @property
    def eligible_codes(self) -> frozenset[str]:
        return frozenset(m.code for m in self.eligible)


def build_eligibility(
    members: list[Institution], declarations: list[ConflictDeclaration]
) -> Eligibility:
    return Eligibility(members=members, conflicted=frozenset(conflict_map(declarations)))


@dataclass(frozen=True)
class Evaluation:
    """决议生效评估结果。"""

    eligible_count: int
    voted_count: int
    for_count: int
    against_count: int
    abstain_count: int
    conflicted: tuple[str, ...]
    outstanding_conditions: tuple[str, ...]
    quorum_reached: bool
    all_for: bool
    conditions_clear: bool
    adopted: bool
    signature_count: int
    signature_required: int
    signatures_complete: bool
    reasons: tuple[str, ...]

    def as_dict(self) -> dict:
        return {
            "eligible_count": self.eligible_count,
            "voted_count": self.voted_count,
            "for": self.for_count,
            "against": self.against_count,
            "abstain": self.abstain_count,
            "conflicted_institutions": list(self.conflicted),
            "outstanding_conditions": list(self.outstanding_conditions),
            "quorum_reached": self.quorum_reached,
            "all_for": self.all_for,
            "conditions_clear": self.conditions_clear,
            "adopted": self.adopted,
            "signature_count": self.signature_count,
            "signature_required": self.signature_required,
            "signatures_complete": self.signatures_complete,
            "reasons": list(self.reasons),
        }


def evaluate(
    issue: Issue,
    members: list[Institution],
    declarations: list[ConflictDeclaration],
    active_votes: list[Vote],
    conditions: list[Condition],
    signature_count: int,
) -> Evaluation:
    """根据法定人数、利益冲突与条件状态评估决议能否生效。

    规则：
    - 利益冲突机构从法定基数中剔除，其已投的票不计入；
    - 法定人数 = 有效票数 / 适格成员数 >= quorum_ratio；
    - 适格成员中已投票者必须全部赞成（反对/弃权都阻止生效，理由会被记录）；
    - 当前提案版本上不得有未满足（outstanding）的条件；
    - 生效后还需达到 signature_ratio 的机构签署（默认赞成票机构即签署主体）。
    """
    eligibility = build_eligibility(members, declarations)
    eligible_codes = eligibility.eligible_codes
    counted = [v for v in active_votes if v.institution in eligible_codes]

    for_count = sum(1 for v in counted if v.stance is Stance.FOR)
    against_count = sum(1 for v in counted if v.stance is Stance.AGAINST)
    abstain_count = sum(1 for v in counted if v.stance is Stance.ABSTAIN)
    voted_count = len(counted)
    eligible_count = len(eligibility.eligible)

    quorum_reached = (
        eligible_count > 0
        and voted_count / eligible_count + 1e-12 >= issue.quorum_ratio
    )
    all_for = voted_count > 0 and for_count == voted_count
    current_outstanding = tuple(
        c.code for c in conditions
        if c.proposal_seq == issue.current_seq
        and c.status is ConditionStatus.OUTSTANDING
    )
    conditions_clear = len(current_outstanding) == 0
    # 签署门槛按赞成机构数计算：默认全体赞成者签署后方可生效
    sig_required = max(1, math.ceil(for_count * issue.signature_ratio))
    signatures_complete = signature_count >= sig_required and for_count > 0

    reasons: list[str] = []
    if eligible_count == 0:
        reasons.append("没有适格的成员机构（全部缺失或存在利益冲突）")
    if not quorum_reached:
        reasons.append(
            f"未达法定人数：{voted_count}/{eligible_count} < {issue.quorum_ratio:.0%}"
        )
    if against_count:
        reasons.append(f"存在 {against_count} 张反对票")
    if abstain_count:
        reasons.append(f"存在 {abstain_count} 张弃权票")
    if quorum_reached and voted_count and not all_for:
        reasons.append("适格成员的表态未全部为赞成")
    if not conditions_clear:
        reasons.append(f"尚有未满足条件: {', '.join(current_outstanding)}")

    adopted = quorum_reached and all_for and conditions_clear
    if adopted and not signatures_complete:
        reasons.append(
            f"决议可生效但签署未完成: {signature_count}/{sig_required}"
        )

    return Evaluation(
        eligible_count=eligible_count,
        voted_count=voted_count,
        for_count=for_count,
        against_count=against_count,
        abstain_count=abstain_count,
        conflicted=tuple(sorted(eligibility.conflicted)),
        outstanding_conditions=current_outstanding,
        quorum_reached=quorum_reached,
        all_for=all_for,
        conditions_clear=conditions_clear,
        adopted=adopted,
        signature_count=signature_count,
        signature_required=sig_required,
        signatures_complete=signatures_complete,
        reasons=tuple(reasons),
    )
