# Fork

Fork 是轻量并行子 Agent 模式，适合多个独立、只读的调查任务。

约束：

- Fork worker 继承主 Agent 当前上下文。
- Fork worker 之间不通信。
- Fork worker 不允许继续创建 Fork 或 Coordinator。
- Fork worker 默认只使用只读/低风险工具。
- 最终汇总由主 Agent 完成。
