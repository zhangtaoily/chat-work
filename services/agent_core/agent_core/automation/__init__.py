"""自动化任务：调度/试运行 HITL/推送/模板（Phase 2，ARCHITECTURE 4.8）。

PLAN P2.4 落地任务域模型与调度执行（agent_core.automation.store），
本包薄壳 re-export 对外 API；试运行 HITL/推送渠道/模板库留后续。
"""

from agent_core.automation.store import (
    create,
    delete,
    detail,
    fire_event,
    history,
    inbox,
    list_tasks,
    mark_inbox_read,
    next_run_time,
    pause,
    publish_event,
    record_run,
    reset,
    restore,
    resume,
    run_once,
    start_scheduler,
    stop_scheduler,
    validate_schedule,
)

__all__ = [
    "create",
    "delete",
    "detail",
    "fire_event",
    "history",
    "inbox",
    "list_tasks",
    "mark_inbox_read",
    "next_run_time",
    "pause",
    "publish_event",
    "record_run",
    "reset",
    "restore",
    "resume",
    "run_once",
    "start_scheduler",
    "stop_scheduler",
    "validate_schedule",
]
