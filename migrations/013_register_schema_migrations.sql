-- P1：在 initdb 通道预登记 schema_migrations 版本表。
-- 背景：docker-entrypoint-initdb.d（compose 挂载 ./migrations，以 owner role
-- 执行 001-012）与应用层 _ensure_schema（按 schema_migrations 版本表执行）
-- 是两条独立通道。initdb 不写版本表 → 应用启动时重放全部 .sql：
--   a) 007/008 的 CREATE POLICY 无守卫时因 "already exists" 失败（已补
--      DROP POLICY IF EXISTS 兜底）；
--   b) 应用以 lvyan_runtime 角色连接时无 DDL 权限，重放必然失败。
-- 本迁移（initdb 字典序最后执行）把既有 .sql 版本全部登记进版本表，
-- 应用启动时看到已应用 → 跳过重放 → runtime 角色无需 DDL 权限。
-- 全新部署顺序：001-012（建表/策略/角色）→ 013（登记）→ 应用启动直接就绪。

CREATE TABLE IF NOT EXISTS schema_migrations (
    version TEXT PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO schema_migrations (version) VALUES
    ('001_agent_runs_threads.sql'),
    ('002_run_status_check_and_cancel.sql'),
    ('003_legal_answer_column.sql'),
    ('004_document_file_column.sql'),
    ('005_case_workspace.sql'),
    ('006_rls_runtime_role.sql'),
    ('007_rls_agent_tables.sql'),
    ('008_rls_workspace.sql'),
    ('009_interrupted_status.sql'),
    ('010_checkpoint_rls.sql'),
    ('011_drop_messages_unique_run_role.sql'),
    ('013_register_schema_migrations.sql')
ON CONFLICT (version) DO NOTHING;
