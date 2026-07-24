# Document Processing Standard

## Contract review
必须识别以下风险类型：
- 霸王条款
- 权利义务不对等条款
- 免责或责任过度限制条款
- 违约责任明显失衡条款
- 自动续约、单方变更、争议解决不公平条款

每个风险点必须包含：
- 条款摘录
- 风险等级（高/中/低）
- 风险说明
- 修改建议
- 可替换条款（可直接粘贴）

## Drafting
若起草《民事起诉状》，至少包含：
- 原告信息（缺失则标注“待补充”）
- 被告信息（缺失则标注“待补充”）
- 诉讼请求
- 事实与理由
- 证据清单

## Missing-data behavior
- 信息不足时继续输出初稿，不中断
- 额外附“待补充信息清单”

## Template priority

查找模板的优先顺序：
1. `assets/templates/official/` — 官方法院 `.docx` 模板
2. `assets/templates/official/samr/` — SAMR 合同模板（可通过 `fetch_samr_template.py` 按需下载）
3. `assets/templates/` — 通用 Markdown 模板

## Pre-export checklist

交付前逐项检查：
- [ ] 当事人名称前后一致
- [ ] 金额、日期数字一致
- [ ] 管辖法院名称完整
- [ ] 附件/证据清单完整
- [ ] `[待补充]` 标记已标注所有缺失信息

## Auto DOCX rule
- 只要本流程生成了任何交付文件（报告、起诉状、律师函等），必须自动导出 `.docx`。
- 回复中必须包含导出路径与导出状态。
