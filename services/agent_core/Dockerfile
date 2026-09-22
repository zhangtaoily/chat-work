# agent-core 生产镜像：uv frozen 同步 + 非 root 运行（infra/README.md）
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH"

# 先同步依赖（利用层缓存：pyproject/uv.lock 不变则跳过）
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

# 再装应用本体（hatchling wheel：packages=["agent_core"]）
COPY agent_core ./agent_core
RUN uv sync --frozen --no-dev

# 非 root 运行
RUN useradd --system --uid 10001 app && chown -R app:app /app
USER app

EXPOSE 8000
# 启动命令由 compose 注入（worker 数可调）；默认单 worker
CMD ["uvicorn", "agent_core.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
