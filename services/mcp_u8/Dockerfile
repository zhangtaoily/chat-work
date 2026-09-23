# mcp-u8 生产镜像：uv frozen 同步（无 build-system，仅装依赖）+ 非 root 运行
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH"

# 依赖层缓存
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

# 源码（.dockerignore 排除 .venv/tests）
COPY . .

RUN useradd --system --uid 10001 app && chown -R app:app /app
USER app

EXPOSE 8007
CMD ["python", "server.py"]
