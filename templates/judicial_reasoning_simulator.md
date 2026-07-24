# 裁判思维模拟器报告

## 一、案件事实（左侧）
{{facts_summary}}

## 二、争议焦点与裁判倾向（右侧）
| 争议焦点 | 支持原告概率 | 支持被告概率 | 关键决定因素 |
|---|---|---|---|
| {{focus_1}} | {{plaintiff_prob_1}} | {{defendant_prob_1}} | {{key_factor_1}} |
| {{focus_2}} | {{plaintiff_prob_2}} | {{defendant_prob_2}} | {{key_factor_2}} |
| {{focus_3_optional}} | {{plaintiff_prob_3_optional}} | {{defendant_prob_3_optional}} | {{key_factor_3_optional}} |

## 三、焦点逐项推理
### 争议焦点1：{{focus_1}}
- 原告论点：{{plaintiff_argument_1}}
- 原告支持证据：{{plaintiff_evidence_1}}
- 原告可能被反驳点：{{plaintiff_rebuttal_risk_1}}
- 原告薄弱点：{{plaintiff_weakness_1}}
- 被告论点：{{defendant_argument_1}}
- 被告支持证据：{{defendant_evidence_1}}
- 被告可能被反驳点：{{defendant_rebuttal_risk_1}}
- 被告薄弱点：{{defendant_weakness_1}}
- 法官可能采纳路径：{{judge_path_1}}

### 争议焦点2：{{focus_2}}
- 原告论点：{{plaintiff_argument_2}}
- 原告支持证据：{{plaintiff_evidence_2}}
- 原告可能被反驳点：{{plaintiff_rebuttal_risk_2}}
- 原告薄弱点：{{plaintiff_weakness_2}}
- 被告论点：{{defendant_argument_2}}
- 被告支持证据：{{defendant_evidence_2}}
- 被告可能被反驳点：{{defendant_rebuttal_risk_2}}
- 被告薄弱点：{{defendant_weakness_2}}
- 法官可能采纳路径：{{judge_path_2}}

## 四、类案比较与规则适用
- 适用规则：{{applied_rules}}
- 类案比较结论：{{case_comparison}}

## 五、裁判倾向预测图
```mermaid
flowchart TD
  A[案件事实] --> B[争议焦点拆解]
  B --> C1[焦点1: {{focus_1}}]
  B --> C2[焦点2: {{focus_2}}]
  C1 --> D1[倾向: 原告 {{plaintiff_prob_1}} / 被告 {{defendant_prob_1}}]
  C2 --> D2[倾向: 原告 {{plaintiff_prob_2}} / 被告 {{defendant_prob_2}}]
  D1 --> E[综合裁判倾向: {{final_tendency}}]
  D2 --> E
```

## 六、下一步行动清单
- 24小时内：{{todo_24h}}
- 3天内：{{todo_3d}}
- 7天内：{{todo_7d}}

## 七、检索执行信息
- 主检索功能：{{primary_retrieval}}
- 双接口执行状态：{{retrieval_status}}
- fallback检索功能：{{fallback}}
- 说明：{{retrieval_note}}

# 风险提示
以上内容仅供信息参考，不构成正式法律意见。重大事项请咨询持证律师。
