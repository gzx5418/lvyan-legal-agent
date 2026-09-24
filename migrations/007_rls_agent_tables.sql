-- P2: 对 agent_threads / agent_runs / agent_messages 启用 RLS。
-- 策略: 每张表直接校验 user_id = current_setting('app.user_id', true)。
-- owner role (lvyan) 因 BYPASSRLS 不受约束；lvyan_runtime 强制受约束。

-- 幂等性：每条 CREATE POLICY 前置 DROP POLICY IF EXISTS 守卫。
-- 背景：docker-entrypoint-initdb.d 与应用层 _ensure_schema（schema_migrations
-- 版本表）是两条独立执行通道，全新数据卷首启时 initdb 先执行本文件、应用
-- 启动后按版本表重放；无守卫会因 "policy already exists" 使生产实例进入
-- crash loop（对照 010_checkpoint_rls.sql 的既有做法）。

-- ============================================================================
-- agent_threads
-- ============================================================================
ALTER TABLE agent_threads ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent_threads FORCE ROW LEVEL SECURITY;

-- 读取策略：仅能看到自己的 thread
DROP POLICY IF EXISTS tenant_select_threads ON agent_threads;
CREATE POLICY tenant_select_threads ON agent_threads
    FOR SELECT
    USING (user_id = current_setting('app.user_id', true));

-- 插入策略：插入时 user_id 必须等于当前租户
DROP POLICY IF EXISTS tenant_insert_threads ON agent_threads;
CREATE POLICY tenant_insert_threads ON agent_threads
    FOR INSERT
    WITH CHECK (user_id = current_setting('app.user_id', true));

-- 更新策略：仅能更新自己的 thread
DROP POLICY IF EXISTS tenant_update_threads ON agent_threads;
CREATE POLICY tenant_update_threads ON agent_threads
    FOR UPDATE
    USING (user_id = current_setting('app.user_id', true))
    WITH CHECK (user_id = current_setting('app.user_id', true));

-- 删除策略：仅能删除自己的 thread
DROP POLICY IF EXISTS tenant_delete_threads ON agent_threads;
CREATE POLICY tenant_delete_threads ON agent_threads
    FOR DELETE
    USING (user_id = current_setting('app.user_id', true));

-- ============================================================================
-- agent_runs
-- ============================================================================
ALTER TABLE agent_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent_runs FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_select_runs ON agent_runs;
CREATE POLICY tenant_select_runs ON agent_runs
    FOR SELECT
    USING (user_id = current_setting('app.user_id', true));

DROP POLICY IF EXISTS tenant_insert_runs ON agent_runs;
CREATE POLICY tenant_insert_runs ON agent_runs
    FOR INSERT
    WITH CHECK (user_id = current_setting('app.user_id', true));

DROP POLICY IF EXISTS tenant_update_runs ON agent_runs;
CREATE POLICY tenant_update_runs ON agent_runs
    FOR UPDATE
    USING (user_id = current_setting('app.user_id', true))
    WITH CHECK (user_id = current_setting('app.user_id', true));

DROP POLICY IF EXISTS tenant_delete_runs ON agent_runs;
CREATE POLICY tenant_delete_runs ON agent_runs
    FOR DELETE
    USING (user_id = current_setting('app.user_id', true));

-- ============================================================================
-- agent_messages
-- ============================================================================
ALTER TABLE agent_messages ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent_messages FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_select_messages ON agent_messages;
CREATE POLICY tenant_select_messages ON agent_messages
    FOR SELECT
    USING (user_id = current_setting('app.user_id', true));

DROP POLICY IF EXISTS tenant_insert_messages ON agent_messages;
CREATE POLICY tenant_insert_messages ON agent_messages
    FOR INSERT
    WITH CHECK (user_id = current_setting('app.user_id', true));

DROP POLICY IF EXISTS tenant_update_messages ON agent_messages;
CREATE POLICY tenant_update_messages ON agent_messages
    FOR UPDATE
    USING (user_id = current_setting('app.user_id', true))
    WITH CHECK (user_id = current_setting('app.user_id', true));

DROP POLICY IF EXISTS tenant_delete_messages ON agent_messages;
CREATE POLICY tenant_delete_messages ON agent_messages
    FOR DELETE
    USING (user_id = current_setting('app.user_id', true));
