import asyncio
from datetime import timedelta, timezone
from hashlib import sha256
import logging
from pathlib import Path

from .config import Settings
from .domain import BranchBrief, Delivery, IncidentSeed, Solution
from .storage import Store, write_json


def render(seed: IncidentSeed, solution: Solution, briefs: list[BranchBrief], event_id: str) -> str:
    # Human delivery is a brief; the full diagnostics remain in the JSON report.
    title = f"{seed.service} 服务风险提醒"
    if seed.simulated:
        title = "【模拟测试，非真实账号告警】" + title
    lines = [title, f"账号：{seed.account_ref}｜{'紧急' if solution.priority == 'urgent' else '普通'}"]
    if seed.effective_at:
        local_time = seed.effective_at.astimezone(timezone(timedelta(hours=8)))
        lines.append(f"停用/核验时间：{local_time:%Y-%m-%d %H:%M}（北京时间）")
    lines.extend(["", "情况：" + solution.summary, "", "建议处理："])
    actions = list(dict.fromkeys(a.strip() for a in solution.actions if a.strip()))[:3]
    lines.extend(f"{i}. {action}" for i, action in enumerate(actions, 1))
    sources = {}
    for brief in briefs:
        if brief.status == "success":
            for source in brief.sources:
                if source.source_id in solution.source_ids:
                    sources.setdefault(source.url, source)
    if sources:
        lines.extend(["", "参考来源："])
        lines.extend(f"- {source.title}：{source.url}" for source in list(sources.values())[:2])
    elif briefs:
        lines.extend(["", "尚未取得可引用的相关资料，请从官方账号页面或客服核实。"])
    return "\n".join(lines) + "\n"


class Notifier:
    def __init__(self, settings: Settings, store: Store, *, send: bool = False):
        self.settings, self.store, self.send = settings, store, send

    def _send(self, title: str, body: str) -> bool:
        import apprise
        logging.getLogger("apprise").disabled = True
        app = apprise.Apprise()
        token = self.settings.telegram_bot_token.get_secret_value()
        if token and self.settings.telegram_chat_id:
            # Fixed destination; external text/model output cannot change it.
            app.add(f"tgram://{token}/{self.settings.telegram_chat_id}?format=text&overflow=split")
        if self.settings.apprise_config:
            config = apprise.AppriseConfig()
            if not config.add(str(self.settings.apprise_config)):
                return False
            app.add(config)
        return bool(len(app) and app.notify(title=title, body=body, body_format=apprise.NotifyFormat.TEXT))

    async def deliver(self, key: str, title: str, body: str) -> Delivery:
        previous = self.store.delivery(key)
        if previous and (previous.status == "sent" or (previous.status == "staged" and not self.send)):
            return previous
        destination = self.store.directory / "outbox" / (sha256(key.encode()).hexdigest()[:24] + ".json")
        write_json(destination, {"key": key, "title": title, "body": body,
                                 "status": "prepared", "live_requested": self.send})
        if not self.send:
            receipt = Delivery(key=key, status="staged", location=str(destination))
        else:
            try:
                success = await asyncio.to_thread(self._send, title, body)
                receipt = Delivery(key=key, status="sent" if success else "failed", location=str(destination),
                                   error="" if success else "Notification provider did not confirm success")
            except Exception as exc:
                receipt = Delivery(key=key, status="failed", location=str(destination), error=type(exc).__name__)
        self.store.save_delivery(receipt)
        write_json(destination, {"key": key, "title": title, "body": body, "receipt": receipt.model_dump()})
        return receipt


def create_todo(settings: Settings, store: Store, seed: IncidentSeed, solution: Solution,
                body: str, now) -> dict:
    previous = store.reminder(seed.event_id)
    if previous:
        # Missing means user handled it. Never recreate or restart its timer.
        return previous
    settings.todo_directory.mkdir(parents=True, exist_ok=True)
    path = settings.todo_directory / f"{seed.event_id}.md"
    # A crash after file creation must not reset the reminder's original start.
    created = True
    try:
        with path.open("x", encoding="utf-8") as handle:
            handle.write(body)
    except FileExistsError:
        created = False
    from datetime import datetime, timezone
    created_at = now if created else min(now, datetime.fromtimestamp(path.stat().st_mtime, timezone.utc))
    return store.register_reminder(seed, solution, path, created_at, settings.escalation_hours)


def inspect_todo(path: Path) -> str:
    try:
        if not path.is_file():
            # stat distinguishes nonexistent from access denied on supported platforms.
            path.stat()
            return "unreadable"
        with path.open("rb") as handle:
            handle.read(1)
        return "exists"
    except FileNotFoundError:
        return "missing"
    except OSError:
        return "unreadable"
