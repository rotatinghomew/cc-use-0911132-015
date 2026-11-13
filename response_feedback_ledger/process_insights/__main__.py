"""命令行入口：

python3 -m response_feedback_ledger.process_insights \
    --input <process_json> --database <incident_db> --output <bottlenecks_json>

只读输入文件与既有 SQLite 证据库，输出确定性 JSON；同一输入重复运行
得到同一 insight_id，可与原 replay/closure 证据交叉对账。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from response_feedback_ledger.process_insights.analysis import build_process_insights


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="返工与并行任务下的处置流程瓶颈归因")
    parser.add_argument("--input", required=True, help="处置过程 JSON（cases/tasks/events/revisions/pauses）")
    parser.add_argument("--database", required=True, help="既有处置账本 SQLite（关联 replay 与 closure 证据）")
    parser.add_argument("--output", required=True, help="瓶颈分析输出 JSON 路径")
    args = parser.parse_args(argv)

    payload = json.loads(Path(args.input).read_text(encoding="utf-8"))
    result = build_process_insights(payload, database=args.database)
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=False),
        encoding="utf-8",
    )
    summary = {
        "insight_id": result["insight_id"],
        "variants": len(result["variants"]),
        "bottlenecks": len(result["bottlenecks"]),
        "unresolved": len(result["unresolved"]),
        "max_conservation_residual_seconds": result["duration_accounting"][
            "max_conservation_residual_seconds"
        ],
        "output": str(output_path),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
