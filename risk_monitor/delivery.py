import asyncio
from hashlib import sha256
import logging
from pathlib import Path

from .config import Settings
from .domain import BranchBrief, Delivery, IncidentSeed, Solution
from .storage import Store, write_json


def render(seed: IncidentSeed, solution: Solution, briefs: list[BranchBrief], event_id: str) -> str:
    lines = [f"# {seed.service} 服务风险事项", "", f"事件：{event_id}",
             f"账号标识：{seed.account_ref}", f"优先级：{solution.priority}",
             f"模拟事件：{'是（不是实际账号告警）' if seed.simulated else '否'}",
             f"输入的停用/核验时间：{seed.effective_at.isoformat() if seed.effective_at else '未知'}",
             "", "## 方案", solution.summary, "", "## 建议动作"]
    lines.extend(f"- {action}" for action in solution.actions)
    lines.extend(["", "## 判断依据", *[f"- {reason}" for reason in solution.reasons],
                  "", "## 限制", *[f"- {item}" for item in solution.limitations],
                  "", "## 三路研究"])
    for brief in briefs:
        lines.extend([f"### {brief.purpose.value} / {brief.status}", brief.content.summary])
        lines.extend(f"- 限制：{item}" for item in brief.content.limitations)
        lines.extend(f"- [{source.title}]({source.url})" for source in brief.sources)
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
