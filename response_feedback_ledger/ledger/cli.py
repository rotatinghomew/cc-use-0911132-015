from __future__ import annotations
import argparse
import json
from response_feedback_ledger.service import run_fixture

def main() -> int:
    parser = argparse.ArgumentParser(description='防灾处置反馈事件溯源账本')
    parser.add_argument("fixture")
    parser.add_argument("--database", required=True)
    args = parser.parse_args()
    result = run_fixture(args.fixture, args.database)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
