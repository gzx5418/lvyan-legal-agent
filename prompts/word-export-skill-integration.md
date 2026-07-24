# Word Export Integration (Single-Skill Mode)

## Goal
由 `legal-copilot` 单独完成 Word 导出，不依赖其他技能。

## Runtime flow
1. 若命中官方模板，先调用 `scripts/extract_docx_text.py` 读取模板正文用于字段映射。
2. 生成结构化 Markdown 成果。
3. 只要发生"生成文件"行为，自动调用导出。

### Export commands

**Windows:**
```powershell
powershell -ExecutionPolicy Bypass -File scripts/export_to_docx.ps1 -InputMarkdown .\output.md -OutputDocx .\output.docx
```

**macOS/Linux (fallback):**
```bash
pandoc output.md -o output.docx
```

4. `export_to_docx.ps1` 内部优先级：`markdown_to_docx_converter` → `pandoc`。
5. 成功后返回 `.docx` 输出路径。

### Result handling

| Result | Action |
|--------|--------|
| ✅ Success | 显示：`✅ 已导出：[path]` 并注明模板来源 |
| ❌ Converter missing | 显示错误，提供原始 Markdown 兜底 |
| ❌ Other failure | 显示错误信息并提供原始 Markdown |

## Dependency policy
- 不要求安装外部 Word 技能。
- 不要求执行 skill 搜索/安装步骤。
- 仅依赖当前 Skill 内置脚本 + 本机转换命令。

## Output requirement
- 返回 `.docx` 绝对路径
- 返回导出执行状态（成功/失败）
- 返回模板来源（官方/通用）与官方模板读取状态
- 失败时返回原因与可执行修复动作
- 附"提交前校验清单"：当事人、金额、日期、附件一致性
