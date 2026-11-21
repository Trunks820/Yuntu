"""End-to-end test: run the full trip workflow from a natural language query."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)


async def main(query: str) -> None:
    from src.agents.intent_parser import parse_intent_with_metadata
    from src.agents.workflow import run_trip_workflow

    print("\n" + "=" * 60)
    print(f"[1/2] 正在解析出行意图: '{query}' ...")
    trip_req, _ = await parse_intent_with_metadata(query)
    print(f"意图解析成功: 目的地={trip_req.to_city}, 天数={trip_req.days}, 偏好={trip_req.preferences}")
    print("=" * 60)

    print("\n[2/2] 开始执行全链路旅行规划与排程引擎...")
    result = await run_trip_workflow(
        query,
        trip_request=trip_req,
        generation_policy_version="v0.12",
    )

    print("\n" + "=" * 60)
    print(f"目的地: {result.trip_request.to_city}  天数: {result.trip_request.days}")
    print(f"偏好: {result.trip_request.preferences}")
    print(f"避开: {result.trip_request.avoid}")
    print("=" * 60)

    for i, plan in enumerate(result.plans, 1):
        print(f"\n{'─' * 40}")
        print(f"方案 {i}: {plan.plan_name}")
        print(f"使用地点 ({len(plan.used_place_names)}): {', '.join(plan.used_place_names)}")
        print(f"{'─' * 40}")
        print(plan.plan_text)

    if result.review_notes:
        print(f"\n{'─' * 40}")
        print("审核意见:")
        print(result.review_notes)

    print("\n[OK] 规划成功完成！travel_plan_record 已落库")


def cli() -> None:
    parser = argparse.ArgumentParser(description="Run trip workflow end-to-end")
    parser.add_argument(
        "query",
        nargs="?",
        default="重庆2天 不想太累 喜欢美食和夜景",
        help="Natural language trip query",
    )
    args = parser.parse_args()
    asyncio.run(main(args.query))


if __name__ == "__main__":
    cli()
