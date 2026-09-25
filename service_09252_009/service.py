"""应用服务：编排事务、幂等、审计与决议生效/修订流程。"""
from __future__ import annotations

import json
from dataclasses import asdict
from datetime import timedelta
from typing import Any

from .clock import Clock, SystemClock, new_id
from .errors import (
    AuthorizationError,
    ConflictError,
    NotFoundError,
    ValidationError,
)
from .models import (
    AuditEvent,
    Condition,
    ConditionStatus,
    Institution,
    Issue,
    IssueStatus,
    Proposal,
    RevisionRequest,
    Signature,
    Stance,
    Vote,
)
from .rules import assert_before_deadline, evaluate, parse_deadline
from .storage import SQLiteRepository

STANCES = {s.value: s for s in Stance}


class ResolutionService:
    def __init__(self, repo: SQLiteRepository, clock: Clock | None = None) -> None:
        self.repo = repo
        self.clock = clock or SystemClock()

    # ==================================================================
    # 辅助
    # ==================================================================
    def _require_issue_conn(self, conn, issue_id: str) -> Issue:
        row = conn.execute("SELECT * FROM issues WHERE id = ?", (issue_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"议题不存在: {issue_id}")
        from .storage import _row_to_issue

        return _row_to_issue(row)

    def _require_institution(self, code: str) -> Institution:
        inst = self.repo.get_institution(code)
        if inst is None:
            raise NotFoundError(f"机构不存在: {code}")
        return inst

    def _require_institution_conn(self, conn, code: str) -> Institution:
        row = conn.execute("SELECT * FROM institutions WHERE code = ?", (code,)).fetchone()
        if row is None:
            raise NotFoundError(f"机构不存在: {code}")
        from .storage import _row_to_institution

        return _row_to_institution(row)

    def _audit(
        self,
        conn,
        event_type: str,
        actor: str,
        payload: dict[str, Any],
        issue_id: str | None = None,
        institution: str | None = None,
    ) -> int:
        event = AuditEvent(
            seq=0,
            event_id=new_id(),
            at=self.clock.now(),
            event_type=event_type,
            issue_id=issue_id,
            institution=institution,
            actor=actor,
            payload=json.dumps(payload, ensure_ascii=False, sort_keys=True),
        )
        return self.repo.insert_audit(conn, event)

    def _idem_lookup(self, key: str | None) -> dict[str, Any] | None:
        if not key:
            return None
        found = self.repo.get_idempotency(key)
        if found is not None:
            ref, event_seq = found
            return {"replayed": True, "idempotency_key": key, "ref": ref, "event_seq": event_seq}
        return None

    def _idem_hit_conn(self, conn, key: str | None) -> dict[str, Any] | None:
        """写事务内的幂等复查，拦截并发同键请求（调用前不得有写操作）。"""
        if not key:
            return None
        row = conn.execute(
            "SELECT result_ref, event_seq FROM idempotency_keys WHERE key = ?", (key,)
        ).fetchone()
        if row is not None:
            return {"replayed": True, "idempotency_key": key,
                    "ref": row["result_ref"], "event_seq": row["event_seq"]}
        return None

    def _idem_store(self, conn, key: str | None, event_seq: int, ref: str) -> None:
        if key:
            self.repo.store_idempotency(conn, key, event_seq, ref, self.clock.now())

    def _evaluate_conn(self, conn, issue: Issue):
        members = [
            self._require_institution_conn(conn, r["code"])
            for r in conn.execute("SELECT code FROM institutions WHERE is_member = 1").fetchall()
        ]
        declarations = self.repo.list_conflicts(conn, issue.id)
        votes = self.repo.list_active_votes(conn, issue.id, issue.current_seq)
        cond_rows = conn.execute(
            "SELECT * FROM conditions WHERE issue_id = ? ORDER BY id", (issue.id,)
        ).fetchall()
        from .storage import _row_to_condition

        conditions = [_row_to_condition(r) for r in cond_rows]
        sigs = self.repo.list_signatures(conn, issue.id, issue.current_seq)
        return evaluate(issue, members, declarations, votes, conditions, len(sigs))

    def _maybe_adopt(self, conn, issue: Issue) -> Issue:
        """重算尚未生效议题；满足条件则生效。返回刷新后的议题。"""
        if issue.status is IssueStatus.OPEN:
            ev = self._evaluate_conn(conn, issue)
            if ev.adopted and ev.signatures_complete:
                now = self.clock.now()
                self.repo.update_issue_state(
                    conn, issue.id, IssueStatus.ADOPTED,
                    adopted_seq=issue.current_seq, adopted_at=now,
                )
                self._audit(
                    conn, "issue_adopted", "system",
                    {"evaluation": ev.as_dict()}, issue_id=issue.id,
                )
                issue = self._require_issue_conn(conn, issue.id)
        return issue

    # ==================================================================
    # 机构登记与代表更换
    # ==================================================================
    def register_institution(
        self, code: str, name: str, delegate: str | None = None, is_member: bool = True
    ) -> dict[str, Any]:
        if not code or not name:
            raise ValidationError("机构代码与名称必填")
        inst = Institution(
            code=code, name=name, delegate=delegate, is_member=is_member,
            created_at=self.clock.now(),
        )
        with self.repo.transaction(write=True) as conn:
            if conn.execute(
                "SELECT 1 FROM institutions WHERE code = ?", (code,)
            ).fetchone():
                raise ConflictError(f"机构已存在: {code}")
            self.repo.insert_institution(conn, inst)
            seq = self._audit(
                conn, "institution_registered", code,
                {"name": name, "delegate": delegate, "is_member": is_member},
                institution=code,
            )
        return {"institution": asdict(inst), "audit_seq": seq}

    def replace_delegate(self, institution: str, new_delegate: str) -> dict[str, Any]:
        """更换代表。历史投票保留当时代表姓名，此后表态使用新代表。"""
        if not new_delegate:
            raise ValidationError("新代表姓名必填")
        self._require_institution(institution)
        with self.repo.transaction(write=True) as conn:
            old = self._require_institution_conn(conn, institution)
            self.repo.update_delegate(conn, institution, new_delegate)
            seq = self._audit(
                conn, "delegate_replaced", institution,
                {"old_delegate": old.delegate, "new_delegate": new_delegate},
                institution=institution,
            )
        return {
            "institution": institution,
            "old_delegate": old.delegate,
            "new_delegate": new_delegate,
            "audit_seq": seq,
        }

    # ==================================================================
    # 议题与版本化提案
    # ==================================================================
    def create_issue(
        self,
        title: str,
        body: str,
        author_institution: str,
        deadline_local: str,
        tzname: str,
        language: str = "zh",
        summary: str = "",
        quorum_ratio: float = 2 / 3,
        signature_ratio: float = 1.0,
        parent_issue_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        replay = self._idem_lookup(idempotency_key)
        if replay is not None:
            return replay  # type: ignore[return-value]
        if not title or not body:
            raise ValidationError("议题标题与初版提案正文必填")
        if not 0 < quorum_ratio <= 1 or not 0 < signature_ratio <= 1:
            raise ValidationError("比例参数必须在 (0, 1] 区间")
        deadline_utc, tzname = parse_deadline(deadline_local, tzname)
        now = self.clock.now()
        if deadline_utc <= now:
            raise ValidationError("截止时间必须晚于当前时间")
        self._require_institution(author_institution)
        if parent_issue_id is not None and self.repo.get_issue(parent_issue_id) is None:
            raise NotFoundError(f"父议题不存在: {parent_issue_id}")

        issue_id = new_id()
        issue = Issue(
            id=issue_id, title=title, status=IssueStatus.OPEN, current_seq=1,
            adopted_seq=None, adopted_at=None, deadline_utc=deadline_utc,
            tzname=tzname, parent_issue_id=parent_issue_id,
            quorum_ratio=quorum_ratio, signature_ratio=signature_ratio,
            created_at=now,
        )
        proposal = Proposal(
            id=0, issue_id=issue_id, seq=1, title=title, body=body,
            language=language, summary=summary, supersedes_seq=None,
            author_institution=author_institution, created_at=now,
        )
        with self.repo.transaction(write=True) as conn:
            replay = self._idem_hit_conn(conn, idempotency_key)
            if replay is not None:
                return replay
            self.repo.insert_issue(conn, issue)
            self.repo.insert_proposal(conn, proposal)
            seq = self._audit(
                conn, "issue_created", author_institution,
                {"title": title, "deadline_utc": deadline_utc.isoformat(),
                 "tzname": tzname, "quorum_ratio": quorum_ratio,
                 "signature_ratio": signature_ratio,
                 "parent_issue_id": parent_issue_id},
                issue_id=issue_id, institution=author_institution,
            )
            self._idem_store(conn, idempotency_key, seq, issue_id)
        return {"issue_id": issue_id, "seq": 1, "replayed": False, "audit_seq": seq}

    def submit_proposal_revision(
        self,
        issue_id: str,
        institution: str,
        body: str,
        title: str | None = None,
        language: str = "zh",
        summary: str = "",
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """提交新版提案（翻译澄清或修改）。旧版未决条件自动失效。"""
        replay = self._idem_lookup(idempotency_key)
        if replay is not None:
            return replay  # type: ignore[return-value]
        if not body:
            raise ValidationError("提案正文必填")
        self._require_institution(institution)
        with self.repo.transaction(write=True) as conn:
            replay = self._idem_hit_conn(conn, idempotency_key)
            if replay is not None:
                return replay
            issue = self._require_issue_conn(conn, issue_id)
            if issue.status is not IssueStatus.OPEN:
                raise ConflictError(
                    "决议已生效，不能直接改版，需发起修订流程"
                    if issue.status is IssueStatus.ADOPTED
                    else "议题正处于修订流程，请在后继议题中提案"
                )
            assert_before_deadline(issue, self.clock.now())
            new_seq = issue.current_seq + 1
            proposal = Proposal(
                id=0, issue_id=issue_id, seq=new_seq,
                title=title or issue.title, body=body, language=language,
                summary=summary, supersedes_seq=issue.current_seq,
                author_institution=institution, created_at=self.clock.now(),
            )
            self.repo.insert_proposal(conn, proposal)
            self.repo.supersede_conditions(conn, issue_id, new_seq)
            self.repo.update_issue_state(
                conn, issue_id, IssueStatus.OPEN, current_seq=new_seq
            )
            seq = self._audit(
                conn, "proposal_revised", institution,
                {"new_seq": new_seq, "supersedes_seq": issue.current_seq,
                 "language": language},
                issue_id=issue_id, institution=institution,
            )
            self._idem_store(conn, idempotency_key, seq, f"{issue_id}:{new_seq}")
        return {"issue_id": issue_id, "seq": new_seq, "replayed": False, "audit_seq": seq}

    # ==================================================================
    # 结构化条件
    # ==================================================================
    def attach_condition(
        self,
        issue_id: str,
        institution: str,
        condition_code: str,
        description: str,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        replay = self._idem_lookup(idempotency_key)
        if replay is not None:
            return replay  # type: ignore[return-value]
        if not condition_code or not description:
            raise ValidationError("条件代码与说明必填")
        self._require_institution(institution)
        with self.repo.transaction(write=True) as conn:
            replay = self._idem_hit_conn(conn, idempotency_key)
            if replay is not None:
                return replay
            issue = self._require_issue_conn(conn, issue_id)
            if issue.status is not IssueStatus.OPEN:
                raise ConflictError("议题已不在协商阶段，不能附加条件")
            assert_before_deadline(issue, self.clock.now())
            existing = self.repo.get_condition(conn, issue_id, condition_code)
            if existing is not None:
                # 自然幂等：同一代码视为重复请求
                return {"issue_id": issue_id, "condition_code": condition_code,
                        "replayed": True, "audit_seq": None}
            cond = Condition(
                id=0, issue_id=issue_id, proposal_seq=issue.current_seq,
                code=condition_code, raised_by=institution, description=description,
                status=ConditionStatus.OUTSTANDING, resolved_by=None, note=None,
                created_at=self.clock.now(), resolved_at=None,
            )
            self.repo.insert_condition(conn, cond)
            seq = self._audit(
                conn, "condition_attached", institution,
                {"condition_code": condition_code, "proposal_seq": issue.current_seq,
                 "description": description},
                issue_id=issue_id, institution=institution,
            )
            self._idem_store(conn, idempotency_key, seq, f"{issue_id}:cond:{condition_code}")
        return {"issue_id": issue_id, "condition_code": condition_code,
                "status": ConditionStatus.OUTSTANDING.value,
                "replayed": False, "audit_seq": seq}

    def resolve_condition(
        self,
        issue_id: str,
        institution: str,
        condition_code: str,
        fulfilled: bool,
        note: str = "",
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """条件由提出机构自行标记为已满足或放弃。机构只能处置自己的条件。"""
        replay = self._idem_lookup(idempotency_key)
        if replay is not None:
            return replay  # type: ignore[return-value]
        self._require_institution(institution)
        with self.repo.transaction(write=True) as conn:
            replay = self._idem_hit_conn(conn, idempotency_key)
            if replay is not None:
                return replay
            issue = self._require_issue_conn(conn, issue_id)
            cond = self.repo.get_condition(conn, issue_id, condition_code)
            if cond is None:
                raise NotFoundError(f"条件不存在: {condition_code}")
            if cond.raised_by != institution:
                raise AuthorizationError("只有提出条件的机构可以更新该条件")
            if cond.status is ConditionStatus.SUPERSEDED:
                raise ConflictError("该条件依附的提案版本已被取代")
            if issue.status is IssueStatus.ADOPTED:
                raise ConflictError("决议已生效，请走修订流程")
            new_status = (
                ConditionStatus.FULFILLED if fulfilled else ConditionStatus.WAIVED
            )
            now = self.clock.now()
            self.repo.update_condition(
                conn, cond.id, new_status, institution, note or None, now
            )
            seq = self._audit(
                conn, "condition_resolved", institution,
                {"condition_code": condition_code, "new_status": new_status.value,
                 "note": note},
                issue_id=issue_id, institution=institution,
            )
            self._idem_store(conn, idempotency_key, seq, f"{issue_id}:cond:{condition_code}")
            issue = self._maybe_adopt(conn, issue)
        return {
            "issue_id": issue_id, "condition_code": condition_code,
            "status": new_status.value, "replayed": False,
            "issue_status": issue.status.value, "audit_seq": seq,
        }

    # ==================================================================
    # 利益冲突
    # ==================================================================
    def declare_conflict(
        self,
        issue_id: str,
        institution: str,
        declared: bool,
        reason: str | None = None,
    ) -> dict[str, Any]:
        self._require_institution(institution)
        with self.repo.transaction(write=True) as conn:
            issue = self._require_issue_conn(conn, issue_id)
            self.repo.upsert_conflict(
                conn, issue_id, institution, declared, reason, self.clock.now()
            )
            seq = self._audit(
                conn, "conflict_declared", institution,
                {"declared": declared, "reason": reason},
                issue_id=issue_id, institution=institution,
            )
            # 冲突状态变化改变适格基数，须重算
            issue = self._maybe_adopt(conn, issue)
        return {"issue_id": issue_id, "institution": institution,
                "declared": declared, "issue_status": issue.status.value,
                "audit_seq": seq}

    # ==================================================================
    # 投票（表态）与撤回
    # ==================================================================
    def cast_vote(
        self,
        issue_id: str,
        institution: str,
        stance: str,
        rationale: str = "",
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        replay = self._idem_lookup(idempotency_key)
        if replay is not None:
            return replay  # type: ignore[return-value]
        if stance not in STANCES:
            raise ValidationError(f"无效表态: {stance}（应为 for/against/abstain）")
        parsed_stance = STANCES[stance]
        inst = self._require_institution(institution)
        if not inst.is_member:
            raise AuthorizationError("非成员机构无表决权")
        if not inst.delegate:
            raise ConflictError("该机构尚未指定代表，不能表态")
        with self.repo.transaction(write=True) as conn:
            replay = self._idem_hit_conn(conn, idempotency_key)
            if replay is not None:
                return replay
            issue = self._require_issue_conn(conn, issue_id)
            if issue.status is not IssueStatus.OPEN:
                raise ConflictError("议题不在投票阶段（已生效或修订中）")
            assert_before_deadline(issue, self.clock.now())
            conflicts = self.repo.list_conflicts(conn, issue_id)
            if any(c.institution == institution and c.declared for c in conflicts):
                raise AuthorizationError("该机构已声明利益冲突，不得投票")

            existing = self.repo.get_vote(conn, issue_id, issue.current_seq, institution)
            now = self.clock.now()
            if (
                existing is not None
                and existing.stance is parsed_stance
                and existing.delegate == inst.delegate
                and existing.rationale == rationale
            ):
                # 同态重复请求（立场、代表、理由均未变）：自然幂等，不产生新审计
                return {"issue_id": issue_id, "seq": issue.current_seq,
                        "institution": institution, "stance": stance,
                        "replayed": True, "audit_seq": None}
            vote = Vote(
                id=0,
                issue_id=issue_id, proposal_seq=issue.current_seq,
                institution=institution, delegate=inst.delegate,
                stance=parsed_stance, rationale=rationale, active=True,
                created_at=now, updated_at=now,
            )
            if existing is not None:
                # 改票：旧表态（含当时代表姓名）留痕，插入新行
                self.repo.deactivate_active_vote(
                    conn, issue_id, issue.current_seq, institution, now
                )
            self.repo.insert_vote(conn, vote)
            event_type = "vote_cast" if existing is None else "vote_changed"
            seq = self._audit(
                conn, event_type, institution,
                {"proposal_seq": issue.current_seq, "stance": stance,
                 "delegate": inst.delegate, "rationale": rationale,
                 "previous_stance": existing.stance.value if existing else None},
                issue_id=issue_id, institution=institution,
            )
            self._idem_store(conn, idempotency_key, seq,
                             f"{issue_id}:vote:{institution}")
            issue = self._maybe_adopt(conn, issue)
            evaluation = self._evaluate_conn(conn, issue)
        return {"issue_id": issue_id, "seq": issue.current_seq,
                "institution": institution, "stance": stance,
                "delegate": inst.delegate, "replayed": False,
                "issue_status": issue.status.value, "audit_seq": seq,
                "evaluation": evaluation.as_dict()}

    def withdraw_vote(self, issue_id: str, institution: str) -> dict[str, Any]:
        """撤回表态：尚未生效议题立即重算；已生效决议只能走修订流程。"""
        self._require_institution(institution)
        with self.repo.transaction(write=True) as conn:
            issue = self._require_issue_conn(conn, issue_id)
            if issue.status in (IssueStatus.ADOPTED, IssueStatus.UNDER_REVISION):
                raise ConflictError("决议已生效，撤回表态须发起修订流程而非直接撤回")
            existing = self.repo.get_vote(conn, issue_id, issue.current_seq, institution)
            if existing is None or not existing.active:
                return {"issue_id": issue_id, "institution": institution,
                        "replayed": True, "active": False, "audit_seq": None}
            now = self.clock.now()
            self.repo.deactivate_vote(conn, existing.id, now)
            seq = self._audit(
                conn, "vote_withdrawn", institution,
                {"proposal_seq": issue.current_seq,
                 "previous_stance": existing.stance.value},
                issue_id=issue_id, institution=institution,
            )
            issue = self._require_issue_conn(conn, issue_id)
            evaluation = self._evaluate_conn(conn, issue)
        return {"issue_id": issue_id, "institution": institution,
                "replayed": False, "active": False,
                "issue_status": issue.status.value, "audit_seq": seq,
                "evaluation": evaluation.as_dict()}

    # ==================================================================
    # 签署
    # ==================================================================
    def sign(
        self,
        issue_id: str,
        institution: str,
        signer: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        replay = self._idem_lookup(idempotency_key)
        if replay is not None:
            return replay  # type: ignore[return-value]
        inst = self._require_institution(institution)
        with self.repo.transaction(write=True) as conn:
            replay = self._idem_hit_conn(conn, idempotency_key)
            if replay is not None:
                return replay
            issue = self._require_issue_conn(conn, issue_id)
            if issue.status not in (IssueStatus.OPEN, IssueStatus.ADOPTED):
                raise ConflictError("议题处于修订流程，不能签署当前版本")
            vote = self.repo.get_vote(conn, issue_id, issue.current_seq, institution)
            if vote is None or not vote.active or vote.stance is not Stance.FOR:
                raise AuthorizationError("只有对当前版本投赞成票的机构可以签署")
            conflicts = self.repo.list_conflicts(conn, issue_id)
            if any(c.institution == institution and c.declared for c in conflicts):
                raise AuthorizationError("存在利益冲突的机构不能签署")
            existing_sig = self.repo.get_signature(
                conn, issue_id, issue.current_seq, institution
            )
            if existing_sig is not None:
                return {"issue_id": issue_id, "seq": issue.current_seq,
                        "institution": institution, "replayed": True,
                        "audit_seq": None}
            sig = Signature(
                id=0, issue_id=issue_id, proposal_seq=issue.current_seq,
                institution=institution, signer=signer or inst.delegate or institution,
                created_at=self.clock.now(),
            )
            self.repo.insert_signature(conn, sig)
            seq = self._audit(
                conn, "signed", institution,
                {"proposal_seq": issue.current_seq, "signer": sig.signer},
                issue_id=issue_id, institution=institution,
            )
            self._idem_store(conn, idempotency_key, seq,
                             f"{issue_id}:sig:{institution}")
            issue = self._maybe_adopt(conn, issue)
            evaluation = self._evaluate_conn(conn, issue)
        return {"issue_id": issue_id, "seq": issue.current_seq,
                "institution": institution, "signer": sig.signer,
                "replayed": False, "issue_status": issue.status.value,
                "audit_seq": seq, "evaluation": evaluation.as_dict()}

    # ==================================================================
    # 修订流程（已生效决议）
    # ==================================================================
    def request_revision(
        self,
        issue_id: str,
        institution: str,
        reason: str,
        new_deadline_local: str | None = None,
        new_tzname: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """已生效决议发起修订：原决议保持有效，开启后继议题重新协商。"""
        replay = self._idem_lookup(idempotency_key)
        if replay is not None:
            return replay  # type: ignore[return-value]
        if not reason:
            raise ValidationError("修订理由必填")
        self._require_institution(institution)
        with self.repo.transaction(write=True) as conn:
            replay = self._idem_hit_conn(conn, idempotency_key)
            if replay is not None:
                return replay
            issue = self._require_issue_conn(conn, issue_id)
            if issue.status is IssueStatus.OPEN:
                raise ConflictError("决议尚未生效，无需修订（可直接改版提案）")
            if issue.status is IssueStatus.UNDER_REVISION:
                row = conn.execute(
                    "SELECT successor_issue_id FROM revision_requests"
                    " WHERE issue_id = ? ORDER BY id DESC LIMIT 1", (issue_id,)
                ).fetchone()
                successor = row["successor_issue_id"] if row else "?"
                raise ConflictError(f"修订流程已在进行中，后继议题: {successor}")
            proposal_row = conn.execute(
                "SELECT * FROM proposals WHERE issue_id = ? AND seq = ?",
                (issue_id, issue.adopted_seq or issue.current_seq),
            ).fetchone()
            from .storage import _row_to_proposal

            latest = _row_to_proposal(proposal_row)

            tzname = new_tzname or issue.tzname
            if new_deadline_local:
                deadline_utc, tzname = parse_deadline(new_deadline_local, tzname)
            else:
                deadline_utc = self.clock.now() + timedelta(days=30)
            now = self.clock.now()
            successor_id = new_id()
            successor = Issue(
                id=successor_id, title=issue.title, status=IssueStatus.OPEN,
                current_seq=1, adopted_seq=None, adopted_at=None,
                deadline_utc=deadline_utc, tzname=tzname,
                parent_issue_id=issue_id, quorum_ratio=issue.quorum_ratio,
                signature_ratio=issue.signature_ratio, created_at=now,
            )
            carried = Proposal(
                id=0, issue_id=successor_id, seq=1, title=latest.title,
                body=latest.body, language=latest.language, summary=latest.summary,
                supersedes_seq=None, author_institution=institution, created_at=now,
            )
            self.repo.insert_issue(conn, successor)
            self.repo.insert_proposal(conn, carried)
            rev = RevisionRequest(
                id=0, issue_id=issue_id, institution=institution, reason=reason,
                created_at=now, successor_issue_id=successor_id,
            )
            rev_id = self.repo.insert_revision_request(conn, rev)
            self.repo.update_issue_state(conn, issue_id, IssueStatus.UNDER_REVISION)
            seq = self._audit(
                conn, "revision_requested", institution,
                {"reason": reason, "successor_issue_id": successor_id,
                 "revision_request_id": rev_id},
                issue_id=issue_id, institution=institution,
            )
            self._idem_store(conn, idempotency_key, seq, successor_id)
        return {"issue_id": issue_id, "successor_issue_id": successor_id,
                "parent_status": IssueStatus.UNDER_REVISION.value,
                "replayed": False, "audit_seq": seq}

    # ==================================================================
    # 查询
    # ==================================================================
    def list_issues(self) -> list[dict[str, Any]]:
        return [
            {"id": i.id, "title": i.title, "status": i.status.value,
             "current_seq": i.current_seq, "parent_issue_id": i.parent_issue_id}
            for i in self.repo.list_issues()
        ]

    def get_status(self, issue_id: str) -> dict[str, Any]:
        issue = self.repo.get_issue(issue_id)
        if issue is None:
            raise NotFoundError(f"议题不存在: {issue_id}")
        with self.repo.transaction() as conn:
            evaluation = self._evaluate_conn(conn, issue)
            proposals = self.repo.list_proposals(issue_id)
            conditions = self.repo.list_conditions(issue_id)
            votes = self.repo.list_vote_history(issue_id)
            conflicts = self.repo.list_conflicts(conn, issue_id)
            signatures = self.repo.list_signatures(conn, issue_id, issue.current_seq)
            revisions = self.repo.list_revision_requests(issue_id)
        from .rules import deadline_local_text

        return {
            "issue": {
                "id": issue.id,
                "title": issue.title,
                "status": issue.status.value,
                "current_seq": issue.current_seq,
                "adopted_seq": issue.adopted_seq,
                "adopted_at": issue.adopted_at.isoformat() if issue.adopted_at else None,
                "deadline_utc": issue.deadline_utc.isoformat(),
                "deadline_local": deadline_local_text(issue),
                "tzname": issue.tzname,
                "parent_issue_id": issue.parent_issue_id,
                "quorum_ratio": issue.quorum_ratio,
                "signature_ratio": issue.signature_ratio,
            },
            "proposals": [
                {"seq": p.seq, "title": p.title, "body": p.body, "language": p.language,
                 "summary": p.summary, "supersedes_seq": p.supersedes_seq,
                 "author_institution": p.author_institution,
                 "created_at": p.created_at.isoformat()}
                for p in proposals
            ],
            "conditions": [
                {"code": c.code, "proposal_seq": c.proposal_seq, "raised_by": c.raised_by,
                 "description": c.description, "status": c.status.value,
                 "resolved_by": c.resolved_by, "note": c.note,
                 "resolved_at": c.resolved_at.isoformat() if c.resolved_at else None}
                for c in conditions
            ],
            "votes": [
                {"proposal_seq": v.proposal_seq, "institution": v.institution,
                 "delegate": v.delegate, "stance": v.stance.value,
                 "rationale": v.rationale, "active": v.active,
                 "updated_at": v.updated_at.isoformat()}
                for v in votes
            ],
            "conflicts": [
                {"institution": c.institution, "declared": c.declared, "reason": c.reason}
                for c in conflicts
            ],
            "signatures": [
                {"institution": s.institution, "signer": s.signer,
                 "created_at": s.created_at.isoformat()}
                for s in signatures
            ],
            "revisions": [
                {"institution": r.institution, "reason": r.reason,
                 "successor_issue_id": r.successor_issue_id,
                 "created_at": r.created_at.isoformat()}
                for r in revisions
            ],
            "evaluation": evaluation.as_dict(),
        }

    def get_rationale(self, issue_id: str, institution: str) -> dict[str, Any]:
        """查询某机构在议题上的表态理由及其提出的条件。"""
        self._require_institution(institution)
        issue = self.repo.get_issue(issue_id)
        if issue is None:
            raise NotFoundError(f"议题不存在: {issue_id}")
        with self.repo.transaction() as conn:
            rows = conn.execute(
                "SELECT * FROM votes WHERE issue_id = ? AND institution = ? ORDER BY id",
                (issue_id, institution),
            ).fetchall()
            from .storage import _row_to_vote

            votes = [_row_to_vote(r) for r in rows]
            cond_rows = conn.execute(
                "SELECT * FROM conditions WHERE issue_id = ? AND raised_by = ? ORDER BY id",
                (issue_id, institution),
            ).fetchall()
            from .storage import _row_to_condition

            conds = [_row_to_condition(r) for r in cond_rows]
        return {
            "issue_id": issue_id,
            "institution": institution,
            "positions": [
                {"proposal_seq": v.proposal_seq, "stance": v.stance.value,
                 "delegate": v.delegate, "rationale": v.rationale,
                 "active": v.active, "at": v.updated_at.isoformat()}
                for v in votes
            ],
            "conditions": [
                {"code": c.code, "proposal_seq": c.proposal_seq,
                 "description": c.description, "status": c.status.value,
                 "note": c.note}
                for c in conds
            ],
        }

    def list_audit(self, issue_id: str | None = None) -> list[dict[str, Any]]:
        events = self.repo.list_audit(issue_id)
        return [
            {"seq": e.seq, "event_id": e.event_id, "at": e.at.isoformat(),
             "event_type": e.event_type, "issue_id": e.issue_id,
             "institution": e.institution, "actor": e.actor,
             "payload": json.loads(e.payload)}
            for e in events
        ]
