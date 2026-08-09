-- P2: 对 case_workspace 系列表启用 RLS。
-- legal_cases: 直接校验 user_id。
-- 子表: 通过 case_id JOIN legal_cases 校验所属案件的 user_id。

-- ============================================================================
-- legal_cases (顶层, 直接 user_id 校验)
-- ============================================================================
ALTER TABLE legal_cases ENABLE ROW LEVEL SECURITY;
ALTER TABLE legal_cases FORCE ROW LEVEL SECURITY;

CREATE POLICY tenant_select_cases ON legal_cases
    FOR SELECT USING (user_id = current_setting('app.user_id', true));

CREATE POLICY tenant_insert_cases ON legal_cases
    FOR INSERT WITH CHECK (user_id = current_setting('app.user_id', true));

CREATE POLICY tenant_update_cases ON legal_cases
    FOR UPDATE
    USING (user_id = current_setting('app.user_id', true))
    WITH CHECK (user_id = current_setting('app.user_id', true));

CREATE POLICY tenant_delete_cases ON legal_cases
    FOR DELETE USING (user_id = current_setting('app.user_id', true));

-- ============================================================================
-- case_evidence (通过 case_id 关联校验)
-- ============================================================================
ALTER TABLE case_evidence ENABLE ROW LEVEL SECURITY;
ALTER TABLE case_evidence FORCE ROW LEVEL SECURITY;

CREATE POLICY tenant_select_evidence ON case_evidence
    FOR SELECT
    USING (EXISTS (
        SELECT 1 FROM legal_cases
        WHERE legal_cases.case_id = case_evidence.case_id
          AND legal_cases.user_id = current_setting('app.user_id', true)
    ));

CREATE POLICY tenant_insert_evidence ON case_evidence
    FOR INSERT
    WITH CHECK (EXISTS (
        SELECT 1 FROM legal_cases
        WHERE legal_cases.case_id = case_evidence.case_id
          AND legal_cases.user_id = current_setting('app.user_id', true)
    ));

CREATE POLICY tenant_update_evidence ON case_evidence
    FOR UPDATE
    USING (EXISTS (
        SELECT 1 FROM legal_cases
        WHERE legal_cases.case_id = case_evidence.case_id
          AND legal_cases.user_id = current_setting('app.user_id', true)
    ));

CREATE POLICY tenant_delete_evidence ON case_evidence
    FOR DELETE
    USING (EXISTS (
        SELECT 1 FROM legal_cases
        WHERE legal_cases.case_id = case_evidence.case_id
          AND legal_cases.user_id = current_setting('app.user_id', true)
    ));

-- ============================================================================
-- legal_documents (通过 case_id 关联校验)
-- ============================================================================
ALTER TABLE legal_documents ENABLE ROW LEVEL SECURITY;
ALTER TABLE legal_documents FORCE ROW LEVEL SECURITY;

CREATE POLICY tenant_select_documents ON legal_documents
    FOR SELECT
    USING (EXISTS (
        SELECT 1 FROM legal_cases
        WHERE legal_cases.case_id = legal_documents.case_id
          AND legal_cases.user_id = current_setting('app.user_id', true)
    ));

CREATE POLICY tenant_insert_documents ON legal_documents
    FOR INSERT
    WITH CHECK (EXISTS (
        SELECT 1 FROM legal_cases
        WHERE legal_cases.case_id = legal_documents.case_id
          AND legal_cases.user_id = current_setting('app.user_id', true)
    ));

CREATE POLICY tenant_update_documents ON legal_documents
    FOR UPDATE
    USING (EXISTS (
        SELECT 1 FROM legal_cases
        WHERE legal_cases.case_id = legal_documents.case_id
          AND legal_cases.user_id = current_setting('app.user_id', true)
    ));

CREATE POLICY tenant_delete_documents ON legal_documents
    FOR DELETE
    USING (EXISTS (
        SELECT 1 FROM legal_cases
        WHERE legal_cases.case_id = legal_documents.case_id
          AND legal_cases.user_id = current_setting('app.user_id', true)
    ));

-- ============================================================================
-- document_versions (通过 document_id → legal_documents → legal_cases)
-- ============================================================================
ALTER TABLE document_versions ENABLE ROW LEVEL SECURITY;
ALTER TABLE document_versions FORCE ROW LEVEL SECURITY;

CREATE POLICY tenant_select_versions ON document_versions
    FOR SELECT
    USING (EXISTS (
        SELECT 1 FROM legal_documents d
        JOIN legal_cases c ON c.case_id = d.case_id
        WHERE d.document_id = document_versions.document_id
          AND c.user_id = current_setting('app.user_id', true)
    ));

CREATE POLICY tenant_insert_versions ON document_versions
    FOR INSERT
    WITH CHECK (EXISTS (
        SELECT 1 FROM legal_documents d
        JOIN legal_cases c ON c.case_id = d.case_id
        WHERE d.document_id = document_versions.document_id
          AND c.user_id = current_setting('app.user_id', true)
    ));

CREATE POLICY tenant_delete_versions ON document_versions
    FOR DELETE
    USING (EXISTS (
        SELECT 1 FROM legal_documents d
        JOIN legal_cases c ON c.case_id = d.case_id
        WHERE d.document_id = document_versions.document_id
          AND c.user_id = current_setting('app.user_id', true)
    ));

-- ============================================================================
-- review_findings (通过 document_id → legal_documents → legal_cases)
-- ============================================================================
ALTER TABLE review_findings ENABLE ROW LEVEL SECURITY;
ALTER TABLE review_findings FORCE ROW LEVEL SECURITY;

CREATE POLICY tenant_select_findings ON review_findings
    FOR SELECT
    USING (EXISTS (
        SELECT 1 FROM legal_documents d
        JOIN legal_cases c ON c.case_id = d.case_id
        WHERE d.document_id = review_findings.document_id
          AND c.user_id = current_setting('app.user_id', true)
    ));

CREATE POLICY tenant_insert_findings ON review_findings
    FOR INSERT
    WITH CHECK (EXISTS (
        SELECT 1 FROM legal_documents d
        JOIN legal_cases c ON c.case_id = d.case_id
        WHERE d.document_id = review_findings.document_id
          AND c.user_id = current_setting('app.user_id', true)
    ));

CREATE POLICY tenant_update_findings ON review_findings
    FOR UPDATE
    USING (EXISTS (
        SELECT 1 FROM legal_documents d
        JOIN legal_cases c ON c.case_id = d.case_id
        WHERE d.document_id = review_findings.document_id
          AND c.user_id = current_setting('app.user_id', true)
    ));

-- ============================================================================
-- document_approvals (通过 document_id → legal_documents → legal_cases)
-- ============================================================================
ALTER TABLE document_approvals ENABLE ROW LEVEL SECURITY;
ALTER TABLE document_approvals FORCE ROW LEVEL SECURITY;

CREATE POLICY tenant_select_approvals ON document_approvals
    FOR SELECT
    USING (EXISTS (
        SELECT 1 FROM legal_documents d
        JOIN legal_cases c ON c.case_id = d.case_id
        WHERE d.document_id = document_approvals.document_id
          AND c.user_id = current_setting('app.user_id', true)
    ));

CREATE POLICY tenant_insert_approvals ON document_approvals
    FOR INSERT
    WITH CHECK (EXISTS (
        SELECT 1 FROM legal_documents d
        JOIN legal_cases c ON c.case_id = d.case_id
        WHERE d.document_id = document_approvals.document_id
          AND c.user_id = current_setting('app.user_id', true)
    ));

-- ============================================================================
-- workspace_audit_events (通过 case_id 关联校验)
-- ============================================================================
ALTER TABLE workspace_audit_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE workspace_audit_events FORCE ROW LEVEL SECURITY;

CREATE POLICY tenant_select_audit ON workspace_audit_events
    FOR SELECT
    USING (EXISTS (
        SELECT 1 FROM legal_cases
        WHERE legal_cases.case_id = workspace_audit_events.case_id
          AND legal_cases.user_id = current_setting('app.user_id', true)
    ));

CREATE POLICY tenant_insert_audit ON workspace_audit_events
    FOR INSERT
    WITH CHECK (EXISTS (
        SELECT 1 FROM legal_cases
        WHERE legal_cases.case_id = workspace_audit_events.case_id
          AND legal_cases.user_id = current_setting('app.user_id', true)
    ));
