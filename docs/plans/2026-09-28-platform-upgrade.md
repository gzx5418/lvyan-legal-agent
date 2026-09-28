# 律言平台化升级计划(Platform Upgrade Plan)v2

> 创建时间:2026-09-28 · **v2 修订:2026-09-28(吸收外部评审:检索优先于模型替换,新增方向 G"法律检索引擎 2.0")**
> 依据:两轮全模块代码审查修复完成(提交 `7d03f07`/`3ee2a85`/`a13b040`)+ 两轮外部对标调研。
> 配套执行清单:**`project_document/UPGRADE_CHECKLIST.md`**(原子步骤 + 进度追踪,推进时以清单为准勾选)。
> 本文件只讲"为什么与怎么做",不重复罗列步骤。

---

## 0. 背景与定位

两轮审查后,律言的**可信底座**已闭环:15 节点 LangGraph 图、版本感知检索、引用三报告审计、输出护栏、HITL 原子认领、RLS 全链路租户上下文、1046 测试、金标回归 + 管线评测 CI 门禁。在开源同类(LaWGPT / ChatLaw / DISC-LawLLM 等)中,这一层的工程纪律没有对手。

**核心定位(v2 明确,是全计划的试金石)**:

> 律言不是让大模型凭记忆回答法律问题,而是把用户问题转换为**法律争议焦点与构成要件**,从有效法律规范、真实案例和用户证据中**逐要件检索、核验**并生成**可追溯**的结论。

对标 Harvey / CoCounsel / Lexis+ 等平台型产品与 LawBench / LexEval / LegalBench-RAG 等评测体系,以及 2025-2026 法律 AI 的行业共识(检索失败弃答、生成后逐条核验、Playbook 合同审查、辅助型 AI 人机分工),差距集中在三轴:

1. **法律知识的另一半**:法条维度完整(版本感知是差异化优势),类案维度为零(仅精编规则桩);
2. **检索的深度**:文档级检索,未到**要件级检索**;给 LLM 的是整段材料而非最小支持片段(Span);
3. **场景的宽度**:咨询问答 + 文书生成已可用,合同审查(条款理解 + Playbook 偏差比对)、批量审查、诉讼准备包等工作流未覆盖。

**v2 关键判断调整(外部评审采纳)**:Legal RAG Bench(2026)的核心发现是——**检索模型对最终 RAG 质量的影响明显大于生成模型**,大量"大模型幻觉"首先是检索失败。我们自己的 pipeline_eval 也印证:劳动案由低分全是检索未召回,不是推理不行。因此"强 LLM + 错法条 ≈ 高质量地胡说;一般 LLM + 对法条反而更可靠"。**资源优先投给检索(方向 G),法律领域模型路由降为实验项**。

## 1. 目标与非目标

**目标**
- G1 类案检索从 0 到 1:真实类案语料入库(要素化 schema)、与法条同栈检索、案号防幻觉审计。
- **G6 法律检索引擎 2.0(v2 新增,核心竞争力)**:LegalIssueFrame 要素化 → 多路查询 → 要件级检索 → 精确 Span 召回 → 检索置信度。
- G2 推理深度:四个规则节点 LLM 化收尾(向 IssueFrame 收敛);语义蕴含全量化;检索失败弃答。
- G3 合同审查工作流:条款理解层(切分/分类/要素)→ Playbook 偏差比对 → 红线建议。
- G4 评测外部对标:LegalBench-RAG 式 precise-span 指标、难负例评测集、真模型夜间评测、金标扩容。
- G5 工程收尾:多实例一致性、文档解析双通道与证据坐标化、web_search 隐私、语料口径。

**非目标(明确不做)**
- 法律语料预训练/微调;~~法律领域模型路由作为主线~~(B2 降为实验项,见方向 B)。
- 自主多智能体 swarm;多 Agent 互聊式检索框架(复用现有 query_rewriter + parallel_retrieval 通道即可)。
- 图数据库(Neo4j 等):法律关系图先做 in-repo 轻量边表,验证价值后再考虑。
- 独立 Skill 框架抽象:能力模块化**并入 prompt_registry 演进**(见方向 B),不新建一层间接。
- 文档 AI 硬依赖:Docling/PaddleOCR-VL 作为可选 extra,扫描件先走已有 VISION_MODEL 通道。

## 2. 分期路线图(v2:在 P1 与 P2 之间插入 Retrieval 2.0)

| 期 | 主题 | 覆盖方向 | 出口标准 |
|---|---|---|---|
| **P1 第一期(2-4 周)** | 类案 0→1(要素化 schema 起步) | A1-A3 | 类案在 OpenSearch 真实语料上检索并过引用审计;CaseDocument 含要件/争议焦点/理由 Span 字段;LeCaRDv2 灌库 + 难负例评测基线 |
| **P1.5 Retrieval 2.0(3-4 周)** | LegalIssueFrame + 要件级检索(v2 新增) | G 全部 + B1 收敛 | 用户问题 → IssueFrame → 3-5 路法律查询 → 要件级召回 → 精确 Span(条/款/项、判决段落)→ 检索置信度;precise-span 指标入评测 |
| **P2 第二期(1-2 月)** | 可验证推理 + 合同 + 评测对标 | B3/B4、C1-C2、D、F 文档/图 | 全量语义蕴含 + 弃答(消费置信度信号);条款理解层 + Playbook MVP;文档双通道 + EvidenceSpan 坐标化;外部基准月报;轻量法律关系图 |
| **P3 第三期(季度)** | 平台化 | C3、E、A1 正式 | 诉讼准备包;批量审查;文书对比;人民法院案例库接入(需数据合作) |

依赖:P2 的弃答依赖 P1.5 的检索置信度;P2 合同依赖条款理解层;A1 正式接入依赖外部数据授权。

## 3. 各方向设计

### 方向 A:类案检索(G1)

**现状**:`tools/cases.py` 是精编桩;`retrieval/case_source.py` 多来源抽象未接线;`ingest_laws.py` 已有 OpenSearch bulk 管线;compose 已含 opensearch。

**设计**:
- **A2 OpenSearch 接线**:`legal_cases` 索引 + `MultiSourceRetriever` 进主链(OpenSearch → curated 兜底)。
- **A1 数据源(v2 调整)**:**LeCaRDv2 为主**(800 queries / 55,192 候选案例,430 万刑事判决书筛出,法律专家按 characterization / penalty / procedure 三维标注)+ CAIL 补充,替代原 LeCaRD。**已知陷阱**(2026 中文类案检索分析):LeCaRDv2 上"同罪名"即可解释大部分排名效果——同罪名 + BM25 就能恢复强模型的大部分提升。因此评测必须配**难负例**(见 D 方向),不做"只刷 LeCaRDv2 分数"的假象。
- **A3 审计与呈现**:案号存在性核验 + 案由一致性校验进 citation_verifier 骨架;效力级别分级呈现。
- **要素化 schema(前置,P1 就做)**:`CaseDocument` 直接携带 `legal_elements / legal_issues / claims / defenses / evidence_summary / reasoning_spans / cited_statutes` 字段(对齐 JUREX-4E 的要件标注思想但自建 schema),避免日后迁移。JUREX-4E 在 LeCaRDv2 上验证了要件表示的价值:BGE + 四要件表示比纯案件事实 embedding 的 NDCG@10 从 0.4737 提升到约 0.5295。
- 人民法院案例库(P3):schema 预留 `source_authority`,不做爬取。

### 方向 G:法律检索引擎 2.0(v2 新增,核心竞争力)

**现状与差距**:现在是"query → hybrid 检索 → rerank → 结果"的文档级检索;给 LLM 的是整段材料。LegalBench-RAG 的立场:法律 RAG 应评价是否精确找到**支撑回答的最小文本片段**,大段无关上下文增加成本、延迟与幻觉风险;COLIEE 已从案件检索发展到 **Case Entailment**(找案例中哪一段真正支持结论)。

**LegalIssueFrame(统一要件表示,由 JUREX 四要件推广到民事)**:

```text
LegalIssueFrame
├── legal_relation        法律关系
├── claims               用户请求/诉请(请求权基础)
├── elements             构成要件(逐要件可检索、可举证)
├── disputed_facts        争议事实
├── defenses              抗辩
├── evidence              对应证据
├── applicable_rules      候选规范
└── temporal_context      法律适用时点(对接 law_as_of_date)
```

劳动纠纷示例(违法解除赔偿金):要件 = ①劳动关系 ②是否解除 ③解除主体 ④解除理由 ⑤理由是否符合法定解除条件 ⑥通知/工会程序 ⑦工作年限 ⑧月工资基数。

**与现有状态的映射(约 60% 已具备,不是推倒重来)**:`disputed_facts` / `evidence_requirements(fact_to_prove)` / `missing_facts` / `law_as_of_date` / reasoner 的 `issues(rules/supporting_facts/counterarguments)` 已是框架的雏形;升级为**一等结构化对象**,由 issue_frame 节点产出(消费 triage/facts),供 planner(多路查询)与 evidence_analyzer(逐要件支撑)消费。

**检索管线(v2 目标形态)**:

```text
用户问题 → IssueFrame
   ↓ Multi-query Generator(复用 query_rewriter)
   原话查询 + 法律术语查询 + 逐要件查询(3~5 条)
   ↓ 并行检索(复用 parallel_retrieval 双层线程池)
   BM25 + Dense + metadata filter(案由/法域/时点/效力等级/法院层级)+ element retrieval
   ↓ RRF 融合 → Legal Reranker(AB 实验:BGE-reranker vs LLM reranker)
   ↓ Support Span Extractor
   法条:条/款/项 Span(chunk 索引已按条切分,补款/项粒度)
   类案:判决段落 Span(裁判理由段落定位)
   ↓ 带SpanRef 的证据(前端可"点击依据 → 跳转合同第7页高亮第13.2条")
```

**检索置信度**(喂给弃答,替代"盲猜"):candidate coverage(各要件是否都有候选)、score margin(top1 与阈值距离)、entailment 通过率 → `retrieval_confidence` 三档(充足/不足/失败),不足时触发 query retry(改写而非让 LLM 硬答),失败时进入弃答。

**明确不做**:多 Agent 互聊(检索不需要对话式协作);GraphRAG All-in(普通咨询走 Hybrid RAG,复杂多法条才走图增强,见方向 E 的轻量法律关系图)。

### 方向 B:模型层(G2)

- **B1 四节点 LLM 化(保留,向 IssueFrame 收敛)**:jurisdiction_triage / missing_fact_assessor / evidence_analyzer / authority_resolver 的 LLM 化工作并入 P1.5 的 issue_frame 节点建设——它们分别产出 frame 的案由/缺失要件/证据支撑/规范候选部分,规则否决权框架不变。
- **B2 法律模型路由(v2 降级为实验项)**:依据 Legal RAG Bench 结论(检索 > 生成),不再作为二期主线;保留 `LEGAL_CHAT_MODEL` 配置实验(智海-录问 / ChatLaw 经网关),在检索 2.0 达标后做对比评测决定是否保留。
- **B3 语义蕴含全量化(保留)**:所有引用必过蕴含;LLM 不可用时 `passed=None(未核验)` 并显著披露。
- **B4 弃答语义(保留,升级输入源)**:核验通过 / 带风险输出 / 弃答三档;弃答触发条件消费 G 方向的 `retrieval_confidence`,不再只看引用审计轮次。
- **能力模块化(替代"Skill 框架")**:外部评审建议参考 Legal-Skills-Chinese(38 个中文法律技能)做能力拆分——**采纳思想、不引框架**:现有 `prompt_registry`(版本化 prompt)+ tools(工具)+ retrieval 通道已是能力单元,演进方向是给 registry 增加"能力 = prompt + 工具集 + 检索配置"的组合语义,并让 IssueFrame 的逐要件处理复用同一批能力。**注意**:Legal-Skills-Chinese 为 CC BY-NC-ND 4.0,只学架构不复制内容(商业化约束),全部自行实现。

### 方向 C:合同审查工作流(G3)

**v2 增加"条款理解层"前置**(依据 CUAD/MAUD 的条款级标注实践与 ContractEval 2025 的发现——推理模式并不总能提高条款风险判断正确率,有时反而把简单任务复杂化;应"提取 → 分类 → 比对 → 判断"而非一个超级 Prompt):

```text
Document → Clause Segmentation(切分)
        → Clause Classification(付款/违约/解除/知产/保密/竞业/争议解决/责任上限/自动续期/赔偿)
        → Term Extraction(金额/期限/比例等结构化要素)
        → Playbook Alignment(三档立场比对)
        → Risk Analysis → Redline(走 HITL)
```

- **C1 Playbook 模型**:workspace 新表,`preferred / fallback / walk-away` 三档立场 + 依据法条;租户级自定义 + 默认种子(劳动合同/买卖合同)。
- **C2 偏差检测与红线建议**:确定性规则(金额上限/期限)前置过滤,LLM 只做语义比对;建议走 HITL 审批("AI 建议、律师定夺"的辅助型定位)。
- **C3 文书对比**:document_versions 两版 diff + 修订标注。

### 方向 D:评测体系(G4)

- **D3 真模型夜间评测(保留,先行)**:金标全集真模型管线 + 漂移告警。
- **D-precise(v2 新增)**:LegalBench-RAG 式 **precise-span 指标**入 retrieval_eval——不只评"召回整份文件",评"召回了支撑回答的最小片段"(文本级 precision/recall)。
- **D-hardneg(v2 新增)**:难负例评测集——同罪名 / 同案由 hard negatives、相似事实不同要件、不同事实同一法律问题;直接针对 LeCaRDv2 的"罪名捷径"问题,防止假高分。
- **D1 外部基准 / D2 轨迹评测 / D4 金标扩容(保留)**:LawBench / LexEval 子集月度跑;轨迹指标入 CI;金标 23 → 100+ 三维分层。

### 方向 E:多智能体与长任务 + 轻量法律关系图

- **E1 诉讼准备包 / E2 批量合同审查 / E3 案件档案**:同 v1 设计。
- **E-Graph(v2 重塑,自 U-40 升格并前移到 P2)**:**法律关系图,不是案件知识图谱**;先做 in-repo 轻量边表(不上 Neo4j):

```text
Claim →requires→ LegalElement →supported_by→ Fact →proved_by→ Evidence
      →governed_by→ Statute →interpreted_by→ JudicialInterpretation →illustrated_by→ Case

StatuteVersion: effective_from / effective_to / replaces / amended_by / references
```

这正好复用律言最强的 version-aware 检索;法律 GraphRAG 研究强调的三要素(层级结构、交叉引用、时间版本)全部命中。检索路由:普通咨询 Hybrid RAG,复杂多法条(多部法律交叉、法条竞合)走图增强 multi-hop。

### 方向 F:工程收尾(G5)

| 项 | 方案(v2 增补加粗) |
|---|---|
| F1 user_preferences DB 化 | 014 迁移 + Postgres store,create_app 按类型切换 |
| F2 MinIO 接入 | BlobStore 接口(本机默认 / MinIO 可选) |
| **F5 文档解析双通道(v2 新增,原属被低估项)** | Document Router:born-digital(有文本层 PDF/DOCX)→ MarkItDown(现状);扫描/拍照/盖章件(无文本层检测)→ **VISION_MODEL 通道(已有)**,Docling / PaddleOCR-VL 作为可选 extra 依赖;输出统一为带结构的 Markdown |
| **F8 EvidenceSpan 坐标化(v2 新增)** | 保存 `EvidenceSpan(file_id, page, bbox, paragraph_id, text)`;引用携带 SpanRef → 前端"点击依据跳转页码/段落高亮"——这是超越普通聊天式法律 AI 的产品级体验 |
| F3 web_search 人名脱敏 / F4 "已修改"口径 / F6 SSE 重连 / F7 overrides 治理 / F9 server.py 拆分 | 同 v1 |

## 4. 风险与缓解

| 风险 | 影响 | 缓解 |
|---|---|---|
| LeCaRDv2 "同罪名捷径" | 类案评测假高分 | 难负例评测集(D-hardneg)+ 要件级检索作为真实目标 |
| IssueFrame 产出质量依赖 LLM | 要件错→检索错 | 规则模板降级路径 + 逐要件可人工修正(HITL)+ 金标分层评测帧质量 |
| Span 粒度标注成本(判决书段落) | P1.5 工期 | 法条侧先做(条/款/项已有 chunk 基础),类案段落 Span 用启发式切分起步 |
| 全量蕴含成本 | B3 成本 | 置信度信号前置(低置信才审)+ 批量分批 + 成本守卫 |
| 文档 AI 依赖重 | 部署变重 | 可选 extra;VISION_MODEL 网关通道先行 |
| 类案数据授权(人民法院案例库) | A1 延期 | LeCaRDv2/CAIL 先行,schema 兼容 |
| 弃答被理解为"产品不行" | 体验 | 弃答文案明确"缺什么、怎么补";分析正文完整保留 |

## 5. 执行纪律(与既有工作流一致)

1. 每个原子步骤独立提交,提交信息带清单 ID(如 `feat(retrieval): G-1 IssueFrame 数据模型 (#U-47)`)。
2. 完成即在 `project_document/UPGRADE_CHECKLIST.md` 勾选并填 commit hash;新发现的问题追加到清单 Backlog 区。
3. 每步完成须过:pytest 全量 + `ruff check` + `ruff format --check` + `run_regression` + `pipeline_eval`;涉检索步骤加 retrieval_eval 对应维度;P1.5 起加 precise-span 与难负例指标。
4. 涉安全/租户的步骤沿用既有模式:RLS 策略迁移(带 DROP IF EXISTS 守卫)+ 租户 GUC 注入 + ownership 404 语义。
5. 直推 main,CI 全绿为准;网络抖动重试(git:443 不可用时经 gh api Git Data API 推送,推送后校验树哈希一致)。

## 附:主要外部依据

[JUREX-4E(arXiv 2025,155 罪名四要件专家标注)](https://arxiv.org) · [LeCaRDv2(SIGIR 2024,800 queries/55k 候选)](https://arxiv.org) · [LegalBench-RAG(6,858 query-answer 对,precise retrieval)](https://github.com/reglab/benchmarks) · Legal RAG Bench(2026,检索影响 > 生成模型) · [COLIEE(Case Entailment)](https://coliee.org) · [CUAD(13k+ 条款专家标注)](https://www.atticusprojectai.org/cuad) · MAUD(47k+ 标注) · ContractEval(2025) · [Legal-Skills-Chinese(CC BY-NC-ND,只学架构)](https://github.com) · [Docling](https://github.com/DS4SD/docling) · PaddleOCR-VL · [LawBench](https://github.com/open-compass/LawBench) · [LexEval](https://www.alphaxiv.org/zh/abs/2409.20288) · [PLawBench](https://evalscope.readthedocs.io/zh-cn/v1.11.0/benchmarks/plawbench.html) · [智海-录问](https://zhuanlan.zhihu.com/p/652301235) · [ChatLaw](https://github.com/PKU-YuanGroup/ChatLaw) · [AI 捏造判例制裁(200+ 案例)](https://www.claytonrice.com) · [辅助型 AI 人机分工(泰和泰)](https://www.tylaw.com.cn)
