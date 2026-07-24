# 官方模板来源（已导入）

以下模板已下载并存放在 `assets/templates/official/`，用于优先复用：

1. 离婚纠纷起诉状（官方示范）
- 来源页面：https://www.sdcourt.gov.cn/jnanszqfy/389659/389660/389662/19798584/index.html
- 直链：https://www.sdcourt.gov.cn/jnanszqfy/389659/389660/389662/19798584/2024042816414995261.docx

2. 买卖合同纠纷起诉状（官方示范）
- 来源页面：https://www.sdcourt.gov.cn/jnanszqfy/389659/389660/389662/19798584/index.html
- 直链：https://www.sdcourt.gov.cn/jnanszqfy/389659/389660/389662/19798584/2024042816445948149.docx

3. 劳动争议纠纷起诉状（官方示范）
- 来源页面：https://www.sdcourt.gov.cn/jnanszqfy/389659/389660/389662/19798584/index.html
- 直链：https://www.sdcourt.gov.cn/jnanszqfy/389659/389660/389662/19798584/2024042816464583814.docx

4. 部分案件起诉状答辩状示范文本（67类官方汇编）
- 来源页面：https://www.ftcourt.gov.cn/ssfw/sszn/content/post_1577086.html
- 直链：https://www.ftcourt.gov.cn/attachment/0/92/92645/1577086.docx
- 上位通知（最高法）：https://www.court.gov.cn/fabu/xiangqing/468671.html

## 使用说明
- 文书生成时，优先匹配 `assets/templates/official/` 中对应案由模板。
- 命中官方模板后，必须先执行 `scripts/extract_docx_text.py` 提取正文，再进行字段填充。
- 若无对应官方模板，再回退 `assets/templates/` 通用模板。
- 输出时应标注“模板来源：官方示范/通用模板”。
