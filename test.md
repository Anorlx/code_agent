# Agent 测试笔记

## 测试目标

测试主要验证当前 Agent 的核心能力是否跑通：

- 主循环与 LangGraph 状态流
- 工具选择与工具执行
- Fork / Coordinator 模式
- MCP 工具发现与强制选择
- 权限审批拦截
- SQLite 会话与 checkpoint
- Prompt cache 与 memory 注入
- 基础安全边界

## 1. 静态编译检查

执行命令：

```bash
conda run -n llamaindex python -m compileall agent main.py
```
结果：通过。

## 2. 主循环离线测试

使用 fake model 跑 `run_agent()`，不调用真实 DashScope，用来验证状态机和 LangGraph 流程。

覆盖场景：

```text
chat          普通对话
tools         模型调用 calculator 后回填结果
fork          触发 fork_tasks 后回到 post_orchestration
coordinator   触发 coordinator_plan 后回到 post_orchestration
```

测试结果：

```text
CASE chat terminal=completed tools=[]
CASE calc terminal=completed tools=['calculator']
CASE fork terminal=completed tools=['fork_tasks']
COORD terminal=completed tools=['coordinator_plan']
```
主循环可以正常走：

```text
初始化 -> 预处理 -> API调用 -> 工具执行 -> 结果回填 -> 验证 -> 下一轮/终止
```
Fork / Coordinator 执行后不会一直重复调用，会在下一轮切到 `post_orchestration`，交给主模型总结。

## 3. 工具真实执行测试

在临时目录中创建测试文件，验证工具真实行为，不修改项目文件。

测试项：

```text
calculator
glob_project
grep_project
read_many_files
file_info
edit_file 多匹配保护
edit_file 单点替换
read_project_file 路径越界保护
run_command 正常命令
run_command 危险命令拒绝
schema required 校验
parallel_safe 并行分组
```

结果：

```text
TOOLTEST_PASS 12 / 12
```

关键结论：

- `grep_project`、`glob_project`、`read_project_file` 这类只读工具可以并行。
- `edit_file`、`write_file`、`delete_file`、`run_command` 这类副作用工具不会并行。
- `rm -rf *` 这类危险命令会被拒绝。
- `../outside.txt` 这类路径逃逸会被拒绝。
- `edit_file` 遇到多个匹配时会要求更精确，不会乱替换。

## 4. MCP 发现测试

检查 MCP cache 和 MCP tool 注册。

结果：

```text
MCP_SERVERS ['amap-maps', 'tavily']
amap-maps: 12 tools
tavily: 5 tools
MCP total: 17 tools
```

强制选择路径：

```text
/@tavily 搜索 LangGraph
```

可以选到 Tavily MCP tools：

```text
mcp__tavily__tavily_search
mcp__tavily__tavily_extract
mcp__tavily__tavily_crawl
...
```

结论：

MCP 已经被工具化，可以作为普通 tool 被 Agent 自动选择，也可以通过 `/@server` 主动指定。

## 5. 权限审批流程测试

构造一个需要审批的工具调用，测试异步拦截链路。

事件顺序：

```text
sub_context
tool_review
permission_decision
tool_start
tool_result
```

结论：

权限系统确实是异步拦截的。Agent 会停在 `permission_prompter`，等待用户允许或拒绝后才继续执行工具。

关键实现位置：

```text
agent/sub_agent/tool_runner.py
```

## 6. SQLite 会话和 Checkpoint 测试

使用临时 SQLite 文件测试：

```text
SessionStore.create_session()
SessionStore.save_messages()
SessionStore.load_messages()
CheckpointStore.save_checkpoint()
CheckpointStore.latest_unfinished()
CheckpointStore.mark_completed()
```

结果：

```text
session_ok=True
checkpoint_ok=True
```

结论：

会话历史和 checkpoint 的基础存取是通的。

## 7. Prompt Cache / Memory 注入测试

测试请求消息构造和 DashScope cache 标记。

结果：

```text
REQUEST roles= ['user', 'assistant', 'system', 'user']
REQUEST cache_flags= [False, True, False, False]
NORMALIZED system_cache= {'type': 'ephemeral'}
NORMALIZED stable_tail_cache= {'type': 'ephemeral'}
```

结论：

- system prompt 会带 `cache_control: ephemeral`。
- 稳定历史尾部会带 `cache_control: ephemeral`。
- memory 不在最前面的 system prompt 中，而是在稳定历史之后、当前用户输入之前作为动态上下文注入。

关键实现位置：

```text
agent/main_agent/model_client.py
agent/main_agent/prompt_cache.py
```

## 8. 敏感文件和 Git 风险检查

`.gitignore` 已包含：

```text
.mcp.json
.agent_data/
logs/
tests/
agent_write/*
memory/**/*.md
```

敏感扫描没有看到真实 API key 泄露。

但是当前 `git status -sb` 显示工作树变化非常大：

```text
大量 week*/day* 文件处于 deleted
根目录 agent/ 是 untracked
main.py 是 untracked
assets/ 有 untracked
```

这不是运行问题，但上传 GitHub 前需要非常小心。

不要直接执行：

```bash
git add .
```

建议先明确提交范围：

```bash
git status -sb
git add agent main.py README.md MAIN_README.md assets .gitignore
git status -sb
git commit -m "..."
```

敏感文件继续不要提交：

```text
.mcp.json
memory/
.agent_data/
logs/
tests/
agent_write/
```
