"""官方法规源发布前质量检查与原子索引重建入口。

本脚本不抓取网页；上游官方数据应以受控目录或 submodule 提供。流程为：扫描源文件、
应用人工核验覆盖、输出质量报告、严格模式阻断不完整版本元数据，最后重建安全索引。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from lvyan.config import AGENT_DIR, LAWTEXT_DIR
from lvyan.retrieval.version_quality import scan_version_quality


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="核验法规源并重建版本感知索引")
    parser.add_argument("--lawtext-dir", type=Path, default=LAWTEXT_DIR)
    parser.add_argument(
        "--report",
        type=Path,
        default=AGENT_DIR / "knowledge" / "manifests" / "version_quality.json",
    )
    parser.add_argument("--strict", action="store_true", help="元数据不完整时阻断发布")
    parser.add_argument(
        "--baseline",
        type=Path,
        help="与已审计质量基线比较；缺陷数增加或法规总数减少时失败",
    )
    parser.add_argument("--rebuild", action="store_true", help="质量检查后强制重建 LVIX 索引")
    args = parser.parse_args(argv)

    report = scan_version_quality(args.lawtext_dir)
    payload = report.model_dump()
    _atomic_json(args.report, payload)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    if args.baseline:
        baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
        defect_fields = (
            "unknown_status",
            "missing_effective_date",
            "repealed_without_expiry",
            "superseded_without_relation",
            "missing_official_source",
        )
        regressions = [
            field for field in defect_fields if int(payload[field]) > int(baseline.get(field, 0))
        ]
        if int(payload["total"]) < int(baseline.get("total", 0)):
            regressions.append("total")
        if regressions:
            print(
                "法规版本元数据质量低于基线：" + ", ".join(regressions),
                file=sys.stderr,
            )
            return 3
    if args.strict and not report.ready:
        print("法规版本元数据未达到严格发布条件", file=sys.stderr)
        return 2
    if args.rebuild:
        from lvyan.scripts.rebuild_indexes import main as rebuild_main

        previous = sys.argv
        try:
            sys.argv = ["rebuild_indexes", "--force"]
            return int(rebuild_main())
        finally:
            sys.argv = previous
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
