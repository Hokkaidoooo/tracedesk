"""One schema per tool serves both model descriptions and runtime validation."""

from datetime import date
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class NewSession(StrictInput):
    customer_id: str = Field(min_length=1, max_length=80)


class NewTurn(StrictInput):
    message: str = Field(min_length=1, max_length=2000)
    request_id: UUID


class ConfirmAction(StrictInput):
    confirmation_token: str = Field(min_length=16, max_length=200)
    request_id: UUID


class SearchKnowledge(StrictInput):
    query: str = Field(min_length=1, max_length=800, description="用户问题的检索表达式")
    as_of: date | None = Field(default=None, description="仅用户明确询问历史政策时填写日期")


class LookupOrder(StrictInput):
    order_id: str = Field(pattern=r"^TD-\d{4,8}$", description="订单编号；身份由服务端绑定")


class PrepareRefund(LookupOrder):
    reason: str = Field(min_length=1, max_length=300, description="用户提出的申请原因")


class EvidenceReference(StrictInput):
    source_id: str = Field(min_length=1, max_length=100)
    quote: str = Field(min_length=8, max_length=3000, description="从工具提供的知识中逐字引用")


class FinalAnswer(StrictInput):
    status: Literal["answered", "needs_information", "handoff"] = "answered"
    citations: list[EvidenceReference] = Field(default_factory=list, max_length=6)
    clarification: Literal["order_id", "one_order", "capabilities", "cancel_existing"] | None = None

    @model_validator(mode="after")
    def require_clarification(self) -> "FinalAnswer":
        if self.status == "needs_information" and self.clarification is None:
            raise ValueError("补充信息必须指定 clarification。")
        if self.status != "needs_information" and self.clarification is not None:
            raise ValueError("clarification 只适用于补充信息。")
        return self
