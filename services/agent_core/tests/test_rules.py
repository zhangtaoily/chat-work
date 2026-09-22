"""规则兜底单元测试：时间解析/时长计算/字段抽取（LLM 接入后的后校验器同样适用）。"""

from datetime import date, datetime

from agent_core.pipeline import rules


def test_extract_leave_type() -> None:
    assert rules.extract_leave_type("我要请年假") == "annual"
    assert rules.extract_leave_type("调休一天") == "comp"
    assert rules.extract_leave_type("病假") == "sick"
    assert rules.extract_leave_type("你好") is None


def test_extract_times_iso() -> None:
    times = rules.extract_times("请2026-09-21到2026-09-22的年假")
    assert times == ["2026-09-21", "2026-09-22"]


def test_normalize_time_defaults() -> None:
    assert rules.normalize_time("2026-09-21") == "2026-09-21T09:00:00"
    assert rules.normalize_time("2026-09-22", end=True) == "2026-09-22T18:00:00"
    assert rules.normalize_time("2026-09-21 14:30") == "2026-09-21T14:30:00"


def test_extract_duration_and_reason() -> None:
    assert rules.extract_duration("请2天年假") == 2.0
    assert rules.extract_duration("请1.5天调休") == 1.5
    assert rules.extract_duration("请假") is None
    assert rules.extract_reason("请假两天，事由：家中有事") == "家中有事"
    assert rules.extract_reason("因为生病需要休息") == "生病需要休息"


def test_calculate_workdays() -> None:
    # 2026-09-21（周一）~ 2026-09-22（周二）：2 个工作日（整天口径向下兼容）
    assert rules.calculate_workdays("2026-09-21T09:00:00", "2026-09-22T18:00:00") == 2.0
    # 2026-09-19（周六）~ 2026-09-20（周日）：0 个工作日
    assert rules.calculate_workdays("2026-09-19T09:00:00", "2026-09-20T18:00:00") == 0.0


def test_calculate_workdays_half_day() -> None:
    """0.5 天粒度：半天槽（上午 09:00-12:00 / 下午 13:00-18:00）有交集即计 0.5。"""
    # 同一天：上午起 → 中午 12:00 止 = 上午半天 0.5
    assert rules.calculate_workdays("2026-09-21T09:00:00", "2026-09-21T12:00:00") == 0.5
    # 同一天：下午 13:00 起 → 18:00 止 = 下午半天 0.5
    assert rules.calculate_workdays("2026-09-21T13:00:00", "2026-09-21T18:00:00") == 0.5
    # 同一天：09:00 起 → 18:00 止 = 全天 1.0
    assert rules.calculate_workdays("2026-09-21T09:00:00", "2026-09-21T18:00:00") == 1.0
    # 跨天：周一 09:00 起 → 周二 12:00 止 = 1.5（周一全天 + 周二上午）
    assert rules.calculate_workdays("2026-09-21T09:00:00", "2026-09-22T12:00:00") == 1.5
    # 跨天：周一 13:00 起 → 周二 18:00 止 = 1.5（周一下午 + 周二全天）
    assert rules.calculate_workdays("2026-09-21T13:00:00", "2026-09-22T18:00:00") == 1.5
    # 午间起始（12:30）= 下午半天；周末日内半天不计
    assert rules.calculate_workdays("2026-09-21T12:30:00", "2026-09-22T18:00:00") == 1.5
    assert rules.calculate_workdays("2026-09-19T09:00:00", "2026-09-19T12:00:00") == 0.0


def test_extract_leave_fields_full() -> None:
    draft = rules.extract_leave_fields("我要请2026-09-21到2026-09-22的年假，共2天，事由：家中有事")
    assert set(draft) == {"leave_type", "start_time", "end_time", "duration_days", "reason"}
    assert draft["leave_type"] == {"value": "annual", "source": "ask"}
    assert draft["duration_days"] == {"value": 2.0, "source": "ask"}
    assert draft["reason"] == {"value": "家中有事", "source": "ask"}


def test_extract_leave_fields_partial() -> None:
    """信息不全 → 缺失字段不出现，由 validate 驱动追问。"""
    draft = rules.extract_leave_fields("我想请下周三年假")
    assert "leave_type" in draft
    assert "reason" not in draft


def test_weekday_next_week() -> None:
    """「下周X」解析为下一个该星期的日期（跨周自动 +7）。"""
    times = rules.extract_times("下周三年假")
    assert len(times) == 1
    parsed = date.fromisoformat(times[0])
    assert parsed.weekday() == 2  # 周三
    assert parsed > datetime.now().astimezone().date()
