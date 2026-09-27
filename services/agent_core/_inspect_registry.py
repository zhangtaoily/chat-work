"""一次性检查脚本：反射技能条目字段（用后即删）。"""

import json

from agent_core.skills.registry import SKILLS

out = {
    k: {f: v.get(f) for f in ("title", "rw", "dept_scope")}
    for k in ("oa_leave_request", "bi_query", "automation_task_create")
    if k in SKILLS
}
print(json.dumps(out, ensure_ascii=False))
