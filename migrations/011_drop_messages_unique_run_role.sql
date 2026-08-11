-- BUG-009: 移除 agent_messages 的 (run_id, role) 唯一约束。
-- 原约束导致同一 run 中同一 role 只能有一条消息，HITL 恢复后会覆盖原始消息，
-- 丢失审计记录。改为允许多条消息并添加普通索引加速查询。

ALTER TABLE agent_messages DROP CONSTRAINT IF EXISTS agent_messages_run_id_role_key;

CREATE INDEX IF NOT EXISTS idx_agent_messages_run_role
    ON agent_messages (run_id, role);
