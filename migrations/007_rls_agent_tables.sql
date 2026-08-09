-- P2: 对 agent_threads / agent_runs / agent_messages 启用 RLS。
-- 策略: 每张表直接校验 user_id = current_setting('app.user_id', true)。
-- owner role (lvyan) 因 BYPASSRLS 不受约束；lvyan_runtime 强制受约束。

-- ============================================================================
-- agent_threads
-- ============================================================================
ALTER TABLE agent_threads ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent_threads FORCE ROW LEVEL SECURITY;

-- 读取策略：仅能看到自己的 thread
CREATE POLICY tenant_select_threads ON agent_threads
    FOR SELECT
    USING (user_id = current_setting('app.user_id', true));

-- 插入策略：插入时 user_id 必须等于当前租户
CREATE POLICY tenant_insert_threads ON agent_threads
    FOR INSERT
    WITH CHECK (user_id = current_setting('app.user_id', true));

-- 更新策略：仅能更新自己的 thread
CREATE POLICY tenant_update_threads ON agent_threads
    FOR UPDATE
    USING (user_id = current_setting('app.user_id', true))
    WITH CHECK (user_id = current_setting('app.user_id', true));

-- 删除策略：仅能删除自己的 thread
CREATE POLICY tenant_delete_threads ON agent_threads
    FOR DELETE
    USING (user_id = current_setting('app.user_id', true));

-- ============================================================================
-- agent_runs
-- ============================================================================
ALTER TABLE agent_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent_runs FORCE ROW LEVEL SECURITY;

CREATE POLICY tenant_select_runs ON agent_runs
    FOR SELECT
    USING (user_id = current_setting('app.user_id', true));

CREATE POLICY tenant_insert_runs ON agent_runs
    FOR INSERT
    WITH CHECK (user_id = current_setting('app.user_id', true));

CREATE POLICY tenant_update_runs ON agent_runs
    FOR UPDATE
    USING (user_id = current_setting('app.user_id', true))
    WITH CHECK (user_id = current_setting('app.user_id', true));

CREATE POLICY tenant_delete_runs ON agent_runs
    FOR DELETE
    USING (user_id = current_setting('app.user_id', true));

-- ============================================================================
-- agent_messages
-- ============================================================================
ALTER TABLE agent_messages ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent_messages FORCE ROW LEVEL SECURITY;

CREATE POLICY tenant_select_messages ON agent_messages
    FOR SELECT
    USING (user_id = current_setting('app.user_id', true));

CREATE POLICY tenant_insert_messages ON agent_messages
    FOR INSERT
    WITH CHECK (user_id = current_setting('app.user_id', true));

CREATE POLICY tenant_update_messages ON agent_messages
    FOR UPDATE
    USING (user_id = current_setting('app.user_id', true))
    WITH CHECK (user_id = current_setting('app.user_id', true));

CREATE POLICY tenant_delete_messages ON agent_messages
    FOR DELETE
    USING (user_id = current_setting('app.user_id', true));
