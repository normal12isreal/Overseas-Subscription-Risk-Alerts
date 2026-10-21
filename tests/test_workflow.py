import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys

import pytest

from risk_monitor.config import Settings
from risk_monitor.delivery import Notifier
from risk_monitor.domain import Collected, Delivery, IncidentSeed, Purpose, Solution
from risk_monitor.llm import DemoModel, QwenModel
from risk_monitor.research import FixtureResearch, json_process
from risk_monitor.storage import Store, process_lock
from risk_monitor.workflow import Workflow

NOW = datetime(2026, 10, 21, 4, tzinfo=timezone.utc)
FIXTURES = Path(__file__).resolve().parent.parent / "examples" / "research-materials.json"


def seed(account="sim-A", status="scheduled_deactivation", source="mail-1"):
    return IncidentSeed(source_ref=source, service="giffgaff", account_ref=account,
                        notice="模拟停用通知，未来两个月后可能停止服务", service_status=status,
                        effective_at=NOW + timedelta(days=61))


class RecordingNotifier(Notifier):
    def __init__(self, settings, store, success=True):
        super().__init__(settings, store, send=True)
        self.keys = []
        self.success = success

    async def deliver(self, key, title, body):
        previous = self.store.delivery(key)
        if previous and previous.status == "sent":
            return previous
        self.keys.append(key)
        result = Delivery(key=key, status="sent" if self.success else "failed")
        self.store.save_delivery(result)
        return result


def setup(tmp_path, research=None, model=None, notifier_kind="sent", timeout=1):
    settings = Settings(todo_directory=tmp_path / "todos", data_directory=tmp_path / "data",
                        branch_timeout_seconds=timeout)
    store = Store(settings.data_directory)
    notifier = (Notifier(settings, store) if notifier_kind == "outbox" else
                RecordingNotifier(settings, store, success=notifier_kind != "failed"))
    workflow = Workflow(settings, store, model or DemoModel(), research or FixtureResearch(FIXTURES), notifier)
    return workflow, store, notifier


def run(coro):
    return asyncio.run(coro)


def test_three_same_service_events_are_isolated_and_exact_repeats_are_deduped(tmp_path):
    workflow, store, _ = setup(tmp_path)
    seeds = [seed("A"), seed("B"), seed("C"), seed("A")]
    reports = run(workflow.run(seeds, NOW))
    assert len(reports) == len(store.incidents()) == 3
    assert len(list((tmp_path / "todos").glob("*.md"))) == 3
    for report in reports:
        assert report["status"] == "completed"
        assert {b["purpose"] for b in report["briefs"]} == set(Purpose)
        assert report["trace"].index("join_research") > max(
            report["trace"].index("distill_" + p.value) for p in Purpose)
        content = Path(report["delivery"]["path"]).read_text(encoding="utf-8")
        assert report["seed"]["account_ref"] in content
    original_due = {r["event_id"]: r["refresh_due_at"] for r in store.incidents()}
    calls = workflow.model.calls
    repeated = run(workflow.run(seeds, NOW + timedelta(hours=2)))
    assert all(r["reused"] for r in repeated)
    assert workflow.model.calls == calls
    assert {r["event_id"]: r["refresh_due_at"] for r in store.incidents()} == original_due


def test_deleted_todo_cancels_only_three_hour_escalation_and_refresh_still_runs(tmp_path):
    workflow, store, notifier = setup(tmp_path)
    original = seed()
    run(workflow.run([original], NOW))
    todo_path = Path(store.reminder(original.event_id)["path"])
    todo_path.unlink()  # Test fixture only; user deletion is the business trigger.
    first = run(workflow.tick(NOW + timedelta(hours=3)))
    assert first["reminders"][0]["status"] == "cancelled"
    assert not notifier.keys
    second = run(workflow.tick(NOW + timedelta(hours=24)))
    assert len(second["events"]) == 1
    assert second["events"][0]["run_kind"] == "refresh"
    assert second["events"][0]["solution"]["priority"] == "ordinary"
    assert notifier.keys == [second["events"][0]["run_key"]]
    assert store.incidents()[0]["refresh_done"]
    assert not todo_path.exists()


def test_persistent_todo_escalates_once_and_each_event_refreshes(tmp_path):
    workflow, store, notifier = setup(tmp_path)
    items = [seed("A"), seed("B")]
    run(workflow.run(items, NOW))
    assert not run(workflow.tick(NOW + timedelta(hours=2)))["events"]
    run(workflow.tick(NOW + timedelta(hours=3)))
    assert len(notifier.keys) == 2 and all(k.endswith(":escalation") for k in notifier.keys)
    run(workflow.tick(NOW + timedelta(hours=4)))
    assert len(notifier.keys) == 2
    result = run(workflow.tick(NOW + timedelta(hours=24)))
    assert len(result["events"]) == 2 and len(notifier.keys) == 4
    run(workflow.tick(NOW + timedelta(hours=48)))
    assert len(notifier.keys) == 4  # Repeating daily is intentionally not assumed.


def test_reminder_survives_incident_row_removal(tmp_path):
    workflow, store, notifier = setup(tmp_path)
    original = seed()
    run(workflow.run([original], NOW))
    with store.connect() as db:
        db.execute("DELETE FROM incidents")  # Reproduce a storage-side removal in a disposable test DB.
    run(workflow.tick(NOW + timedelta(hours=3)))
    assert notifier.keys == [original.event_id + ":escalation"]


def test_initial_urgent_and_deleted_todo_do_not_filter_mandatory_refresh(tmp_path):
    workflow, store, notifier = setup(tmp_path)
    original = seed(status="suspended")
    reports = run(workflow.run([original], NOW))
    assert reports[0]["solution"]["priority"] == "urgent"
    assert not store.pending_reminders()
    run(workflow.tick(NOW + timedelta(hours=24)))
    assert len(notifier.keys) == 2


def test_staged_or_failed_refresh_does_not_complete_and_retry_reuses_research(tmp_path):
    workflow, store, _ = setup(tmp_path, notifier_kind="outbox")
    original = seed()
    run(workflow.run([original], NOW))
    result = run(workflow.tick(NOW + timedelta(hours=24)))
    assert result["events"][0]["delivery"]["status"] == "staged"
    assert not store.incidents()[0]["refresh_done"]
    calls = workflow.model.calls
    workflow.notifier = RecordingNotifier(workflow.settings, store, success=False)
    retry = run(workflow.tick(NOW + timedelta(hours=25)))
    assert retry["events"][0]["delivery"]["status"] == "failed"
    assert workflow.model.calls == calls
    workflow.notifier.success = True
    run(workflow.tick(NOW + timedelta(hours=26)))
    assert store.incidents()[0]["refresh_done"]
    assert workflow.model.calls == calls


def test_failure_timeout_empty_all_reach_join_and_do_not_block_other_events(tmp_path):
    class BrokenResearch:
        async def collect(self, task):
            if task.purpose == Purpose.BACKGROUND:
                raise RuntimeError("unreachable")
            if task.purpose == Purpose.APPEAL:
                await asyncio.sleep(1)
            return Collected(purpose=task.purpose, query=task.query, status="empty")

    workflow, _, _ = setup(tmp_path, research=BrokenResearch(), timeout=.02)
    reports = run(workflow.run([seed("A"), seed("B")], NOW))
    assert all(r["status"] == "completed" for r in reports)
    for report in reports:
        assert {b["status"] for b in report["briefs"]} == {"failed", "timed_out", "empty"}
        assert report["solution"]["limitations"]


def test_slow_branch_is_really_joined_after_both_faster_branches(tmp_path):
    class SlowResearch(FixtureResearch):
        async def collect(self, task):
            await asyncio.sleep(.08 if task.purpose == Purpose.RESPONSE else .001)
            return await super().collect(task)

    workflow, _, _ = setup(tmp_path, research=SlowResearch(FIXTURES))
    result = run(workflow.run([seed()], NOW))[0]
    assert len(result["briefs"]) == 3
    assert result["trace"].index("join_research") > result["trace"].index("distill_response")


def test_unreadable_todo_is_not_treated_as_deleted(tmp_path, monkeypatch):
    workflow, store, notifier = setup(tmp_path)
    run(workflow.run([seed()], NOW))
    monkeypatch.setattr("risk_monitor.workflow.inspect_todo", lambda path: "unreadable")
    result = run(workflow.tick(NOW + timedelta(hours=3)))
    assert result["reminders"][0]["status"] == "unreadable"
    assert not notifier.keys and len(store.pending_reminders()) == 1


def test_one_event_model_failure_does_not_lose_other_events(tmp_path):
    class PartialModel(DemoModel):
        async def generate(self, schema, task, payload):
            if task == "extract" and payload["seed"]["account_ref"] == "A":
                raise ValueError("bad model response")
            return await super().generate(schema, task, payload)

    workflow, store, _ = setup(tmp_path, model=PartialModel())
    reports = run(workflow.run([seed("A"), seed("B")], NOW))
    assert {r["status"] for r in reports} == {"failed", "completed"}
    assert len(store.incidents()) == 2


def test_timeout_kills_research_subprocess(tmp_path):
    marker = tmp_path / "should-not-be-written.txt"
    code = "import time,pathlib;time.sleep(.4);pathlib.Path(" + repr(str(marker)) + ").write_text('bad')"

    async def check():
        with pytest.raises(TimeoutError):
            await json_process([sys.executable, "-c", code], .03)
        await asyncio.sleep(.5)
    run(check())
    assert not marker.exists()


def test_hallucinated_summary_sources_are_rejected(tmp_path):
    class HallucinatingModel(DemoModel):
        async def generate(self, schema, task, payload):
            result = await super().generate(schema, task, payload)
            if task == "distill":
                result.source_ids = ["invented-source"]
            return result

    workflow, _, notifier = setup(tmp_path, model=HallucinatingModel())
    result = run(workflow.run([seed(status="suspended")], NOW))[0]
    assert result["status"] == "completed"
    assert all(b["status"] == "failed" for b in result["briefs"])
    assert result["solution"]["source_ids"] == []


def test_restart_reuses_store_and_no_duplicate_todos(tmp_path):
    first, store, _ = setup(tmp_path)
    run(first.run([seed()], NOW))
    second, _, _ = setup(tmp_path)
    reports = run(second.run([seed()], NOW + timedelta(hours=1)))
    assert reports[0]["reused"]
    assert second.model.calls == 0
    assert len(list((tmp_path / "todos").glob("*.md"))) == 1


def test_failed_video_sources_are_not_offered_as_evidence_to_solution_model(tmp_path):
    class PartialResearch(FixtureResearch):
        async def collect(self, task):
            result = await super().collect(task)
            if task.purpose == Purpose.APPEAL:
                result.status = "failed"
                result.materials[0].status = "failed"
            return result

    class CapturingModel(DemoModel):
        payload = None

        async def generate(self, schema, task, payload):
            if task == "compose":
                self.payload = payload
            return await super().generate(schema, task, payload)

    model = CapturingModel()
    workflow, _, _ = setup(tmp_path, model=model, research=PartialResearch(FIXTURES))
    report = run(workflow.run([seed()], NOW))[0]
    assert report["status"] == "completed"
    failed = next(b for b in model.payload["briefs"] if b["purpose"] == Purpose.APPEAL)
    assert failed["sources"] == []
    allowed = {s["source_id"] for s in model.payload["sources"]}
    assert "fixture-giffgaff-support" not in allowed
    assert set(report["solution"]["source_ids"]) == allowed


def test_compose_retry_reuses_saved_branch_results(tmp_path):
    class OnceBrokenModel(DemoModel):
        fail = True

        async def generate(self, schema, task, payload):
            if task == "compose" and self.fail:
                raise RuntimeError("temporary model failure")
            return await super().generate(schema, task, payload)

    class CountingResearch(FixtureResearch):
        calls = 0

        async def collect(self, task):
            self.calls += 1
            return await super().collect(task)

    model, research = OnceBrokenModel(), CountingResearch(FIXTURES)
    workflow, _, _ = setup(tmp_path, model=model, research=research)
    failed = run(workflow.run([seed()], NOW))[0]
    assert failed["failed_node"] == "compose_solution" and research.calls == 3
    calls = model.calls
    model.fail = False
    completed = run(workflow.run([seed()], NOW + timedelta(minutes=1)))[0]
    assert completed["status"] == "completed" and research.calls == 3
    assert model.calls == calls + 1


def test_qwen_adapter_sends_bounded_json_request_and_accounts_for_usage(tmp_path, monkeypatch):
    import httpx
    from risk_monitor.domain import EventFacts

    actual_client = httpx.AsyncClient

    def handler(request):
        data = json.loads(request.content)
        assert data["model"] == "qwen-flash"
        assert data["enable_thinking"] is False
        assert data["response_format"] == {"type": "json_object"}
        assert data["max_tokens"] == 1400
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content":
            json.dumps({"title": "模拟", "summary": "模拟事件", "known_facts": [], "unknowns": []})}}],
            "usage": {"prompt_tokens": 11, "completion_tokens": 7}})

    monkeypatch.setattr("risk_monitor.llm.httpx.AsyncClient",
                        lambda **kw: actual_client(transport=httpx.MockTransport(handler), **kw))
    settings = Settings(todo_directory=tmp_path, llm_base_url="https://example.invalid/v1", llm_api_key="test-key",
                        llm_max_calls=1)
    model = QwenModel(settings)
    assert run(model.generate(EventFacts, "extract", {"seed": seed().model_dump(mode="json")})).title == "模拟"
    assert (model.calls, model.input_tokens, model.output_tokens) == (1, 11, 7)
    with pytest.raises(RuntimeError, match="budget"):
        run(model.generate(EventFacts, "extract", {}))


def test_apprise_telegram_adapter_parses_target_without_network(tmp_path, monkeypatch):
    from apprise.plugins.telegram import NotifyTelegram

    sent = []

    def fake_send(self, body, title="", **kwargs):
        sent.append((title, body))
        return True

    monkeypatch.setattr(NotifyTelegram, "send", fake_send)
    settings = Settings(todo_directory=tmp_path, telegram_bot_token="123456789:" + "a" * 35,
                        telegram_chat_id="123456789")
    notifier = Notifier(settings, Store(tmp_path / "data"), send=True)
    receipt = run(notifier.deliver("test", "模拟标题", "模拟通知"))
    assert receipt.status == "sent" and len(sent) == 1
    run(notifier.deliver("test", "模拟标题", "模拟通知"))
    assert len(sent) == 1


def test_near_deadline_guard_overrides_ordinary_model_priority(tmp_path):
    workflow, _, notifier = setup(tmp_path)
    near = seed().model_copy(update={"effective_at": NOW + timedelta(hours=1)})
    report = run(workflow.run([near], NOW))[0]
    assert report["solution"]["priority"] == "urgent"
    assert report["delivery"]["kind"] == "notification" and len(notifier.keys) == 1


def test_overlapping_processes_cannot_share_one_store_lock(tmp_path):
    with process_lock(tmp_path):
        with pytest.raises(RuntimeError, match="Another monitor process"):
            with process_lock(tmp_path):
                pytest.fail("Overlapping run acquired the same lock")
    with process_lock(tmp_path):
        pass
