"""文件转 Markdown 服务（基于微软 markitdown）。

职责
----
- 文档类文件（PDF/DOCX/PPTX/XLSX/HTML 等）→ 调用 markitdown 转为 Markdown
- 纯文本文件（txt/md/csv/json 等）→ 直接读取
- 图片文件（png/jpg/jpeg/webp/gif/bmp）→ 交给视觉模型生成文本描述

公开接口
--------
- ``convert_to_markdown(file_path) -> ConvertResult``
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from lvyan.config import settings

_logger = logging.getLogger("lvyan.tools.file_converter")

# 支持的文件类型分类
_TEXT_EXTS = {".txt", ".md", ".csv", ".json", ".xml", ".html", ".htm", ".log", ".yaml", ".yml", ".toml", ".ini", ".rst"}
_DOC_EXTS = {".pdf", ".docx", ".doc", ".pptx", ".ppt", ".xlsx", ".xls", ".odt", ".rtf"}
_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tiff", ".tif"}

# markitdown 单例（惰性初始化）
_markitdown: Any = None


def _get_markitdown() -> Any:
    """惰性初始化 markitdown 单例。"""
    global _markitdown
    if _markitdown is not None:
        return _markitdown
    try:
        from markitdown import MarkItDown
        _markitdown = MarkItDown()
        _logger.info("markitdown 初始化成功")
    except ImportError:
        _logger.warning("markitdown 未安装，文档转换功能不可用")
        _markitdown = None
    except Exception as exc:  # noqa: BLE001
        _logger.warning("markitdown 初始化失败: %s", exc)
        _markitdown = None
    return _markitdown


def _read_text_file(file_path: Path, max_chars: int = 50000) -> str:
    """直接读取文本文件内容。"""
    for enc in ("utf-8", "gbk", "gb2312", "latin-1"):
        try:
            text = file_path.read_text(encoding=enc)
            return text[:max_chars]
        except UnicodeDecodeError:
            continue
    return ""


def _convert_with_markitdown(file_path: Path) -> str:
    """调用 markitdown 转换文档为 Markdown。"""
    md = _get_markitdown()
    if md is None:
        return f"[markitdown 未安装，无法解析 {file_path.name}]"
    try:
        result = md.convert(str(file_path))
        text = result.text_content if hasattr(result, "text_content") else str(result)
        # 截断过长的输出
        if len(text) > 50000:
            text = text[:50000] + "\n\n... (内容已截断，共 " + str(len(text)) + " 字符)"
        return text or f"[{file_path.name} 转换结果为空]"
    except Exception as exc:  # noqa: BLE001
        _logger.warning("markitdown 转换失败 %s: %s", file_path.name, exc)
        return f"[转换失败: {exc}]"


def _convert_image_with_vision(file_path: Path) -> str:
    """用视觉模型理解图片内容，生成文本描述。

    将图片 base64 编码后发送到模型网关的 /v1/chat/completions，
    使用 vision_model（默认 Pro/Qwen/Qwen2.5-VL-7B-Instruct）生成描述。
    """
    import base64
    import mimetypes

    gateway = settings.model_gateway_url
    api_key = settings.model_gateway_api_key
    vision_model = settings.vision_model

    if not gateway or not api_key:
        return f"[视觉模型未配置，无法解析图片 {file_path.name}]"

    # 读取图片并 base64 编码
    try:
        image_bytes = file_path.read_bytes()
        image_b64 = base64.b64encode(image_bytes).decode("utf-8")
        mime_type = mimetypes.guess_type(str(file_path))[0] or "image/png"
        data_url = f"data:{mime_type};base64,{image_b64}"
    except Exception as exc:  # noqa: BLE001
        return f"[图片读取失败: {exc}]"

    # 调用视觉模型
    try:
        import httpx

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": vision_model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "请详细描述这张图片的内容。如果图片包含文字（如合同、"
                                "证据截图、法律文书等），请完整提取文字内容。"
                                "如果图片是场景照片，请描述与法律分析相关的视觉信息。"
                            ),
                        },
                        {
                            "type": "image_url",
                            "image_url": {"url": data_url},
                        },
                    ],
                }
            ],
            "max_tokens": 2000,
            "temperature": 0.1,
        }

        resp = httpx.post(
            f"{gateway.rstrip('/')}/v1/chat/completions",
            json=payload,
            headers=headers,
            timeout=30.0,
        )
        resp.raise_for_status()
        data = resp.json()
        text = data["choices"][0]["message"]["content"]
        return text or f"[图片 {file_path.name} 识别结果为空]"
    except Exception as exc:  # noqa: BLE001
        _logger.warning("视觉模型调用失败 %s: %s", file_path.name, exc)
        return f"[视觉模型调用失败: {exc}]"


def get_file_category(ext: str) -> str:
    """根据扩展名返回文件类别：text / doc / image / unknown。"""
    ext = ext.lower()
    if ext in _TEXT_EXTS:
        return "text"
    if ext in _DOC_EXTS:
        return "doc"
    if ext in _IMAGE_EXTS:
        return "image"
    return "unknown"


def convert_to_markdown(file_path: Path | str) -> dict[str, Any]:
    """将文件转换为 Markdown 文本。

    Args:
        file_path: 文件路径（Path 或字符串）。

    Returns:
        dict 包含字段：
        - ``file_name``: 文件名
        - ``category``: 文件类别（text/doc/image/unknown）
        - ``markdown``: 转换后的 Markdown 文本
        - ``char_count``: 文本字符数
        - ``converter``: 使用的转换器（direct/markitdown/vision）
    """
    fp = Path(file_path)
    if not fp.is_file():
        return {
            "file_name": str(file_path),
            "category": "unknown",
            "markdown": "[文件不存在]",
            "char_count": 0,
            "converter": "none",
        }

    ext = fp.suffix.lower()
    category = get_file_category(ext)

    if category == "text":
        text = _read_text_file(fp)
        converter = "direct"
    elif category == "doc":
        text = _convert_with_markitdown(fp)
        converter = "markitdown"
    elif category == "image":
        text = _convert_image_with_vision(fp)
        converter = "vision"
    else:
        # 未知类型，尝试用 markitdown 兜底
        text = _convert_with_markitdown(fp)
        converter = "markitdown-fallback"

    # 包装为 Markdown 区块
    header = f"## 附件：{fp.name}\n\n"
    markdown = header + text

    return {
        "file_name": fp.name,
        "category": category,
        "markdown": markdown,
        "char_count": len(text),
        "converter": converter,
    }


__all__ = ["convert_to_markdown", "get_file_category"]
