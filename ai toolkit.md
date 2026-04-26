# AI Toolkit 初次接触

> 本文基于 [AI Toolkit for Visual Studio Code](https://github.com/microsoft/vscode-ai-toolkit) 官方仓库、[Foundry Toolkit 官方文档](https://code.visualstudio.com/docs/intelligentapps/overview) 以及 [Microsoft Agent Framework](https://github.com/microsoft/agent-framework) 整理，旨在从业务视角描述它能做什么、怎么用。

---

## 一、它到底是什么

AI Toolkit（现已更名为 **Foundry Toolkit for Visual Studio Code**）是微软推出的一个 [VS Code 扩展](https://code.visualstudio.com/docs/intelligentapps/overview)，目标很直白：**让开发者在 VS Code 里就能完成 AI 应用的全生命周期**——从选模型、调提示词、给 Agent 接工具，到部署上云，全部一站式搞定。

它内置了 [Microsoft Foundry](https://github.com/microsoft/microsoft-foundry-for-vscode) 扩展，可以直接管理 Azure 上的 AI 资源（模型部署、Agent 管理等），不需要离开编辑器。

---

## 二、业务上它解决什么问题

| 业务痛点 | AI Toolkit 怎么解决 |
|---|---|
| **选模型难** — 市面上模型太多，不知道哪个适合我的场景 | 提供 [Model Catalog](https://code.visualstudio.com/docs/intelligentapps/models)，汇聚 Microsoft Foundry、GitHub、OpenAI、Anthropic、Google、Ollama、ONNX 等多家模型，可以在 Playground 里直接对比测试 |
| **Agent 开发门槛高** — 要自己搭框架、写编排逻辑 | 提供两条路径：无代码的 [Agent Builder](https://code.visualstudio.com/docs/intelligentapps/agentbuilder)（拖拽式）和代码级的 [Agent Framework SDK](https://github.com/microsoft/agent-framework)（Python / C#），覆盖从零基础到专业开发者 |
| **调试困难** — Agent 行为不透明，出了问题很难排查 | 内置 [Agent Inspector](https://code.visualstudio.com/docs/intelligentapps/agent-inspector)，按 F5 即可断点调试，实时查看 Agent 的流式响应和多 Agent 工作流执行路径 |
| **工具集成麻烦** — 想让 Agent 调 API、查数据库 | 原生支持 [MCP（Model Context Protocol）](https://code.visualstudio.com/docs/intelligentapps/agentbuilder#_use-mcp-servers) 服务器，可以一键接入现成的 MCP Server，也可以从脚手架新建自定义 MCP Server |
| **上线环节割裂** — 本地调完还得另外搞部署 | 直接从 VS Code 命令面板执行 `Deploy to Microsoft Foundry`，将 Hosted Agent 部署到云端 |

---

## 三、两种 Agent 开发方式

这是 AI Toolkit 在 Agent 开发上最核心的设计，提供了**无代码**和**代码级**两条并行路径，按照 [官方文档的说法](https://github.com/microsoft/vscode-ai-toolkit#-build-ai-agents)：

### 3.1 无代码路径：Agent Builder（Prompt Agent）

适合**业务人员或快速原型**场景。

操作流程：
1. 打开 Foundry Toolkit 侧栏 → Developer Tools → Create Agent → Open Agent Builder
2. 选一个模型（如 GPT-4o）
3. 在 Instructions 框里写清楚你要 Agent 做什么——比如"你是一个客服助手，只回答退换货相关问题"
4. 在下方聊天框里输入测试消息，实时查看 Agent 的回答
5. 反复调整 Instructions 直到满意
6. 可选：给 Agent 挂 MCP Server 工具（如文件系统访问、数据库查询）
7. 点击 **View Code** 一键导出 Python 代码或完整项目

关键亮点（均来自 [Agent Builder 文档](https://code.visualstudio.com/docs/intelligentapps/agentbuilder)）：
- **Inspire Me** 功能可以帮你自动生成初始提示词
- 支持 **结构化输出**（Structured Output），让 Agent 按 JSON Schema 返回结果
- 支持 **Function Calling**（函数调用），用自定义 JSON Schema 定义工具
- 每次保存自动生成版本，可随时回退到历史版本
- 可查看历史对话记录（Conversations 标签页），方便诊断问题

### 3.2 代码级路径：Hosted Agent（Agent Framework SDK）

适合**需要多 Agent 编排、复杂业务逻辑、生产级部署**的场景。

底层使用的是 [Microsoft Agent Framework](https://github.com/microsoft/agent-framework)（前身为 Semantic Kernel + AutoGen 的合并演进），支持 Python 和 C#/.NET 双语言。

一个最简单的 Python Agent 长这样（来自 [官方 README](https://github.com/microsoft/agent-framework#basic-agent---python)）：

```python
# pip install agent-framework
# 用 `az login` 完成 Azure 认证
import asyncio
from agent_framework import Agent
from agent_framework.foundry import FoundryChatClient
from azure.identity import AzureCliCredential

async def main():
    agent = Agent(
        client=FoundryChatClient(credential=AzureCliCredential()),
        name="HaikuBot",
        instructions="You are an upbeat assistant that writes beautifully.",
    )
    print(await agent.run("写一首关于春天的俳句"))

if __name__ == "__main__":
    asyncio.run(main())
```

Agent Framework 的核心能力（来自 [官方 Highlights](https://github.com/microsoft/agent-framework#-highlights)）：

| 能力 | 说明 |
|---|---|
| **Graph-based Workflows** | 用图编排多个 Agent 和确定性函数，支持流式传输、检查点、Human-in-the-loop、时间回溯 |
| **多 Agent 支持** | 多个 Agent 协作完成复杂任务，各自分工 |
| **Middleware 中间件** | 类似 Web 框架的中间件管道，灵活拦截请求/响应 |
| **多 LLM Provider** | 支持 Azure OpenAI、OpenAI、Anthropic 等多家提供商 |
| **OpenTelemetry 可观测性** | 内置分布式追踪，接入 Tracing/Monitoring 系统 |
| **DevUI** | 交互式开发界面，用于 Agent 测试和调试工作流 |

---

## 四、典型业务场景示例

### 场景 A：智能客服 Bot（无代码，30 分钟上线原型）

1. 在 Agent Builder 中选择 GPT-4o 模型
2. Instructions 里写：`你是 XX 公司的客服助手，负责回答退换货政策相关问题。如果用户问的不是退换货问题，礼貌地引导回主题。`
3. 挂一个 MCP Server 连接公司知识库（比如 Filesystem MCP Server 指向文档目录）
4. 在 Playground 里测试不同用户问题
5. View Code 导出 Python 代码，集成到公司现有 Web 应用

### 场景 B：多 Agent 工单处理系统（代码级）

```
用户提交工单
    → Agent 1（分类器）：判断是技术问题 / 账单问题 / 一般咨询
    → Agent 2（技术专家）：查知识库，给出解决方案
    → Agent 3（审核员）：检查回复的准确性和合规性
    → 返回最终答复
```

这种场景用 Agent Framework 的 Graph-based Workflow 来编排，每个 Agent 独立定义自己的指令和工具，通过数据流连接。

### 场景 C：本地隐私优先的文档分析

- 使用 ONNX 或 Ollama 跑本地模型，数据不出设备
- 在 Playground 里测试效果
- 确认后导出代码集成到业务系统

---

## 五、开发环境准备

根据 [官方安装指南](https://code.visualstudio.com/docs/intelligentapps/overview#_install-and-setup)：

1. **安装 VS Code** 并从扩展市场搜索 `Foundry Toolkit for Visual Studio Code` 安装
2. **登录 Azure**（如果要用云端模型）：终端执行 `az login`
3. **创建 Foundry Project**（如果还没有）：侧栏 → My Resources → Microsoft Foundry Resources → Create Project
4. **Python 环境**（如果走代码路径）：建议用 `uv` 或 `venv` 创建虚拟环境

安装完成后，VS Code 左侧 Activity Bar 会出现 Foundry Toolkit 图标，侧栏分三大区域：
- **My Resources**：管理本地和云端 AI 资源（模型、Agent、工具、向量存储）
- **Developer Tools**：Model Catalog、Agent Builder、Agent Inspector、Playground、微调、模型转换等
- **Help and Feedback**：文档和社区链接

---

## 六、从 Agent Builder 到生产代码的完整链路

```
Agent Builder 设计提示词 & 测试
        ↓
    效果满意？
        ↓ 是
View Code 导出 Python/C# 代码
        ↓
本地 F5 启动 + Agent Inspector 调试
        ↓
    调试通过？
        ↓ 是
命令面板 → Deploy to Microsoft Foundry
        ↓
  云端 Hosted Agent 上线
        ↓
Monitor → Tracing & Evaluation 持续监控
```

这是 [官方文档](https://code.visualstudio.com/docs/intelligentapps/agentbuilder#_code-snippets-and-code-projects) 描述的推荐工作流。Agent Builder 生成的代码使用 [Agent Framework SDK](https://github.com/microsoft/agent-framework) 或模型提供商自己的 SDK（如 OpenAI SDK），可以直接用在生产环境。

---

## 七、与竞品的定位差异

| 维度 | AI Toolkit (Foundry Toolkit) | LangChain / LangGraph | OpenAI Assistants API |
|---|---|---|---|
| 形态 | VS Code 扩展 + SDK | Python 库 | 云 API |
| 无代码能力 | 有（Agent Builder） | 无 | 无 |
| 可视化调试 | 有（Agent Inspector，F5 断点） | 需借助 LangSmith | 无 |
| 模型支持 | 多家（Foundry、GitHub、OpenAI、Anthropic、Google、本地模型） | 多家 | 仅 OpenAI |
| 部署目标 | Microsoft Foundry（Azure） | 自行部署 | OpenAI 云 |
| MCP 支持 | 原生内置 | 社区插件 | 无 |

> ⚠️ 以上对比基于各产品公开文档的功能描述，各产品迭代速度快，具体能力请以最新文档为准。

---

## 八、值得关注的限制

- **Azure 依赖**：云端部署目标是 Microsoft Foundry（Azure），如果企业不用 Azure 需自行适配
- **扩展较新**：AI Toolkit 近期刚更名为 Foundry Toolkit，部分功能标注 Preview，API 可能有变动
- **Agent Framework 迭代快**：项目前身涉及 Semantic Kernel 和 AutoGen，提供了 [从 Semantic Kernel 迁移](https://learn.microsoft.com/en-us/agent-framework/migration-guide/from-semantic-kernel) 和 [从 AutoGen 迁移](https://learn.microsoft.com/en-us/agent-framework/migration-guide/from-autogen) 的官方指南

---

## 九、官方资源汇总

| 资源 | 链接 |
|---|---|
| AI Toolkit GitHub 仓库 | https://github.com/microsoft/vscode-ai-toolkit |
| Foundry Toolkit 官方文档 | https://code.visualstudio.com/docs/intelligentapps/overview |
| Agent Builder 文档 | https://code.visualstudio.com/docs/intelligentapps/agentbuilder |
| Microsoft Agent Framework | https://github.com/microsoft/agent-framework |
| Agent Framework 文档（MS Learn） | https://learn.microsoft.com/agent-framework/overview/agent-framework-overview |
| Agent Framework Quick Start | https://learn.microsoft.com/agent-framework/tutorials/quick-start |
| Discord 社区 | https://discord.gg/b5zjErwbQM |
