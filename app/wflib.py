# -*- coding: utf-8 -*-
"""工作流模块（参照腾讯云 ADP 工作流设计，简化实现）。

画布编排：节点 + 连线。节点类型（对齐 ADP 四大类的常用子集）：
- start      基础节点：定义输入变量 {{var}}
- llm        信息处理：大模型节点，prompt 模板引用变量
- kb_search  信息处理：知识检索节点
- condition  基础节点：条件判断（关键词命中选分支）
- widget     信息处理：输出交互卡片（模板）
- reply      基础节点：最终回复（模板）
- end        基础节点：结束

引擎：从 start 沿边拓扑执行，节点输出存入 vars[node_id]，
模板里 {{node_id}} / {{input.xxx}} 引用上游变量。
"""
import json, re, time
from .db import execute, query, query_one

_SCHEMA = """
CREATE TABLE IF NOT EXISTS workflows(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  description TEXT DEFAULT '',
  spec_json TEXT DEFAULT '{}',
  builtin INTEGER DEFAULT 0,
  creator TEXT DEFAULT '',
  created_at REAL,
  updated_at REAL
);
CREATE TABLE IF NOT EXISTS workflow_runs(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  wf_id INTEGER NOT NULL,
  input_json TEXT DEFAULT '{}',
  status TEXT DEFAULT 'running',
  output TEXT DEFAULT '',
  log_json TEXT DEFAULT '[]',
  error TEXT DEFAULT '',
  created_at REAL
);
"""

NODE_TYPES = ("start", "llm", "kb_search", "condition", "widget", "reply", "end")

_TPL_RE = re.compile(r"\{\{\s*([\w.]+)\s*\}\}")


def _now():
    return time.time()


def _validate(spec):
    try:
        s = json.loads(spec if isinstance(spec, str) else json.dumps(spec))
    except Exception:
        raise ValueError("spec 不是合法 JSON")
    nodes = s.get("nodes") or []
    ids = [n.get("id") for n in nodes]
    if not nodes or any(not i for i in ids) or len(ids) != len(set(ids)):
        raise ValueError("节点为空或存在重复/缺失 id")
    for n in nodes:
        if n.get("type") not in NODE_TYPES:
            raise ValueError("不支持的节点类型：" + str(n.get("type")))
    if not any(n["type"] == "start" for n in nodes):
        raise ValueError("缺少 start 节点")
    edges = s.get("edges") or []
    for e in edges:
        if e.get("from") not in ids or e.get("to") not in ids:
            raise ValueError("连线引用了不存在的节点")
    return json.dumps(s, ensure_ascii=False)


def ensure_tables():
    from .db import conn as _conn, _lock
    with _lock:
        c = _conn()
        c.executescript(_SCHEMA)
        if c.execute("SELECT COUNT(*) FROM workflows WHERE builtin=1").fetchone()[0] == 0:
            t = _now()
            for wf in BUILTIN_WFS:
                c.execute("INSERT INTO workflows(name,description,spec_json,builtin,creator,"
                          "created_at,updated_at) VALUES(?,?,?,1,'AgentForge 平台',?,?)",
                          (wf["name"], wf["description"],
                           json.dumps(wf["spec"], ensure_ascii=False), t, t))
        c.commit()
        c.close()


BUILTIN_WFS = [
    {
        "name": "知识库问答流程",
        "description": "标准 RAG 流程：问题 → 知识检索 → 大模型作答（带来源引用）",
        "spec": {
            "nodes": [
                {"id": "n1", "type": "start", "name": "开始", "x": 60, "y": 140,
                 "config": {"vars": [{"key": "question", "label": "问题", "type": "textarea"}]}},
                {"id": "n2", "type": "kb_search", "name": "知识检索", "x": 300, "y": 140,
                 "config": {"kb_id": 1, "query": "{{input.question}}", "top_k": 5}},
                {"id": "n3", "type": "llm", "name": "大模型作答", "x": 540, "y": 140,
                 "config": {"prompt": "请根据以下检索资料回答用户问题，关键结论标注来源文档。\n\n"
                                      "【检索资料】\n{{n2}}\n\n【用户问题】{{input.question}}"}},
                {"id": "n4", "type": "reply", "name": "回复", "x": 780, "y": 140,
                 "config": {"template": "{{n3}}"}},
            ],
            "edges": [{"from": "n1", "to": "n2"}, {"from": "n2", "to": "n3"},
                      {"from": "n3", "to": "n4"}],
        },
    },
    {
        "name": "信息收集流程",
        "description": "演示流程：先收集信息（Widget 卡片），再汇总生成",
        "spec": {
            "nodes": [
                {"id": "n1", "type": "start", "name": "开始", "x": 60, "y": 140,
                 "config": {"vars": [{"key": "topic", "label": "主题", "type": "text"}]}},
                {"id": "n2", "type": "widget", "name": "补充信息卡片", "x": 300, "y": 140,
                 "config": {"spec": {"type": "form", "title": "补充信息：{{input.topic}}",
                                     "fields": [{"name": "补充要点", "kind": "textarea",
                                                 "placeholder": "补充背景或要求"}],
                                     "submit": "提交"}}},
                {"id": "n3", "type": "llm", "name": "生成", "x": 540, "y": 140,
                 "config": {"prompt": "围绕主题「{{input.topic}}」，结合用户补充的要点，"
                                      "输出一份结构化建议（3-5条）。\n用户补充：{{n2}}"}},
                {"id": "n4", "type": "reply", "name": "回复", "x": 780, "y": 140,
                 "config": {"template": "{{n3}}"}},
            ],
            "edges": [{"from": "n1", "to": "n2"}, {"from": "n2", "to": "n3"},
                      {"from": "n3", "to": "n4"}],
        },
    },
]


# ------------------------------------------------------------------ CRUD
def list_workflows():
    return [{"id": w["id"], "name": w["name"], "description": w["description"],
             "builtin": w["builtin"], "updated_at": w["updated_at"]}
            for w in query("SELECT * FROM workflows ORDER BY builtin DESC,updated_at DESC")]


def get_workflow(wid):
    r = query_one("SELECT * FROM workflows WHERE id=?", (wid,))
    if not r:
        return None
    try:
        r["spec"] = json.loads(r.pop("spec_json") or "{}")
    except Exception:
        r["spec"] = {}
    return r


def create_workflow(name, description="", spec_json="{}", creator=""):
    spec = _validate(spec_json)
    t = _now()
    return execute("INSERT INTO workflows(name,description,spec_json,builtin,creator,"
                   "created_at,updated_at) VALUES(?,?,?,?,0,?,?)",
                   (name.strip(), description.strip(), spec, creator or "", t, t))


def update_workflow(wid, name=None, description=None, spec_json=None):
    r = query_one("SELECT * FROM workflows WHERE id=?", (wid,))
    if not r:
        raise ValueError("工作流不存在")
    sets, args = ["updated_at=?"], [_now()]
    if name is not None:
        sets.append("name=?"); args.append(name.strip())
    if description is not None:
        sets.append("description=?"); args.append(description.strip())
    if spec_json is not None:
        sets.append("spec_json=?"); args.append(_validate(spec_json))
    execute("UPDATE workflows SET " + ",".join(sets) + " WHERE id=?", args + [wid])
    return get_workflow(wid)


def delete_workflow(wid):
    r = query_one("SELECT * FROM workflows WHERE id=?", (wid,))
    if not r:
        return False
    if r["builtin"]:
        raise ValueError("内置工作流不可删除，可另存副本")
    execute("DELETE FROM workflow_runs WHERE wf_id=?", (wid,))
    execute("DELETE FROM workflows WHERE id=?", (wid,))


def duplicate(wid, creator=""):
    w = get_workflow(wid)
    if not w:
        raise ValueError("工作流不存在")
    nid = create_workflow(w["name"] + "（副本）", w["description"],
                          json.dumps(w["spec"], ensure_ascii=False), creator)
    return get_workflow(nid)


# ------------------------------------------------------------------ 执行引擎
def _render(tpl, vars_):
    def sub(m):
        key = m.group(1)
        if key.startswith("input."):
            return str(vars_.get("input", {}).get(key[6:], ""))
        v = vars_.get(key, "")
        if isinstance(v, dict):
            return json.dumps(v, ensure_ascii=False)
        return str(v)
    return _TPL_RE.sub(sub, tpl or "")


def run_workflow(wid, inputs, llm_chat, kb_search_fn=None):
    """执行工作流。llm_chat(system,user)->result；kb_search_fn(kb_ids,q,top_k)->hits。
    返回 run 记录 dict。"""
    w = get_workflow(wid)
    if not w:
        raise ValueError("工作流不存在")
    spec = w["spec"]
    nodes = {n["id"]: n for n in spec.get("nodes", [])}
    edges = spec.get("edges", [])
    vars_ = {"input": dict(inputs or {})}
    log = []
    t0 = _now()
    rid = execute("INSERT INTO workflow_runs(wf_id,input_json,status,created_at) "
                  "VALUES(?,?, 'running',?)",
                  (wid, json.dumps(inputs or {}, ensure_ascii=False), t0))

    cur = next((n for n in nodes.values() if n["type"] == "start"), None)
    output, widget_spec = "", None
    try:
        while cur:
            nid, ntype, cfg = cur["id"], cur["type"], (cur.get("config") or {})
            ts = time.time()
            if ntype == "start":
                pass  # 输入已在 vars_.input
            elif ntype == "llm":
                r = llm_chat("你是严谨的业务助手，用中文回答。", _render(cfg.get("prompt", ""), vars_))
                vars_[nid] = r.content
            elif ntype == "kb_search":
                if not kb_search_fn:
                    raise ValueError("知识检索不可用")
                q = _render(cfg.get("query", ""), vars_)
                ctx = kb_search_fn([cfg.get("kb_id")], q, int(cfg.get("top_k", 5)))
                vars_[nid] = (ctx if isinstance(ctx, str) and ctx.strip() else "（未检索到相关内容）")
            elif ntype == "condition":
                var = _render(cfg.get("var", ""), vars_)
                nxt = None
                for rule in (cfg.get("rules") or []):
                    if rule.get("contains") and rule["contains"] in var:
                        nxt = rule.get("to"); break
                nxt = nxt or cfg.get("default_to")
                log.append({"node": cur["name"], "type": ntype, "ms": int((time.time()-ts)*1000),
                            "detail": "命中分支 → " + str(nodes.get(nxt, {}).get("name", nxt))})
                cur = nodes.get(nxt)
                continue
            elif ntype == "widget":
                widget_spec = json.loads(_render(json.dumps(cfg.get("spec", {}), ensure_ascii=False), vars_))
                vars_[nid] = "（已输出交互卡片）"
            elif ntype == "reply":
                output = _render(cfg.get("template", ""), vars_)
            log.append({"node": cur["name"], "type": ntype,
                        "ms": int((time.time() - ts) * 1000)})
            # 找下一个非条件直连节点
            nxt = next((e["to"] for e in edges if e["from"] == nid), None)
            cur = nodes.get(nxt) if nxt else None
        if not output:
            output = "（流程执行完毕，无回复节点输出）"
        result = {"ok": True, "output": output, "widget": widget_spec, "log": log,
                  "ms": int((time.time() - t0) * 1000)}
        execute("UPDATE workflow_runs SET status='done',output=?,log_json=? WHERE id=?",
                (output, json.dumps(log, ensure_ascii=False), rid))
        return result
    except Exception as e:
        err = str(e)[:400]
        execute("UPDATE workflow_runs SET status='failed',error=? WHERE id=?", (err, rid))
        return {"ok": False, "error": err, "log": log}


def list_runs(wid, limit=20):
    return query("SELECT id,status,output,error,log_json,created_at FROM workflow_runs "
                 "WHERE wf_id=? ORDER BY id DESC LIMIT ?", (wid, limit))
