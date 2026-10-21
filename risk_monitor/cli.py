import argparse
import asyncio
import json
import logging
from pathlib import Path
import sys

from .config import Settings
from .delivery import Notifier
from .domain import IncidentSeed, parse_time, utc_now
from .llm import DemoModel, QwenModel
from .research import FixtureResearch, YouTubeResearch
from .storage import Store, process_lock
from .workflow import Workflow


def parser():
    root = argparse.ArgumentParser(description="模拟事件入口；不读取邮箱或识别邮件风险")
    commands = root.add_subparsers(dest="command", required=True)
    for name in ("run", "tick", "watch", "status", "graph"):
        item = commands.add_parser(name)
        item.add_argument("--settings", type=Path, default=Path("settings.toml"))
        item.add_argument("--model", choices=["demo", "qwen"], default="qwen")
        item.add_argument("--research", choices=["fixture", "youtube"], default="youtube")
        item.add_argument("--data-dir", type=Path, help="覆盖运行数据目录，演示/测试可单独保存")
        item.add_argument("--todo-dir", type=Path, help="覆盖待办目录")
        item.add_argument("--notify", action="store_true", help="实际发送；默认只准备本地发件箱")
        item.add_argument("--now", help="带时区的模拟当前时间；不改变系统时钟")
        if name in {"run", "watch"}:
            item.add_argument("--input", type=Path, default=Path("examples/incidents.json"))
        if name == "watch":
            item.add_argument("--interval", type=float, default=3600, help="轮询秒数，前台运行")
    return root


def compact(report):
    return {key: report[key] for key in ("event_id", "run_kind", "status", "error", "reason", "failed_node", "delivery", "reused") if key in report}


async def execute(args, settings: Settings, store: Store):
    now = parse_time(args.now) if args.now else utc_now()
    if args.command == "watch" and (args.now or args.interval < 1):
        raise ValueError("watch needs real time and an interval of at least one second")
    if args.command == "status":
        print(json.dumps({"incidents": [{k: row[k] for k in (
            "event_id", "detected_at", "refresh_due_at", "initial_done", "refresh_done")}
            for row in store.incidents()], "pending_reminders": len(store.pending_reminders())}, ensure_ascii=False, indent=2))
        return 0
    model = QwenModel(settings) if args.model == "qwen" and args.command != "graph" else DemoModel()
    research = YouTubeResearch(settings) if args.research == "youtube" else FixtureResearch(
        Path(__file__).resolve().parent.parent / "examples" / "research-materials.json")
    workflow = Workflow(settings, store, model, research, Notifier(settings, store, send=args.notify))
    if args.command == "graph":
        print(workflow.event_graph.get_graph().draw_mermaid())
        return 0
    output = {"model_mode": args.model, "research_mode": args.research,
              "notification_mode": "live" if args.notify else "outbox"}
    if args.command in {"run", "watch"}:
        seeds = [IncidentSeed.model_validate(item) for item in json.loads(args.input.read_text(encoding="utf-8-sig"))]
        output["events"] = [compact(r) for r in await workflow.run(seeds, now)]
    else:
        result = await workflow.tick(now)
        output.update(reminders=result.get("reminders", []), events=[compact(r) for r in result.get("events", [])])
    output["model_usage"] = {"calls": model.calls, "input_tokens": model.input_tokens, "output_tokens": model.output_tokens}
    print(json.dumps(output, ensure_ascii=False, indent=2), flush=True)
    if args.command == "watch":
        while True:
            await asyncio.sleep(args.interval)
            # A fresh call budget for each round, never an unlimited background test here.
            if args.model == "qwen":
                model.calls = 0
            result = await workflow.tick(utc_now())
            print(json.dumps({"reminders": result.get("reminders", []),
                              "events": [compact(r) for r in result.get("events", [])]}, ensure_ascii=False), flush=True)
    failed = any(r.get("status") == "failed" or r.get("delivery", {}).get("status") == "failed"
                 for r in output.get("events", []))
    failed |= any(r.get("status") in {"failed", "unreadable"} for r in output.get("reminders", []))
    return 1 if failed else 0


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    # Upstream notification libraries may log URLs containing credentials on failure.
    # Our receipts provide sanitized error categories instead.
    logging.getLogger("apprise").disabled = True
    args = parser().parse_args()
    try:
        settings = Settings.load(args.settings)
        if args.data_dir:
            settings.data_directory = args.data_dir.resolve()
        if args.todo_dir:
            settings.todo_directory = args.todo_dir.resolve()
        with process_lock(settings.data_directory):
            result = asyncio.run(execute(args, settings, Store(settings.data_directory)))
    except KeyboardInterrupt:
        result = 130
    except Exception as exc:
        print(f"运行失败：{type(exc).__name__}。请核对配置或报告；未输出密钥。", file=sys.stderr)
        result = 1
    raise SystemExit(result)
