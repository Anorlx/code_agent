# real_agent - Week 1 / Day 2

这是我从 0 开始搭的第一个 Python Agent 小项目。今天主要目标不是一次性做一个很完整的 agent，而是先把最小可运行的骨架搭起来，后面再慢慢往里面加工具、技能和 MCP。

## Day 1 做了什么

Day 1 先完成了一个异步的对话循环。用户在 terminal 里输入问题后，程序会进入一个 `while` 循环，维护当前会话状态，并把历史消息继续传给模型。主 agent 使用 `glm-5`，工具选择 subagent 使用 `qwen3.5-flash`。

核心流程大概是：

1. 初始化状态
2. 预处理用户输入和历史消息
3. 调用 `tool_search_subagent` 选择本轮可能需要的工具
4. 调用主模型并流式输出回复
5. 如果模型触发工具调用，就执行工具
6. 把工具结果回填到消息历史里
7. 继续下一轮，直到完成或达到停止条件

这个循环目前最大 turn 数是 10，用来防止模型一直调用工具导致无限循环。

## Day 2 新增内容

Day 2 重点加了两个能力：

- `run_command` 工具：可以在当前项目内本地运行命令，比如跑 Python 脚本、跑单元测试、做一次项目检查。
- `permission_review_subagent`：在工具真正执行前审查工具名、参数和上下文，判断这次操作是否可能影响项目。

本地运行工具没有直接开放 shell，而是使用参数列表执行命令，并且工作目录只能在项目根目录里面。这样可以跑代码，但不会让命令随便跳到项目外面。

权限审查 subagent 会返回结构化结果：

```json
{"allowed": true, "risk": "low", "reason": "只运行单元测试"}
```

如果它判断风险太高，比如删除大量文件、修改 git 历史、推送远端、执行不明安装脚本，就会返回 `allowed=false`。主循环收到这个结果后不会执行工具，而是把“被拦截”的工具结果回填给主 agent，让主 agent 继续给用户解释。

```json
[tool_review] run_command allow risk=low reason=仅删除刚创建的空目录，范围明确且有限
```



## 异步和流式

今天的一个重点是把 agent 做成异步的，而不是等模型完整回答完才显示。

主循环 `run_agent` 是一个异步生成器，会不断 yield 状态事件、模型文本片段、工具调用和工具结果。这样 terminal 可以实时看到 agent 当前处于哪个阶段。

模型调用也走流式接口，收到一段内容就马上输出一段内容。工具执行也是 async 的，像项目搜索这种可能耗时的操作会放到线程里跑，避免卡住事件循环。

## 终端状态展示

工具执行时不会再把大段文件内容直接刷到 terminal 里，而是显示工具调用、工具开始、工具完成和状态流转。只读工具可以并发执行，所以可以看到多个 `read_project_file` 同时进入 `(parallel)` 状态。

![Day 1 terminal tool status](assets/day1-tool-status.svg)

## 主 Agent 和 Subagent

现在有三个 agent 角色：

- 主 agent: `glm-5`
- 工具选择 subagent: `qwen3.5-flash`
- 权限审查 subagent: `qwen3.5-flash`

`tool_search_subagent` 不负责执行工具，只负责判断“这一轮要开放哪些工具给主 agent”。它会读取 `agent/tools/README.md` 里的工具目录摘要，然后返回类似这样的 JSON：

```json
{"tools": ["ls_project", "grep_project"]}
```

主 agent 拿到这些工具名以后，再从 registry 中找到对应的工具 schema，并把这些工具暴露给模型。

`permission_review_subagent` 发生在工具执行前。它不会选工具，也不会执行工具，只负责看这次工具调用是否应该放行。主循环收到审查结果后，会 yield 一个 `[tool_review]` 事件，然后才决定是否进入 `[tool_start]`。

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

文件工具现在的权限是当前项目根目录，也就是可以修改这个 agent 项目本身，但仍然不能访问项目目录之外的路径。`agent_write/` 现在只是一个普通的项目子目录，可以继续用来放 agent 生成的临时文件或练习文件。

工具现在带有类别和并发安全标记。只读工具和搜索工具可以并发执行，比如 `read_file`、`ls_project`、`grep_project`、`read_project_file`。会修改文件或产生副作用的工具，比如 `write_file`、`delete_file`、`run_command`，默认顺序执行，避免多个工具同时影响项目。

## 项目结构

```text
.
├── agent/
│   ├── agent_loop.py
│   ├── cli.py
│   ├── config.py
│   ├── input.py
│   ├── models/
│   ├── subagents/
│   └── tools/
├── agent_write/
├── tests/
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

## 测试

目前写了一些测试，用来验证 agent 循环、工具、输入层和工具目录。

运行：

```bash
conda run -n llamaindex python -m unittest discover -s tests
```

## 今天的收获

这两天主要理解了几个点：

- agent 的核心其实是状态循环
- 流式输出需要把模型调用、工具调用和 UI 展示拆成事件
- subagent 可以只做一件很小的事，比如选择工具
- 权限审查也可以拆成 subagent，并且只通过 JSON 和主循环通信
- 工具目录可以用 README 这种轻量摘要来节省 prompt
- 真正的工具 schema 不需要每次都给 subagent，只在主模型需要调用工具时再传
- 本地命令工具要做安全边界，不能直接开放任意 shell 命令

## 下一步可以做什么

后面可以继续加：

- 更好的上下文压缩
- 更完整的 stop hook / tool hook
- MCP 工具接入
- 任务计划和多步骤执行
- 更清晰的 terminal UI
- 把 agent 的状态保存到文件或数据库
