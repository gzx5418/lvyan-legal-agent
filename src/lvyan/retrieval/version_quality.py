"""法规版本元数据质量报告与发布门禁。"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path

from lvyan.config import LAWTEXT_DIR
from lvyan.retrieval.version_resolver import LawMetadata, build_version_groups, scan_all_laws


@dataclass(frozen=True)
class VersionQualityReport:
    total: int
    unknown_status: int
    missing_effective_date: int
    repealed_without_expiry: int
    superseded_without_relation: int
    missing_official_source: int

    @property
    def unknown_ratio(self) -> float:
        return self.unknown_status / self.total if self.total else 1.0

    @property
    def ready(self) -> bool:
        return bool(self.total) and all(
            value == 0
            for value in (
                self.unknown_status,
                self.repealed_without_expiry,
                self.superseded_without_relation,
                self.missing_official_source,
            )
        )

    def model_dump(self) -> dict[str, int | float | bool]:
        payload: dict[str, int | float | bool] = asdict(self)
        payload["unknown_ratio"] = round(self.unknown_ratio, 6)
        payload["ready"] = self.ready
        return payload


def assess_version_quality(records: list[LawMetadata]) -> VersionQualityReport:
    # 版本聚合会标记同标题下被较新有效版本替代的记录。
    build_version_groups(records)
    return VersionQualityReport(
        total=len(records),
        unknown_status=sum(item.status == "unknown" for item in records),
        missing_effective_date=sum(item.effective_date is None for item in records),
        repealed_without_expiry=sum(
            item.status == "repealed" and item.expiry_date is None for item in records
        ),
        superseded_without_relation=sum(
            item.superseded and not item.superseded_by for item in records
        ),
        missing_official_source=sum(not item.official_urls for item in records),
    )


def scan_version_quality(lawtext_dir: Path | None = None) -> VersionQualityReport:
    return assess_version_quality(scan_all_laws(lawtext_dir or LAWTEXT_DIR))


__all__ = ["VersionQualityReport", "assess_version_quality", "scan_version_quality"]
