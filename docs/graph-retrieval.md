# 图检索与 Microsoft GraphRAG

图检索为知识快照补充有来源的关系检索。SQLite 中的文档版本、来源块和已发布快照仍是唯一真源；本地图、Neo4j 和 Microsoft GraphRAG 索引都是该快照的投影。上传资料和图关系不会修改退款资格、身份校验或金额规则。

## 来源与发布

`GraphService.prepare(evidence, facts=...)` 返回可序列化的 `{"graph": ...}`。知识服务在构建新快照时调用它，只有全部索引构建成功才发布快照。图服务没有切换当前快照的接口。外部索引成功而后续步骤失败时，该投影仍不可通过已发布知识快照访问。

每条 `GraphFact` 必须引用当前快照中存在的来源 ID。证据不能混用租户或快照，引用的每一个原文块都必须出现关系两端实体；拉丁实体按完整单词边界匹配，统一 Unicode 与大小写。相同三元组的来源合并并排序。原文摘要、稳定来源 ID、文档版本和页码始终保留。

这些检查证明身份、原文位置与索引来源一致，不代替对关系含义的判断。人工关系由调用者显式提供；模型关系由真实 `ModelProvider.complete` 工具调用生成，并接受相同检查。

`extraction="explicit"` 是默认值。未传入关系时，产物明确记录 `extraction="explicit"`、`state="empty"` 和 `fact_count=0`；请求图检索返回 `graph_empty`，不会改用关键词结果。`extraction="model"` 需要实际模型，缺少模型返回 `graph_model_unavailable`。显式传入 `facts` 时记录为人工关系来源。

## 调用接口

```python
from tracedesk.graphs import GraphConfig, GraphService

graphs = GraphService(GraphConfig(), provider=model_provider)
artifacts = await graphs.prepare(snapshot_evidence, facts=source_backed_facts)
matches = await graphs.search(
    "指定实体的关联规定", snapshot_evidence, artifacts, method="graph", limit=5,
)
await graphs.close()
```

`snapshot_evidence` 为同一租户、同一快照的 `tuple[Evidence, ...]`；`source_backed_facts` 为 `tuple[GraphFact, ...]`。`search` 还接受按调用注入的 `provider`，使研究工作流能够共用模型调用预算。来源不匹配、配置后端改变、索引损坏或依赖缺失都会显式报错。

| 检索方式 | 执行路径 |
| --- | --- |
| `graph` | 按配置选择本地有界路径检索或 Neo4j 路径查询 |
| `neo4j` | 仅查询已完成的 Neo4j 投影 |
| `text2cypher` | 模型生成受限 Cypher，验证后在 Neo4j 执行 |
| `graphrag_local` | Microsoft GraphRAG 官方 Local Search |
| `graphrag_global` | Microsoft GraphRAG 官方 Global Search |

本地检索从问题中出现的实体出发，沿有向关系展开。默认最多 3 跳、20,000 次展开、20 条返回证据；超出展开预算会失败。结果的 `graph_path` 保留方向与多跳路径，证据正文仍为原始来源块。

## Neo4j

驱动依赖为 `neo4j>=5.28,<6`。连接可通过 `Neo4jProjection(reader_driver=..., writer_driver=..., database=...)` 注入，或由 `GraphConfig.from_env()` 配置后创建。

| 环境变量 | 含义 |
| --- | --- |
| `TRACEDESK_GRAPH_BACKEND` | `local`（默认）或 `neo4j` |
| `TRACEDESK_NEO4J_URI` | 明确的 Neo4j 连接地址 |
| `TRACEDESK_NEO4J_DATABASE` | 数据库名，默认 `neo4j` |
| `TRACEDESK_NEO4J_READER_USER` / `READER_PASSWORD` | 读取身份 |
| `TRACEDESK_NEO4J_WRITER_USER` / `WRITER_PASSWORD` | 导入身份 |

写入客户端只用于版本导入与自有标签的唯一约束，读取客户端只用于重新编译的查询。`READ` 路由参数只选择读取服务器，**不证明数据库账号具有只读权限**；生产部署应在 Neo4j 中给读取账号分配真实只读权限。相同账号的本地开发验证不能作为权限验收。

节点使用 `(tenant_id, snapshot_id, name)` 作用域，快照使用 `(tenant_id, snapshot_id)` 作用域。每条边也保存租户、快照、关系与来源 ID。导入在同一数据事务内完成，最后才标记 `ready`；失败会回滚。已存在版本的摘要不同会被拒绝，不能覆盖另一份投影。

每次读取先检查版本摘要和 `ready` 状态，再执行带租户、快照参数的语句。数据库返回的方向、关系、来源 ID 和路径长度还要与原投影比对。查询使用服务器事务超时与客户端超时。服务仅关闭自身拥有的驱动；注入驱动默认由调用者关闭，传入 `owns_drivers=True` 可以交由图服务管理。

## 受限 Text2Cypher

模型通过 `submit_cypher` 工具生成受限查询，例如：

```cypher
MATCH p=(a:Entity {name: "Alpha"})-[:USES|FOLLOWS*1..2]->(b:Entity)
RETURN p LIMIT 5
```

解析器接受单个路径表达式、`Entity` 标签、`name` 属性、快照中存在的关系类型、明确的跳数区间和 `LIMIT`。可指定目标实体，并支持入边与无向路径。查询不能提供租户、快照、任意属性、过程调用、子查询、聚合或额外子句。

这是完整的允许语法解析，不是写入词黑名单。词法或结构不合格时不会执行；即使实体名称中含有 `DELETE` 等单词，只要它是原文中的真实实体，也只作为绑定参数。通过验证后，编译器从固定模板重建 Cypher；模型提交的原始文本从不发送到 Neo4j。

生成最多尝试两次。第一次失败后，将明确的验证错误交给模型纠正，第二次仍须从解析、图 schema 和预算开始重新验证。任何验证失败都不能进入执行状态。查询计划在执行前还会再次验证。

少样本示例通过可注入的 `SemanticExampleRetriever.retrieve(query, limit=3)` 接入。应用默认装配 `SemanticCypherExamples`，对八个自建问题/Cypher 示例执行真实向量检索，最多返回三个。它复用知识模块的嵌入模型、维度、模型指纹、缓存、批量大小和超时，不增加另一套模型配置；示例仅用于说明查询语法，不能作为业务证据。

应用的 Text2Cypher 入口同时要求可用的 Neo4j 投影、聊天模型和语义示例嵌入服务。嵌入未配置、模型指纹改变或示例召回失败时明确不可用，不会改用固定示例、关键词示例或无示例查询。真实模型效果仍须另行验收，协议和合成示例测试不证明查询质量。

## Microsoft GraphRAG 2.7.0

适配器固定支持官方 [`graphrag==2.7.0`](https://pypi.org/project/graphrag/2.7.0/)，Python 范围为 3.10–3.12。其他版本会明确拒绝，升级应先核对上游 API 和来源表契约。

实际调用的官方 Python API 为：

- [`create_graphrag_config(values, root_dir=...)`](https://github.com/microsoft/graphrag/blob/v2.7.0/graphrag/config/create_graphrag_config.py)。
- [`build_index(config=..., method="standard", is_update_run=False, input_documents=...)`](https://github.com/microsoft/graphrag/blob/v2.7.0/graphrag/api/index.py)。
- [`local_search(...)` 和 `global_search(...)`](https://github.com/microsoft/graphrag/blob/v2.7.0/graphrag/api/query.py)，并读取其返回的上下文表。

官方 2.7.0 把模型客户端缓存在进程级 `ModelManager` 中，未提供公开关闭接口。本适配器每次操作使用一个独立 Python 子进程，操作结束即释放该进程中的客户端，避免快照之间复用隐式全局客户端。子进程错误会保存到快照目录的 `worker.log`，同时向调用者返回失败状态；超过预算会终止该子进程。

默认关闭 Microsoft GraphRAG。先在独立 Python 环境中安装官方包，使用官方 `graphrag init --root <配置目录>` 生成设置和提示词，再配置实际聊天与嵌入模型。模型设置通过 JSON 或 YAML 文件提供，`${变量名}` 从服务进程环境中解析；本适配器不自动加载 `.env` 或读取其他项目凭据。

```powershell
$env:TRACEDESK_GRAPHRAG_ENABLED = "true"
$env:TRACEDESK_GRAPHRAG_SETTINGS = "D:\apps\tracedesk-config\settings.yaml"
$env:TRACEDESK_GRAPHRAG_ROOT = "D:\apps\tracedesk-data\graphrag"
```

这些路径是部署示例，需替换为实际配置位置。API 密钥、模型名及兼容端点由设置文件或其引用的环境变量提供；缺项会失败，不使用模拟模型。

每个租户、快照使用分别哈希后的独立目录。输入 DataFrame 的文档 ID 直接使用稳定来源 ID，分块按 `id` 分组；输出表、缓存、日志和 LanceDB 均限定到该目录。适配器不接受共享云向量存储，也不支持未建立来源映射的 claims；显式启用这些配置会报错。将 `TRACEDESK_GRAPHRAG_ENABLED=false` 可关闭整个可选能力。

官方初始化生成的相对提示词路径按设置文件目录解析。模型设置和提示词内容纳入配置摘要；配置改变后需要构建新快照。设置不会把凭据复制进索引元数据，元数据仅保存摘要。

构建必须实际完成，并产生 `documents`、`text_units`、`entities`、`relationships`、`communities` 和 `community_reports` 六张 Parquet 表。任一工作流返回错误、表缺失或来源映射失败，都不会生成可发布产物。检索前复核这些表和来源清单的 SHA-256。

Local Search 的源块上下文 ID 对应 `text_units.human_readable_id`；Global Search 的报告上下文 ID 对应报告的 **`community` 字段**。社区经 `communities.text_unit_ids`、源块经 `text_units.document_ids` 回指最初的稳定来源 ID。引用未知 ID、候选上下文标志异常或跨快照来源均会失败。返回给客服的是对应原文 `Evidence`，不会把模型生成的社区摘要伪装成原文。

GraphRAG SDK 内部模型调用独立于 TraceDesk 的 `ModelProvider` 代理。其超时受本适配器限制，模型并发、重试及服务额度由官方设置控制；这些 SDK 内部调用不计入研究工作流的共享模型调用预算，因此不作为该工作流的默认检索角色。协议夹具只证明 API 与来源映射契约，不能证明真实模型质量或实际服务权限。

## 主要预算与错误

`GraphConfig.from_env()` 接受 `TRACEDESK_GRAPH_MAX_HOPS`、`MAX_FACTS`、`MAX_NODES`、`MAX_EXPANSIONS`、`MAX_ROWS`、`QUERY_TIMEOUT_SECONDS`、`TEXT2CYPHER_ATTEMPTS`、`MAX_PROMPT_CHARS`（每项均带 `TRACEDESK_GRAPH_` 前缀）。默认关系上限 5,000、节点上限 2,000、查询超时 10 秒、模型输入上限 24,000 字符。模型输入超额会报错，不截断后继续声称完整处理。

Microsoft GraphRAG 的社区层级由 `TRACEDESK_GRAPHRAG_COMMUNITY_LEVEL` 控制，默认 2；单次操作超时由 `TRACEDESK_GRAPHRAG_TIMEOUT_SECONDS` 控制，默认 600 秒。子进程请求体上限为 64 MiB。

| 错误码 | 含义 |
| --- | --- |
| `graph_scope_mismatch` | 来源租户、快照或原文摘要不匹配 |
| `graph_fact_unsupported` | 指定来源未同时出现两端实体 |
| `graph_empty` | 当前快照没有可用关系 |
| `graph_query_budget` | 展开、行数或数据库调用超过预算 |
| `cypher_validation_failed` | 模型查询经有限纠错后仍不合格，未执行 |
| `neo4j_unavailable` | 缺少驱动、连接配置或该快照的投影 |
| `graphrag_unavailable` | 未启用、未配置或未安装官方包 |
| `graphrag_build_failed` | 官方构建工作流失败 |
| `graphrag_provenance_invalid` | 官方上下文不能回指已知原文 |
| `graphrag_config_changed` | 模型设置或提示词已改变，需要新快照 |
