# Coordinator

Coordinator 是多 Agent 编排模式，适合复杂工程任务。

当前 v1 实现：

1. Research：并行创建只读 worker 调查不同方向。
2. Synthesis：Coordinator 读取 worker 结果并生成实施规格。
3. Scratchpad：把研究结果和实施规格写入 `.agent_data/coordinator_scratchpad/`。

当前暂不自动进入 Implementation / Verification 写文件阶段，避免多 worker 并发修改造成冲突。
