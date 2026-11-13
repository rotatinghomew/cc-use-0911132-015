import json
import sys
from pathlib import Path

first = json.loads(Path(sys.argv[1]).read_text())
replay = json.loads(Path(sys.argv[2]).read_text())
required = ['insight_id', 'variants', 'duration_accounting', 'bottlenecks', 'comparisons', 'unresolved']
missing = [field for field in required if field not in first]
if missing:
    raise SystemExit("缺少分析字段：" + ", ".join(missing))
if not first['insight_id'] or first['insight_id'] != replay.get('insight_id'):
    raise SystemExit("同一输入重放未引用同一分析记录")
print("分析输出结构和重放标识核验完成")
