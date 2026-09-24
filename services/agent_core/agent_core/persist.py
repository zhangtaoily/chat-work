"""本地文件快照兜底：REDIS_URL 未配置时，各 store 快照落 data/snapshots/。

与各 store 的 Redis 快照同一语义（写事件后镜像、启动 restore），介质换成
进程外文件——dev 零依赖模式重启后任务/配置/知识库/记忆不再丢失；生产配了
REDIS_URL 仍走 Redis，本模块不参与。

- write_json / read_json：整快照原子写（临时文件 + os.replace），防半截文件
- append_line / read_tail_lines：audit 流水 JSONL 增量落盘（启动装回内存环）
- 目录与 cwd 解耦：默认 agent_core 包同级 data/snapshots，可用 SNAPSHOT_DIR
  env 或 set_dir()（测试隔离）覆盖
"""

import asyncio
import json
import os
from pathlib import Path
from typing import Any
from uuid import uuid4

from agent_core.config import env

_DIR: Path | None = None


def snapshot_dir() -> Path:
    """快照目录（惰性创建）。"""
    global _DIR
    if _DIR is None:
        base = env("SNAPSHOT_DIR").strip()
        d = Path(base) if base else Path(__file__).resolve().parents[1] / "data" / "snapshots"
        d.mkdir(parents=True, exist_ok=True)
        _DIR = d
    return _DIR


def set_dir(path: Path) -> None:
    """重定向快照目录（测试隔离用；下次读写即生效）。"""
    global _DIR
    _DIR = Path(path)


def _path(key: str, suffix: str = ".json") -> Path:
    safe = key.replace(":", "_").replace("/", "_")
    return snapshot_dir() / (safe + suffix)


def _write_sync(path: Path, text: str) -> None:
    tmp = path.with_name(f"{path.name}.{uuid4().hex}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)  # 原子替换，进程中断不留半截快照


async def write_json(key: str, payload: dict[str, Any]) -> None:
    """整快照落盘（原子写）。"""
    await asyncio.to_thread(_write_sync, _path(key), json.dumps(payload, ensure_ascii=False))


async def read_json(key: str) -> dict[str, Any] | None:
    """读整快照；不存在或损坏返回 None（损坏不阻断启动）。"""
    path = _path(key)
    try:
        raw = await asyncio.to_thread(path.read_text, encoding="utf-8")
    except FileNotFoundError:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _append_sync(path: Path, line: str) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


async def append_line(key: str, obj: dict[str, Any]) -> None:
    """JSONL 追加一行（audit 流水增量落盘）。"""
    await asyncio.to_thread(_append_sync, _path(key, ".jsonl"), json.dumps(obj, ensure_ascii=False))


async def read_tail_lines(key: str, tail: int) -> list[dict[str, Any]]:
    """读 JSONL 尾部 N 条（损坏行跳过），并压实文件为尾部防无限膨胀。"""
    path = _path(key, ".jsonl")
    try:
        raw = await asyncio.to_thread(path.read_text, encoding="utf-8")
    except FileNotFoundError:
        return []
    items: list[dict[str, Any]] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            items.append(obj)
    items = items[-tail:]
    text = "".join(json.dumps(x, ensure_ascii=False) + "\n" for x in items)
    await asyncio.to_thread(_write_sync, path, text)
    return items


async def remove(key: str) -> None:
    """删除快照文件（测试隔离用）。"""
    for suffix in (".json", ".jsonl"):
        await asyncio.to_thread(_path(key, suffix).unlink, True)
