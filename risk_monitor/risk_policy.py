"""Shared business context for model reasoning and the deterministic urgency floor."""

from datetime import datetime, timedelta

from .domain import IncidentSeed

URGENT_WITHIN_DAYS = 7


def risk_context(seed: IncidentSeed, now: datetime) -> dict:
    due_soon = bool(seed.effective_at and seed.effective_at <= now + timedelta(days=URGENT_WITHIN_DAYS))
    return {
        "as_of": now.isoformat(),
        "urgent_within_days": URGENT_WITHIN_DAYS,
        "current_service_status": seed.service_status,
        "hours_until_deadline": (round((seed.effective_at - now).total_seconds() / 3600, 2)
                                 if seed.effective_at else None),
        "force_urgent": seed.service_status in {"suspended", "cancelled"} or due_soon,
        "rules": "已停用/取消、截止已过或将在七天内停用/暂停/要求完成身份核验，均为紧急。超过七天结合通知判断；时间未知不能编造期限。七天只是用户自定的提醒阈值，不是服务商的停用周期或宽限期，不能当成通知事实或放进搜索词。当前事件的问题和状态以原通知为准，不把其他风险类别添加到事件中。",
        "simulated": seed.simulated,
        "simulation_handling": "模拟标记仅用于交付时说明测试性质；按给定假设情境研究实际账号问题、评估紧急程度。不要研究模拟系统、测试通知、内部培训或审计，不因模拟标记判为无需处理。",
    }
