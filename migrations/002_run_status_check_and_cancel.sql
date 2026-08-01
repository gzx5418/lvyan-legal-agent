-- P1-2 / 五（数据库设计优化）：run 状态约束 + 跨实例取消字段。
--
-- 1. agent_runs.cancel_requested_at：跨实例协作取消的时间戳。
--    RunManager.request_cancel 置位，worker 轮询 is_cancel_requested 发现后停止。
-- 2. agent_runs.status CHECK：防止存储层被错误调用写入非法状态。
--    使用 DO + ALTER TABLE CONVERSION，兼容已存在非法历史数据（仅约束未来写入）。
--
-- 本 migration 与 001 在同一 advisory-lock 事务内串行执行（见 _ensure_schema）。

ALTER TABLE agent_runs
    ADD COLUMN IF NOT EXISTS cancel_requested_at TIMESTAMPTZ;

-- status CHECK 约束。若历史库中已存在非法 status，添加约束会失败；这里先做
-- 一次性兜底：把不在白名单的 status 归一化为 'failed'，再加约束，保证幂等。
DO $$
BEGIN
    UPDATE agent_runs
    SET status = 'failed', error = COALESCE(error, '') || ' [normalized from invalid status]'
    WHERE status NOT IN (
        'started', 'running', 'awaiting_hitl',
        'completed', 'failed', 'cancelled', 'abandoned'
    );
END $$;

-- 删除已存在的同名约束（幂等）再重建，避免重复执行报错。
ALTER TABLE agent_runs DROP CONSTRAINT IF EXISTS agent_runs_status_check;
ALTER TABLE agent_runs
    ADD CONSTRAINT agent_runs_status_check CHECK (
        status IN (
            'started', 'running', 'awaiting_hitl',
            'completed', 'failed', 'cancelled', 'abandoned'
        )
    );

-- cancel_requested_at 命中时便于检索
CREATE INDEX IF NOT EXISTS idx_agent_runs_cancel_requested
    ON agent_runs (cancel_requested_at)
    WHERE cancel_requested_at IS NOT NULL;
