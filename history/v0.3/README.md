# real_agent - Week 1 / Day 3

这是我从 0 开始搭的第一个 Python Agent 小项目。今天主要目标是把原来的手写状态循环整理成 LangGraph，同时加入一个长期记忆系统，后面再慢慢往里面加更多工具、技能和 MCP。

## Day 3 做了什么

今天主要做了两件事：

- 把 agent 的主循环重构为 LangGraph，让状态继承和节点路由更清楚。
- 新增长期记忆模块，用 Markdown 文件保存少量高价值记忆。

模型默认值统一放在 `agent/main_agent/config.py`。现在主 agent 和所有 subagent 默认都使用 `qwen3.5-122b-a10b`；后面要换模型只改这个配置文件。

## LangGraph 重构

原来手写的 `while True` 状态循环换成了 LangGraph。现在 `agent/main_agent/graph.py` 里有一个 `build_agent_graph()`，用 `StateGraph` 明确描述每个阶段：

```text
START
  -> preprocess
  -> api_call
  -> termination_check 或 tool_execution
  -> result_backfill
  -> preprocess
  -> END
```

这样做的好处是状态继承关系更清楚：每个节点只负责读写一小段 state，路由函数负责决定下一步走哪里。后面如果要加上下文压缩、memory、MCP、人工确认、更多 subagent，就可以继续加节点，而不是把所有逻辑堆在一个大循环里。

这次还新增了 `tool_runner`。主 agent 触发工具调用后，LangGraph 会进入 `tool_execution` 节点，把完整上下文交给 `tool_runner`。tool runner 可以执行工具、做权限审查、并发运行只读工具，但它不会把自己的内部上下文全部塞回主 agent，只把标准的 `role="tool"` 工具结果回填。

当前版本确认：

- Python: 当前 conda 环境是 Python 3.10
- LangGraph: 当前环境里是 `langgraph==0.6.11`
- 文档参考的是 LangGraph 0.6.x 的 `StateGraph`、`START`、`END`、`add_conditional_edges` 和异步图执行 API
- Python 3.10 下异步节点不适合依赖 `get_stream_writer()`，所以项目里用 `asyncio.Queue` 做事件流式输出，LangGraph 负责状态流转，queue 负责实时 terminal 事件
- DashScope 适配器统一保留在 `agent/main_agent/model_client.py`，不再保留重复的 `dashscope_client.py`
- 当前 LangGraph 依赖链可能打印一条 `allowed_objects` 的 pending deprecation warning，这是上游未来版本提示，不影响现在运行

## Memory 模块

今天还新增了长期记忆系统。记忆不用数据库，而是放在项目根目录的 `memory/` 里，用 Markdown 文件保存：

```text
memory/
├── MEMORY.md
├── user/
├── feedback/
├── project/
└── reference/
```

`MEMORY.md` 只是索引，不放记忆正文。每条具体记忆是一个独立 Markdown 文件，带 YAML frontmatter，例如：

```md
---
name: pre-commit-lint-requirement
title: Pre commit lint requirement
description: Must run lint before commit
type: feedback
---

**Rule**: Run lint before every commit.
```

现在有两个 memory subagent：

- `memory_writer`: 对话结束后后台观察最近对话，判断是否值得长期保存；默认每 3 轮才触发一次，避免每轮都跑。
- `memory_retrieval`: 新一轮对话前读取 `MEMORY.md` 索引，再根据当前用户问题选择少量相关正文。

还有一个 `save_memory` 工具。主 agent 如果明确判断某条信息应该长期保存，可以主动调用它。只要主 agent 本轮已经主动保存，后台 `memory_writer` 就会跳过，避免重复写入。

记忆的原则是“少而精”：只保存无法从代码、文件或 Git 重新推导的信息，比如用户长期偏好、项目长期约束、已经确认的协作方式、外部系统入口。文件结构、API 列表、临时任务、debug 过程和一次性对话都不应该保存。

读取记忆时也不能无脑相信。`MEMORY.md` 只是路由表，具体记忆只是线索，真正使用前还要检查当前项目状态是否仍然成立。

## 异步和流式

今天的一个重点是把 agent 做成异步的，而不是等模型完整回答完才显示。

`run_agent` 仍然是一个异步生成器，会不断 yield 状态事件、模型文本片段、工具调用和工具结果。区别是：原来的循环控制逻辑已经交给 LangGraph，节点内部把事件写入 queue，terminal 从 queue 里实时读取并展示。

模型调用也走流式接口，收到一段内容就马上输出一段内容。工具执行也是 async 的，像项目搜索这种可能耗时的操作会放到线程里跑，避免卡住事件循环。

## 终端状态展示

工具执行时不会再把大段文件内容直接刷到 terminal 里，而是显示工具调用、工具开始、工具完成和状态流转。只读工具可以并发执行，所以可以看到多个 `read_project_file` 同时进入 `(parallel)` 状态。

## 主 Agent 和 Subagent

现在有这些 agent 角色：

- 主 agent: `qwen3.5-122b-a10b`
- 工具选择 subagent: `qwen3.5-122b-a10b`
- 工具执行 subagent: 继承主流程配置
- 权限审查 subagent: `qwen3.5-122b-a10b`
- 记忆提取 subagent: `qwen3.5-122b-a10b`
- 记忆读取 subagent: `qwen3.5-122b-a10b`

`tool_search` 不负责执行工具，只负责判断“这一轮要开放哪些工具给主 agent”。它会读取 `agent/tools/README.md` 里的工具目录摘要，然后返回类似这样的 JSON：

```json
{"tools": ["ls_project", "grep_project"]}
```

主 agent 拿到这些工具名以后，再从 registry 中找到对应的工具 schema，并把这些工具暴露给模型。

`permission_review` 发生在工具执行前。它不会选工具，也不会执行工具，只负责看这次工具调用是否应该放行。主循环收到审查结果后，会 yield 一个 `[tool_review]` 事件，然后才决定是否进入 `[tool_start]`。

`tool_runner` 是真正执行工具的地方。它拿到主 agent 的完整上下文和工具调用列表，然后只把工具结果返回给 LangGraph 的 `result_backfill` 节点。这样主 agent 的上下文不会混入 subagent 的中间推理，只会看到工具输出。

## 当前工具

现在已经有一些基础工具：

- `read_file`: 读取当前项目里的文件
- `write_file`: 写入当前项目里的文件
- `delete_file`: 删除当前项目里的文件
- `list_dir`: 查看当前项目目录
- `ls_project`: 查看当前项目结构
- `grep_project`: 在当前项目里搜索文本
- `read_project_file`: 读取当前项目内的文件
- `calculator`: 简单安全计算器
- `current_time`: 获取当前时间
- `run_command`: 在当前项目内运行本地命令
- `save_memory`: 保存长期记忆到 `memory/`

文件工具现在的权限是当前项目根目录，也就是可以修改这个 agent 项目本身，但仍然不能访问项目目录之外的路径。`agent_write/` 现在只是一个普通的项目子目录，可以继续用来放 agent 生成的临时文件或练习文件。

工具现在带有类别和并发安全标记。只读工具和搜索工具可以并发执行，比如 `read_file`、`ls_project`、`grep_project`、`read_project_file`。会修改文件或产生副作用的工具，比如 `write_file`、`delete_file`、`run_command`，默认顺序执行，避免多个工具同时影响项目。

## 项目结构

```text
.
├── agent/
│   ├── main_agent/
│   │   ├── cli.py
│   │   ├── config.py
│   │   ├── graph.py
│   │   ├── model_client.py
│   │   ├── state.py
│   │   └── terminal_input.py
│   ├── memory_system/
│   ├── sub_agent/
│   └── tools/
├── agent_write/
├── memory/
├── main.py
└── README.md
```

## 如何运行

使用 conda 环境：

```bash
conda activate llamaindex
python main.py
```

或者：

```bash
conda run -n llamaindex python main.py
```

需要设置 DashScope API Key：

```bash
export DASHSCOPE_API_KEY="你的 key"
```

如果需要自定义 DashScope OpenAI-compatible endpoint，可以设置：

```bash
export DASHSCOPE_BASE_URL="https://dashscope.aliyuncs.com/compatible-mode/v1"
```

## 验证

整理代码时会临时跑测试或 smoke check，确认没问题后不把 `tests/` 目录放进项目里。

常用验证命令：

```bash
conda run -n llamaindex python -m compileall agent main.py
conda run -n llamaindex python main.py --help
```

## 今天的收获

今天主要理解了几个点：

- agent 的核心其实是状态循环
- 流式输出需要把模型调用、工具调用和 UI 展示拆成事件
- subagent 可以只做一件很小的事，比如选择工具
- 权限审查也可以拆成 subagent，并且只通过 JSON 和主循环通信
- 工具目录可以用 README 这种轻量摘要来节省 prompt
- 真正的工具 schema 不需要每次都给 subagent，只在主模型需要调用工具时再传
- 本地命令工具要做安全边界，不能直接开放任意 shell 命令
- 长期记忆应该先走索引，再按需读取正文，避免一开始污染上下文

## 下一步可以做什么

后面可以继续加：

- 更好的上下文压缩
- 更完整的 stop hook / tool hook
- MCP 工具接入
- 任务计划和多步骤执行
- 更清晰的 terminal UI
- 把 agent 的状态保存到文件或数据库
