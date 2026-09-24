"""Run optional Ragas checks over a previously saved run, without product calls."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "apps" / "api"))

from evaluation.ragas_support import score_saved_run


async def main() -> int:
    parser = argparse.ArgumentParser(description="对已保存的 EchoTrace 评测运行做 Ragas 辅助检查")
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--no-faithfulness", action="store_true", help="只检查 ID，不调用评判模型")
    args = parser.parse_args()
    if not (args.run_dir / "raw_results.jsonl").exists():
        parser.error("run_dir 不含 raw_results.jsonl")
    try:
        result = await score_saved_run(args.run_dir, faithfulness=not args.no_faithfulness)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
