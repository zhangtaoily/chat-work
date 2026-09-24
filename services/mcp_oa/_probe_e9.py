"""连通 dev E9：regist → applytoken → 按名称反查「调休申请」workflowId（一次性探测）。"""

import asyncio
import base64
import sys

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://192.168.0.83:9026"
APPID = "4E2DA988-2568-11F0-96FA-5254005EB14A"
CLIENT_ID = "xa.crm"


async def main() -> None:
    client_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    cpk = base64.b64encode(
        client_key.public_key().public_bytes(
            Encoding.DER, PublicFormat.SubjectPublicKeyInfo
        )
    ).decode()
    async with httpx.AsyncClient(base_url=BASE, timeout=15) as client:
        r = await client.post(
            "/api/ec/dev/auth/regist",
            headers={
                "appid": APPID,
                "ClientId": CLIENT_ID,
                "OperationCode": "xa.oa.wf.regist",
                "cpk": cpk,
            },
        )
        print("regist", r.status_code, r.text[:400])
        body = r.json()
        spk, secrit = body.get("spk"), body.get("secrit")
        if not spk or not secrit:
            sys.exit("regist 未返回 spk/secrit")
        server_pub = serialization.load_der_public_key(base64.b64decode(spk))
        secret = base64.b64encode(
            server_pub.encrypt(secrit.encode(), padding.PKCS1v15())
        ).decode()
        r = await client.post(
            "/api/ec/dev/auth/applytoken",
            headers={
                "appid": APPID,
                "secret": secret,
                "ClientId": CLIENT_ID,
                "OperationCode": "xa.oa.wf.applytoken",
                "time": "3600",
            },
        )
        print("applytoken", r.status_code, r.text[:400])
        token = r.json().get("token", "")
        if not token:
            sys.exit("applytoken 未返回 token")
        # doCreateRequest 路由探测：无效 workflowId=0（E9 必拒，不会真建单），
        # 验证地址路由 + 认证 + 响应结构
        op = {
            "appid": APPID,
            "token": token,
            "ClientId": CLIENT_ID,
            "OperationCode": "xa.oa.wf.doCreateRequest",
            "userid": base64.b64encode(server_pub.encrypt(b"1", padding.PKCS1v15())).decode(),
            "userIdNoEncrypt": "1",
            "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
        }
        r = await client.post(
            "/api/workflow/paService/doCreateRequest",
            data={
                "mainData": "[]",
                "detailData": "[]",
                "requestName": "探针-无效流程勿建",
                "workflowId": "0",
                "otherParams": '{"isnextflow":1}',
            },
            headers=op,
        )
        print("doCreateRequest", r.status_code, r.text[:500])


asyncio.run(main())
