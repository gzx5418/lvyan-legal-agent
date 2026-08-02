-- P1-6：持久化结构化法律输出 LegalAnswerV1。
--
-- agent_runs.legal_answer：JSONB，保存 finalizer 节点输出的脱敏后结构化数据。
-- 完成时由 _update_metadata 写入，历史恢复（/state、刷新页面）时读取。
-- document 模式或 HITL 编辑后为 NULL，前端回退到 final_output（Markdown）。

ALTER TABLE agent_runs
    ADD COLUMN IF NOT EXISTS legal_answer JSONB;
