-- P2: LangGraph checkpoint 表的 RLS 策略。
-- checkpoint 表由 AsyncPostgresSaver 在应用首次启动时创建；若此初始化脚本运行时
-- 表尚不存在，则由 TenantAwareCheckpointer.setup() 在建表后执行同等策略。

DO $$
DECLARE
    checkpoint_table TEXT;
    policy_name TEXT;
BEGIN
    IF to_regclass('public.agent_threads') IS NULL THEN
        RAISE EXCEPTION 'agent_threads 不存在，无法安装 checkpoint RLS';
    END IF;

    FOREACH checkpoint_table IN ARRAY ARRAY['checkpoints', 'checkpoint_blobs', 'checkpoint_writes']
    LOOP
        IF to_regclass('public.' || checkpoint_table) IS NOT NULL THEN
            policy_name := 'tenant_' || checkpoint_table;
            EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', checkpoint_table);
            EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', checkpoint_table);
            EXECUTE format('DROP POLICY IF EXISTS %I ON %I', policy_name, checkpoint_table);
            EXECUTE format(
                'CREATE POLICY %I ON %I FOR ALL '
                || 'USING (EXISTS (SELECT 1 FROM agent_threads '
                || 'WHERE agent_threads.thread_id = %I.thread_id '
                || 'AND agent_threads.user_id = current_setting(''app.user_id'', true))) '
                || 'WITH CHECK (EXISTS (SELECT 1 FROM agent_threads '
                || 'WHERE agent_threads.thread_id = %I.thread_id '
                || 'AND agent_threads.user_id = current_setting(''app.user_id'', true)))',
                policy_name, checkpoint_table, checkpoint_table, checkpoint_table
            );
        ELSE
            RAISE NOTICE '跳过 checkpoint RLS：% 尚未由 LangGraph 创建', checkpoint_table;
        END IF;
    END LOOP;
END
$$;
