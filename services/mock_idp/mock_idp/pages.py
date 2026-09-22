"""OIDC 端点页面模板（开发态内嵌 HTML，无外部静态资源）。"""

from typing import Any

_BASE_CSS = """
  body { font-family: 'Segoe UI', 'Microsoft YaHei', sans-serif; background:#f0f2f5;
         display:flex; align-items:center; justify-content:center; min-height:100vh; margin:0; }
  .card { background:#fff; border-radius:12px; box-shadow:0 4px 16px rgba(0,0,0,.08);
          padding:40px 48px; width:360px; }
  h1 { font-size:20px; color:#1f2329; margin:0 0 6px; }
  .sub { color:#646a73; font-size:13px; margin:0 0 24px; }
  input { width:100%; box-sizing:border-box; padding:10px 12px; border:1px solid #d0d3d6;
          border-radius:6px; font-size:14px; margin-bottom:16px; }
  input:focus { outline:none; border-color:#3370ff; }
  button { width:100%; padding:10px 0; background:#3370ff; color:#fff; border:none;
           border-radius:6px; font-size:14px; cursor:pointer; }
  button:hover { background:#2860e1; }
  .error { background:#fef1f1; color:#d83931; border:1px solid #f8d2d0;
           border-radius:6px; padding:10px 12px; font-size:13px; margin-bottom:16px; }
  .hint { color:#8f959e; font-size:12px; margin-top:16px; line-height:1.6; }
"""


def login_page(params: dict[str, str], error: str | None = None) -> str:
    """授权登录页：工号即身份（HR 主数据权威源，PRD 8.5.6）。"""
    hidden = "".join(
        f'<input type="hidden" name="{k}" value="{v}" />' for k, v in params.items()
    )
    err_html = f'<div class="error">{error}</div>' if error else ""
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="utf-8" /><title>Chat-Work 统一登录</title>
<style>{_BASE_CSS}</style></head>
<body>
  <form class="card" method="post" action="/realms/chat-work/protocol/openid-connect/auth">
    <h1>Chat-Work 统一登录</h1>
    <p class="sub">通过公司统一身份认证登录（开发环境：Mock Broker）</p>
    {err_html}
    {hidden}
    <input name="emp_no" placeholder="员工工号，如 E1001" autofocus />
    <button type="submit">登录</button>
    <p class="hint">
      登录即同意以你的身份调用 OA / BI 系统。<br />
      无法登录？工号未匹配到 HR 主数据时请联系信息科。
    </p>
  </form>
</body>
</html>"""


def token_error_page(error: str, description: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="utf-8" /><title>登录失败</title>
<style>{_BASE_CSS}</style></head>
<body>
  <div class="card">
    <h1>登录失败</h1>
    <p class="sub">{description}</p>
    <div class="error">{error}</div>
    <p class="hint">请关闭此窗口回到客户端重新发起登录。</p>
  </div>
</body>
</html>"""


def jwks_response(jwks: dict[str, Any]) -> dict[str, Any]:
    return jwks
