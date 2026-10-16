# 第三方依赖

TraceDesk 的新增代码使用 MIT License。以下第三方项目保留其原有版权与许可证；版本由 Python 约束文件和 `web/package-lock.json` 记录。

| 组件 | 用途 | 许可证 / 来源 |
| --- | --- | --- |
| FastAPI | HTTP API 和 OpenAPI schema | [MIT](https://github.com/fastapi/fastapi/blob/master/LICENSE) |
| Starlette | ASGI 服务基础 | [BSD-3-Clause](https://github.com/encode/starlette/blob/main/LICENSE.md) |
| Pydantic | 工具与请求模型 | [MIT](https://github.com/pydantic/pydantic/blob/main/LICENSE) |
| Uvicorn | ASGI 服务器 | [BSD-3-Clause](https://github.com/encode/uvicorn/blob/main/LICENSE.md) |
| HTTPX | 模型 HTTP 传输 | [BSD-3-Clause](https://github.com/encode/httpx/blob/master/LICENSE.md) |
| Vite | 前端构建，产物含 modulepreload helper | [MIT](https://github.com/vitejs/vite/blob/main/LICENSE) |
| TypeScript | 前端类型检查 | [Apache-2.0](https://github.com/microsoft/TypeScript/blob/main/LICENSE.txt) |
| Playwright | CI 中的浏览器测试 | [Apache-2.0](https://github.com/microsoft/playwright/blob/main/LICENSE) |
| pytest / Ruff | 测试和静态检查 | 各包随附的 MIT License |

应用没有复制其他客服系统的界面资源，也不打包模型权重、GraphRAG 数据、课程教材或用户上传文件。

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
