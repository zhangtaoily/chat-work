"""一次性诊断探针：doCreateRequest 分场景二分定位 SYSTEM_INNER_ERROR。

用法：uv run python _probe_e9_submit.py <user_id> [scenario]
scenario：
- full     全部 6 字段（与线上 submit_leave 同口径，缺省）
- min      仅 Requester
- empty    mainData=[]
- nosec    日期时间带秒（YYYY-MM-DD HH:MM:SS）
- dateonly 仅日期（YYYY-MM-DD）
- noflow   全字段 + isnextflow=0（不走流转）
均直连 _post_with_auth，响应原样打印；报错场景不会真建单。
"""

import asyncio
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent))
load_dotenv(Path(__file__).resolve().parent / ".env")
# e9_client 全链路日志：请求/响应/4xx-5xx 响应体直接进探针输出
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
)

import httpx

from adapters.e9_client import E9OaAdapter

_START, _END = datetime(2026, 10, 5, 8, 30), datetime(2026, 10, 5, 16, 30)


def _build_form(scenario: str, e9_user_id: str, workflow_id: int) -> dict[str, str]:
    if scenario == "dateonly":
        dxqs, dxqz = "2026-10-05", "2026-10-05"
    elif scenario == "nosec":
        dxqs, dxqz = _START.strftime("%Y-%m-%d %H:%M:%S"), _END.strftime("%Y-%m-%d %H:%M:%S")
    else:
        dxqs, dxqz = _START.strftime("%Y-%m-%d %H:%M"), _END.strftime("%Y-%m-%d %H:%M")
    spec: list[tuple[str, str]] = [("Requester", e9_user_id)]
    if scenario not in ("min", "empty"):
        spec += [
            ("dxqs", dxqs),
            ("dxqz", dxqz),
            ("dxts", "1"),
            ("dxxss", "8"),
            ("Content", "联调探针（可删除）"),
        ]
    # TXReason（调休时长来源下拉）值口径二分：数字 0/1/2 vs 文案「加班」
    if scenario == "tx0":
        spec.append(("TXReason", "0"))
    elif scenario == "txlabel":
        spec.append(("TXReason", "加班"))
    return {
        "mainData": json.dumps(
            [{"fieldName": k, "fieldValue": v} for k, v in spec], ensure_ascii=False
        ),
        "detailData": "[]",
        "requestName": f"{e9_user_id}的请假申请（1天）",
        "workflowId": str(workflow_id),
        "otherParams": json.dumps(
            {"isnextflow": 0 if scenario in ("noflow", "tx0", "txlabel") else 1}
        ),
    }


async def main() -> None:
    user_id = sys.argv[1]
    scenario = sys.argv[2] if len(sys.argv) > 2 else "full"
    base = os.environ.get("E9_BASE_URL", "http://192.168.0.83:9026")
    client = httpx.AsyncClient(base_url=base, timeout=httpx.Timeout(60.0))
    adapter = E9OaAdapter(client=client)
    try:
        e9_user_id = adapter._user_map.get(user_id, user_id)
        form = _build_form(scenario, e9_user_id, adapter._workflow_id)
        body = await adapter._post_with_auth(
            "/api/workflow/paService/doCreateRequest",
            "xa.oa.wf.doCreateRequest",
            form,
            user_id,
        )
        print(f"[{scenario}] ->", json.dumps(body, ensure_ascii=False))
    except httpx.HTTPStatusError as exc:
        print(f"[{scenario}] HTTP {exc.response.status_code}")
        print(exc.response.text[:2000])
    except Exception as exc:  # noqa: BLE001
        print(f"[{scenario}] {type(exc).__name__}: {exc}")
    finally:
        await adapter.aclose()


asyncio.run(main())
