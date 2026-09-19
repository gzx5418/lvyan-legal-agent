#!/bin/bash
# 为 lvyan_runtime role 设置登录密码（可选，但生产启用 RLS 时必做）。
# ============================================================
# 背景：006_rls_runtime_role.sql 以 PASSWORD NULL 创建 lvyan_runtime
# （fail-closed：设密码前不可登录，杜绝"已知默认密码"旁路）。本脚本在
# docker-entrypoint-initdb.d 流程末尾（字母序 012 > 006）从环境变量读取
# 密码并启用该角色。
#
# 用法：在 .env 中设置 POSTGRES_RUNTIME_PASSWORD（强随机值），并把应用侧
# DATABASE_URL 切换为 lvyan_runtime 角色以真正受 RLS 约束（见 .env.example）。
#
# 未设置 POSTGRES_RUNTIME_PASSWORD 时跳过并打印醒目警告：角色保持不可登录，
# 应用继续以 owner role 连接（RLS 不生效），生产部署应补齐该变量。
# 已有 lvyan-postgres-data 卷的环境不会重跑 init，需手动执行：
#   docker exec -it lvyan-postgres psql -U lvyan -d lvyan \
#     -c "ALTER ROLE lvyan_runtime PASSWORD '<强随机值>'"
set -e

if [ -z "${POSTGRES_RUNTIME_PASSWORD:-}" ]; then
    echo "[012] WARNING: POSTGRES_RUNTIME_PASSWORD 未设置，跳过 —— lvyan_runtime 保持不可登录，" \
         "应用将以 owner role 连接（RLS 实际不生效）。生产部署必须在 .env 中设置该变量。"
    exit 0
fi

psql -v ON_ERROR_STOP=1 -U "${POSTGRES_USER:-lvyan}" -d "${POSTGRES_DB:-lvyan}" <<EOSQL
DO \$\$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'lvyan_runtime') THEN
        ALTER ROLE lvyan_runtime WITH LOGIN PASSWORD '$POSTGRES_RUNTIME_PASSWORD';
    END IF;
END
\$\$;
EOSQL

echo "[012] lvyan_runtime 密码已设置：请将应用 DATABASE_URL 切换为 lvyan_runtime 角色以启用 RLS。"
