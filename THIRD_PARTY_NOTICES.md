# 第三方依赖

TraceDesk 源码与原创示例资料使用 MIT License。以下主要第三方组件保留其原有版权与许可证；Python 依赖范围见 `pyproject.toml`，约束见 `requirements.lock`，前端版本见 `web/package-lock.json`。完整依赖还应保留各安装包的随附声明。

| 组件 | 用途 | 许可证 / 来源 |
| --- | --- | --- |
| FastAPI | HTTP API 和 OpenAPI schema | [MIT](https://github.com/fastapi/fastapi/blob/master/LICENSE) |
| Starlette | ASGI 服务基础 | [BSD-3-Clause](https://github.com/encode/starlette/blob/main/LICENSE.md) |
| Pydantic | 工具与请求模型 | [MIT](https://github.com/pydantic/pydantic/blob/main/LICENSE) |
| Uvicorn | ASGI 服务器 | [BSD-3-Clause](https://github.com/encode/uvicorn/blob/main/LICENSE.md) |
| HTTPX | 模型 HTTP 传输 | [BSD-3-Clause](https://github.com/encode/httpx/blob/master/LICENSE.md) |
| LangGraph | 研究状态图与编排 | [MIT](https://github.com/langchain-ai/langgraph/blob/main/LICENSE) |
| Neo4j Python Driver | 图投影导入与受限查询 | [Apache-2.0，部分代码为 PSF-2.0](https://github.com/neo4j/neo4j-python-driver/blob/5.28.2/LICENSE.txt) |
| ONNX Runtime | 本地 BGE 推理 | [MIT](https://github.com/microsoft/onnxruntime/blob/main/LICENSE) |
| Hugging Face tokenizers | 本地模型分词 | [Apache-2.0](https://github.com/huggingface/tokenizers/blob/main/LICENSE) |
| NumPy | 张量与数值操作 | [BSD-3-Clause 及随包声明](https://github.com/numpy/numpy/blob/v2.2.6/LICENSE.txt) |
| Microsoft GraphRAG 2.7.0 | 可选官方索引、Local / Global Search | [MIT](https://github.com/microsoft/graphrag/blob/v2.7.0/LICENSE) |
| mineru-open-api | 可选官方 Flash Extract 客户端 | [Apache-2.0](https://github.com/opendatalab/MinerU-Ecosystem/blob/main/LICENSE) |
| MinerU 2.7.6 | 可选 PDF 解析服务 | [AGPL-3.0](https://github.com/opendatalab/MinerU/blob/mineru-2.7.6-released/LICENSE.md) |
| BAAI / Xenova BGE-small-zh-v1.5 | 可选下载的中文嵌入模型与 ONNX 转换 | 原始模型[标注 MIT](https://huggingface.co/BAAI/bge-small-zh-v1.5)；转换与来源见 [Xenova 模型卡](https://huggingface.co/Xenova/bge-small-zh-v1.5/tree/75c43b069aac4d136ba6bc1122f995fedcfd2781) |
| Vite | 前端构建，产物含 modulepreload helper | [MIT](https://github.com/vitejs/vite/blob/main/LICENSE) |
| TypeScript | 前端类型检查 | [Apache-2.0](https://github.com/microsoft/TypeScript/blob/main/LICENSE.txt) |
| Playwright | CI 中的浏览器测试 | [Apache-2.0](https://github.com/microsoft/playwright/blob/main/LICENSE) |
| pytest / Ruff | 测试和静态检查 | 各包随附的 MIT License |

发行包不包含模型权重、MinerU 服务器、用户上传文件或运行生成的 GraphRAG 索引。模型下载脚本固定来源 revision 并保留模型卡；安装和部署外部服务时，应遵循所选组件与模型的许可。

Vite 分发的 modulepreload helper 遵守以下 MIT 条款：

Copyright (c) 2019-present, VoidZero Inc. and Vite contributors

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
