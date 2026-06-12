from __future__ import annotations

import json
from typing import Any

COORDINATOR_SYNTHESIS_PROMPT = """你是 Coordinator。
你不直接修改代码，不直接运行命令。
你的职责是理解 worker 的研究结果，并综合成明确、可执行的实施规格。

必须体现四阶段工作流：
1. Research 已完成什么
2. Synthesis 得出什么设计
3. Implementation 应该如何分配任务
4. Verification 应该如何验证

输出 Markdown，要求：
- 给出核心结论
- 写清楚文件/模块层面的实施规格
- 标注可以并行和必须串行的工作
- 标注风险、未知点和验证步骤
- 不要编造 worker 没有发现的信息
"""


def synthesis_user_prompt(task: str, research_results: list[dict[str, Any]], scratchpad_files: list[str]) -> str:
    return json.dumps(
        {
            "task": task,
            "scratchpad_files": scratchpad_files,
            "research_results": research_results,
        },
        ensure_ascii=False,
        indent=2,
    )
