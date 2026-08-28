"""RLS 上线前检查脚本：确认无孤儿/无归属数据。

用途
----
在启用 RLS 策略迁移之前执行，确保：
1. 所有表中 user_id 非空
2. agent_runs 中的 thread_id 都有对应的 agent_threads 记录
3. agent_messages 中的 run_id 都有对应的 agent_runs 记录
4. legal_cases 中的 user_id 非空

存在孤儿数据时输出报告并 exit(1)，迁移不可继续。

CLI 用法
--------
    python -m lvyan.scripts.rls_preflight
    python -m lvyan.scripts.rls_preflight --fix  # 自动回填 anonymous
"""

from __future__ import annotations

import argparse
import logging
import sys

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
_logger = logging.getLogger(__name__)

_CHECKS = [
    # (描述, 检测 SQL, 修复 SQL)
    (
        "agent_threads: user_id 为空或 NULL",
        "SELECT count(*) FROM agent_threads WHERE user_id IS NULL OR user_id = ''",
        "UPDATE agent_threads SET user_id = 'anonymous' WHERE user_id IS NULL OR user_id = ''",
    ),
    (
        "agent_runs: user_id 为空或 NULL",
        "SELECT count(*) FROM agent_runs WHERE user_id IS NULL OR user_id = ''",
        "UPDATE agent_runs SET user_id = 'anonymous' WHERE user_id IS NULL OR user_id = ''",
    ),
    (
        "agent_messages: user_id 为空或 NULL",
        "SELECT count(*) FROM agent_messages WHERE user_id IS NULL OR user_id = ''",
        "UPDATE agent_messages SET user_id = 'anonymous' WHERE user_id IS NULL OR user_id = ''",
    ),
    (
        "agent_runs: thread_id 无对应 agent_threads",
        """SELECT count(*) FROM agent_runs r
           WHERE NOT EXISTS (SELECT 1 FROM agent_threads t WHERE t.thread_id = r.thread_id)""",
        None,  # 不可自动修复，需手动处理
    ),
    (
        "agent_messages: run_id 无对应 agent_runs",
        """SELECT count(*) FROM agent_messages m
           WHERE NOT EXISTS (SELECT 1 FROM agent_runs r WHERE r.run_id = m.run_id)""",
        None,
    ),
    (
        "legal_cases: user_id 为空或 NULL",
        "SELECT count(*) FROM legal_cases WHERE user_id IS NULL OR user_id = ''",
        "UPDATE legal_cases SET user_id = 'anonymous' WHERE user_id IS NULL OR user_id = ''",
    ),
]


def main() -> int:
    parser = argparse.ArgumentParser(description="RLS 上线前数据检查")
    parser.add_argument("--fix", action="store_true", help="自动修复可修复的问题")
    parser.add_argument(
        "--dsn",
        default=None,
        help="PostgreSQL DSN（默认读取 DATABASE_URL 环境变量）",
    )
    args = parser.parse_args()

    import os

    dsn = args.dsn or os.getenv("DATABASE_URL", "")
    if not dsn:
        _logger.error("未配置 DATABASE_URL 或 --dsn")
        return 1

    # 转换 SQLAlchemy URL 为 psycopg URL（覆盖常见驱动前缀）
    for prefix in ("postgresql+psycopg://", "postgresql+psycopg2://", "postgresql+asyncpg://"):
        if dsn.startswith(prefix):
            dsn = "postgresql://" + dsn[len(prefix) :]
            break

    try:
        import psycopg
    except ImportError:
        _logger.error("未安装 psycopg，无法执行 RLS 预检")
        return 1

    try:
        conn = psycopg.connect(dsn, autocommit=True)
    except psycopg.Error as exc:
        _logger.error("数据库连接失败: %s", exc)
        return 1

    issues_found = 0
    issues_fixed = 0

    # 连接统一在 finally 中关闭：循环体内的非 psycopg 异常（如结果解包）
    # 不应让连接泄漏
    try:
        for desc, check_sql, fix_sql in _CHECKS:
            try:
                cur = conn.execute(check_sql)
                count = cur.fetchone()[0]
            except psycopg.Error as exc:
                # 表可能不存在（首次部署），跳过
                _logger.warning("检查跳过 (%s): %s", desc, exc)
                continue

            if count > 0:
                issues_found += 1
                _logger.warning("发现问题: %s (count=%d)", desc, count)

                if args.fix and fix_sql:
                    try:
                        conn.execute(fix_sql)
                        _logger.info("已修复: %s", desc)
                        issues_fixed += 1
                    except psycopg.Error as exc:
                        _logger.error("修复失败: %s (%s)", desc, exc)
                elif fix_sql is None:
                    _logger.error("  → 不可自动修复，需人工处理")
            else:
                _logger.info("通过: %s", desc)
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001 关闭失败不影响检查结论
            pass

    _logger.info("检查完成: 发现 %d 个问题，修复 %d 个", issues_found, issues_fixed)

    if issues_found > issues_fixed:
        _logger.error("存在未修复问题，RLS 迁移不可执行")
        return 1

    _logger.info("所有检查通过，可安全执行 RLS 迁移")
    return 0


if __name__ == "__main__":
    sys.exit(main())
