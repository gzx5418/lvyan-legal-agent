"""附件链路辅助(U-17):从 server.py 拆出的路径解析与 Markdown 读取。

行为与拆分前逐字一致;``upload_dir`` 作为参数传入(server 薄包装在调用时
读取自己的全局,保持测试 monkeypatch ``server._UPLOAD_DIR`` 的隔离语义)。

安全约束:
- ``resolve_upload_path``:路径穿越防护(resolve 后必须仍在上传目录内);
- ``metadata_path_for_file_id``:file_id 白名单正则,拒绝路径分隔符/盘符。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from fastapi import HTTPException

__all__ = [
    "UPLOAD_FILE_ID_RE",
    "resolve_upload_path",
    "metadata_path_for_file_id",
    "load_attachment_markdown",
]

UPLOAD_FILE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


def resolve_upload_path(path_str: str, *, upload_dir: Path) -> Path:
    """把 ``path_str`` 解析为绝对路径，并校验仍在 ``upload_dir`` 内。

    M5：防止 ``markdown_path`` / ``raw_path`` 被构造为 ``../../etc/passwd`` 等
    路径穿越。任一路径逃逸出上传目录抛 :class:`HTTPException(404)`。
    """
    candidate = (Path(path_str)).resolve()
    try:
        candidate.relative_to(Path(upload_dir).resolve())
    except ValueError as exc:
        raise HTTPException(
            status_code=404,
            detail=f"附件路径非法（逃逸出上传目录）：{path_str}",
        ) from exc
    return candidate


def metadata_path_for_file_id(file_id: str, *, upload_dir: Path) -> Path:
    """返回附件元数据路径，并拒绝把 file_id 解释为路径。

    ``file_id`` 来自 API 请求体；即使后缀固定为 ``.json``，也不能允许
    ``../``、盘符或路径分隔符参与路径拼接。当前上传生成 16 位 hex，放宽
    为安全的字母数字、下划线和连字符以兼容旧数据。
    """
    if not UPLOAD_FILE_ID_RE.fullmatch(file_id):
        raise HTTPException(status_code=422, detail="附件 file_id 格式非法")
    return resolve_upload_path(str(Path(upload_dir) / f"{file_id}.json"), upload_dir=upload_dir)


def load_attachment_markdown(
    meta: dict[str, Any],
    fid: str,
    case_vault: Any = None,
    *,
    upload_dir: Path,
) -> str:
    """从附件元数据读取 Markdown 正文，按以下顺序：

    1. 优先读取 ``markdown_path`` 指向的 ``.md`` 文件（M5 新格式）。
    2. 路径不存在或读取失败 → 回退到旧 JSON 中的 ``markdown`` 字段（M5 兼容）。
    3. 最后回退 ``text_preview``。

    Markdown 文件被声明却不存在时，按场景返回 404；不能静默忽略。
    """
    markdown_path_str = meta.get("markdown_path")
    if markdown_path_str:
        if str(markdown_path_str).startswith("vault://"):
            if case_vault is None:
                raise HTTPException(status_code=503, detail="附件加密空间不可用")
            vault_ref = str(markdown_path_str)[len("vault://") :]
            try:
                thread_id, doc_id = vault_ref.split("/", 1)
            except ValueError as exc:
                raise HTTPException(status_code=422, detail="附件加密引用无效") from exc
            # 上传暂存区按用户（upload-{sha256(user_id)}）而非 run 线程存储，
            # 用户归属已由 run 端点的 ownership 校验保证；此处做同 thread 自校验，
            # 满足 vault 必填 expected_thread_id 的 fail-closed 契约。
            content = case_vault.retrieve(thread_id, doc_id, expected_thread_id=thread_id)
            if content is None:
                raise HTTPException(status_code=404, detail=f"附件 {fid} 不存在或已过期")
            return content.decode("utf-8", errors="replace")
        md_path = resolve_upload_path(markdown_path_str, upload_dir=upload_dir)
        if not md_path.is_file():
            raise HTTPException(
                status_code=404,
                detail=f"附件 {fid} 的 Markdown 文件不存在：{md_path.name}",
            )
        try:
            return md_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise HTTPException(
                status_code=503,
                detail=f"附件 {fid} 的 Markdown 文件读取失败：{exc}",
            ) from exc

    # 兼容旧 JSON：markdown 字段直接存了全文
    legacy_md = meta.get("markdown")
    if legacy_md:
        return str(legacy_md)

    return meta.get("text_preview", "") or ""
