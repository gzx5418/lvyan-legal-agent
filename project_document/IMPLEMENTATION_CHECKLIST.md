# 律言生产级完善 - 完整实施清单

> 创建时间: 2026-08-09
> 总计: 5阶段 / 78个原子步骤
> 每个步骤可独立提交，标注依赖关系

---

## 阶段1: 安全阻断项 (1周, 步骤 1-18)

### 1.1 MsgPack 安全索引格式

**步骤1** - 新增 `src/lvyan/retrieval/safe_index.py` 模块
- File: `src/lvyan/retrieval/safe_index.py` (新建)
- 实现 `SafeIndexStore` 类:
  - `save(path, data, *, corpus_hash, schema_version)`: 写入 MsgPack 格式索引
  - `load(path, *, expected_schema_version) -> dict | None`: 安全读取并校验
  - 格式头: magic bytes `b"LVIX"` + uint8 version + uint32 payload_length
  - 元数据字段: schema_version, corpus_hash, payload_sha256, item_count, created_at
  - 安全限制: payload_size ≤ 500MB, item_count ≤ 10_000_000
  - 写入策略: 临时文件 → fsync → os.replace 原子替换
- 依赖: 需在 pyproject.toml 添加 `msgpack` 依赖

**步骤2** - `pyproject.toml` 添加 `msgpack` 和 `cryptography` 直接依赖
- File: `pyproject.toml`
- 在 dependencies 中添加:
  - `"msgpack>=1.0"` (安全索引序列化)
  - `"cryptography>=42.0"` (直接依赖, 不依赖 PyJWT 传递)

**步骤3** - 改造 `lexical.py` 的 article_index 加载逻辑
- File: `src/lvyan/retrieval/lexical.py`
- 将 `_load_article_chunks()` 中的 pickle.load 替换为 `SafeIndexStore.load()`
- 将 pickle 写入替换为 `SafeIndexStore.save()`
- 保留 JSON 回退读取（向后兼容过渡期）
- 删除所有 `import pickle` 和 pickle 相关代码路径
- 文件扩展名从 `.pkl` 改为 `.lvix`

**步骤4** - 改造 `lexical.py` 的 bm25_index 加载逻辑
- File: `src/lvyan/retrieval/lexical.py`
- 将 `_load_or_build_bm25_index()` 中的 pickle.load 替换为 SafeIndexStore
- 将 bm25 写入替换为 SafeIndexStore.save()
- 删除 bm25_index.pkl 相关代码路径

**步骤5** - 改造 `manifest.py` 的一致性校验
- File: `src/lvyan/retrieval/manifest.py`
- `verify_index_integrity()` 中 2处 pickle.load 替换为 SafeIndexStore.load()
- 更新文件名常量: `article_index_v2.pkl` → `article_index_v3.lvix`, `bm25_index.pkl` → `bm25_index_v3.lvix`
- manifest schema 字段更新

**步骤6** - 改造 `server.py` 健康检查中的 pickle 兼容读取
- File: `src/lvyan/api/server.py`
- `_get_law_db_info()` 中删除 pickle.load 路径（L367-374）
- 改为优先读 SafeIndexStore `.lvix` 文件，回退读 JSON
- 彻底移除 `import pickle`

**步骤7** - 新增 `scripts/rebuild_indexes.py`
- File: `src/lvyan/scripts/rebuild_indexes.py` (新建)
- 从法规源或 JSON 缓存重建 MsgPack 索引
- 供部署时预生成，以及迁移期从旧 JSON 缓存转换

**步骤8** - 新增测试: 恶意 Pickle 不可执行
- File: `tests/security/test_pickle_removed.py` (新建)
- 测试1: 构造恶意 pickle 文件 → SafeIndexStore.load() 拒绝
- 测试2: 确认源码中无 `pickle.load` / `pickle.loads`
- 测试3: `.pkl` 文件被忽略，不影响正常运行

### 1.2 加密降级阻断

**步骤9** - `case_vault.py` 添加生产加密强制逻辑
- File: `src/lvyan/memory/case_vault.py`
- 新增环境变量 `CASE_VAULT_ALLOW_INSECURE`:
  - 生产环境: 缺少合法 CASE_VAULT_KEY 时 **拒绝启动** (raise RuntimeError)
  - 开发环境: 仅在 `CASE_VAULT_ALLOW_INSECURE=true` 时允许 base64 降级
- `_encrypt()` / `_decrypt()`: ImportError 时根据模式决定行为
- 新增 `validate_encryption_config()` 供启动时调用

**步骤10** - `config.py` 注册新配置字段
- File: `src/lvyan/config.py`
- Settings 新增:
  - `case_vault_allow_insecure: bool = False`
  - `case_vault_key: str = ""` (从环境变量 CASE_VAULT_KEY 读取)
- `validate_runtime_config()` 新增加密校验逻辑

**步骤11** - 测试: 生产加密无法降级
- File: `tests/security/test_case_vault_encryption.py` (新建)
- 测试1: RUNTIME_MODE=production + 无 CASE_VAULT_KEY → 启动失败
- 测试2: RUNTIME_MODE=development + CASE_VAULT_ALLOW_INSECURE=false + 无 key → 启动失败
- 测试3: RUNTIME_MODE=development + CASE_VAULT_ALLOW_INSECURE=true + 无 key → base64 降级
- 测试4: 有合法 key → AES-256-GCM 加解密正常

### 1.3 宽异常审计与 Ruff BLE001

**步骤12** - Ruff 配置启用 BLE001 规则
- File: `pyproject.toml`
- `[tool.ruff.lint]` 添加 `select = ["BLE001"]` (或在 extend-select 中)
- 配置 `[tool.ruff.lint.per-file-ignores]` 为允许的边界位置添加 `# noqa: BLE001`

**步骤13** - 审计并修复关键路径宽异常 (数据库/HTTP)
- Files: `src/lvyan/api/server.py`, `src/lvyan/api/sse.py`, `src/lvyan/memory/store.py`
- 数据库路径: `except Exception` → `except (SQLAlchemyError, OperationalError, ...)`
- HTTP路径: → `except (httpx.HTTPError, httpx.TimeoutException, ...)`
- `CancelledError` 必须单独处理并重新抛出

**步骤14** - 审计并修复关键路径宽异常 (文件/JSON/Pydantic)
- Files: `src/lvyan/retrieval/lexical.py`, `src/lvyan/tools/file_converter.py`, `src/lvyan/nodes/*`
- 文件I/O: → `except (OSError, IOError)`
- JSON解析: → `except (json.JSONDecodeError, ValueError)`
- Pydantic校验: → `except (ValidationError,)`
- 保留 `# boundary-exception:` 标记的合法宽捕获

**步骤15** - 标记允许的边界异常
- Files: 各 API 边界、后台任务顶层、可选遥测
- 格式: `except Exception:  # boundary-exception: API顶层错误转换`
- 必须满足: 记录异常、转换为明确状态（HTTPException/error event）

### 1.4 阶段1退出验证

**步骤16** - CI 流水线添加 pickle 检测步骤
- File: `.github/workflows/ci.yml`
- 新增步骤: `grep -r "pickle\.load\|pickle\.loads" src/ && exit 1 || echo OK`

**步骤17** - 新增 ruff BLE001 集成测试
- File: `.github/workflows/ci.yml`
- 确保 `ruff check` 包含 BLE001 规则

**步骤18** - 更新 Dockerfile 预生成 MsgPack 索引
- File: `Dockerfile`
- 构建阶段调用 `python -m lvyan.scripts.rebuild_indexes`
- 部署时 `.pkl` 文件不复制/忽略

---

## 阶段2: 多租户与生产运行基础 (2周, 步骤 19-35)

### 2.1 RLS 策略与角色分离

**步骤19** - 新增迁移: 创建 runtime role
- File: `migrations/006_rls_roles.sql` (新建)
- `CREATE ROLE lvyan_runtime LOGIN PASSWORD ... NOSUPERUSER NOCREATEDB NOCREATEROLE`
- `GRANT CONNECT ON DATABASE lvyan TO lvyan_runtime`
- `GRANT USAGE ON SCHEMA public TO lvyan_runtime`
- `GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO lvyan_runtime`
- 无 `BYPASSRLS` 权限

**步骤20** - 新增迁移: agent_threads/runs/messages 启用 RLS
- File: `migrations/007_rls_agent_tables.sql` (新建)
- `ALTER TABLE agent_threads ENABLE ROW LEVEL SECURITY; ALTER TABLE agent_threads FORCE ROW LEVEL SECURITY;`
- Policy: `CREATE POLICY tenant_isolation ON agent_threads USING (user_id = current_setting('app.user_id', true))`
- 同理 agent_runs, agent_messages (直接校验 user_id)

**步骤21** - 新增迁移: case_workspace 表启用 RLS
- File: `migrations/008_rls_workspace.sql` (新建)
- legal_cases: `USING (user_id = current_setting('app.user_id', true))`
- 子表 (case_evidence, legal_documents, document_versions, review_findings, document_approvals, workspace_audit_events): 通过 `case_id` JOIN `legal_cases` 校验

**步骤22** - 新增迁移: LangGraph checkpoint 表 RLS
- File: `migrations/009_rls_checkpoints.sql` (新建)
- checkpoint 系列表通过 `thread_id → agent_threads.user_id` 的策略隔离
- Policy 在 JOIN 条件中使用 `current_setting('app.user_id', true)`

**步骤23** - 数据库连接层: 每事务设置租户上下文
- File: `src/lvyan/db/tenant_context.py` (新建)
- 实现 `set_tenant_context(conn, user_id)`: 执行 `SET LOCAL app.user_id = %s`
- 提供 `get_tenant_connection(pool, user_id)` 上下文管理器
- SQLAlchemy event listener: 在 connection checkout 时自动设置

**步骤24** - 实现 `TenantAwareAsyncPostgresSaver`
- File: `src/lvyan/memory/tenant_checkpointer.py` (新建)
- 继承 `AsyncPostgresSaver`
- 使用连接池 (asyncpg / psycopg async)
- 在同一事务内先 `SET LOCAL app.user_id` 再执行 checkpoint 读写
- 配合 LangGraph checkpointer 接口

**步骤25** - 孤儿记录检查与归属回填脚本
- File: `src/lvyan/scripts/rls_preflight.py` (新建)
- 检查所有表中 user_id 为空/NULL 的记录
- 检查 thread_id 无对应 agent_threads 记录的 checkpoint
- 存在孤儿数据时输出报告并 exit(1) (迁移不可继续)

### 2.2 Redis 限流

**步骤26** - `pyproject.toml` 添加 `redis` 可选依赖
- File: `pyproject.toml`
- 新增 optional-dependencies `production`: `["redis>=5.0", "prometheus-client>=0.20"]`

**步骤27** - 重构限流: 抽象后端接口
- File: `src/lvyan/api/rate_limit.py`
- 定义 `RateLimitBackend` 协议 (Protocol)
- `InMemoryBackend`: 现有滑动窗口实现
- `RedisBackend`: 基于 Redis sorted set 的滑动窗口
- 配置: `RATE_LIMIT_BACKEND=redis|memory`

**步骤28** - Redis 不可用策略
- File: `src/lvyan/api/rate_limit.py`
- Redis 连接失败时:
  - run/upload/hitl (高成本写): 返回 503
  - 读取/健康检查: 不受影响 (pass-through)
- 健康检查端点: 报告 redis 状态

**步骤29** - 认证用户限流改为 per-user-id
- File: `src/lvyan/api/rate_limit.py`
- 当请求带有已认证 user_id 时，限流 key 使用 user_id（非 IP）
- 匿名请求: 使用 X-Forwarded-For 可信代理解析后的 IP

**步骤30** - `config.py` 新增 Redis/限流相关配置
- File: `src/lvyan/config.py`
- 新增字段:
  - `redis_url: str = ""`
  - `rate_limit_backend: str = "memory"` (memory|redis)
  - `rls_enforced: bool = False` (生产必须 true)
- `validate_runtime_config()`: 生产模式强制 rls_enforced=true, rate_limit_backend=redis

### 2.3 多租户测试

**步骤31** - 跨租户隔离测试 (API 层)
- File: `tests/security/test_tenant_isolation.py` (新建)
- 测试: 用户A创建 thread → 用户B GET/DELETE → 404
- 测试: 用户A创建 run → 用户B HITL/cancel → 404
- 测试: case_workspace 跨用户访问 → 404

**步骤32** - 跨租户隔离测试 (直接 SQL)
- File: `tests/security/test_rls_direct_sql.py` (新建)
- 测试: SET app.user_id='A' → INSERT → SET app.user_id='B' → SELECT → 0 rows
- 测试: checkpoint 恢复跨租户 → 失败
- 测试: 并发访问同一资源

**步骤33** - HITL 审批跨租户测试
- File: `tests/security/test_hitl_tenant.py` (新建)
- 测试: 用户A的 run 进入 awaiting_hitl → 用户B提交审批 → 拒绝

**步骤34** - 限流测试
- File: `tests/unit/test_rate_limit_redis.py` (新建)
- 测试: Redis backend 基本限流功能
- 测试: Redis 不可用 → 写接口 503, 读接口正常
- 测试: per-user 限流独立于 per-IP

**步骤35** - 生产配置校验测试
- File: `tests/unit/test_config_production.py` (新建)
- 测试: production + rls_enforced=false → 启动失败
- 测试: production + rate_limit_backend=memory → 启动失败
- 测试: production + 无 CASE_VAULT_KEY → 启动失败

---

## 阶段3: 架构拆分、异步化与优雅停机 (2-3周, 步骤 36-55)

### 3.1 路由模块拆分

**步骤36** - 拆分 `server.py` → agent 路由
- File: `src/lvyan/api/routes_agent.py` (新建)
- 迁移: /api/agent/run, /api/agent/stream, /api/agent/state, /api/agent/threads, /api/agent/hitl, /api/agent/cancel
- server.py 保留 `create_app()` 装配函数

**步骤37** - 拆分 `server.py` → upload 路由
- File: `src/lvyan/api/routes_upload.py` (新建)
- 迁移: /api/upload 相关代码

**步骤38** - 拆分 `server.py` → health 路由
- File: `src/lvyan/api/routes_health.py` (新建)
- 迁移: /livez, /readyz, /api/health

**步骤39** - `server.py` 瘦身为 `create_app` 装配器
- File: `src/lvyan/api/server.py`
- 仅保留: create_app(), 依赖装配, 中间件注册, 生命周期管理
- 通过 `app.include_router()` 引入各路由模块

### 3.2 SSE/Run 模块拆分

**步骤40** - 拆分 `sse.py` → RunManager 服务
- File: `src/lvyan/api/run_manager.py` (新建)
- 迁移 RunManager 类及其所有方法
- sse.py 重导出保持向后兼容

**步骤41** - 拆分 `sse.py` → HITL 恢复服务
- File: `src/lvyan/api/hitl_service.py` (新建)
- 迁移 HITL resume/cancel 逻辑

**步骤42** - 拆分 `sse.py` → SSE 事件流模块
- File: `src/lvyan/api/sse_stream.py` (新建)
- 迁移 format_sse_event, SSE generator 逻辑
- sse.py 保留重导出 (向后兼容)

### 3.3 Lexical 模块拆分

**步骤43** - 拆分 `lexical.py` → 安全索引存储
- 已在步骤1完成 (`safe_index.py`)

**步骤44** - 拆分 `lexical.py` → BM25 构建器
- File: `src/lvyan/retrieval/bm25_builder.py` (新建)
- 迁移 BM25 索引构建、序列化/反序列化逻辑

**步骤45** - 拆分 `lexical.py` → 搜索器
- File: `src/lvyan/retrieval/bm25_searcher.py` (新建)
- 迁移 bm25_search, search 公共函数
- lexical.py 保留公共接口重导出

### 3.4 异步 LLM 客户端

**步骤46** - 新增异步 LLM 客户端
- File: `src/lvyan/llm/async_client.py` (新建)
- 实现 `achat()`, `achat_json()`, `achat_structured()`
- 共享 `httpx.AsyncClient` 实例（连接池、重试、取消、超时）
- shutdown 时关闭客户端
- 信号量: `MAX_LLM_CONCURRENCY` 限制并发 LLM 请求

**步骤47** - 新增检索并发信号量
- File: `src/lvyan/retrieval/__init__.py` 或新模块
- `MAX_RETRIEVAL_CONCURRENCY` 信号量
- 磁盘转换和 CPU 型 BM25 构建通过有界线程池

### 3.5 节点异步化

**步骤48** - fact_extractor 异步化
- File: `src/lvyan/nodes/fact_extractor.py`
- `fact_extractor(state) → async fact_extractor(state)`
- 内部 LLM 调用改用 `await achat_json()`

**步骤49** - planner 异步化
- File: `src/lvyan/nodes/planner.py`
- `planner(state) → async planner(state)`

**步骤50** - parallel_retrieval + authority_resolver 异步化
- Files: `src/lvyan/nodes/retrieve_statutes.py`, `src/lvyan/nodes/evidence_analyzer.py`
- 网络I/O (OpenSearch, httpx) 改为 await

**步骤51** - legal_reasoner + critic + citation_verifier 异步化
- Files: 对应3个节点文件
- LLM 调用改用 `await achat_structured()`

**步骤52** - CLI 改用 `graph.ainvoke()`
- File: `src/lvyan/cli.py`
- `graph.invoke()` → `asyncio.run(graph.ainvoke())`

### 3.6 优雅停机

**步骤53** - 实现生命周期管理器
- File: `src/lvyan/api/lifecycle.py` (新建)
- FastAPI lifespan:
  - startup: 初始化资源（LLM client, Redis, DB pool, checkpointer, telemetry）
  - shutdown:
    1. readyz → 503, 拒绝新 run
    2. 等待最多 SHUTDOWN_GRACE_SECONDS (默认30s)
    3. 未完成运行持久化为 `interrupted`
    4. SSE 发送 `server_shutdown` 事件
    5. 取消所有 asyncio.Task
    6. 关闭 LLM client, Redis, DB, checkpointer, telemetry

**步骤54** - agent_runs.status 新增 `interrupted`
- File: `migrations/010_interrupted_status.sql` (新建)
- ALTER CHECK 约束: 添加 `'interrupted'` 到 status 枚举
- SSE 新增 `server_shutdown` 事件类型

**步骤55** - Docker 停止宽限期配置
- File: `docker-compose.yml`
- app service: `stop_grace_period: 45s`
- 新增环境变量: `SHUTDOWN_GRACE_SECONDS`

---

## 阶段4: 日志、指标和质量门禁 (2周, 步骤 56-67, 与阶段3并行)

### 4.1 结构化日志

**步骤56** - 引入 structlog
- File: `pyproject.toml` → 添加 `structlog` 依赖
- File: `src/lvyan/observability/logging.py` (新建)
- 开发: 彩色文本输出
- 生产 (LOG_FORMAT=json): JSON 格式
- contextvars 自动绑定: run_id, thread_id, tenant_id, trace_id

**步骤57** - 日志脱敏策略
- File: `src/lvyan/observability/logging.py`
- 处理器: 禁止记录原始问题、附件正文、提示词、令牌、明文用户标识
- tenant_id 使用 HMAC 派生的稳定不可逆标识

### 4.2 Prometheus 指标

**步骤58** - 实现 `/metrics` 端点
- File: `src/lvyan/observability/prometheus.py` (新建)
- 使用 `prometheus-client`
- 指标:
  - `lvyan_http_requests_total` (method, path, status)
  - `lvyan_http_request_duration_seconds` (method, path)
  - `lvyan_active_runs` (gauge)
  - `lvyan_sse_connections` (gauge)
  - `lvyan_run_result_total` (status: completed/failed/cancelled/interrupted)
  - `lvyan_llm_requests_total` (node, status)
  - `lvyan_llm_duration_seconds` (node)
  - `lvyan_llm_tokens_total` (node, direction)
  - `lvyan_retrieval_duration_seconds` (backend)
  - `lvyan_hitl_pending` (gauge)
  - `lvyan_rate_limit_rejected_total`
  - `lvyan_index_rebuild_total`
- 访问保护: Bearer METRICS_AUTH_TOKEN 或内部网络
- 标签禁止高基数字段 (run_id, user_id, 问题文本)

**步骤59** - 指标中间件注入
- File: `src/lvyan/api/server.py` (create_app)
- 注册 Prometheus ASGI 中间件 (request count/latency)
- RunManager 注入指标回调

### 4.3 测试覆盖率门禁

**步骤60** - 添加 pytest-cov 配置
- File: `pyproject.toml`
- dev 依赖添加 `pytest-cov`
- `[tool.pytest.ini_options]` 添加 `--cov=src/lvyan --cov-report=xml --cov-report=html`

**步骤61** - CI 强制覆盖率门禁
- File: `.github/workflows/ci.yml`
- 第一阶段: 生成覆盖率报告, 上传 artifact
- 第二阶段: `--cov-fail-under=70` (总体)
- 安全/租户模块: `--cov-fail-under=85` (单独步骤)

### 4.4 补齐直接单元测试

**步骤62** - rate_limit 单元测试
- File: `tests/unit/test_rate_limit.py` (新建/扩展)
- 测试滑动窗口、GC、路径匹配、限额配置

**步骤63** - workspace routes 单元测试
- File: `tests/unit/test_routes_workspace.py` (新建)
- 测试 CRUD、权限边界、错误响应

**步骤64** - RunContext / exact_match / query_rewriter / file_converter 单元测试
- Files: 对应 test_*.py (新建或扩展)

### 4.5 CI 集成与 E2E

**步骤65** - PR 流水线扩展金标集
- File: `.github/workflows/ci.yml`
- PR: 至少7条完整 Graph 案例 (每个类别1条)
- 夜间: 40+ 金标集 + 真实 PG/Redis/OpenSearch + mock LLM

**步骤66** - 服务级 E2E 测试
- File: `tests/e2e/test_full_flow.py` (新建)
- 上传材料 → 提问 → 分诊 → 检索 → 推理 → 输出 → 历史恢复
- HITL 恢复、取消、重启
- 租户越权尝试
- 依赖故障降级

**步骤67** - E2E 阻断 PR 合并
- File: `.github/workflows/ci.yml`
- E2E 失败 → `required` check 阻断合并

---

## 阶段5: LLM 能力与多来源案例库 (3-4周, 步骤 68-78)

### 5.1 LLM 增强节点

**步骤68** - 分诊节点 LLM 增强
- File: `src/lvyan/nodes/triage.py`
- 定义 `TriageOutput` Pydantic 模型 (case_type enum, jurisdiction enum, complexity, risk_level)
- LLM 优先 (chat_structured) + 规则降级
- 输出必须通过枚举校验

**步骤69** - 缺失事实节点 LLM 增强
- File: `src/lvyan/nodes/planner.py` (missing_fact_assessor 部分)
- 定义 `MissingFactAssessment` Pydantic 模型
- LLM 评估关键事实缺失 + 规则降级

**步骤70** - 证据分析节点 LLM 增强
- File: `src/lvyan/nodes/evidence_analyzer.py`
- 定义 `EvidenceAnalysisOutput` Pydantic 模型
- LLM 识别证据缺口 + 规则降级

**步骤71** - 权威解析节点 LLM 增强
- File: `src/lvyan/nodes/evidence_analyzer.py` (authority_resolver)
- 定义 `AuthorityResolution` Pydantic 模型
- 约束: LLM 仅允许在已检索候选中排序，不得生成新法条/案号
- 接地校验: 所有输出 source_id 必须存在于 state.statutes

**步骤72** - Critic 节点 LLM 增强
- File: `src/lvyan/nodes/critic.py`
- 定义 `LLMCriticReport` Pydantic 模型
- LLM 对抗性评审 + 规则降级
- 证据引用和事实接地校验

**步骤73** - Prompt 注册表与节点级降级指标
- File: `src/lvyan/llm/prompt_registry.py` (新建)
- 版本化 prompt 管理 (版本号, 模板内容, 适用节点)
- 节点级降级指标: `lvyan_llm_fallback_total{node}`
- 支持按节点灰度启用 LLM (不使用单一总开关)

### 5.2 多来源案例库

**步骤74** - 定义统一 `CaseProvider` 和 `CaseRecord`
- File: `src/lvyan/retrieval/case_providers/__init__.py` (新建)
- `CaseRecord` dataclass:
  - source, source_id, case_number, court, cause_of_action
  - judgment_date, fact_summary, reasoning, judgment_result
  - source_url, authorization_scope, updated_at
- `CaseProvider` Protocol:
  - `search(query, top_k) -> list[CaseRecord]`
  - `get_by_id(source_id) -> CaseRecord | None`
  - `health_check() -> bool`

**步骤75** - 精编规则库适配器
- File: `src/lvyan/retrieval/case_providers/curated.py` (新建)
- 从现有 knowledge/curated 读取
- 实现 CaseProvider 接口
- 权重: 0.6 (最低优先级降级源)

**步骤76** - 官方离线数据适配器
- File: `src/lvyan/retrieval/case_providers/official.py` (新建)
- 读取 JSONL/Parquet 离线数据
- 实现 CaseProvider 接口
- 权重: 1.0 (最高优先级)

**步骤77** - 商业案例网关适配器
- File: `src/lvyan/retrieval/case_providers/commercial_gateway.py` (新建)
- 统一 REST 接口: Bearer 认证 + 标准搜索请求/响应
- 不绑定具体供应商 SDK
- 权重: 0.9
- 配置: `CASE_PROVIDERS`, 网关地址/令牌

**步骤78** - 聚合排序与去重
- File: `src/lvyan/retrieval/case_providers/aggregator.py` (新建)
- 聚合排序: 按配置权重 (官方1.0, 商业0.9, 精编0.6)
- 去重: 先按案号; 无案号时按标准化内容 SHA-256
- 每项结果标注来源和更新时间
- 来源不可用时降级: 标记在输出和指标中
- 禁止: 网页抓取, 机构私有案件进入公共索引

---

## 新增公共配置总览

```python
# config.py Settings 新增字段
case_vault_allow_insecure: bool = False
redis_url: str = ""
rate_limit_backend: str = "memory"       # memory | redis
rls_enforced: bool = False
log_format: str = "text"                 # text | json
metrics_enabled: bool = False
metrics_auth_token: str = ""
shutdown_grace_seconds: int = 30
max_llm_concurrency: int = 10
max_retrieval_concurrency: int = 20
case_providers: str = "curated"          # curated,official,commercial
commercial_gateway_url: str = ""
commercial_gateway_token: str = ""
```

## 生产配置校验强制项

```python
# validate_runtime_config() 生产模式必须满足:
assert rls_enforced == True
assert rate_limit_backend == "redis"
assert redis_url != ""
assert case_vault_key != "" (32 bytes hex)
assert log_format == "json"
assert checkpointer_backend == "postgres"
assert metrics_enabled == True
```

---

## 发布顺序

1. **P1 合并**: 安全格式 + 加密修复, Dockerfile 预生成索引
2. **P2 合并**: 预发布环境回填 + RLS + 攻击测试 → 切换 runtime role
3. **P3 合并**: 模块拆分 (API/导入兼容, 每次拆分单独 commit) → 异步化 → 停机
4. **P4 合并**: structlog + Prometheus + 覆盖率门禁 + E2E
5. **P5 合并**: LLM节点灰度 (fact_extractor → planner → reasoner → critic) → 案例源
6. **放量**: 10% → 50% → 100%; 异常即回滚
