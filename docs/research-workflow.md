# 有证据约束的研究工作流

研究接口用于查询已发布知识快照。模型可以提出检索任务、改写查询、撰写结论和核验引用；业务订单、退款、消息发送和任意代码执行均不在它的工具集合中。返回的结论只是知识回答，不表示已经办理业务。

## 两种明确选择的模式

`multi` 使用 LangGraph 的 `plan → execute → draft → verify → end` 状态图：

1. `planner` 通过模型工具调用产生有编号、有检索方法和依赖关系的任务。
2. 每个 `retriever` 通过独立模型调用选择或改写查询，再由绑定的检索回调取原文。独立任务有限并行；依赖任务收到前置任务的不可变 `Evidence` 后才继续。
3. `writer` 从本轮证据生成逐条 `claim_id / claim / source_id / quote`。
4. 独立 `verifier` 根据完整来源正文，逐条给出支持或拒绝判断。

`single` 是 **单个 researcher 加公共 verifier** 的消融基线：固定一个任务，同一个 researcher 顺序完成查询选择和撰写，保留连续的工具对话，再进入与多角色模式相同的最终核验。它没有模型规划、任务分解或并行取证，也不代表只调用一次模型。两种模式使用同一个取证回调、模型传输接口和最终引用校验，不会自动互相降级。

依赖库仅在调用研究功能时加载。没有配置模型或缺少 `tracedesk[research]` 时，接口明确返回 `SERVICE_NOT_CONFIGURED`；不会生成提取式替代回答。

## 接入边界

公共入口为 `ResearchConfig`、`ResearchOrchestrator` 和 `Retrieve`。服务装配层先锁定商店、会话和已发布快照，再创建取证回调：

```python
Retrieve = Callable[
    [str, str, tuple[Evidence, ...], ModelProvider],
    Awaitable[tuple[Evidence, ...]],
]
```

参数依次为查询、检索方法、直接前置任务的原文证据，以及**本轮共享预算约束的模型 provider**。Text2Cypher 等检索流程需要模型时，必须使用第四个参数；不得绕开它创建模型客户端。回调只返回证据，不能修改会话或业务状态。LangGraph 节点和 DAG 协调器合并结果，运行记录和会话持久化由外层负责。

第四个参数只在本次取证回调期间有效。回调内并发调用模型时，必须等待全部调用结束；其中一次失败，协调器会取消并等待剩余调用。回调提前返回而仍有模型请求未结束时，返回 `RESEARCH_MODEL_TASK_UNFINISHED`，不会把它当成功取证；回调结束后再使用该 provider 返回 `RESEARCH_MODEL_SCOPE_CLOSED`，不会继续发送请求。

```python
config = replace(
    ResearchConfig.from_env(),
    mode="multi",
    available_methods=available_methods_for_snapshot,
)
research = ResearchOrchestrator(provider, bound_retrieve, config)
result = await research.answer(question, history=history, trace=trace)
```

`history` 是只包含 `role` 和 `content` 的消息序列；角色限 `user`、`assistant`。历史内容和来源正文作为不可信数据传入模型，不能注入系统角色或额外工具。每次 `answer` 创建独立预算和状态，不依赖模块级对话或跨请求计数器。

可启用的方法是 `lexical`、`semantic`、`hybrid`、`graph`、`text2cypher`。配置白名单必须与当前快照和服务能力取交集，只把确实可用的方法暴露给模型；方法不可用、服务缺失或检索失败时立即报错，不替换检索方法。

Microsoft GraphRAG 的内部模型调用目前没有接入同一预算与用量接口，因此 `graphrag_local`、`graphrag_global` 不能加入研究方法白名单。它们由独立检索接口提供，不计作本研究工作流已经支持共享预算的能力。

## 引用与核验

证据必须使用公共的不可变 `Evidence` 类型。协调器拒绝跨商店、跨快照混用，也拒绝同一个来源编号对应不同正文、或同一文档版本对应不同原件 SHA-256。

确定性校验要求：

- `source_id` 来自本轮真实返回的证据。
- `quote` 是相应正文中连续的逐字片段，不做空白归一化或改写。
- 每条结论编号唯一，核验角色必须对每条结论恰好判断一次。
- 最终引用的标题、商店、快照、版本、原件哈希和页码均由服务端证据生成，不接受模型填入；引用起止位置也由服务端计算。

服务装配层仍要按绑定的商店和快照，在持久化前对照知识库真源复核引用。研究模块能发现返回结果之间的范围冲突，但不能替代数据库的身份校验。

来源编号存在或原文包含引号中的字串，只能证明引用对应关系，不能证明结论语义正确。公共 verifier 额外检查否定、条件、例外、对象、时间、金额、范围和因果关系。它的判断仍受所配置模型能力影响，需要真实模型评测；协议测试不能证明答案准确率。

最终答案只包含核验支持的结论：全部支持返回 `answered`，部分支持返回 `partially_supported`，没有可支持结论返回 `insufficient_evidence`。被拒绝的结论不会进入答案或最终引用，元数据保留其结论编号和简短拒绝理由。没有原文可供取证则报 `RESEARCH_NO_EVIDENCE`，不进入撰写阶段。

## 预算与运行记录

| 环境变量 | 默认值 | 含义 |
| --- | --- | --- |
| `TRACEDESK_RESEARCH_MODE` | `multi` | `multi` 或 `single` |
| `TRACEDESK_RESEARCH_METHODS` | `lexical,semantic,hybrid,graph,text2cypher` | 装配层还需与实际可用能力取交集 |
| `TRACEDESK_RESEARCH_MAX_TASKS` | `6` | 整个计划的任务上限 |
| `TRACEDESK_RESEARCH_MAX_PARALLEL` | `3` | 并行任务及共享模型调用槽位上限 |
| `TRACEDESK_RESEARCH_MAX_MODEL_CALLS` | `16` | 包含嵌套检索模型调用的总请求预算 |
| `TRACEDESK_RESEARCH_MAX_RETRIEVAL_CALLS` | `8` | 所有任务共享的取证回调调用预算 |
| `TRACEDESK_RESEARCH_MAX_CONTEXT_CHARS` | `240000` | 所有模型请求的累计上下文字符预算 |
| `TRACEDESK_RESEARCH_TIMEOUT_SECONDS` | `120` | 整个研究流程的总时限 |

模型请求在共享锁内预约调用和上下文预算，取得共享并发槽位后才发送。上下文按 `messages` 和 `tools` 的紧凑 JSON 字符数累计，包含全部提示、历史、依赖证据和响应工具 schema；它不是 token 估算。超限直接报 `RESEARCH_BUDGET_EXCEEDED`，不裁剪证据、静默缩短历史或跳过核验。

`research` 元数据包括计划、完成任务的实际查询与来源、角色记录、取证记录、核验判断、预算使用量和耗时。`total_tokens` 只累计 provider 真正返回的用量；任何一次调用缺少该字段或未收到完整响应，总值就是 `null`，每次已报告的用量仍单独保留。没有估算费用或伪造 token 数据。

角色记录只保存角色、步骤、任务编号、用量、耗时和结果状态；不会保存模型私有推理或完整回复。发生取消、超时或错误时，协调器取消并等待仍在运行的兄弟任务，保留明确追踪。异常转换为 `DomainError` 时保留原始异常链，回调提供的服务错误码不会被替换成成功结果。

## 验证范围

`tests/test_research.py` 使用现有 `ChatCompletionsProvider` 和 `parse_reply` 的结构化协议夹具，验证角色交接、工具边界、真实 LangGraph 状态图执行、并发屏障、依赖先后、总预算、嵌套模型计数、引用错配、核验拒绝、异常和取消传播，以及不同运行之间的隔离。

测试没有调用未经配置的真实模型，不代表真实研究质量已经验收。配置模型后，应在相同知识快照、相同问题和相同预算下对比两种模式，检查答案支持率、错误拒绝、覆盖范围、用量和耗时。
