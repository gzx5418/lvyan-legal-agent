# 律言法律智能体 · Agent Runtime

> 基于 LangGraph 编排的中国法律智能 Agent，证据优先，引用可验证。

## 核心能力

- **12 节点 LangGraph 状态图**：预检 → 管辖分流 → 事实抽取 → 缺失评估 → 规划 → 法条检索 → 权威解析 → 法律推理 → 评审 → 引用校验 → 生成 → 安全护栏
- **四路混合检索**：BM25 词法 + Dense 向量 + 规则匹配 + 版本感知，85639 条法规chunk
- **三种输出模式**：简要（日常咨询快答）/ 深度（案件分析报告）/ 文书（法律文书生成）
- **文件上传与转换**：集成微软 markitdown，支持 PDF/DOCX/PPTX/XLSX 转 Markdown
- **视觉模型图片识别**：Qwen3-VL-8B-Instruct，支持合同/证据截图 OCR + 法律场景描述
- **Human-in-the-Loop**：不可逆操作人工审批
- **完备前端**：对话 / 文件上传 / 历史管理 / 消息操作 / 导出 / 快捷键

## 技术栈

| 层 | 技术 |
|---|---|
| 编排 | LangGraph v1 + PostgreSQL checkpoint |
| API | FastAPI + SSE（Server-Sent Events）|
| 前端 | 原生 HTML/CSS/JS（无框架依赖）|
| LLM | DeepSeek-V4-Flash（硅基流动）|
| Embedding | BAAI/bge-m3（1024 维）|
| Reranker | BAAI/bge-reranker-v2-m3 |
| Vision | Qwen/Qwen3-VL-8B-Instruct |
| 文档转换 | microsoft/markitdown |
| 可观测性 | OpenTelemetry + Langfuse |

## 快速开始

### 1. 安装依赖

```bash
cd AGENT
pip install -e .
pip install python-multipart markitdown
```

### 2. 配置环境变量

```bash
cp .env.example .env
# 编辑 .env 填入你的 SiliconFlow API Key
```

### 3. 启动服务

```bash
# 方式一：直接 uvicorn
PYTHONPATH=src python -m uvicorn lvyan.api.server:app --host 0.0.0.0 --port 8000

# 方式二：开发模式（热重载）
PYTHONPATH=src python -m uvicorn lvyan.api.server:app --reload --port 8000
```

访问 http://localhost:8000 即可使用前端界面。

### 4. 命令行使用

```bash
# 简要模式
PYTHONPATH=src python -m lvyan "劳动合同到期公司不续签，有补偿吗？"

# 深度模式
PYTHONPATH=src python -m lvyan --mode deep "被网络诽谤如何维权？"

# 文书模式
PYTHONPATH=src python -m lvyan --mode document "帮我写一份劳动争议起诉状"
```

## API 端点

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/agent/run` | 启动 Agent 运行 |
| GET | `/api/agent/stream/{run_id}` | SSE 流式获取节点进度 |
| GET | `/api/agent/state/{thread_id}` | 获取会话状态 |
| DELETE | `/api/agent/state/{thread_id}` | 删除会话 |
| GET | `/api/agent/threads` | 列出所有会话 |
| POST | `/api/agent/hitl/{run_id}` | 人工审批响应 |
| POST | `/api/upload` | 文件上传（自动转 Markdown）|
| GET | `/api/health` | 健康检查 |

## 项目结构

```
AGENT/
├── src/lvyan/
│   ├── api/              # FastAPI + SSE + 前端静态文件
│   ├── graph/            # LangGraph 状态图定义
│   ├── nodes/            # 12 个专家节点
│   ├── retrieval/        # 四路混合检索
│   ├── schemas/          # Pydantic 数据模型
│   ├── tools/            # 工具集（文件转换/计算器/导出等）
│   ├── validators/       # 安全验证器
│   ├── memory/           # 短期记忆 + 案件库
│   └── observability/    # 追踪与指标
├── knowledge/            # 精编法律知识库
├── templates/            # 法律文书模板
├── prompts/              # 提示词标准
├── tests/                # 539+ 测试
└── pyproject.toml
```

## 测试

```bash
PYTHONPATH=src python -m pytest tests/ -q
```

## 许可证

本项目仅用于学习和研究目的。法律分析结果仅供参考，不构成正式法律意见。
