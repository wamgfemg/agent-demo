# -*- coding: utf-8 -*-
"""通用执行运行时。

平台上一共有两类执行方式：
  1. 专用流水线（doc/ppt/calc/report）——针对长文档等重型场景，带章节 checkpoint、并行与断点续跑；
  2. 对话运行时（chat）——任意新建智能体的默认执行方式，带 ReAct 工具循环：
     模型可自主发起工具调用，平台执行后把结果回灌，循环直到产出最终答复。

新建智能体只需选一种运行时 + 挂技能与工具，即可发布给业务人员使用。
"""
import os, json, re, time
from . import db, llm, toolregistry, tools, pipeline, engines

MAX_ROUNDS = 6          # ReAct 最大轮次，防止模型陷入死循环
MAX_TOOL_CHARS = 6000   # 单次工具回灌文本上限，保住小机内存与上下文

RUNTIMES = [
    {"id": "chat", "name": "对话问答（通用）",
     "desc": "模型自主理解需求并可调用所挂工具，适合问答、分析、改写等大多数场景"},
    {"id": "doc", "name": "长文档流水线（多智能体）",
     "desc": "大纲拆分 → 并行章节生成 → 逐章评审 → 合稿 docx，支持断点续跑与数字台账"},
    {"id": "ppt", "name": "汇报 PPT",
     "desc": "生成结构化分页内容并输出可编辑 pptx（每页元素独立，非图片）"},
    {"id": "calc", "name": "数据计算",
     "desc": "自然语言转计算代码，在受限沙箱执行后给出结论"},
    {"id": "report", "name": "汇总报告",
     "desc": "基于输入资料生成周报、风险清单、跟踪报告等"},
]

OUTPUTS = [
    {"id": "markdown", "name": "Markdown 文件"},
    {"id": "docx", "name": "Word 文档（可编辑）"},
    {"id": "none", "name": "仅返回结果（不落文件）"},
]


# 每个执行引擎的「详情」，仅供前端构建器只读展示，不参与执行。
RUNTIME_DETAILS = {
    "chat": {
        "usage": "问答、分析、改写、咨询等通用场景；挂上工具后可自主查资料、算数、写文件。",
        "mechanism": "ReAct 工具循环：模型自主决定是否调用工具，最多 6 轮，工具结果回灌后继续推理，直到给出最终答复。",
        "inputs": "你的指令或问题；若该智能体配置了输入项，则按表单填写。",
        "artifacts": "对话答复；按输出类型可选 Markdown 文件 / Word 文档 / 仅返回结果。",
        "resumable": "不支持（单轮生成，无执行单元）",
        "tools": "工具由模型在对话中按需调用；技能正文会拼进系统提示词。",
        "tags": ["问答", "分析", "改写", "工具调用"],
    },
    "doc": {
        "usage": "技术方案、投标文件、长报告等需要多章节、大体量、口径统一的文档。",
        "mechanism": "大纲拆分 → 并行章节生成（默认并发 3）→ 逐章评审 → 合稿；带数字台账，保证跨章节数字口径一致。",
        "inputs": "文档 / 项目标题 + 各章节要求（可在智能体里配置输入项）。",
        "artifacts": "可编辑的 Word 文档（.docx），章节为原生段落，非图片。",
        "resumable": "支持（章节即执行单元；中断后只重跑失败 / 未完成章节，已完成的直接复用）",
        "tools": "按章节自动检索知识库；可挂写作类技能。",
        "tags": ["长文档", "多智能体", "可续跑", "数字台账"],
    },
    "ppt": {
        "usage": "汇报 / 宣讲型演示材料，如方案汇报、成果汇报。",
        "mechanism": "先规划页面大纲 → 生成每页要点与讲稿 → 输出 pptx。",
        "inputs": "项目 / 主题名称 + 汇报要点（页数默认 10 页）。",
        "artifacts": "可编辑的 PowerPoint（.pptx），每页文本、图形均为独立可编辑元素（非图片）。",
        "resumable": "不支持（一次性生成，无执行单元）",
        "tools": "可挂演示 / 写作类技能；自动检索知识库素材。",
        "tags": ["汇报", "可编辑", "非图片"],
    },
    "calc": {
        "usage": "需要精确计算或数据处理的任务，如报价测算、指标核算。",
        "mechanism": "把自然语言转成计算代码，在受限沙箱中执行（超时约 10 秒）后给出结论。",
        "inputs": "计算问题 + 相关数据（在指令或表单中提供）。",
        "artifacts": "计算报告（Markdown），含代码、执行结果与结论。",
        "resumable": "不支持（单次执行，无执行单元）",
        "tools": "在沙箱内执行 Python，不调用外部工具。",
        "tags": ["计算", "沙箱", "可信结果"],
    },
    "report": {
        "usage": "周报、跟踪报告、风险清单等周期性 / 汇总性产出。",
        "mechanism": "基于输入资料汇总分析，生成结构化报告（进度 / 风险 / 待办 / 下周计划）。",
        "inputs": "本期项目资料或数据 + 报告要求。",
        "artifacts": "项目跟踪报告（Markdown）。",
        "resumable": "不支持（单次生成，无执行单元）",
        "tools": "可挂写作类技能。",
        "tags": ["汇总", "周报", "风险清单"],
    },
}


# ------------------------------------------------------------------ 输入渲染
def render_input(spec, inp):
    """把动态表单的值渲染成给模型看的文本。"""
    schema = spec.get("input_schema") or []
    lines = []
    for f in schema:
        k = f.get("key")
        v = inp.get(k)
        if v is None or v == "":
            continue
        lines.append(f"{f.get('label') or k}：{v}")
    if lines:
        return "\n".join(lines)
    return "\n".join(f"{k}：{v}" for k, v in (inp or {}).items() if v not in (None, ""))


def md_to_chapters(md, fallback_title="成果"):
    """把 Markdown 按二级标题切成章节，便于输出结构化的 Word。"""
    md = (md or "").strip()
    if not md:
        return [{"title": fallback_title, "content": "（无内容）"}]
    parts = re.split(r"\n(?=#{1,3}\s)", md)
    chapters, buf_title, buf = [], None, []
    for p in parts:
        m = re.match(r"^(#{1,3})\s+(.*)", p)
        if m:
            if buf_title or buf:
                chapters.append({"title": buf_title or fallback_title,
                                 "content": "\n".join(buf).strip()})
            buf_title, buf = m.group(2).strip(), [p]
        else:
            buf.append(p)
    if buf_title or buf:
        chapters.append({"title": buf_title or fallback_title, "content": "\n".join(buf).strip()})
    return [c for c in chapters if c["content"]] or [{"title": fallback_title, "content": md}]


# ------------------------------------------------------------------ ReAct
def _tool_definitions(spec):
    """生成 OpenAI function calling 格式的 tools；仅包含该智能体挂载且已注册的工具。"""
    out = []
    for name in spec.get("tools") or []:
        s = toolregistry.get(name)
        if not s:
            continue
        out.append({"type": "function",
                    "function": {"name": name, "description": s.get("description") or name,
                                 "parameters": s.get("parameters") or {"type": "object", "properties": {}}}})
    return out


async def react(system, user, spec, task_id, model=None, temperature=0.4):
    """模型自主调用工具的循环。模拟模式下退化为单次生成。"""
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    allowed = set(spec.get("tools") or [])
    tdefs = _tool_definitions(spec)
    trace = []

    for rnd in range(engines.iparam("chat", "tool_rounds", MAX_ROUNDS)):
        res = await _call_llm(messages, tdefs, task_id, model, temperature)
        if not res.tool_calls:
            return res.content or "", trace
        # 有工具调用：先回灌 assistant 消息，再逐条执行工具
        messages.append({"role": "assistant", "content": res.content or "",
                         "tool_calls": (res.raw or {}).get("tool_calls")})
        for tc in res.tool_calls:
            name, args = tc.get("name"), tc.get("arguments") or {}
            if name not in allowed:
                out = {"ok": False, "error": f"工具未授权：{name}"}
            else:
                out = await toolregistry.call(name, args, timeout=60)
            db.log_tool_call(task_id, name, (toolregistry.get(name) or {}).get("source", "?"),
                             args, bool(out.get("ok")), out.get("_ms", 0))
            trace.append({"round": rnd + 1, "tool": name, "args": args,
                          "ok": bool(out.get("ok")), "ms": out.get("_ms", 0)})
            messages.append({"role": "tool", "tool_call_id": tc.get("id"),
                             "content": json.dumps(out, ensure_ascii=False)[:MAX_TOOL_CHARS]})
        _set_progress(task_id, rnd)
    # 轮次用尽仍无最终答复：再要一次纯文本总结
    messages.append({"role": "user", "content": "请基于以上工具结果直接给出最终答复，不要再调用工具。"})
    res = await _call_llm(messages, None, task_id, model, temperature)
    return res.content or "", trace


async def _call_llm(messages, tdefs, task_id, model, temperature):
    fn = lambda: llm.chat_messages(messages, model=model, temperature=temperature,
                                   max_tokens=4096, tools=tdefs if tdefs else None,
                                   task_id=task_id, role="智能体")
    import asyncio
    return await asyncio.to_thread(fn)


def _set_progress(task_id, rnd):
    db.update_task(task_id, stage=f"工具调用第 {rnd + 1} 轮",
                   progress=min(90, 30 + rnd * 10))


# ------------------------------------------------------------------ 对话运行时
async def run_chat(task_id, spec, inp, params):
    title = (inp.get("title") or inp.get("project_name") or spec.get("name") or "智能体任务").strip()
    persona = spec.get("persona") or "你是资深业务专家，回答需结构清晰、结论先行、可落地。"
    system = pipeline.build_system(spec, persona)

    db.update_task(task_id, stage="理解需求", progress=10)
    user = render_input(spec, inp)
    if not user.strip():
        user = title

    db.update_task(task_id, stage="检索素材", progress=20)
    trace = []
    material = await pipeline.retrieve(task_id, spec, f"{title} {user[:200]}", top_k=3, trace=trace)
    if material:
        user += "\n\n参考资料（优先采用其中的事实与表述）：\n" + material

    db.update_task(task_id, stage="推理生成", progress=35)
    content, calls = await react(system, user, spec, task_id,
                                 model=spec.get("model"),
                                 temperature=float(spec.get("temperature") or 0.4))
    trace = trace + calls

    out_type = spec.get("output") or "markdown"
    db.update_task(task_id, stage="生成产物", progress=92)
    artifact = None
    if out_type == "docx":
        chapters = md_to_chapters(content, title)
        fname = f"{title}-{time.strftime('%Y%m%d-%H%M%S')}.docx"
        path = os.path.join(pipeline.ARTIFACT_DIR, fname)
        await _thread(tools.write_docx, title, chapters, path, None, None,
                      f"{spec.get('name') or 'AgentForge'} · 自动生成")
        pipeline._save_artifact(task_id, fname, path,
                                "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
        artifact = fname
    elif out_type == "markdown":
        fname = f"{title}-{time.strftime('%Y%m%d-%H%M%S')}.md"
        path = os.path.join(pipeline.ARTIFACT_DIR, fname)
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"# {title}\n\n> 由「{spec.get('name')}」生成 · "
                    f"{time.strftime('%Y-%m-%d %H:%M:%S')}\n\n{content}\n")
        pipeline._save_artifact(task_id, fname, path, "text/markdown")
        artifact = fname

    db.execute("UPDATE tasks SET input_json=? WHERE id=?",
               (db.j({**inp, "_answer": content[:20000], "_trace": trace}), task_id))
    db.update_task(task_id, status="done", stage="已完成", progress=100)
    return {"artifact": artifact, "answer": content[:20000], "tool_calls": trace}


async def _thread(fn, *a):
    import asyncio
    return await asyncio.to_thread(fn, *a)


# ------------------------------------------------------------------ 统一入口
async def run_task(task_id, spec, inp, params):
    rt = (spec.get("runtime") or "chat").lower()
    if rt == "doc":
        return await pipeline.run_doc_pipeline(task_id, spec, inp, params)
    if rt == "ppt":
        return await pipeline.run_ppt_pipeline(task_id, spec, inp, params)
    if rt == "calc":
        return await pipeline.run_calc(task_id, spec, inp, params)
    if rt == "report":
        return await pipeline.run_report(task_id, spec, inp, params)
    return await run_chat(task_id, spec, inp, params)
