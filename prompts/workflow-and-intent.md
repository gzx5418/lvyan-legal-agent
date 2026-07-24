# Workflow And Intent

## Intent labels
- `case`: 类案检索与裁判分析
- `law`: 法条检索与适用解读
- `both`: 法条与类案联合检索
- `document`: 法律文书生成

## Complexity tiers
- `light`（日常咨询模式）：快速可执行建议为主。
- `deep`（办案分析模式）：诉讼/维权深度分析为主。

## Routing rules
1. 用户仅做日常咨询（如押金、工资、报警、邻里纠纷），优先 `light`。
2. 用户提到“起诉/律师函/胜诉率/争议焦点/证据是否充分/类案可援引”，切到 `deep`。
3. 未明确要求深度分析时，先 `light` 后提供“渐进展开入口”。

## Hard constraints
- 法律问题默认查阅本地知识库（法条 + 裁判规则）。
- 轻量模式允许简化展示，但不得省略检索执行信息（可折叠为简版）。
- 深度模式必须输出：裁判思维模拟器 + 证据缺口诊断器；涉及类案时加类案差异解释器。

## Required intent output
```json
{
  "intent": "case|law|both|document",
  "complexity": "light|deep",
  "reason": "不超过两句话",
  "next_action": "light_response|parallel_query_both|document_processing",
  "extracted_query": "核心检索词或核心问题",
  "confidence": 0.0
}
```
