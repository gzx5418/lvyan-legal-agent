# Official Templates

该目录保存已导入并可复用的官方 `.docx` 模板。

使用规则：
- 生成文书时优先匹配本目录模板。
- 命中后先执行 `scripts/extract_docx_text.py` 读取模板正文，再进行字段映射填充。
- 若读取失败或无匹配模板，再回退到 `assets/templates/` 通用模板。

