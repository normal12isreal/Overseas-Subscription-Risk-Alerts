"""Read -> identify -> atomically register; downstream event graphs stay shared."""

import asyncio
from datetime import datetime
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from .config import Settings
from .domain import IncidentSeed
from .gmail import GmailError
from .mail import MailAnalysis, MailMessage
from .storage import Store


class InboxState(TypedDict, total=False):
    now: datetime
    mail: dict
    identification: dict


def seeds_from_analysis(message: MailMessage, analysis: MailAnalysis) -> list[IncidentSeed]:
    original = " ".join((message.subject + "\n" + message.body).split()).casefold()
    seeds = {}
    for risk in analysis.risks:
        if " ".join(risk.evidence.split()).casefold() not in original:
            raise ValueError("Risk evidence must be quoted from the email")
        seed = IncidentSeed(
            source_ref=f"gmail:{message.mailbox}:{message.message_id}",
            service=risk.service, account_ref=risk.account_ref,
            notice=(f"邮件声称（尚未通过官方账户核实）：{risk.evidence}\n"
                    f"收信时间：{message.received_at.isoformat()}\n发件人：{message.sender}\n"
                    f"主题：{message.subject}\n正文：{message.body}\n"
                    + ("注意：正文被截断或只取得摘要，可能缺失关键信息。" if message.truncated else "")),
            service_status=risk.service_status, effective_at=risk.effective_at, simulated=False)
        if seed.event_id in seeds:
            raise ValueError("Return one combined risk per service/account in this email")
        seeds[seed.event_id] = seed
    return list(seeds.values())


class InboxWorkflow:
    def __init__(self, settings: Settings, store: Store, model, reader):
        self.settings, self.store, self.model, self.reader = settings, store, model, reader
        graph = StateGraph(InboxState)
        graph.add_node("read_recent_mail", self.read_recent_mail)
        graph.add_node("identify_service_risks", self.identify_service_risks)
        graph.add_edge(START, "read_recent_mail")
        graph.add_edge("read_recent_mail", "identify_service_risks")
        graph.add_edge("identify_service_risks", END)
        self.graph = graph.compile()

    async def read_recent_mail(self, state: InboxState):
        try:
            result = await asyncio.to_thread(self.reader.read, state["now"], self.store)
        except Exception as exc:
            result = {"status": "failed", "error": type(exc).__name__}
            if isinstance(exc, GmailError):
                result["reason"] = str(exc)
        # A read failure still allows previously queued mail to be identified.
        return {"mail": result}

    async def identify_service_risks(self, state: InboxState):
        result = {"analyzed": 0, "no_risk": 0, "event_ids": [], "errors": []}
        for message in self.store.pending_mail(self.settings.gmail_max_messages):
            try:
                # Rotate failed items behind unattempted/older attempts instead
                # of allowing one bad batch to block every future email.
                self.store.mail_attempted(message, state["now"])
                analysis = await self.model.generate(MailAnalysis, "identify", {
                    "email": message.model_dump(mode="json"), "as_of": state["now"].isoformat()})
                seeds = seeds_from_analysis(message, analysis)
                self.store.complete_mail(message, seeds, state["now"], self.settings.refresh_hours)
                result["analyzed"] += 1
                result["no_risk"] += not seeds
                result["event_ids"].extend(s.event_id for s in seeds)
            except Exception as exc:
                # Leave durable input pending, including when it exits the 2h read
                # window. Never label a failed model call as "no risk".
                result["errors"].append({"message_id": message.message_id, "error": type(exc).__name__})
        result["pending"] = self.store.mail_counts().get("pending", 0)
        return {"identification": result}

    async def poll(self, now: datetime) -> dict:
        return await self.graph.ainvoke({"now": now})
