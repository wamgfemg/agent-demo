# -*- coding: utf-8 -*-

"""智能体注册表：智能体的全生命周期管理。



数据来源有两处，统一收敛到 SQLite：

  1. agents/*.yaml —— 平台内置种子，首次启动时种入，之后不覆盖用户在线修改；

  2. 界面创建的自定义智能体 —— 直接落在库里，origin='custom'。



生命周期：草稿(draft) → 试运行 → 发布(published) → 下线(archived)

  发布 = 把当前草稿冻结为线上快照（published_json + 版本记录）。

  业务人员执行的是快照，构建者继续改草稿不会打扰线上使用者——这是"发布"的真正含义。

"""

import os, json, uuid, time

import yaml

from . import db, toolregistry, skills as skilllib

from .config import AGENT_DIR



_cache = {}

DEFAULT_PERSONA = "你是资深业务专家，回答需结构清晰、结论先行、可落地，避免空话。"



SPEC_KEYS = ("id", "name", "icon", "category", "description", "persona", "skills", "tools",

             "runtime", "output", "input_schema", "params", "model", "temperature")





def _normalize(spec: dict) -> dict:

    """补齐默认值 + 兼容旧的 mode 字段。"""

    s = dict(spec or {})

    if not s.get("runtime"):

        s["runtime"] = s.get("mode") or "chat"

    s.setdefault("output", "docx" if s["runtime"] == "doc" else "markdown")

    s.setdefault("persona", DEFAULT_PERSONA)

    s.setdefault("skills", [])

    s.setdefault("tools", [])

    # 输入表单兼容两种写法：["brief"] 与 [{key,label,type}]

    fixed = []

    for f in (s.get("input_schema") or []):

        if isinstance(f, str):

            fixed.append({"key": f, "label": f, "type": "textarea", "required": False})

        elif isinstance(f, dict) and f.get("key"):

            f.setdefault("label", f["key"])

            f.setdefault("type", "textarea")

            fixed.append(f)

    s["input_schema"] = fixed

    s.setdefault("params", {})

    s.setdefault("icon", "🤖")

    s.setdefault("category", "通用")

    return s





def new_id():

    return "agt_" + uuid.uuid4().hex[:8]





def validate(spec: dict):

    """引用合法性校验：配了却不存在的工具/技能必须暴露，绝不能静默失效。"""

    warn = []

    known_tools = toolregistry.names()

    known_skills = {s["id"] for s in skilllib.list_skills()}

    for t in spec.get("tools") or []:

        if t not in known_tools:

            warn.append(f"工具未注册：{t}")

    for s in spec.get("skills") or []:

        if s not in known_skills:

            warn.append(f"技能缺失：{s}")

    return warn





# ------------------------------------------------------------------ 加载

def load_agents(force=False):

    """加载 YAML 种子（不覆盖已存在记录），再把库中全部智能体装入缓存。"""

    if os.path.isdir(AGENT_DIR):

        for fn in sorted(os.listdir(AGENT_DIR)):

            if not fn.endswith((".yaml", ".yml")):

                continue

            try:

                spec = yaml.safe_load(open(os.path.join(AGENT_DIR, fn), "r", encoding="utf-8"))

            except Exception as e:

                print(f"[registry] 解析失败 {fn}: {e}")

                continue

            if not spec or "id" not in spec:

                continue

            spec = _normalize(spec)

            spec["_file"] = fn

            db.upsert_agent(spec, origin="yaml", status="published", touch=force)

    _cache.clear()

    for r in db.query("SELECT * FROM agents"):

        spec = db.uj(r["spec_json"]) or {"id": r["id"]}

        spec = _normalize(spec)

        spec["_status"] = r["status"] or "published"

        spec["_origin"] = r["origin"] or "yaml"

        spec["_version"] = r["version"] or 1

        spec["_published_at"] = r["published_at"]

        spec["_icon"] = r["icon"] or spec.get("icon")

        _cache[r["id"]] = spec

    # P2-3: 历史种子（status=published 但 published_at 为空）回填发布时间
    db.execute("UPDATE agents SET published_at=COALESCE(created_at,?) "
               "WHERE status='published' AND (published_at IS NULL OR published_at=0)", (db.now(),))
    return list_agents()





def get_agent(agent_id):

    if agent_id in _cache:

        return _cache[agent_id]

    row = db.query_one("SELECT spec_json,status,origin,version,icon FROM agents WHERE id=?", (agent_id,))

    if not row:

        return None

    spec = _normalize(db.uj(row["spec_json"]) or {"id": agent_id})

    spec["_status"] = row["status"] or "published"

    spec["_origin"] = row["origin"] or "yaml"

    spec["_version"] = row["version"] or 1

    _cache[agent_id] = spec

    return spec





def get_published_or_draft(agent_id):

    """任务执行时取用的定义：已发布的用线上快照，否则用草稿。"""

    row = db.query_one("SELECT status,published_json,spec_json FROM agents WHERE id=?", (agent_id,))

    if not row:

        return get_agent(agent_id)

    src = row["published_json"] if (row["status"] == "published" and row["published_json"]) else row["spec_json"]

    spec = _normalize(db.uj(src) or {})

    spec.setdefault("id", agent_id)

    # 快照里可能缺运行时元数据（旧版本 YAML），用当前草稿补齐

    cur = get_agent(agent_id) or {}

    for k in ("runtime", "output", "model", "temperature"):

        if not spec.get(k) and cur.get(k):

            spec[k] = cur[k]

    return spec





def list_agents(scope="all"):

    where = "WHERE status != 'archived'" if scope == "all" else "WHERE status=?"

    args = () if scope == "all" else (scope,)

    rows = db.query(f"SELECT id,name,category,description,icon,enabled,status,origin,version,"

                    f"published_at,created_at,updated_at,spec_json FROM agents {where} "

                    f"ORDER BY status, category, created_at", args)

    out = []

    for r in rows:

        spec = db.uj(r["spec_json"]) or {}

        out.append({

            "id": r["id"], "name": r["name"], "category": r["category"],

            "description": r["description"], "icon": r["icon"] or spec.get("icon") or "🤖",

            "enabled": r["enabled"], "status": r["status"], "origin": r["origin"],

            "version": r["version"], "published_at": r["published_at"],

            "runtime": spec.get("runtime") or spec.get("mode") or "chat",

            "output": spec.get("output") or "markdown",

            "skills": spec.get("skills") or [], "tools": spec.get("tools") or [],

            "input_schema": spec.get("input_schema") or [],

            "created_at": r["created_at"], "updated_at": r["updated_at"],

        })

    return out





def detail(agent_id):

    spec = get_agent(agent_id)

    if not spec:

        return None

    row = db.query_one("SELECT status,origin,version,published_at,icon,enabled FROM agents WHERE id=?", (agent_id,))

    return {

        "id": spec["id"], "name": spec.get("name"), "icon": row["icon"] if row else spec.get("icon"),

        "category": spec.get("category"), "description": spec.get("description"),

        "persona": spec.get("persona"), "runtime": spec.get("runtime"),

        "output": spec.get("output"), "model": spec.get("model"),

        "temperature": spec.get("temperature"),

        "status": row["status"] if row else "draft",

        "enabled": int(row["enabled"]) if (row and row["enabled"] is not None) else 1,

        "origin": row["origin"] if row else "custom",

        "version": row["version"] if row else 1,

        "published_at": row["published_at"] if row else None,

        "input_schema": spec.get("input_schema") or [],

        "params": spec.get("params") or {},

        "skills": [{"id": s, "name": (skilllib.get_skill(s) or {}).get("meta", {}).get("name"),

                    "exists": bool(skilllib.get_skill(s))} for s in (spec.get("skills") or [])],

        "tools": [{"name": t, "exists": bool(toolregistry.get(t)),

                   "source": (toolregistry.get(t) or {}).get("source"),

                   "server": (toolregistry.get(t) or {}).get("server"),

                   "description": (toolregistry.get(t) or {}).get("description")}

                  for t in (spec.get("tools") or [])],

        "warnings": validate(spec),

    }





# ------------------------------------------------------------------ 增删改

def create(payload: dict):

    aid = (payload.get("id") or "").strip() or new_id()

    if db.query_one("SELECT id FROM agents WHERE id=?", (aid,)):

        raise ValueError(f"智能体 ID 已存在：{aid}")

    spec = _normalize({k: v for k, v in payload.items() if k in SPEC_KEYS})

    spec["id"] = aid

    if not spec.get("name"):

        raise ValueError("智能体名称不能为空")

    db.execute("INSERT INTO agents(id,name,category,description,icon,spec_json,enabled,created_at,"

               "updated_at,status,origin,version) VALUES(?,?,?,?,?,?,1,?,?,'draft','custom',1)",

               (aid, spec.get("name"), spec.get("category"), spec.get("description"), spec.get("icon"),

                db.j(spec), db.now(), db.now()))

    load_agents()

    return detail(aid)





def update(agent_id, payload: dict):

    row = db.query_one("SELECT spec_json,origin FROM agents WHERE id=?", (agent_id,))

    if not row:

        raise ValueError("智能体不存在")

    spec = _normalize(db.uj(row["spec_json"]) or {"id": agent_id})

    for k, v in payload.items():

        if k in SPEC_KEYS and k != "id":

            spec[k] = v

    spec["id"] = agent_id

    db.execute("UPDATE agents SET name=?,category=?,description=?,icon=?,spec_json=?,updated_at=? WHERE id=?",

               (spec.get("name"), spec.get("category"), spec.get("description"), spec.get("icon"),

                db.j(spec), db.now(), agent_id))

    load_agents()

    return detail(agent_id)





def delete(agent_id):

    row = db.query_one("SELECT origin FROM agents WHERE id=?", (agent_id,))

    if not row:

        raise ValueError("智能体不存在")

    if (row["origin"] or "yaml") != "custom":

        raise ValueError("内置智能体不可删除，可改为下线（归档）")

    db.execute("DELETE FROM agents WHERE id=?", (agent_id,))

    db.execute("DELETE FROM agent_versions WHERE agent_id=?", (agent_id,))

    load_agents()

    return True





def archive(agent_id):

    db.execute("UPDATE agents SET status='archived',updated_at=? WHERE id=?", (db.now(), agent_id))

    load_agents()

    return True





def clone(agent_id):

    src = db.query_one("SELECT spec_json,name FROM agents WHERE id=?", (agent_id,))

    if not src:

        raise ValueError("智能体不存在")

    spec = _normalize(db.uj(src["spec_json"]) or {})

    spec["id"] = new_id()

    spec["name"] = (src["name"] or "") + " 副本"

    return create(spec)





def publish(agent_id, note=""):

    spec = get_agent(agent_id)

    if not spec:

        raise ValueError("智能体不存在")

    warn = validate(spec)

    if not (spec.get("name") or "").strip():

        raise ValueError("智能体名称不能为空")

    ver = db.publish_agent(agent_id, note)

    load_agents()

    return {"id": agent_id, "version": ver, "warnings": warn, **detail(agent_id)}





def unpublish(agent_id):

    db.unpublish_agent(agent_id)

    load_agents()

    return detail(agent_id)





def enable(agent_id, on=True):

    db.execute("UPDATE agents SET enabled=?,updated_at=? WHERE id=?", (1 if on else 0, db.now(), agent_id))

    load_agents()

    return True





def require_enabled(agent_id):
    """运行入口前置校验：停用的智能体禁止被业务侧调用（构建者试运行不受限）。"""
    row = db.query_one("SELECT enabled,status FROM agents WHERE id=?", (agent_id,))
    if not row:
        raise ValueError("智能体不存在")
    if not row["enabled"]:
        raise ValueError("智能体已停用，请在构建器中重新启用后再使用")


def unarchive(agent_id):

    row = db.query_one("SELECT published_json FROM agents WHERE id=?", (agent_id,))

    if not row:

        raise ValueError("智能体不存在")

    # 曾发布过则恢复为已发布，否则回到草稿，由构建者重新发布

    status = "published" if row["published_json"] else "draft"

    db.execute("UPDATE agents SET status=?,updated_at=? WHERE id=?", (status, db.now(), agent_id))

    load_agents()

    return True





def rollback(agent_id, version):

    """回滚到指定历史版本：恢复该版本定义并重新发布（版本号递增，便于追溯）。"""

    row = db.query_one("SELECT spec_json FROM agent_versions WHERE agent_id=? AND version=?",

                       (agent_id, version))

    if not row:

        raise ValueError(f"版本 v{version} 不存在")

    spec = _normalize(db.uj(row["spec_json"]) or {})

    spec["id"] = agent_id

    db.execute("UPDATE agents SET name=?,category=?,description=?,icon=?,spec_json=?,updated_at=? "

               "WHERE id=?",

               (spec.get("name"), spec.get("category"), spec.get("description"), spec.get("icon"),

                db.j(spec), db.now(), agent_id))

    load_agents()

    return publish(agent_id, f"回滚至 v{version}")



