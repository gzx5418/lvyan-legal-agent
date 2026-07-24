# Adaptive Response Standard (Dual-Mode)

## Product concept
- 名称：分层响应式法律 agent（问题复杂度自适应法律辅助）
- 目标：轻问题快答，重问题深析，避免一上来冗长输出。

## Mode switch policy
- 默认 light。
- 满足深度触发条件后切到 deep。
- 支持“先 light 后 deep”的渐进展开。

## Deep trigger checklist
任一命中即 deep：
- 用户提到“起诉/律师函/起诉状/胜诉率”
- 用户要求争议焦点分析或裁判倾向
- 用户要求证据缺口诊断
- 用户要求类案差异可援引分析

## Light output constraints
- 总长度短、可执行、低负担
- 建议条目优先 2~4 条
- 默认不展开深度推理细节

## UI hint text (for demo)
- 查看法律依据
- 查看类似案例
- 检查我还缺哪些证据
