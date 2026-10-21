import argparse
import asyncio
import json
import logging
from pathlib import Path
import sys

from .config import Settings
from .delivery import Notifier
from .domain import IncidentSeed, parse_time, utc_now
from .gmail import GmailError, GmailReader, authorize
from .inbox import InboxWorkflow
from .llm import DemoModel, QwenModel
from .research import FixtureResearch, YouTubeResearch
from .storage import Store, process_lock
from .workflow import Workflow


def parser():
    root = argparse.ArgumentParser(description="Gmail 最近两小时风险监测、三路研究与独立到期提醒")
    commands = root.add_subparsers(dest="command", required=True)
    for name in ("auth", "poll", "run", "tick", "watch", "status", "graph"):
        item = commands.add_parser(name)
        item.add_argument("--settings", type=Path, default=Path("settings.toml"))
        item.add_argument("--model", choices=["demo", "qwen"], default="qwen")
        item.add_argument("--research", choices=["fixture", "youtube"], default="youtube")
        item.add_argument("--data-dir", type=Path, help="覆盖运行数据目录，演示/测试可单独保存")
        item.add_argument("--todo-dir", type=Path, help="覆盖待办目录")
        item.add_argument("--notify", action="store_true", help="实际发送；默认只准备本地发件箱")
        item.add_argument("--now", help="带时区的模拟当前时间；不改变系统时钟")
        if name == "run":
            item.add_argument("--input", type=Path, default=Path("examples/incidents.json"))
        if name == "graph":
            item.add_argument("--view", choices=["event", "inbox"], default="event")
        if name == "watch":
            item.add_argument("--interval", type=float, default=3600, help="轮询秒数，前台运行")
    return root


def compact(report):
    return {key: report[key] for key in ("event_id", "run_kind", "status", "error", "reason", "failed_node", "delivery", "reused") if key in report}


async def execute(args, settings: Settings, store: Store):
    now = parse_time(args.now) if args.now else utc_now()
    if args.command == "auth":
        await asyncio.to_thread(authorize, settings)
        return 0
    if args.command in {"poll", "watch"} and (args.now or args.model != "qwen"):
        raise GmailError("poll/watch 使用真实时间和千问识别；离线演示请用 run --model demo")
    if args.command == "watch" and (args.now or args.interval < 1):
        raise ValueError("watch needs real time and an interval of at least one second")
    if args.command == "status":
        print(json.dumps({"incidents": [{k: row[k] for k in (
            "event_id", "detected_at", "refresh_due_at", "initial_done", "refresh_done")}
            for row in store.incidents()], "pending_reminders": len(store.pending_reminders()),
            "mail": store.mail_counts()}, ensure_ascii=False, indent=2))
        return 0
    model = QwenModel(settings) if args.model == "qwen" and args.command != "graph" else DemoModel()
    research = YouTubeResearch(settings) if args.research == "youtube" else FixtureResearch(
        Path(__file__).resolve().parent.parent / "examples" / "research-materials.json")
    workflow = Workflow(settings, store, model, research, Notifier(settings, store, send=args.notify))
    if args.command == "graph":
        graph = (InboxWorkflow(settings, store, model, None).graph if args.view == "inbox"
                 else workflow.event_graph)
        print(graph.get_graph().draw_mermaid())
        return 0
    output = {"model_mode": args.model, "research_mode": args.research,
              "notification_mode": "live" if args.notify else "outbox"}
    if args.command == "run":
        seeds = [IncidentSeed.model_validate(item) for item in json.loads(args.input.read_text(encoding="utf-8-sig"))]
        output["events"] = [compact(r) for r in await workflow.run(seeds, now)]
    elif args.command in {"poll", "watch"}:
        output.update(await poll_once(settings, store, model, workflow, now))
    else:
        result = await workflow.tick(now)
        output.update(reminders=result.get("reminders", []), events=[compact(r) for r in result.get("events", [])])
    output["model_usage"] = {"calls": model.calls, "input_tokens": model.input_tokens, "output_tokens": model.output_tokens}
    print(json.dumps(output, ensure_ascii=False, indent=2), flush=True)
    if args.command == "watch":
        while True:
            await asyncio.sleep(args.interval)
            model.calls = model.input_tokens = model.output_tokens = 0
            result = await poll_once(settings, store, model, workflow, utc_now())
            result["model_usage"] = {"calls": model.calls, "input_tokens": model.input_tokens,
                                     "output_tokens": model.output_tokens}
            print(json.dumps(result, ensure_ascii=False), flush=True)
    failed = any(r.get("status") == "failed" or r.get("delivery", {}).get("status") == "failed"
                 for r in output.get("events", []))
    failed |= any(r.get("status") in {"failed", "unreadable"} for r in output.get("reminders", []))
    failed |= (output.get("mail", {}).get("status") == "failed"
               or bool(output.get("mail", {}).get("errors"))
               or bool(output.get("identification", {}).get("errors")))
    return 1 if failed else 0


async def poll_once(settings, store, model, workflow, now):
    reader = GmailReader(settings)
    try:
        inbox = await InboxWorkflow(settings, store, model, reader).poll(now)
    finally:
        if reader.session is not None:
            reader.session.close()
    # Timers and previously registered events run even if Gmail/auth/analysis
    # failed. New events are already durable, so the existing tick graph picks
    # them up without racing a second event worker on the same incident.
    result = await workflow.tick(utc_now())
    return {"mail": inbox["mail"], "identification": inbox["identification"],
            "reminders": result.get("reminders", []),
            "events": [compact(r) for r in result.get("events", [])]}


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
    except GmailError as exc:
        print(str(exc), file=sys.stderr)
        result = 1
    except Exception as exc:
        print(f"运行失败：{type(exc).__name__}。请核对配置或报告；未输出密钥。", file=sys.stderr)
        result = 1
    raise SystemExit(result)
