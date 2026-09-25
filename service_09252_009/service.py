"""应用服务：协商过程的用例编排与规则执行。

所有写用例遵循同一结构::

    1. BEGIN IMMEDIATE 事务（并发写串行）
    2. 幂等键命中则直接回放首次结果
    3. 载入聚合 → 鉴权/业务校验 → 变更
    4. 重新评估“尚未生效”的议题，必要时生效或撤销生效
    5. 写审计日志、登记幂等结果、提交

关键规则：

- 机构只能修改自己的立场（表态/撤回/签署）；
  代表更换后旧代表的令牌立即失效，立场随院校保留，由新代表自然继承。
- 表态与条件变化都会触发重算；撤回表态时若议题尚未生效则重新计算，
  已生效决议不会被静默推翻，任何改动必须走修订流程。
- 截止时间按提案携带的 IANA 时区解释并统一折算 UTC；截止时刻本身仍可操作。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any, Optional

from .errors import (
    AuthorizationError,
    ConflictError,
    DeadlinePassedError,
    NotFoundError,
    StateError,
    ValidationError,
)
from .models import (
    COND_CLASS_ADVISORY,
    COND_CLASS_REQUIRED,
    COND_MET,
    COND_WAIVED,
    STANCE_WITHDRAWN,
    STATUS_CANCELLED,
    STATUS_EFFECTIVE,
    STATUS_OPEN,
    STATUS_SUPERSEDED,
    Condition,
    Proposal,
    ProposalAggregate,
    Vote,
    evaluate,
    iso,
)
from .storage import SQLiteStore


class Clock:
    """可替换时钟端口。"""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class ResolutionService:
    """决议应用服务。"""

    def __init__(self, store: SQLiteStore, clock: Optional[Clock] = None):
        self.store = store
        self.clock = clock or Clock()

    # ---- 内部工具 ------------------------------------------------------

    def _now(self) -> datetime:
        return self.clock.now().astimezone(timezone.utc)

    def _load_aggregate(self, conn, proposal_id: int,
                        version: Optional[int] = None) -> ProposalAggregate:
        try:
            proposal = self.store.get_proposal(conn, proposal_id, version)
        except LookupError:
            raise NotFoundError(
                f"议题 {proposal_id}" + (f" v{version}" if version else "")
                + " 不存在")
        institutions = self.store.list_institutions(conn)
        conflicts = self.store.list_conflicts(conn, proposal_id, proposal.version)
        conditions = self.store.list_conditions(conn, proposal_id, proposal.version)
        stances = self.store.list_stances(conn, proposal_id, proposal.version)
        signatures = self.store.list_signatures(conn, proposal_id, proposal.version)
        return ProposalAggregate(
            proposal=proposal,
            institutions=institutions,
            conflicts=conflicts,
            conditions=conditions,
            stances=stances,
            signatures=signatures,
        )

    def _authenticate(self, conn, institution_id: int,
                      representative_id: int) -> tuple:
        """校验机构与代表：代表必须属于该机构且在任。"""
        try:
            institution = self.store.get_institution(conn, institution_id)
        except LookupError:
            raise NotFoundError(f"机构 {institution_id} 不存在")
        try:
            rep = self.store.get_representative(conn, representative_id)
        except LookupError:
            raise AuthorizationError("代表身份无效或已更换")
        if rep.institution_id != institution_id:
            raise AuthorizationError("代表与所属院校不符")
        if not rep.active:
            raise AuthorizationError("该代表已被更换，立场由院校保留，请由在任代表操作")
        return institution, rep

    def _recompute(self, conn, agg: ProposalAggregate, at: datetime) -> dict:
        """对尚未生效（含表决中）的当前版本执行生效/撤销生效。

        已生效但评估不再通过的情形只可能由撤回引发；撤回用例对已生效议题
        直接拒绝，因此这里不静默撤销已生效决议。
        """
        p = agg.proposal
        result = evaluate(agg, at)

        if p.status == STATUS_OPEN and result.effective:
            self.store.update_proposal_status(
                conn, p.id, p.version, status=STATUS_EFFECTIVE, at=at)
            agg.proposal = self.store.get_proposal(conn, p.id, p.version)
        elif p.status == STATUS_EFFECTIVE and not result.effective:
            # 理论不可达：已生效议题的表态/条件在各用例中被拦截。
            # 保留状态回退以保证不变量自洽。
            self.store.update_proposal_status(
                conn, p.id, p.version, status=STATUS_OPEN, at=at)
            agg.proposal = self.store.get_proposal(conn, p.id, p.version)
        return result.to_dict()

    def _require_deadline_open(self, proposal: Proposal, at: datetime,
                               action: str = "操作") -> None:
        if proposal.deadline is not None and at > proposal.deadline:
            raise DeadlinePassedError(
                f"{action}的截止时刻为 "
                f"{iso(proposal.deadline)}（UTC），当前 {iso(at)}，已截止")

    def _require_current(self, agg: ProposalAggregate) -> Proposal:
        p = agg.proposal
        if p.status == STATUS_SUPERSEDED:
            raise StateError(f"议题 {p.id} 的 v{p.version} 已被新版本取代，"
                             f"请对最新版本操作或发起修订")
        if p.status == STATUS_CANCELLED:
            raise StateError(f"议题 {p.id} v{p.version} 已作废")
        return p

    def _run(self, scope: str, key: Optional[str], audit_action: str,
             target_type: str, target_id: "str | Callable[[dict], str]",
             fn: Callable[[Any, datetime], Any],
             actor_institution_id: Optional[int] = None,
             actor_representative_id: Optional[int] = None,
             audit_payload: Optional[dict] = None) -> dict:
        """统一的事务/幂等/审计模板。

        幂等键按用例作用域命名空间化：同一客户端键串用于不同用例时不会
        互相回放。
        """
        idem_key = f"{scope}:{key}" if key else None
        with self.store.transaction() as conn:
            if idem_key:
                cached = self.store.get_idempotent(conn, idem_key)
                if cached is not None:
                    resolved = target_id(cached) if callable(target_id) else target_id
                    self.store.write_audit(
                        conn, at=self._now(), action=f"{audit_action}.replay",
                        target_type=target_type, target_id=resolved,
                        payload={"idempotency_key": key}, result="replayed",
                        actor_institution_id=actor_institution_id,
                        actor_representative_id=actor_representative_id)
                    return cached

            at = self._now()
            response = fn(conn, at)

            resolved = target_id(response) if callable(target_id) else target_id
            self.store.write_audit(
                conn, at=at, action=audit_action, target_type=target_type,
                target_id=resolved, payload=audit_payload or {}, result="ok",
                actor_institution_id=actor_institution_id,
                actor_representative_id=actor_representative_id)
            if idem_key:
                self.store.put_idempotent(conn, idem_key, scope, at, response)
            return response

    # ---- 机构与代表（管理用例）----------------------------------------

    def register_institution(self, code: str, name: str, *,
                             idempotency_key: Optional[str] = None) -> dict:
        code = _require_text(code, "code", max_len=64)
        name = _require_text(name, "name", max_len=200)

        def work(conn, at):
            existing = self.store.find_institution_by_code(conn, code)
            if existing is not None:
                raise ConflictError(f"院校代码 {code} 已存在", code="institution_exists")
            inst = self.store.create_institution(conn, code, name, at)
            return {"institution": self._institution_dict(inst)}

        return self._run(
            "register_institution", idempotency_key, "institution.register",
            "institution", code, work, audit_payload={"code": code, "name": name})

    def appoint_representative(self, institution_id: int, name: str, *,
                               replace: bool = False,
                               idempotency_key: Optional[str] = None) -> dict:
        """任命代表；replace=True 时先停任旧代表（代表更换）。"""
        name = _require_text(name, "representative_name", max_len=100)

        def work(conn, at):
            try:
                self.store.get_institution(conn, institution_id)
            except LookupError:
                raise NotFoundError(f"机构 {institution_id} 不存在")
            if replace:
                rep = self.store.replace_representative(conn, institution_id, name, at)
            else:
                rep = self.store.add_representative(conn, institution_id, name, at)
            return {"representative": {
                "id": rep.id, "institution_id": rep.institution_id,
                "name": rep.name, "active": rep.active,
                "created_at": iso(rep.created_at)}}

        return self._run(
            "appoint_representative", idempotency_key,
            "representative.replace" if replace else "representative.appoint",
            "institution", str(institution_id), work,
            actor_institution_id=institution_id,
            audit_payload={"name": name, "replace": replace})

    # ---- 提案与修订 ----------------------------------------------------

    def submit_proposal(self, title: str, body: str, *, language: str = "zh",
                        author_institution_id: Optional[int] = None,
                        quorum_required: int = 1,
                        deadline: Optional[str] = None,
                        deadline_timezone: str = "UTC",
                        idempotency_key: Optional[str] = None) -> dict:
        """提交议题（v1）。截止时刻按时区参数解释。"""
        title = _require_text(title, "title", max_len=300)
        body = _require_text(body, "body", max_len=20000)
        if not isinstance(quorum_required, int) or quorum_required < 1:
            raise ValidationError("quorum_required 必须为不小于 1 的整数")
        deadline_dt = _parse_deadline(deadline, deadline_timezone)

        def work(conn, at):
            if author_institution_id is not None:
                try:
                    self.store.get_institution(conn, author_institution_id)
                except LookupError:
                    raise NotFoundError(
                        f"提案院校 {author_institution_id} 不存在")
            proposal_id = self.store.allocate_proposal_id(conn)
            proposal = self.store.insert_proposal(
                conn, proposal_id=proposal_id, version=1, title=title, body=body,
                language=language or "zh",
                author_institution_id=author_institution_id,
                quorum_required=quorum_required, deadline=deadline_dt,
                status=STATUS_OPEN, based_on_version=None, at=at)
            return {
                "proposal_id": proposal.id,
                "version": proposal.version,
                "status": proposal.status,
                "created_at": iso(proposal.created_at),
            }

        return self._run(
            "submit_proposal", idempotency_key, "proposal.submit",
            "proposal", lambda r: f"{r['proposal_id']}:v{r['version']}", work,
            actor_institution_id=author_institution_id,
            audit_payload={"title": title, "quorum_required": quorum_required,
                           "deadline": iso(deadline_dt),
                           "deadline_timezone": deadline_timezone})

    def revise_proposal(self, proposal_id: int, title: str, body: str, *,
                        language: str = "zh",
                        requesting_institution_id: int,
                        representative_id: int,
                        reason: str = "",
                        deadline: Optional[str] = None,
                        deadline_timezone: str = "UTC",
                        idempotency_key: Optional[str] = None) -> dict:
        """对已生效决议启动修订：旧版本置为 superseded，创建新版本重新表决。

        已生效决议的任何实质变更都必须走此流程；尚在表决中的议题直接改投即可，
        无需修订。新一轮表决可单独设置截止时间。
        """
        title = _require_text(title, "title", max_len=300)
        body = _require_text(body, "body", max_len=20000)
        reason = _require_text(reason, "reason", max_len=2000, allow_empty=True)
        deadline_dt = _parse_deadline(deadline, deadline_timezone)

        def work(conn, at):
            agg = self._load_aggregate(conn, proposal_id)
            current = agg.proposal
            if current.status != STATUS_EFFECTIVE:
                raise StateError(
                    f"只有已生效决议才能启动修订；议题 {proposal_id} 当前状态为 "
                    f"{current.status}（表决中可直接改投或撤回）")
            self._authenticate(conn, requesting_institution_id,
                               representative_id)
            new_version = self.store.next_version(conn, proposal_id)
            self.store.update_proposal_status(
                conn, proposal_id, current.version,
                status=STATUS_SUPERSEDED, at=at)
            new = self.store.insert_proposal(
                conn, proposal_id=proposal_id, version=new_version, title=title,
                body=body, language=language or "zh",
                author_institution_id=requesting_institution_id,
                quorum_required=current.quorum_required,
                deadline=deadline_dt, status=STATUS_OPEN,
                based_on_version=current.version, at=at)
            self.store.add_revision(
                conn, proposal_id=proposal_id, from_version=current.version,
                new_version=new_version, reason=reason,
                requested_by_institution_id=requesting_institution_id, at=at)
            return {
                "proposal_id": new.id,
                "version": new.version,
                "status": new.status,
                "based_on_version": current.version,
                "superseded_version": current.version,
                "created_at": iso(new.created_at),
            }

        return self._run(
            "revise_proposal", idempotency_key, "proposal.revise",
            "proposal", str(proposal_id), work,
            actor_institution_id=requesting_institution_id,
            audit_payload={"title": title, "reason": reason})

    def cancel_proposal(self, proposal_id: int, *,
                        idempotency_key: Optional[str] = None) -> dict:
        """作废一个仍在表决中的议题（会议管理动作）。"""

        def work(conn, at):
            agg = self._load_aggregate(conn, proposal_id)
            p = agg.proposal
            if p.status != STATUS_OPEN:
                raise StateError(
                    f"只有表决中的议题可以作废；当前状态 {p.status}")
            self.store.update_proposal_status(
                conn, proposal_id, p.version, status=STATUS_CANCELLED, at=at)
            return {"proposal_id": proposal_id, "version": p.version,
                    "status": STATUS_CANCELLED}

        return self._run(
            "cancel_proposal", idempotency_key, "proposal.cancel",
            "proposal", str(proposal_id), work)

    # ---- 利益冲突 ------------------------------------------------------

    def declare_conflict(self, proposal_id: int, institution_id: int,
                         reason: str = "", *,
                         idempotency_key: Optional[str] = None) -> dict:
        """登记院校对该议题版本的利益冲突。

        冲突院校：不计入法定人数分母、不得表态；其已有立场在评估中被排除。
        对已生效版本登记冲突属于实质变更，必须先修订。
        """
        reason = _require_text(reason, "reason", max_len=500, allow_empty=True)

        def work(conn, at):
            agg = self._load_aggregate(conn, proposal_id)
            p = self._require_current(agg)
            if p.status == STATUS_EFFECTIVE:
                raise StateError(
                    "决议已生效，利益冲突登记属于实质变更，请先发起修订")
            try:
                self.store.get_institution(conn, institution_id)
            except LookupError:
                raise NotFoundError(f"机构 {institution_id} 不存在")
            if any(c.institution_id == institution_id for c in agg.conflicts):
                raise ConflictError(
                    f"机构 {institution_id} 对议题 {proposal_id} v{p.version} "
                    "的利益冲突已登记", code="conflict_exists")
            conflict = self.store.add_conflict(
                conn, proposal_id, p.version, institution_id, reason, at)
            evaluation = self._recompute(conn, self._load_aggregate(
                conn, proposal_id, p.version), at)
            return {"conflict_id": conflict.id, "proposal_id": proposal_id,
                    "version": p.version, "evaluation": evaluation}

        return self._run(
            "declare_conflict", idempotency_key, "conflict.declare",
            "proposal", lambda r: _audit_target(r, proposal_id), work,
            audit_payload={"institution_id": institution_id, "reason": reason})

    # ---- 条件 ----------------------------------------------------------

    def add_condition(self, proposal_id: int, title: str, *,
                      detail: str = "", kind: str = COND_CLASS_REQUIRED,
                      owner_institution_id: Optional[int] = None,
                      acting_institution_id: int,
                      representative_id: int,
                      idempotency_key: Optional[str] = None) -> dict:
        """为当前版本附加结构化条件（必备/参考）。"""
        title = _require_text(title, "title", max_len=300)
        detail = _require_text(detail, "detail", max_len=2000, allow_empty=True)
        if kind not in (COND_CLASS_REQUIRED, COND_CLASS_ADVISORY):
            raise ValidationError(
                f"条件类型必须是 {COND_CLASS_REQUIRED} 或 {COND_CLASS_ADVISORY}")

        def work(conn, at):
            _, rep = self._authenticate(
                conn, acting_institution_id, representative_id)
            agg = self._load_aggregate(conn, proposal_id)
            p = self._require_current(agg)
            self._require_deadline_open(p, at, "附加条件")
            if p.status == STATUS_EFFECTIVE:
                raise StateError(
                    "决议已生效，新增条件属于实质变更，请先发起修订")
            if owner_institution_id is not None:
                try:
                    self.store.get_institution(conn, owner_institution_id)
                except LookupError:
                    raise NotFoundError(
                        f"责任机构 {owner_institution_id} 不存在")
            seq = max((c.seq for c in agg.conditions), default=0) + 1
            cond = self.store.add_condition(
                conn, proposal_id=proposal_id, version=p.version, seq=seq,
                title=title, detail=detail, kind=kind,
                owner_institution_id=owner_institution_id,
                created_by_institution_id=acting_institution_id, at=at)
            evaluation = self._recompute(
                conn, self._load_aggregate(conn, proposal_id, p.version), at)
            return {"condition": self._condition_dict(cond, rep_id=rep.id),
                    "evaluation": evaluation}

        return self._run(
            "add_condition", idempotency_key, "condition.add",
            "proposal", lambda r: _audit_target(r, proposal_id), work,
            actor_institution_id=acting_institution_id,
            actor_representative_id=representative_id,
            audit_payload={"title": title, "kind": kind})

    def satisfy_condition(self, condition_id: int, *,
                          acting_institution_id: int,
                          representative_id: int,
                          idempotency_key: Optional[str] = None) -> dict:
        """报告条件已满足，并重算议题。任何在任代表均可替会议确认条件完成。"""

        def work(conn, at):
            self._authenticate(conn, acting_institution_id, representative_id)
            try:
                cond = self.store.get_condition(conn, condition_id)
            except LookupError:
                raise NotFoundError(f"条件 {condition_id} 不存在")
            agg = self._load_aggregate(conn, cond.proposal_id, cond.version)
            p = self._require_current(agg)
            # 截止时间只约束表决中议题的条件变化；已生效后的条件确认/解除
            # 属于会后行政动作，不受其限制。
            if p.status == STATUS_OPEN:
                self._require_deadline_open(p, at, "确认条件")
            if cond.status == COND_MET:
                raise ConflictError(
                    f"条件 #{cond.seq} 已处于满足状态", code="condition_already_met")
            self.store.set_condition_status(
                conn, condition_id, status=COND_MET, at=at,
                actor_institution_id=acting_institution_id)
            cond = self.store.get_condition(conn, condition_id)
            agg2 = self._load_aggregate(conn, cond.proposal_id, cond.version)
            evaluation = self._recompute(conn, agg2, at)
            return {"condition": self._condition_dict(cond),
                    "evaluation": evaluation}

        return self._run(
            "satisfy_condition", idempotency_key, "condition.satisfy",
            "condition", str(condition_id), work,
            actor_institution_id=acting_institution_id,
            actor_representative_id=representative_id)

    def waive_condition(self, condition_id: int, *, waiver: str = "",
                        acting_institution_id: int,
                        representative_id: int,
                        idempotency_key: Optional[str] = None) -> dict:
        """解除条件（生效后发现不再需要），记录解除理由；解除后重算。"""
        waiver = _require_text(waiver, "waiver", max_len=1000, allow_empty=True)

        def work(conn, at):
            self._authenticate(conn, acting_institution_id, representative_id)
            try:
                cond = self.store.get_condition(conn, condition_id)
            except LookupError:
                raise NotFoundError(f"条件 {condition_id} 不存在")
            agg = self._load_aggregate(conn, cond.proposal_id, cond.version)
            self._require_current(agg)
            if cond.status == COND_WAIVED:
                raise ConflictError(
                    f"条件 #{cond.seq} 已解除", code="condition_already_waived")
            self.store.set_condition_status(
                conn, condition_id, status=COND_WAIVED, at=at,
                actor_institution_id=acting_institution_id, waiver=waiver)
            cond = self.store.get_condition(conn, condition_id)
            agg2 = self._load_aggregate(conn, cond.proposal_id, cond.version)
            evaluation = self._recompute(conn, agg2, at)
            return {"condition": self._condition_dict(cond),
                    "evaluation": evaluation}

        return self._run(
            "waive_condition", idempotency_key, "condition.waive",
            "condition", str(condition_id), work,
            actor_institution_id=acting_institution_id,
            actor_representative_id=representative_id,
            audit_payload={"waiver": waiver})

    # ---- 表态与撤回 ----------------------------------------------------

    def cast_stance(self, proposal_id: int, *, institution_id: int,
                    representative_id: int, vote: str,
                    rationale: str = "",
                    idempotency_key: Optional[str] = None) -> dict:
        """院校表态（支持/反对/弃权），可反复修改，但只能修改本校立场。

        已生效决议不接受新表态——实质变更请走修订。
        """
        try:
            vote_enum = Vote.parse(vote)
        except ValueError as exc:
            raise ValidationError(str(exc))
        rationale = _require_text(rationale, "rationale",
                                  max_len=2000, allow_empty=True)

        def work(conn, at):
            _, rep = self._authenticate(conn, institution_id, representative_id)
            agg = self._load_aggregate(conn, proposal_id)
            p = self._require_current(agg)
            self._require_deadline_open(p, at, "表态")
            if p.status == STATUS_EFFECTIVE:
                raise StateError(
                    "决议已生效，不能再改变立场；如需变更请发起修订")
            if any(c.institution_id == institution_id for c in agg.conflicts):
                raise AuthorizationError(
                    f"院校 {institution_id} 对该议题存在利益冲突，"
                    "不得表态且不计入法定人数")
            existing = agg.active_stance(institution_id)
            stance = self.store.upsert_stance(
                conn, proposal_id=proposal_id, version=p.version,
                institution_id=institution_id, representative_id=rep.id,
                vote=vote_enum, rationale=rationale, at=at)
            changed = existing is None or existing.vote != vote_enum or \
                existing.rationale != rationale
            agg2 = self._load_aggregate(conn, proposal_id, p.version)
            evaluation = self._recompute(conn, agg2, at)
            return {
                "stance": self._stance_dict(stance),
                "changed": changed,
                "evaluation": evaluation,
            }

        return self._run(
            "cast_stance", idempotency_key, "stance.cast",
            "proposal", lambda r: _audit_target(r, proposal_id), work,
            actor_institution_id=institution_id,
            actor_representative_id=representative_id,
            audit_payload={"vote": vote_enum.value})

    def withdraw_stance(self, proposal_id: int, *, institution_id: int,
                        representative_id: int, reason: str = "",
                        idempotency_key: Optional[str] = None) -> dict:
        """撤回院校表态。

        - 议题尚未生效：撤回后立即重新计算（可能失去法定人数/多数而无法生效，
          或本来就生效不了——表决中的议题始终可重算）；
        - 决议已生效：拒绝静默推翻，必须启动修订流程。
        """
        reason = _require_text(reason, "reason", max_len=1000, allow_empty=True)

        def work(conn, at):
            self._authenticate(conn, institution_id, representative_id)
            agg = self._load_aggregate(conn, proposal_id)
            p = self._require_current(agg)
            self._require_deadline_open(p, at, "撤回表态")
            if p.status == STATUS_EFFECTIVE:
                raise StateError(
                    "决议已生效，撤回表态不能推翻决议；"
                    "请使用修订流程（revise）产生新版本重新表决")
            existing = agg.active_stance(institution_id)
            if existing is None:
                raise ConflictError(
                    f"院校 {institution_id} 在议题 {proposal_id} v{p.version} "
                    "没有在效表态，无需撤回", code="no_active_stance")
            self.store.withdraw_stance(
                conn, proposal_id, p.version, institution_id, reason, at)
            agg2 = self._load_aggregate(conn, proposal_id, p.version)
            evaluation = self._recompute(conn, agg2, at)
            return {
                "proposal_id": proposal_id,
                "version": p.version,
                "institution_id": institution_id,
                "status": STANCE_WITHDRAWN,
                "evaluation": evaluation,
            }

        return self._run(
            "withdraw_stance", idempotency_key, "stance.withdraw",
            "proposal", lambda r: _audit_target(r, proposal_id), work,
            actor_institution_id=institution_id,
            actor_representative_id=representative_id,
            audit_payload={"reason": reason})

    # ---- 签署 ----------------------------------------------------------

    def sign(self, proposal_id: int, *, institution_id: int,
             representative_id: int, note: str = "",
             idempotency_key: Optional[str] = None) -> dict:
        """院校签署当前生效决议。重复签署幂等（覆盖备注与签署代表）。"""
        note = _require_text(note, "note", max_len=1000, allow_empty=True)

        def work(conn, at):
            _, rep = self._authenticate(conn, institution_id, representative_id)
            agg = self._load_aggregate(conn, proposal_id)
            p = agg.proposal
            if p.status != STATUS_EFFECTIVE:
                raise StateError(
                    f"只能签署已生效决议；议题 {proposal_id} 当前为 {p.status}")
            if any(c.institution_id == institution_id for c in agg.conflicts):
                raise AuthorizationError("存在利益冲突的院校不能签署该决议")
            sig = self.store.add_signature(
                conn, proposal_id=proposal_id, version=p.version,
                institution_id=institution_id, representative_id=rep.id,
                note=note, at=at)
            return {"signature": {
                "id": sig.id, "proposal_id": proposal_id,
                "version": p.version, "institution_id": institution_id,
                "representative_id": rep.id, "representative_name": rep.name,
                "note": note, "signed_at": iso(sig.created_at)}}

        return self._run(
            "sign", idempotency_key, "resolution.sign",
            "proposal", lambda r: _audit_target(r, proposal_id), work,
            actor_institution_id=institution_id,
            actor_representative_id=representative_id)

    # ---- 查询 ----------------------------------------------------------

    def get_proposal(self, proposal_id: int,
                     version: Optional[int] = None) -> dict:
        """查询议题状态与“尚未满足的条件/理由”（供会议纪要核对）。"""
        with self.store.transaction() as conn:
            agg = self._load_aggregate(conn, proposal_id, version)
            at = self._now()
            evaluation = evaluate(agg, at).to_dict()
            p = agg.proposal
            revisions = self.store.list_revisions(conn, proposal_id)
            return {
                "proposal": self._proposal_dict(p),
                "evaluation": evaluation,
                "conflicts": [
                    {"institution_id": c.institution_id, "reason": c.reason,
                     "declared_at": iso(c.created_at)}
                    for c in agg.conflicts],
                "conditions": [self._condition_dict(c) for c in agg.conditions],
                "stances": [self._stance_dict(s) for s in agg.stances],
                "signatures": [{
                    "institution_id": s.institution_id,
                    "representative_id": s.representative_id,
                    "note": s.note, "signed_at": iso(s.created_at)}
                    for s in agg.signatures],
                "revisions": [{
                    "from_version": r.from_version, "new_version": r.new_version,
                    "reason": r.reason,
                    "requested_by_institution_id": r.requested_by_institution_id,
                    "created_at": iso(r.created_at)} for r in revisions],
            }

    def list_proposals(self, status: Optional[str] = None) -> dict:
        allowed = {STATUS_OPEN, STATUS_EFFECTIVE, STATUS_SUPERSEDED,
                   STATUS_CANCELLED}
        if status is not None and status not in allowed:
            raise ValidationError(
                f"未知状态过滤: {status!r}，可选 {sorted(allowed)}")
        with self.store.transaction() as conn:
            proposals = self.store.list_proposals(conn, status=status)
            return {"proposals": [self._proposal_dict(p) for p in proposals]}

    def get_stance_rationale(self, proposal_id: int, institution_id: int,
                             version: Optional[int] = None) -> dict:
        """查询某院校的表态与理由（含已撤回历史）。"""
        with self.store.transaction() as conn:
            agg = self._load_aggregate(conn, proposal_id, version)
            p = agg.proposal
            stances = [s for s in agg.stances
                       if s.institution_id == institution_id]
            if not stances:
                raise NotFoundError(
                    f"院校 {institution_id} 在议题 {proposal_id} v{p.version} "
                    "没有表态记录")
            return {"proposal_id": proposal_id, "version": p.version,
                    "institution_id": institution_id,
                    "stances": [self._stance_dict(s) for s in stances]}

    def get_audit(self, *, proposal_id: Optional[int] = None,
                  limit: int = 100) -> dict:
        limit = max(1, min(int(limit), 1000))
        with self.store.transaction() as conn:
            rows = self.store.list_audit(conn, proposal_id=proposal_id, limit=limit)
            for r in rows:
                try:
                    r["payload"] = json_loads(r.get("payload"))
                except Exception:
                    pass
            return {"audit": rows}

    # ---- 序列化 --------------------------------------------------------

    @staticmethod
    def _institution_dict(inst) -> dict:
        return {"id": inst.id, "code": inst.code, "name": inst.name,
                "created_at": iso(inst.created_at)}

    @staticmethod
    def _proposal_dict(p: Proposal) -> dict:
        return {
            "id": p.id, "version": p.version, "title": p.title, "body": p.body,
            "language": p.language,
            "author_institution_id": p.author_institution_id,
            "quorum_required": p.quorum_required,
            "deadline": iso(p.deadline), "status": p.status,
            "based_on_version": p.based_on_version,
            "created_at": iso(p.created_at),
            "effective_at": iso(p.effective_at),
            "superseded_at": iso(p.superseded_at),
            "cancelled_at": iso(p.cancelled_at),
        }

    @staticmethod
    def _condition_dict(c: Condition, rep_id: Optional[int] = None) -> dict:
        return {
            "id": c.id, "proposal_id": c.proposal_id, "version": c.version,
            "seq": c.seq, "title": c.title, "detail": c.detail, "kind": c.kind,
            "owner_institution_id": c.owner_institution_id,
            "created_by_institution_id": c.created_by_institution_id,
            "status": c.status, "created_at": iso(c.created_at),
            "satisfied_at": iso(c.satisfied_at),
            "satisfied_by_institution_id": c.satisfied_by_institution_id,
            "waiver": c.waiver,
            "waived_at": iso(c.waived_at),
            "waived_by_institution_id": c.waived_by_institution_id,
        }

    @staticmethod
    def _stance_dict(s) -> dict:
        return {
            "id": s.id, "proposal_id": s.proposal_id, "version": s.version,
            "institution_id": s.institution_id,
            "representative_id": s.representative_id,
            "vote": s.vote.value, "rationale": s.rationale,
            "status": s.status, "created_at": iso(s.created_at),
            "updated_at": iso(s.updated_at),
            "withdrawn_at": iso(s.withdrawn_at),
            "withdraw_reason": s.withdraw_reason,
        }


def _parse_deadline(deadline: Optional[str], tz_name: str) -> Optional[datetime]:
    """把“本地时间 + IANA 时区”的截止时间折算为 UTC。

    若时间串自带偏移则忽略 tz_name（以串本身为准）；
    否则用 tz_name 指定的时区解释。
    """
    if deadline is None or deadline.strip() == "":
        return None
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    raw = deadline.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        raise ValidationError(f"无法解析截止时间: {deadline!r}")
    try:
        tz = ZoneInfo(tz_name or "UTC")
    except ZoneInfoNotFoundError:
        raise ValidationError(
            f"未知时区: {tz_name!r}（需为 IANA 名称，如 Asia/Shanghai）")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return dt.astimezone(timezone.utc)


def json_loads(value: Any) -> Any:
    return json.loads(value) if value else {}


def _audit_target(response: Any, proposal_id: int) -> str:
    """从用例响应中还原审计目标 id（议题:版本）。"""
    version = None
    if isinstance(response, dict):
        for key in ("stance", "condition", "signature"):
            nested = response.get(key)
            if isinstance(nested, dict) and nested.get("version"):
                version = nested["version"]
                break
        if version is None:
            version = response.get("version")
    return f"{proposal_id}:v{version}" if version else f"{proposal_id}:latest"


def _any_active_rep_id(conn: Any, institution_id: int) -> int:
    """取任一在任代表 id，仅用于需要“院校身份”的用例鉴权。"""
    row = conn.execute(
        "SELECT id FROM representatives WHERE institution_id=? AND active=1 "
        "ORDER BY id LIMIT 1", (institution_id,)).fetchone()
    if row is None:
        raise AuthorizationError(
            f"院校 {institution_id} 没有在任代表，无法发起修订")
    return row["id"]


def _require_text(value: Any, field: str, *, max_len: int,
                  allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{field} 必须是字符串")
    value = value.strip()
    if not value and not allow_empty:
        raise ValidationError(f"{field} 不能为空")
    if len(value) > max_len:
        raise ValidationError(f"{field} 长度不能超过 {max_len}")
    return value
