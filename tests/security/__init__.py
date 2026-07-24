"""安全评测测试包（Task 18）。

覆盖五类安全评测：
- 18.1 提示注入（``test_prompt_injection``）
- 18.2 伪造法条（``test_citation_forgery``）
- 18.3 数据库污染与记忆投毒（``test_memory_poisoning``）
- 18.4 工具越权（``test_tool_privilege_escalation``）
- 18.5 无限制循环（``test_runaway_loop``）

所有测试均使用 mock state / 临时目录，不依赖真实数据库或 LLM。
"""
