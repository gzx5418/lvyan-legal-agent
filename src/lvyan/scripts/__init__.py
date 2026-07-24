"""脚本层：法规入库（ingest_laws）、索引重建、健康检查等运维脚本。"""

from lvyan.scripts.ingest_laws import build_article_index

__all__ = ["build_article_index"]
