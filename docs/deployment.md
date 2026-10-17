# 安装与配置

## 从源码运行

推荐 Python 3.10 或 3.12、Node.js 22.12+。基础功能、本地 ONNX 和本地图不要求 Docker、WSL、GPU 或额外数据库；Neo4j 与 Microsoft GraphRAG 是独立的可选能力。

```powershell
git clone https://github.com/Hokkaidoooo/tracedesk.git
Set-Location .\tracedesk
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e '.[dev,research,graph,semantic]' -c requirements.lock
Set-Location .\web
npm ci
npm run typecheck
npm run build
Set-Location ..
tracedesk serve
```

Linux/macOS 使用 `source .venv/bin/activate` 激活环境。打开 `http://127.0.0.1:8765`，进入“资料库”加载原创示例，即可使用 4 份文档、6 条有来源的人工关系、词面检索和本地关系路径。此流程不调用语言模型，也不代表模型抽图效果。

依赖按功能安装：`research` 提供 LangGraph，`graph` 提供 Neo4j 驱动，`semantic` 提供 ONNX Runtime、tokenizers 与 NumPy。安装依赖不会自动启用模型、启动 Neo4j 或下载权重。

首次启动初始化 `.tracedesk/tracedesk.db`，示例订单签收时间以首次初始化为基准。重启保留订单、会话、回执、原件与知识快照。需要新的演示数据时使用新文件：

```powershell
tracedesk serve --database .tracedesk\another-demo.db --port 8766
```

schema 1 升级前会核对旧表、约束、完整性与外键，再事务迁移到 schema 2；未知或损坏的结构明确拒绝。升级应先备份数据库，不通过删除或重新填充来恢复旧库。

## 安装已构建的 wheel

wheel 包含前端构建产物与示例资料，运行时不再需要 Node.js。以下以本地构建的 0.2.0 文件为例：

```powershell
python -m pip install '.\dist\tracedesk-0.2.0-py3-none-any.whl[research,graph,semantic]'
tracedesk serve
```

## 环境变量约定

[.env.example](../.env.example) 仅为参考，程序不自动读取 `.env`。通过终端或服务管理器注入变量。示例中的相对路径需要按启动目录解析；ONNX 模型和 MinerU CLI 可执行文件要求实际传入绝对路径，下面使用 `Resolve-Path` 解析。

默认 `TRACEDESK_PROVIDER=extractive`、`TRACEDESK_KNOWLEDGE_EMBEDDING_PROVIDER=none`、`TRACEDESK_KNOWLEDGE_PDF_PARSER=none`。模型或服务失败会返回明确错误，不切换解析器、检索方式或研究模式。

## 本地 BGE 语义检索

下载脚本固定使用 `Xenova/bge-small-zh-v1.5` 的 revision `75c43b069aac4d136ba6bc1122f995fedcfd2781`。量化 ONNX 模型约 24 MB；脚本核验上游文件大小与摘要，生成 `verified-model.json`，不修改应用配置。

```powershell
python scripts\download_embedding_model.py .tracedesk\models\bge-small-zh-v1.5
$env:TRACEDESK_KNOWLEDGE_EMBEDDING_PROVIDER = 'onnx'
$env:TRACEDESK_KNOWLEDGE_EMBEDDING_MODEL = 'Xenova/bge-small-zh-v1.5'
$env:TRACEDESK_KNOWLEDGE_EMBEDDING_DIMENSIONS = '512'
$env:TRACEDESK_KNOWLEDGE_ONNX_MODEL_PATH = (Resolve-Path '.tracedesk\models\bge-small-zh-v1.5\onnx\model_quantized.onnx').Path
$env:TRACEDESK_KNOWLEDGE_TOKENIZER_PATH = (Resolve-Path '.tracedesk\models\bge-small-zh-v1.5\tokenizer.json').Path
$env:TRACEDESK_KNOWLEDGE_TOKENIZER_MAX_LENGTH = '512'
$env:TRACEDESK_KNOWLEDGE_MAX_CHUNK_CHARACTERS = '320'
$env:TRACEDESK_KNOWLEDGE_EMBEDDING_QUERY_PREFIX = '为这个句子生成表示以用于检索相关文章：'
tracedesk serve
```

界面中勾选“语义与混合检索”后构建发布，或运行：

```powershell
tracedesk knowledge load-demo --with-embeddings
tracedesk knowledge search "没有网线插口时如何使用有线网络？" --method semantic
```

该适配器使用 CPU、CLS 池化和 L2 归一化。它只读取已配置的文件，不自动下载或替换模型。320 字符是这里为 512-token 模型选择的分块示例；超出 token 限制会报错，不静默截断。改变分块设置影响之后解析的文档，已经成功解析的来源块保持不变。

改变模型、tokenizer、查询前缀或有关配置会改变指纹，需要新建向量快照。重新索引不会改写历史快照；历史语义检索仍要求匹配原模型配置。

## 兼容 HTTP 嵌入

也可显式选择 OpenAI-compatible `/embeddings` 服务。聊天模型和嵌入模型使用不同配置：

```powershell
$env:TRACEDESK_KNOWLEDGE_EMBEDDING_PROVIDER = 'openai'
$env:TRACEDESK_KNOWLEDGE_EMBEDDING_BASE_URL = 'http://127.0.0.1:8001/v1'
$env:TRACEDESK_KNOWLEDGE_EMBEDDING_MODEL = 'your-embedding-model'
$env:TRACEDESK_KNOWLEDGE_EMBEDDING_DIMENSIONS = '512'
# 按实际模型修改名称、维度，并单独注入 TRACEDESK_KNOWLEDGE_EMBEDDING_API_KEY。
```

基础地址不包含 `/embeddings`。服务必须返回配置的模型名、对应输入顺序的 `index`、正确数量与维度的有限非零向量。模型名与维度不能沿用不匹配的示例值。

## MinerU PDF 解析

两种 PDF 适配互相独立，需要显式选择。TXT / Markdown 始终使用本地 UTF-8 文本解析。

**HTTP 服务。** 适配针对[官方 MinerU 2.7.6 的 `/file_parse` 协议](https://github.com/opendatalab/MinerU/blob/mineru-2.7.6-released/mineru/cli/fast_api.py)。发送 multipart 文件和 `return_content_list=true`，解析响应内序列化的 `content_list`，页号由 `page_idx + 1` 得到。

```powershell
$env:TRACEDESK_KNOWLEDGE_PDF_PARSER = 'mineru_http'
$env:TRACEDESK_KNOWLEDGE_MINERU_BASE_URL = 'http://127.0.0.1:8000'
$env:TRACEDESK_KNOWLEDGE_MINERU_BACKEND = 'pipeline'
$env:TRACEDESK_KNOWLEDGE_MINERU_LANGUAGE = 'ch'
```

该地址不加 `/file_parse` 或 `/v1`。支持 `pipeline`、`vlm-auto-engine`、`hybrid-auto-engine`；相应模型与算力由 MinerU 服务提供。适配器拒绝不符合此契约的响应，不把新版 `/v1/file_parse` 当作兼容接口。

**官方 CLI。** 安装 [mineru-open-api](https://github.com/opendatalab/MinerU-Ecosystem/tree/main/cli/mineru-open-api) 后，指定可直接启动的官方可执行文件。以下相对位置仅为示例，需替换为本机安装位置；Windows 不应填入 PowerShell `.ps1` 启动脚本。

```powershell
$env:TRACEDESK_KNOWLEDGE_PDF_PARSER = 'mineru_cli'
$env:TRACEDESK_KNOWLEDGE_MINERU_CLI_PATH = (Resolve-Path '.\tools\mineru-open-api.exe').Path
```

CLI 调用官方 `flash-extract`，得到 Markdown 文本与表格，不提供可靠分页，因此 `page=null`。该方式会把文件发送到 MinerU 的解析服务；并非本地 PDF 解析。适配器限制文件不超过 10 MiB，上游页数、额度和服务限制仍需满足。需要更大文件或其他格式时，应选择合适的 MinerU 服务，不能通过伪造页码或换解析器掩盖失败。

解析失败保存原件、状态和错误。修正服务配置后，可在资料库点击“重试解析”；无效原件再次解析仍会失败。将 `PDF_PARSER=none` 可关闭 PDF 外部解析。

OCR 可能误识别字符，解析成功不等于关键字段正确；可下载原件逐项回查。实际调用范围和已知失败见[v0.2 验证记录](validation-v0.2.md)。

## 聊天模型与研究

设置 `TRACEDESK_PROVIDER=llm` 后，售后工具选择、模型关系抽取和资料研究使用配置的 chat-completions function-calling 服务：

```powershell
$env:TRACEDESK_PROVIDER = 'llm'
$env:TRACEDESK_BASE_URL = 'https://api.deepseek.com/v1'
$env:TRACEDESK_MODEL = 'deepseek-chat'
# 通过当前进程环境或密钥管理器注入 TRACEDESK_API_KEY。
$env:TRACEDESK_RESEARCH_MODE = 'multi'
tracedesk probe-model
tracedesk serve
```

聊天模型远程地址必须是 HTTPS，只有 loopback 地址允许 HTTP；地址不接受内嵌凭据、查询参数或片段。不要把真实密钥写入仓库或运行记录。需要交互输入时可以使用 PowerShell 的不回显输入：

```powershell
$modelSecret = Read-Host 'API key' -AsSecureString
$modelSecretPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($modelSecret)
try {
    $env:TRACEDESK_API_KEY = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($modelSecretPointer)
} finally {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($modelSecretPointer)
    $modelSecret.Dispose()
}
```

`probe-model` 在隔离演示数据库上发送真实政策咨询，只证明这次模型接通。资料研究需要另外在固定知识快照上使用 `tracedesk research` 或网页研究入口验证。`single` 是单研究者加公共 verifier，`multi` 是分工研究；两者均进行来源与结论核验。API 省略 `mode` 时使用 `TRACEDESK_RESEARCH_MODE`；客户端网络重试应保留首次解析的模式与快照。

| 研究变量 | 默认值 |
| --- | --- |
| `TRACEDESK_RESEARCH_MODE` | `multi` |
| `TRACEDESK_RESEARCH_METHODS` | `lexical,semantic,hybrid,graph,text2cypher` |
| `TRACEDESK_RESEARCH_MAX_MODEL_CALLS` | `16` |
| `TRACEDESK_RESEARCH_MAX_TASKS` | `6` |
| `TRACEDESK_RESEARCH_MAX_PARALLEL` | `3` |
| `TRACEDESK_RESEARCH_MAX_RETRIEVAL_CALLS` | `8` |
| `TRACEDESK_RESEARCH_MAX_CONTEXT_CHARS` | `240000`，累计模型输入字符 |
| `TRACEDESK_RESEARCH_TIMEOUT_SECONDS` | `120`，整个研究流程 |

研究方法会与已发布快照的实际可用能力取交集。Microsoft GraphRAG 内部模型调用未纳入该共享预算，因此不能加入 `RESEARCH_METHODS`，应通过独立检索入口调用。

## 图与 Neo4j

默认 `TRACEDESK_GRAPH_BACKEND=local`、`TRACEDESK_GRAPH_EXTRACTION=explicit`。原创示例提供人工来源关系，自有文档可通过 API / CLI 提供关系文件；若要自动抽取，先配置真实聊天模型，再设 `TRACEDESK_GRAPH_EXTRACTION=model`。

Neo4j 驱动与数据库服务分别安装。启用时需设置 `TRACEDESK_GRAPH_BACKEND=neo4j`、`TRACEDESK_NEO4J_URI`、`DATABASE`、`READER_USER`、`READER_PASSWORD`、`WRITER_USER`、`WRITER_PASSWORD`，后六项均使用 `TRACEDESK_NEO4J_` 前缀。

读取、导入身份应在数据库侧具有相应权限；驱动的 `READ` 路由不等于只读授权。Text2Cypher 要求可用的 Neo4j 投影、聊天模型和资料库嵌入模型，允许语法、跳数与返回行数均有明确上限。

语义 few-shot 不新增环境变量，直接复用 `TRACEDESK_KNOWLEDGE_EMBEDDING_*`、本地模型路径、批量与超时设置。首次使用时嵌入随包的 8 对自建合成问题 / 只读 Cypher，按向量相似度选取最多 3 个；缓存绑定当前模型指纹。未配置嵌入时明确不可用，不改成零样本提示。示例不是知识事实，原文依据仍来自当前资料快照。格式与验证规则见[图检索说明](graph-retrieval.md)。

## Microsoft GraphRAG 2.7.0

适配只接受官方 `graphrag==2.7.0`，其 Python 范围为 3.10–3.12。建议为包含该组件的服务建立专用虚拟环境，并确认目标平台能安装其完整依赖；无需为基础功能启用这一组件。

在实际运行 TraceDesk 的同一个 Python 环境中安装并初始化：

```powershell
python -m pip install -e '.[research,graph,semantic,graphrag]'
graphrag init --root .tracedesk\graphrag-config
```

按官方设置配置真实聊天、嵌入服务与提示词，随后显式启用：

```powershell
$env:TRACEDESK_GRAPHRAG_ENABLED = 'true'
$env:TRACEDESK_GRAPHRAG_SETTINGS = (Resolve-Path '.tracedesk\graphrag-config\settings.yaml').Path
$env:TRACEDESK_GRAPHRAG_ROOT = '.tracedesk\graphrag'
$env:TRACEDESK_KNOWLEDGE_BUILD_TIMEOUT_SECONDS = '1200'
$env:TRACEDESK_GRAPHRAG_TIMEOUT_SECONDS = '600'
```

仅生成配置文件不表示索引可用。必须使用带图构建的资料快照，实际完成官方索引与来源映射后，Local / Global Search 才可读取。模型配置和提示词变化要求新快照；错误不会改用本地图。官方包调用运行在受时限控制的独立子进程中，仍使用当前服务的 Python 解释器。

将 `TRACEDESK_GRAPHRAG_ENABLED=false` 可关闭此可选能力。完整平台、目录、来源表和 SDK 预算边界见[专门说明](graph-retrieval.md#microsoft-graphrag-270)。

## 主要配置

知识配置均使用 `TRACEDESK_KNOWLEDGE_` 前缀：

| 后缀 | 默认值 / 用途 |
| --- | --- |
| `PDF_PARSER` | `none`；可选 `mineru_http / mineru_cli` |
| `MINERU_BASE_URL / MINERU_CLI_PATH` | 空；所选解析器的地址或可执行文件 |
| `MINERU_BACKEND / MINERU_LANGUAGE` | `pipeline / ch` |
| `EMBEDDING_PROVIDER` | `none`；可选 `onnx / openai` |
| `EMBEDDING_MODEL / EMBEDDING_API_KEY` | 空，需显式设置模型与所需凭据 |
| `EMBEDDING_BASE_URL` | 空，HTTP 嵌入基础地址 |
| `EMBEDDING_DIMENSIONS / EMBEDDING_BATCH_SIZE` | `0 / 32`；启用嵌入后维度必须为正 |
| `ONNX_MODEL_PATH / TOKENIZER_PATH` | 空，已有本地模型与 tokenizer 文件 |
| `TOKENIZER_MAX_LENGTH / EMBEDDING_QUERY_PREFIX` | `512 / 空` |
| `MAX_UPLOAD_BYTES / MAX_CHUNK_CHARACTERS` | `20971520 / 1200` |
| `PARSE_TIMEOUT_SECONDS / EMBEDDING_TIMEOUT_SECONDS` | `120 / 60` |
| `BUILD_TIMEOUT_SECONDS` | `300`，整个快照构建时限 |
| `MAX_QUERY_CHARACTERS / MAX_SEARCH_RESULTS` | `2000 / 50`；HTTP 搜索另有最多 20 条的上限 |
| `RRF_K` | `60` |

售后与应用配置使用 `TRACEDESK_` 前缀：

| 后缀 | 默认值 / 用途 |
| --- | --- |
| `DATABASE / DEMO_MODE` | `.tracedesk/tracedesk.db / true` |
| `PROVIDER / RETRIEVAL_METHOD` | `extractive / ngram`，后者仅控制正式政策检索 |
| `BASE_URL / MODEL / API_KEY` | `https://api.deepseek.com/v1 / deepseek-chat / 空` |
| `TIMEOUT_SECONDS / MAX_MODEL_STEPS` | `30 / 6` |
| `MAX_TOOL_CALLS / MAX_COMPLETION_TOKENS` | `12 / 1800` |
| `MAX_TOOL_OUTPUT_CHARS / MAX_MESSAGE_CHARS` | `16000 / 2000` |
| `MAX_HISTORY_TURNS` | `6` |
| `ACTION_TTL_SECONDS / RUN_LEASE_SECONDS` | `600 / 300` |

运行租约必须大于单步模型超时乘以最大步数，研究流程总时限也必须短于运行租约。调整可配置预算会改变对应上限；商店归属、不可变来源与确认事务不提供绕过开关。

## 检查与运行边界

```powershell
pytest -q
ruff check src tests
python -m build
tracedesk evaluate --output reports\policy-evaluation.json
```

前端验证入口为 `web` 目录下的 `npm run typecheck` 和 `npm run test:e2e`；后者需要可用的浏览器。资料检索消融使用独立评测数据库，操作见[知识 CLI](knowledge-cli.md#检索消融评测)。启动服务或安装依赖不等于对应真实调用已验收。

API schema 位于 `/openapi.json`。默认只监听本机，不开放跨域访问；对外监听须显式指定 `--allow-public-demo --host 0.0.0.0`，仍然是演示身份系统。真实业务使用需要另行完成身份、备份、TLS、费用与速率限制及业务系统适配。

远程 MinerU、嵌入和聊天配置会发送相应文件或文本；本地 ONNX 与默认文本解析在本机处理。请按资料的实际使用范围选择服务端点。
