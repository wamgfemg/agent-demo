# -*- coding: utf-8 -*-
"""统一 LLM 入口：OpenAI 兼容协议（OpenRouter / 各家），无密钥时进入模拟模式。

模拟模式不是"假装成功"，而是产出结构完全一致、内容可阅读的占位结果，
让整条流水线在无密钥状态下仍可端到端跑通、可验证、可切换。
"""
import json, time, uuid, random
import httpx
from . import db, models
from .config import DEFAULT_BASE_URL


class LLMResult:
    def __init__(self, content, tin=0, tout=0, model="", mocked=False, latency_ms=0,
                 tool_calls=None, raw=None):
        self.content = content
        self.tokens_in = tin
        self.tokens_out = tout
        self.model = model
        self.mocked = mocked
        self.latency_ms = latency_ms
        # OpenAI function calling 返回的工具调用列表：[{id, name, arguments(dict)}]
        self.tool_calls = tool_calls or []
        self.raw = raw


def cfg():
    return db.all_settings()


def is_mocked():
    return not (cfg().get("llm_api_key") or "").strip()


# ---------------------------------------------------------------- 模拟生成
def _mk_requirement(ctx):
    p = ctx.get("project_name") or "本项目"
    brief = ctx.get("brief") or ""
    return json.dumps({
        "project_name": p,
        "background": f"{p}围绕业务连续性与运维效率提升展开，需在既定工期内完成平台建设与交付。",
        "score_points": [
            "技术方案完整性与针对性",
            "架构先进性与可扩展性",
            "实施计划与资源配置合理性",
            "服务保障与应急响应能力",
            "项目团队与业绩佐证",
        ],
        "deliverables": ["总体技术方案", "实施方案", "服务保障方案", "项目管理方案"],
        "constraints": ["需满足招标文件中全部实质性条款", "不得出现与评分点无关的空泛表述"],
        "brief": brief[:500],
    }, ensure_ascii=False)


def _mk_outline(ctx):
    p = ctx.get("project_name") or "本项目"
    n = int(ctx.get("chapter_count") or 8)
    chapters = []
    for i in range(1, n + 1):
        chapters.append({
            "key": f"ch{i:02d}",
            "title": f"第{i}章 " + ["项目概述与需求理解", "总体架构设计", "功能模块设计",
                                 "关键技术实现", "安全与合规设计", "实施方案与进度计划",
                                 "服务保障与运维体系", "项目团队与业绩"][(i - 1) % 8],
            "target_words": 1200,
            "keypoints": ["现状与问题", "设计思路", "实现要点", "验收标准"],
        })
    return json.dumps({"project_name": p, "chapters": chapters}, ensure_ascii=False)


def _mk_governance(ctx):
    return json.dumps({
        "glossary": [
            {"term": "统一运维管理平台", "definition": "本项目交付的核心平台，承担监控、告警、工单与资产管理职能"},
            {"term": "AIOps", "definition": "基于机器学习与规则引擎的智能运维能力集合"},
            {"term": "SLA", "definition": "服务等级协议，本项目承诺的可用性指标为 99.9%"},
        ],
        "numbers": [
            {"key": "系统可用性", "value": "99.9", "unit": "%", "source": "招标文件服务要求"},
            {"key": "故障响应时限", "value": "15", "unit": "分钟", "source": "服务承诺"},
            {"key": "实施周期", "value": "6", "unit": "个月", "source": "项目计划"},
        ],
    }, ensure_ascii=False)


def _mk_chapter(ctx):
    ch = ctx.get("chapter") or {}
    title = ch.get("title") or "章节"
    kps = ch.get("keypoints") or ["总体设计", "实现路径", "保障措施"]
    proj = ctx.get("project_name") or "本项目"
    parts = [f"本章围绕{title.replace('第', '').split('章')[-1].strip()}展开，结合{proj}的实际场景与招标要求，"
             f"给出可落地的设计与实现说明。方案坚持“先体检再加固”的实施思路，确保所有设计均可追溯至需求条目。"]
    for kp in kps:
        parts.append(f"### {kp}")
        parts.append(f"在{kp}方面，平台采用分层解耦的设计原则：能力层沉淀通用服务，应用层面向具体业务场景编排，"
                     f"接入层统一鉴权与流量管控。该设计使{proj}后续扩展新场景时无需改动既有模块，"
                     f"仅需新增适配器即可完成对接，有效降低二次开发成本。")
        parts.append(f"具体到{kp}的落地，项目组将按“现状调研、方案确认、试点验证、规模推广”四步推进；"
                     f"每一步均设置明确的验收口径与回退条件，未通过验收不进入下一阶段，"
                     f"以此控制交付风险并保证与招标文件要求的一致性。")
    parts.append(f"### 小结\n本章明确了{title}相关的设计原则、实现路径与验收口径，"
                 f"为后续章节的细化描述奠定基础，所有指标均可与数字台账中的登记值逐条对应。")
    return "\n\n".join(parts)


def _mk_review(ctx):
    return json.dumps({"passed": True, "score": 92,
                       "issues": [],
                       "comment": "章节要素完整，术语与数字台账一致，可进入合稿。"}, ensure_ascii=False)


def _mk_ppt_outline(ctx):
    p = ctx.get("project_name") or "本项目"
    n = int(ctx.get("slide_count") or 10)
    slides = []
    titles = ["项目背景与痛点", "建设目标", "总体架构", "核心能力一：统一监控",
              "核心能力二：智能告警", "核心能力三：自动化处置", "安全与合规",
              "实施路径", "服务保障", "团队与业绩", "投资与收益", "总结"]
    for i in range(n):
        slides.append({
            "index": i + 1,
            "title": titles[i % len(titles)],
            "bullets": ["现状问题：分散建设导致数据不通",
                        "方案要点：统一平台 + 能力复用",
                        "预期成效：故障定位时间下降 60%"],
            "notes": f"本页讲解{p}的{titles[i % len(titles)]}，重点强调与招标评分点的对应关系。",
        })
    return json.dumps({"project_name": p, "slides": slides}, ensure_ascii=False)


def _mk_calc(ctx):
    q = ctx.get("question") or "计算示例"
    return json.dumps({"language": "python",
                       "code": "result = {'items': 3, 'unit_price': 12000, 'total': 3 * 12000}\nprint(result)",
                       "explanation": f"针对「{q}」构建计算模型并输出结构化结果。"}, ensure_ascii=False)


def _mk_report(ctx):
    return "本项目本周按计划推进：需求确认已完成，架构设计与接口联调进行中，暂无高风险项；" \
           "下周重点为试点环境部署与数据接入。"


def _mk_chat(ctx):
    """通用对话型智能体的模拟输出：结构完整、可读，便于无密钥时验证全链路。"""
    # 只取首行：避免把参考资料、表单其他字段一起塞进这句话里
    q = (ctx.get("brief") or ctx.get("question") or "").strip().split("\n")[0][:40]
    head = f"针对「{q}」" if q else "针对本次需求"
    return "\n\n".join([
        f"{head}，结合已配置的作业规范与可用工具，输出如下结论。",
        "### 一、结论摘要",
        "1. 需求边界已明确，关键约束项均已纳入考虑；\n"
        "2. 建议按“先体检、再加固”的顺序推进，避免返工；\n"
        "3. 涉及的量化指标需在数字台账中登记后方可引用，确保前后口径一致。",
        "### 二、分析过程",
        "第一步，梳理输入信息与既有素材，识别缺失项并标注需人工确认的内容；\n"
        "第二步，对照作业规范逐条检查表述口径与章节要素；\n"
        "第三步，输出可交付的结构化成果，并保留可追溯的依据说明。",
        "### 三、下一步建议",
        "- 补齐上述待确认项后重新执行，可显著提升成果完整度；\n"
        "- 如需产出正式文档，可在智能体配置中将输出类型改为 Word 或 PPT。",
        "> 说明：当前为模拟模式（未配置大模型密钥），以上为占位内容，用于验证链路完整性。",
    ])


_MOCK = {
    "chat": _mk_chat,
    "requirement": _mk_requirement,
    "outline": _mk_outline,
    "governance": _mk_governance,
    "chapter": _mk_chapter,
    "review": _mk_review,
    "ppt_outline": _mk_ppt_outline,
    "calc": _mk_calc,
    "report": _mk_report,
}


def _mock(ctx, hint):
    fn = _MOCK.get(hint, _mk_chapter)
    return fn(ctx or {})


# ---------------------------------------------------------------- 真实调用
def _real(messages, model, temperature, max_tokens, tools=None, tool_choice=None):
    c = cfg()
    base = (c.get("llm_base_url") or DEFAULT_BASE_URL).rstrip("/")
    key = (c.get("llm_api_key") or "").strip()
    # 最后一道防线：占位值（default/auto/空）绝不能当真实模型 ID 发出去
    model = models.resolve(model)
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    if c.get("llm_app_name"):
        headers["HTTP-Referer"] = "https://agentforge.local"
        headers["X-Title"] = c["llm_app_name"]
    payload = {"model": model, "messages": messages, "temperature": temperature,
               "max_tokens": max_tokens}
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = tool_choice or "auto"
    t0 = time.time()
    r = httpx.post(f"{base}/chat/completions", json=payload, headers=headers, timeout=180)
    latency = int((time.time() - t0) * 1000)
    if r.status_code >= 400:
        raise RuntimeError(f"LLM {r.status_code}: {r.text[:400]}")
    data = r.json()
    msg = data["choices"][0]["message"]
    usage = data.get("usage") or {}
    calls = []
    for tc in (msg.get("tool_calls") or []):
        fn = tc.get("function") or {}
        args = fn.get("arguments") or "{}"
        try:
            args = json.loads(args)
        except Exception:
            args = {"_raw": args}
        calls.append({"id": tc.get("id"), "name": fn.get("name"), "arguments": args})
    return LLMResult(msg.get("content") or "", usage.get("prompt_tokens", 0),
                     usage.get("completion_tokens", 0), model, False, latency,
                     tool_calls=calls, raw=msg)


def chat(system, user, model=None, temperature=0.4, max_tokens=4096,
         hint="chapter", ctx=None, task_id=None, unit_id=None, role="agent", tools=None):
    """统一入口。返回 LLMResult。"""
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": user})

    if is_mocked():
        time.sleep(0.05)
        content = _mock(ctx, hint)
        res = LLMResult(content, len(user) // 2, len(content) // 2, "mock", True, 50)
    else:
        res = _real(messages, model, temperature, max_tokens, tools=tools)

    try:
        db.add_trace(task_id, unit_id, role, res.model, res.mocked,
                     res.tokens_in, res.tokens_out, res.latency_ms)
        if task_id:
            db.execute("UPDATE tasks SET tokens_in=tokens_in+?, tokens_out=tokens_out+? WHERE id=?",
                       (res.tokens_in, res.tokens_out, task_id))
            if unit_id:
                db.execute("UPDATE task_units SET tokens_in=tokens_in+?, tokens_out=tokens_out+? WHERE id=?",
                           (res.tokens_in, res.tokens_out, unit_id))
    except Exception:
        pass
    return res


def chat_messages(messages, model=None, temperature=0.4, max_tokens=4096, tools=None,
                  task_id=None, unit_id=None, role="agent"):
    """多轮对话入口（ReAct 循环用）。messages 为完整对话列表，含 system。"""
    if is_mocked():
        last = ""
        for m in reversed(messages):
            if m.get("role") == "user":
                last = m.get("content") or ""
                break
        res = LLMResult(_mock({"brief": last, "question": last}, "chat"),
                        len(last) // 2, 400, "mock", True, 50)
    else:
        res = _real(messages, model, temperature, max_tokens, tools=tools)
    try:
        db.add_trace(task_id, unit_id, role, res.model, res.mocked,
                     res.tokens_in, res.tokens_out, res.latency_ms)
        if task_id:
            db.execute("UPDATE tasks SET tokens_in=tokens_in+?, tokens_out=tokens_out+? WHERE id=?",
                       (res.tokens_in, res.tokens_out, task_id))
    except Exception:
        pass
    return res


class _StreamHttpError(Exception):
    def __init__(self, status, body):
        super().__init__(f"LLM {status}: {body}")
        self.status, self.body = status, body


def stream_chat_messages(messages, model=None, temperature=0.4, max_tokens=4096):
    """流式多轮对话（/api/chat stream 模式专用）。生成器：
    yield {"type":"delta","v":文本片段} ... 最后 yield {"type":"done", ...统计}。
    配置/headers 与 _real 一致；完成后写 trace。HTTP 层错误抛 _StreamHttpError，
    400 且疑似不支持 stream_options 时自动降级重试（无 usage 统计）。"""
    c = cfg()
    base = (c.get("llm_base_url") or DEFAULT_BASE_URL).rstrip("/")
    key = (c.get("llm_api_key") or "").strip()
    model = models.resolve(model)
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    if c.get("llm_app_name"):
        headers["HTTP-Referer"] = "https://agentforge.local"
        headers["X-Title"] = c["llm_app_name"]

    def _try_stream(use_stream_options):
        payload = {"model": model, "messages": messages, "temperature": temperature,
                   "max_tokens": max_tokens, "stream": True}
        if use_stream_options:
            payload["stream_options"] = {"include_usage": True}
        t0 = time.time()
        content_parts, tin, tout = [], 0, 0
        with httpx.stream("POST", f"{base}/chat/completions", json=payload,
                          headers=headers, timeout=180) as r:
            if r.status_code >= 400:
                raise _StreamHttpError(r.status_code,
                                       r.read().decode("utf-8", "ignore")[:400])
            for line in r.iter_lines():
                line = line.strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    obj = json.loads(data)
                except Exception:
                    continue
                if obj.get("error"):
                    raise RuntimeError(str(obj.get("error"))[:400])
                usage = obj.get("usage") or {}
                if usage.get("prompt_tokens") is not None:
                    tin = usage.get("prompt_tokens", 0) or 0
                    tout = usage.get("completion_tokens", 0) or 0
                ch = obj.get("choices") or []
                if ch:
                    v = (ch[0].get("delta") or {}).get("content")
                    if v:
                        content_parts.append(v)
                        yield {"type": "delta", "v": v}
        latency = int((time.time() - t0) * 1000)
        full = "".join(content_parts)
        try:
            db.add_trace(None, None, "agent", model, False, tin, tout, latency)
        except Exception:
            pass
        yield {"type": "done", "content": full, "model": model, "tokens_in": tin,
               "tokens_out": tout, "latency_ms": latency}

    try:
        yield from _try_stream(True)
    except _StreamHttpError as e:
        if e.status == 400:
            # 网关可能不支持 stream_options，去掉后重试一次
            yield from _try_stream(False)
        else:
            raise RuntimeError(str(e))


def chat_json(system, user, **kw):
    """请求结构化输出，失败时抛出，交由上层重试。"""
    res = chat(system, user, **kw)
    txt = res.content.strip()
    if txt.startswith("```"):
        txt = txt.split("```")[1]
        if txt.startswith("json"):
            txt = txt[4:]
    s, e = txt.find("{"), txt.rfind("}")
    if s < 0 or e < 0:
        raise ValueError("模型未返回 JSON: " + txt[:200])
    return json.loads(txt[s:e + 1]), res
