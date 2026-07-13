# Tool Catalog

这个文件是给 `tool_search` 看的轻量工具目录。它只负责帮 subagent 判断“本轮应该开放哪些工具”；真正传给主模型的 function schema 由 `registry.py` 按 AlwaysLoad / LazyLoad 分层生成。

## 加载策略

系统启动时会加载完整工具注册表，但主模型每一轮 API 调用不会默认看到全部 schema。

AlwaysLoad 是每轮默认暴露给主模型的基础工具，要求高频、低风险、通用、schema 小：

- `tool_search`
- `read_project_file`
- `grep_project`
- `glob_project`
- `ls_project`
- `file_info`
- `calculator`
- `current_time`

LazyLoad 是系统内部知道存在、但只有任务需要时才把完整 schema 暴露给主模型的工具：

- 文件副作用：`write_file`、`edit_file`、`delete_file`
- 批量读取：`read_many_files`
- 本地执行：`run_command`
- 记忆：`save_memory`、`delete_memory`、`prune_memories`
- 上下文和任务状态：`snip_context`、`todo_write`
- 编排：`fork_tasks`、`coordinator_plan`
- MCP：`mcp__tavily__*`、`mcp__amap_maps__*`
- Skills：`skill_verify`、`skill_debug`、`skill_simplify` 等

主模型默认只拿到 AlwaysLoad schema。预处理阶段或主模型调用 `tool_search` 后，系统会把本轮选中的 LazyLoad schema 追加到下一次 API 调用的 tools 列表中。权限、schema 校验、permission_review 和交互式确认仍然在执行阶段生效。

## 目录结构

- `agent/tools/tool/`: 内置本地工具，例如文件、项目搜索、命令、记忆和上下文工具。
- `agent/tools/mcp/`: MCP 工具适配层，从 `.mcp.json` 发现 stdio MCP server 并转换成普通工具。
- `agent/tools/skills/`: skills 工具预留目录，后续接入 slash command / skill 系统。
- `agent/fork/`: Fork 编排模式，负责并行只读 worker。
- `agent/Coordinator/`: Coordinator 编排模式，负责 research workers 和实施规格综合。

## 工具列表

- `tool_search`
  - 路径: `agent/tools/tool/discovery.py`
  - 类别: 发现
  - 暴露策略: AlwaysLoad
  - 并发安全: 是
  - 作用: 根据当前任务从完整工具目录中发现需要延迟加载的工具。
  - 使用场景: 主模型需要写文件、执行命令、调用 MCP、调用 skills 或编排能力，但当前还没有看到对应 schema。
  - 限制: 只返回工具名和摘要，不直接执行工具；真正执行仍走下一轮 schema 暴露和权限管线。

- `read_file`
  - 路径: `agent/tools/tool/filesystem.py`
  - 类别: 文件
  - 暴露策略: LazyLoad
  - 并发安全: 是
  - 作用: 读取当前项目内的文本文件。
  - 使用场景: 需要查看项目文件、源码、文档或 agent 生成的文件。

- `write_file`
  - 路径: `agent/tools/tool/filesystem.py`
  - 类别: 文件
  - 暴露策略: LazyLoad
  - 并发安全: 否
  - 作用: 写入当前项目内的文本文件，会自动创建父目录。
  - 使用场景: 需要修改项目文件、生成脚本、笔记、计划或临时输出文件。
  - 审查: `requires_review`，执行前会进入权限审查，通常需要用户交互确认。

- `delete_file`
  - 路径: `agent/tools/tool/filesystem.py`
  - 类别: 文件
  - 暴露策略: LazyLoad
  - 并发安全: 否
  - 作用: 删除当前项目内的文件；只能删除文件，不能删除目录。
  - 使用场景: 需要移除 agent 生成的临时文件或明确要删除的项目文件。
  - 审查: `requires_review`，执行前会进入权限审查，通常需要用户交互确认。

- `list_dir`
  - 路径: `agent/tools/tool/filesystem.py`
  - 类别: 文件
  - 暴露策略: LazyLoad
  - 并发安全: 是
  - 作用: 列出当前项目内目录的文件名。
  - 使用场景: 需要查看项目目录里已有文件。

- `ls_project`
  - 路径: `agent/tools/tool/project.py`
  - 类别: 搜索
  - 暴露策略: AlwaysLoad
  - 并发安全: 是
  - 作用: 类似 `ls`，列出当前项目目录中的文件/目录，可递归；只读。
  - 使用场景: 用户让 agent 查看项目结构、目录、文件列表。

- `grep_project`
  - 路径: `agent/tools/tool/project.py`
  - 类别: 搜索
  - 暴露策略: AlwaysLoad
  - 并发安全: 是
  - 作用: 类似 `grep`/`rg`，在当前项目内搜索文本，支持 `content/files/count` 输出、glob 过滤、ignore、大小写和上下文行。
  - 使用场景: 用户让 agent 查找函数、变量、关键词、报错文本、配置项，或只需要知道哪些文件命中。

- `glob_project`
  - 路径: `agent/tools/tool/project.py`
  - 类别: 搜索
  - 暴露策略: AlwaysLoad
  - 并发安全: 是
  - 作用: 按 glob 文件名模式查找当前项目内文件，例如 `**/*.py`。
  - 使用场景: 需要按扩展名、目录模式、文件名片段快速定位文件。

- `read_project_file`
  - 路径: `agent/tools/tool/project.py`
  - 类别: 文件
  - 暴露策略: AlwaysLoad
  - 并发安全: 是
  - 作用: 读取当前项目内的文本文件；只读。
  - 使用场景: `ls_project` 或 `grep_project` 定位到文件后，需要查看具体文件内容。

- `read_many_files`
  - 路径: `agent/tools/tool/project.py`
  - 类别: 文件
  - 暴露策略: LazyLoad
  - 并发安全: 是
  - 作用: 一次读取多个项目内文本文件，每个文件可单独限制读取长度。
  - 使用场景: 批量代码审查、fork/coordinator 汇总多个相关文件上下文。

- `file_info`
  - 路径: `agent/tools/tool/project.py`
  - 类别: 文件
  - 暴露策略: AlwaysLoad
  - 并发安全: 是
  - 作用: 查看项目内文件或目录是否存在、大小、类型和修改时间。
  - 使用场景: 需要先确认文件状态、大小或路径是否正确。

- `edit_file`
  - 路径: `agent/tools/tool/project.py`
  - 类别: 文件
  - 暴露策略: LazyLoad
  - 并发安全: 否
  - 作用: 对当前项目内文本文件做精确字符串替换。
  - 使用场景: 小范围代码修改，不适合整文件重写。
  - 行为: `old_string` 找不到会失败；默认只允许唯一匹配，多次匹配时要求更精确或设置 `replace_all=true`。
  - 审查: `requires_review`，执行前会进入权限审查，通常需要用户交互确认。

- `calculator`
  - 路径: `agent/tools/tool/calculator.py`
  - 类别: 执行
  - 暴露策略: AlwaysLoad
  - 并发安全: 是
  - 作用: 安全计算四则运算表达式。
  - 使用场景: 用户要求算数、验证简单数学结果。

- `current_time`
  - 路径: `agent/tools/tool/time_tool.py`
  - 类别: 执行
  - 暴露策略: AlwaysLoad
  - 并发安全: 是
  - 作用: 获取指定 IANA 时区的当前时间，默认 `Asia/Shanghai`。
  - 使用场景: 用户询问当前时间、日期，或需要时间戳。

- `run_command`
  - 路径: `agent/tools/tool/command.py`
  - 类别: 执行
  - 暴露策略: LazyLoad
  - 并发安全: 否
  - 作用: 在当前项目内本地运行命令，例如运行 Python 脚本、单元测试或检查命令；不通过 shell 执行，工作目录不能离开项目。
  - 使用场景: 用户要求跑代码、跑测试、验证脚本输出或执行项目内命令。
  - 审查: 执行前会交给 `permission_review` 判断风险，危险命令会被拦截。

- `save_memory`
  - 路径: `agent/tools/tool/memory_tools.py`
  - 类别: 记忆
  - 暴露策略: LazyLoad
  - 并发安全: 否
  - 作用: 保存一条长期记忆到 `memory/` 目录，并更新 `memory/MEMORY.md` 索引。
  - 使用场景: 用户明确表达长期偏好、项目长期约束、已确认的协作方式或外部引用时使用。
  - 生命周期: 支持 `ttl_days`、`salience`、`confidence`、`replace_path`，用于 TTL、显著性衰减和冲突覆盖。
  - 限制: 只保存无法从代码、文件或 Git 重新推导的信息；不要保存临时任务、debug 过程、文件结构或一次性对话。

- `delete_memory`
  - 路径: `agent/tools/tool/memory_tools.py`
  - 类别: 记忆
  - 暴露策略: LazyLoad
  - 并发安全: 否
  - 作用: 根据用户显式要求删除一条具体长期记忆，并更新 `memory/MEMORY.md`。
  - 使用场景: 用户说“忘记这条”“删除这个记忆”“以后不要记这个”。
  - 审查: 需要 `permission_review` 放行。

- `prune_memories`
  - 路径: `agent/tools/tool/memory_tools.py`
  - 类别: 记忆
  - 暴露策略: LazyLoad
  - 并发安全: 否
  - 作用: 按 TTL、使用频率和显著性衰减清理过期或低价值长期记忆。
  - 使用场景: 用户要求清理长期记忆，或需要手动触发遗忘策略。

- `snip_context`
  - 路径: `agent/tools/tool/context_tools.py`
  - 类别: 上下文
  - 暴露策略: LazyLoad
  - 并发安全: 否
  - 作用: 标记旧工具结果可以被裁剪，系统会把匹配的工具结果内容替换为 `[Old tool result content cleared]`。
  - 使用场景: 读取/搜索了大量文件，分析完成后不再需要保留完整工具结果时使用。
  - 限制: 不删除消息，只清理工具结果正文，保持 tool_call_id 链路完整。

- `todo_write`
  - 路径: `agent/tools/tool/todo.py`
  - 类别: 任务
  - 暴露策略: LazyLoad
  - 并发安全: 否
  - 作用: 写入或更新当前会话 todo 列表，用于长任务规划和进度跟踪。
  - 存储: `.agent_data/todos/<session_id>.json`，属于本地运行态数据，不提交 GitHub。
  - 审查: `requires_review`，因为会写入本地任务状态。

- `fork_tasks`
  - 路径: `agent/fork/tool.py`
  - 类别: 编排
  - 暴露策略: LazyLoad
  - 并发安全: 否
  - 作用: 并行运行多个短生命周期只读 Fork worker。
  - 使用场景: 分别分析多个独立模块、多方向搜索、方案比较、只读代码考古。
  - 限制: Fork worker 不允许递归创建 Fork/Coordinator；默认只拿只读/低风险工具。
  - 审查: `requires_review`，因为会触发额外模型调用和并行 worker。

- `coordinator_plan`
  - 路径: `agent/Coordinator/tool.py`
  - 类别: 编排
  - 暴露策略: LazyLoad
  - 并发安全: 否
  - 作用: 按 Coordinator 模式执行 Research + Synthesis，生成实施规格。
  - 使用场景: 复杂多阶段工程任务，需要先调查多个方向，再综合成明确实施方案。
  - 限制: 当前 v1 不直接修改项目代码；scratchpad 写入 `.agent_data/coordinator_scratchpad/`。
  - 审查: `requires_review`，因为会创建 research workers 并写 scratchpad。

## MCP 工具

MCP server 不固定写死在这个 README 里，而是由 `agent/tools/mcp/` 从项目根目录的 `.mcp.json` 动态发现。发现后的工具会注册成：

```text
mcp__<server_name>__<tool_name>
```

当前已接入的 MCP server：

- `amap-maps`：通过 stdio 启动 `@amap/amap-maps-mcp-server`，适合地址解析、地点搜索、经纬度查询和路线规划。
- `tavily`：通过 stdio 启动 `tavily-mcp@latest`，适合联网搜索、网页内容提取、站点地图发现和网页爬取。

`.mcp.json` 保存本地 API key，已经加入 `.gitignore`，不要提交到远端。

MCP 工具同样走普通工具管线：

- `tool_search` 只看到摘要和 schema，按问题选择是否开放 MCP 工具。
- 主模型拿到被选中的 MCP function schema 后生成参数。
- `tool_runner` 做参数校验、settings 权限规则、上下文审查和必要的用户确认。
- 运行时通过 stdio fork MCP server，调用具体 MCP tool，并只把结果回填给主 agent。

Terminal 里输入 `/@` 可以手动选择 MCP server，表示“本轮我明确希望使用某个 MCP”。也可以直接输入 `/@amap-maps 查询杭州西湖附近的咖啡店` 或 `/@tavily 搜索 LangGraph 最新文档`。

## Skills 工具

`agent/tools/skills/` 现在接入了内置技能（Bundled skills）。启动时通过 `init_bundled_skills()` 注册，随后由 `registry.py` 转成普通工具，工具名统一为：

```text
skill_<skill_name>
```

例如 `/verify` 对应 `skill_verify`，`/update-config` 对应 `skill_update_config`。

技能工具本身只生成技能 prompt 和必要的引用文件路径，不直接修改项目；真正的读写、命令、MCP 调用仍然要继续经过普通工具权限管线。

当前内置技能：

- `update-config`
  - 工具名: `skill_update_config`
  - 作用: 配置 settings.json 的各项设置。
  - 场景: 修改权限规则、环境变量、默认模型。

- `keybindings-help`
  - 工具名: `skill_keybindings_help`
  - 作用: 键盘快捷键自定义帮助。
  - 场景: 查看和自定义按键绑定。

- `verify`
  - 工具名: `skill_verify`
  - 作用: 验证代码变更的正确性。
  - 场景: 提交前最终验证、CI 前本地检查。
  - 引用文件: 首次调用时提取 `verify-guide.md` 到 `.agent_data/bundled_skills/verify/`。

- `debug`
  - 工具名: `skill_debug`
  - 作用: 调试辅助，提供诊断思路。
  - 场景: 定位 bug 根因、分析错误堆栈。
  - 引用文件: 首次调用时提取 `debug-guide.md` 到 `.agent_data/bundled_skills/debug/`。

- `simplify`
  - 工具名: `skill_simplify`
  - 作用: 代码简化与重构审查。
  - 场景: 消除重复代码、降低圈复杂度。

- `skillify`
  - 工具名: `skill_skillify`
  - 作用: 将 prompt 转换为可复用的技能。
  - 场景: 把一次性的 prompt 模板化为 `SKILL.md`。

- `remember`
  - 工具名: `skill_remember`
  - 作用: 记忆管理。
  - 场景: 添加项目规范、团队约定、长期偏好。

- `batch`
  - 工具名: `skill_batch`
  - 作用: 批量文件处理。
  - 场景: 批量重命名、批量格式化、批量替换。

- `stuck`
  - 工具名: `skill_stuck`
  - 作用: 帮助模型走出困境。
  - 场景: 模型陷入循环时重新整理目标、事实和下一步。

- `lorem-ipsum`
  - 工具名: `skill_lorem_ipsum`
  - 作用: 生成占位内容。
  - 场景: UI 开发时的占位文本。

Terminal 支持：

- `/skills`: 查看内置技能列表。
- `/verify 当前改动`: 强制调用 verify 技能。
- `/debug 报错信息`: 强制调用 debug 技能。

文件型技能预留格式：

```text
.agents/skills/my-skill/SKILL.md
```

`SKILL.md` 支持 YAML frontmatter，例如 `name`、`description`、`when_to_use`、`allowed-tools`、`model`、`effort`、`context`、`paths` 等字段。后续用户自定义技能也会进入同一个工具池。

## 审查子智能体

- `permission_review`
  - 路径: `agent/sub_agent/permission_review.py`
  - 作用: 在工具真正执行前审查工具名、参数、工具职责和当前上下文，返回 `allowed/risk/reason`。
  - 设计: 权限管线包含 `validateInput`、规则匹配、上下文审查和 `interactivePrompt`。读文件、搜索、计算、查时间默认低风险；写文件、删文件、本地命令属于需要谨慎的操作；删除大量文件、修改 git 历史、推送远端、离开项目目录、执行不明安装脚本等高风险行为会被阻止或要求用户确认。

- `memory_writer`
  - 路径: `agent/sub_agent/memory_writer.py`
  - 作用: 对话结束后由后台 forked memory agent 观察当前 working memory，按候选提取流水线提取长期记忆并写入 `memory/`。
  - 互斥: 如果主 agent 已经用 `save_memory` 主动保存，本轮后台提取会跳过，避免重复。

- `memory_retrieval`
  - 路径: `agent/sub_agent/memory_retrieval.py`
  - 作用: 新一轮对话前读取 `memory/MEMORY.md` 作为记忆目录，并按当前问题选择少量相关正文。
  - 设计: 主 agent 默认只看到索引；只有相关时才读取具体 `memory/**/*.md` 文件正文。
