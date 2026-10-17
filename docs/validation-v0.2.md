# v0.2 验证与检索结果

本版增加文档版本与快照、MinerU 解析、语义检索、Neo4j、受限 Text2Cypher、Microsoft GraphRAG 适配和 LangGraph 研究流程。下面分别记录实际运行结果、协议验证范围和仍未取得的证据。

## 工程回归

最终本地 Python 3.10 全量回归 **523 项通过**，Ruff 静态与格式检查通过。TypeScript 检查、生产构建和 **13 项浏览器回归**通过；手机卡片宽度修正后，两项窄屏测试及实际渲染复验通过。研究问答的浏览器测试使用明确标记的 HTTP 协议夹具，其余浏览器流程访问实际应用后端。原有 16 条提取式业务工作流也已重新运行通过。

测试覆盖旧数据库迁移、商店和快照隔离、原件留存、失败构建不发布、研究请求幂等、受限图查询、来源错配、预算及取消。ONNX 的 12 项取消边界测试之外，还进行了[真实原生推理复验](validation/onnx-cancellation.json)：取消和超时会发出终止信号并等待计算线程退出，随后请求使用新的运行选项正常生成向量。等待清理可能超过配置的超时点，不承诺响应严格在同一毫秒返回。

[CI](https://github.com/Hokkaidoooo/tracedesk/actions/workflows/ci.yml)包含 Ubuntu／Windows × Python 3.10／3.12、真实 HTTP 浏览器、隔离 Neo4j 和官方 GraphRAG API 兼容性任务。各环境结果以对应运行回执为准；官方包导入及签名检查不等于真实模型效果评测。

## 固定资料上的真实检索

2026 年 10 月 17 日，四份自建虚构资料、18 个原文块和六条人工标注关系组成同一快照。28 道固定问题中，23 道有答案、五道无答案。问题没有进入知识索引，也没有为了此次结果调整检索参数。

语义检索实际运行 `Xenova/bge-small-zh-v1.5` 的量化 ONNX 模型，使用 512 维 CLS 向量；图检索实际访问隔离的 Neo4j 5.26.12。权重固定到 revision `75c43b069aac4d136ba6bc1122f995fedcfd2781`，[模型文件摘要](validation/embedding-model.json)可用于核对。此次没有调用聊天模型。

| 方法 | 文档 Recall@3 | 原文支持覆盖@3 | MRR@3 |
| --- | ---: | ---: | ---: |
| 词面 lexical | 1.0000 | 0.8696 | 0.9710 |
| 语义 semantic | 0.9565 | 0.8478 | 0.8841 |
| 混合 hybrid | 1.0000 | 0.9130 | 0.9493 |
| 图 graph | 0.8043 | 0.5000 | 0.8261 |

文档召回只说明找到了对应文档；原文支持覆盖进一步检查返回块是否包含标注片段。混合方法在这组资料上的支持覆盖较高，图检索没有整体优势。多跳路径可提供额外关系依据，但六条人工关系不能覆盖文档的所有限制、参数和咨询条件。

五道无答案问题在四种方法下均返回了候选块，无答案误召回率均为 `1.0`。候选检索结果不等于最终回答；本次没有测量生成答案的正确率或幻觉率。全部 43 条“方法 × 问题”失败记录及逐题排名、原文、耗时、指纹保存在[完整结果](validation/knowledge-evaluation.json)，包括漏召回、支持片段不足和无答案误召回。

复现数据与指标说明见[知识评测命令](knowledge-cli.md#检索消融评测)。按[部署说明](deployment.md)配置嵌入与 Neo4j 后，使用新的演示数据库运行：

```powershell
tracedesk knowledge load-demo --with-embeddings --database .\data\validation.db
tracedesk evaluate-knowledge --database .\data\validation.db --methods lexical,semantic,hybrid,graph --output .\reports\knowledge-validation.json
```

不同机器的耗时不可直接比较；报告没有预热，首个请求可能包含初始化。此数据集规模小、内容为虚构示例，不能据此宣称真实客户效果或语义检索普遍优于词面检索。

## 真实 PDF 解析

[自建一页 PDF](validation/mineru-sample.pdf)通过官方 MinerU CLI `v0.5.9` 的 `flash-extract` 进入实际 `KnowledgeService.ingest`，随后发布快照、查询表格并取回原件。原件字节和 SHA-256 一致，解析得到五个块，其中一个为 HTML 表格。

12 个表格单元格有 11 个精确匹配。原文 `DP Alt Mode` 被识别为 `DP AIt Mode`，小写字母 `l` 变成了大写 `I`；这条误识别仍保留在[解析回执](validation/mineru-extraction.json)，没有用人工修正掩盖。应用提供原件下载用于核对，解析成功不能证明 OCR 内容全部正确。

`flash-extract` 只返回 Markdown，没有提供页码，因此所有证据的 `page` 保持 `null`。自部署 MinerU 2.x `/file_parse` 的结构化 `content_list` 页码映射、表格和错误处理有协议测试；本次没有运行该 HTTP 服务，不能把 CLI 结果算作结构化页码的完整实测。

## 模型与服务边界

| 能力 | 已取得的证据 | 尚未取得的证据 |
| --- | --- | --- |
| 本地 BGE 语义检索 | 实际权重、指纹和四方法对照 | 真实业务分布的检索效果 |
| Neo4j | 实际 Bolt 连接、投影和多跳查询 | 生产部署的只读角色权限 |
| MinerU CLI | 真实解析、原件回取、表格逐格对照 | 全文 OCR 无误；一处误识别已记录 |
| Text2Cypher | 实际 ONNX 语义示例召回；受限查询、纠错和危险查询拒绝测试 | 配置真实聊天模型后的查询正确率 |
| Microsoft GraphRAG 2.7.0 | 官方包的构建及 Local/Global 接口适配、来源映射和失败测试 | 使用真实聊天与嵌入服务完成官方索引及问答效果评测 |
| LangGraph 单角色／多角色研究 | 实际状态图、预算、依赖、取消、引用和持久化的协议测试 | 真实聊天模型的回答质量及单／多角色效果差异 |

协议测试中的确定性模型响应只用于检查调用和失败路径。真实聊天模型由使用者配置后，先运行 `tracedesk probe-model`，再使用同一固定快照比较研究结果。单角色研究仍包括公共核验步骤，不能理解成一次模型调用。Microsoft GraphRAG 独立检索尚未纳入研究的统一模型调用预算，因此不会作为研究任务的可选检索方法。

Cypher few-shot 的[真实调用回执](validation/cypher-examples.json)记录八个自建示例、三条改写问题和每题前三个语义召回结果。调用由应用装配入口创建，与文档检索复用同一个 ONNX 嵌入器；三条改写分别召回材料要求、反向适用关系和多跳包装要求示例。这只验证真实嵌入与装配，不构成独立测试集上的查询生成准确率。
