# Tool Catalog

这个文件是给 `tool_search_subagent` 看的轻量工具目录。它只负责帮 subagent 判断“本轮应该开放哪些工具”；真正传给主模型的 function schema 仍由 `registry.py` 根据选中工具生成。

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
