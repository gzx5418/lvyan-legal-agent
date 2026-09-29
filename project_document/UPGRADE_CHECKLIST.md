# 律言平台化升级 - 执行清单(UPGRADE CHECKLIST)

> 创建时间: 2026-09-28
> 配套计划: `docs/plans/2026-09-28-platform-upgrade.md`(设计依据与验收标准,本文档只管"做什么、做到哪了")
> 总计: 4 期 / 7 方向 / 57 个原子步骤(v2,2026-09-28 修订)
>
> ## 执行状态总览
> - **第一期(类案 0→1,要素化 schema 起步)**: 待开始(步骤 U-01 ~ U-17)
> - **第 1.5 期(Retrieval 2.0 / Legal Issue Frame)**: 待开始(步骤 U-47 ~ U-54)——v2 新增
> - **第二期(可验证推理 + 合同 + 评测对标)**: 待开始(步骤 U-18 ~ U-32、U-55 ~ U-57)
> - **第三期(平台化)**: 待开始(步骤 U-33 ~ U-38、U-41 ~ U-46)
>
> ## v2 修订记录(2026-09-28,吸收外部评审)
> - 核心判断调整:检索天花板优先于生成模型(Legal RAG Bench 2026:检索影响 > 生成模型;自身 pipeline_eval 劳动案由低分全是检索失败佐证)→ 新增第 1.5 期"Retrieval 2.0 / Legal Issue Frame"(U-47~U-54);LEGAL_CHAT_MODEL 降为实验项(U-19)。
> - U-01 schema 直接要素化(避免日后迁移);U-03 数据源改 LeCaRDv2 为主 + 同罪名捷径警示;U-07 关联难负例(U-54)。
> - 新增 U-55 文档双通道、U-56 EvidenceSpan 坐标化、U-57 合同条款理解层;U-40 重塑为轻量法律关系图并前移至第二期。
>
> ## 使用约定
> 1. 完成一步:勾选 `[x]` 并在行尾补 `✅ <commit-hash> <日期>`;部分完成标 `[~]` 并写明卡点。
> 2. 提交信息带清单 ID,如 `feat(cases): A2-1 类案索引灌库管线 (#U-05)`。
> 3. 每步通用验收(除特别说明):`pytest tests/ -q -m "not slow"` 全过、`ruff check` + `ruff format --check` 全绿、`run_regression` PASS、`pipeline_eval` 0 失败。
> 4. 新发现的问题/想法追加到文末 Backlog 区,不打乱既有编号。
> 5. 依赖未完成的步骤不得开始(依赖以"⇐ U-xx"标注)。

---

# 第一期:类案从 0 到 1 + 智能收尾(2-4 周)

## A. 类案检索(A2 接线 + A3 审计呈现)

**[x]** **U-01** - 类案数据 schema 定义(v2 要素化) ✅ 48c3f77
- File: `src/lvyan/schemas/case.py` 或新建 `src/lvyan/schemas/case_document.py`
- 定义 `CaseDocument` 模型,基础字段:case_id / case_number / court / case_type(案由)/ effective_level(指导性案例|参考案例)/ brief_facts / ruling_summary / disputed_focus / judgment_date / source_url / source_name / content_hash
- **v2 必含要素化字段(一次到位,避免日后迁移;对齐 JUREX-4E 思想但自建 schema)**:legal_elements(构成要件列表)/ legal_issues(争议焦点)/ claims(诉请/请求权)/ defenses(抗辩)/ evidence_summary(证据概要)/ reasoning_spans(裁判理由段落定位 SpanRef)/ cited_statutes(引用法条)
- 与 `CaseAuthority` 的映射函数(检索结果 → 领域模型,对齐 `version_aware` 的 chunk→Authority 模式)
- 依赖:无
- 验收:模型可 JSON 序列化;与 curated 桩数据双向转换测试;要素化字段可空但类型稳定
- 与 `CaseAuthority` 的映射函数(检索结果 → 领域模型,对齐 `version_aware` 的 chunk→Authority 模式)
- 依赖:无
- 验收:模型可 JSON 序列化;与 curated 桩数据双向转换测试

**[x]** **U-02** - OpenSearch 类案索引建索引脚本 ✅ 48c3f77(CI 集成测试待 external-services job 验证)
- File: `src/lvyan/scripts/ingest_cases.py`(新建,参照 `ingest_laws.py:526-608` 的 bulk 管线)
- `legal_cases` 索引 mapping(中文 analyzer 与法条索引一致);幂等创建 + 全量重建
- 支持 `--source lecard|cail|curated` 与 `--batch-size`
- 依赖:U-01;需 OpenSearch 可用(compose 已含,本机可跳过并在 CI external-services job 补)
- 验收:灌库后 `count` 断言;重复执行幂等

**[x]** **U-03** - LeCaRDv2 / CAIL 公开数据适配器(v2 更换主数据源) ✅ 3f397b7(姓名启发式为文档化已知限制,数据集下载由部署方执行)
- File: `src/lvyan/scripts/case_datasets.py`(新建)
- **LeCaRDv2 为主**(800 queries / 55,192 候选案例,源自 430 万刑事判决书,法律专家按 characterization / penalty / procedure 三维标注)+ CAIL 公开赛题数据补充 → `CaseDocument` 流式转换;字段脱敏(当事人姓名 → 角色 placeholder,与 privacy.redact_privacy 口径一致)
- **已知陷阱**:2026 中文类案检索分析指出 LeCaRDv2 上"同罪名"即可解释大部分排名效果(同罪名+BM25 恢复强模型大部分提升)——灌库时保留 charge/case_type 元数据供难负例评测(U-54)使用
- 依赖:U-01
- 验收:抽样 100 条转换零失败;当事人姓名零残留断言;charge 字段完整率断言

**[x]** **U-04** - `MultiSourceRetriever` 接入检索主链 ✅ 7afce8b(build_default_retriever 组源;节点透传 case_type;真实 OpenSearch 环境联调待 CI external-services)
- File: `src/lvyan/retrieval/case_source.py`(已有多来源抽象,未接线)、`src/lvyan/tools/cases.py`
- `search_cases` 切换:OpenSearch(可用时)→ curated 精编兜底;OpenSearch 不可用降级路径与现有"类案参考规则"标注兼容
- `parallel_retrieval` 的类案 job 透传 `case_type`/`user_goal`(已有通道)
- 依赖:U-02
- 验收:OpenSearch 在/不在两种环境下节点测试双路径全过;输出标注随来源切换(真实案号 → "类案(i, 案号)";兜底 → "类案参考规则(非真实案例检索)")

**[x]** **U-05** - 类案引用审计器 ✅ 6653b15
- File: `src/lvyan/nodes/citation_verifier.py`、`src/lvyan/validators/`(新建 `case_citation.py`)
- 案号存在性核验(输出中案号须在 `state.cases` 中,对齐法条 not_found→fabricated 语义)与案由一致性校验(类案案由 vs triage.case_type 偏离 → warning)
- 依赖:U-04
- 验收:伪造案号用例(输出含检索结果外的案号)被拦;`tests/security/test_citation_forgery.py` 增类案用例

**[x]** **U-06** - 类案效力分级呈现 ✅ 2c8da0a
- File: `src/lvyan/nodes/composer_common.py`(`_format_cases`)
- 指导性案例 > 参考案例标注;节尾披露按来源区分(真实库 → "请核对人民法院案例库";curated → 维持现状文案)
- 依赖:U-04
- 验收:两类来源快照测试

**[x]** **U-07** - 金标集类案维度用例 ✅ 2d16008(类案命中指标待 CI 真实 OpenSearch 出首份报告)
- File: `tests/evals/golden_set.json`
- 新增 ≥10 条类案检索用例(案由覆盖与既有 7 类一致 + 刑事起步),`expected_cases` 字段(对齐 `expected_statutes` 结构)
- 依赖:U-05
- 验收:`retrieval_eval` 增类案指标(Recall@k/MRR)报告;CI 全绿;**v2:难负例维度由 U-54 补齐,本步先建普通基线防止只刷 LeCaRDv2 分数**

## B. 模型层(B1 四节点 LLM 化,向 IssueFrame 收敛)

**[x]** **U-08** - jurisdiction_triage LLM 化收尾 ✅ 8c2ecc8
- File: `src/lvyan/nodes/triage.py`(已有 `_try_llm_triage`,补交叉校验缺口)
- LLM 案由/复杂度与规则结果冲突时的双向消解(现状:仅 LLM 误判 document 可降级;规则误判不可纠正——二轮审查确认);负向用例("收到律师函怎么办"不得判 document)
- 依赖:无(prompt_registry 已有)
- 验收:`tests/agent/test_triage_fact.py` 增冲突消解双向用例;离线降级路径不变

**[x]** **U-09** - missing_fact_assessor LLM 化 + 追问路径激活 ✅ 9b49a34
- File: `src/lvyan/nodes/planner.py`(`missing_fact_assessor` 现状:missing_facts 非空即早退,LLM 几乎不运行)
- LLM 评估在"案由已识别但关键事实缺失"时运行;blocking 白名单仅限时效起算类字段(injury_date/breach_date 等,复用 triage 的 urgency 信号)
- 依赖:无
- 验收:时效临近场景("超过诉讼时效了吗")触发 blocking 追问的端到端用例;非紧迫场景不追问(回归)

**[x]** **U-10** - evidence_analyzer LLM 化 ✅ c989aca
- File: `src/lvyan/nodes/evidence_analyzer.py`(LLM 只能在既有 requirement_id 内修正——保持)
- 依赖 checklist 覆盖 12 案由(已补合同纠纷/婚姻家庭/知识产权);LLM 置信度修正带去重合并
- 依赖:无
- 验收:三案由清单生成快照;未知 ID 丢弃回归不破

**[x]** **U-11** - authority_resolver LLM 化 ✅ c989aca
- File: `src/lvyan/nodes/evidence_analyzer.py`(authority_resolver 同文件)
- LLM 辅助多版本/层级冲突的裁决建议;确定性 `verify_statute_status` 仍握否决权(版本判定单一来源已统一到 version_aware)
- 依赖:无
- 验收:新法/旧法冲突用例(民法典 vs 合同法,as_of 双侧)各得正确结论

## B. 模型层(B4 弃答语义)

**U-12** - 弃答状态与审计字段
- File: `src/lvyan/schemas/output.py`、`src/lvyan/graph/state.py`、`src/lvyan/nodes/citation_verifier.py`
- `citation_audit` 增 `abstain_recommended: bool` 与 `unverifiable_citations: list[str]`;2 轮重检索后仍不通过时置位(替代现行"强制通过",风险标注保留)
- 依赖:无
- 验收:强制通过路径的既有测试改为弃答断言;`route_after_citation` 不受影响(路由仍进 guardrail)

**U-13** - composer/finalizer 弃答渲染
- File: `src/lvyan/nodes/composer.py`、`composer_light.py`、`composer_deep.py`、`legal_answer_finalizer.py`
- 弃答卡片:分析正文完整保留 + 结论区替换为"现有检索结果不足以支撑结论" + 缺失引用清单 + 补充建议(哪些案情/证据能解锁);`legal_answer.abstained` 结构化字段;SSE 事件透传
- File(前端): `templates/static/app.js` 弃答卡片渲染
- 依赖:U-12;**v2 建议在 U-52(检索置信度)之后执行,弃答触发条件从"引用审计轮次"升级为消费 retrieval_confidence**
- 验收:端到端用例(构造检索必失败)→ 前端契约测试;HITL 不适用于弃答路径断言

## D. 评测(D3 真模型夜间评测)

**U-14** - 真模型评测入口
- File: `tests/evals/live_eval.py`(新建)
- 复用 pipeline_eval,`--require-real`:网关不可用直接退出码 2(区分于失败);指标含三报告 + 弃答率 + 成本/延迟分布;报告落 `outputs/evals/live-<date>.json`
- 依赖:无
- 验收:离线跑退出码 2;网关可用时产出报告

**U-15** - 夜间 workflow
- File: `.github/workflows/nightly-eval.yml`(新建)
- cron 夜间 + workflow_dispatch;网关 secret 从仓库 secrets 注入;漂移对比(与最近 7 天均值)超阈值打 issue 标签;不阻塞 PR 门禁
- 依赖:U-14
- 验收:手动 dispatch 首跑产出报告

**U-16** - 金标集劳动案由离线盲区修复
- File: `tests/evals/golden_set.json`、`tests/evals/pipeline_eval.py`
- labor_001-004 离线低分(既有现象,二轮基线对比确认):查询措辞与规则降级检索的匹配校准,或标注 `requires_llm: true` 仅入真模型评测
- 依赖:无
- 验收:离线 pipeline_eval 全维度无 0 分用例(或明确标注豁免)

## F. 工程收尾

**U-17** - server.py 附件链路拆分(F5)
- File: 新建 `src/lvyan/api/attachments.py`;`server.py` run/upload 端点的附件校验/vault 写入/回滚内联逻辑(~230 行)迁出
- 行为等价重构,现有 `test_attachment_markdown.py`/`test_api_safety.py` 全过为准;纯机械传参,不改语义
- 依赖:无
- 验收:server.py 减 ~200 行;附件相关测试零修改通过(允许 import 路径更新)

---

# 第 1.5 期:Retrieval 2.0 / Legal Issue Frame(3-4 周,v2 新增)

> 核心论据:Legal RAG Bench 2026——检索模型对最终 RAG 质量的影响明显大于生成模型,大量"幻觉"首先是检索失败;自身 pipeline_eval 劳动案由低分全为检索未召回。本期把"文档级检索"升级为"要件级检索 + 精确 Span",是律言最难被复制的壁垒。

**U-47** - LegalIssueFrame 数据模型与状态接入
- File: `src/lvyan/schemas/`(新建 `issue_frame.py`)、`src/lvyan/graph/state.py`、`src/lvyan/schemas/case.py`
- 模型字段:legal_relation(法律关系)/ claims(诉请与请求权基础)/ elements(构成要件,逐条可检索可举证)/ disputed_facts / defenses / evidence(要件↔证据映射)/ applicable_rules(候选规范)/ temporal_context(对接 law_as_of_date)
- GraphState 增 `issue_frame` 字段(覆盖语义;加入 GraphState↔CaseState 一致性测试白名单评估——属运行域还是领域模型需判定);preflight 复位清单同步
- 依赖:无
- 验收:与既有字段映射文档(disputed_facts/evidence_requirements/missing_facts ≈ 60% 雏形,明确升级关系);一致性守护测试更新

**U-48** - issue_frame 节点(规则 + LLM 双路径)
- File: `src/lvyan/nodes/issue_frame.py`(新建)、`graph/builder.py`(插入主链:fact_extractor → missing_fact_assessor → **issue_frame** → planner)、`llm/prompt_registry.py`
- LLM 产出 frame;规则降级路径从案由模板 + 既有 reasoner issues 结构合成;**B1 的四节点 LLM 化工作向本节点收敛**(triage→legal_relation、missing_fact_assessor→elements 缺失、evidence_analyzer→evidence 映射、authority_resolver→applicable_rules);规则否决权框架不变(LLM 只能修正不能创造)
- 依赖:U-47
- 验收:劳动纠纷示例端到端(违法解除赔偿金 8 要件);离线规则路径产出完整 frame;与 B1 步骤(U-08~U-11)的合并/拆分在执行时按实际收敛

**U-49** - Multi-query 检索规划器
- File: `src/lvyan/retrieval/query_rewriter.py` 扩展、`nodes/planner.py`
- 从 IssueFrame 生成 3~5 路查询:原话 + 法律术语化 + 逐要件查询;复用 parallel_retrieval 双层线程池并行;计划步骤携带 element 关联
- 依赖:U-48
- 验收:单查询 → 多查询的 planner 测试;成本守卫(policies)对多路查询的预算适配

**U-50** - 要件级检索(element-aware retrieval)
- File: `src/lvyan/retrieval/`(扩展 hybrid/version_aware;类案走 U-04 的 OpenSearch 通道)
- 法条:要件关键词 ↔ 条文映射检索;类案:CaseDocument.legal_elements 字段索引与召回;metadata filter 强化(案由/法院层级/效力等级/时点)
- 依赖:U-49、U-04
- 验收:同要件在法条与类案两侧均可召回;违反要件过滤用例

**U-51** - Support Span Extractor(精确片段)
- File: `src/lvyan/retrieval/span_extractor.py`(新建)、`schemas/`(SpanRef: source_type/statute|case, source_id, locator(条/款/项 or 段落区间), text)
- 法条侧:条内款/项粒度切分(chunk 索引已按条);类案侧:裁判理由段落定位(启发式切分起步:"本院认为/经审理查明"锚点);引用携带 SpanRef 进最终输出
- 依赖:U-50
- 验收:Span 定位准确率抽检(法条 ≥95%,类案段落 ≥80%);SpanRef 随 citation 流转的端到端测试

**U-52** - 检索置信度信号
- File: `src/lvyan/retrieval/confidence.py`(新建)、`nodes/retrieve_statutes.py`
- 三信号:candidate coverage(各要件是否都有候选)/ score margin(top1 与阈值距离)/ 后续 entailment 通过率预留;输出 `retrieval_confidence: sufficient|insufficient|failed` 进状态
- 依赖:U-50
- 验收:三档构造用例;低置信触发 query retry(改写而非硬答)的节点测试

**U-53** - precise-span 评测指标(LegalBench-RAG 式)
- File: `tests/evals/retrieval_eval.py`
- 新指标:span-level precision/recall(召回的是最小支撑片段而非整文);报告标注与文档级指标的对比
- 依赖:U-51
- 验收:金标集抽 10 条人工标注支撑片段作为种子;指标可复现

**U-54** - 难负例评测集
- File: `tests/evals/hard_negatives.json`(新建)、`retrieval_eval.py` 接入
- 四类:同罪名 hard negatives / 同案由 hard negatives / 相似事实不同要件 / 不同事实同一法律问题;针对 LeCaRDv2"同罪名捷径"设计
- 依赖:U-03
- 验收:同罪名+BM25 基线在难负例集上**不再**恢复大部分提升(即指标能区分真要件检索与罪名捷径);报告独立呈现

---

# 第二期:可验证推理 + 合同审查 + 评测对标(1-2 月)

> v2 执行顺序建议:U-57(条款理解层)→ U-22~U-24(Playbook)→ U-25~U-27 → U-55/U-56(文档解析)→ U-20/U-21(蕴含)→ U-28~U-31(评测)→ U-32;**U-18/U-19 为实验项,最后执行**(依据:检索优先于模型替换)。

## B. 模型层(B3 全量蕴含 + 实验项)

**U-18** - LEGAL_CHAT_MODEL 路由
- File: `src/lvyan/config.py`、`src/lvyan/llm/client.py`
- 新增 `LEGAL_CHAT_MODEL`(默认空 = 全部走 CHAT_MODEL);`chat_json` 按节点角色参数(`role="reasoning"|"triage"`)选模型;不可用回退 CHAT_MODEL;trace 记录实际模型
- 依赖:无
- 验收:路由选择/回退单测;.env.example 文档

**U-19** - 法律领域模型对比评测(v2 降级为实验项)
- 依赖:U-18、U-14;**前置:P1.5 检索 2.0 达标**(依据 Legal RAG Bench 2026:先提高检索天花板,再讨论生成模型天花板)
- 智海-录问 / ChatLaw(经魔搭或网关部署)与当前 CHAT_MODEL 在金标全集对比;结论写入 `docs/evals/legal-model-comparison.md`
- 验收:对比报告含准确率/虚构率/成本/延迟四维;若提升不显著则记录结论并关闭 LEGAL_CHAT_MODEL 维护面

**U-20** - 语义蕴含全量化(B3)
- File: `src/lvyan/validators/grounding.py`、`src/lvyan/nodes/citation_verifier.py`
- 移除前 20 截断(批量分批,每批 ≤20);LLM 不可用时引用审计 `passed=None(未核验)`;输出层显著披露"法条引用未经语义核验"
- 依赖:无
- 验收:全量/降级双路径测试;成本回归(单 run LLM 调用数不翻倍——灰带优先策略保留)

**U-21** - 成本守卫与蕴含审查协同
- File: `src/lvyan/graph/policies.py`
- 全量蕴含后单 run 成本上限重估;$2 预算下的降级序(蕴含审查 → 重检索 → 弃答)明确化
- 依赖:U-20
- 验收:预算耗尽路径的端到端用例

## C. 合同审查(C1-C2 MVP)

**U-22** - Playbook 数据模型 + 迁移
- File: `migrations/014_contract_playbooks.sql`(RLS 策略带 DROP IF EXISTS 守卫,模式照抄 007/008);`src/lvyan/memory/case_workspace.py` 增表与租户注入(模式照抄现有 Store)
- 字段:playbook_id / user_id / contract_type / clause_type / preferred / fallback / walk_away / legal_basis(法条引用) / updated_at
- 依赖:无
- 验收:迁移幂等(双通道重放不炸——013 预登记已覆盖);RLS 未注入 GUC 时 INSERT 失败断言

**U-23** - 默认 playbook 种子数据
- File: `knowledge/playbooks/labor_contract.yaml`、`sales_contract.yaml`(新建)
- 劳动合同(违约金/竞业/试用期/社保)与买卖合同(验收/付款/违约责任/争议解决)各 ≥6 类条款三档立场,法条依据用 overrides 后的权威来源
- 依赖:U-22
- 验收:法条依据全部通过 verify_statute_status;启动时可选灌库脚本

**U-24** - Playbook CRUD 端点
- File: `src/lvyan/api/routes_workspace.py`
- GET/PUT/DELETE `/api/cases/{id}/playbook`;ownership 404 语义与现有 16 端点一致
- 依赖:U-22
- 验收:跨租户 404 测试;白名单校验( sanitize 模式照抄 preferences)

**U-57** - 条款理解层(v2 新增,Playbook 的前置)
- File: `src/lvyan/nodes/contract_understanding.py`(新建)
- 三级流水:Clause Segmentation(条款切分,编号/层级感知)→ Clause Classification(付款/违约责任/解除/知产/保密/竞业限制/争议解决/责任上限/自动续期/赔偿等标准类目)→ Term Extraction(金额/期限/比例等结构化要素)
- 思路对齐 CUAD/MAUD 的条款级标注实践;**ContractEval 2025 发现:推理模式不总提高条款风险判断正确率**——确定性切分/分类规则前置,LLM 只做语义归类兜底
- 依赖:无
- 验收:三类合同(劳动/买卖/租赁)切分+分类抽检准确率;结构化 term 抽取(违约金比例/期限)断言

**U-25** - 偏差检测节点
- File: `src/lvyan/nodes/contract_reviewer.py`(新建;MCP/CLI 不接,走 workspace 流程)
- 逐条款:LLM 比对 ↔ playbook → 三色 + 建议替换文本 + 依据;确定性规则(金额上限/期限)前置过滤降 LLM 量;结果入 review_findings 现有状态机
- 依赖:U-23、**U-57(v2:先有条款理解层再做偏差比对)**
- 验收:违约金超限/竞业过宽/无偏离三类用例;离线降级(规则-only)路径可用

**U-26** - 红线建议与 HITL 审批
- File: `src/lvyan/nodes/contract_reviewer.py`、`src/lvyan/api/server.py`(review 流接线)
- "不可接受"条款的替换建议走 pending_human_approval(interrupt 模式复用);批准后生成红线版 DOCX(export.py 扩展:保留修订痕迹)
- 依赖:U-25
- 验收:审批 approve/reject/edit 三路径端到端;DOCX 含修订标注快照

**U-27** - 合同审查金标用例
- File: `tests/evals/golden_set.json`(新 category:合同审查)
- ≥10 条:标准条款(应 pass)/偏离(应 flag)/不可接受(应 escalate),`expected_findings` 字段
- 依赖:U-25
- 验收:run_regression 扩展支持合同审查维度

## D. 评测(D1/D2/D4)

**U-28** - 外部基准接入(LawBench/LexEval 子集)
- File: `tests/evals/external/`(新建;独立入口,月度跑,不进 PR 门禁)
- LawBench 法律问答 + 法条检索子集、LexEval 记忆/理解层;数据下载脚本带版本固定
- 依赖:无
- 验收:月度 workflow 产出报告;外部数据缺失时优雅跳过

**U-29** - 轨迹级评测入 CI(D2)
- File: `tests/evals/agent_eval.py`(已有)→ 接入 pipeline_eval 报告与 CI
- 指标:节点访问序列、无效工具调用数、循环率、(U-13 后)弃答正确率;阈值宽松起步
- 依赖:U-13
- 验收:CI 报告含轨迹段;阈值违反仅告警不打红(首月观察后收紧)

**U-30** - 金标扩容 100+(D4)
- File: `tests/evals/golden_set.json`
- 23 → 100+:案由 × 难度(规则可答/需推理/需类案)× 时点三维分层;分层单独出报告
- 依赖:U-07(类案用例就位)
- 验收:各层 ≥15 条;分层报告字段齐备

**U-31** - 评测报告聚合页
- File: `src/lvyan/scripts/eval_report.py`(新建)
- 汇总离线/真模型/外部基准三次报告为单页 Markdown(趋势 + 漂移),落 `outputs/evals/summary.md`
- 依赖:U-15、U-28
- 验收:三源聚合快照测试

## F. 工程收尾(续)

**U-32** - SSE 断线重连去重(F6)
- File: `src/lvyan/api/run_context.py`、`src/lvyan/api/server.py`(stream 端点)
- RunContext 事件递增 seq → SSE `id:` 行;stream 读 `Last-Event-ID` 只重放其后条目;final 哨兵幂等
- 依赖:无
- 验收:重连不重复投递测试;旧客户端(无 Last-Event-ID)行为不变

**U-55** - 文档解析双通道路由(v2 新增,F5,原方案低估项)
- File: `src/lvyan/tools/document_router.py`(新建)、`tools/file_converter.py` 扩展
- born-digital(有文本层 PDF/DOCX/XLSX)→ MarkItDown(现状);扫描/拍照/盖章件(文本层检测:PDF 抽取字符数阈值)→ **VISION_MODEL 网关通道(已有,直接复用)**;Docling / PaddleOCR-VL 作为可选 extra 依赖(pyproject `[project.optional-dependencies] documents-ai`),不设硬依赖
- 路由决策记录进附件元数据(转换通道 + 置信度)
- 依赖:无
- 验收:扫描合同/法院 PDF/微信截图三类样本端到端;无文本层 PDF 正确路由到视觉通道;纯 born-digital 路径零回归

**U-58** - 轻量法律关系图(v2 新增,原 U-40 升格前移)
- File: `src/lvyan/graph/legal_relation.py`(新建,in-repo 边表 JSON/networkx,不上图数据库)
- 节点与边:Claim →requires→ LegalElement →supported_by→ Fact →proved_by→ Evidence →governed_by→ Statute →interpreted_by→ JudicialInterpretation →illustrated_by→ Case;StatuteVersion(effective_from/to、replaces、amended_by、references)——直接复用 version-aware 检索的既有语义
- 检索路由:普通咨询 Hybrid RAG(现状);复杂多法条(多部法律交叉/法条竞合)走图增强 multi-hop;依据法律 GraphRAG 研究:层级结构/交叉引用/时间版本三要素
- 依赖:U-47(IssueFrame 提供节点实体)
- 验收:法条竞合场景(交通事故+工伤)multi-hop 检索用例;图构建随 run 增量更新;普通咨询路径零回归

**U-56** - EvidenceSpan 坐标化与前端跳转高亮(v2 新增,F8)
- File: `src/lvyan/schemas/attachment.py`(EvidenceSpan: file_id/page/bbox/paragraph_id/text)、`tools/document_router.py` 输出携带坐标、引用结构增 SpanRef → 前端"点击依据 → 跳转页码/段落高亮"
- 与 U-51 的 SpanRef 体系合流(法条:条/款/项;类案:判决段落;用户文档:页/bbox/段落)
- 依赖:U-55、U-51
- 验收:合同第 7 页第 13.2 条点击跳转高亮的前端契约测试;坐标数据随附件持久化

---

# 第三期:平台化(季度)

> v2 调整:原 U-40(争议焦点图谱)已升格为 U-58(轻量法律关系图)前移至第二期;本期为 U-33 ~ U-38、U-41 ~ U-46。

## C. 合同审查(C3)

**U-33** - 文书对比视图
- File: `src/lvyan/tools/document_diff.py`(新建)+ workspace 端点
- 两版逐段 diff(段落对齐 + 修订标注渲染 Markdown);前端呈现
- 依赖:无(依赖 document_versions 既有表)
- 验收:增/删/改三类 diff 快照;大文档(5 万字)性能 <2s

## E. 多智能体与长任务

**U-34** - 诉讼准备包流水线(E1)
- File: `src/lvyan/graph/pipelines.py`(新建,复用现有节点)
- 证据梳理 → 时效测算(calculators)→ 管辖分析 → 文书起草 → 材料清单;输出打包(报告 + DOCX + 清单);SSE 多阶段进度
- 依赖:U-13(弃答语义,缺证据时包内标注)
- 验收:劳动争议端到端打包用例;各阶段可单独重跑

**U-35** - 批量合同审查(E2)
- File: `src/lvyan/api/server.py`(upload 批次语义)、`src/lvyan/nodes/contract_reviewer.py`
- 目录级上传(批内逐份校验,单份失败不阻断)→ 逐份走 C 管线 → 风险矩阵汇总报告;并发受现有信号量约束
- 依赖:U-26
- 验收:20 份混合批次用例(含 1 份损坏文件);SSE 进度事件完整

**U-36** - 结构化案件档案(E3)
- File: `src/lvyan/memory/case_workspace.py`、`src/lvyan/nodes/legal_answer_finalizer.py`、`tools/conversation_history.py`
- facts/证据/文书/时间线入 workspace 案件绑定;会话摘要改"结构化档案 + 近期对话"双通道(3 轮/800 字截断的早期事实丢失问题闭环)
- 依赖:无
- 验收:跨 5 轮会话首轮事实仍可召回的端到端用例

## A. 类案(A1 正式接入)

**U-37** - 人民法院案例库数据合作评估
- 产出:数据获取路径决策文档(官方合作/授权采集/暂缓);schema 兼容性确认(`CaseDocument.source_authority` 预留字段已覆盖)
- 依赖:产品/商务输入;技术侧无阻塞
- 验收:决策文档评审通过

**U-38** - 案例库数据接入(依 U-37 结论)
- File: `src/lvyan/scripts/case_datasets.py` 增适配器;灌库走 U-02 管线
- 依赖:U-37、U-02
- 验收:抽样核验与官方页面一致;效力级别字段准确

## B. 模型层(延伸)

**U-39** - 推理深度分级(deep+ 模式)
- File: `src/lvyan/graph/builder.py`、`config.py`
- deep 增加可配深度(多问题分解/多轮检索);light/deep/document 三档语义不变,deep 内部按 `analysis_depth` 参数化
- 依赖:U-20(成本模型重估后)
- 验收:分级成本/延迟曲线报告;深度提升在金标需推理层有效

**(v2 移出)U-40 原第三期条目已重塑为"轻量法律关系图"并前移至第二期,见 U-58**

## F. 工程收尾(续)

**U-41** - user_preferences DB 化(F1)
- File: `migrations/015_user_preferences.sql`、`src/lvyan/memory/user_preferences.py`(Postgres 实现)、`server.py`(按 metadata_store 类型切换)
- sanitize/白名单逻辑原样复用;本机文件实现保留为 dev 默认
- 依赖:无
- 验收:双实现行为一致测试;多实例一致性(PG job 内验证)

**U-42** - MinIO 对象存储接入(F2)
- File: `src/lvyan/memory/blob_store.py`(新建接口 + 本机/MinIO 双实现)、upload/export 路径切换
- 本机实现为默认(零依赖);MinIO 按 .env 配置启用
- 依赖:无
- 验收:双实现契约测试;多实例附件 404 场景在 MinIO 模式下消失

**U-43** - web_search 人名脱敏(F3)
- File: `src/lvyan/tools/web_search.py`、`src/lvyan/validators/privacy.py`
- 按 F3 决策执行(保守启发式:称谓词/引号专名;或文档披露已知限制)
- 依赖:产品决策(U-37 同期提出)
- 验收:脱敏用例或披露文案落地

**U-44** - "已修改"状态口径(F4)
- File: `src/lvyan/retrieval/version_resolver.py`(`_STATUS_MAP` 增已修改→effective + superseded 推导)、`version_quality.py` 门禁联动
- 389 个文件回归现行法查询;version_quality 基线更新
- 依赖:无
- 验收:被排除的代表性法条(抽样 20)恢复召回;`sync_sources` 报告无新增缺陷

**U-45** - overrides 治理常态化(F7)
- File: `scripts`/workflow:月度 sync_sources 报告 + 版本质量基线对比,新增缺口生成 issue 清单
- 依赖:U-44
- 验收:月度 workflow 首跑产出缺口清单

**U-46** - 收尾与文档
- README 能力表更新(类案检索/合同审查/诉讼包);API 文档;/docs 端点注释;CHANGELOG
- 依赖:各期主体完成
- 验收:CI 全绿;README 与实现一致(抽查 5 项)

---

# Backlog(新发现问题追加区,不打乱编号)

> 格式:`- [ ] <日期> <一句话描述> (来源: <审查/评测/用户反馈>) —— 评估后编入某期或关闭`

- [x] 2026-09-28 **ruff format(0.16+)会格式化 .json 并输出尾随逗号**,曾损坏 golden_set.json——修复并约定:ruff format 不再对 *.json 调用;JSON 一律用 python json.dumps 写回
- [ ] 2026-09-28 `/tmp` 与 Windows Python 路径不兼容曾影响冒烟测试脚本(运维注意项,非代码问题)
- [ ] 2026-09-28 离线模式运行时 LangGraph checkpoint 反序列化告警(unregistered type ×8)——评估显式注册 allowed_msgpack_modules(来源:启动冒烟)
- [ ] 2026-09-28 `.env` 模型网关 key 失效(401)——运维:密钥轮转流程
