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
# 未设置 POSTGRES_RUNTIME_PASSWORD 时仅打印警告并跳过（角色保持不可登录，
# 应用继续以 owner role 连接，RLS 不生效）。
#
# 已有 lvyan-postgres-data 卷的环境不会重跑 init，需手动执行：
#   docker exec -it lvyan-postgres psql -U lvyan -d lvyan \
#     -c "ALTER ROLE lvyan_runtime PASSWORD '<强随机值>'"
#
# 实现注意：
# - 不使用 exit：本文件可能被 docker-entrypoint 以 `.` source 方式执行
#   （文件不可执行时），exit 会终止整个 entrypoint 导致容器启动失败。
# - 密码经 psql 客户端变量（-v + :'pwd'）传递：psql 对 :'var' 做字面量
#   安全转义，密码含单引号/反斜杠/美元符均不会破坏 SQL 语句。

if [ -z "${POSTGRES_RUNTIME_PASSWORD:-}" ]; then
    echo "[012] WARNING: POSTGRES_RUNTIME_PASSWORD 未设置，跳过 —— lvyan_runtime 保持不可登录，" \
         "应用将以 owner role 连接（RLS 实际不生效）。生产部署必须在 .env 中设置该变量。"
else
    # 顶层直写（不经 DO 块）：psql 的 :'var' 字面量插值只发生在引号外的
    # 词法层，dollar-quoted 块内不会被替换。006 在同一 initdb 流程中先于
    # 本脚本执行，role 必然已存在；手动运行时若不存在则报错退出（诚实失败）。
    psql -v ON_ERROR_STOP=1 -U "${POSTGRES_USER:-lvyan}" -d "${POSTGRES_DB:-lvyan}" \
        -v pwd="$POSTGRES_RUNTIME_PASSWORD" <<'EOSQL'
ALTER ROLE lvyan_runtime WITH LOGIN PASSWORD :'pwd';
EOSQL
    echo "[012] lvyan_runtime 密码已设置：请将应用 DATABASE_URL 切换为 lvyan_runtime 角色以启用 RLS。"
fi
