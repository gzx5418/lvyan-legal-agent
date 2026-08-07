<div align="center">
  <img src="docs/assets/lvyan-readme-hero.png" alt="律言法律智能体：法律文书、可验证引用与 Agent 工作流" width="100%" />
</div>

<h1 align="center">律言法律智能体 · Lvyan Legal Agent</h1>

<p align="center">
  面向中国大陆法律场景的证据优先型 AI Agent。<br />
  以 LangGraph 编排事实提取、版本感知检索、法律推理、引用审计和安全输出。
</p>

<p align="center">
  <a href="https://github.com/gzx5418/lvyan-legal-agent/actions/workflows/ci.yml"><img src="https://github.com/gzx5418/lvyan-legal-agent/actions/workflows/ci.yml/badge.svg" alt="CI" /></a>
  <img src="https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white" alt="Python 3.11+" />
  <img src="https://img.shields.io/badge/LangGraph-1.x-1C3C3C" alt="LangGraph 1.x" />
  <img src="https://img.shields.io/badge/FastAPI-SSE-009688?logo=fastapi&logoColor=white" alt="FastAPI + SSE" />
  <img src="https://img.shields.io/badge/License-MIT-gold" alt="MIT License" />
</p>

> [!IMPORTANT]
> 律言用于学习、研究与辅助分析。生成内容不构成律师出具的正式法律意见，也不能替代对原始证据、有效法源和具体案情的人工核验。

## 为什么是律言

普通问答模型容易忽略证据缺口、混淆法规版本，或在最终答案中生成无法核验的法条。律言把这些风险放进显式工作流：

- **先事实、后结论**：提取当事人、时间、金额、行为和证据，阻断性信息不足时先追问。
- **版本感知检索**：支持 `law_as_of_date`，区分法规当前状态与案件发生时的有效状态。
- **最终文本审计**：对用户实际看到的输出执行法条存在性、内容匹配、时点有效性和结论支撑校验。
- **受控迭代**：Critic、重检索与输出重写均设上限，避免失控循环。
- **可恢复执行**：PostgreSQL checkpoint 与 run metadata 支持服务重启后的 HITL 恢复。

## 核心能力

| 能力 | 实现 |
|---|---|
| 12 节点 Agent 图 | 预检、管辖、事实提取、缺失评估、规划、检索、权威解析、推理、评审、生成、引用校验、输出护栏 |
| 混合检索 | BM25、Dense、规则匹配、版本过滤与 Reranker |
| 历史法规 | 请求级 `law_as_of_date` 贯穿检索与完整引用审计 |
| 三种输出模式 | `light` 快答、`deep` 深度分析、`document` 文书生成 |
| 文档处理 | PDF、DOCX、PPTX、XLSX 等格式转 Markdown；图片 OCR/理解 |
| 引用可信度 | Citation、Authority Status、Grounding 三层校验 |
| Human-in-the-Loop | 不可逆操作人工审批；PostgreSQL 原子 claim 防止重复恢复 |
| API 与前端 | FastAPI、SSE、文件上传、历史会话与原生 Web UI |
| 可观测性 | OpenTelemetry、Langfuse、节点耗时与成本摘要 |

## 工作流

```mermaid
flowchart LR
    A[预检] --> B[管辖分流]
    B --> C[事实提取]
    C --> D[缺失事实评估]
    D -->|需要补充| U[向用户追问]
    D -->|继续| E[规划]
    E --> F[并行检索]
    F --> G[权威解析]
    G --> H[法律推理]
    H --> I[Critic 评审]
    I -->|重试| H
    I -->|通过| J[生成初稿]
    J --> K[最终文本引用审计]
    K -->|重检索| F
    K -->|通过| L[输出安全护栏]
    L -->|重写| J
    L -->|完成| M[最终输出]
```

## 快速开始

### 环境要求

- Python 3.11+
- PostgreSQL 16（生产持久化与跨实例 HITL 必需）
- 可选：OpenSearch、MinIO、Langfuse

### 1. 安装

```bash
git clone https://github.com/gzx5418/lvyan-legal-agent.git
cd lvyan-legal-agent

python -m venv .venv
# Linux / macOS
source .venv/bin/activate
# Windows PowerShell
# .\.venv\Scripts\Activate.ps1

python -m pip install --upgrade pip
pip install -e ".[dev,documents]"
```

仅安装核心运行时：

```bash
pip install -e .
```

### 2. 配置

```bash
# Linux / macOS
cp .env.example .env

# Windows PowerShell
# Copy-Item .env.example .env
```

至少配置模型网关：

```dotenv
MODEL_GATEWAY_URL=https://api.siliconflow.cn
MODEL_GATEWAY_API_KEY=your-api-key
CHAT_MODEL=deepseek-ai/DeepSeek-V4-Flash
EMBEDDING_MODEL=BAAI/bge-m3
RERANKER_MODEL=BAAI/bge-reranker-v2-m3
VISION_MODEL=Qwen/Qwen3-VL-8B-Instruct
```

生产环境还应显式配置：

```dotenv
DATABASE_URL=postgresql+psycopg://lvyan:strong-password@localhost:5432/lvyan
AUTH_ENABLED=true
CORS_ALLOWED_ORIGINS=https://your-frontend.example.com
```

认证启用后，服务只能部署在可信 API Gateway / OIDC Proxy 后方：网关必须剥离
客户端自行携带的 `X-User-ID`，再注入经认证的身份。若选择让服务校验 Bearer JWT，
还必须设置 `JWT_VERIFY_IN_PROCESS=true` 以及 `JWT_JWKS_URL`、`JWT_ISSUER`、
`JWT_AUDIENCE`；未验签的 Bearer JWT 会被拒绝。

### 3. 启动服务

提供两种方式：**Docker 一键部署**（推荐远程部署）或**本地开发**（pip install + uvicorn）。

#### 方式 A：Docker 一键部署（推荐）

仅需安装 Docker 与 Docker Compose，无需本地 Python 环境。compose 会拉起应用
容器 + PostgreSQL + OpenSearch + MinIO，migrations 首次启动自动执行。

```bash
# 1. 准备配置（已在第 2 步完成）
cp .env.example .env

# 2. 构建镜像并启动全部服务
docker compose up -d --build

# 3. 查看应用日志
docker compose logs -f app

# 4. 健康检查
curl http://localhost:8000/livez   # {"status":"ok"}
curl http://localhost:8000/readyz  # status=ready 表示全部依赖就绪
```

最小化部署（仅 app + PostgreSQL，不需要检索/对象存储）：

```bash
docker compose up -d --build postgres app
```

可选启动 Langfuse 可观测性栈：

```bash
docker compose --profile full up -d --build
```

应用容器特性：

- 多阶段构建，非 root 用户运行，`/livez` 健康检查
- 生产模式默认开启（`RUNTIME_MODE=production` + `CHECKPOINTER_BACKEND=postgres`），
  禁止任何静默降级
- `migrations/*.sql` 挂载到 postgres 的 `/docker-entrypoint-initdb.d/`，
  首次启动自动建表；应用层 `_ensure_schema` 兜底
- 持久化卷：`lvyan-app-data`（上传与线程索引）、`lvyan-app-outputs`（文书导出）、
  `lvyan-app-manifests`（检索索引缓存）
- 端口映射可通过 `.env` 的 `APP_PORT` / `POSTGRES_PORT` 等覆盖

> ⚠️ `.env.example` 中的默认密码仅方便本地试用，生产部署前必须替换为强随机值，
> 并按需关闭对外的 5432/9200/9000/9001 端口映射。

#### 方式 B：本地开发

仅启动基础设施（PostgreSQL 必需，OpenSearch/MinIO/Langfuse 可选）：

```bash
docker compose up -d postgres        # 最小化
docker compose up -d                 # 全部依赖
```

`docker-compose.yml` 中的密码仅供本地开发，生产部署前必须替换并限制端口暴露。

安装依赖并启动服务：

```bash
python -m uvicorn lvyan.api.server:app --host 0.0.0.0 --port 8000
```

开发热重载：

```bash
python -m uvicorn lvyan.api.server:app --reload --port 8000
```

打开 [http://localhost:8000](http://localhost:8000) 使用内置前端。

## 命令行

```bash
# 日常咨询
python -m lvyan "劳动合同到期公司不续签，有补偿吗？"

# 深度案件分析
python -m lvyan --mode deep "被网络诽谤后应如何固定证据并维权？"

# 法律文书
python -m lvyan --mode document "根据已知事实生成一份劳动争议起诉状"
```

## API 示例

### 启动一次历史时点分析

```bash
curl -X POST http://localhost:8000/api/agent/run \
  -H "Content-Type: application/json" \
  -d '{
    "query": "2018 年签订的合同发生争议，应适用什么法律？",
    "complexity": "deep",
    "law_as_of_date": "2018-06-01"
  }'
```

响应：

```json
{
  "run_id": "run-...",
  "thread_id": "thread-...",
  "status": "started"
}
```

### 订阅运行事件

```bash
curl -N http://localhost:8000/api/agent/stream/run-...
```

### 主要端点

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/api/agent/run` | 启动 Agent，可传 `law_as_of_date` |
| `GET` | `/api/agent/stream/{run_id}` | 获取节点事件与最终输出 |
| `POST` | `/api/agent/hitl/{run_id}` | 提交人工审批决策 |
| `GET` | `/api/agent/state/{thread_id}` | 获取会话状态摘要 |
| `DELETE` | `/api/agent/state/{thread_id}` | 删除会话 |
| `GET` | `/api/agent/threads` | 列出当前用户会话 |
| `POST` | `/api/upload` | 上传并转换证据文件 |
| `GET` | `/livez` | 进程存活检查 |
| `GET` | `/readyz` | 数据库、元数据表和检索就绪检查 |
| `GET` | `/api/health` | 综合健康状态 |

完整交互式接口文档：启动服务后访问 [http://localhost:8000/docs](http://localhost:8000/docs)。

## 多实例部署说明

- PostgreSQL 保存 LangGraph checkpoint、thread owner 与 run metadata。
- HITL 使用原子状态领取，同一审批只有一个实例能够恢复。
- 已完成 run 的最终输出可从其他实例恢复。
- **运行中的实时 SSE 仍需要负载均衡器开启 sticky session / session affinity。** 如需任意实例订阅实时事件，应接入 Redis Streams、Pub/Sub 或独立事件表。
- 生产环境应在 API Gateway / OIDC Proxy 完成身份验证，并可信注入 `X-User-ID`。

## 项目结构

```text
.
├── src/lvyan/
│   ├── api/              # FastAPI、SSE、认证与内置前端
│   ├── graph/            # LangGraph 图与路由策略
│   ├── nodes/            # 12 个 Agent 节点
│   ├── retrieval/        # 混合检索、重排与法规版本解析
│   ├── validators/       # 引用、权威状态、接地与输出验证
│   ├── memory/           # Checkpoint、run metadata 与案件记忆
│   ├── schemas/          # Pydantic 状态与领域模型
│   ├── tools/            # 法规、案例、文档和计算工具
│   └── observability/    # 指标、追踪与成本记录
├── migrations/           # PostgreSQL 业务元数据迁移
├── knowledge/            # 精编法律知识库
├── templates/            # 分析报告与法律文书模板
├── prompts/              # 法律推理、证据和输出标准
├── tests/                # unit / integration / security / retrieval / evals
├── Dockerfile            # 多阶段构建镜像（非 root、健康检查）
├── .dockerignore         # 构建上下文排除清单
└── docker-compose.yml    # 一键部署：app + postgres + opensearch + minio
```

## 测试与质量

```bash
# 全部测试
python -m pytest tests/ -q

# 排除依赖完整法律库的慢测试
python -m pytest tests/ -q -m "not slow"

# 静态检查
python -m ruff check src/ tests/
```

CI 包含单元与集成测试、金标集回归、Agent Pipeline 回归和 Ruff 检查。

## 安全与隐私

- 上传文档按不可信输入处理，并检测提示注入。
- thread、run 和附件均执行用户归属校验。
- 最终输出经过隐私、引用、接地和格式护栏。
- 请勿把真实生产密钥、当事人敏感信息或未脱敏证据提交到仓库。
- 本地 `docker-compose.yml` 默认凭据不适用于生产。

## 参与贡献

欢迎提交 Issue 和 Pull Request。涉及法规数据、检索指标或法律结论时，请同时提供：

1. 可复现输入与预期结果；
2. 法源及适用日期；
3. 新增或更新的回归测试；
4. 对安全、隐私和向后兼容性的影响说明。

## License

项目代码按 [MIT License](https://opensource.org/licenses/MIT) 使用。法律数据、官方示范文本和第三方模型分别受其原始许可与使用条款约束。
