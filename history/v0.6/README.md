# code_agent - Week 2 / Day 1

Week 2 Day 1 主要重做了记忆写入逻辑。旧版本是“每隔几轮抽取一次”，现在改成每轮对话结束后都让后台 memory subagent 做候选提取。

## Week 2 Day 1 做了什么

- `MemoryObserver` 去掉固定 interval，不再每 3 轮才运行。
- 每轮对话结束都会后台启动一次记忆候选提取。
- 同一轮记忆写入互斥：如果主 Agent 已经用 `save_memory` 主动写过记忆，后台 forked memory agent 会跳过本轮。
- Ctrl+C / exit 退出前会 flush 当前 history，并等待后台记忆任务完成。
- 记忆写入任务加了串行锁，避免多个后台任务同时写 `memory/` 和 `MEMORY.md`。
- `memory_writer` 提示词改成 10 步流水线：候选提取、长期价值判断、可推导性过滤、置信度判断、类型分类、时间稳定性检查、去重/冲突、安全/作用域、结构化生成、写入索引。
- 新增 `scope` 和 `confidence` 字段，写入 Markdown frontmatter。
- `write_memory` 增加敏感信息过滤，避免把 token/password/private key 这类内容持久化。
- 新增长期记忆遗忘策略：TTL、使用频率、显著性衰减、用户显式删除、冲突覆盖。
- 新增 `delete_memory` 和 `prune_memories` 工具。

新的原则是：

```text
Only save non-derivable knowledge.
```

也就是说，只保存那些不能从代码、文件、Git、grep、package.json、数据库 schema 低成本重新推导的信息。

记忆 subagent 现在输出的结构类似：

```json
{
  "memories": [
    {
      "type": "feedback",
      "scope": "project-local",
      "title": "提交前运行 lint",
      "description": "提交前必须运行 lint",
      "content": "**Rule**: 提交前运行 lint。\\n\\n**Why**: 避免未检查代码进入提交。\\n\\n**How to apply**: 每次 commit 前先跑 lint。",
      "confidence": "high",
      "replace_path": ""
    }
  ]
}
```

如果新记忆和旧记忆冲突，subagent 可以输出 `replace_path`，系统会删除旧记忆并写入新记忆。

### 长期记忆遗忘

记忆文件现在不只是静态 Markdown，而是带生命周期元数据：

```yaml
created_at: 1760000000.0
updated_at: 1760000000.0
last_used_at:
use_count: 0
salience: 1.000
ttl_days: 365
expires_at: 1791536000.0
status: active
```

遗忘策略包括：

- TTL：`project` 默认 365 天，`reference` 默认 180 天；`user` 和 `feedback` 默认不过期，除非显式传入 `ttl_days`。
- 使用频率：被 `memory_retrieval` 读到的记忆会增加 `use_count`，更新 `last_used_at`，并略微提升 `salience`。
- 显著性衰减：长期不用、显著性很低、且没有使用记录的记忆会被清理。
- 用户显式删除：`delete_memory` 可以删除指定记忆文件。
- 冲突覆盖：`save_memory` / `memory_writer` 可以通过 `replace_path` 删除旧记忆并写入新记忆。

`MEMORY.md` 会显示每条记忆的当前 score，方便判断哪些记忆仍然重要。

---

这是我从 0 开始搭的第一个 Python Agent 小项目。Day 5 是 Week 1 的最后一天，今天主要不动后端能力，而是优化 terminal 里的对话体验。

## Day 5 做了什么

今天主要做了前端展示层优化：

- 新增 `agent/main_agent/terminal_ui.py`，专门负责 terminal 面板、状态行、工具事件和帮助信息。
- 启动时显示更干净的欢迎面板，包含 workspace、session、input mode、工具数量和 max turns。
- 会话选择界面从普通 `print` 列表改成面板式列表，更容易看出每个会话的摘要。
- 对话 prompt 改成 `code_agent>`，会话选择 prompt 改成 `session>`。
- 事件展示从日志感很强的 `[state] ...` 改成更紧凑的 `state / tool_call / tool_start / tool_done / terminal` 行。
- 保留流式输出，assistant 内容仍然是边生成边显示。
- 新增纯前端命令：`/help`、`/session`、`/clear`。

这次参考了 pico 的 terminal 风格：固定宽度面板、少量元信息、清晰命令、少噪音输出。但没有引入额外 UI 框架，也没有改 agent 后端图结构。

启动后的感觉大概是：

```text
+============================================================================+
|                                  code_agent                                  |
|                            local terminal agent                             |
|                           quiet shell, streaming work                       |
+----------------------------------------------------------------------------+
| WORKSPACE  /Users/aoligei/Desktop/real_agent                                |
| SESSION    新会话 (abc123)                 INPUT     prompt_toolkit          |
| TOOLS      12                              MAXTURN   10                      |
| Commands: /help  /session  /clear  exit                                    |
+============================================================================+

code_agent>
```

## Day 4 做了什么

今天主要做了三件事：

- 新增 `context_manager`，实现四级上下文管理：Snip、MicroCompact、Collapse、AutoCompact。
- 新增 `snip_context` 工具，让主 agent 可以主动清理旧工具结果。
- 把上下文管理接入 LangGraph 的 `preprocess` 阶段，请求模型前先判断是否需要释放空间。
- 新增 SQLite 会话历史。每次启动先选择历史会话或新建会话。
- 新增 `session_summarizer`，为每个会话生成很短的标题和摘要，方便下次启动时选择。
- 新增 sub agent 上下文切片：Main 保存全局工作记忆，Sub 只拿当前任务需要的局部工作集。

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

这次还新增了 `tool_runner`。主 agent 触发工具调用后，LangGraph 会进入 `tool_execution` 节点，把工具调用交给 `tool_runner`。tool runner 可以执行工具、做权限审查、并发运行只读工具，但它不会把自己的内部上下文全部塞回主 agent，只把标准的 `role="tool"` 工具结果回填。

当前版本确认：

- Python: 当前 conda 环境是 Python 3.10
- LangGraph: 当前环境里是 `langgraph==0.6.11`
- 文档参考的是 LangGraph 0.6.x 的 `StateGraph`、`START`、`END`、`add_conditional_edges` 和异步图执行 API
- Python 3.10 下异步节点不适合依赖 `get_stream_writer()`，所以项目里用 `asyncio.Queue` 做事件流式输出，LangGraph 负责状态流转，queue 负责实时 terminal 事件
- DashScope 适配器统一保留在 `agent/main_agent/model_client.py`，不再保留重复的 `dashscope_client.py`
- 当前 LangGraph 依赖链可能打印一条 `allowed_objects` 的 pending deprecation warning，这是上游未来版本提示，不影响现在运行

## Context 管理

Day 4 新增的上下文管理代码在 `agent/main_agent/context_manager.py`。它不是一个单独的 agent，而是主图在 `preprocess` 阶段会先执行的一层上下文整理逻辑。

四个 level 是渐进式的：

1. `Snip`: 最轻量，不调用 LLM。把旧工具结果内容替换为 `[Old tool result content cleared]`，保留消息结构和 `tool_call_id`。
2. `MicroCompact`: 时间触发。如果距离上一次 assistant 消息超过阈值，就保留最近 N 个工具结果，清理更早的工具结果。
3. `Collapse`: 结构级折叠。当上下文使用率达到主动整理阈值时，保留最近消息，把更早消息折叠成边界标记和简短说明。
4. `AutoCompact`: 最后的兜底。超过自动压缩阈值时，调用模型生成 `<analysis>` + `<summary>`，最终只保留 `<summary>` 进入上下文。

Snip 的清理标记是：

```text
[Old tool result content cleared]
```

这样做不是直接删除消息，是为了不破坏工具调用链。后续消息可能引用前面的工具调用 ID，如果直接删除，消息结构会断。Snip 只清空大段内容，保留工具消息本身。

现在也新增了 `snip_context` 工具。当主 agent 认为某些旧工具结果已经分析完、不再需要原文时，可以调用这个工具。工具执行后，`result_backfill` 会根据 `tool_call_ids` 或 `tool_names` 清理对应旧工具结果。

上下文管理会输出一个事件：

```text
[context] micro_compact freed≈12000
```

终端里可以看到具体触发了哪一级，以及大概释放了多少 token。

### Token 预算

`agent/main_agent/config.py` 里放了上下文相关配置：

- `CONTEXT_WINDOW_TOKENS`: 模型窗口估计值
- `OUTPUT_TOKEN_RESERVE`: 给输出预留的 token
- `CONTEXT_EFFECTIVE_LIMIT`: 有效上下文上限
- `AUTO_COMPACT_THRESHOLD_RATIO`: 自动压缩阈值
- `COLLAPSE_COMMIT_RATIO`: Collapse 主动整理阈值
- `MICRO_COMPACT_IDLE_SECONDS`: MicroCompact 的时间阈值
- `MICRO_COMPACT_KEEP_RECENT`: 微压缩保留最近几个工具结果

Token 统计现在分两层：

- 请求前：仍然使用本地估算，服务于预处理、blocking limit 和上下文压缩触发。
- 请求后：DashScope 流式 API 会通过 `stream_options={"include_usage": true}` 返回真实 `usage`，终端显示 `prompt_tokens / completion_tokens / total_tokens`。

也就是说，`preprocess` 阶段看到的是估算值；模型真正回复完成后，`token` 行会优先显示 DashScope 返回的真实用量。只有模型或兼容端点没有返回 `usage` 时，才回退到本地估算。

## 会话历史

现在启动方式仍然是：

```bash
python main.py
```

但进入聊天前会先出现一个会话选择列表：

```text
请选择这次要继续的会话：
[0] 新会话
[1] 上下文管理实现 -- 加入 Snip/MicroCompact/AutoCompact (24 msgs, 05-22 18:30)
```

会话历史存在本地 SQLite：

```text
.agent_data/sessions.sqlite3
```

SQLite 只用于本地运行时状态，不应该上传到 GitHub。每轮对话结束后，系统会保存完整 messages，并在后台启动 `session_summarizer` 生成会话标题和一句话摘要。

这个摘要和记忆系统有关系：摘要 sub agent 会参考 `MEMORY.md` 索引，但不会把长期记忆正文复制进会话摘要。它的职责只是帮助启动时判断“这个会话主要在干什么”。

### AutoCompact 摘要

AutoCompact 使用专门提示词，并明确要求模型不能调用任何工具。输出分为两个 XML 块：

```xml
<analysis>
用于整理思路，最终会丢弃。
</analysis>

<summary>
正式摘要，会继续留在上下文里。
</summary>
```

最终进入上下文的只有 `<summary>`。这样可以给模型一点整理空间，又避免把分析草稿继续塞回上下文。

每次 Collapse 或 AutoCompact 都会插入一个 `CompactBoundaryMessage` 风格的系统消息，记录压缩级别、压缩前 token 和涉及消息数量，方便后续识别上下文边界。

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

- `memory_writer`: 对话结束后后台观察当前 working memory，按候选提取流水线判断是否值得长期保存。
- `memory_retrieval`: 新一轮对话前读取 `MEMORY.md` 索引，再根据当前用户问题选择少量相关正文。

还有一个 `save_memory` 工具。主 agent 如果明确判断某条信息应该长期保存，可以主动调用它。同一轮里主 Agent 和后台记忆提取 Agent 互斥：主 Agent 写了，后台 forked memory agent 就不写；主 Agent 没写，后台才会尝试候选提取。

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
- 会话摘要 subagent: `qwen3.5-122b-a10b`

`tool_search` 不负责执行工具，只负责判断“这一轮要开放哪些工具给主 agent”。它会读取 `agent/tools/README.md` 里的工具目录摘要，然后返回类似这样的 JSON：

```json
{"tools": ["ls_project", "grep_project"]}
```

主 agent 拿到这些工具名以后，再从 registry 中找到对应的工具 schema，并把这些工具暴露给模型。

`permission_review` 发生在工具执行前。它不会选工具，也不会执行工具，只负责看这次工具调用是否应该放行。主循环收到审查结果后，会 yield 一个 `[tool_review]` 事件，然后才决定是否进入 `[tool_start]`。

`tool_runner` 是真正执行工具的地方。它现在不会再复制 Main Agent 的完整上下文，而是通过 `agent/sub_agent/context_builder.py` 构造任务切片。

Main Agent 保存的是全局工作记忆：

- 长对话历史
- 长期记忆索引
- 当前任务状态
- 压缩摘要
- 工具结果链

Sub Agent 拿到的是局部任务工作集：

- 当前用户目标
- 本轮工具调用
- 少量最近消息
- relevant summary
- 相关记忆摘要片段
- 从工具参数推断出的相关文件路径

这样做是为了避免 fork sub agent 时把 Main 的大上下文无限复制。Main 更像长期协调者，Sub 更像短期执行者。两者可以共用上下文管理思想，但作用范围不同：Main 管全局长期状态，Sub 管局部短期任务。

终端里会显示类似：

```text
[sub_context] tool_runner messages=8 ctx≈1200
```

这表示 sub agent 实际拿到的是压缩后的局部上下文，而不是整段会话历史。

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
- `snip_context`: 裁剪旧工具结果，释放上下文空间

文件工具现在的权限是当前项目根目录，也就是可以修改这个 agent 项目本身，但仍然不能访问项目目录之外的路径。`agent_write/` 现在只是一个普通的项目子目录，可以继续用来放 agent 生成的临时文件或练习文件。

工具现在带有类别和并发安全标记。只读工具和搜索工具可以并发执行，比如 `read_file`、`ls_project`、`grep_project`、`read_project_file`。会修改文件或产生副作用的工具，比如 `write_file`、`delete_file`、`run_command`，默认顺序执行，避免多个工具同时影响项目。

## 项目结构

```text
.
├── agent/
│   ├── main_agent/
│   │   ├── cli.py
│   │   ├── config.py
│   │   ├── context_manager.py
│   │   ├── graph.py
│   │   ├── logging_config.py
│   │   ├── model_client.py
│   │   ├── session_store.py
│   │   ├── state.py
│   │   ├── terminal_ui.py
│   │   ├── token_usage.py
│   │   └── terminal_input.py
│   ├── memory_system/
│   ├── sub_agent/
│   └── tools/
├── agent_write/
├── memory/
├── main.py
└── README.md
```

运行时还会创建：

```text
.agent_data/
logs/
```

这些是本地状态和日志，不上传到 GitHub。

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

- 上下文管理最好是渐进式的，先用零成本手段，再考虑 LLM 摘要。
- Snip 不应该删除消息，而是替换工具结果正文，这样能保留工具调用链。
- MicroCompact 适合用户暂停后回来这种自然断点。
- Collapse 是主动整理，不等到上下文真的爆掉才开始。
- AutoCompact 是兜底手段，成本最高，所以要放在最后。
- 记忆系统和上下文压缩互补，重要决策应该写入 `memory/`，避免压缩后丢失。
- 会话摘要不是长期记忆，它只服务于“启动时选择哪段历史”。
- Main/Sub 的关键不是谁更高级，而是工作记忆层级不同：Main 是全局长期，Sub 是局部短期。
- Terminal UI 也应该有单独边界，不应该把展示逻辑继续堆在 agent 后端流程里。

## 下一步可以做什么

后面可以继续加：

- 更好的上下文压缩
- 更完整的 stop hook / tool hook
- MCP 工具接入
- 任务计划和多步骤执行
- 用真实 tokenizer 改进请求前的本地预估
