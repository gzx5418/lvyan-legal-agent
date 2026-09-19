-- P2: 创建受 RLS 约束的 runtime role。
-- 应用运行时使用此角色连接，无 BYPASSRLS 权限，强制受策略约束。
-- 迁移仍由 owner role (lvyan) 执行。
--
-- 安全语义（fail-closed）：角色以**无密码**状态创建——在部署方显式设置密码
-- 之前（见 migrations/012_runtime_role_password.sh 或手动 ALTER ROLE），
-- 任何人都无法用它登录，不存在"已知默认密码"的旁路。

-- 幂等创建：若已存在则跳过
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'lvyan_runtime') THEN
        CREATE ROLE lvyan_runtime LOGIN PASSWORD NULL
            NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;
    END IF;
END
$$;

-- 授予连接权限
GRANT CONNECT ON DATABASE lvyan TO lvyan_runtime;

-- 授予 schema 使用权限
GRANT USAGE ON SCHEMA public TO lvyan_runtime;

-- 授予表级 DML 权限（不含 DDL：ALTER/DROP/TRUNCATE）
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO lvyan_runtime;

-- 对未来创建的表也自动授权（避免新增迁移后遗漏）
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO lvyan_runtime;

-- 授予序列使用权限（agent_messages.message_id 等自增列需要）
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO lvyan_runtime;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO lvyan_runtime;

-- 允许 SET LOCAL（RLS 上下文设置需要）
-- 注：PostgreSQL 默认允许 SET LOCAL，此处显式声明意图
