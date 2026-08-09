-- P3: 新增 'interrupted' 运行状态（优雅停机使用）。
-- agent_runs.status 原有值：started / running / awaiting_hitl / completed / failed / cancelled
-- 新增：interrupted（服务关闭时未完成的运行，重启后可从 checkpoint 恢复）

-- 原 CHECK 约束不含 interrupted，需要重建
-- 先删除旧约束（如果存在）
DO $$
BEGIN
    -- agent_runs 的 status CHECK 约束名可能因 PG 版本不同而变化
    -- 使用动态查找并删除
    PERFORM 1 FROM pg_constraint
    WHERE conrelid = 'agent_runs'::regclass
      AND contype = 'c'
      AND pg_get_constraintdef(oid) LIKE '%status%';

    IF FOUND THEN
        EXECUTE (
            SELECT format('ALTER TABLE agent_runs DROP CONSTRAINT %I',
                         conname)
            FROM pg_constraint
            WHERE conrelid = 'agent_runs'::regclass
              AND contype = 'c'
              AND pg_get_constraintdef(oid) LIKE '%status%'
            LIMIT 1
        );
    END IF;
END
$$;

-- 添加包含 interrupted 的新约束
ALTER TABLE agent_runs
    ADD CONSTRAINT agent_runs_status_check
    CHECK (status IN ('started', 'running', 'awaiting_hitl', 'completed', 'failed', 'cancelled', 'interrupted'));
