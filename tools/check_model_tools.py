"""One synthetic protocol probe, disabled unless --execute is explicit."""

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps/api"))


async def main(execute):
    if not execute:
        print(
            "仅显示计划：1 次工具决策，最多 3 次 HTTP 尝试；不使用用户记录。加 --execute 才会调用百炼。"
        )
        return
    from app.clients.ai import BailianClient
    from app.config import Settings
    from app.services.agent_runtime import Budget, tool_decision

    settings = Settings(_env_file=Path(__file__).resolve().parents[1] / "apps/api/.env")
    ai = BailianClient(settings)
    ai.budget = Budget(3, 2000, 30)
    result = await tool_decision(
        ai,
        {
            "question": "合成协议测试：没有历史记录，请结束为证据不足。",
            "plan": {},
            "observations": [],
            "evidence": [],
        },
    )
    print(
        {
            "valid_tool_structure": True,
            "action": result.tool,
            "physical_calls": ai.budget.calls,
            "reported_tokens": ai.budget.tokens,
        }
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true")
    asyncio.run(main(parser.parse_args().execute))
