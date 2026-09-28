"""类案库 OpenSearch 灌库脚本(U-02)。

把 :class:`lvyan.schemas.CaseDocument` 灌入 OpenSearch ``legal_cases`` 索引,
供 :class:`lvyan.retrieval.case_source.OpenSearchCaseSource` 检索。与
``ingest_laws.write_to_opensearch``(法条索引)平行,字段对齐其查询字段
(``title/summary/full_text/case_type/court`` 多字段检索 + term 过滤)。

CLI 用法::

    python -m lvyan.scripts.ingest_cases --input cases.jsonl [--index legal_cases]
        [--force] [--batch-size 500]

``--input`` 为 JSONL(每行一个 CaseDocument JSON,由 U-03 数据适配器生成)。
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

_logger = logging.getLogger("lvyan.scripts.ingest_cases")

CASES_INDEX_NAME = "legal_cases"

# OpenSearchCaseSource 的多字段检索与 term 过滤依赖这些字段;
# 其余字段进 CaseResult.metadata(要素化字段随 metadata 返回)
_CASE_INDEX_MAPPING: dict = {
    "settings": {"index": {"number_of_shards": 1, "number_of_replicas": 0}},
    "mappings": {
        "properties": {
            # 检索核心字段(对齐 OpenSearchCaseSource._search_sync)
            "case_id": {"type": "keyword"},
            "title": {"type": "text"},
            "case_number": {"type": "keyword"},
            "court": {"type": "text", "fields": {"keyword": {"type": "keyword"}}},
            "date": {"type": "keyword"},  # OpenSearchCaseSource 读 str 型 date
            "case_type": {"type": "keyword"},
            "summary": {"type": "text"},
            "full_text": {"type": "text"},
            # 过滤/分级
            "effective_level": {"type": "keyword"},
            "judgment_date": {"type": "date"},
            "source_url": {"type": "keyword"},
            "source_name": {"type": "keyword"},
            "content_hash": {"type": "keyword"},
            # 要素化字段(U-50 要件级检索使用)
            "legal_elements": {"type": "text"},
            "legal_issues": {"type": "text"},
            "claims": {"type": "text"},
            "defenses": {"type": "text"},
            "cited_statute_titles": {"type": "keyword"},
        }
    },
}


def case_document_to_index_doc(doc) -> dict:
    """CaseDocument → OpenSearch 索引文档(含 OpenSearchCaseSource 核心字段)。

    ``title/summary/full_text/date`` 是数据源层的读取契约;要素化字段平铺,
    cited_statutes 折叠为标题列表供 keyword 过滤。
    """
    from lvyan.schemas import CaseDocument

    if not isinstance(doc, CaseDocument):
        doc = CaseDocument.model_validate(doc)
    judgment = doc.judgment_date.isoformat() if doc.judgment_date else ""
    return {
        # 数据源核心契约字段
        "case_id": doc.case_id,
        "title": doc.title,
        "case_number": doc.case_number,
        "court": doc.court or "",
        "date": judgment,
        "case_type": doc.case_type,
        "summary": doc.ruling_summary,
        "full_text": "\n".join(
            part for part in (doc.title, doc.brief_facts, doc.ruling_summary) if part
        ),
        # 过滤/分级
        "effective_level": doc.effective_level,
        "judgment_date": doc.judgment_date.isoformat() if doc.judgment_date else None,
        "source_url": doc.source_url,
        "source_name": doc.source_name,
        "content_hash": doc.content_hash,
        # 要素化字段
        "legal_elements": doc.legal_elements,
        "legal_issues": doc.legal_issues,
        "claims": doc.claims,
        "defenses": doc.defenses,
        "cited_statute_titles": [ref.title for ref in doc.cited_statutes],
    }


def ensure_case_index(client, index_name: str = CASES_INDEX_NAME, *, force: bool = False) -> bool:
    """确保索引存在(mapping 幂等创建);``force`` 先删后建。"""
    if force and client.indices.exists(index=index_name):
        client.indices.delete(index=index_name)
    if not client.indices.exists(index=index_name):
        client.indices.create(index=index_name, body=dict(_CASE_INDEX_MAPPING))
        _logger.info("已创建类案索引 %s", index_name)
    return True


def ingest_case_documents(
    docs: list,
    *,
    index_name: str = CASES_INDEX_NAME,
    client=None,
    force: bool = False,
    batch_size: int = 500,
) -> tuple[int, int]:
    """灌入类案文档,返回 (成功数, 失败数)。OpenSearch 不可达时返回 (0, 0)。"""
    from lvyan.schemas import CaseDocument

    validated: list[CaseDocument] = []
    for raw in docs:
        validated.append(raw if isinstance(raw, CaseDocument) else CaseDocument.model_validate(raw))
    if not validated:
        return (0, 0)
    seen_ids: set[str] = set()
    for doc in validated:
        if doc.case_id in seen_ids:
            _logger.warning("重复 case_id,后者覆盖前者:%s", doc.case_id)
        seen_ids.add(doc.case_id)

    if client is None:
        client = _default_client()
        if client is None:
            return (0, 0)

    ensure_case_index(client, index_name, force=force)
    from opensearchpy.helpers import bulk  # type: ignore[import-untyped]

    actions = (
        {
            "_op_type": "index",
            "_index": index_name,
            "_id": doc.case_id,
            "_source": case_document_to_index_doc(doc),
        }
        for doc in validated
    )
    succeeded, failed = bulk(
        client, actions, raise_on_error=False, stats_only=True, chunk_size=batch_size
    )
    if failed:
        _logger.warning("类案 bulk 写入存在失败:成功 %d / 失败 %d", succeeded, failed)
    client.indices.refresh(index=index_name)
    return (int(succeeded), int(failed))


def _default_client():
    """按 settings 构建客户端;OpenSearch 未配置/不可达时返回 None(优雅降级)。"""
    from lvyan.config import settings

    if not settings.opensearch_url:
        _logger.info("OPENSEARCH_URL 未配置,跳过类案灌库")
        return None
    try:
        import os

        from opensearchpy import OpenSearch

        verify_certs = os.getenv("OPENSEARCH_VERIFY_CERTS", "true").strip().lower() not in {
            "0",
            "false",
            "no",
            "off",
        }
        client = OpenSearch(
            hosts=[settings.opensearch_url],
            http_auth=(settings.opensearch_user, settings.opensearch_password),
            use_ssl=settings.opensearch_url.startswith("https://"),
            verify_certs=verify_certs,
        )
        if not client.ping():
            _logger.warning("OpenSearch 不可达(%s),跳过类案灌库", settings.opensearch_url)
            return None
        return client
    except Exception as exc:  # noqa: BLE001 boundary-exception: 可选数据源
        _logger.warning("OpenSearch 客户端构建失败,跳过类案灌库: %s", exc)
        return None


def load_documents_from_jsonl(path: Path) -> list:
    """读取 JSONL(每行一个 CaseDocument JSON);损坏行告警并跳过。"""
    docs = []
    with open(path, encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                docs.append(json.loads(line))
            except json.JSONDecodeError as exc:
                _logger.warning("JSONL 第 %d 行损坏,跳过: %s", line_no, exc)
    return docs


def main(args: argparse.Namespace | None = None) -> int:
    parsed = args or _build_parser().parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    input_path = Path(parsed.input)
    if not input_path.is_file():
        _logger.error("输入文件不存在: %s", input_path)
        return 2
    docs = load_documents_from_jsonl(input_path)
    if not docs:
        _logger.error("输入文件为空或全部损坏: %s", input_path)
        return 2

    succeeded, failed = ingest_case_documents(
        docs, index_name=parsed.index, force=parsed.force, batch_size=parsed.batch_size
    )
    _logger.info("类案灌库完成:成功 %d / 失败 %d(索引 %s)", succeeded, failed, parsed.index)
    return 0 if failed == 0 and succeeded > 0 else 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="类案库 OpenSearch 灌库(U-02)")
    parser.add_argument("--input", required=True, help="JSONL 输入文件(每行一个 CaseDocument)")
    parser.add_argument("--index", default=CASES_INDEX_NAME)
    parser.add_argument("--force", action="store_true", help="先删后建索引")
    parser.add_argument("--batch-size", type=int, default=500)
    return parser


if __name__ == "__main__":
    sys.exit(main())
