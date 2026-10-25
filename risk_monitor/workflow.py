"""One isolated event graph per incident, plus dynamic batch fan-out."""

import asyncio
from datetime import datetime
import operator
from pathlib import Path
import re
from typing import Annotated, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from .config import Settings
from .delivery import Notifier, create_todo, inspect_todo, render
from .domain import (BranchBrief, BriefContent, Collected, EventFacts, IncidentSeed,
                     Purpose, ResearchPlan, Solution, SolutionContent, parse_time)
from .storage import Store
from .risk_policy import risk_context
from .research import simple_research_plan


class EventState(TypedDict, total=False):
    seed: IncidentSeed
    now: datetime
    run_kind: str
    run_key: str
    facts: EventFacts
    plan: ResearchPlan
    collected: Annotated[list[Collected], operator.add]
    briefs: Annotated[list[BranchBrief], operator.add]
    solution: Solution
    delivery: dict
    report: dict
    trace: Annotated[list[str], operator.add]


class BatchState(TypedDict, total=False):
    jobs: list[dict]
    results: Annotated[list[dict], operator.add]


class TickState(TypedDict, total=False):
    now: datetime
    reminders: list[dict]
    events: list[dict]


def brief_error(purpose: Purpose, query: str, status: str, message: str,
                sources=None) -> BranchBrief:
    return BranchBrief(purpose=purpose, query=query, status=status,
                       content=BriefContent(summary="本路未取得可用研究结论。", actions=[],
                                            source_ids=[], limitations=[message]), sources=sources or [])


class Workflow:
    def __init__(self, settings: Settings, store: Store, model, research, notifier: Notifier):
        self.settings, self.store = settings, store
        self.model, self.research, self.notifier = model, research, notifier
        self.event_gate = asyncio.Semaphore(settings.max_parallel_events)
        self.branch_gate = asyncio.Semaphore(settings.max_parallel_branches)
        self.event_graph = self.build_event_graph()
        self.batch_graph = self.build_batch_graph()
        self.tick_graph = self.build_tick_graph()

    async def extract_event(self, state: EventState):
        facts = await self.model.generate(EventFacts, "extract", {
            "seed": state["seed"].model_dump(mode="json"),
            "business_context": risk_context(state["seed"], state["now"])})
        return {"facts": facts, "trace": ["extract_event"]}

    async def plan_research(self, state: EventState):
        seed = state["seed"]
        plan = simple_research_plan(seed.service, seed.service_status, seed.notice)
        for task in plan.tasks:
            if seed.service.casefold() not in task.query.casefold():
                raise ValueError("Research query must include the service")
            account_pattern = r"(?<!\w)" + re.escape(seed.account_ref) + r"(?!\w)"
            if re.search(account_pattern, task.query, re.IGNORECASE) or "@" in task.query:
                raise ValueError("Research query contains account information")
        return {"plan": plan, "trace": ["plan_research"]}

    def collect_node(self, purpose: Purpose):
        async def collect(state: EventState):
            task = next(t for t in state["plan"].tasks if t.purpose == purpose)
            try:
                async with self.branch_gate:
                    result = await asyncio.wait_for(self.research.collect(task), self.settings.branch_timeout_seconds)
                if result.purpose != purpose:
                    raise ValueError("Research branch identity mismatch")
            except TimeoutError:
                result = Collected(purpose=purpose, query=task.query, status="timed_out", note="研究时间预算已用完")
            except Exception as exc:
                result = Collected(purpose=purpose, query=task.query, status="failed", note=type(exc).__name__)
            return {"collected": [result], "trace": [f"collect_{purpose.value}"]}
        return collect

    def distill_node(self, purpose: Purpose):
        async def distill(state: EventState):
            materials = next(c for c in state["collected"] if c.purpose == purpose)
            success = [m for m in materials.materials if m.status == "success" and (m.text or m.segments)]
            if materials.status != "success" or not success:
                brief = brief_error(purpose, materials.query, materials.status if materials.status != "success" else "empty",
                                    materials.note or "没有可用转写文稿或视频简介", [m.source for m in materials.materials])
            else:
                try:
                    # The worker bounds each video separately; include every acquired transcript.
                    payload = []
                    for item in success:
                        text = item.text or "\n".join(f"[{s.start:.1f}s] {s.text}" for s in item.segments)
                        if item.segments:
                            text = "\n".join(f"[{s.start:.1f}s] {s.text}" for s in item.segments)
                        payload.append({"source": item.source.model_dump(), "text": text, "note": item.note,
                                        "content_kind": item.content_kind, "language_code": item.language_code})
                    content = await self.model.generate(BriefContent, "distill", {
                        "purpose": purpose.value, "query": materials.query, "materials": payload,
                        "goal": next(t.goal for t in state["plan"].tasks if t.purpose == purpose),
                        "incident": {"service": state["seed"].service,
                                     "service_status": state["seed"].service_status,
                                     "notice": state["seed"].notice[:4000]},
                        "facts": state["facts"].model_dump(),
                        "business_context": risk_context(state["seed"], state["now"])})
                    allowed = {m["source"]["source_id"] for m in payload}
                    if not set(content.source_ids) <= allowed:
                        raise ValueError("Summary cited an unknown source")
                    if len(success) < len(materials.materials):
                        content.limitations = ["部分候选视频没有取得正文资料，未据此推断事实", *content.limitations][:10]
                    if not content.source_ids:
                        brief = brief_error(purpose, materials.query, "empty", "取得资料但没有与当前事件相关的支持资料")
                    else:
                        brief = BranchBrief(purpose=purpose, query=materials.query, status="success", content=content,
                                            sources=[m.source for m in success if m.source.source_id in content.source_ids])
                except Exception as exc:
                    brief = brief_error(purpose, materials.query, "failed", "提炼失败：" + type(exc).__name__,
                                        [m.source for m in success])
            return {"briefs": [brief], "trace": [f"distill_{purpose.value}"]}
        return distill

    def join_research(self, state: EventState):
        if len(state["briefs"]) != 3 or {b.purpose for b in state["briefs"]} != set(Purpose):
            raise ValueError("All three branch terminal results are required before composing")
        return {"trace": ["join_research"]}

    async def compose_solution(self, state: EventState):
        seed = state["seed"]
        context = risk_context(seed, state["now"])
        sources = {s.source_id: s for b in state["briefs"] if b.status == "success" for s in b.sources}
        evidence = []
        for brief in state["briefs"]:
            value = brief.model_dump()
            if brief.status != "success":
                value["sources"] = []
            evidence.append(value)
        content = await self.model.generate(SolutionContent, "compose", {
            "seed": seed.model_dump(mode="json"), "facts": state["facts"].model_dump(),
            "briefs": evidence,
            "sources": [s.model_dump() for s in sources.values()], "as_of": state["now"].isoformat(),
            "business_context": context})
        # Provenance belongs to acquired evidence, not to model-generated identifiers.
        solution = Solution(**content.model_dump(), source_ids=list(sources))
        # Deadline/state guard is deterministic; LLM cannot downgrade an immediate stop.
        if context["force_urgent"]:
            solution.priority = "urgent"
            solution.reasons = ["已停用/取消、截止已过或距离停用/核验截止不超过七天，按业务规则判为紧急", *solution.reasons][:8]
        if any(b.status != "success" for b in state["briefs"]):
            solution.limitations = ["研究证据不完整；方案包含待人工核实项", *solution.limitations][:10]
        if seed.simulated:
            solution.limitations = ["这是模拟事件，不代表真实账号已被停用", *solution.limitations][:10]
        return {"solution": solution, "trace": ["compose_solution"]}

    def route_delivery(self, state: EventState):
        return "write_todo" if state["run_kind"] == "initial" and state["solution"].priority == "ordinary" else "notify_user"

    def write_todo(self, state: EventState):
        body = render(state["seed"], state["solution"], state["briefs"], state["seed"].event_id)
        reminder = create_todo(self.settings, self.store, state["seed"], state["solution"], body, state["now"])
        return {"delivery": {"kind": "todo", "status": "written", "path": reminder["path"]}, "trace": ["write_todo"]}

    async def notify_user(self, state: EventState):
        label = "24小时复查更新" if state["run_kind"] == "refresh" else "紧急风险提醒"
        receipt = await self.notifier.deliver(state["run_key"], f"{state['seed'].service} {label}",
                                             render(state["seed"], state["solution"], state["briefs"], state["seed"].event_id))
        return {"delivery": {"kind": "notification", **receipt.model_dump()}, "trace": ["notify_user"]}

    def record_result(self, state: EventState):
        completed = state["delivery"]["status"] in {"sent", "written"}
        report = {"event_id": state["seed"].event_id, "run_key": state["run_key"],
                  "execution": {"model_adapter": type(self.model).__name__,
                                "research_adapter": type(self.research).__name__},
                  "run_kind": state["run_kind"], "as_of": state["now"].isoformat(),
                  "status": "completed" if completed else "awaiting_delivery",
                  "seed": state["seed"].model_dump(mode="json"), "facts": state["facts"].model_dump(),
                  "business_context": risk_context(state["seed"], state["now"]),
                  "plan": state["plan"].model_dump(), "collected": [c.model_dump() for c in state["collected"]],
                  "briefs": [b.model_dump() for b in state["briefs"]], "solution": state["solution"].model_dump(),
                  "delivery": state["delivery"], "trace": [*state["trace"], "record_result"]}
        self.store.save_report(state["run_key"], report)
        if completed:
            self.store.mark_done(state["seed"].event_id, state["run_kind"])
        return {"report": report, "trace": ["record_result"]}

    def build_event_graph(self):
        graph = StateGraph(EventState)
        for name in ("extract_event", "plan_research", "join_research", "compose_solution",
                     "write_todo", "notify_user", "record_result"):
            graph.add_node(name, getattr(self, name))
        graph.add_conditional_edges(START, lambda state: "compose_solution" if len(state.get("briefs", [])) == 3 else "extract_event",
                                    ["extract_event", "compose_solution"])
        graph.add_edge("extract_event", "plan_research")
        ends = []
        for purpose in Purpose:
            collect, distill = "collect_" + purpose.value, "distill_" + purpose.value
            graph.add_node(collect, self.collect_node(purpose))
            graph.add_node(distill, self.distill_node(purpose))
            graph.add_edge("plan_research", collect)
            graph.add_edge(collect, distill)
            ends.append(distill)
        # List form is a barrier: all three must finish, including failure/timeout results.
        graph.add_edge(ends, "join_research")
        graph.add_edge("join_research", "compose_solution")
        graph.add_conditional_edges("compose_solution", self.route_delivery, ["write_todo", "notify_user"])
        graph.add_edge("write_todo", "record_result")
        graph.add_edge("notify_user", "record_result")
        graph.add_edge("record_result", END)
        return graph.compile()

    async def process_event(self, job: dict):
        seed, now, kind = job["seed"], job["now"], job["run_kind"]
        record = self.store.register(seed, now, self.settings.refresh_hours)
        seed = IncidentSeed.model_validate_json(record["seed_json"])
        key = seed.event_id + (":initial" if kind == "initial" else ":refresh_" + parse_time(record["refresh_due_at"]).strftime("%Y%m%dT%H%M%SZ"))
        async with self.event_gate:
            latest_state = {}
            try:
                previous = self.store.run_report(key)
                if previous and "solution" in previous:
                    if previous["status"] == "completed":
                        return {**previous, "reused": True}
                    # A failed/staged delivery retries the saved solution without buying research again.
                    state = {"seed": seed, "now": now, "run_kind": kind, "run_key": key,
                             "solution": Solution.model_validate(previous["solution"]),
                             "briefs": [BranchBrief.model_validate(b) for b in previous["briefs"]]}
                    update = self.write_todo(state) if self.route_delivery(state) == "write_todo" else await self.notify_user(state)
                    previous["delivery"] = update["delivery"]
                    previous["status"] = "completed" if update["delivery"]["status"] in {"sent", "written"} else "awaiting_delivery"
                    self.store.save_report(key, previous)
                    if previous["status"] == "completed":
                        self.store.mark_done(seed.event_id, kind)
                    return {**previous, "reused": True}
                initial = {"seed": seed, "now": now, "run_kind": kind, "run_key": key,
                           "collected": [], "briefs": [], "trace": []}
                if previous and previous.get("failed_node") == "compose_solution" and len(previous.get("briefs", [])) == 3:
                    initial.update(facts=EventFacts.model_validate(previous["facts"]),
                                   plan=ResearchPlan.model_validate(previous["plan"]),
                                   collected=[Collected.model_validate(c) for c in previous["collected"]],
                                   briefs=[BranchBrief.model_validate(b) for b in previous["briefs"]],
                                   trace=previous.get("trace", []))
                async for update in self.event_graph.astream(initial, stream_mode="values"):
                    latest_state = update
                return latest_state["report"]
            except Exception as exc:
                report = {"event_id": seed.event_id, "run_key": key, "run_kind": kind,
                          "status": "failed", "error": type(exc).__name__}
                safe_messages = {
                    "Research query must include the service", "Research query contains account information",
                    "Solution cited an unknown source", "All three branch terminal results are required before composing",
                    "Model call budget reached", "Model output exceeded configured token limit",
                }
                if str(exc) in safe_messages:
                    report["reason"] = str(exc)
                for note in getattr(exc, "__notes__", []):
                    match = re.search(r"During task with name '([^']+)'", note)
                    if match:
                        report["failed_node"] = match.group(1)
                for field in ("facts", "plan", "collected", "briefs", "solution", "trace"):
                    if field in latest_state:
                        value = latest_state[field]
                        report[field] = ([item.model_dump(mode="json") if hasattr(item, "model_dump") else item for item in value]
                                         if isinstance(value, list) else value.model_dump(mode="json"))
                self.store.save_report(key, report)
                return report

    def build_batch_graph(self):
        graph = StateGraph(BatchState)

        def dispatch(state):
            return [Send("process_event", job) for job in state["jobs"]] or END

        async def process(job):
            return {"results": [await self.process_event(job)]}

        graph.add_node("process_event", process)
        graph.add_conditional_edges(START, dispatch, ["process_event", END])
        graph.add_edge("process_event", END)
        return graph.compile()

    async def run(self, seeds: list[IncidentSeed], now: datetime, run_kind="initial") -> list[dict]:
        # Dedupe exact repeats only; separate accounts/notices still get separate graph states.
        seeds = list({seed.event_id: seed for seed in seeds}.values())
        result = await self.batch_graph.ainvoke({"jobs": [
            {"seed": seed, "now": now, "run_kind": run_kind} for seed in seeds], "results": []})
        return result["results"]

    async def check_reminders(self, now: datetime) -> list[dict]:
        results = []
        for row in self.store.pending_reminders():
            try:
                inspection = inspect_todo(Path(row["path"]))
                result = {"event_id": row["event_id"], "inspection": inspection, "status": "waiting"}
                if inspection == "missing":
                    self.store.close_reminder(row["event_id"], now, sent=False)
                    result["status"] = "cancelled"
                elif inspection == "unreadable":
                    result["status"] = "unreadable"
                elif parse_time(row["due_at"]) <= now:
                    seed = IncidentSeed.model_validate_json(row["seed_json"])
                    solution = Solution.model_validate_json(row["solution_json"])
                    solution.priority = "urgent"
                    solution.reasons = ["待办仍存在，已达到独立的3小时升级阈值", *solution.reasons][:8]
                    receipt = await self.notifier.deliver(row["event_id"] + ":escalation",
                        f"{seed.service} 待办已升级为紧急", render(seed, solution, [], row["event_id"]))
                    result["status"] = receipt.status
                    if receipt.status == "sent":
                        self.store.close_reminder(row["event_id"], now, sent=True)
                results.append(result)
            except Exception as exc:
                results.append({"event_id": row["event_id"], "status": "failed", "error": type(exc).__name__})
        return results

    def build_tick_graph(self):
        graph = StateGraph(TickState)

        async def todos(state):
            return {"reminders": await self.check_reminders(state["now"])}

        async def refreshes(state):
            # Retry unfinished initial work independently of mandatory due refreshes.
            pending = [IncidentSeed.model_validate_json(r["seed_json"]) for r in self.store.incidents() if not r["initial_done"]]
            results = await self.run(pending, state["now"])
            due = [IncidentSeed.model_validate_json(r["seed_json"]) for r in self.store.due_refreshes(state["now"])]
            results.extend(await self.run(due, state["now"], "refresh"))
            return {"events": results}

        graph.add_node("check_todos", todos)
        graph.add_node("refresh_due", refreshes)
        graph.add_edge(START, "check_todos")
        graph.add_edge(START, "refresh_due")
        graph.add_edge("check_todos", END)
        graph.add_edge("refresh_due", END)
        return graph.compile()

    async def tick(self, now: datetime) -> dict:
        return await self.tick_graph.ainvoke({"now": now})
