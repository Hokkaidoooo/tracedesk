# 知识与研究命令

这些命令调用与 HTTP API 相同的文档服务、固定快照检索和持久化研究流程。`serve`、`evaluate`、`probe-model` 的原有用法保持不变；新的知识评测写入独立报告。

## 建立可查询的演示资料库

```powershell
$knowledgeDb = ".\data\knowledge-demo.db"
tracedesk knowledge --database $knowledgeDb load-demo
tracedesk knowledge list --database $knowledgeDb
tracedesk knowledge search "X100 可以搭配哪款扩展坞？" --method graph --database $knowledgeDb
```

`load-demo` 摄入四份自建虚构资料：X100 主机、Y20 扩展坞、Z90 扩展坞与演示商店咨询说明。默认同时发布六条有原文来源的**人工示例关系**，不会把它们标成模型抽取结果。默认配置只使用本地文档与本地图，不需要聊天模型。

`--database` 未指定时沿用 `TRACEDESK_DATABASE`，默认数据库为 `.tracedesk\tracedesk.db`。知识命令的数据库与客户选项可以放在具体动作前或后。`--customer` 默认为 `customer-a`，也可选择 `customer-b`；身份由既有演示会话绑定，命令不会输出会话令牌或模型密钥。

| 命令 | 作用 |
| --- | --- |
| `knowledge list` | 文档版本、快照、当前快照和实际可用方法 |
| `knowledge ingest FILE --title TITLE` | 摄入 TXT、Markdown 或 PDF 原件 |
| `knowledge retry DOCUMENT_ID VERSION` | 使用已保存原件重试指定版本的解析 |
| `knowledge build` | 将当前可发布文档构建为新快照 |
| `knowledge load-demo` | 摄入四份演示资料并发布快照 |
| `knowledge search QUERY` | 返回带稳定来源、版本和页码的真实证据 |
| `research QUERY` | 在已发布快照上执行持久化资料研究 |
| `evaluate-knowledge` | 比较同一快照上的知识检索方法 |

## 摄入与版本

```powershell
tracedesk knowledge ingest .\manual.md --title "设备操作说明" --database $knowledgeDb
tracedesk knowledge build --database $knowledgeDb
tracedesk knowledge search "设备如何连接显示器？" --method lexical --database $knowledgeDb
```

摄入成功只表示文档版本解析完成；`build` 成功后，新版本才进入已发布快照。相同文件更新后会保留版本，历史快照仍可按原来源查询。PDF 使用已配置的解析器；未配置或解析失败时命令非零退出，原件和失败记录保留。

解析重试使用 `knowledge list` 返回的 `document_id` 与 `version`：

```powershell
tracedesk knowledge retry $documentId $documentVersion --database $knowledgeDb
```

重试读取数据库中保存的原件，不会偷偷替换为磁盘上的另一份文件。原件本身无效或解析器仍不可用时，重试仍会明确失败。

## 向量与图构建

```powershell
tracedesk knowledge load-demo --with-embeddings --database $knowledgeDb
tracedesk knowledge search "没有网线插口时如何使用有线网络？" --method semantic --database $knowledgeDb
tracedesk knowledge search "Y20 供电输入有多大？" --method hybrid --database $knowledgeDb
```

`--with-embeddings` 使用实际配置的 ONNX 或兼容嵌入服务；没有可用嵌入模型时会失败。模型配置见[部署说明](deployment.md)。`load-demo --without-graph` 可只构建文档索引；`knowledge build` 只有在显式提供 `--with-graph` 时构建图。

人工关系文件是 `GraphFact` 数组，例如：

```json
[
  {
    "subject": "X100",
    "predicate": "COMPATIBLE_WITH",
    "object": "Y20",
    "source_ids": ["从真实检索结果复制的稳定来源ID"]
  }
]
```

```powershell
tracedesk knowledge build --with-graph --relations .\relations.json --database $knowledgeDb
```

首次制作自有资料的关系文件，可以先发布词面索引，再从 `search` 返回的证据取得稳定 `source_id`。原文版本不变时，该来源 ID 可以用于下一份图快照。图构建仍会检查租户、版本、原文摘要和实体出现位置；不能自行编造 ID。未提供人工关系时，只有配置了真实模型抽取模式才能构建图。完整约束见[图检索说明](graph-retrieval.md)。

## 固定快照查询与研究

`knowledge list` 返回 `current_snapshot.id` 和全部快照。传入 `--snapshot-id` 可查询指定已发布版本：

```powershell
tracedesk knowledge search "Y20 与 Z90 的协议要求有什么不同？" --method lexical --snapshot-id $snapshotId --database $knowledgeDb
tracedesk research "结合 X100 与 Z90 的资料，说明它们能否配合使用。" --mode single --snapshot-id $snapshotId --database $knowledgeDb
tracedesk research "X100 如何连接显示器，选型和售前咨询应注意什么？" --mode multi --snapshot-id $snapshotId --database $knowledgeDb
```

省略快照 ID 时，命令在本次请求开始前解析一次当前快照，随后使用固定 ID。研究所选模式、快照、运行状态和结果进入与网页相同的持久化流程。`single` 为单角色研究，`multi` 为多角色研究；两者都需要真实聊天模型。未配置模型时非零退出，并保留真实失败记录。

成功输出是 JSON，包含实际回答、引用、运行标识和研究元数据。Windows 下输出统一为 UTF-8，命令结束后释放客户端资源。

## 检索消融评测

固定评测使用独立数据库，内容必须是四份原始演示资料。不要向评测快照加入额外文档，也不要将问题文件上传成知识。

```powershell
$evaluationDb = ".\data\knowledge-evaluation.db"
tracedesk knowledge load-demo --database $evaluationDb
tracedesk evaluate-knowledge --database $evaluationDb --output .\reports\knowledge-local.json
```

已经配置真实嵌入模型后，可以比较四种方法：

```powershell
tracedesk knowledge load-demo --with-embeddings --database $evaluationDb
tracedesk evaluate-knowledge --database $evaluationDb --methods lexical,semantic,hybrid,graph --output .\reports\knowledge-four-methods.json
```

默认只选择该快照实际支持的 `lexical`、`semantic`、`hybrid`、`graph`。显式要求未启用的方法会报错，不会悄悄删掉该方法。评测不运行 Text2Cypher、Microsoft GraphRAG 或聊天模型；图方法仅查询该快照已有关系，语义方法真实调用配置的嵌入后端。

问题位于 `data\knowledge-eval.jsonl`，共 28 题：20 题标记为 `heldout`，8 题标记为 `diagnostic`；23 题有答案，5 题无答案。标签包含单来源、多来源、否定、表格、条件、对比和咨询流程。这里的 `heldout` 仅表示问题未进入索引或检索参数调整，不表示独立行业测试集，也不宣称与模型预训练数据隔离。

每题标注必要文档标题和可复核原文片段。评测开始前检查四份原件的 SHA-256、文档标题唯一性和标注片段；多方法共享同一个固定快照。当前发布指针变化不会把后半段问题切到新版本。

| 指标 | 计算方式 |
| --- | --- |
| Recall@3 | 前三个证据块覆盖的必要文档数 / 必要文档总数，再对有答案问题宏平均 |
| Micro Recall@3 | 所有有答案问题的正确文档数之和 / 必要文档数之和 |
| MRR@3 | 首个正确文档对应证据块的排名倒数；前三块未命中为 0 |
| nDCG@3 | 首次命中必要文档获得二元增益，同一文档的重复块不重复加分 |
| 支持片段覆盖率 | 前三个证据块包含的标注片段数 / 必要片段数，再宏平均 |
| 无答案误召回率 | 无答案问题仍返回任意块的比例，不等同于生成答案幻觉率 |

例如，一个问题必须结合 X100 和 Z90 两份说明，前三块即使都来自 X100，Recall@3 仍只有 `1/2`。即使命中了 Y20 的标题，如果返回块没有端口数量对应的表格行，支持片段覆盖率也不能记为完整。

每题保存实际来源、得分、版本、图路径和耗时，并按数据分组及标签汇总。报告还记录问题集、语料、快照、运行时检索选项、嵌入模型和图投影指纹，并区分模型配置与本次选用方法。耗时没有预热，第一次调用可能包含模型或后端初始化；不同机器、配置和冷启动条件下的绝对耗时不宜直接比较。

漏召回、缺少支持片段以及无答案误召回都会保留在 `failures`。不预设哪种检索更好，也不把人工图关系上的成绩当作模型抽图效果。若某方法发生执行故障，其后续问题标为未运行，汇总质量指标为 `null`，其他独立方法继续执行；这份报告状态为 `incomplete`。

`evaluate-knowledge` 默认写入 `reports\knowledge-evaluation.json`；禁止以 `evaluation.json` 为输出文件名，以保护旧版评测产物。正常完成检索测量返回退出码 `0`，即使有真实漏召回；方法执行不完整返回 `1`；配置、资料或输入错误返回 `2`。本评测没有擅自设置“必须满分”的发布门槛。
