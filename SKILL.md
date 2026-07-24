---
name: law
description: >
  律言 — Chinese legal assistant agent. Use when the user asks about PRC law,
  legal disputes, contract review, litigation strategy, evidence analysis,
  court rulings, lawsuit preparation, or legal document drafting.
  Trigger for 法律, 起诉, 维权, 合同, 证据, 裁判, 法条, 工伤, 劳动仲裁,
  租赁纠纷, 借贷, or "能不能告他", "怎么要回押金", "赔偿标准是什么".
  DO NOT TRIGGER for non-Chinese law (US law, EU GDPR, etc.) or pure academic theory.
argument-hint: "<法律问题描述或合同文件>"
---

# 律言 — Chinese Legal Assistant Agent

> **免责声明：** 本 Agent 辅助法律分析流程，输出不构成正式法律意见，重大事项请咨询执业律师。

## 适用边界

仅覆盖**中国大陆**法律体系。涉及港澳台地区法律或涉外法律事务（含跨境合同、外国法适用）建议咨询专业涉外律师。

## 运行入口

实际推理由 Python Agent Runtime 执行，本文件仅作触发与边界声明。

- **CLI:** `python -m lvyan.main "<法律问题>"`
- **Python API:** `from lvyan.main import run_agent; run_agent("问题")`
- 工作流编排由 `src/lvyan/graph/` 下的 LangGraph 状态图承担，节点链、检索策略、条件路由与策略守卫均不在本文件展开。

## 输入输出约定

- **输入：** 用户法律问题（自然语言文本）或合同/文书文件。
- **输出模式：** 按复杂度分三档，统一以 Markdown 交付。
  - `light` — 日常咨询速答
  - `deep` — 办案深度分析（含类案、证据、裁判推演）
  - `document` — 法律文书生成（起诉状、律师函、合同审查报告等）
- 所有法条/案例引用须经 **Citation Verifier** 节点校验；不可逆法律行为（如代书提交、发送律师函）须经用户批准。

## Policy Packs 索引

`prompts/` 目录存放按需加载的策略包，Runtime 在对应节点读取相关文件，不在本入口内联：

| 文件 | 说明 |
|------|------|
| `workflow-and-intent.md` | 工作流、意图识别与复杂度分级 |
| `adaptive-response-standard.md` | light/deep 双模式自适应响应标准 |
| `law-query-standard.md` | 法条检索参数、效力位阶排序、场景化解读 |
| `case-analysis-standard.md` | 类案检索参数、分析结构与胜诉率表达 |
| `case-difference-explainer-standard.md` | 类案差异解释器标准 |
| `evidence-burden-matrix.md` | 案由举证要件矩阵 |
| `evidence-gap-diagnostic-standard.md` | 证据缺口诊断标准 |
| `judicial-reasoning-standard.md` | 裁判思维模拟器标准 |
| `document-processing-standard.md` | 合同审查与法律文书生成标准 |
| `output-standard.md` | 分层输出结构与质量门槛 |
| `docx-export-standard.md` | Markdown 转 Word 格式要求 |
| `word-export-skill-integration.md` | 单技能 Word 导出集成规范 |
| `official-template-sources.md` | 已导入官方模板的来源与使用规则 |
| `samr_contract_templates.json` | SAMR 合同模板标准化索引（含按需下载 URL） |

## 质量底线

- 不得编造法条、案号或裁判结果。
- 所有法律结论必须来自经版本校验的权威证据。
- Agent 自主分析，但不可逆法律行为须经用户批准。

## 后续可选：MCP Server

将 `src/lvyan/tools/` 下的标准工具集封装为 MCP（Model Context Protocol）Server，
可供 Claude Code / ChatGPT / IDE Agent 等外部宿主共享调用律言的法规检索、类案检索、
文书生成等能力。本能力为后续可选方向，当前 Task 20 不强制实现。

