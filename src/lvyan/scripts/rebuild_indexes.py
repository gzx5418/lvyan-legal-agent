"""从法规源重建安全索引文件 (LVIX 格式)。

用途
----
- 部署时预生成 MsgPack 索引，避免首次启动时现场构建。
- 迁移期从旧 JSON 缓存转换为 LVIX 格式。
- 强制重建索引（忽略现有缓存）。

CLI 用法
--------
    python -m lvyan.scripts.rebuild_indexes
    python -m lvyan.scripts.rebuild_indexes --force
    python -m lvyan.scripts.rebuild_indexes --manifests-dir /app/knowledge/manifests
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
_logger = logging.getLogger(__name__)


def rebuild_article_index(
    manifests_dir: Path,
    *,
    force: bool = False,
    lawtext_dir: Path | None = None,
) -> bool:
    """重建 article_index LVIX 文件。

    Args:
        manifests_dir: 索引输出目录。
        force: 忽略现有有效索引，强制重建。
        lawtext_dir: 现场构建时的法规源目录；None 时用默认 ``LAWTEXT_DIR``
            （供 sync_sources 等入口把用户参数传递到重建流程）。

    Returns:
        True 表示成功重建, False 表示跳过或失败。
    """
    from lvyan.retrieval.safe_index import SafeIndexStore
    from lvyan.retrieval.lexical import ARTICLE_INDEX_SCHEMA_VERSION, _compute_chunk_signature

    lvix_path = manifests_dir / "article_index_v3.lvix"
    json_path = manifests_dir / "article_index_v2.json"

    if not force and lvix_path.is_file() and SafeIndexStore.is_valid(lvix_path):
        _logger.info("article_index LVIX 已存在且有效，跳过（使用 --force 强制重建）")
        return True

    # 优先从 JSON 缓存读取（损坏/不可读时告警并降级走现场构建，而非裸崩）。
    # 注意 --force 跳过本路径：JSON 缓存固化的元数据可能已过时（如
    # law_metadata_overrides.yaml 新增的失效日期只在现场构建时经
    # parse_law_metadata 应用），force 的语义就是从法规源全量重建。
    if not force and json_path.is_file():
        import json

        _logger.info("从 JSON 缓存转换: %s", json_path)
        try:
            with open(json_path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            _logger.warning("JSON 缓存读取失败 (%s)，降级从法规源现场构建", exc)
            raw = None
        if isinstance(raw, dict) and isinstance(raw.get("chunks"), list):
            chunks_data = raw["chunks"]
            corpus_hash = _compute_chunk_signature(chunks_data)
            SafeIndexStore.save(
                lvix_path,
                {"schema_version": ARTICLE_INDEX_SCHEMA_VERSION, "chunks": chunks_data},
                corpus_hash=corpus_hash,
                schema_version=ARTICLE_INDEX_SCHEMA_VERSION,
                item_count=len(chunks_data),
            )
            _logger.info("article_index LVIX 写入完成: %s (%d chunks)", lvix_path, len(chunks_data))
            return True

    # 从法规源现场构建
    _logger.info("JSON 缓存不存在或不可用，从法规源现场构建 ...")
    try:
        from lvyan.scripts.ingest_laws import build_article_index, save_index_json

        chunks = build_article_index(lawtext_dir)
        if not chunks:
            _logger.warning("构建结果为空（法规源可能不可用）")
            return False

        # 先写 JSON（兼容）
        manifests_dir.mkdir(parents=True, exist_ok=True)
        save_index_json(chunks, json_path)

        # 再写 LVIX
        corpus_hash = _compute_chunk_signature(chunks)
        SafeIndexStore.save(
            lvix_path,
            {
                "schema_version": ARTICLE_INDEX_SCHEMA_VERSION,
                # mode="json"：date 字段转 ISO 字符串，避免 msgpack 无法序列化
                # datetime.date（issue #11）
                "chunks": [c.model_dump(mode="json") for c in chunks],
            },
            corpus_hash=corpus_hash,
            schema_version=ARTICLE_INDEX_SCHEMA_VERSION,
            item_count=len(chunks),
        )
        _logger.info("article_index LVIX 写入完成: %s (%d chunks)", lvix_path, len(chunks))
        return True
    except Exception as exc:  # noqa: BLE001 - CLI rebuild boundary
        _logger.error("article_index 构建失败: %s", exc)
        return False


def rebuild_bm25_index(
    manifests_dir: Path,
    *,
    force: bool = False,
    lawtext_dir: Path | None = None,
) -> bool:
    """重建 bm25_index LVIX 文件。

    Args:
        manifests_dir: 索引输出目录（同时作为 article chunks 缓存的读取目录）。
        force: 忽略现有有效索引，强制重建。
        lawtext_dir: 现场构建时的法规源目录；None 时用默认 ``LAWTEXT_DIR``。

    Returns:
        True 表示成功重建, False 表示跳过或失败。
    """
    from lvyan.retrieval.safe_index import SafeIndexStore
    from lvyan.retrieval.lexical import ARTICLE_INDEX_SCHEMA_VERSION

    lvix_path = manifests_dir / "bm25_index_v3.lvix"
    json_path = manifests_dir / "bm25_index.json"

    if not force and lvix_path.is_file() and SafeIndexStore.is_valid(lvix_path):
        _logger.info("bm25_index LVIX 已存在且有效，跳过（使用 --force 强制重建）")
        return True

    # 从 JSON 缓存转换（损坏/不可读时告警并降级，而非裸崩）；
    # --force 跳过缓存（理由同 rebuild_article_index：缓存元数据可能过时）。
    if not force and json_path.is_file():
        import json

        _logger.info("从 JSON 缓存转换: %s", json_path)
        try:
            with open(json_path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            _logger.warning("JSON 缓存读取失败 (%s)，降级从 article chunks 构建", exc)
            raw = None
        if isinstance(raw, dict) and raw.get("n_docs"):
            corpus_hash = str(raw.get("signature", ""))
            SafeIndexStore.save(
                lvix_path,
                raw,
                corpus_hash=corpus_hash,
                schema_version=ARTICLE_INDEX_SCHEMA_VERSION,
                item_count=int(raw.get("n_docs", 0)),
            )
            _logger.info(
                "bm25_index LVIX 写入完成: %s (n_docs=%d)", lvix_path, raw.get("n_docs", 0)
            )
            return True

    _logger.warning("bm25 JSON 缓存不存在，需要先加载 article chunks 再构建")
    _logger.info("尝试从 article_index 构建 bm25 ...")
    try:
        from lvyan.retrieval.lexical import (
            _load_article_chunks,
            _build_bm25_index,
            _serialize_bm25_index,
            _compute_chunk_signature,
        )

        # 显式传入 manifests_dir：不再固定读 AGENT_DIR 默认目录下的缓存
        chunks = _load_article_chunks(manifests_dir, lawtext_dir=lawtext_dir)
        if not chunks:
            _logger.warning("article chunks 为空，无法构建 bm25 索引")
            return False

        index = _build_bm25_index(chunks)
        serialized = _serialize_bm25_index(index)
        corpus_hash = _compute_chunk_signature(chunks)

        manifests_dir.mkdir(parents=True, exist_ok=True)

        # 写 LVIX
        SafeIndexStore.save(
            lvix_path,
            serialized,
            corpus_hash=corpus_hash,
            schema_version=ARTICLE_INDEX_SCHEMA_VERSION,
            item_count=index["n_docs"],
        )
        _logger.info("bm25_index LVIX 写入完成: %s (n_docs=%d)", lvix_path, index["n_docs"])

        # 同时写 JSON 兼容
        import json

        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(serialized, f, ensure_ascii=False)
        return True
    except Exception as exc:  # noqa: BLE001 - CLI rebuild boundary
        _logger.error("bm25_index 构建失败: %s", exc)
        return False


def main(args: argparse.Namespace | None = None) -> int:
    """重建入口。

    Args:
        args: 可选的已解析参数对象（``sync_sources --rebuild`` 复用自身 args
            时传入，避免用户参数被静默忽略）；None 时从 CLI 解析，保持
            ``python -m lvyan.scripts.rebuild_indexes`` 的兼容用法。

    Returns:
        进程退出码：0 全部成功，1 部分失败。
    """
    if args is None:
        parser = argparse.ArgumentParser(description="重建律言安全索引文件 (LVIX)")
        parser.add_argument(
            "--manifests-dir",
            type=Path,
            default=None,
            help="索引文件目录（默认: AGENT/knowledge/manifests）",
        )
        parser.add_argument("--force", action="store_true", help="强制重建（忽略现有有效索引）")
        args = parser.parse_args()

    from lvyan.config import AGENT_DIR

    # 外部传入的 Namespace 可能缺字段（如 sync_sources 无 --manifests-dir/--force），
    # 缺省时回退 CLI 默认值
    manifests_dir = getattr(args, "manifests_dir", None) or (AGENT_DIR / "knowledge" / "manifests")
    force = bool(getattr(args, "force", False))
    lawtext_dir = getattr(args, "lawtext_dir", None)

    _logger.info("索引目录: %s", manifests_dir)
    _logger.info("强制重建: %s", force)

    t0 = time.monotonic()
    # 两个 rebuild 独立捕获异常：第一条失败不阻断第二条（issue #16）
    try:
        ok_article = rebuild_article_index(manifests_dir, force=force, lawtext_dir=lawtext_dir)
    except Exception as exc:  # noqa: BLE001 - CLI rebuild boundary
        _logger.error("article_index 重建异常: %s", exc)
        ok_article = False
    try:
        ok_bm25 = rebuild_bm25_index(manifests_dir, force=force, lawtext_dir=lawtext_dir)
    except Exception as exc:  # noqa: BLE001 - CLI rebuild boundary
        _logger.error("bm25_index 重建异常: %s", exc)
        ok_bm25 = False
    elapsed = time.monotonic() - t0

    _logger.info("完成 (%.1fs): article=%s, bm25=%s", elapsed, ok_article, ok_bm25)

    if not ok_article or not ok_bm25:
        _logger.error("部分索引重建失败")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
