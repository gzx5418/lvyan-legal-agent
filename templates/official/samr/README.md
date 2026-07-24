# SAMR Official Template Cache

该目录用于缓存从 SAMR（国家市场监督管理总局合同示范文本库）按需下载的官方模板。

## 生成方式
- 先执行 `scripts/import_contract_library.py` 生成标准化模板索引：
  - `references/samr_contract_templates.json`
- 再执行 `scripts/fetch_samr_template.py` 下载具体模板：
  - 示例：`python scripts/fetch_samr_template.py --keyword 商品房买卖合同 --type word`

## 使用规则
- 本目录内模板优先级高于通用 `assets/templates/` 模板。
- 命中模板后，先通过 `scripts/extract_docx_text.py` 提取正文，再做字段映射填充。
- 任何“生成文件”流程仍需最终调用 `scripts/export_to_docx.ps1` 导出目标 `.docx`。
