# -*- coding: utf-8 -*-
"""模型目录：从 OpenAI 兼容端点（默认 OpenRouter）拉取可用模型，精简后缓存。

设计要点：
1. OpenRouter 单次返回 300+ 模型、原始 JSON 可达数百 KB，必须在服务端精简后再下发，
   否则前端渲染会卡死，也浪费每次设置弹窗的带宽。
2. 缓存在 DB（settings 表）+ 内存两级，默认 TTL 30 分钟；force=1 强制刷新。
3. 未配置密钥时返回内置推荐清单（PREFERRED），保证下拉框永远不是空的。
"""
import time
import httpx

from . import db
from .config import DEFAULT_BASE_URL

CACHE_KEY = "llm_models_cache"
CACHE_TTL = 1800  # 30 分钟

# 未配置密钥 / 拉取失败时的兜底清单（OpenRouter 上长期可用的主流模型）
PREFERRED = [
    {"id": "openai/gpt-4o-mini", "name": "OpenAI GPT-4o mini", "group": "OpenAI",
     "ctx": 128000, "pin": 0.15, "pout": 0.6, "dyn": False, "free": False, "tools": True},
    {"id": "openai/gpt-4o", "name": "OpenAI GPT-4o", "group": "OpenAI",
     "ctx": 128000, "pin": 2.5, "pout": 10.0, "dyn": False, "free": False, "tools": True},
    {"id": "anthropic/claude-haiku-4.5", "name": "Anthropic Claude Haiku 4.5", "group": "Anthropic",
     "ctx": 200000, "pin": 1.0, "pout": 5.0, "dyn": False, "free": False, "tools": True},
    {"id": "google/gemini-2.5-flash", "name": "Google Gemini 2.5 Flash", "group": "Google",
     "ctx": 1048576, "pin": 0.3, "pout": 2.5, "dyn": False, "free": False, "tools": True},
    {"id": "deepseek/deepseek-v4-flash", "name": "DeepSeek V4 Flash", "group": "DeepSeek",
     "ctx": 131072, "pin": 0.068, "pout": 0.135, "dyn": False, "free": False, "tools": True},
    {"id": "qwen/qwen3.7-flash", "name": "通义千问 3.7 Flash", "group": "Qwen",
     "ctx": 131072, "pin": 0.03, "pout": 0.13, "dyn": False, "free": False, "tools": True},
    {"id": "moonshotai/kimi-k2.5", "name": "Moonshot Kimi K2.5", "group": "Moonshot / Kimi",
     "ctx": 262144, "pin": 0.45, "pout": 2.25, "dyn": False, "free": False, "tools": True},
    {"id": "meta-llama/llama-3.3-70b-instruct", "name": "Llama 3.3 70B Instruct", "group": "Meta",
     "ctx": 131072, "pin": 0.1, "pout": 0.32, "dyn": False, "free": False, "tools": True},
]

GROUPS = [
    ("openai", "OpenAI"), ("anthropic", "Anthropic"), ("google", "Google"),
    ("deepseek", "DeepSeek"), ("qwen", "Qwen"), ("moonshotai", "Moonshot / Kimi"),
    ("x-ai", "xAI Grok"), ("mistralai", "Mistral"), ("meta-llama", "Meta Llama"),
    ("microsoft", "Microsoft"), ("amazon", "Amazon"), ("nvidia", "NVIDIA"),
    ("minimax", "MiniMax"), ("z-ai", "智谱 GLM"), ("baidu", "百度"),
    ("stepfun", "阶跃星辰"), ("cohere", "Cohere"), ("perplexity", "Perplexity"),
]

_MEM = {"ts": 0.0, "payload": None}


def _group_of(mid: str) -> str:
    head = (mid or "").split("/")[0].lower()
    for key, label in GROUPS:
        if head == key:
            return label
    return head.capitalize() if head else "其他"


def _f(v, digits=3):
    """价格字符串 → 每百万 token 的数字（OpenRouter 单价是 per-token）。"""
    try:
        n = float(v)
    except (TypeError, ValueError):
        return 0.0
    return round(n * 1_000_000, digits)


def _slim(m: dict) -> dict:
    mid = m.get("id") or m.get("canonical_slug") or ""
    pricing = m.get("pricing") or {}
    params = m.get("supported_parameters") or []
    price_in = pricing.get("prompt")
    price_out = pricing.get("completion")
    pin = _f(price_in, 3)
    pout = _f(price_out, 3) if price_out not in (None, "") else 0.0
    # OpenRouter 用 -1 表示"随路由动态定价"，直接显示会变成 $-1000000
    dyn = pin < 0 or pout < 0
    return {
        "id": mid,
        "name": m.get("name") or mid,
        "group": _group_of(mid),
        "ctx": int(m.get("context_length") or m.get("top_provider", {}).get("context_length") or 0),
        "pin": max(pin, 0.0),   # 每百万输入 token 价格（USD）
        "pout": max(pout, 0.0),
        "dyn": dyn,             # 动态定价（如 openrouter/auto）
        "free": (pin == 0.0 and pout == 0.0) and not dyn,
        "tools": ("tools" in params) or bool(m.get("supports_tool_calls")),
        "mod": (m.get("architecture") or {}).get("input_modalities") or [],
    }


def _fetch(base_url: str, api_key: str, timeout=25):
    url = base_url.rstrip("/") + "/models"
    headers = {"Authorization": f"Bearer {api_key}"}
    r = httpx.get(url, headers=headers, timeout=timeout)
    if r.status_code >= 400:
        raise RuntimeError(f"模型列表请求失败 HTTP {r.status_code}: {r.text[:200]}")
    data = r.json() or {}
    raw = data.get("data") or data.get("models") or []
    return [_slim(m) for m in raw if isinstance(m, dict) and (m.get("id") or m.get("name"))]


def list_models(force=False, q="", only_free=False, only_tools=False, top=None):
    """返回精简后的模型目录。结构：{ok, source, total, items, groups, cached_at, error}"""
    cfg = db.all_settings()
    base = (cfg.get("llm_base_url") or DEFAULT_BASE_URL).strip()
    key = (cfg.get("llm_api_key") or "").strip()

    payload = _MEM.get("payload")
    fresh = payload and (time.time() - _MEM.get("ts", 0) < CACHE_TTL)
    if (force or not fresh) and payload is None:
        # 内存无缓存时先看 DB（服务重启后可复用）
        cached = db.get_setting(CACHE_KEY)
        if cached:
            try:
                import json
                payload = json.loads(cached)
                _MEM["payload"], _MEM["ts"] = payload, time.time()
            except Exception:
                payload = None

    if force or not payload or (time.time() - _MEM.get("ts", 0) >= CACHE_TTL):
        if not key:
            return {"ok": False, "source": "preset", "total": len(PREFERRED),
                    "items": PREFERRED, "groups": sorted({m["group"] for m in PREFERRED}),
                    "cached_at": 0, "error": "尚未配置 API Key，当前显示内置推荐模型"}
        try:
            items = _fetch(base, key)
            if not items:
                raise RuntimeError("接口返回空模型列表")
            # 排序：真实定价优先于动态定价 → 支持工具优先 → 价格升序 → id
            items.sort(key=lambda x: (bool(x.get("dyn")), not x["tools"], x["pin"] or 0, x["id"]))
            payload = {"source": "live", "total": len(items), "items": items,
                       "groups": sorted({m["group"] for m in items}), "cached_at": time.time()}
            _MEM["payload"], _MEM["ts"] = payload, time.time()
            db.set_settings({CACHE_KEY: __import__("json").dumps(payload, ensure_ascii=False)})
        except Exception as e:
            if payload:
                payload = dict(payload, stale=True, error=f"刷新失败，使用缓存：{str(e)[:160]}")
            else:
                return {"ok": False, "source": "preset", "total": len(PREFERRED),
                        "items": PREFERRED, "groups": sorted({m["group"] for m in PREFERRED}),
                        "cached_at": 0, "error": f"拉取失败：{str(e)[:200]}"}

    items = list(payload.get("items") or [])
    kw = (q or "").strip().lower()
    if kw:
        items = [m for m in items
                 if kw in m["id"].lower() or kw in (m["name"] or "").lower()
                 or kw in (m["group"] or "").lower()]
    if only_free:
        items = [m for m in items if m.get("free")]
    if only_tools:
        items = [m for m in items if m.get("tools")]
    if top:
        items = items[:int(top)]

    return {"ok": True, "source": payload.get("source", "live"),
            "total": payload.get("total", len(items)), "returned": len(items),
            "items": items, "groups": sorted({m["group"] for m in items}),
            "cached_at": payload.get("cached_at", 0),
            "stale": bool(payload.get("stale")), "error": payload.get("error"),
            "configured": bool(key)}


# 这些值语义上都是"跟随全局默认"，绝不能当真实模型 ID 发给供应商
PLACEHOLDER = {"", "default", "auto", "none", "null", "global", "follow", "inherit", "默认"}


def is_placeholder(m):
    return not m or str(m).strip().lower() in PLACEHOLDER


def resolve(model=None, strong=False):
    """取实际使用的模型：显式指定 > 强模型配置 > 默认配置 > 常量默认。

    占位值（default / auto / 空）一律视为"未指定"，否则会拿着
    model="default" 去请求供应商，返回 400 invalid model ID。
    """
    if not is_placeholder(model):
        return model
    cfg = db.all_settings()
    if strong and not is_placeholder(cfg.get("llm_model_strong")):
        return cfg["llm_model_strong"]
    if not is_placeholder(cfg.get("llm_model")):
        return cfg["llm_model"]
    return "openai/gpt-4o-mini"
