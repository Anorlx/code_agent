# Tool Catalog

这个文件是给 `tool_search` 看的轻量工具目录。它只负责帮 subagent 判断“本轮应该开放哪些工具”；真正传给主模型的 function schema 仍由 `registry.py` 根据选中工具生成。

## 工具列表

- `read_file`
  - 路径: `agent/tools/filesystem.py`
  - 类别: 文件
  - 并发安全: 是
  - 作用: 读取当前项目内的文本文件。
  - 使用场景: 需要查看项目文件、源码、文档或 agent 生成的文件。

- `write_file`
  - 路径: `agent/tools/filesystem.py`
  - 类别: 文件
  - 并发安全: 否
  - 作用: 写入当前项目内的文本文件，会自动创建父目录。
  - 使用场景: 需要修改项目文件、生成脚本、笔记、计划或临时输出文件。

- `delete_file`
  - 路径: `agent/tools/filesystem.py`
  - 类别: 文件
  - 并发安全: 否
  - 作用: 删除当前项目内的文件；只能删除文件，不能删除目录。
  - 使用场景: 需要移除 agent 生成的临时文件或明确要删除的项目文件。

- `list_dir`
  - 路径: `agent/tools/filesystem.py`
  - 类别: 文件
  - 并发安全: 是
  - 作用: 列出当前项目内目录的文件名。
  - 使用场景: 需要查看项目目录里已有文件。

- `ls_project`
  - 路径: `agent/tools/project.py`
  - 类别: 搜索
  - 并发安全: 是
  - 作用: 类似 `ls`，列出当前项目目录中的文件/目录，可递归；只读。
  - 使用场景: 用户让 agent 查看项目结构、目录、文件列表。

- `grep_project`
  - 路径: `agent/tools/project.py`
  - 类别: 搜索
  - 并发安全: 是
  - 作用: 类似 `grep`/`rg`，在当前项目内搜索文本，返回路径、行号、列号和匹配行。
  - 使用场景: 用户让 agent 查找函数、变量、关键词、报错文本、配置项。

- `read_project_file`
  - 路径: `agent/tools/project.py`
  - 类别: 文件
  - 并发安全: 是
  - 作用: 读取当前项目内的文本文件；只读。
  - 使用场景: `ls_project` 或 `grep_project` 定位到文件后，需要查看具体文件内容。

- `calculator`
  - 路径: `agent/tools/calculator.py`
  - 类别: 执行
  - 并发安全: 是
  - 作用: 安全计算四则运算表达式。
  - 使用场景: 用户要求算数、验证简单数学结果。

- `current_time`
  - 路径: `agent/tools/time_tool.py`
  - 类别: 执行
  - 并发安全: 是
  - 作用: 获取指定 IANA 时区的当前时间，默认 `Asia/Shanghai`。
  - 使用场景: 用户询问当前时间、日期，或需要时间戳。

- `run_command`
  - 路径: `agent/tools/command.py`
  - 类别: 执行
  - 并发安全: 否
  - 作用: 在当前项目内本地运行命令，例如运行 Python 脚本、单元测试或检查命令；不通过 shell 执行，工作目录不能离开项目。
  - 使用场景: 用户要求跑代码、跑测试、验证脚本输出或执行项目内命令。
  - 审查: 执行前会交给 `permission_review` 判断风险，危险命令会被拦截。

- `save_memory`
  - 路径: `agent/tools/memory_tools.py`
  - 类别: 记忆
  - 并发安全: 否
  - 作用: 保存一条长期记忆到 `memory/` 目录，并更新 `memory/MEMORY.md` 索引。
  - 使用场景: 用户明确表达长期偏好、项目长期约束、已确认的协作方式或外部引用时使用。
  - 限制: 只保存无法从代码、文件或 Git 重新推导的信息；不要保存临时任务、debug 过程、文件结构或一次性对话。

## 审查子智能体

- `permission_review`
  - 路径: `agent/sub_agent/permission_review.py`
  - 作用: 在工具真正执行前审查工具名、参数、工具职责和当前上下文，返回 `allowed/risk/reason`。
  - 设计: 读文件、搜索、计算、查时间默认低风险；写文件、删文件、本地命令属于需要谨慎的操作；删除大量文件、修改 git 历史、推送远端、离开项目目录、执行不明安装脚本等高风险行为会被阻止。

- `memory_writer`
  - 路径: `agent/sub_agent/memory_writer.py`
  - 作用: 对话结束后后台观察最近对话，按节流规则提取长期记忆并写入 `memory/`。
  - 互斥: 如果主 agent 已经用 `save_memory` 主动保存，本轮后台提取会跳过，避免重复。

- `memory_retrieval`
  - 路径: `agent/sub_agent/memory_retrieval.py`
  - 作用: 新一轮对话前读取 `memory/MEMORY.md` 作为记忆目录，并按当前问题选择少量相关正文。
  - 设计: 主 agent 默认只看到索引；只有相关时才读取具体 `memory/**/*.md` 文件正文。
