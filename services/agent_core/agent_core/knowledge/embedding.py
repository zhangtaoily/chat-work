"""向量嵌入后端（PRD 9.5.3 RAG）：OpenAI 兼容 /embeddings 优先，本地降级。

- EMBEDDING_BASE_URL 配置时调用 OpenAI 兼容 /embeddings（OpenAI/vLLM/网关均可）；
  未配置或调用失败 → 本地字符 bigram 向量（feature hashing 512 维 + L2 归一化），
  保证 mock/离线环境全链路可跑，生产切换仅改环境变量
- 后端标记（backend tag）：不同后端的向量不在同一空间、余弦不可比——
  embed_texts 返回批内统一 backend tag；知识库索引记录入库时的标记，
  后端切换（API 上线/故障降级）时检索侧检测不一致即全量重建（store.reindex）
"""

import hashlib
import math
import re
from typing import Any

import httpx

from agent_core.config import settings

LOCAL_TAG = "local:char-bigram"
THRESHOLD_API = 0.75  # 命中阈值（PRD 9.5.3，真实 embedding 模型口径）
# 本地 bigram 降级后端：短 query 与长 chunk 的余弦几何上限低（bigram 重叠率
# 被 chunk 长度稀释），0.75 恒不命中——按后端独立校准（实测区分度 ~0.45 命中 / ~0 无关）
THRESHOLD_LOCAL = 0.35
_LOCAL_DIM = 512
_cache: dict[str, tuple[str, list[float]]] = {}  # 文本 md5 -> (backend, vector)


def reset() -> None:
    """清空向量缓存（测试隔离）。"""
    _cache.clear()


def backend_tag() -> str:
    """当前配置下的预期后端标记（API 未配置即本地）。"""
    return f"api:{settings.embedding_model}" if settings.embedding_base_url else LOCAL_TAG


def sim_threshold(tag: str) -> float:
    """按后端取命中阈值（PRD 0.75 语义保留给真实 embedding 模型）。"""
    return THRESHOLD_LOCAL if tag == LOCAL_TAG else THRESHOLD_API


def _md5(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def _embed_local(text: str) -> list[float]:
    """本地向量：字符 bigram feature hashing（中文友好，纯计算无外部依赖）。

    数字序列（日期/金额/单号）先折叠为空格：它们无语义贡献，却贡献大量
    唯一 bigram 稀释余弦（query/doc 两侧对称清洗，避免带参 query 命中率骤降）。
    """
    vec = [0.0] * _LOCAL_DIM
    clean = re.sub(r"\d+", " ", text.lower())
    clean = re.sub(r"[\s\-—·./:：,，。;；()（）]+", "", clean)
    grams = [clean[i : i + 2] for i in range(len(clean) - 1)] or ([clean] if clean else [])
    for gram in grams:
        digest = int(hashlib.md5(gram.encode("utf-8")).hexdigest(), 16)
        vec[digest % _LOCAL_DIM] += 1.0
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


def cosine(a: list[float], b: list[float]) -> float:
    """余弦相似度（输入已归一化时等价于点积，此处通用实现）。"""
    num = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(x * x for x in b)) or 1.0
    return num / (na * nb)


def _embed_all_local(texts: list[str]) -> tuple[str, list[list[float]]]:
    """整批本地嵌入（带缓存）。"""
    out: list[list[float]] = []
    for text in texts:
        key = _md5(text)
        hit = _cache.get(key)
        if hit and hit[0] == LOCAL_TAG:
            out.append(hit[1])
        else:
            vec = _embed_local(text)
            _cache[key] = (LOCAL_TAG, vec)
            out.append(vec)
    return LOCAL_TAG, out


async def _post_embeddings(texts: list[str]) -> list[list[float]]:
    """OpenAI 兼容 /embeddings：POST {model, input} → data[{index, embedding}]。"""
    base = settings.embedding_base_url
    assert base is not None  # 调用方保证
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            base.rstrip("/") + "/embeddings",
            json={"model": settings.embedding_model, "input": texts},
            headers={"Authorization": f"Bearer {settings.embedding_api_key}"},
        )
        resp.raise_for_status()
        data: list[dict[str, Any]] = resp.json()["data"]
    data.sort(key=lambda item: item["index"])
    return [item["embedding"] for item in data]


async def embed_texts(texts: list[str]) -> tuple[str, list[list[float]]]:
    """批量嵌入：返回 (backend_tag, vectors)，整批向量同后端（可比性保证）。

    - API 配置时：api-tag 缓存命中直接复用，缺失项现算；调用失败整批降级本地
      （api 缓存弃用），杜绝混空间比较
    - 未配置：本地向量（缓存加速）
    """
    if not texts:
        return backend_tag(), []
    if settings.embedding_base_url:
        tag = backend_tag()
        keys = [_md5(t) for t in texts]
        cached = [_cache.get(k) for k in keys]
        if all(c and c[0] == tag for c in cached):
            return tag, [c[1] for c in cached]
        missing = [i for i, c in enumerate(cached) if not (c and c[0] == tag)]
        try:
            computed = await _post_embeddings([texts[i] for i in missing])
        except Exception:  # noqa: BLE001  # 网络/协议异常统一降级本地，不阻塞对话主流程
            return _embed_all_local(texts)
        for i, vec in zip(missing, computed):
            _cache[keys[i]] = (tag, vec)
        return tag, [_cache[k][1] for k in keys]
    return _embed_all_local(texts)
