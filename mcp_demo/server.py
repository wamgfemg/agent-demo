# -*- coding: utf-8 -*-
"""AgentForge 内置演示 MCP Server（Streamable HTTP，仅监听 127.0.0.1:8099）。

作用有二：
  1. 验证平台 MCP 客户端的 initialize / tools/list / tools/call 全链路确实可用；
  2. 作为编写自有 MCP Server 的最小范例——照着改就能接自己的业务系统。
"""
import os, json, re, time
from fastapi import FastAPI, Request, Response

app = FastAPI()
SESSION = "agentforge-demo-session"
KB = os.environ.get("AF_KB_DIR", "/opt/agentforge/data/knowledge")

TOOLS = [
    {"name": "search_knowledge",
     "description": "检索内置知识库（AIOps 能力说明 / 售前交付经验），返回相关片段",
     "inputSchema": {"type": "object",
                     "properties": {"query": {"type": "string", "description": "检索关键词"},
                                    "top_k": {"type": "integer", "default": 3}},
                     "required": ["query"]}},
    {"name": "calc_sla",
     "description": "按可用性百分比计算年度允许不可用时长（分钟）",
     "inputSchema": {"type": "object",
                     "properties": {"availability": {"type": "number", "description": "如 99.9 表示 99.9%"}},
                     "required": ["availability"]}},
    {"name": "list_capabilities",
     "description": "列出平台已沉淀的标准能力清单",
     "inputSchema": {"type": "object", "properties": {}}},
]

CAPS = ["统一监控", "智能告警降噪", "根因定位", "容量分析", "自动化处置",
        "CMDB 与配置管理", "工单与流程联动", "可视化大屏"]


# 中文用二字滑窗（bigram）代替分词：无第三方依赖，也能命中"可用性"这类概念
_STOP = {"的", "了", "是", "在", "与", "和", "及", "对", "为", "以", "并", "等", "中", "上", "下",
         "我们", "可以", "进行", "一个", "什么", "如何", "哪些", "这个", "那个", "以及", "通过"}


def _terms(query):
    q = (query or "").lower()
    terms = set()
    for w in re.findall(r"[\u4e00-\u9fa5]+", q):
        if len(w) == 1:
            terms.add(w)
        else:
            for i in range(len(w) - 1):
                t = w[i:i + 2]
                if t not in _STOP:
                    terms.add(t)
    terms |= set(re.findall(r"[a-z]{3,}", q))
    return terms


def _search(query, top_k=3):
    if not os.path.isdir(KB):
        return {"ok": False, "error": f"知识目录不存在：{KB}", "hits": []}
    qs = _terms(query)
    if not qs:
        return {"ok": False, "error": "查询为空", "hits": []}
    hits = []
    for fn in sorted(os.listdir(KB)):
        if not fn.endswith((".md", ".txt")):
            continue
        text = open(os.path.join(KB, fn), "r", encoding="utf-8", errors="replace").read()
        low = text.lower()
        score = sum(low.count(t) for t in qs)
        if score <= 0:
            continue
        pos = min((low.find(t) for t in qs if low.find(t) >= 0), default=0)
        hits.append({"file": fn, "score": score,
                     "snippet": ("..." if pos > 100 else "") +
                                text[max(0, pos - 100): pos + 500].strip() + "..."})
    hits.sort(key=lambda x: -x["score"])
    return {"ok": True, "query": query, "total": len(hits), "hits": hits[:max(1, int(top_k or 3))]}


def _call(name, args):
    if name == "search_knowledge":
        return _search(args.get("query") or "", args.get("top_k") or 3)
    if name == "calc_sla":
        a = float(args.get("availability") or 99.9)
        minutes = (100 - a) / 100 * 365 * 24 * 60
        return {"ok": True, "availability": a,
                "annual_down_minutes": round(minutes, 1),
                "annual_down_hours": round(minutes / 60, 2)}
    if name == "list_capabilities":
        return {"ok": True, "capabilities": CAPS, "count": len(CAPS)}
    return {"ok": False, "error": f"未知工具：{name}"}


@app.post("/mcp")
async def endpoint(req: Request):
    body = await req.json()
    method, _id = body.get("method"), body.get("id")
    if method == "initialize":
        result = {"protocolVersion": "2025-06-18",
                  "capabilities": {"tools": {"listChanged": False}},
                  "serverInfo": {"name": "agentforge-demo", "version": "1.0.0"}}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        p = body.get("params") or {}
        out = _call(p.get("name"), p.get("arguments") or {})
        result = {"content": [{"type": "text", "text": json.dumps(out, ensure_ascii=False)}],
                  "isError": not out.get("ok", True)}
    elif method in ("notifications/initialized", "notifications/cancelled"):
        return Response(status_code=202, headers={"Mcp-Session-Id": SESSION})
    elif method == "ping":
        result = {}
    else:
        return Response(content=json.dumps({"jsonrpc": "2.0", "id": _id,
                                            "error": {"code": -32601, "message": f"不支持的方法：{method}"}},
                                           ensure_ascii=False),
                        media_type="application/json", headers={"Mcp-Session-Id": SESSION})
    payload = {"jsonrpc": "2.0", "id": _id, "result": result}
    return Response(content=json.dumps(payload, ensure_ascii=False),
                    media_type="application/json", headers={"Mcp-Session-Id": SESSION})


@app.get("/health")
def health():
    return {"ok": True, "tools": [t["name"] for t in TOOLS], "ts": time.strftime("%H:%M:%S")}
