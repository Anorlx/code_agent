# code_agent

_从零开始搭建的本地 Python Coding Agent：按周记录一个 Agent 从最小循环长成 LangGraph、工具系统、上下文管理和长期记忆。_

<p align="center">
  <img src="./assets/readme-hero.svg" alt="code_agent project hero" width="100%">
</p>

<p align="center">
  <a href="https://github.com/Anorlx/code_agent"><img alt="GitHub repo" src="https://img.shields.io/badge/GitHub-Anorlx%2Fcode__agent-181717?logo=github"></a>
  <img alt="Python" src="https://img.shields.io/badge/Python-3.10+-3776AB?logo=python&logoColor=white">
  <img alt="LangGraph" src="https://img.shields.io/badge/LangGraph-StateGraph-2563eb">
  <img alt="DashScope" src="https://img.shields.io/badge/Model-DashScope%20compatible-16a34a">
</p>

## 🧭 这个仓库是什么

`code_agent` 是一个本地 terminal coding agent 的成长记录。它不是一次性写完的完整框架，而是按 `week1/day*`、`week2/day*` 保存每个阶段的代码：先做异步对话循环，再加入工具选择、权限审查、LangGraph 主循环、长期记忆、会话历史、上下文压缩，最后把 terminal 体验和记忆写入逻辑继续打磨。

根目录的 README 是整个仓库的总入口；每个阶段自己的细节说明保留在对应目录里。

## 🗺️ 演进路线

| 阶段 | 核心变化 | 入口 |
| --- | --- | --- |
| Week 1 Day 1 | 最小异步 Agent 循环、流式输出、基础工具目录 | `week1/day1` |
| Week 1 Day 2 | 加入 `run_command` 和 `permission_review`，把命令执行放进审查管线 | `week1/day2` |
| Week 1 Day 3 | 主循环迁移到 LangGraph，并加入 Markdown 长期记忆 | `week1/day3` |
| Week 1 Day 4 | 加入上下文管理、SQLite 会话历史、Sub Agent 短上下文 | `week1/day4` |
| Week 1 Day 5 | 优化 terminal UI：会话选择、状态行、工具事件、帮助命令 | `week1/day5` |
| Week 2 Day 1 | 重做记忆写入：每轮后台观察、互斥写入、TTL 和显著性衰减 | `week2/day1` |

## 🧠 当前主线理解

当前版本可以拆成三条清晰的线：

- **主 Agent 线**：`chat_loop` 收到用户输入，加载相关记忆，然后把任务交给 `run_agent`
- **图执行线**：LangGraph 在 `preprocess -> api_call -> tool_execution -> result_backfill` 之间循环，直到没有工具调用
- **支撑系统线**：工具权限、会话保存、记忆检索/写入、上下文压缩都在主循环外侧支撑长期运行

<p align="center">
  <img src="./assets/current-project-review.svg" alt="code_agent clean architecture map" width="100%">
</p>

## 🔁 一轮对话怎么跑

```mermaid
flowchart TB
    accTitle: code_agent Runtime Path
    accDescr: One user turn flows from terminal input through memory retrieval, LangGraph preprocessing, model streaming, optional tool execution, result backfill, and finally session and memory persistence.

    user_input([📥 用户输入]) --> cli[🖥️ chat_loop]
    cli --> memory_retrieval[💾 读取相关长期记忆]
    memory_retrieval --> run_agent[⚙️ run_agent]

    subgraph graph_loop ["LangGraph 主循环"]
        preprocess[🔧 preprocess<br/>上下文整理 + 工具选择]
        api_call[🧠 api_call<br/>模型流式输出]
        branch{🔍 是否有工具调用?}
        tool_execution[🛡️ tool_execution<br/>工具审查与执行]
        result_backfill[📤 result_backfill<br/>工具结果回填]
        termination[✅ termination_check]

        preprocess --> api_call
        api_call --> branch
        branch -->|有| tool_execution
        tool_execution --> result_backfill
        result_backfill --> preprocess
        branch -->|没有| termination
    end

    run_agent --> preprocess
    termination --> save_session[📝 保存会话]
    save_session --> memory_observer[💾 后台记忆观察]

    classDef input fill:#ede9fe,stroke:#7c3aed,stroke-width:2px,color:#3b0764
    classDef process fill:#dbeafe,stroke:#2563eb,stroke-width:2px,color:#1e3a5f
    classDef memory fill:#fce7f3,stroke:#db2777,stroke-width:2px,color:#831843
    classDef decision fill:#fef3c7,stroke:#d97706,stroke-width:2px,color:#78350f
    classDef success fill:#dcfce7,stroke:#16a34a,stroke-width:2px,color:#14532d

    class user_input,cli input
    class run_agent,preprocess,api_call,tool_execution,result_backfill,save_session process
    class memory_retrieval,memory_observer memory
    class branch decision
    class termination success
```

## ✨ 模块分工

| 模块 | 它解决的问题 | 关键文件 |
| --- | --- | --- |
| Terminal 层 | 用户输入、会话选择、流式事件展示、退出前等待后台任务 | `week2/day1/agent/main_agent/cli.py`, `week2/day1/agent/main_agent/terminal_ui.py` |
| LangGraph 主循环 | 把 Agent 状态拆成可路由节点，避免所有逻辑堆在一个 `while` | `week2/day1/agent/main_agent/graph.py` |
| 工具选择 | 根据当前问题和工具目录，决定本轮暴露哪些工具给主模型 | `week2/day1/agent/sub_agent/tool_search.py`, `week2/day1/agent/tools/README.md` |
| 工具执行 | 构造子 Agent 短上下文、校验 schema、审查权限、并发执行只读工具 | `week2/day1/agent/sub_agent/tool_runner.py`, `week2/day1/agent/sub_agent/permission_review.py` |
| 工具注册表 | 统一管理工具 schema、执行函数、并发安全和审查要求 | `week2/day1/agent/tools/registry.py` |
| 上下文管理 | Snip、MicroCompact、Collapse、AutoCompact，减少旧工具结果占用 | `week2/day1/agent/main_agent/context_manager.py` |
| 长期记忆 | 只保存不可低成本推导的信息，并支持 TTL、score、冲突覆盖 | `week2/day1/agent/memory_system/store.py`, `week2/day1/agent/memory_system/observer.py` |
| 会话历史 | SQLite 保存完整 messages，并用摘要帮助下次选择会话 | `week2/day1/agent/main_agent/session_store.py`, `week2/day1/agent/sub_agent/session_summarizer.py` |
| 模型适配 | DashScope OpenAI-compatible 流式输出，合并 tool call fragments | `week2/day1/agent/main_agent/model_client.py` |

## 🚀 快速开始

```bash
git clone https://github.com/Anorlx/code_agent.git
cd code_agent/week2/day1
export DASHSCOPE_API_KEY="你的 DashScope API Key"
python3 main.py
```

如果你想看某一天的实现，就进入对应目录运行。当前最新阶段是 `week2/day1`。

> 说明：这个仓库按学习过程保存阶段代码。不同 day 的依赖和模型默认值可能不同，建议优先从最新目录开始看。

## 🧪 终端体验

运行后会先选择会话，然后进入 `code_agent>`。一次任务里你可以看到这些事件：

```text
state / tools ls_project,read_project_file
tool_call read_project_file path=agent/main_agent/graph.py
review read_project_file allow risk=low
tool_done read_project_file path=agent/main_agent/graph.py
token dashscope_usage in=... out=... total=...
context micro_compact freed≈...
```

这些事件不是装饰，它们对应真实的运行边界：主图状态、工具调用、权限审查、工具结果、模型 token 用量和上下文清理。

## 📌 设计取向

- **本地优先**：会话、记忆、日志和工具工作区都在项目本地
- **主循环清晰**：LangGraph 只处理状态流转，工具执行和权限判断下沉到子 Agent
- **记忆克制**：只保存无法从代码、文件或 Git 重新推导的信息
- **可观察**：每一步都尽量通过 terminal 事件暴露出来
- **按阶段沉淀**：每个 day 都能单独回看当时的设计选择
