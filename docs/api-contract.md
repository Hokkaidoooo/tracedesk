# HTTP 接口

所有金额以分为整数存储，时间为 UTC ISO 8601。该版本操作自建演示订单；退款申请不会接入支付机构。业务接口需要 `Authorization: Bearer <session token>`，客户身份只能来自会话。

## 创建会话

`GET /api/info` → `{name, version, runtime, model, demo, business_date}`。

`GET /api/demo/customers` → `{customers: [{id, name, order_ids}]}`。演示客户 ID 为 `customer-a`、`customer-b`。

`POST /api/sessions`，正文 `{customer_id}` → `{id, token, customer: {id, name}, created_at}`。

`GET /api/sessions/{session_id}` → `{id, customer: {id, name}, turns: [Turn]}`。token 仅在创建时返回。

## 对话

`POST /api/sessions/{session_id}/turns`，正文 `{message, request_id}`。`request_id` 为每次用户发送生成的 UUID；网络重试须沿用同一个 ID。返回一个 Turn：

```json
{
  "id": "turn-id",
  "run_id": "run-id",
  "request_id": "uuid",
  "message": "订单 TD-1001 可以退款吗？",
  "answer": "已准备退款申请，请核对后确认。",
  "status": "needs_confirmation",
  "runtime": "extractive",
  "citations": [{"source_id": "returns@2", "title": "退货与退款", "quote": "可核对的原文", "version": 2, "effective_from": "2026-01-01", "effective_to": null, "score": 0.0}],
  "order": {"id": "TD-1001", "product": "折叠键盘", "amount_minor": 12900, "currency": "CNY", "status": "delivered", "delivered_at": "2026-10-14T00:00:00Z", "version": 1},
  "action": {"id": "action-id", "kind": "refund_request", "state": "pending", "order_id": "TD-1001", "amount_minor": 12900, "currency": "CNY", "reason": "用户申请退款", "expires_at": "2026-10-16T12:00:00Z", "confirmation_token": "one-time-secret"},
  "trace": [{"seq": 1, "stage": "retrieve", "label": "检索政策", "status": "ok", "detail": "命中 2 条生效知识", "duration_ms": 1.2}],
  "created_at": "2026-10-16T11:50:00Z"
}
```

`status` 为 `answered`、`needs_information`、`needs_confirmation`、`handoff`。`order`、`action` 可为 null。`runtime` 为 `extractive` 或 `llm`。引用内容来自本次工具结果；运行记录只保存可观察事件，不保存模型思维链。

## 售后申请

`POST /api/actions/{action_id}/confirm`，正文 `{confirmation_token, request_id}` → `{id, state: "submitted", order_id, amount_minor, currency, receipt_id, submitted_at, replayed}`。

`POST /api/actions/{action_id}/cancel`，正文 `{}` → `{id, state: "cancelled"}`。

提交必须来自准备该申请的会话。重复提交返回同一回执；过期、取消、金额或订单状态改变均显式失败。

## 运行记录与评测

`GET /api/runs/{run_id}` → `{id, status, runtime, trace, turn}`。仅当前会话可读取。

`GET /api/evaluation` → 固定回归集最近一次实际生成的评测 JSON。没有报告时返回显式 404，不生成默认成功指标。报告包含 `generated_at`、`scope`、`dataset_sha256`、`retrieval`（各方法 recall_at_3、mrr、queries）和 `workflow`（total、passed、cases）。这些是演示知识上的固定回归结果，不代表真实客户满意度或线上模型准确率。

## 错误

业务错误为 `{"error": {"code": "ACTION_EXPIRED", "message": "申请已过期，请重新准备。"}}`；参数验证错误同样有结构化 `code` 与 `message`。模型配置和调用错误不会切换为离线回答。
