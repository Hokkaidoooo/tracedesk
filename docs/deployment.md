# 安装与配置

## 从源码运行

需要 Python 3.10+、Node.js 22.12+。Windows 不需要 Docker 或 WSL，也不需要 MySQL、Redis、Neo4j 或 GPU。

```powershell
git clone https://github.com/Hokkaidoooo/tracedesk.git
cd tracedesk
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]" -c requirements.lock
cd web
npm ci
npm run typecheck
npm run build
cd ..
tracedesk serve
```

Linux/macOS 激活命令为 `source .venv/bin/activate`，其余步骤相同。打开 `http://127.0.0.1:8765`。

服务第一次启动会初始化 `.tracedesk/tracedesk.db`，示例订单的签收时间以首次初始化时间为基准。重启不会覆盖订单、申请、回执或历史会话。需要一套全新演示数据时，显式指定新的数据库文件：

```powershell
tracedesk serve --database .tracedesk\another-demo.db --port 8766
```

不要用删除数据库作为升级或异常恢复方法。生产身份、实际业务数据迁移与支付适配不在此版本范围内。

## 使用 wheel

Release 中的 wheel 包含前端构建产物和示例知识。安装后不再需要 Node.js：

```powershell
pip install .\tracedesk-0.1.0-py3-none-any.whl
tracedesk serve
```

## 接入真实模型

默认 `extractive` 不发送模型请求。`.env.example` 是环境变量说明，程序不会隐式读取当前目录的 `.env` 文件。通过终端、服务管理器或部署平台注入变量。

以支持 function calling 的 chat-completions 服务为例，PowerShell 可设置：

```powershell
$env:TRACEDESK_PROVIDER = 'llm'
$env:TRACEDESK_BASE_URL = 'https://api.deepseek.com/v1'
$env:TRACEDESK_MODEL = 'deepseek-chat'
# TRACEDESK_API_KEY 由你在本机安全注入；不要写进仓库、命令记录或聊天。
tracedesk probe-model
tracedesk serve
```

若部署平台没有密钥管理器，可在 PowerShell 中用不回显输入设置当前进程的变量：

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

本地兼容服务可把地址设为 `http://127.0.0.1:端口/v1`；只有 loopback HTTP 被允许，远程地址必须使用 HTTPS。地址不能嵌入凭据或查询参数。模型必须支持工具调用；不支持或返回无效协议会明确失败，不会退回另一服务或另一运行模式。

`probe-model` 在隔离的临时演示数据库上真实发送一次政策咨询，输出回答、引用和执行记录，结束后清理临时数据库。它证明一次接通，不等于全面模型评测。该版本的固定评测报告没有真实模型成绩。

## 配置与约束

| 环境变量 | 默认值 | 用途 |
| --- | --- | --- |
| `TRACEDESK_PROVIDER` | `extractive` | `extractive` 或 `llm`，明确切换运行模式 |
| `TRACEDESK_RETRIEVAL_METHOD` | `ngram` | `bm25`、`ngram`、`hybrid` |
| `TRACEDESK_DATABASE` | `.tracedesk/tracedesk.db` | 持久化数据库位置 |
| `TRACEDESK_BASE_URL` | `https://api.deepseek.com/v1` | 模型 chat-completions 基础地址 |
| `TRACEDESK_MODEL` | `deepseek-chat` | 模型名称 |
| `TRACEDESK_API_KEY` | 空 | 远程模型必须提供；不记录到运行 trace |
| `TRACEDESK_TIMEOUT_SECONDS` | `30` | 单次请求总时限与连接空闲时限 |
| `TRACEDESK_MAX_MODEL_STEPS` | `6` | 每轮最大模型请求数 |
| `TRACEDESK_MAX_TOOL_CALLS` | `12` | 每轮工具调用总数 |
| `TRACEDESK_MAX_COMPLETION_TOKENS` | `1800` | 发送给服务商的完成 token 上限 |
| `TRACEDESK_MAX_TOOL_OUTPUT_CHARS` | `16000` | 工具数据输出字符预算 |
| `TRACEDESK_MAX_MESSAGE_CHARS` | `2000` | 用户消息上限，HTTP schema 另有 2000 字符硬上限 |
| `TRACEDESK_MAX_HISTORY_TURNS` | `6` | 发送给模型的历史轮数 |
| `TRACEDESK_ACTION_TTL_SECONDS` | `600` | 待确认申请有效期 |
| `TRACEDESK_RUN_LEASE_SECONDS` | `300` | 孤儿运行租约；必须大于超时乘以最大步数 |
| `TRACEDESK_DEMO_MODE` | `true` | 演示声明；关闭会显式报错，因为没有生产身份适配 |

改变预算或有效期会改变相应边界；参数必须为正且时限有限。身份归属、唯一申请和事务约束是接口不变量，不提供绕过开关。

服务默认只监听本机。显式使用 `--allow-public-demo --host 0.0.0.0` 才能对外提供演示身份接口；这不把项目变为生产系统。真实部署还需要外部身份认证、TLS、速率与费用限制、数据库备份和业务系统适配。

## 检查与评测

```powershell
pytest -q
ruff check src tests
ruff format --check src tests
python -m build
tracedesk evaluate --output reports\evaluation.json
```

评测使用临时 SQLite 和固定业务时间，不修改当前服务数据库。若要更新页面展示的随包报告，在源码根目录运行：

```powershell
tracedesk evaluate --output src\tracedesk\data\evaluation.json
```

接口 schema 位于 `/openapi.json`，详细请求见 [HTTP 接口](api-contract.md)。页面不加载外部 CDN，默认不开放跨域调用。
