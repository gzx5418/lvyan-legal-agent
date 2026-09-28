# 律言平台化升级计划(Platform Upgrade Plan)

> 创建时间:2026-09-28
> 依据:两轮全模块代码审查(2026-09)已完成修复(共 2 批:P0×4 + P1×9 + P2 批量,提交 `7d03f07`/`3ee2a85`/`a13b040`)+ 六路外部对标调研。
> 配套执行清单:**`project_document/UPGRADE_CHECKLIST.md`**(原子步骤 + 进度追踪,推进时以清单为准勾选)。
> 状态标记约定见清单文档头部;本文件只讲"为什么与怎么做",不重复罗列步骤。

---

## 0. 背景与定位

两轮审查后,律言的**可信底座**已闭环:15 节点 LangGraph 图、版本感知检索、引用三报告审计、输出护栏、HITL 原子认领、RLS 全链路租户上下文、1046 测试、金标回归 + 管线评测 CI 门禁。在开源同类(LaWGPT / ChatLaw / DISC-LawLLM 等)中,这一层的工程纪律没有对手。

对标 Harvey / CoCounsel / Lexis+ 等平台型产品与 LawBench / LexEval / PLawBench 等评测基准,以及 2025-2026 法律 AI 的行业共识(检索失败弃答、生成后逐条核验、Playbook 合同审查、辅助型 AI 人机分工),剩余差距集中在三轴:

1. **法律知识的另一半**:法条维度完整(版本感知是差异化优势),类案维度为零(仅精编规则桩);
2. **智能的深度**:依赖通用模型 + 提示词,法律领域模型未接入;语义蕴含核验未全量化;检索失败时"强制放行"而非"弃答";
3. **场景的宽度**:咨询问答 + 文书生成已可用,合同审查(Playbook 偏差比对)、批量审查、诉讼准备包等工作流未覆盖。

## 1. 目标与非目标

**目标**
- G1 类案检索从 0 到 1:真实类案语料入库、与法条同栈检索、案号防幻觉审计。
- G2 推理深度:四个规则节点 LLM 化收尾;法律领域模型可选路由;语义蕴含全量化;检索失败弃答。
- G3 合同审查工作流:Playbook 数据模型 + 偏差检测 + 红线建议,复用 workspace 审批流。
- G4 评测外部对标:接入公开基准;轨迹级评测入 CI;真模型评测夜间自动化;金标扩容分层。
- G5 工程收尾:多实例一致性(user_prefs/附件存储)、web_search 隐私、语料口径、代码结构。

**非目标(明确不做)**
- 法律语料预训练/微调(LaWGPT 路线):工程重、收益低于现成模型路由一个量级。
- 自主多智能体 swarm:reflection 循环的实践共识是"仅在失败路径选择性使用",现有 critic/引用回路已是正确形态。
- 替换 LangGraph 或重写检索底座:底座是优势,不动。

## 2. 分期路线图

| 期 | 主题 | 覆盖方向 | 出口标准(可验收的整体标志) |
|---|---|---|---|
| **P1 第一期(2-4 周)** | 类案从 0 到 1 + 智能收尾 | A2/A3、B1、B4、D3、E 全部 | 类案可在 OpenSearch 真实语料上检索并过引用审计;四节点 LLM 化且规则否决权保留;弃答语义上线;真模型夜间评测出首份报告;遗留工程项清零 |
| **P2 第二期(1-2 月)** | 领域深化 + 评测对标 | B2、B3、D1、D2、D4、C1-C2 | 法律模型路由可选可用且评测对比报告产出;全量语义蕴含;LawBench/LexEval 子集回归接入 CI;金标 100+ 条分层;合同 Playbook MVP(单类合同端到端) |
| **P3 第三期(季度)** | 平台化 | C3、E1-E3、A1(正式) | 诉讼准备包端到端;批量合同审查;文书对比;人民法院案例库接入(需数据合作) |

依赖关系:第二期的 B2/B3 依赖第一期 D3 建立的真模型基线;C 系列依赖 workspace 现有骨架(已具备);A1 正式接入依赖外部数据授权,先行用 A2 的开源数据跑通管线。

## 3. 各方向设计

### 方向 A:类案检索(G1)

**现状**:`tools/cases.py` 是精编 `case_patterns.md` 桩(无真实案号);`retrieval/case_source.py` 的多来源抽象(curated JSON / OpenSearch / 外部 API / 向量库)已实现但 `MultiSourceRetriever` 未接入节点主链;`scripts/ingest_laws.py:526-608` 已有 OpenSearch bulk 灌库管线;compose 已含 opensearch 服务;输出端 `_format_cases` 已标注"非真实案例检索"。

**设计**:
- **A2 OpenSearch 接线(先行)**:类案语料(LeCaRD 刑事判决 + CAIL 公开数据起步)按统一 schema(案号/法院/案由/事实/裁判要旨/效力级别/来源 URL)灌入 OpenSearch `legal_cases` 索引;`search_cases` 切换到 `MultiSourceRetriever`(OpenSearch → curated 兜底);复用混合检索的重排与 RRF。
- **A3 审计与呈现**:新增两个审计器进 `citation_verifier` 骨架——案号存在性核验(检索结果集中存在该案号)与案由一致性校验;类案引用标注效力级别(指导性案例 > 参考案例);`_format_cases` 按来源分级呈现。
- **A1 人民法院案例库(第三期)**:需数据合作/授权,schema 设计预留 `source_authority` 字段即可,不做爬取。

**验收**:金标集新增类案检索维度用例 ≥20 条;`retrieval_eval` 类案指标(Recall@k/MRR)有基线报告;伪造案号用例被审计拦截。

### 方向 B:模型层深化(G2)

**现状**:jurisdiction_triage / missing_fact_assessor / evidence_analyzer / authority_resolver 部分规则化(prompt_registry 已为全部节点建版本化 prompt 通道);引用内容阈值 4/0.15 + grounding LLM 蕴含仅前 20 条、降级已留痕(`llm_reviewed`/`llm_degraded_reason`);引用校验 2 轮不通过后强制放行 + 标高风险。

**设计**:
- **B1 四节点 LLM 化**:每节点按既有模式(prompt_registry 注册 + LLM 结果只能修正/否决不能创造 + 降级回退规则)接入;missing_fact_assessor 的 blocking 判定仅由 LLM 白名单字段产生(追问路径目前接近死代码,借 LLM 化激活时效临近等场景)。
- **B2 法律模型路由**:新增 `LEGAL_CHAT_MODEL` 配置,网关侧路由——分诊/抽取用轻量通用模型,推理/文书用法律领域模型(智海-录问 / ChatLaw 等,经魔搭或网关部署);不可用时回退 `CHAT_MODEL`,路由选择记录进 trace。
- **B3 语义蕴含全量化**:所有引用必过蕴含审查(移除前 20 截断,批量分批调用);LLM 不可用时引用审计 `passed` 从 True 改为 None(未核验),输出层显著披露"本回答的法条引用未经语义核验"。
- **B4 弃答语义**:引用审计不通过且达重检索上限时,三档分支——核验通过 / 带风险输出 / **弃答**(`abstained=true` + 结构化原因:哪些引用无法核验、建议补充什么);前端渲染弃答卡片;HITL 不适用于弃答路径。

**验收**:四节点各有 LLM/降级双路径测试;B2 有路由对比评测报告;B3/B4 有端到端用例(构造检索必失败场景验证弃答);弃答事件有 SSE 语义与前端展示。

### 方向 C:合同审查工作流(G3)

**现状**:upload → markitdown 转换 → `analyze_contract_clause`(9 类风险关键词规则桩)→ workspace(案件/文书/版本/审批/审计事件六表)已具备;RAG 面无逐条比对;无 Playbook 概念。

**设计**:
- **C1 Playbook 模型**:workspace 新表 `contract_playbooks`(`user_id` 租户隔离 + 条款类型 + preferred/fallback/walk-away 三档立场 + 依据法条);提供默认 playbook(劳动合同/买卖合同起步);`routes_workspace` 增 CRUD。
- **C2 偏差检测与红线建议**:LLM 逐条比对条款 ↔ playbook,输出三色(符合/偏离/不可接受)+ 建议替换文本 + 依据;结果入 `review_findings`(现有状态机);红线建议走 HITL 审批(辅助型 AI 定位:AI 建议、人定夺)。
- **C3 文书对比(第二期)**:基于 `document_versions` 渲染两版差异(逐段 diff + 修订标注),覆盖"对方发回修改稿"场景。

**验收**:单类合同(劳动合同)端到端:上传 → 逐条检测 → findings 状态机流转 → 审批 → 报告;playbook 偏离用例(违约金超限/竞业过宽)被正确标记;全部测试离线可跑。

### 方向 D:评测体系(G4)

**现状**:三层评测方法论(evaluator 自洽 → 管线回归 → 生产真模型)已建立,金标 23 条(含 3 条历史 as_of);`retrieval_eval` 已透传 as_of 并标注 embedding/rerank 真实/桩模式;`run_regression` 有 `--degrade` 反向验证;CI 门禁齐备;`agent_eval.py`(轨迹指标)存在但未入 CI;C 层真模型评测手动。

**设计**:
- **D3 真模型夜间评测(先行)**:夜间 job(或手动 workflow_dispatch)——网关可用时对金标全集跑真模型管线,输出与离线基线的漂移对比,阈值告警;报告落 `outputs/evals/`。
- **D1 外部基准**:选 LawBench 的法律问答/法条检索子集与 LexEval 的记忆/理解层,做成 `tests/evals/external/` 独立入口(不进 CI 主门禁,防外部数据变动打断;月度跑)。
- **D2 轨迹级评测**:把 `agent_eval.py` 的节点序列/无效工具调用/循环率指标接入 pipeline_eval 报告并进 CI(先阈值宽松起步)。
- **D4 金标扩容**:23 → 100+,按案由 × 难度(规则可答/需推理/需类案)× 时点(现行/历史)三维分层,分层单独出报告;新增类案维度用例与弃答用例。

**验收**:CI 报告含轨迹指标;夜间评测连续一周产出;外部基准月报;金标分层报告。

### 方向 E:多智能体与长任务(G3/G1 延伸)

**设计**:
- **E1 诉讼准备包**:现有节点组合成"证据梳理 → 时效测算(calculators 已有)→ 管辖分析 → 文书起草 → 材料清单"长链,输出打包(分析报告 + 起诉状 DOCX + 证据清单);deep 模式的自然延伸,无新技术。
- **E2 批量合同审查**:目录级批量上传(解除单文件 10MB/10 个限制的批次语义)→ 逐份走 C 系列管线 → 汇总风险矩阵报告;进度经 SSE phase 事件呈现。
- **E3 案件档案**:facts/证据/文书/时间线结构化入 workspace 并与案件绑定;会话摘要生成改为"结构化档案 + 近期对话"双通道,长周期案件(跨年)不丢上下文。

### 方向 F:工程收尾(G5,承接二轮审查遗留)

| 项 | 现状/证据 | 方案 |
|---|---|---|
| F1 user_preferences DB 化 | 本机文件存储,与生产多实例定位矛盾(`memory/user_preferences.py`) | 014 迁移 + Postgres store(复用 PostgresRunMetadataStore 连接与租户注入),create_app 按 metadata_store 类型切换 |
| F2 MinIO 接入 | .env.example 标注"保留位未实际接入" | 附件/文书/导出迁对象存储,抽象 `BlobStore` 接口(本机文件为默认实现) |
| F3 web_search 隐私 | 出站查询人名不脱敏(`_prepare_query` 仅脱敏号码类) | 保守启发式(称谓词/引号专名)或文档披露,二选一需产品决策 |
| F4 "已修改"状态口径 | 389 个文件 status=已修改 → unknown,现行法查询排除 16% 语料(`_STATUS_MAP` 无此键) | 按"effective + superseded 推导"处理;需 version_quality 门禁联动 |
| F5 server.py 拆分 | 2072 行,run 端点内联附件链路 ~230 行 | 先抽 `api/attachments.py`(唯一带失败回滚复杂度的部分),其余不动 |
| F6 SSE 断线重连 | 事件无 `id:`,重连重复投递 | RunContext 维护递增 seq → SSE `id:` 行;stream 端点读 `Last-Event-ID` 过滤 |
| F7 索引元数据治理常态化 | overrides 机制就绪(九部法律已回填),但缺流程 | sync_sources 报告 + 版本质量基线联动升级为月度任务;覆盖"已修改"口径后重估 |

## 4. 风险与缓解

| 风险 | 影响 | 缓解 |
|---|---|---|
| 类案数据授权不确定(人民法院案例库) | A1 延期 | A2 先用开源数据(LeCaRD/CAIL)跑通全链,schema 兼容两种来源 |
| 法律领域模型推理质量不达预期 | B2 无效 | 路由设计为可选+可回退;先小规模对比评测再放量 |
| 全量蕴含审查成本放大 | B3 成本 | 批量分批调用 + 确定性阈值前置过滤(仅灰带必审)+ 网关成本预算守卫(已有 $2/run) |
| 弃答被用户理解为"产品不行" | B4 体验 | 弃答文案明确"缺什么、怎么补";分析正文仍完整输出,仅结论定性降级 |
| 外部基准数据变动打断 CI | D1 | 外部基准独立入口、月度跑,不进 PR 门禁 |

## 5. 执行纪律(与既有工作流一致)

1. 每个原子步骤独立提交,提交信息带清单 ID(如 `feat(cases): A2-1 OpenSearch 类案索引 schema (#U-12)`)。
2. 完成即在 `project_document/UPGRADE_CHECKLIST.md` 勾选并填 commit hash;新发现的问题追加到清单末尾 Backlog 区,不打乱编号。
3. 每步完成须过:pytest 全量 + `ruff check` + `ruff format --check` + `run_regression` + `pipeline_eval`;涉检索的步骤加对应 retrieval_eval 维度。
4. 涉安全/租户的步骤(C1 playbook 表、F1/F2)必须沿用既有模式:RLS 策略迁移(带 DROP IF EXISTS 守卫)+ 租户 GUC 注入 + ownership 404 语义。
5. 直推 main,CI 全绿为准;网络抖动重试(既有偏好)。
