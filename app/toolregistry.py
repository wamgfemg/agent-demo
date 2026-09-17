# -*- coding: utf-8 -*-
"""工具注册中心。

平台内一切可被智能体调用的能力都在这里登记，来源有三类：
  1. builtin —— 平台内置（docx/pptx/沙箱/知识检索/文件写入）
  2. mcp     —— 通过 MCP 协议接入的外部 server（启动时自动拉取 tools/list 注册）
  3. http    —— 简单 HTTP 工具（预留）

智能体 YAML 里的 `tools: [name...]` 按名字引用此处登记的工具；
名字不存在会在注册智能体时告警，避免"配了却不生效"。
"""
import os, json, time, asyncio, traceback
from . import db, tools as builtin
from .config import ARTIFACT_DIR, KNOWLEDGE_DIR

_REG = {}  # name -> spec


# ------------------------------------------------------------------ 内置工具
def _t_docx(args):
    name = args.get("project_name") or "技术方案"
    chapters = args.get("chapters") or []
    if not chapters:
        return {"ok": False, "error": "chapters 为空"}
    fname = f"{name}-{time.strftime('%Y%m%d-%H%M%S')}.docx"
    path = os.path.join(ARTIFACT_DIR, fname)
    builtin.write_docx(name, chapters, path, args.get("glossary"), args.get("numbers"),
                       args.get("subtitle") or "自动生成 · AgentForge")
    return {"ok": True, "path": path, "file": fname,
            "chars": sum(len(c.get("content", "")) for c in chapters)}


def _t_pptx(args):
    name = args.get("project_name") or "方案汇报"
    slides = args.get("slides") or []
    if not slides:
        return {"ok": False, "error": "slides 为空"}
    fname = f"{name}-汇报PPT-{time.strftime('%Y%m%d-%H%M%S')}.pptx"
    path = os.path.join(ARTIFACT_DIR, fname)
    builtin.write_pptx(name, slides, path)
    return {"ok": True, "path": path, "file": fname, "slides": len(slides)}


def _t_python(args):
    return builtin.run_python(args.get("code") or "")


def _t_kb(args):
    return builtin.kb_search(args.get("query") or "", args.get("top_k") or 3)


def _t_file(args):
    name = args.get("name") or f"output-{time.strftime('%H%M%S')}.md"
    content = args.get("content") or ""
    path = os.path.join(ARTIFACT_DIR, name)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return {"ok": True, "path": path, "file": name, "size": len(content)}


def _t_fetch(args):
    import httpx
    url = args.get("url") or ""
    if not url.startswith(("http://", "https://")):
        return {"ok": False, "error": "仅允许 http/https"}
    try:
        r = httpx.get(url, timeout=15, follow_redirects=True)
        txt = r.text[:6000]
        return {"ok": True, "status": r.status_code, "length": len(r.text), "text": txt}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


BUILTIN = [
    ("docx_write", "生成可编辑 Word 文档（原生 docx 对象，非图片）",
     {"type": "object", "properties": {
         "project_name": {"type": "string", "description": "文档标题"},
         "chapters": {"type": "array", "description": "章节列表 [{title, content}]，content 为 Markdown"},
         "glossary": {"type": "array", "description": "术语表 [{term, definition}]"},
         "numbers": {"type": "array", "description": "数字台账 [{key, value, unit, source}]"}},
      "required": ["project_name", "chapters"]}, _t_docx),
    ("pptx_write", "生成可编辑 PPT（每页标题/要点/备注均为独立文本框）",
     {"type": "object", "properties": {
         "project_name": {"type": "string"},
         "slides": {"type": "array", "description": "[{title, bullets[], notes, subtitle}]"}},
      "required": ["project_name", "slides"]}, _t_pptx),
    ("python_sandbox", "在受限沙箱执行 Python 计算（禁文件/网络/进程）",
     {"type": "object", "properties": {"code": {"type": "string", "description": "仅标准库"}},
      "required": ["code"]}, _t_python),
    ("kb_search", "检索本地知识库（knowledge/ 目录下的 md/txt）",
     {"type": "object", "properties": {
         "query": {"type": "string"}, "top_k": {"type": "integer", "default": 3}},
      "required": ["query"]}, _t_kb),
    ("file_write", "把文本内容落为产物文件",
     {"type": "object", "properties": {"name": {"type": "string"}, "content": {"type": "string"}},
      "required": ["name", "content"]}, _t_file),
    ("http_fetch", "抓取网页正文（限 6000 字符）",
     {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}, _t_fetch),
]


# ------------------------------------------------------------------ 注册表
def register(name, description, parameters, handler, source="builtin", server=None):
    _REG[name] = {"name": name, "description": description, "parameters": parameters or {},
                  "handler": handler, "source": source, "server": server,
                  "registered_at": time.time()}


def register_builtin():
    for n, d, p, h in BUILTIN:
        register(n, d, p, h, source="builtin")


def unregister_source(server):
    for n in [k for k, v in _REG.items() if v.get("server") == server]:
        _REG.pop(n, None)


def get(name):
    return _REG.get(name)


def list_tools(source=None):
    items = list(_REG.values())
    if source:
        items = [i for i in items if i["source"] == source]
    return [{"name": i["name"], "description": i["description"],
             "parameters": i["parameters"], "source": i["source"],
             "server": i["server"]} for i in sorted(items, key=lambda x: (x["source"], x["name"]))]


def names():
    return set(_REG.keys())


async def call(name, args=None, timeout=60):
    """统一调用入口。同步 handler 走线程池，异步 handler 直接 await。"""
    spec = _REG.get(name)
    if not spec:
        return {"ok": False, "error": f"工具未注册：{name}"}
    args = args or {}
    t0 = time.time()
    try:
        h = spec["handler"]
        if asyncio.iscoroutinefunction(h):
            out = await asyncio.wait_for(h(args), timeout=timeout)
        else:
            out = await asyncio.wait_for(asyncio.to_thread(h, args), timeout=timeout)
        if not isinstance(out, dict):
            out = {"ok": True, "result": out}
        out.setdefault("ok", True)
        out["_ms"] = int((time.time() - t0) * 1000)
        return out
    except asyncio.TimeoutError:
        return {"ok": False, "error": f"工具超时（>{timeout}s）", "_ms": int((time.time() - t0) * 1000)}
    except Exception as e:
        traceback.print_exc()
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "_ms": int((time.time() - t0) * 1000)}


def prompt_block(names_):
    """生成给模型看的工具清单文本。"""
    lines = []
    for n in names_:
        s = _REG.get(n)
        if not s:
            continue
        props = (s["parameters"] or {}).get("properties") or {}
        sig = ", ".join(props.keys()) or "-"
        lines.append(f"- {n}({sig})：{s['description']}")
    return "\n".join(lines)
