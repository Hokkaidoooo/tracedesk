"""The same tool registry drives model schemas and authorized execution."""

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ValidationError

from .errors import DomainError
from .refunds import RefundService
from .retrieval import Document, KnowledgeBase
from .schemas import FinalAnswer, LookupOrder, PrepareRefund, SearchKnowledge
from .settings import Settings
from .store import Store
from .trace import Trace

ORDER_FIELDS = ("id", "product", "amount_minor", "currency", "status", "delivered_at", "version")
ORDER_STATUSES = {
    "delivered": "已签收",
    "shipped": "运输中",
    "refund_requested": "已提交退款申请",
}
CLARIFICATIONS = {
    "order_id": "请提供订单号，例如 TD-1001。我会先核对订单归属和退货条件。",
    "one_order": "请一次提供一个订单号，以便分别核对金额和申请条件。",
    "capabilities": "可以查询退货政策和演示订单，也可以准备待确认的退款申请。请告诉我问题或订单号。",
    "cancel_existing": "本轮没有准备新申请。如需取消已有待确认申请，请点击对应卡片上的取消。",
}


def public_order(order: dict[str, Any]) -> dict[str, Any]:
    return {key: order[key] for key in ORDER_FIELDS}


def describe_order(order: dict[str, Any]) -> str:
    state = ORDER_STATUSES.get(order["status"], order["status"])
    return (
        f"订单 {order['id']}：{order['product']}，实付 {order['amount_minor'] / 100:.2f} 元，"
        f"当前状态为{state}。"
    )


def citation(document: Document, score: float | None, quote: str | None = None) -> dict[str, Any]:
    return {
        "source_id": document.source_id,
        "title": document.title,
        "quote": document.text if quote is None else quote,
        "version": document.version,
        "effective_from": document.effective_from.isoformat(),
        "effective_to": document.effective_to.isoformat() if document.effective_to else None,
        "score": score,
    }


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    schema: type[BaseModel]
    handler: Callable[[Any], dict[str, Any]]
    label: str
    stage: str


class ToolExecutor:
    def __init__(
        self,
        store: Store,
        knowledge: KnowledgeBase,
        refunds: RefundService,
        settings: Settings,
        session: dict[str, Any],
        run_id: str,
        trace: Trace,
    ) -> None:
        self.store, self.knowledge, self.refunds = store, knowledge, refunds
        self.settings, self.session, self.run_id, self.trace = settings, session, run_id, trace
        self.evidence: dict[str, tuple[Document, float | None]] = {}
        self.order: dict[str, Any] | None = None
        self.action: dict[str, Any] | None = None
        self.final: dict[str, Any] | None = None
        self.last_error: DomainError | None = None
        specs = (
            ToolSpec(
                "search_knowledge",
                "在当前商店的生效知识中检索。政策问题先调用此工具；返回原文和来源 ID。",
                SearchKnowledge,
                self._search,
                "检索生效知识",
                "retrieve",
            ),
            ToolSpec(
                "lookup_order",
                "查询当前会话客户的订单。不能传入客户身份，不存在或不属于本人的订单均不可读取。",
                LookupOrder,
                self._lookup,
                "查询订单",
                "tool",
            ),
            ToolSpec(
                "prepare_refund",
                "按当前政策准备退款申请。金额取自订单，只生成待确认申请，不执行提交或打款。",
                PrepareRefund,
                self._prepare,
                "核对售后条件",
                "policy",
            ),
            ToolSpec(
                "final_answer",
                "选择证据并结束本轮；服务端将原文和真实订单结果组装为答复。"
                "不接受自由编写的操作成功声明。政策引用必须使用本轮工具给出的 source_id 和逐字原文。"
                "缺少订单号选 needs_information；无依据或需人工处理选 handoff。"
                "准备申请后，服务端会生成准确的待确认说明。",
                FinalAnswer,
                self._finish,
                "核对回答依据",
                "validate",
            ),
        )
        self.registry = {spec.name: spec for spec in specs}

    def schemas(self) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": spec.name,
                    "description": spec.description,
                    "parameters": spec.schema.model_json_schema(),
                },
            }
            for spec in self.registry.values()
        ]

    def execute(self, name: str, arguments: str | dict[str, Any]) -> dict[str, Any]:
        started = self.trace.start()
        spec = self.registry.get(name)
        if spec is None:
            error = DomainError("TOOL_NOT_ALLOWED", "该工具未注册，未执行任何操作。")
            self.trace.add("gate", "拦截未知工具", "error", error.code, started)
            return {"ok": False, "error": error.as_dict()}
        try:
            value = (
                spec.schema.model_validate_json(arguments)
                if isinstance(arguments, str)
                else (spec.schema.model_validate(arguments))
            )
            result = spec.handler(value)
        except ValidationError as exc:
            fields = sorted({str(error["loc"][0]) for error in exc.errors() if error["loc"]})
            error = DomainError(
                "INVALID_TOOL_ARGUMENTS", f"工具参数不符合契约：{', '.join(fields)}。"
            )
            self.trace.add(spec.stage, spec.label, "error", error.code, started)
            return {"ok": False, "error": error.as_dict()}
        except DomainError as exc:
            self.last_error = exc
            self.trace.add(spec.stage, spec.label, "error", f"{exc.code}：{exc.message}", started)
            return {"ok": False, "error": exc.as_dict()}
        visible = {
            "ok": True,
            **{key: value for key, value in result.items() if not key.startswith("_")},
        }
        if len(json.dumps(visible, ensure_ascii=False)) > self.settings.max_tool_output_chars:
            error = DomainError("TOOL_OUTPUT_BUDGET", "工具结果超过预算，本轮已停止。", 422)
            self.trace.add(spec.stage, spec.label, "error", error.code, started)
            raise error
        if "_final" in result:
            self.final = result["_final"]
        self.trace.add(spec.stage, spec.label, "ok", result["_trace_detail"], started)
        return visible

    def _search(self, args: SearchKnowledge) -> dict[str, Any]:
        as_of = args.as_of or self.store.now().date()
        hits = self.knowledge.search(
            args.query,
            tenant_id=self.session["tenant_id"],
            as_of=as_of,
            method=self.settings.retrieval_method,
        )
        for hit in hits:
            self.evidence[hit.document.source_id] = (hit.document, hit.score)
        return {
            "as_of": as_of.isoformat(),
            "documents": [citation(hit.document, hit.score) for hit in hits],
            "_trace_detail": f"按 {as_of.isoformat()} 的有效期检索，返回 {len(hits)} 条知识。",
        }

    def _lookup(self, args: LookupOrder) -> dict[str, Any]:
        if self.action and self.action["order_id"] != args.order_id:
            raise DomainError(
                "ORDER_CONTEXT_LOCKED", "本轮已准备申请，查询其他订单请另开一轮对话。"
            )
        self.order = self.store.get_order(self.session, args.order_id)
        return {
            "order": public_order(self.order),
            "_trace_detail": f"已核对订单 {args.order_id} 的归属。",
        }

    def _prepare(self, args: PrepareRefund) -> dict[str, Any]:
        if self.action:
            if self.action["order_id"] != args.order_id or self.action["reason"] != args.reason:
                raise DomainError(
                    "ONE_ACTION_PER_TURN", "本轮已有申请；更改订单或原因请另开一轮对话。"
                )
            current = self.refunds.current_action(self.session, self.action["id"])
            if current["state"] != "pending":
                raise DomainError("ACTION_NOT_PENDING", "本轮申请已结束，请重新准备。")
            return self._prepared_result(replayed=True)
        policy = self.knowledge.get(
            "returns", tenant_id=self.session["tenant_id"], as_of=self.store.now().date()
        )
        if policy is not None:
            self.evidence[policy.source_id] = (policy, None)
        self.action = self.refunds.prepare(self.session, args.order_id, args.reason, self.run_id)
        self.order = self.store.get_order(self.session, args.order_id)
        return self._prepared_result(replayed=False)

    def _prepared_result(self, *, replayed: bool) -> dict[str, Any]:
        assert self.action is not None
        policy = self.knowledge.get(
            "returns", tenant_id=self.session["tenant_id"], as_of=self.store.now().date()
        )
        safe_action = {key: val for key, val in self.action.items() if key != "confirmation_token"}
        return {
            "action": safe_action,
            "replayed": replayed,
            "policy": citation(policy, None) if policy else None,
            "_trace_detail": f"申请 {self.action['id']} 待用户确认；尚未提交。",
        }

    def _finish(self, args: FinalAnswer) -> dict[str, Any]:
        citations = self._check_references(args)
        status = args.status
        if self.action:
            action = self.action
            policy_source = action["policy_source_id"]
            if policy_source not in self.evidence:
                raise DomainError(
                    "POLICY_EVIDENCE_MISSING", "申请对应的政策原文未提供，本轮不能交付申请。"
                )
            policy_document, policy_score = self.evidence[policy_source]
            citations = [citation(policy_document, policy_score)]
            status = "needs_confirmation"
            answer = (
                f"已准备订单 {action['order_id']} 的退款申请，金额为 "
                f"{action['amount_minor'] / 100:.2f} 元。请核对申请卡片后确认提交。"
                "确认只会在本地演示账本创建申请，不会触发实际打款。"
            )
        elif status == "answered":
            if self.order is None and not citations:
                raise DomainError(
                    "EVIDENCE_REQUIRED", "没有知识引用或已核验订单，不能生成确定性回答。"
                )
            sections = [describe_order(self.order)] if self.order else []
            sections.extend(item["quote"] for item in citations)
            answer = "\n\n".join(sections)
        elif status == "needs_information":
            answer = CLARIFICATIONS[args.clarification]
        elif self.last_error:
            answer = self.last_error.message + " 如需进一步处理，请联系人工客服。"
        else:
            answer = "当前知识库没有足够依据回答这个问题。请补充相关信息，或联系人工客服确认。"
        return {
            "accepted": True,
            "_final": {"answer": answer, "status": status, "citations": citations},
            "_trace_detail": f"{len(citations)} 条引用逐字核对通过；结果为 {status}。",
        }

    def _check_references(self, args: FinalAnswer) -> list[dict[str, Any]]:
        citations = []
        seen = set()
        for reference in args.citations:
            if reference.source_id not in self.evidence:
                raise DomainError("UNKNOWN_CITATION", "引用不属于本轮工具提供的证据。")
            if reference.source_id in seen:
                raise DomainError("DUPLICATE_CITATION", "相同来源应合并为一条引用。")
            doc, score = self.evidence[reference.source_id]
            if reference.quote not in doc.text:
                raise DomainError("QUOTE_MISMATCH", "引用文字与来源原文不一致。")
            seen.add(reference.source_id)
            # Keep the complete passage so a selected excerpt cannot drop policy qualifications.
            citations.append(citation(doc, score))
        return citations
