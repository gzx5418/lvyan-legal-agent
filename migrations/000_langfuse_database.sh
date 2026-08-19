#!/bin/bash
# PostgreSQL 首次初始化时为 Langfuse v2 创建独立数据库。
# ============================================================
# 背景：docker-compose 的 langfuse 服务（langfuse/langfuse:2）自带 prisma
# 迁移，会在 DATABASE_URL 指向的库中建自己的表。若与业务库 lvyan 混用同一
# 库，langfuse 的表会与 agent_runs / agent_threads 等业务表互相污染。
#
# 本脚本挂载于 /docker-entrypoint-initdb.d/（compose 挂载 ./migrations），
# 仅在数据卷为空、实例首次初始化时执行；幂等（库已存在则跳过）。
#
# 已有 lvyan-postgres-data 卷的环境不会重跑 init，需手动补建：
#   docker exec -it lvyan-postgres psql -U lvyan -d postgres \
#     -c "CREATE DATABASE langfuse"
set -e

psql -v ON_ERROR_STOP=1 -U "${POSTGRES_USER:-lvyan}" -d postgres <<EOSQL
SELECT 'CREATE DATABASE langfuse'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'langfuse')
\gexec
EOSQL
