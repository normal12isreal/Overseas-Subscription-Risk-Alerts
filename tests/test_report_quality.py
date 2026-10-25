"""Seven-day business context, relevant provenance, and compact user delivery."""

from datetime import timedelta

import pytest

from risk_monitor.delivery import render
from risk_monitor.domain import BranchBrief, BriefContent, Purpose, Solution, Source
from risk_monitor.llm import DemoModel
from risk_monitor.risk_policy import risk_context
from tests.test_workflow import NOW, run, seed, setup


@pytest.mark.parametrize('status', ['scheduled_deactivation', 'verification_required'])
@pytest.mark.parametrize('remaining,urgent', [(timedelta(days=-1), True), (timedelta(days=6), True),
    (timedelta(days=7), True), (timedelta(days=7, seconds=1), False)])
def test_seven_day_boundary_covers_suspension_and_identity_checks(status, remaining, urgent):
    event = seed(status=status).model_copy(update={'effective_at': NOW + remaining})
    assert risk_context(event, NOW)['force_urgent'] is urgent


def test_unknown_deadline_is_not_invented_and_stopped_services_are_immediate():
    event = seed().model_copy(update={'effective_at': None})
    context = risk_context(event, NOW)
    assert context['hours_until_deadline'] is None and not context['force_urgent']
    for status in ('suspended', 'cancelled'):
        assert risk_context(event.model_copy(update={'service_status': status}), NOW)['force_urgent']


def test_policy_is_injected_before_model_reasoning_and_guard_agrees(tmp_path):
    class CaptureModel(DemoModel):
        def __init__(self):
            self.payloads = []

        async def generate(self, schema, task, payload):
            self.payloads.append((task, payload))
            return await super().generate(schema, task, payload)

    model = CaptureModel()
    workflow, _, notifier = setup(tmp_path, model=model)
    event = seed(status='verification_required').model_copy(update={'effective_at': NOW + timedelta(days=6)})
    report = run(workflow.run([event], NOW))[0]
    assert report['solution']['priority'] == 'urgent'
    assert len(notifier.keys) == 1
    assert {task for task, _ in model.payloads} == {'extract', 'distill', 'compose'}
    for _, payload in model.payloads:
        assert payload['business_context']['urgent_within_days'] == 7
        assert payload['business_context']['hours_until_deadline'] == 144
        assert payload['business_context']['simulated'] is True
    assert report['business_context']['force_urgent'] is True


def test_uncited_or_unrelated_material_is_not_a_successful_branch_or_visible_source(tmp_path):
    class IrrelevantModel(DemoModel):
        async def generate(self, schema, task, payload):
            result = await super().generate(schema, task, payload)
            if task == 'distill':
                result.source_ids = []
            return result

    workflow, _, _ = setup(tmp_path, model=IrrelevantModel())
    report = run(workflow.run([seed(status='suspended')], NOW))[0]
    assert all(b['status'] == 'empty' and b['sources'] == [] for b in report['briefs'])
    assert report['solution']['source_ids'] == []


def test_user_report_omits_diagnostics_failed_sources_and_repeated_actions():
    source = Source(source_id='ok', title='Relevant help', url='https://example.invalid/help')
    unrelated = Source(source_id='bad', title='Google account', url='https://example.invalid/unrelated')
    content = BriefContent(summary='Internal research detail', actions=[], source_ids=['ok'], limitations=['Irrelevant limitations'])
    briefs = [BranchBrief(purpose=Purpose.BACKGROUND, query='query', status='success', content=content, sources=[source]),
              BranchBrief(purpose=Purpose.APPEAL, query='query', status='empty', content=content, sources=[unrelated])]
    solution = Solution(priority='urgent', summary='通知称需要处理账号问题。',
                        actions=['操作一', '操作一', '操作二', '操作三', '操作四'],
                        reasons=['internal reason'], limitations=['internal limitation'], source_ids=['ok'])
    body = render(seed(), solution, briefs, 'internal-event-id')
    assert '北京时间' in body and '模拟测试' in body
    assert body.count('操作一') == 1 and '操作三' in body and '操作四' not in body
    assert source.url in body and unrelated.url not in body
    for hidden in ('internal-event-id', '判断依据', '限制', '三路研究', 'Internal research detail', 'Google account'):
        assert hidden not in body


def test_absent_research_has_one_actionable_gap_instead_of_a_limitations_section():
    brief = BranchBrief(purpose=Purpose.APPEAL, query='query', status='empty',
                        content=BriefContent(summary='No evidence', actions=[], source_ids=[], limitations=[]))
    solution = Solution(priority='urgent', summary='通知称可能停用。', reasons=[], actions=['登录官网核实'],
                        limitations=['many irrelevant unknowns'], source_ids=[])
    body = render(seed(), solution, [brief], 'id')
    assert body.count('尚未取得') == 1 and '##' not in body and 'many irrelevant' not in body
