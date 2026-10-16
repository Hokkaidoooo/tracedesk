# TraceDesk

[![CI](https://github.com/Hokkaidoooo/tracedesk/actions/workflows/ci.yml/badge.svg)](https://github.com/Hokkaidoooo/tracedesk/actions/workflows/ci.yml)

带知识来源、工具执行记录和确认流程的客服 Agent。可以查询商店政策和订单，核对退货条件，在用户确认后创建退款申请。

项目同时提供两种明确区分的运行方式：`extractive` 使用中文检索和原文摘录，无需模型密钥；`llm` 使用支持 function calling 的聊天模型选择工具。模型故障会显式报错，不会切换到另一种模式。

所有订单和知识均为自行构造的演示数据。申请写入本地 SQLite 账本，不接入真实支付或商家系统。

## 运行

需要 Python 3.10+。从源码运行还需要 Node.js 22.12+。

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]" -c requirements.lock
cd web
npm ci
npm run build
cd ..
tracedesk serve
```

打开 `http://127.0.0.1:8765`，选择演示用户 A，依次尝试：

1. `签收后多久可以退货？`
2. `查询订单 TD-1001`
3. `这个订单可以退吗？`
4. 核对申请卡片，确认后查看本地申请回执。

`TD-1002` 已超过当前退货时限，`TD-1003` 是数字商品，`TD-1004` 尚未签收；`TD-2001` 属于演示用户 B。它们用于检查规则和订单归属，不是随机生成的成功展示。

## 技术重点

- 中文 BM25、字符 n-gram 与 RRF 消融；检索前按商店和生效日期筛选。政策文本中的天数由同一份结构化规则展开。
- Pydantic 工具参数同时用于模型 JSON Schema 和执行校验。客户身份、订单金额和写操作结果由服务端确定。
- 退款申请采用准备、确认、事务提交；检查有效期、政策版本、订单版本、所属会话和幂等请求。
- 对话、运行事件、申请与回执集中保存在 SQLite。模型返回非法工具、引用错配、预算耗尽和连接错误都保留失败记录。
- 完整 TypeScript 前端，可以展开来源、查看执行步骤、取消申请和查看评测结果。

模型只选择工具、引用和需要补充的信息。最终答复由完整政策原文、已核验订单与申请状态组装；不能让一段模型生成的“操作成功”代替实际回执。

## 可复现结果

60 条固定合成检索用例中，BM25、字符 n-gram、RRF 的 Recall@3 均为 **90.625%**；MRR 分别为 **90.278%、91.667%、90.625%**。因此应用默认采用 n-gram，融合保留为可切换对照。16 个提取式业务工作流回归全部通过。

评测保留未完整召回的用例；这些数字不是线上模型准确率。详见 [原始报告](src/tracedesk/data/evaluation.json) 与 [指标、失败案例和复现条件](docs/evaluation.md)。

工程验证覆盖 Windows / Ubuntu × Python 3.10 / 3.12，每组 **202 项测试**，以及 **7 项 Chromium 浏览器测试**。发行 wheel 在源码目录之外安装并完成真实 HTTP 申请闭环，详见 [验证范围与反例](docs/validation.md)。

## 文档

- [HTTP 接口](docs/api-contract.md)
- [架构与模型边界](docs/architecture.md)
- [安装、模型配置与部署限制](docs/deployment.md)
- [中文检索与消融](docs/retrieval.md)
- [确认与事务](docs/transactions.md)
- [界面与交互](docs/ui.md)
- [来源与贡献范围](docs/provenance.md)

模型负责选择工具和证据，回答由服务端以完整原文、订单字段和动作状态组装。引用检查能验证来源与摘录相符，不能证明来源本身正确或所选知识足以回答问题。固定合成用例的指标不能代替真实用户评价或真实模型验收。

## 来源与许可

本项目由客服学习原型的使用经历出发重新设计和实现；没有复制原包源码、前端产物、教材或上传资料。新增源码和示例数据采用 MIT 许可。第三方依赖继续遵守各自许可证。
