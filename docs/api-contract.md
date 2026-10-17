# HTTP 接口

业务接口使用 `Authorization: Bearer <session token>`。客户与商店身份来自会话，不能由请求正文或模型指定。金额为整数分，时间使用 UTC ISO 8601。当前身份和订单用于演示，退款申请不连接支付机构。

## 会话与售后对话

| 方法与路径 | 请求 / 返回 |
| --- | --- |
| `GET /api/info` | `name, version, runtime, model, demo, business_date` |
| `GET /api/demo/customers` | `customers: [{id, name, order_ids}]` |
| `POST /api/sessions` | 正文 `{customer_id}`；返回 `id, token, customer, created_at` |
| `GET /api/sessions/{session_id}` | 返回 `id, customer, turns`；URL 会话必须与 token 匹配 |
| `POST /api/sessions/{session_id}/turns` | 正文 `{message, request_id}`；返回售后 `Turn` |

`request_id` 必须为 UUID。同一会话的网络重试沿用相同 ID 和原始请求参数；修改消息应使用新 ID。会话 token 仅在创建时返回。

售后 `Turn` 包含：

- `id, run_id, request_id, message, answer, created_at`。
- `status`：`answered / needs_information / needs_confirmation / handoff`。
- `runtime`：`extractive / llm`。
- `citations`：政策引用。
- `order`：已核验订单或 `null`。
- `action`：售后申请或 `null`。
- `trace`：真实事件，字段为 `seq, stage, label, status, detail, duration_ms`。

政策引用字段为 `source_id, title, quote, version, effective_from, effective_to, score`。按政策版本直接读取时 `score=null`；检索分值仅用于排序，不表示回答正确率。

## 资料库与能力

`GET /api/knowledge` 返回当前会话所属商店的资料：

| 字段 | 含义 |
| --- | --- |
| `documents` | 全部原文版本，包括解析失败和历史版本 |
| `snapshots` | 构建中、已发布与失败快照 |
| `current_snapshot` | 当前发布快照，无发布时为 `null` |
| `methods` | 当前快照在现有配置下可用的检索方法 |
| `method_status` | 按方法返回 `{available, error}`，不可用时保留原因 |
| `capabilities` | PDF、嵌入、图配置、研究是否启用、默认模式和上传限制 |

`capabilities` 的字段为 `pdf_parser, embedding_provider, embedding_model, graph_backend, graph_extraction, microsoft_graphrag, research, research_default_mode, max_upload_bytes`。

`method_status` 检查当前模型指纹、来源、索引版本和所需配置；不把每次资料列表读取当作对所有外部服务的实时健康探测。真正请求遇到连接、额度或响应错误仍会失败。

`GET /api/knowledge/snapshots/{snapshot_id}` 读取当前商店的指定已发布快照，附带重新判定的 `methods`。不能通过指定 ID 读取其他商店、构建中或失败快照。

## 上传、重试与原件

`POST /api/knowledge/documents?filename=guide.md&title=设备指南` 使用**文件原始字节**作为正文，不是 JSON 或 multipart。可使用 `Content-Type: application/octet-stream`，由后端按扩展名确定格式；也可提供与文件匹配的媒体类型。

支持 UTF-8 `.txt / .md / .markdown` 和已配置解析器的 `.pdf`。文件名必须不含目录或控制字符，标题限制为 200 字符，上传大小受 `max_upload_bytes` 约束。

创建或幂等返回文档记录时为 HTTP 201。文档记录包含：

```text
document_id, tenant_id, title, version, filename, media_type,
source_sha256, status, parser, parse_id, parse_started_at,
created_at, finished_at, block_count, attempt_count, error, idempotent
```

`status` 为 `parsing / ready / failed`。相同商店、相同原件 SHA-256 重复上传返回同一文档版本；同标题的新原件递增版本。HTTP 201 不等于一个已有的在途解析已经完成，应检查 `status`。

首次解析失败会返回结构化错误，并保留已接收原件与失败状态。上传失败后可通过资料列表查询具体版本，不应假定错误响应意味着原件从未保存。

`POST /api/knowledge/documents/{document_id}/versions/{version}/retry` 显式重试失败版本。它读取数据库原件、保留文档版本并分配新解析尝试；已经成功或仍在活动解析的版本返回 `DOCUMENT_NOT_FAILED`。配置仍不可用或原件无效时，重试继续返回真实错误。

`GET /api/knowledge/documents/{document_id}/versions/{version}/source` 返回授权原件，含：

- `Content-Type`：保存的媒体类型。
- `Content-Disposition: attachment; filename*=UTF-8''...`。
- `X-Source-SHA256`：原件摘要。

下载需要同一个 Bearer 认证入口，不能把 token 放进下载 URL。后台校验原件摘要；当前商店可以读取其历史原件，包括解析失败的版本。

## 构建与示例

`POST /api/knowledge/snapshots`：

```json
{
  "with_embeddings": false,
  "with_graph": false
}
```

需要人工图关系时，可增加 `relations`：

```json
{
  "with_graph": true,
  "relations": [
    {
      "subject": "原文中的实体",
      "predicate": "RELATED_TO",
      "object": "原文中的另一实体",
      "source_ids": ["从实际证据响应取得的来源ID"]
    }
  ]
}
```

`source_ids` 必须来自实际资料块，不能自行生成。首次自有关系构建可以先发布词面快照，再取其中的稳定来源 ID；原件版本不变时，来源 ID 可用于后续图快照。

未提供关系时，普通图构建需要启用真实模型抽取。嵌入或图步骤失败不切换发布指针。构建只选择各文档最新版本；有最新版本尚未解析成功时明确拒绝，不能悄悄忽略。

成功构建返回 HTTP 201 与已发布快照。快照字段包括 `id, tenant_id, status, created_at, finished_at, with_embeddings, embedding_fingerprint, source_count, artifacts, error`。失败快照保留在列表中，不作为可检索版本。

`POST /api/knowledge/demo` 加载原创的 4 份示例资料与 6 条人工来源关系。正文可省略各选项：

```json
{
  "with_embeddings": false,
  "with_graph": true
}
```

返回 HTTP 201、最新 catalog 和 `loaded_snapshot_id`。启用 `with_embeddings` 要求真实嵌入配置；示例关系不需要语言模型，不能将其视作模型抽图结果。

## 固定快照检索

`POST /api/knowledge/search`：

```json
{
  "query": "需要核对的问题",
  "method": "lexical",
  "snapshot_id": "从资料列表取得的已发布快照ID",
  "limit": 5
}
```

`method` 为 `lexical / semantic / hybrid / graph / text2cypher / graphrag_local / graphrag_global`。默认 `lexical`，`limit` 为 1–20，查询最多 2000 字符。省略 `snapshot_id` 时，在请求开始解析一次当前发布快照，响应返回实际固定的 ID。

返回 `{snapshot_id, method, evidence: [Evidence]}`。未构建向量、当前嵌入指纹变化、图配置不匹配或依赖不可用时，不会自动换检索方法。`graph` 按已配置后端使用本地或 Neo4j；`text2cypher` 要求 Neo4j 投影、聊天模型和用于语义示例召回的嵌入模型。

`GET /api/knowledge/snapshots/{snapshot_id}/evidence/{source_id}` 可读取一条已发布原文证据。路径参数需要 URL 编码。

`Evidence` 包含：

| 字段 | 含义 |
| --- | --- |
| `source_id / tenant_id / snapshot_id` | 稳定来源与商店、快照作用域 |
| `document_id / document_version / title` | 原文身份与版本 |
| `text / quote` | 原始块正文与展示引用 |
| `source_sha256` | 原件 SHA-256 |
| `page` | 真实页号；未提供分页时为 `null` |
| `kind` | `text / table` |
| `score / method` | 实际分值与检索路径 |
| `graph_path` | 关系路径数组，没有图路径时为空 |

## 资料研究

`POST /api/sessions/{session_id}/research`：

```json
{
  "message": "结合这些资料给出可核对的结论。",
  "request_id": "a0000000-0000-4000-8000-000000000001",
  "mode": "multi",
  "snapshot_id": "从资料列表取得的已发布快照ID"
}
```

研究必须提供固定 `snapshot_id`。`mode` 可为 `multi / single`；省略或为 `null` 时使用服务配置的默认模式。客户端应在首次发送时保存解析后的模式，并在重试时显式复用。

返回研究 `Turn`：

- `runtime` 为 `research:multi:<snapshot_id>` 或 `research:single:<snapshot_id>`。
- `status` 为 `answered / partially_supported / insufficient_evidence`。
- `citations` 是文档 Evidence，额外包含 `claim_id, quote_start, quote_end`；`quote` 为支持结论的连续原文片段。
- `order=null`、`action=null`，研究不会办理退款。
- `research` 元数据包含模式、固定快照、计划、任务、角色记录、核验与实际预算用量。
- `trace` 保存可观察执行事件，不保存模型私有思维链。

`single` 使用单研究者加公共 verifier，`multi` 使用规划、分工取证、撰写和同样的 verifier。只有核验支持的结论进入最终回答。未配置模型、来源错配、无证据或预算耗尽时返回错误，不给出替代成功回答。

研究请求在 `runs.request_context` 中保存 `{research_mode, snapshot_id}`；售后请求使用空上下文。同一会话和请求 UUID 改变消息、模式或快照返回 `REQUEST_CONFLICT`。provider 配置切换不改变已经保存的合法回放。`runtime` 用于记录实际运行方式，不单独承担幂等判断。

## 售后申请

`POST /api/actions/{action_id}/confirm`，正文 `{confirmation_token, request_id}`，返回 `id, state, order_id, amount_minor, currency, receipt_id, submitted_at, replayed`。

`POST /api/actions/{action_id}/cancel`，正文 `{}`，返回 `{id, state: "cancelled"}`。

提交必须来自准备申请的会话。重复提交返回同一回执；过期、取消、金额或订单状态改变均显式失败。研究引用和上传资料不会覆盖正式退款政策。

## 运行、评测与错误

`GET /api/runs/{run_id}` 返回 `id, status, runtime, trace, turn`，失败时包含错误信息，仅所属会话可读取。

`GET /api/evaluation` 返回随包的政策与工作流评测报告。没有报告返回明确 404，不生成默认成功指标。新版资料检索评测通过 [CLI](knowledge-cli.md#检索消融评测) 生成独立报告。

业务错误使用 `{"error": {"code": "...", "message": "...", "details": {}}}`；`details` 可省略。HTTP 参数错误也有统一错误结构。常见处理方式：

| 错误 | 处理 |
| --- | --- |
| `UNAUTHORIZED` | 检查当前会话 token，不改用其他身份 |
| `DOCUMENT_ENCODING / DOCUMENT_FORMAT` | 提供有效原件；原件变更应上传为新版本 |
| `PARSER_NOT_CONFIGURED / PARSER_UNAVAILABLE` | 修正解析服务后，显式重试失败版本 |
| `DOCUMENT_NOT_READY` | 等待或修复最新原文版本，再构建 |
| `SNAPSHOT_BUSY / SNAPSHOT_NOT_ACTIVE` | 检查构建状态，不直接改发布指针 |
| `EMBEDDING_MODEL_MISMATCH` | 使用匹配模型，或构建新向量快照 |
| `RETRIEVAL_UNAVAILABLE` | 检查 `methods / method_status`，选择实际可用方法 |
| `SERVICE_NOT_CONFIGURED` | 配置所需真实模型和依赖 |
| `REQUEST_CONFLICT` | 新消息、模式或快照使用新 UUID；网络重试保留原参数 |

错误码来自具体服务，图和研究的详细错误见各自说明。
