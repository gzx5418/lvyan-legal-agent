"""法规版本解析器。

从官方法律全文库（``lawtext_extracted/laws-main/content``）的 Markdown front matter
中解析版本元数据，按标题聚合多个历史版本，建立版本关系链并标记当前有效版本。

解决「索引丢失版本元数据」核心问题：原 ``律言skill/scripts/build_law_index.py``
仅保留 title/category/file/path/size，导致 ``law_index.json`` 中同一部法律的多个版本
（如「不动产登记暂行条例」有 3 条）无法区分有效性。本模块补回 publication_date /
effective_date / status / official_urls / content_hash 等关键元数据，并提供版本聚合
与关系链。

公开接口：
    parse_law_metadata(filepath) -> LawMetadata
    build_version_groups(metadata_list) -> list[VersionGroup]
    mark_current_effective(groups) -> list[VersionGroup]
    scan_all_laws(lawtext_dir) -> list[LawMetadata]
    find_law_files_by_title(title, lawtext_dir) -> list[Path]
    compute_content_hash(text) -> str
"""

from __future__ import annotations

import hashlib
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field

from lvyan.config import settings

# ---------------------------------------------------------------------------
# 类型与常量
# ---------------------------------------------------------------------------
AuthorityStatus = Literal["effective", "repealed", "not_yet_effective", "unknown"]
"""法规状态枚举：effective=有效 / repealed=已废止 / not_yet_effective=尚未生效 / unknown=未知。"""

# 中文 status -> AuthorityStatus 映射
_STATUS_MAP: dict[str, AuthorityStatus] = {
    "有效": "effective",
    "已废止": "repealed",
    "失效": "repealed",
    "尚未生效": "not_yet_effective",
    "未生效": "not_yet_effective",
}


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------
class LawMetadata(BaseModel):
    """单部法规文件的解析元数据。"""

    source_id: str
    title: str
    link_title: str | None = None
    author: str | None = None
    publication_date: date | None = None
    effective_date: date | None = None
    # P0-1：新增 expiry_date / superseded_by，支持历史法规时间窗口判定
    expiry_date: date | None = None
    superseded_by: str | None = None
    status: AuthorityStatus = "unknown"
    group: str | None = None  # 法律/行政法规/司法解释/宪法/监察法规
    categories: list[str] = Field(default_factory=list)
    official_urls: list[str] = Field(default_factory=list)
    raw_filepath: str
    content_hash: str
    superseded: bool = False  # 被更新的有效版本取代时置 True


class VersionGroup(BaseModel):
    """同一标题下多个历史版本的聚合。"""

    title: str
    versions: list[LawMetadata] = Field(default_factory=list)
    current_effective: LawMetadata | None = None
    replaces_chain: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------
def compute_content_hash(text: str) -> str:
    """对正文文本计算 sha256，返回前 16 位 hex。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _parse_date(value: Any) -> date | None:
    """解析 YAML 中的日期字段，支持字符串 / date 对象，空值返回 None。"""
    if value is None:
        return None
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        try:
            return date.fromisoformat(s)
        except ValueError:
            # 兼容 'YYYY/MM/DD' 等格式
            try:
                return date.fromisoformat(s.replace("/", "-"))
            except ValueError:
                return None
    return None


def _map_status(raw: Any) -> AuthorityStatus:
    """中文状态值映射到 AuthorityStatus。"""
    if isinstance(raw, str):
        return _STATUS_MAP.get(raw.strip(), "unknown")
    return "unknown"


def _as_str_list(value: Any) -> list[str]:
    """将 YAML 值规整为 str 列表。"""
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v) for v in value]
    return [str(value)]


def _read_front_matter(filepath: Path) -> tuple[dict, str]:
    """读取 Markdown 文件的 YAML front matter。

    返回 ``(metadata_dict, body_text)``，body_text 为 front matter 之后的全部正文。
    文件无 front matter / 解析失败时返回 ``({}, 全文)``，不抛异常。
    """
    try:
        text = filepath.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {}, ""

    lines = text.splitlines(keepends=True)
    if not lines:
        return {}, ""

    # front matter 首行必须恰好为 ---
    if lines[0].rstrip("\r\n").rstrip() != "---":
        return {}, text

    # 查找关闭的 --- 行
    close_idx = -1
    for i in range(1, len(lines)):
        if lines[i].rstrip("\r\n").rstrip() == "---":
            close_idx = i
            break
    if close_idx == -1:
        return {}, text

    yaml_text = "".join(lines[1:close_idx])
    body_text = "".join(lines[close_idx + 1 :])
    try:
        data = yaml.safe_load(yaml_text) or {}
    except yaml.YAMLError:
        return {}, text
    if not isinstance(data, dict):
        return {}, text
    return data, body_text


# ---------------------------------------------------------------------------
# SubTask 6.1: YAML 元数据解析
# ---------------------------------------------------------------------------
def parse_law_metadata(filepath: Path) -> LawMetadata:
    """解析单个 Markdown 法规文件的 front matter 元数据。

    文件无 front matter 时返回 status="unknown" 的默认元数据，不抛异常。
    """
    filepath = Path(filepath)
    data, body_text = _read_front_matter(filepath)

    def _get(key: str, default: Any = None) -> Any:
        return data.get(key, default)

    source_id = _get("id")
    if not isinstance(source_id, str) or not source_id:
        source_id = filepath.stem

    title = _get("title")
    if not isinstance(title, str) or not title:
        title = filepath.stem

    link_title = _get("LinkTitle")
    link_title = link_title if isinstance(link_title, str) and link_title else None

    author = _get("author")
    author = author if isinstance(author, str) and author else None

    group = _get("group")
    group = group if isinstance(group, str) and group else None

    # publication_date 优先取自身字段，缺失时回退到 date
    pub_raw = _get("publication_date", _get("date"))

    # P0-1：expiry_date 优先取自身字段，缺失时回退到 repeal_date
    expiry_raw = _get("expiry_date", _get("repeal_date"))

    return LawMetadata(
        source_id=source_id,
        title=title,
        link_title=link_title,
        author=author,
        publication_date=_parse_date(pub_raw),
        effective_date=_parse_date(_get("effective_date")),
        expiry_date=_parse_date(expiry_raw),
        superseded_by=_get("superseded_by") if isinstance(_get("superseded_by"), str) else None,
        status=_map_status(_get("status")),
        group=group,
        categories=_as_str_list(_get("categories")),
        official_urls=_as_str_list(_get("urls")),
        raw_filepath=str(filepath),
        content_hash=compute_content_hash(body_text),
    )


# ---------------------------------------------------------------------------
# SubTask 6.2 & 6.3: 版本聚合 / 关系链 / 当前有效版本标记
# ---------------------------------------------------------------------------
def _sort_key(meta: LawMetadata) -> tuple[int, date]:
    """effective_date 升序排列键，None 排最后。"""
    if meta.effective_date is None:
        return (1, date.min)
    return (0, meta.effective_date)


def _select_current_effective(versions: list[LawMetadata]) -> LawMetadata | None:
    """从版本列表中选择当前有效版本。

    规则：
      - status="effective" 且 effective_date 最新者为当前有效版本
      - 若有多个 status="effective"，取 effective_date 最新者
      - 若 effective_date 相同，取 publication_date 最新者（content_hash 隐含不同）
    """
    effective = [v for v in versions if v.status == "effective"]
    if not effective:
        return None
    max_date = max((v.effective_date or date.min) for v in effective)
    candidates = [v for v in effective if (v.effective_date or date.min) == max_date]
    if len(candidates) == 1:
        return candidates[0]
    return max(candidates, key=lambda v: v.publication_date or date.min)


def _apply_superseded_mark(versions: list[LawMetadata], current: LawMetadata | None) -> None:
    """对非 current_effective 的 effective 版本标记 superseded=True。"""
    for v in versions:
        v.superseded = False
    if current is None:
        return
    for v in versions:
        if v.status == "effective" and v.source_id != current.source_id:
            v.superseded = True


def build_version_groups(metadata_list: list[LawMetadata]) -> list[VersionGroup]:
    """按 title 聚合版本，建立版本关系链并标记当前有效版本。

    - 同一 title 下按 effective_date 升序排列（None 排最后）
    - replaces_chain：按时间顺序排列 source_id，每个版本 replaces 前一个
    - current_effective：status="effective" 且 effective_date 最新者
    - 非 current 的 effective 版本标记 superseded=True
    """
    groups_map: dict[str, list[LawMetadata]] = defaultdict(list)
    for meta in metadata_list:
        groups_map[meta.title].append(meta)

    result: list[VersionGroup] = []
    for title, versions in groups_map.items():
        sorted_versions = sorted(versions, key=_sort_key)
        current = _select_current_effective(sorted_versions)
        _apply_superseded_mark(sorted_versions, current)
        result.append(
            VersionGroup(
                title=title,
                versions=sorted_versions,
                current_effective=current,
                replaces_chain=[v.source_id for v in sorted_versions],
            )
        )
    return result


def mark_current_effective(groups: list[VersionGroup]) -> list[VersionGroup]:
    """重新计算每个 VersionGroup 的 current_effective 与 superseded 标记。

    可对已有 groups 重算（例如外部修改 versions 后重新标记）。
    """
    for group in groups:
        current = _select_current_effective(group.versions)
        _apply_superseded_mark(group.versions, current)
        group.current_effective = current
        # 关系链按时间顺序刷新
        group.replaces_chain = [v.source_id for v in sorted(group.versions, key=_sort_key)]
    return groups


# ---------------------------------------------------------------------------
# 辅助：按标题查找文件 / 全库扫描
# ---------------------------------------------------------------------------
def find_law_files_by_title(title: str, lawtext_dir: Path | None = None) -> list[Path]:
    """扫描 lawtext_dir 下所有 .md 文件，返回 title 匹配的文件路径列表。"""
    root = Path(lawtext_dir) if lawtext_dir is not None else settings.lawtext_dir
    matches: list[Path] = []
    if not root.is_dir():
        return matches
    for md_file in root.rglob("*.md"):
        try:
            data, _ = _read_front_matter(md_file)
        except Exception:
            continue
        file_title = data.get("title")
        if isinstance(file_title, str) and file_title == title:
            matches.append(md_file)
    return matches


def scan_all_laws(lawtext_dir: Path | None = None) -> list[LawMetadata]:
    """扫描 lawtext_dir 下全部 .md 文件，返回元数据列表。

    单文件异常被捕获并跳过，保证全库扫描不中断。
    """
    root = Path(lawtext_dir) if lawtext_dir is not None else settings.lawtext_dir
    if not root.is_dir():
        return []
    result: list[LawMetadata] = []
    for md_file in root.rglob("*.md"):
        try:
            result.append(parse_law_metadata(md_file))
        except Exception:
            continue
    return result


def status_distribution(metadatas: list[LawMetadata]) -> dict[str, int]:
    """统计元数据列表的 status 分布，便于冒烟测试观察。"""
    return dict(Counter(m.status for m in metadatas))


__all__ = [
    "AuthorityStatus",
    "LawMetadata",
    "VersionGroup",
    "parse_law_metadata",
    "build_version_groups",
    "mark_current_effective",
    "scan_all_laws",
    "find_law_files_by_title",
    "compute_content_hash",
    "status_distribution",
]
