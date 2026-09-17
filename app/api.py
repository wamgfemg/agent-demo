# -*- coding: utf-8 -*-



"""AgentForge API 层：FastAPI 单进程，SQLite 兼作队列，asyncio 并发执行。"""



import os, json, time, asyncio, re, gzip



from fastapi import FastAPI, HTTPException, Request



from urllib.parse import unquote



from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse



from pydantic import BaseModel



from typing import Optional, Dict, Any, List







from . import (db, registry, pipeline, llm, toolregistry, mcp, runtime, models, engines,



               skills as skilllib, ptpl, widgetlib, kblib, memlib, wflib)



from .config import WEB_DIR, ARTIFACT_DIR, AGENT_DIR







app = FastAPI(title="AgentForge", version="2.0.0")



# ================================================================== 鉴权网关（P1-1）

# 设置环境变量 AGENTFORGE_API_TOKEN 后，除 /api/health 外的所有 /api/* 必须携带

# `Authorization: Bearer <token>`，否则返回 401。未设置该变量时网关自动关闭，向后兼容。

API_TOKEN = os.environ.get("AGENTFORGE_API_TOKEN")

_AUTH_SKIP = {"/api/health"}





@app.middleware("http")

async def _auth_gate(request, call_next):

    if not API_TOKEN:

        return await call_next(request)

    path = request.url.path

    if not path.startswith("/api/") or path in _AUTH_SKIP:

        return await call_next(request)

    if request.headers.get("Authorization") == "Bearer " + API_TOKEN:

        return await call_next(request)

    return JSONResponse({"ok": False, "error": "未授权：缺少或错误的 API Token"},

                        status_code=401)















@app.on_event("startup")



async def _startup():



    db.init_db()
    ptpl.ensure_tables()
    widgetlib.ensure_tables()
    kblib.ensure_tables()
    memlib.ensure_tables()
    wflib.ensure_tables()



    toolregistry.register_builtin()          # 先注册内置工具



    await mcp.connect_all()                  # 再接入 MCP server（其工具进入同一注册中心）



    registry.load_agents()                   # 最后加载智能体，可校验工具/技能引用是否命中



    pipeline.resume_interrupted()



    asyncio.create_task(pipeline.scheduler())



    print("[agentforge] started, agents:", len(registry.list_agents()),



          "tools:", len(toolregistry.list_tools()))











_INDEX_CACHE = {"etag": "", "html": "", "gz": b""}


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    """首页：按 mtime 内存缓存 + ETag/304 协商缓存 + gzip 传输。
    Cache-Control: no-cache 保证浏览器每次都来服务器校验，前端更新仍即时生效；
    内容未变时返回 304（零正文传输），变化时下发 gzip 页面（132KB → 约25KB）。"""
    p = os.path.join(WEB_DIR, "index.html")
    try:
        st = os.stat(p)
    except OSError:
        return HTMLResponse("<h3>前端未部署</h3>")
    etag = f'W/"af-{st.st_mtime_ns:x}-{st.st_size:x}"'
    base_headers = {"ETag": etag, "Cache-Control": "no-cache"}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=base_headers)
    if _INDEX_CACHE["etag"] != etag:
        html = open(p, "r", encoding="utf-8").read()
        _INDEX_CACHE.update(etag=etag, html=html,
                            gz=gzip.compress(html.encode("utf-8"), 6))
    if "gzip" in (request.headers.get("accept-encoding") or "").lower():
        return Response(content=_INDEX_CACHE["gz"], media_type="text/html; charset=utf-8",
                        headers={**base_headers, "Content-Encoding": "gzip",
                                 "Vary": "Accept-Encoding"})
    return HTMLResponse(_INDEX_CACHE["html"],
                        headers={**base_headers, "Vary": "Accept-Encoding"})


FAVICON_SVG = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
               '<rect width="64" height="64" rx="14" fill="#10b981"/>'
               '<text x="32" y="44" font-size="30" font-weight="bold" text-anchor="middle" '
               'fill="#0e1512" font-family="system-ui,sans-serif">AF</text></svg>')


@app.get("/favicon.ico")
def favicon():
    return Response(FAVICON_SVG, media_type="image/svg+xml",
                    headers={"Cache-Control": "public, max-age=604800"})











@app.get("/api/health")



def health():



    cfg = db.all_settings()



    return {"ok": True, "mocked": llm.is_mocked(), "time": time.strftime("%Y-%m-%d %H:%M:%S"),



            "model": models.resolve(), "configured": bool((cfg.get("llm_api_key") or "").strip()),



            "strong": cfg.get("llm_model_strong") or ""}











# ================================================================== 智能体



@app.get("/api/catalog")



def catalog():



    """构建器用到的下拉数据：执行引擎、输出类型、技能、工具。"""



    return {"runtimes": engines.runtime_list(), "outputs": runtime.OUTPUTS, "runtime_details": engines.details_map(),



            "skills": skilllib.list_skills(), "tools": toolregistry.list_tools()}











@app.get("/api/agents")



def agents(scope: str = "all"):



    return {"items": registry.list_agents(scope), "mocked": llm.is_mocked()}











class AgentIn(BaseModel):



    id: Optional[str] = None



    name: Optional[str] = None



    icon: Optional[str] = None



    category: Optional[str] = None



    description: Optional[str] = None



    persona: Optional[str] = None



    skills: Optional[List[str]] = None



    tools: Optional[List[str]] = None



    runtime: Optional[str] = None



    output: Optional[str] = None



    input_schema: Optional[List[Dict[str, Any]]] = None



    params: Optional[Dict[str, Any]] = None



    model: Optional[str] = None



    temperature: Optional[float] = None











@app.post("/api/agents")



def agent_create(body: AgentIn):



    try:



        return registry.create({k: v for k, v in body.dict().items() if v is not None})



    except ValueError as e:



        raise HTTPException(400, str(e))











@app.post("/api/agents/reload")



def reload_agents():



    return {"items": registry.load_agents(force=True)}











@app.post("/api/agents/{aid}/clone")



def agent_clone(aid: str):



    try:



        return registry.clone(aid)



    except ValueError as e:



        raise HTTPException(400, str(e))











@app.post("/api/agents/{aid}/publish")



def agent_publish(aid: str, body: Dict[str, Any] = None):



    try:



        return registry.publish(aid, (body or {}).get("note", ""))



    except ValueError as e:



        raise HTTPException(400, str(e))











@app.post("/api/agents/{aid}/unpublish")



def agent_unpublish(aid: str):



    return registry.unpublish(aid)











@app.post("/api/agents/{aid}/archive")



def agent_archive(aid: str):



    registry.archive(aid)



    return {"ok": True}











@app.post("/api/agents/{aid}/unarchive")

def agent_unarchive(aid: str):

    registry.unarchive(aid)

    return {"ok": True}





@app.post("/api/agents/{aid}/enable")



def agent_enable(aid: str, body: Dict[str, Any] = None):



    registry.enable(aid, bool((body or {}).get("on", True)))



    return {"ok": True}











@app.get("/api/agents/{aid}/versions")



def agent_versions(aid: str):



    return {"items": db.agent_versions(aid)}











class TestIn(BaseModel):



    input: Dict[str, Any] = {}



    params: Dict[str, Any] = {}











@app.post("/api/agents/{aid}/test")



async def agent_test(aid: str, body: TestIn):



    """试运行：同步执行一遍，直接把结果返回给构建者，不进任务中心。"""



    spec = registry.get_agent(aid)



    if not spec:



        raise HTTPException(404, "智能体不存在")



    tid = pipeline.create_task(aid, "试运行 · " + (spec.get("name") or aid), body.input, body.params)



    t0 = time.time()



    await pipeline.execute_task(tid)



    row = db.query_one("SELECT status,error,input_json FROM tasks WHERE id=?", (tid,))



    data = db.uj(row["input_json"]) or {}



    arts = db.query("SELECT id,name,size FROM artifacts WHERE task_id=? ORDER BY created_at", (tid,))



    return {"task_id": tid, "status": row["status"], "error": row["error"],



            "answer": data.get("_answer") or "", "tool_calls": data.get("_trace") or [],



            "artifacts": arts, "seconds": round(time.time() - t0, 2)}











@app.get("/api/agents/{aid}")



def agent_detail(aid: str):



    d = registry.detail(aid)



    if not d:



        raise HTTPException(404, "智能体不存在")



    return d











@app.put("/api/agents/{aid}")



def agent_update(aid: str, body: AgentIn):



    try:



        return registry.update(aid, {k: v for k, v in body.dict().items() if v is not None})



    except ValueError as e:



        raise HTTPException(400, str(e))











@app.delete("/api/agents/{aid}")



def agent_delete(aid: str):



    try:



        registry.delete(aid)



        return {"ok": True}



    except ValueError as e:



        raise HTTPException(400, str(e))











@app.post("/api/agents/{aid}/rollback/{ver}")

def agent_rollback(aid: str, ver: int):

    try:

        return registry.rollback(aid, ver)

    except ValueError as e:

        raise HTTPException(400, str(e))





@app.get("/api/agents/{aid}/prompt")



def agent_prompt(aid: str):



    """预览该智能体最终生效的 system prompt（人设 + 技能正文 + 工具清单）。"""



    spec = registry.get_agent(aid)



    if not spec:



        raise HTTPException(404, "智能体不存在")



    base = spec.get("persona") or pipeline.DOC_SYS



    return {"agent_id": aid, "runtime": spec.get("runtime"),



            "skills": spec.get("skills") or [], "tools": spec.get("tools") or [],



            "system": pipeline.build_system(spec, base)}











# ================================================================== 工具中心



@app.get("/api/tools")



def list_tools(source: str = None):



    return {"items": toolregistry.list_tools(source), "total": len(toolregistry.list_tools())}











class ToolTestIn(BaseModel):



    name: str



    args: Dict[str, Any] = {}











@app.post("/api/tools/test")



async def test_tool(body: ToolTestIn):



    r = await toolregistry.call(body.name, body.args, timeout=30)



    db.log_tool_call(None, body.name, (toolregistry.get(body.name) or {}).get("source", "?"),



                     body.args, bool(r.get("ok")), r.get("_ms", 0))



    return r











# ================================================================== MCP



@app.get("/api/mcp/servers")



def mcp_list():



    return {"items": mcp.list_servers()}











class MCPIn(BaseModel):



    id: Optional[str] = None



    name: str



    transport: str = "http"



    url: Optional[str] = None



    command: Optional[str] = None



    args: Optional[list] = None



    env: Optional[Dict[str, Any]] = None



    headers: Optional[Dict[str, Any]] = None



    enabled: bool = True











@app.post("/api/mcp/servers")



async def mcp_save(body: MCPIn):



    sid = mcp.save_server(body.dict())



    if body.enabled:



        res = await mcp.connect_server(sid)



        registry.load_agents()



        return {"ok": True, "id": sid, "connect": res}



    return {"ok": True, "id": sid, "connect": None}











@app.post("/api/mcp/servers/test")



async def mcp_test(body: MCPIn):



    return await mcp.test_server(body.dict())











@app.post("/api/mcp/servers/{sid}/connect")



async def mcp_connect(sid: str):



    res = await mcp.connect_server(sid)



    registry.load_agents()



    return res











@app.delete("/api/mcp/servers/{sid}")



def mcp_delete(sid: str):



    mcp.delete_server(sid)



    registry.load_agents()



    return {"ok": True}











# ================================================================== 技能



@app.get("/api/engines")
def engine_list():
    """执行引擎列表（含版本号与启停状态）。"""
    return {"items": engines.list_engines()}


@app.get("/api/engines/{eid}/versions")
def engine_versions_ep(eid: str):
    """引擎的版本历史。"""
    if not engines.get_engine(eid):
        raise HTTPException(404, "引擎不存在")
    return {"items": engines.versions(eid)}


@app.get("/api/engines/{eid}")
def engine_get(eid: str):
    e = engines.get_engine(eid)
    if not e:
        raise HTTPException(404, "引擎不存在")
    e["versions"] = engines.versions(eid)
    return e


class EngineIn(BaseModel):
    name: Optional[str] = None
    desc: Optional[str] = None
    detail: Optional[dict] = None
    params: Optional[dict] = None
    enabled: Optional[bool] = None
    note: Optional[str] = None


@app.post("/api/engines/{eid}")
def engine_save(eid: str, body: EngineIn):
    """保存引擎定义为新版本（版本号 +1）。"""
    if not engines.get_engine(eid):
        raise HTTPException(404, "引擎不存在")
    patch = {"name": body.name, "desc": body.desc, "detail": body.detail,
             "params": body.params, "enabled": body.enabled}
    out = engines.save_engine(eid, patch, body.note or "")
    return {"ok": True, "engine": out}


class EngineRollbackIn(BaseModel):
    version: int
    note: Optional[str] = None


@app.post("/api/engines/{eid}/rollback")
def engine_rollback(eid: str, body: EngineRollbackIn):
    """以历史版本内容创建新版本。"""
    out = engines.rollback(eid, body.version)
    if not out:
        raise HTTPException(404, "版本不存在或引擎不存在")
    return {"ok": True, "engine": out}


@app.get("/api/skills")



def skill_list():



    return {"items": skilllib.list_skills()}











@app.get("/api/skills/{sid}")



def skill_get(sid: str):



    s = skilllib.get_skill(sid)



    if not s:



        raise HTTPException(404, "技能不存在")



    return s











class SkillIn(BaseModel):



    id: str



    name: Optional[str] = None



    description: Optional[str] = None



    body: str = ""



    version: Optional[str] = "1.0"











@app.post("/api/skills")



def skill_save(body: SkillIn):



    import re



    if not re.match(r"^[A-Za-z0-9_\-]+$", body.id or ""):



        raise HTTPException(400, "技能 id 只能包含字母数字下划线与短横线")



    r = skilllib.save_skill(body.id, body.name, body.description, body.body, body.version)



    registry.load_agents()



    return {"ok": True, **r}











@app.delete("/api/skills/{sid}")



def skill_delete(sid: str):



    ok = skilllib.delete_skill(sid)



    registry.load_agents()



    return {"ok": ok}











# ================================================================== 任务



class TaskIn(BaseModel):



    agent_id: str



    title: Optional[str] = None



    input: Dict[str, Any] = {}



    params: Dict[str, Any] = {}



    auto_run: bool = True



    published_only: bool = False











@app.get("/api/tasks")



def tasks(limit: int = 30, agent_id: str = None):



    sql = ("SELECT id,agent_id,title,status,progress,stage,created_at,updated_at,error,"



           "tokens_in,tokens_out FROM tasks")



    args = []



    if agent_id:



        sql += " WHERE agent_id=?"



        args.append(agent_id)



    sql += " ORDER BY created_at DESC LIMIT ?"



    args.append(limit)



    return {"items": db.query(sql, args)}











@app.post("/api/tasks")



async def create_task(body: TaskIn, request: Request):



    row = db.query_one("SELECT status,name,enabled FROM agents WHERE id=?", (body.agent_id,))



    if not row:



        raise HTTPException(404, "智能体不存在")



    if not row.get("enabled"):
        raise HTTPException(409, "智能体已停用，请在构建器中重新启用后再使用")

    # 业务人员入口强制只跑已发布版本，避免构建中的草稿被误用



    if body.published_only and row["status"] != "published":



        raise HTTPException(403, "该智能体尚未发布，请先在构建器中发布")



    title = body.title or f"{row['name'] or body.agent_id} - {time.strftime('%m-%d %H:%M')}"



    tid = pipeline.create_task(body.agent_id, title, body.input, body.params)
    _log_prompt(request, "task", tid, body.agent_id, _task_input_text(body.input))



    if body.auto_run:



        try:



            pipeline.start_task(tid)



        except RuntimeError:



            pass



    return {"id": tid}











@app.get("/api/tasks/{tid}")



def task_detail(tid: str):



    t = db.query_one("SELECT * FROM tasks WHERE id=?", (tid,))



    if not t:



        raise HTTPException(404, "任务不存在")



    units = db.query("SELECT id,unit_key,seq,title,stage,status,attempt,review_json,"



                     "tokens_in,tokens_out,error FROM task_units WHERE task_id=? ORDER BY seq", (tid,))



    arts = db.query("SELECT id,name,size,mime,created_at FROM artifacts WHERE task_id=? ORDER BY created_at", (tid,))



    gl = db.query("SELECT term,definition FROM glossary WHERE task_id=?", (tid,))



    nb = db.query("SELECT key,value,unit,source FROM numbers WHERE task_id=?", (tid,))



    calls = db.query("SELECT tool,source,ok,ms,created_at,args_json FROM tool_calls "



                     "WHERE task_id=? ORDER BY id DESC LIMIT 50", (tid,))



    return {"task": t, "units": units, "artifacts": arts, "glossary": gl, "numbers": nb,



            "tool_calls": calls, "answer": (db.uj(t["input_json"]) or {}).get("_answer") or ""}











@app.post("/api/tasks/{tid}/run")



async def run_task(tid: str):



    t = db.query_one("SELECT status FROM tasks WHERE id=?", (tid,))



    if not t:



        raise HTTPException(404, "任务不存在")



    if t["status"] in ("running",):



        return {"started": False, "reason": "already running"}



    db.update_task(tid, status="queued", error=None)



    ok = pipeline.start_task(tid)



    return {"started": ok}











@app.post("/api/tasks/{tid}/cancel")



def cancel_task(tid: str):



    db.update_task(tid, status="canceled", stage="已取消")



    return {"ok": True}











@app.post("/api/tasks/{tid}/retry")



async def retry_task(tid: str):



    """只重跑失败或未完成的单元，已完成的章节直接复用。"""
    row = db.query_one("SELECT status FROM tasks WHERE id=?", (tid,))
    if not row:
        raise HTTPException(404, "任务不存在")
    st = row["status"]
    if st == "running":
        raise HTTPException(409, "任务正在运行，请先取消再续跑")
    if st == "done":
        raise HTTPException(409, "任务已完成，无需续跑")




    db.execute("UPDATE task_units SET status='pending',attempt=0,error=NULL "



               "WHERE task_id=? AND status IN ('failed','review_failed','pending','running')", (tid,))



    db.update_task(tid, status="queued", error=None, progress=0)



    ok = pipeline.start_task(tid)



    return {"started": ok}











@app.delete("/api/tasks/{tid}")



def delete_task(tid: str):



    db.execute("DELETE FROM task_units WHERE task_id=?", (tid,))



    db.execute("DELETE FROM tool_calls WHERE task_id=?", (tid,))



    db.execute("DELETE FROM glossary WHERE task_id=?", (tid,))



    db.execute("DELETE FROM numbers WHERE task_id=?", (tid,))



    db.execute("DELETE FROM artifacts WHERE task_id=?", (tid,))



    db.execute("DELETE FROM tasks WHERE id=?", (tid,))



    return {"ok": True}











@app.get("/api/artifacts/{aid}/download")



def download(aid: str):



    a = db.query_one("SELECT * FROM artifacts WHERE id=?", (aid,))



    if not a or not os.path.exists(a["path"]):



        raise HTTPException(404, "产物不存在")



    return FileResponse(a["path"], filename=a["name"], media_type=a["mime"])











# ================================================================== 设置



@app.get("/api/settings")



def get_settings():



    s = db.all_settings()



    masked = dict(s)



    if masked.get("llm_api_key"):



        k = masked["llm_api_key"]



        masked["llm_api_key"] = k[:6] + "..." + k[-4:] if len(k) > 12 else "***"



    return {"settings": masked, "mocked": llm.is_mocked()}











class SettingsIn(BaseModel):



    llm_base_url: Optional[str] = None



    llm_api_key: Optional[str] = None



    llm_model: Optional[str] = None



    llm_model_strong: Optional[str] = None











@app.post("/api/settings")



def set_settings(body: SettingsIn):



    d = {k: v for k, v in body.dict().items() if v is not None and v != ""}



    if d:



        db.set_settings(d)



    return {"ok": True, "mocked": llm.is_mocked()}











@app.post("/api/llm/test")



def test_llm():



    if llm.is_mocked():



        return {"ok": True, "mocked": True, "msg": "当前为模拟模式（未配置密钥）"}



    try:



        r = llm.chat("你是一个测试助手。", "回复 OK 两个字", hint="report")



        return {"ok": True, "mocked": False, "reply": r.content[:200], "model": r.model}



    except Exception as e:



        return {"ok": False, "error": str(e)[:300]}











class TestModelIn(BaseModel):



    model: Optional[str] = None











@app.post("/api/llm/test-model")



def test_model(body: TestModelIn = None):



    """用指定模型发一次真实请求，验证该模型可用（不落库）。"""



    if llm.is_mocked():



        return {"ok": False, "error": "尚未配置 API Key"}



    model = (body.model if body else None) or models.resolve()



    t0 = time.time()



    try:



        r = llm.chat("你是一个测试助手。", "回复 OK 两个字", model=model, hint="report")



        return {"ok": True, "model": r.model, "reply": r.content[:200],



                "ms": int((time.time() - t0) * 1000),



                "tokens": {"in": r.tokens_in, "out": r.tokens_out}}



    except Exception as e:



        return {"ok": False, "model": model, "error": str(e)[:400]}











@app.get("/api/llm/models")



async def llm_models(force: int = 0, q: str = "", free: int = 0,



                     tools: int = 0, top: int = 0):



    """模型目录。force=1 强制刷新；q 搜索；free/tools 过滤；top 截断。"""



    return await asyncio.to_thread(



        models.list_models, bool(force), q or "", bool(free), bool(tools), top or None)











@app.get("/api/stats")



def stats():



    t = db.query_one("SELECT COUNT(*) c FROM tasks")



    a = db.query_one("SELECT COUNT(*) c FROM artifacts")



    ag = db.query_one("SELECT COUNT(*) c FROM agents WHERE status='published'")



    tr = db.query_one("SELECT COALESCE(SUM(tokens_in),0) i, COALESCE(SUM(tokens_out),0) o FROM tasks")



    return {"tasks": t["c"], "artifacts": a["c"], "published_agents": ag["c"],



            "tokens_in": tr["i"], "tokens_out": tr["o"]}











# ================================================================== 对话（Chat）



class ChatMessageIn(BaseModel):



    conversation_id: Optional[str] = None



    agent_id: str = ""



    message: str



    system_prompt: Optional[str] = None

    stream: Optional[bool] = False











@app.post("/api/chat")



async def chat_endpoint(body: ChatMessageIn, request: Request):



    """多轮对话：自动创建或复用 conversation，携带历史发给 LLM，返回助手回复。"""



    import uuid



    cid = body.conversation_id



    if not cid:



        # 新建对话



        cid = db.create_conversation(body.agent_id, body.message[:40])



    



    # P2-1: 业务侧对话前校验智能体是否启用且存在
    if body.agent_id:
        try:
            registry.require_enabled(body.agent_id)
        except ValueError as e:
            raise HTTPException(409, str(e))

    # 保存用户消息



    db.add_chat_message(cid, "user", body.message)
    _plog_id = _log_prompt(request, "chat", cid, body.agent_id, body.message)



    



    # 构建历史消息列表



    conv_data = db.get_conversation(cid)



    history_msgs = []



    if conv_data:



        for m in conv_data.get("messages", []):



            if m["role"] in ("user", "assistant"):



                history_msgs.append({"role": m["role"], "content": m["content"]})



    



    # 获取智能体 system prompt（如果有）——统一使用发布快照，与任务执行语义一致（P1-3）



    agent_spec = None



    if body.agent_id:



        agent_spec = registry.get_published_or_draft(body.agent_id)



    # P2-5: 对话上下文截断（保留最近 20 轮，避免长对话撑爆 token/超时）
    MAX_TURNS = 20
    truncated = False
    if len(history_msgs) > MAX_TURNS * 2:
        history_msgs = history_msgs[-MAX_TURNS * 2:]
        truncated = True

    system = body.system_prompt or (agent_spec.get("persona") if agent_spec else "你是一个有帮助的AI助手。请用中文回答。")
    # Widget 协议注入：让智能体可以在回复中输出交互卡片
    system = (system or "") + "\n\n" + widgetlib.WIDGET_PROMPT

    # 知识库 RAG：智能体绑定的知识库 → 检索注入参考材料
    _kb_ids = (agent_spec or {}).get("knowledge_bases") or []
    _kb_hits = []
    if _kb_ids:
        try:
            _kb_hits = kblib.search(_kb_ids, body.message, 5)
            if _kb_hits:
                system += "\n\n" + kblib.format_context(_kb_hits)
        except Exception:
            pass
    # 长期记忆：召回注入
    _op_id, _op_nm = _operator(request)
    _mem_ctx = memlib.format_context(memlib.get_memory(_op_id, body.agent_id or ""))
    if _mem_ctx:
        system += "\n\n" + _mem_ctx

    # 记忆异步沉淀（守护线程，失败不影响对话）
    def _memory_update(assistant_text):
        try:
            import threading
            threading.Thread(
                target=memlib.summarize_update,
                args=(_op_id, body.agent_id or "", body.message, assistant_text,
                      lambda s, u: llm.chat(s, u, hint="memory")),
                daemon=True).start()
        except Exception:
            pass




    



    messages = [{"role": "system", "content": system}] + history_msgs


    # ---------------------- 流式分支（body.stream=true 时走 SSE，逐字下发） ----------------------
    if body.stream:
        def _sse(obj):
            return "data: " + json.dumps(obj, ensure_ascii=False) + "\n\n"

        def _sse_gen():
            yield _sse({"t": "meta", "conversation_id": cid, "truncated": truncated,
                        "prompt_log_id": _plog_id})
            parts = []
            try:
                if llm.is_mocked():
                    r = llm.chat(system, body.message, hint="chat", ctx={"brief": body.message})
                    parts.append(r.content)
                    yield _sse({"t": "delta", "v": r.content})
                    db.add_chat_message(cid, "assistant", r.content, r.model, r.mocked,
                                        r.tokens_in, r.tokens_out, r.latency_ms)
                    yield _sse({"t": "done", "model": r.model, "mocked": r.mocked,
                                "tokens_in": r.tokens_in, "tokens_out": r.tokens_out})
                    return
                final = None
                for ev in llm.stream_chat_messages(messages):
                    if ev.get("type") == "delta":
                        parts.append(ev["v"])
                        yield _sse({"t": "delta", "v": ev["v"]})
                    elif ev.get("type") == "done":
                        final = ev
                if final is None:
                    raise RuntimeError("流式响应未正常结束")
                db.add_chat_message(cid, "assistant", final["content"], final["model"], False,
                                    final["tokens_in"], final["tokens_out"], final["latency_ms"])
                yield _sse({"t": "done", "model": final["model"], "mocked": False,
                            "tokens_in": final["tokens_in"], "tokens_out": final["tokens_out"]})
                _memory_update(final["content"])
            except Exception as e:
                err_msg = str(e)[:500]
                db.add_chat_message(cid, "assistant", "".join(parts), error=err_msg)
                yield _sse({"t": "error", "error": err_msg})

        return StreamingResponse(_sse_gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


    # 调用 LLM（带历史）



    try:



        # 用 chat_messages 方式传入完整历史



        messages = [{"role": "system", "content": system}] + history_msgs



        



        if llm.is_mocked():



            r = await asyncio.to_thread(llm.chat, system, body.message, hint="chat", ctx={"brief": body.message})



        else:



            r = await asyncio.to_thread(llm.chat_messages, messages)



        



        # 保存助手回复



        db.add_chat_message(cid, "assistant", r.content, r.model, r.mocked,



                          r.tokens_in, r.tokens_out, r.latency_ms)



        



        _memory_update(r.content)
        return {



            "conversation_id": cid,



            "reply": r.content,



            "model": r.model,



            "mocked": r.mocked,



            "tokens_in": r.tokens_in,



            "tokens_out": r.tokens_out,
            "truncated": truncated,
            "prompt_log_id": _plog_id,
        }



    except Exception as e:



        err_msg = str(e)[:500]



        db.add_chat_message(cid, "assistant", "", error=err_msg)



        return {"conversation_id": cid, "error": err_msg, "reply": f"⚠️ 模型调用失败: {err_msg}"}











@app.get("/api/conversations")



def list_conversations_api(agent_id: str = None, limit: int = 30):



    return {"items": db.list_conversations(agent_id, limit)}











@app.get("/api/conversations/{cid}")



def get_conversation_api(cid: str):



    data = db.get_conversation(cid)



    if not data:



        raise HTTPException(404, "对话不存在")



    return data











@app.delete("/api/conversations/{cid}")



def delete_conversation_api(cid: str):



    db.delete_conversation(cid)



    return {"ok": True}











@app.post("/api/conversations")



def create_conversation_api(body: dict = None):



    body = body or {}



    cid = db.create_conversation(body.get("agent_id", ""), body.get("title", ""))



    return {"id": cid}











# ================================================================== 监控（Langfuse 风格可观测性）



# 各模型单价（USD / 1M tokens: input, output），用于成本估算；匹配不到用默认



MODEL_PRICES = {



    "openai/gpt-4o": (2.5, 10.0),



    "openai/gpt-4o-mini": (0.15, 0.6),



    "anthropic/claude-3.5-sonnet": (3.0, 15.0),



    "anthropic/claude-3-haiku": (0.25, 1.25),



    "google/gemini": (1.25, 5.0),



    "meta-llama/llama-3.1-70b": (0.35, 0.4),



    "deepseek/deepseek-chat": (0.14, 0.28),



    "dots-studio/dots-3-note-preview:free": (0.0, 0.0),
    "qwen/qwen2.5": (0.3, 0.9),
    "moonshot": (0.6, 0.6),
    "deepseek/deepseek-reasoner": (0.55, 2.19),
    "zhipu/glm": (0.5, 0.5),
    "minimax": (0.3, 1.0),



}



DEFAULT_PRICE = (0.5, 1.5)











def _pct(vals, p):
    """线性插值分位；无数据返回 0。"""
    if not vals:
        return 0.0
    s = sorted(vals)
    k = (len(s) - 1) * p
    f = int(k)
    c = min(f + 1, len(s) - 1)
    if f == c:
        return float(s[f])
    return s[f] + (s[c] - s[f]) * (k - f)


def _price_of(model):



    if not model:



        return DEFAULT_PRICE



    m = (model or "").lower()



    # P2-7: 环境变量覆盖单价（JSON: {"model_substr": [in_usd, out_usd]}）
    ov = os.environ.get("AGENTFORGE_MODEL_PRICES")
    if ov:
        try:
            custom = json.loads(ov)
            for k, v in custom.items():
                if k.lower() in m:
                    return (float(v[0]), float(v[1]))
        except Exception:
            pass
    if ":free" in m:



        return (0.0, 0.0)



    for k, v in MODEL_PRICES.items():



        if k.lower() in m:



            return v



    return DEFAULT_PRICE











def _obs_sql():



    """统一的 LLM 调用流：对话型(chat) + 任务型(task)，每条 assistant 回复算一次 observation。"""



    return """



    SELECT 'chat' AS src, cm.id AS oid, cm.conversation_id AS session_id, c.agent_id AS agent_id,



           cm.role AS role, cm.model AS model, cm.mocked AS mocked,



           cm.tokens_in AS ti, cm.tokens_out AS to_, cm.latency_ms AS lat, cm.error AS err, cm.created_at AS ts



    FROM chat_messages cm JOIN conversations c ON cm.conversation_id = c.id



    WHERE cm.role = 'assistant'



    UNION ALL



    SELECT 'task' AS src, t.id, t.task_id, tk.agent_id, t.role, t.model, t.mocked,



           t.tokens_in, t.tokens_out, t.latency_ms, NULL, t.created_at



    FROM traces t JOIN tasks tk ON t.task_id = tk.id



    WHERE t.role = 'assistant'



    """











@app.get("/api/monitor/overview")
def monitor_overview():
    row = db.query_one(
        "SELECT COUNT(*) n, COALESCE(SUM(ti),0) ti, COALESCE(SUM(to_),0) to_, "
        "COALESCE(AVG(lat),0) lat, "
        "COALESCE(SUM(CASE WHEN err IS NOT NULL AND err<>'' THEN 1 ELSE 0 END),0) errn, "
        "COALESCE(SUM(mocked),0) mockn FROM (" + _obs_sql() + ")")
    ag = db.query_one("SELECT COUNT(DISTINCT agent_id) a FROM conversations WHERE agent_id<>''")
    sess = db.query_one("SELECT COUNT(*) s FROM conversations")
    rows = db.query("SELECT model, ti, to_ FROM (" + _obs_sql() + ")")
    cost = 0.0
    for r in rows:
        pi, po = _price_of(r["model"])
        cost += (r["ti"] * pi + r["to_"] * po) / 1e6
    n = row["n"] or 0
    # P2-2: 延迟分位
    lats = [r["lat"] for r in db.query("SELECT lat FROM (" + _obs_sql() + ") WHERE lat IS NOT NULL")]
    p50 = _pct(lats, 0.5); p95 = _pct(lats, 0.95); p99 = _pct(lats, 0.99)
    # P2-2: 工具调用统计
    tc = db.query_one("SELECT COUNT(*) n, COALESCE(SUM(CASE WHEN ok=0 THEN 1 ELSE 0 END),0) f FROM tool_calls")
    tool_total = tc["n"] or 0; tool_failed = tc["f"] or 0
    tool_failed_rate = round(tool_failed / tool_total, 4) if tool_total else 0
    return {"calls": n, "tokens_in": row["ti"] or 0, "tokens_out": row["to_"] or 0,
            "tokens_total": (row["ti"] or 0) + (row["to_"] or 0),
            "avg_latency_ms": round(row["lat"] or 0, 1),
            "p50_latency_ms": round(p50, 1), "p95_latency_ms": round(p95, 1),
            "p99_latency_ms": round(p99, 1),
            "error_rate": round((row["errn"] or 0) / n, 4) if n else 0,
            "mocked_rate": round((row["mockn"] or 0) / n, 4) if n else 0,
            "tool_calls_total": tool_total, "tool_calls_failed": tool_failed,
            "tool_failed_rate": tool_failed_rate,
            "active_agents": ag["a"] if ag else 0, "sessions": sess["s"] if sess else 0,
            "cost_usd": round(cost, 4)}


@app.get("/api/monitor/daily")



def monitor_daily(days: int = 14):



    items = db.query(



        "SELECT date(ts,'unixepoch','localtime') d, COUNT(*) n, "



        "COALESCE(SUM(ti),0) ti, COALESCE(SUM(to_),0) to_ FROM (" + _obs_sql() + ") "



        "GROUP BY d ORDER BY d DESC LIMIT ?", (days,))



    return {"items": items}











@app.get("/api/monitor/models")



def monitor_models():



    items = db.query(



        "SELECT model, COUNT(*) n, COALESCE(SUM(ti),0) ti, COALESCE(SUM(to_),0) to_, "



        "COALESCE(AVG(lat),0) lat FROM (" + _obs_sql() + ") GROUP BY model ORDER BY n DESC")



    for it in items:



        pi, po = _price_of(it["model"])



        it["cost_usd"] = round((it["ti"] * pi + it["to_"] * po) / 1e6, 4)



    return {"items": items}











@app.get("/api/monitor/agents")



def monitor_agents():



    items = db.query(



        "SELECT agent_id, COUNT(*) n, COALESCE(SUM(ti),0) ti, COALESCE(SUM(to_),0) to_, "



        "COALESCE(AVG(lat),0) lat, COALESCE(SUM(CASE WHEN err IS NOT NULL AND err<>'' THEN 1 ELSE 0 END),0) errn "



        "FROM (" + _obs_sql() + ") GROUP BY agent_id ORDER BY n DESC LIMIT 20")



    names = {a["id"]: a["name"] for a in db.query("SELECT id,name FROM agents")}



    for it in items:



        it["name"] = names.get(it["agent_id"], it["agent_id"]) if it["agent_id"] else "(未指定)"



    return {"items": items}











@app.get("/api/monitor/traces")



def monitor_traces(limit: int = 50, agent_id: str = None):



    sql = "SELECT * FROM (" + _obs_sql() + ")"



    args = []



    if agent_id:



        sql += " WHERE agent_id=?"



        args.append(agent_id)



    sql += " ORDER BY ts DESC LIMIT ?"



    args.append(limit)



    items = db.query(sql, args)



    names = {a["id"]: a["name"] for a in db.query("SELECT id,name FROM agents")}



    titles = {c["id"]: c["title"] for c in db.query("SELECT id,title FROM conversations")}



    for it in items:



        it["agent_name"] = names.get(it["agent_id"], it["agent_id"]) if it["agent_id"] else "(未指定)"



        if it["src"] == "chat":



            it["session_title"] = titles.get(it["session_id"], it["session_id"])



        else:



            it["session_title"] = it["session_id"]



    return {"items": items}


# ================================================================== 提示词资产（P1）
# 采集业务人员真实使用的提示词 → 筛选/标注 → 一键沉淀为「技能」或「提示词模板」。
# 合规默认项：入库前脱敏（db.mask_pii）、可全局关闭采集（prompt_capture_enabled）。


def _operator(request):
    oid = (request.headers.get("X-Operator") or "").strip()[:64]
    onm = (request.headers.get("X-Operator-Name") or "").strip()[:64]
    try:
        oid = unquote(oid)
        onm = unquote(onm)
    except Exception:
        pass
    return oid, (onm or oid)


def _task_input_text(inp):
    try:
        if isinstance(inp, dict):
            parts = [str(v).strip() for v in inp.values() if isinstance(v, str) and str(v).strip()]
            if parts:
                return "\n".join(parts)
            return json.dumps(inp, ensure_ascii=False)
        return str(inp or "")
    except Exception:
        return ""


def _capture_on():
    try:
        return str(db.get_setting("prompt_capture_enabled", "1")) != "0"
    except Exception:
        return True


def _log_prompt(request, source, ref_id, agent_id, content, meta=None):
    """埋点写提示词流水。任何异常都静默吞掉，绝不拖累主流程。"""
    try:
        if not _capture_on():
            return None
        if not (content or "").strip():
            return None
        oid, onm = _operator(request)
        name = agent_id
        try:
            row = db.query_one("SELECT name FROM agents WHERE id=?", (agent_id,))
            if row and row.get("name"):
                name = row["name"]
        except Exception:
            pass
        return db.add_prompt_log(source, ref_id, agent_id, name, oid, onm, content, meta)
    except Exception:
        return None


@app.get("/api/prompts/stats")
def prompts_stats():
    return db.prompt_stats()


@app.get("/api/prompts/settings")
def prompts_settings_get():
    return {"capture_enabled": _capture_on()}


@app.post("/api/prompts/settings")
def prompts_settings_set(body: dict = None):
    body = body or {}
    on = bool(body.get("capture_enabled", True))
    db.set_settings({"prompt_capture_enabled": "1" if on else "0"})
    return {"ok": True, "capture_enabled": on}


@app.get("/api/prompts")
def prompts_list(status: Optional[str] = None, agent_id: Optional[str] = None, source: Optional[str] = None,
                 operator: Optional[str] = None, q: Optional[str] = None, limit: int = 200):
    return {"items": db.list_prompts(status=status, agent_id=agent_id, source=source,
                                     operator=operator, q=q, limit=limit)}


@app.get("/api/prompt-assets")
def prompt_assets_list(category: Optional[str] = None, q: Optional[str] = None, limit: int = 300):
    return {"items": db.list_assets(category=category, q=q, limit=limit)}


@app.delete("/api/prompt-assets/{aid}")
def prompt_asset_delete(aid: int):
    db.execute("DELETE FROM prompt_assets WHERE id=?", (aid,))
    return {"ok": True}


@app.post("/api/prompts/{pid}/feedback")
def prompt_feedback(pid: int, body: dict = None):
    body = body or {}
    kind = body.get("kind")
    if kind not in ("up", "down", "adopt", "copy"):
        raise HTTPException(400, "无效的反馈类型")
    if not db.update_prompt_feedback(pid, kind):
        raise HTTPException(404, "提示词记录不存在")
    return db.get_prompt(pid)


@app.post("/api/prompts/{pid}/status")
def prompt_set_status(pid: int, body: dict = None):
    body = body or {}
    st = body.get("status")
    if st not in ("new", "starred", "asset", "ignored"):
        raise HTTPException(400, "无效的状态")
    if not db.set_prompt_status(pid, st):
        raise HTTPException(404, "提示词记录不存在")
    return db.get_prompt(pid)


@app.post("/api/prompts/{pid}/to-skill")
def prompt_to_skill(pid: int, body: dict = None):
    body = body or {}
    p = db.get_prompt(pid)
    if not p:
        raise HTTPException(404, "提示词记录不存在")
    sid = (body.get("id") or "").strip()
    if not re.match(r"^[A-Za-z0-9_\-]+$", sid or ""):
        raise HTTPException(400, "技能标识只能包含字母、数字、下划线与连字符，且不能为空")
    name = (body.get("name") or p.get("summary") or sid)[:60]
    desc = (body.get("description") or ("沉淀自业务提示词 · " + (p.get("agent_name") or "通用")))[:160]
    text = (body.get("body") or "").strip()
    if not text:
        text = ("## 适用场景\n" + (p.get("summary") or "") +
                "\n\n## 提示词样例\n```\n" + (p.get("content") or "") +
                "\n```\n\n## 使用要求\n- 按上述样例的要素、结构与口径组织输出。\n")
    r = skilllib.save_skill(sid, name, desc, text, version="1.0")
    db.set_prompt_status(pid, "asset")
    try:
        registry.load_agents()
    except Exception:
        pass
    return {"ok": True, "id": sid, "skill": r}


@app.post("/api/prompts/{pid}/to-template")
def prompt_to_template(pid: int, body: dict = None):
    body = body or {}
    p = db.get_prompt(pid)
    if not p:
        raise HTTPException(404, "提示词记录不存在")
    aid = db.add_asset(
        title=(body.get("title") or p.get("summary") or (p.get("content") or "")[:30]),
        category=(body.get("category") or (p.get("agent_name") or "通用")),
        body=(body.get("body") or p.get("content") or ""),
        source_log_id=pid,
        author=(p.get("operator_name") or p.get("operator_id") or ""),
    )
    db.set_prompt_status(pid, "asset")
    return {"ok": True, "id": aid}


# ================================================================== 提示词模板（ptpl）
# 参照腾讯云 ADP：内置模板 + 自定义模板 + 版本管理

class PTplIn(BaseModel):
    title: str
    overview: str = ""
    ttype: str = "通用"
    tags: str = ""
    content: str = ""
    note: str = ""


@app.get("/api/ptpl")
def ptpl_list(tab: str = None, tag: str = None, ttype: str = None, q: str = None,
              fav: bool = False, page: int = 1, size: int = 15):
    return ptpl.list_templates(tab=tab, tag=tag, ttype=ttype, q=q, fav=fav,
                               page=page, size=size)


@app.post("/api/ptpl")
def ptpl_create(body: PTplIn, request: Request):
    if not body.title.strip():
        raise HTTPException(400, "标题不能为空")
    _oid, onm = _operator(request)
    pid = ptpl.create_template(body.title.strip(), body.overview, body.ttype,
                               body.tags, body.content, creator=onm)
    return {"ok": True, "id": pid}


@app.get("/api/ptpl/{pid}")
def ptpl_get(pid: int):
    t = ptpl.get_template(pid)
    if not t:
        raise HTTPException(404, "模板不存在")
    return t


@app.put("/api/ptpl/{pid}")
def ptpl_update(pid: int, body: PTplIn, request: Request):
    t = ptpl.get_template(pid)
    if not t:
        raise HTTPException(404, "模板不存在")
    op = _operator(request)
    if t["builtin"]:
        raise HTTPException(403, "内置模板不可直接编辑，请先「另存为」自定义模板")
    ptpl.update_template(pid, body.title, body.overview, body.ttype, body.tags,
                         body.content, body.note or "")
    return {"ok": True, "item": ptpl.get_template(pid)}


@app.delete("/api/ptpl/{pid}")
def ptpl_delete(pid: int):
    try:
        ptpl.delete_template(pid)
    except ValueError as e:
        raise HTTPException(403, str(e))
    return {"ok": True}


@app.post("/api/ptpl/{pid}/copy")
def ptpl_copy(pid: int):
    t = ptpl.get_template(pid)
    if not t:
        raise HTTPException(404, "模板不存在")
    ptpl.mark_copy(pid)
    return {"ok": True, "title": t["title"], "content": t["content"]}


@app.post("/api/ptpl/{pid}/favorite")
def ptpl_favorite(pid: int):
    try:
        fav = ptpl.toggle_favorite(pid)
    except ValueError as e:
        raise HTTPException(404, str(e))
    return {"ok": True, "favorite": fav}


@app.post("/api/ptpl/{pid}/duplicate")
def ptpl_duplicate(pid: int, request: Request):
    _oid, onm = _operator(request)
    try:
        item = ptpl.duplicate(pid, creator=onm)
    except ValueError as e:
        raise HTTPException(404, str(e))
    return {"ok": True, "item": item}


@app.get("/api/ptpl/{pid}/versions")
def ptpl_versions(pid: int):
    return ptpl.list_versions(pid)


@app.get("/api/ptpl/{pid}/versions/{ver}")
def ptpl_version_detail(pid: int, ver: int):
    try:
        return ptpl.get_version(pid, ver)
    except ValueError as e:
        raise HTTPException(404, str(e))


@app.post("/api/ptpl/{pid}/rollback/{ver}")
def ptpl_rollback(pid: int, ver: int):
    t = ptpl.get_template(pid)
    if not t:
        raise HTTPException(404, "模板不存在")
    if t["builtin"]:
        raise HTTPException(403, "内置模板不可回滚")
    try:
        item = ptpl.rollback(pid, ver)
    except ValueError as e:
        raise HTTPException(404, str(e))
    return {"ok": True, "item": item}


# ================================================================== Widget 组件库（对话交互卡片）
class WidgetIn(BaseModel):
    name: str
    category: str = "简单信息"
    description: str = ""
    spec_json: str = "{}"


@app.get("/api/widgets")
def widget_list(tab: str = None, cat: str = None, q: str = None, page: int = 1, size: int = 12):
    return widgetlib.list_widgets(tab=tab, cat=cat, q=q, page=page, size=size)


@app.post("/api/widgets")
def widget_create(body: WidgetIn, request: Request):
    if not body.name.strip():
        raise HTTPException(400, "名称不能为空")
    try:
        _oid, onm = _operator(request)
        wid = widgetlib.create_widget(body.name.strip(), body.category, body.description,
                                      body.spec_json, creator=onm)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "id": wid}


@app.get("/api/widgets/{wid}")
def widget_get(wid: int):
    w = widgetlib.get_widget(wid)
    if not w:
        raise HTTPException(404, "Widget 不存在")
    return w


@app.put("/api/widgets/{wid}")
def widget_update(wid: int, body: WidgetIn):
    try:
        w = widgetlib.update_widget(wid, body.name, body.category, body.description,
                                    body.spec_json)
    except ValueError as e:
        raise HTTPException(400 if "JSON" in str(e) or "type" in str(e) else 404, str(e))
    if not w:
        raise HTTPException(404, "Widget 不存在")
    return {"ok": True, "item": w}


@app.delete("/api/widgets/{wid}")
def widget_delete(wid: int):
    try:
        widgetlib.delete_widget(wid)
    except ValueError as e:
        raise HTTPException(403, str(e))
    return {"ok": True}


@app.post("/api/widgets/{wid}/copy")
def widget_copy(wid: int):
    w = widgetlib.get_widget(wid)
    if not w:
        raise HTTPException(404, "Widget 不存在")
    widgetlib.mark_copy(wid)
    return {"ok": True, "name": w["name"], "spec": w["spec"]}


@app.post("/api/widgets/{wid}/duplicate")
def widget_duplicate(wid: int, request: Request):
    _oid, onm = _operator(request)
    try:
        item = widgetlib.duplicate(wid, creator=onm)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "item": item}


# ================================================================== 知识库（RAG）
class KBIn(BaseModel):
    name: str
    description: str = ""


@app.get("/api/kbs")
def kb_list():
    return {"items": kblib.list_kbs()}


@app.post("/api/kbs")
def kb_create(body: KBIn):
    if not body.name.strip():
        raise HTTPException(400, "名称不能为空")
    try:
        kid = kblib.create_kb(body.name, body.description)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "id": kid}


@app.put("/api/kbs/{kid}")
def kb_update(kid: int, body: KBIn):
    if not kblib.get_kb(kid):
        raise HTTPException(404, "知识库不存在")
    kblib.update_kb(kid, body.name, body.description)
    return {"ok": True}


@app.delete("/api/kbs/{kid}")
def kb_delete(kid: int):
    kblib.delete_kb(kid)
    return {"ok": True}


@app.get("/api/kbs/{kid}/docs")
def kb_docs(kid: int):
    if not kblib.get_kb(kid):
        raise HTTPException(404, "知识库不存在")
    return {"items": kblib.list_docs(kid)}


@app.post("/api/kbs/{kid}/docs")
async def kb_upload(kid: int, request: Request):
    if not kblib.get_kb(kid):
        raise HTTPException(404, "知识库不存在")
    form = await request.form()
    up = form.get("file")
    if up is None or not hasattr(up, "read"):
        raise HTTPException(400, "缺少文件字段 file")
    data = await up.read()
    try:
        did, n = kblib.add_doc(kid, up.filename or "未命名.txt", data)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "id": did, "chunks": n}


@app.delete("/api/kbs/{kid}/docs/{did}")
def kb_doc_delete(kid: int, did: int):
    kblib.delete_doc(kid, did)
    return {"ok": True}


class KBSearchIn(BaseModel):
    kb_ids: List[int] = []
    q: str
    top_k: int = 5


@app.post("/api/kbs/search")
def kb_search_api(body: KBSearchIn):
    """召回测试：输入问题，返回各知识库命中片段（含得分与来源）。"""
    if not body.q.strip():
        raise HTTPException(400, "查询不能为空")
    hits = kblib.search(body.kb_ids, body.q, body.top_k)
    return {"items": hits}


# ================================================================== 长期记忆
@app.get("/api/memory")
def memory_get(request: Request, agent_id: str = ""):
    oid, _onm = _operator(request)
    return {"content": memlib.get_memory(oid, agent_id)}


@app.put("/api/memory")
def memory_set(request: Request, body: dict = None):
    body = body or {}
    agent_id = body.get("agent_id") or ""
    content = body.get("content") or ""
    oid, _onm = _operator(request)
    memlib.set_memory(oid, agent_id, content)
    return {"ok": True}


# ================================================================== 工作流
class WFIn(BaseModel):
    name: str
    description: str = ""
    spec_json: str = "{}"


@app.get("/api/wf")
def wf_list():
    return {"items": wflib.list_workflows()}


@app.post("/api/wf")
def wf_create(body: WFIn, request: Request):
    if not body.name.strip():
        raise HTTPException(400, "名称不能为空")
    _oid, onm = _operator(request)
    try:
        wid = wflib.create_workflow(body.name, body.description, body.spec_json, creator=onm)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "id": wid}


@app.get("/api/wf/{wid}")
def wf_get(wid: int):
    w = wflib.get_workflow(wid)
    if not w:
        raise HTTPException(404, "工作流不存在")
    return w


@app.put("/api/wf/{wid}")
def wf_update(wid: int, body: WFIn):
    try:
        w = wflib.update_workflow(wid, body.name, body.description, body.spec_json)
    except ValueError as e:
        code = 400 if ("JSON" in str(e) or "节点" in str(e) or "连线" in str(e)) else 404
        raise HTTPException(code, str(e))
    if not w:
        raise HTTPException(404, "工作流不存在")
    return {"ok": True, "item": w}


@app.delete("/api/wf/{wid}")
def wf_delete(wid: int):
    try:
        wflib.delete_workflow(wid)
    except ValueError as e:
        raise HTTPException(403, str(e))
    return {"ok": True}


@app.post("/api/wf/{wid}/duplicate")
def wf_duplicate(wid: int, request: Request):
    _oid, onm = _operator(request)
    try:
        wid2 = wflib.duplicate(wid, creator=onm)
    except ValueError as e:
        raise HTTPException(404, str(e))
    return {"ok": True, "id": wid2}


class WFRunIn(BaseModel):
    inputs: dict = {}


@app.post("/api/wf/{wid}/run")
async def wf_run(wid: int, body: WFRunIn):
    def _llm(system, user):
        return llm.chat(system, user, hint="workflow")
    def _kb(ids, q, k):
        return kblib.format_context(kblib.search(ids, q, k))
    try:
        r = await asyncio.to_thread(wflib.run_workflow, wid, body.inputs, _llm, _kb)
    except ValueError as e:
        raise HTTPException(404, str(e))
    if not r.get("ok"):
        raise HTTPException(400, r.get("error") or "执行失败")
    return r


@app.get("/api/wf/{wid}/runs")
def wf_runs(wid: int):
    return {"items": wflib.list_runs(wid)}
