-- P1-7：Agent run / thread 元数据表（PostgreSQL DDL）。
--
-- 用于把进程内的 ``RunManager._runs`` 与 ``data/thread_index.json`` 迁出，
-- 支持服务重启 / 多实例部署下的：
--   1. ``agent_runs.run_id`` → ``thread_id`` 反查（HITL 审批可跨实例）
--   2. ``agent_threads.user_id`` 过滤（按用户列出会话）
--   3. ``interrupt_payload`` 持久化（实例崩溃后可恢复 HITL）
--
-- 与 ``langgraph`` 自带的 ``checkpoints`` / ``checkpoint_writes`` 表分离：
-- 本表仅存「业务元数据」，checkpoint 仍由 ``PostgresSaver`` 管理。

CREATE TABLE IF NOT EXISTS agent_threads (
    thread_id    TEXT PRIMARY KEY,
    user_id      TEXT NOT NULL DEFAULT 'anonymous',
    title        TEXT NOT NULL DEFAULT '',
    complexity   TEXT NOT NULL DEFAULT 'light',
    has_output   BOOLEAN NOT NULL DEFAULT FALSE,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_agent_threads_user
    ON agent_threads (user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS agent_runs (
    run_id            TEXT PRIMARY KEY,
    thread_id         TEXT NOT NULL REFERENCES agent_threads(thread_id) ON DELETE CASCADE,
    user_id           TEXT NOT NULL DEFAULT 'anonymous',
    status            TEXT NOT NULL DEFAULT 'started',
    -- started / running / awaiting_hitl / completed / failed / cancelled
    interrupt_payload JSONB,
    final_output      TEXT,
    error             TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at      TIMESTAMPTZ,
    expires_at        TIMESTAMPTZ  -- TTL，gc 时清理
);

CREATE INDEX IF NOT EXISTS idx_agent_runs_thread ON agent_runs (thread_id);
CREATE INDEX IF NOT EXISTS idx_agent_runs_user_status
    ON agent_runs (user_id, status, created_at DESC);

CREATE TABLE IF NOT EXISTS agent_messages (
    message_id  BIGSERIAL PRIMARY KEY,
    thread_id   TEXT NOT NULL REFERENCES agent_threads(thread_id) ON DELETE CASCADE,
    run_id      TEXT NOT NULL REFERENCES agent_runs(run_id) ON DELETE CASCADE,
    user_id     TEXT NOT NULL DEFAULT 'anonymous',
    role        TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content     TEXT NOT NULL,
    attachments JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (run_id, role)
);

CREATE INDEX IF NOT EXISTS idx_agent_messages_thread
    ON agent_messages (thread_id, message_id);

-- updated_at 触发器
CREATE OR REPLACE FUNCTION trg_agent_threads_set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS agent_threads_set_updated_at ON agent_threads;
CREATE TRIGGER agent_threads_set_updated_at
    BEFORE UPDATE ON agent_threads
    FOR EACH ROW EXECUTE FUNCTION trg_agent_threads_set_updated_at();
