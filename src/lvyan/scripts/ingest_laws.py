"""法规条文级切分与索引 v2。

将「整部法律作为一个检索单元」改为「按编/章/节/条切分为条文级索引」。

切分策略：
    - 跳过 YAML front matter，从正文开始
    - 按「第X条」切分（支持中文数字与阿拉伯数字，兼容 markdown 加粗格式 ``- **第X条**``）
    - 追踪最近的「第X章 / 第X节」markdown 标题，挂到所属条文的 chapter / section 字段
    - 无「第X条」格式的法规（如部分决议）整部正文作为一个 chunk，article_number 为空

公开接口：
    chunk_law_articles(metadata) -> list[ArticleChunk]
    build_article_index(lawtext_dir) -> list[ArticleChunk]
    save_index_json(chunks, output_path) -> None
    write_to_opensearch(chunks) -> int
    write_to_postgres(chunks) -> int
    main() -> CLI 入口
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import socket
import sys
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

from pydantic import BaseModel

from lvyan.config import AGENT_DIR, settings
from lvyan.retrieval.version_resolver import (
    AuthorityStatus,
    LawMetadata,
    scan_all_laws,
)

ARTICLE_INDEX_SCHEMA_VERSION = 3

# ---------------------------------------------------------------------------
# 正则与常量
# ---------------------------------------------------------------------------
# 中文数字 + 阿拉伯数字字符集
_NUM = r"[一二三四五六七八九十百千零0-9]+"

# 条文标记：行首 可选「- **」+ 第X条 + 可选「**」
# 要求行首无缩进，避免误匹配条文正文中带缩进的「第X条」引用
_ARTICLE_RE = re.compile(rf"^(?:-\s*\*\*)?(第{_NUM}条)(?:\*\*)?")

# 章 / 节标题：仅识别 markdown 标题行（# 开头）
_CHAPTER_RE = re.compile(rf"^#+\s*(第{_NUM}章)[\s\u3000]*(.*)$")
_SECTION_RE = re.compile(rf"^#+\s*(第{_NUM}节)[\s\u3000]*(.*)$")

# group -> authority_level 映射
_AUTHORITY_LEVEL_MAP: dict[str, str] = {
    "宪法": "宪法",
    "法律": "法律",
    "行政法规": "行政法规",
    "司法解释": "司法解释",
    "监察法规": "监察法规",
}

# 中文数字字符 -> 数值
_DIGIT_MAP: dict[str, int] = {
    "零": 0, "〇": 0, "一": 1, "二": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------
class ArticleChunk(BaseModel):
    """单个条文切分单元。"""

    chunk_id: str
    source_id: str
    title: str
    article_number: str = ""
    article_text: str
    chapter: str | None = None
    section: str | None = None
    authority_level: str
    status: AuthorityStatus
    effective_date: date | None = None
    expiry_date: date | None = None
    superseded_by: str | None = None
    jurisdiction: str = "中国大陆"
    official_source: str | None = None
    content_hash: str
    publication_date: date | None = None


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------
def _parse_chinese_number(text: str) -> int | None:
    """中文数字转 int，支持 十/二十三/一百零五/一千二百六十 等。

    纯阿拉伯数字直接 int()。无法解析时返回 None。
    """
    if not text:
        return None
    text = text.strip()
    # 纯阿拉伯数字
    if text.isdigit():
        try:
            return int(text)
        except ValueError:
            return None

    result = 0
    current = 0  # 当前「个位」数字，遇到 十/百/千 时乘上去
    for ch in text:
        if ch in _DIGIT_MAP:
            current = _DIGIT_MAP[ch]
        elif ch == "十":
            if current == 0:
                current = 1
            result += current * 10
            current = 0
        elif ch == "百":
            if current == 0:
                current = 1
            result += current * 100
            current = 0
        elif ch == "千":
            if current == 0:
                current = 1
            result += current * 1000
            current = 0
        # 忽略其他字符
    result += current  # 剩余个位
    return result if result > 0 or text == "零" else None


def _strip_front_matter(text: str) -> str:
    """跳过 YAML front matter，返回正文。

    front matter 以 ``---`` 开始、``---`` 结束；不存在时返回原文。
    """
    if not text.startswith("---"):
        return text
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].rstrip("\r\n").strip() != "---":
        return text
    for i in range(1, len(lines)):
        if lines[i].rstrip("\r\n").strip() == "---":
            return "".join(lines[i + 1:])
    return text


def _extract_articles(body: str) -> list[tuple[str, str, str | None, str | None]]:
    """从正文中提取条文，返回 (article_number, article_text, chapter, section) 列表。

    - 章节标题通过最近的 markdown 标题行（``# 第X章`` / ``# 第X节``）追踪
    - 条文以 ``第X条`` 开头（兼容 ``- **第X条**`` markdown 加粗格式）
    - 每条正文延伸到下一条 / 章节标题 / 文件末尾
    - 无条文的文件返回空列表（由调用方处理整文 fallback）
    """
    current_chapter: str | None = None
    current_section: str | None = None

    # 当前正在收集的条文
    art_number: str | None = None
    art_lines: list[str] = []
    art_chapter: str | None = None
    art_section: str | None = None

    result: list[tuple[str, str, str | None, str | None]] = []

    def _flush() -> None:
        if art_number is not None and art_lines:
            text = "\n".join(art_lines).strip()
            if text:
                result.append((art_number, text, art_chapter, art_section))

    for line in body.splitlines():
        # 章标题
        m = _CHAPTER_RE.match(line)
        if m:
            _flush()
            art_number = None
            art_lines = []
            chap_num = m.group(1)
            chap_title = (m.group(2) or "").strip()
            # 规整空白（含全角空格）
            chap_title = re.sub(r"[\s\u3000]+", " ", chap_title).strip()
            current_chapter = f"{chap_num} {chap_title}".strip() if chap_title else chap_num
            current_section = None  # 进入新章时重置节
            continue

        # 节标题
        m = _SECTION_RE.match(line)
        if m:
            _flush()
            art_number = None
            art_lines = []
            sec_num = m.group(1)
            sec_title = (m.group(2) or "").strip()
            sec_title = re.sub(r"[\s\u3000]+", " ", sec_title).strip()
            current_section = f"{sec_num} {sec_title}".strip() if sec_title else sec_num
            continue

        # 条文开头
        m = _ARTICLE_RE.match(line)
        if m:
            _flush()
            art_number = m.group(1)
            art_lines = [line]
            art_chapter = current_chapter
            art_section = current_section
            continue

        # 条文续行（当前正在收集条文时追加）
        if art_number is not None:
            art_lines.append(line)

    _flush()
    return result


def _resolve_authority_level(group: str | None) -> str:
    """从 metadata.group 推导 authority_level。"""
    if group and group in _AUTHORITY_LEVEL_MAP:
        return _AUTHORITY_LEVEL_MAP[group]
    return group or "其他"


# ---------------------------------------------------------------------------
# SubTask 7.1: 条文级切分
# ---------------------------------------------------------------------------
def chunk_law_articles(metadata: LawMetadata) -> list[ArticleChunk]:
    """读取法规 Markdown，按条文切分为 ArticleChunk 列表。

    - 跳过 YAML front matter，从正文开始
    - 按「第X条」切分（支持中文数字和阿拉伯数字）
    - 无条文的法规整文作为一个 chunk（article_number 为空）
    """
    filepath = Path(metadata.raw_filepath)
    try:
        text = filepath.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []

    body = _strip_front_matter(text)
    articles = _extract_articles(body)

    authority_level = _resolve_authority_level(metadata.group)
    official_source = metadata.official_urls[0] if metadata.official_urls else None

    # 无条文格式：整部正文作为单个 chunk
    if not articles:
        body_stripped = body.strip()
        if not body_stripped:
            return []
        chunk = ArticleChunk(
            chunk_id=f"{metadata.source_id}#full",
            source_id=metadata.source_id,
            title=metadata.title,
            article_number="",
            article_text=body_stripped,
            chapter=None,
            section=None,
            authority_level=authority_level,
            status=metadata.status,
            effective_date=metadata.effective_date,
            expiry_date=metadata.expiry_date,
            superseded_by=metadata.superseded_by,
            jurisdiction="中国大陆",
            official_source=official_source,
            content_hash=hashlib.sha256(body_stripped.encode("utf-8")).hexdigest()[:16],
            publication_date=metadata.publication_date,
        )
        return [chunk]

    chunks: list[ArticleChunk] = []
    for article_number, article_text, chapter, section in articles:
        chunk = ArticleChunk(
            chunk_id=f"{metadata.source_id}#{article_number}",
            source_id=metadata.source_id,
            title=metadata.title,
            article_number=article_number,
            article_text=article_text,
            chapter=chapter,
            section=section,
            authority_level=authority_level,
            status=metadata.status,
            effective_date=metadata.effective_date,
            expiry_date=metadata.expiry_date,
            superseded_by=metadata.superseded_by,
            jurisdiction="中国大陆",
            official_source=official_source,
            content_hash=hashlib.sha256(article_text.encode("utf-8")).hexdigest()[:16],
            publication_date=metadata.publication_date,
        )
        chunks.append(chunk)
    return chunks


# ---------------------------------------------------------------------------
# SubTask 7.2: 索引构建
# ---------------------------------------------------------------------------
def build_article_index(lawtext_dir: Path | None = None) -> list[ArticleChunk]:
    """扫描全部法规并切分为条文级 chunk。

    - 调用 ``scan_all_laws`` 获取全部 LawMetadata
    - 对每个 metadata 调用 ``chunk_law_articles``
    - 单文件异常被捕获并跳过，保证全库扫描不中断
    """
    metadatas = scan_all_laws(lawtext_dir)
    chunks: list[ArticleChunk] = []
    for meta in metadatas:
        try:
            chunks.extend(chunk_law_articles(meta))
        except Exception:
            continue
    return chunks


def save_index_json(chunks: list[ArticleChunk], output_path: Path) -> None:
    """序列化 chunks 为 JSON 文件（用于离线测试和降级，暂不依赖 OpenSearch）。"""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "schema_version": ARTICLE_INDEX_SCHEMA_VERSION,
        "chunks": [chunk.model_dump(mode="json") for chunk in chunks],
    }
    output_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _save_article_index_pickle(chunks: list[ArticleChunk], pkl_path: Path) -> None:
    """P0-2：把 ArticleChunk 列表序列化为 pickle（运行时加载比 JSON 快 3-5x）。

    与 ``lvyan.retrieval.lexical._load_article_chunks`` 的 pickle schema 保持一致：
    ``{"schema_version": int, "chunks": [ArticleChunk, ...]}``。
    """
    import pickle

    pkl_path = Path(pkl_path)
    pkl_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": ARTICLE_INDEX_SCHEMA_VERSION,
        "chunks": chunks,
    }
    with open(pkl_path, "wb") as f:
        pickle.dump(payload, f)


def prewarm_bm25_index(chunks: list[ArticleChunk], manifests_dir: Path) -> None:
    """P0-2：构建并落盘全局 BM25 倒排索引（pickle + JSON）。

    Docker 构建期调用，确保运行时首个用户请求无需等待 10-30s 索引构建。
    复用 ``lvyan.retrieval.lexical`` 的构建 / 序列化逻辑，保证与运行时
    加载逻辑完全一致（schema_version / signature 校验可命中缓存）。
    """
    import pickle

    from lvyan.retrieval.lexical import (
        ARTICLE_INDEX_SCHEMA_VERSION as LEX_SCHEMA_VERSION,
        _build_bm25_index,
        _serialize_bm25_index,
    )

    manifests_dir = Path(manifests_dir)
    manifests_dir.mkdir(parents=True, exist_ok=True)

    bm25_pkl = manifests_dir / "bm25_index.pkl"
    bm25_json = manifests_dir / "bm25_index.json"

    print(f"[prewarm] 构建 BM25 倒排索引（{len(chunks)} chunks）...", file=sys.stderr)
    index = _build_bm25_index(chunks)
    serialized = _serialize_bm25_index(index)
    # _serialize 已写入 schema_version，但显式校验避免未来漂移
    assert serialized["schema_version"] == LEX_SCHEMA_VERSION

    with open(bm25_pkl, "wb") as f:
        pickle.dump(serialized, f)
    print(f"[prewarm] 已写入 pickle BM25 索引 -> {bm25_pkl}", file=sys.stderr)

    with open(bm25_json, "w", encoding="utf-8") as f:
        json.dump(serialized, f, ensure_ascii=False)
    print(f"[prewarm] 已写入 JSON BM25 索引 -> {bm25_json}", file=sys.stderr)


# ---------------------------------------------------------------------------
# SubTask 7.3: 数据库/索引写入桩
# ---------------------------------------------------------------------------
def _tcp_reachable(host: str, port: int, timeout: float = 1.0) -> bool:
    """检查 TCP 端口是否可达。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        return sock.connect_ex((host, port)) == 0
    except OSError:
        return False
    finally:
        sock.close()


def write_to_opensearch(chunks: list[ArticleChunk]) -> int:
    """将 chunks 写入 OpenSearch。

    桩实现：检查 OpenSearch 是否可达，不可达则打印警告并返回 0。
    真正的写入逻辑可在后续完善，重点保证桩优雅降级。
    """
    url = settings.opensearch_url
    try:
        parsed = urlparse(url)
        host = parsed.hostname or "localhost"
        port = parsed.port or (443 if parsed.scheme == "https" else 9200)
    except ValueError:
        host, port = "localhost", 9200

    if not _tcp_reachable(host, port):
        print(
            f"[warn] OpenSearch 不可达 ({host}:{port})，跳过写入 {len(chunks)} 条",
            file=sys.stderr,
        )
        return 0

    # 可达时的真实写入逻辑（后续完善）
    try:
        from opensearchpy import OpenSearch  # type: ignore[import-untyped]

        client = OpenSearch(
            hosts=[{"host": host, "port": port}],
            http_auth=(settings.opensearch_user, settings.opensearch_password),
            use_ssl=parsed.scheme == "https",
            verify_certs=False,
            ssl_show_warn=False,
        )
        for chunk in chunks:
            client.index(index="law_articles_v2", id=chunk.chunk_id, body=chunk.model_dump(mode="json"))
        return len(chunks)
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] OpenSearch 写入失败：{exc}", file=sys.stderr)
        return 0


def write_to_postgres(chunks: list[ArticleChunk]) -> int:
    """将 chunks 写入 PostgreSQL。

    桩实现：检查 PostgreSQL 是否可达，不可达则打印警告并返回 0。
    真正的写入逻辑可在后续完善，重点保证桩优雅降级。
    """
    url = settings.database_url
    match = re.search(r"@([^:/]+):(\d+)", url)
    if not match:
        print("[warn] PostgreSQL 连接串解析失败，跳过写入", file=sys.stderr)
        return 0
    host = match.group(1)
    port = int(match.group(2))

    if not _tcp_reachable(host, port):
        print(
            f"[warn] PostgreSQL 不可达 ({host}:{port})，跳过写入 {len(chunks)} 条",
            file=sys.stderr,
        )
        return 0

    # 可达时的真实写入逻辑（后续完善）
    try:
        from sqlalchemy import create_engine, text  # type: ignore[import-untyped]

        engine = create_engine(url)
        with engine.begin() as conn:
            for chunk in chunks:
                conn.execute(
                    text(
                        "INSERT INTO law_articles_v2 "
                        "(chunk_id, source_id, title, article_number, article_text, "
                        "chapter, section, authority_level, status, content_hash) "
                        "VALUES (:cid, :sid, :title, :anum, :atext, :chap, :sec, :alvl, :st, :ch)"
                        " ON CONFLICT (chunk_id) DO NOTHING"
                    ),
                    {
                        "cid": chunk.chunk_id,
                        "sid": chunk.source_id,
                        "title": chunk.title,
                        "anum": chunk.article_number,
                        "atext": chunk.article_text,
                        "chap": chunk.chapter,
                        "sec": chunk.section,
                        "alvl": chunk.authority_level,
                        "st": chunk.status,
                        "ch": chunk.content_hash,
                    },
                )
        return len(chunks)
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] PostgreSQL 写入失败：{exc}", file=sys.stderr)
        return 0


# ---------------------------------------------------------------------------
# CLI 入口
# ---------------------------------------------------------------------------
def main() -> None:
    """命令行入口：扫描法规库、切分条文、输出索引 JSON。"""
    parser = argparse.ArgumentParser(
        description="法规条文级切分与索引 v2（按编/章/节/条切分）",
    )
    parser.add_argument(
        "--lawtext-dir",
        type=Path,
        default=None,
        help="法律目录（默认使用 settings.LAWTEXT_DIR）",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=AGENT_DIR / "knowledge" / "manifests" / "article_index_v2.json",
        help="输出 JSON 路径（默认 AGENT/knowledge/manifests/article_index_v2.json）",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="只处理前 N 部法律（用于快速测试，默认无限制）",
    )
    parser.add_argument(
        "--stats",
        action="store_true",
        help="打印统计信息",
    )
    parser.add_argument(
        "--prewarm",
        action="store_true",
        help=(
            "P0-2：预热运行时缓存。除 JSON 索引外，额外生成 article_index_v2.pkl "
            "（pickle 加速加载）与 bm25_index.pkl / bm25_index.json（全局 BM25 "
            "倒排索引）。Docker 构建期使用，避免首个用户请求触发 10-30s 冷启动。"
        ),
    )
    args = parser.parse_args()

    lawtext_dir = args.lawtext_dir or settings.lawtext_dir

    print(f"[ingest] 扫描法规库：{lawtext_dir}", file=sys.stderr)
    metadatas = scan_all_laws(lawtext_dir)
    if args.limit is not None:
        metadatas = metadatas[: args.limit]
    print(f"[ingest] 法规数：{len(metadatas)}", file=sys.stderr)

    chunks: list[ArticleChunk] = []
    for meta in metadatas:
        try:
            chunks.extend(chunk_law_articles(meta))
        except Exception:
            continue

    save_index_json(chunks, args.output)

    if args.prewarm:
        # P0-2：生成 pickle 缓存 + BM25 倒排索引，供运行时直接加载
        pkl_path = args.output.with_suffix(".pkl")
        _save_article_index_pickle(chunks, pkl_path)
        print(f"[ingest] 已写入 pickle 索引：{pkl_path}", file=sys.stderr)
        prewarm_bm25_index(chunks, args.output.parent)
        # P0-B：生成 corpus_manifest.json，供运行时校验法库/索引一致性
        from lvyan.retrieval.manifest import write_corpus_manifest

        manifest_path = write_corpus_manifest(chunks, lawtext_dir, args.output.parent)
        print(f"[ingest] 已写入 corpus_manifest：{manifest_path}", file=sys.stderr)

    if args.stats:
        with_article = sum(1 for c in chunks if c.article_number)
        with_chapter = sum(1 for c in chunks if c.chapter)
        with_section = sum(1 for c in chunks if c.section)
        print(f"[stats] 法规数        : {len(metadatas)}")
        print(f"[stats] chunk 总数    : {len(chunks)}")
        print(f"[stats] 有 article_number: {with_article}")
        print(f"[stats] 有 chapter      : {with_chapter}")
        print(f"[stats] 有 section      : {with_section}")

    print(f"[ingest] 已写入：{args.output}", file=sys.stderr)


if __name__ == "__main__":
    main()
