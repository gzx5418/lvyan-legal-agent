-- P1-A：持久化文书文件信息。
-- agent_runs.document_file：JSONB，保存 finalizer 节点渲染的文书文件元数据
-- （output_path / format / file_size / success）。完成时由 _update_metadata 写入。
-- 下载端点 /api/documents/{run_id}/download 据此定位文件并做 ownership 校验。

ALTER TABLE agent_runs
    ADD COLUMN IF NOT EXISTS document_file JSONB;
