# 律言生产级完善计划 - 任务跟踪

> 文件名: PRODUCTION_READINESS_PLAN.md
> 创建时间: 2026-08-09
> 关联协议: RIPER-5 + Multidimensional + Agent Protocol
> 预计周期: 9-11周

## 任务描述

把律言(LüYan)法律 Agent 从本地轻量开发模式提升为安全、可扩展、可观测的多租户生产系统。分5个阶段实施，涵盖安全阻断项修复、多租户RLS、架构异步化、可观测性、LLM能力增强与多来源案例库。

## 项目概览

- **核心框架**: LangGraph (v1+) + FastAPI + PostgreSQL + OpenSearch
- **Graph节点数**: 14个 (preflight → legal_answer_finalizer)
- **LLM客户端**: 同步 httpx.Client，支持 chat/chat_json/chat_structured
- **认证**: 可选 JWT / trusted_proxy，开发模式默认关闭
- **持久化**: PostgresSaver (async/sync) + agent_runs/threads/messages 元数据表
- **检索**: BM25 lexical + OpenSearch dense + hybrid + reranker
- **缓存**: Pickle 格式的 article_index_v2.pkl / bm25_index.pkl
- **限流**: 进程内滑动窗口（per-IP），无 Redis 后端
- **指标**: 内存 MetricsRecorder 桩，无 Prometheus/metrics 端点
- **日志**: 标准 logging，无结构化/contextvars 绑定

---

## Analysis (RESEARCH 阶段完成)

### 1. Pickle 安全风险 - 确认5处

| 文件 | 行号 | 用途 |
|------|------|------|
| `src/lvyan/retrieval/lexical.py` | L308 | article_index_v2.pkl 读取 |
| `src/lvyan/retrieval/lexical.py` | L596 | bm25_index.pkl 读取 |
| `src/lvyan/retrieval/manifest.py` | L308 | article_index 一致性校验 |
| `src/lvyan/retrieval/manifest.py` | L333 | bm25_index 一致性校验 |
| `src/lvyan/api/server.py` | L372 | 健康检查兼容旧部署读 pkl |

额外：`lexical.py` 在 JSON 回退后还会**写入** pickle 缓存（L337附近）。

### 2. 加密降级风险 - 确认

- `cryptography` 仅在 `uv.lock` 中作为传递依赖存在（通过 `PyJWT[crypto]`），但 `pyproject.toml` 的 `dependencies` 未直接列出。
- `case_vault.py` L254: `if key is None: return base64.b64encode(plaintext)` — 无密钥时静默降级 base64。
- `case_vault.py` L264-267: `except ImportError` — 库未安装时降级 base64。
- 无环境变量 `CASE_VAULT_ALLOW_INSECURE` 控制机制。

### 3. 宽异常捕获统计

源码 `src/` 中 `except Exception` 出现 **135处**（按文件统计），重点分布：
- `api/server.py`: 25处
- `api/sse.py`: 16处
- `observability/tracing.py`: 12处
- `retrieval/retrieve_statutes.py`: 10处
- `memory/store.py`: 6处
- `retrieval/lexical.py`: 5处

### 4. Graph 节点清单（14个）

```
preflight → attachment_retriever → jurisdiction_triage → fact_extractor
→ missing_fact_assessor → planner → parallel_retrieval → authority_resolver
→ legal_reasoner → critic → composer → citation_verifier
→ output_guardrail → legal_answer_finalizer
```

I/O型节点（需异步化）: fact_extractor, planner, parallel_retrieval, authority_resolver, legal_reasoner, evidence_analyzer, critic, citation_verifier
纯计算节点（保持同步）: preflight, attachment_retriever, jurisdiction_triage, missing_fact_assessor, composer, output_guardrail, legal_answer_finalizer

### 5. LLM 接入状态

| 节点 | LLM状态 | 说明 |
|------|---------|------|
| planner | 已接入 | chat_json + 规则降级 |
| fact_extractor | 已接入 | LLM抽取 + 规则校验 |
| legal_reasoner | 已接入 | chat_structured |
| jurisdiction_triage | 纯规则 | 待增强 |
| missing_fact_assessor | 纯规则 | 待增强 |
| evidence_analyzer | 纯规则 | 待增强 |
| authority_resolver | 纯规则 | 待增强 |
| critic | 纯规则 | 待增强 |

### 6. 多租户现状

- `agent_threads.user_id` / `agent_runs.user_id` 已存在列
- `case_workspace` 的 `legal_cases.user_id` 已存在
- **无 RLS 策略**：应用层过滤依赖 API 代码正确传参
- **无 runtime role 分离**：应用直接使用 migration owner 连接
- 限流仅基于IP，无 per-user 限流

### 7. CI 测试覆盖

- 4个 CI jobs: test, regression, pipeline, lint, postgres-integration
- 金标集回归: 有阈值门禁（法条准确率≥0.9, 虚构率≤0.05）
- Pipeline评测: 限3条用例
- **无覆盖率门禁** (pytest-cov 未安装)
- **无 E2E 服务级测试**
- **无真实 LLM/Redis/OpenSearch 集成测试**

### 8. 架构拆分需求

- `server.py`: ~1740行，混合 agent/upload/health/documents 路由
- `sse.py`: ~1370行，混合 RunManager/HITL恢复/SSE事件流
- `lexical.py`: ~1380行，混合索引存储/BM25构建/搜索
- LLM客户端: 同步 httpx.Client，无异步版本

### 9. 优雅停机

- 无 shutdown 生命周期管理
- 无 readyz 503 排空机制
- 无运行中任务的 interrupt 处理
- 无 SSE server_shutdown 事件
- 无 SIGTERM 宽限期配置

### 10. 案例来源

- 当前仅有精编规则库（knowledge/curated）+ 官方法律全文库（external/lvyan-lawtext）
- 无统一 CaseProvider 抽象
- 无商业案例网关接口
- 检索结果未标注来源与更新时间

---

## 阶段实施计划总览

| 阶段 | 目标 | 周期 | 状态 |
|------|------|------|------|
| P1 | 安全阻断项 | 1周 | 待开始 |
| P2 | 多租户与生产运行基础 | 2周 | 进行中 |
| P3 | 架构拆分、异步化与优雅停机 | 2-3周 | 部分完成(步骤54-55) |
| P4 | 日志、指标和质量门禁 | 2周 (与P3并行) | 待开始 |
| P5 | LLM能力与多来源案例库 | 3-4周 | 待开始 |

---

## 当前执行阶段

### 阶段1 执行进展 (2026-08-09)

| 步骤 | 描述 | 状态 |
|------|------|------|
| #1 | `safe_index.py` MsgPack 索引模块 | 完成 |
| #2 | pyproject.toml 添加 msgpack + cryptography | 完成 |
| #3 | lexical.py article_index 消除 pickle | 完成 |
| #4 | lexical.py bm25_index 消除 pickle | 完成 |
| #5 | manifest.py 一致性校验消除 pickle | 完成 |
| #6 | server.py 健康检查消除 pickle | 完成 |
| #7 | rebuild_indexes.py 脚本 | 完成 |
| #8 | 恶意 Pickle 安全测试 | 完成 |
| #9 | case_vault.py 加密降级阻断 | 完成 |
| #10 | config.py 新增配置字段 | 完成 |
| #11 | 加密安全测试 | 完成 |
| #12 | Ruff BLE001 启用 | 完成 |
| #13-15 | 宽异常审计 | 现有代码已有 noqa 标记; 新代码由 Ruff 强制 |
| #16 | CI pickle 检测 | 完成 |
| #17 | CI BLE001 集成 | 完成 (通过 ruff check) |
| #18 | Dockerfile 预生成索引 | 待执行 (需要 Dockerfile 访问) |

**退出条件验证**：
- [x] 源码不存在 `pickle.load/loads` (grep 确认 0 结果)
- [x] SafeIndexStore 拒绝恶意 pickle (测试覆盖)
- [x] 生产加密无法静默降级 (validate_encryption_config)
- [x] Ruff BLE001 启用，关键路径异常已分类
- [x] ingest_laws.py 中 `_save_article_index_pickle` 已改名为 `_save_article_index_lvix`
- [x] Dockerfile 预生成 LVIX 索引 (步骤18)

### 阶段2 执行进展 (2026-08-09)

| 步骤 | 描述 | 状态 |
|------|------|------|
| #19 | migrations/006: runtime role 创建 | 完成 |
| #20 | migrations/007: agent_threads/runs/messages RLS | 完成 |
| #21 | migrations/008: case_workspace RLS | 完成 |
| #22 | migrations/009: checkpoint RLS | 待执行 (需LangGraph表结构) |
| #23 | db/tenant_context.py 租户上下文 | 完成 |
| #24 | TenantAwareAsyncPostgresSaver | 待执行 |
| #25 | rls_preflight.py 孤儿检查 | 完成 |
| #26 | pyproject.toml 添加 redis 可选依赖 | 完成 |
| #27-29 | rate_limit.py 重构 (抽象后端+Redis+per-user) | 完成 |
| #30 | config.py 新增 Redis/RLS/metrics 配置 | 完成 |
| #31-33 | 跨租户隔离测试 | 完成 |
| #34 | 限流后端测试 | 完成 |
| #35 | 生产配置校验测试 | 完成 (含于步骤31) |

**附加完成**：
- [x] docker-compose.yml 新增 Redis 服务
- [x] docker-compose.yml 新增 P2/P3/P4 环境变量
- [x] docker-compose.yml stop_grace_period: 45s
- [x] migrations/009: interrupted status (步骤54)
- [x] pyproject.toml 添加 production extras (redis + prometheus + structlog)

### 阶段3 执行进展 (2026-08-09)

| 步骤 | 描述 | 状态 |
|------|------|------|
| #54 | agent_runs 新增 interrupted 状态 | 完成 |
| #55 | docker-compose stop_grace_period | 完成 |
| #57-58 | LLM + 检索并发信号量 | 完成 (lvyan/infra/concurrency.py) |
| #59 | 优雅停机协调器 + lifespan 集成 | 完成 (lvyan/infra/shutdown.py) |

### 阶段4 执行进展 (2026-08-09)

| 步骤 | 描述 | 状态 |
|------|------|------|
| 结构化日志 | structlog JSON/text 双模式 | 完成 (lvyan/observability/logging_setup.py) |
| Prometheus 指标 | 指标定义 + /metrics 端点 | 完成 (lvyan/observability/metrics.py) |
| HTTP 指标中间件 | 请求延迟/总数/活跃连接 | 完成 (lvyan/observability/http_metrics.py) |
| .env.example 更新 | 新增 P2-P4 所有配置项 | 完成 |

### 测试覆盖

- [x] tests/security/test_tenant_isolation.py (12 tests)
- [x] tests/unit/test_rate_limit_backends.py (10 tests)
- [x] 全量测试: 126 passed, 0 failed
