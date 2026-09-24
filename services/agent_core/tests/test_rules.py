"""规则兜底单元测试：时间解析/时长计算/字段抽取（LLM 接入后的后校验器同样适用）。"""

from datetime import date, datetime

import pytest

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
    # 缺省时刻按当日作息：夏令时（5~9月）8:00-17:00 / 冬令时（10~次年4月）8:30-16:30
    assert rules.normalize_time("2026-09-21") == "2026-09-21T08:00:00"
    assert rules.normalize_time("2026-09-22", end=True) == "2026-09-22T17:00:00"
    assert rules.normalize_time("2026-10-21") == "2026-10-21T08:30:00"
    assert rules.normalize_time("2026-10-22", end=True) == "2026-10-22T16:30:00"
    assert rules.normalize_time("2026-09-21 14:30") == "2026-09-21T14:30:00"


def test_md_dates_and_single_day_inference(monkeypatch: pytest.MonkeyPatch) -> None:
    """口语日期（10月5号）解析 + 单日整天/半天推理（按作息冬夏令时）。"""
    monkeypatch.setattr(rules, "_today", lambda: date(2026, 9, 24))
    # 口语月日 → ISO；已过日期顺延次年
    assert rules.extract_times("我10月5号请假") == ["2026-10-05"]
    assert rules.extract_times("5月1号开始") == ["2027-05-01"]
    assert rules.extract_times("2027年3月1日请假") == ["2027-03-01"]
    # 分句防串扰：首个含日期子句优先，作息说明中的日期不参与起止推理
    assert rules.extract_times("我10月5号请假，家里有事。冬令至10月1号到次年4月30是8点30到16点30") == [
        "2026-10-05"
    ]
    # 单日推理：冬令时整天 8:30-16:30
    draft = rules.extract_leave_fields("我10月5号请假，家里有事")
    assert draft["start_time"] == {"value": "2026-10-05T08:30:00", "source": "ask"}
    assert draft["end_time"] == {"value": "2026-10-05T16:30:00", "source": "ask"}
    assert draft["reason"] == {"value": "家里有事", "source": "ask"}
    # 上午/下午 → 对应半天槽（午休 11:00-12:00）
    assert rules.extract_leave_fields("10月5号上午请半天")["end_time"] == {
        "value": "2026-10-05T11:00:00",
        "source": "ask",
    }
    assert rules.extract_leave_fields("10月5号下午请半天")["start_time"] == {
        "value": "2026-10-05T12:00:00",
        "source": "ask",
    }
    # 「半天」未指明上/下午 → 不猜，仅记开始日期（由补问收口）
    half = rules.extract_leave_fields("10月5号请半天")
    assert "end_time" not in half
    assert "start_time" in half


def test_calculate_workdays_winter_schedule() -> None:
    """冬令时（10月~次年4月）作息槽：整天 8:30-16:30 = 1.0，午休 11:00-12:00。"""
    # 2026-10-05（周一）整天
    assert rules.calculate_workdays("2026-10-05T08:30:00", "2026-10-05T16:30:00") == 1.0
    # 上午半天 8:30-11:00 / 下午半天 12:00-16:30
    assert rules.calculate_workdays("2026-10-05T08:30:00", "2026-10-05T11:00:00") == 0.5
    assert rules.calculate_workdays("2026-10-05T12:00:00", "2026-10-05T16:30:00") == 0.5
    # 夏令时整天 8:00-17:00（5~9月）
    assert rules.calculate_workdays("2026-09-21T08:00:00", "2026-09-21T17:00:00") == 1.0


def test_extract_reason_loose() -> None:
    """无引导词事由兜底：噪声剥离后剩余短句视为事由；操作句/无关句不误伤。"""
    assert rules.extract_reason_loose("我10月5号请假，家里有事") == "家里有事"
    assert rules.extract_reason_loose("我下周三请假，陪家人旅游") == "陪家人旅游"
    assert rules.extract_reason_loose("请年假 2026-09-21 到 2026-09-22") is None
    assert rules.extract_reason_loose("我要调休 10月1号 到 10月2号，来源是加班") is None
    # 改期操作句不落事由：extract_leave_fields 守「改」字，lose 推理不触发
    draft = rules.extract_leave_fields("结束时间改成 10月8号 12:00")
    assert "reason" not in draft


def test_extract_duration_and_reason() -> None:
    assert rules.extract_duration("请2天年假") == 2.0
    assert rules.extract_duration("请1.5天调休") == 1.5
    assert rules.extract_duration("请假") is None
    assert rules.extract_reason("请假两天，事由：家中有事") == "家中有事"
    assert rules.extract_reason("因为生病需要休息") == "生病需要休息"


def test_extract_tx_reason() -> None:
    """调休时长来源（E9 TXReason）：「来源」引导词 + 选项词 → 枚举值 0/1/2。"""
    assert rules.extract_tx_reason("调休1天，来源是加班") == 0
    assert rules.extract_tx_reason("时长来源：旅游") == 1
    assert rules.extract_tx_reason("调休时长来源 其他") == 2
    assert rules.extract_tx_reason("来源为其他") == 2
    # 无来源引导词不误伤（如「用调休抵掉上周的加班」）
    assert rules.extract_tx_reason("用调休抵掉上周的加班") is None
    assert rules.extract_tx_reason("请2天年假，事由：家中有事") is None
    # 草稿集成：写入 tx_reason（ask 来源）
    draft = rules.extract_leave_fields("请2天调休，来源是加班")
    assert draft["tx_reason"] == {"value": 0, "source": "ask"}


def test_calculate_workdays() -> None:
    # 2026-09-21（周一）~ 2026-09-22（周二）：2 个工作日（整天口径向下兼容）
    assert rules.calculate_workdays("2026-09-21T09:00:00", "2026-09-22T18:00:00") == 2.0
    # 2026-09-19（周六）~ 2026-09-20（周日）：0 个工作日
    assert rules.calculate_workdays("2026-09-19T09:00:00", "2026-09-20T18:00:00") == 0.0


def test_calculate_workdays_half_day() -> None:
    """0.5 天粒度：半天槽（夏令时上午 08:00-11:00 / 下午 12:00-17:00）有交集即计 0.5。"""
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
