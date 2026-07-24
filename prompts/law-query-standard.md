# 法条检索标准（律言 — 本地知识库模式）

## 检索方式
律言采用本地知识库 + AI 模型知识的混合检索模式，无需外部 API。

### 本地知识库检索
```bash
# 脚本检索（推荐）
python scripts/query_local.py -q "民法典 租赁合同" --type law -o result.json

# 或直接查阅知识文件
knowledge/civil_code.md      — 民法典核心条文
knowledge/labor_law.md       — 劳动与社保法律
knowledge/consumer_and_tort.md — 消费者权益与侵权
knowledge/procedure_law.md   — 诉讼程序法律要点
```

### AI 模型知识补充
当本地知识库未覆盖所需法条时，使用 AI 模型知识补充，但必须：
- 标注 "[AI知识库补充，未经本地条文验证]"
- 建议用户通过国家法律法规数据库（https://flk.npc.gov.cn/）核对

## 知识库覆盖范围
| 文件 | 法律领域 | 条文数 |
|------|---------|-------|
| civil_code.md | 民法典（总则/物权/合同/人格权/婚姻家庭/继承/侵权） | 82条 |
| labor_law.md | 劳动合同法/劳动争议仲裁法/工伤保险条例/工资支付规定 | 29条 |
| consumer_and_tort.md | 消费者权益保护法/食品安全法/网络侵权 | 15条 |
| procedure_law.md | 民事诉讼法/仲裁法/诉讼时效 | 25+要点 |

## Interpretation standard
- 法条原文必须准确，不得改写原文内容
- 按效力位阶排序展示：
  - 法律
  - 行政法规
  - 地方法规（含地方性法规、地方政府规章）
- 每条法条后必须给出场景化解读：
  - 适用前提
  - 支持点
  - 不利点
  - 举证建议
