# -*- coding: utf-8 -*-
"""执行引擎（Engine）：平台"执行架构"的元数据 + 可调参数 + 版本历史。

边界（务必保持）：
  - **执行逻辑由代码实现**（runtime.run_task 分派 → pipeline.* 流水线 / run_chat）；
    本模块不改变执行逻辑，也不能新增执行器。新增一种引擎仍需改代码。
  - 本模块管理的是引擎的【定义】：名称、说明、详情文案、可调参数，并做版本化
    （每次保存产生新版本，可查看历史与回滚）。
  - 可调参数会被运行时读取（runtime.py / pipeline.py），读不到时回落到代码默认，
    因此"从未编辑过"时行为与改造前完全一致。
"""
from . import db

# ------------------------------------------------------- 可调参数：代码默认（回落基线）
PARAM_DEFAULTS = {
    "chat":   {"tool_rounds": 6},
    "doc":    {"chapter_parallel": 3, "chapter_retry": 2},
    "ppt":    {"default_pages": 10},
    "calc":   {"sandbox_timeout": 20},
    "report": {},
}

# ------------------------------------------------------- 参数展示元数据（label/单位/范围/提示）
PARAM_META = {
    "chat": {
        "tool_rounds": {"label": "工具循环上限", "unit": "轮", "min": 1, "max": 20,
                        "hint": "模型自主调用工具的最大轮次，防止陷入死循环"},
    },
    "doc": {
        "chapter_parallel": {"label": "章节并发数", "unit": "章", "min": 1, "max": 8,
                             "hint": "同时生成的最大章节数；机器较弱可下调"},
        "chapter_retry":    {"label": "评审重试次数", "unit": "次", "min": 0, "max": 5,
                             "hint": "章节评审未通过时的重跑次数"},
    },
    "ppt": {
        "default_pages": {"label": "默认页数", "unit": "页", "min": 3, "max": 40,
                          "hint": "未显式指定页数时使用"},
    },
    "calc": {
        "sandbox_timeout": {"label": "沙箱超时", "unit": "秒", "min": 3, "max": 120,
                            "hint": "计算代码在受限沙箱中的最长执行时间"},
    },
    "report": {},
}

# ------------------------------------------------------- 执行器（代码位置），用于界面标注
CODE_IMPL = {
    "chat":   "runtime.run_chat（ReAct 工具循环）",
    "doc":    "pipeline.run_doc_pipeline（章节流水线）",
    "ppt":    "pipeline.run_ppt_pipeline",
    "calc":   "pipeline.run_calc（python_sandbox）",
    "report": "pipeline.run_report",
}


# ------------------------------------------------------- 已下架的执行方式
# 不再出现在构建器（智能体「执行方式」）的可选列表里；runtime.run_task 的分派逻辑
# 保留，以兼容已经配置过的智能体。report 的作业规范已抽成 skills/project_report.md，
# 等价于「对话问答（通用）+ 该技能」，且 chat 链路才真正会用上智能体挂载的工具。
RETIRED = {"report"}


def _builtin():
    """内置定义：名称/说明取自 runtime.RUNTIMES，详情取自 runtime.RUNTIME_DETAILS。
    延迟导入 runtime 以避免模块级循环依赖。"""
    from . import runtime
    out = []
    details = getattr(runtime, "RUNTIME_DETAILS", {}) or {}
    for r in getattr(runtime, "RUNTIMES", []) or []:
        eid = r.get("id")
        out.append({
            "id": eid,
            "name": r.get("name") or eid,
            "desc": r.get("desc") or "",
            "detail": dict(details.get(eid) or {}),
            "params": dict(PARAM_DEFAULTS.get(eid) or {}),
            "param_meta": dict(PARAM_META.get(eid) or {}),
            "code_impl": CODE_IMPL.get(eid, ""),
        })
    return out


def _spec(eid, name, desc, detail, params, param_meta, code_impl):
    return {"id": eid, "name": name, "desc": desc, "detail": detail or {},
            "params": params or {}, "param_meta": param_meta or {},
            "code_impl": code_impl or ""}


def _spec_of(eid, ver):
    row = db.query_one("SELECT spec_json FROM engine_versions WHERE engine_id=? AND version=?",
                       (eid, ver))
    return db.uj(row["spec_json"], {}) if row else None


# ------------------------------------------------------------------ seed
def seed_if_empty():
    """表空时把内置引擎录入为 v1；幂等，重复调用无副作用。"""
    for b in _builtin():
        if db.query_one("SELECT engine_id FROM engine_defs WHERE engine_id=?", (b["id"],)):
            continue
        spec = _spec(b["id"], b["name"], b["desc"], b["detail"], b["params"], b["param_meta"],
                     b["code_impl"])
        db.execute("INSERT INTO engine_defs(engine_id,name,enabled,current_version,updated_at)"
                   " VALUES(?,?,?,?,?)", (b["id"], b["name"], 1, 1, db.now()))
        db.execute("INSERT INTO engine_versions(engine_id,version,spec_json,note,created_at)"
                   " VALUES(?,?,?,?,?)", (b["id"], 1, db.j(spec), "初始内置版本", db.now()))


# ------------------------------------------------------------------ 读
def list_engines():
    seed_if_empty()
    rows = db.query("SELECT engine_id,name,enabled,current_version,updated_at FROM engine_defs")
    order = {b["id"]: i for i, b in enumerate(_builtin())}
    rows.sort(key=lambda r: order.get(r["engine_id"], 999))
    out = []
    for r in rows:
        spec = _spec_of(r["engine_id"], r["current_version"]) or {}
        out.append({
            "id": r["engine_id"],
            "retired": r["engine_id"] in RETIRED,
            "name": r["name"],
            "enabled": int(r["enabled"] if r["enabled"] is not None else 1),
            "version": int(r["current_version"] or 1),
            "desc": spec.get("desc", ""),
            "param_meta": spec.get("param_meta") or dict(PARAM_META.get(r["engine_id"]) or {}),
            "code_impl": spec.get("code_impl") or CODE_IMPL.get(r["engine_id"], ""),
            "updated_at": r["updated_at"],
        })
    return out


def get_engine(eid):
    seed_if_empty()
    r = db.query_one("SELECT engine_id,name,enabled,current_version,updated_at FROM engine_defs"
                     " WHERE engine_id=?", (eid,))
    if not r:
        return None
    spec = _spec_of(eid, r["current_version"]) or {}
    return {"id": eid, "name": r["name"],
            "enabled": int(r["enabled"] if r["enabled"] is not None else 1),
            "version": int(r["current_version"] or 1),
            "updated_at": r["updated_at"], "spec": spec}


def versions(eid):
    return db.query("SELECT version,note,created_at FROM engine_versions WHERE engine_id=?"
                    " ORDER BY version DESC LIMIT 50", (eid,))


# ------------------------------------------------------------------ 写
def save_engine(eid, patch, note=""):
    """保存定义为新版本（版本号 +1）。patch 支持 name/desc/detail/params/enabled。"""
    cur = get_engine(eid)
    if not cur:
        return None
    spec = dict(cur["spec"])
    for k in ("name", "desc", "detail", "params", "code_impl"):
        if patch.get(k) is not None:
            spec[k] = patch[k]
    spec["id"] = eid
    spec["param_meta"] = dict(PARAM_META.get(eid) or {})   # 参数元数据固定来自代码
    ver = int(cur["version"]) + 1
    db.execute("INSERT INTO engine_versions(engine_id,version,spec_json,note,created_at)"
               " VALUES(?,?,?,?,?)", (eid, ver, db.j(spec), (note or "更新定义"), db.now()))
    if patch.get("enabled") is None:
        enabled = cur["enabled"]
    else:
        enabled = 1 if patch["enabled"] else 0
    db.execute("UPDATE engine_defs SET name=?, enabled=?, current_version=?, updated_at=?"
               " WHERE engine_id=?",
               (spec.get("name") or cur["name"], enabled, ver, db.now(), eid))
    return get_engine(eid)


def rollback(eid, version):
    """以历史版本内容创建新版本（可追溯，不删历史）。"""
    cur = get_engine(eid)
    if not cur:
        return None
    src = _spec_of(eid, version)
    if not src:
        return None
    src = dict(src)
    src["id"] = eid
    src["param_meta"] = dict(PARAM_META.get(eid) or {})
    newver = int(cur["version"]) + 1
    db.execute("INSERT INTO engine_versions(engine_id,version,spec_json,note,created_at)"
               " VALUES(?,?,?,?,?)", (eid, newver, db.j(src), "回滚到 v%s" % version, db.now()))
    db.execute("UPDATE engine_defs SET name=?, current_version=?, updated_at=? WHERE engine_id=?",
               (src.get("name") or cur["name"], newver, db.now(), eid))
    return get_engine(eid)


# ------------------------------------------------------------------ 运行时读取（带代码默认回落）
def params(eid):
    """返回该引擎当前生效的参数；DB 不可用/未定义时回落到代码默认。"""
    base = dict(PARAM_DEFAULTS.get(eid) or {})
    try:
        r = db.query_one("SELECT current_version FROM engine_defs WHERE engine_id=?", (eid,))
        if r:
            spec = _spec_of(eid, r["current_version"]) or {}
            for k, v in (spec.get("params") or {}).items():
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    base[k] = v
    except Exception:
        pass
    return base


def iparam(eid, key, default):
    """整数参数读取：无值/非法时返回 default（注意 0 是合法值，不能用 or 兜底）。"""
    v = params(eid).get(key)
    if v is None:
        return default
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


# ------------------------------------------------------------------ 构建器（catalog）用
def runtime_list():
    """构建器下拉：读 store（编辑后立即同步），失败回落内置；不含已停用。"""
    try:
        items = list_engines()
    except Exception:
        items = []
    if items:
        return [{"id": e["id"], "name": e["name"], "desc": e["desc"]}
                for e in items if e.get("enabled") and e["id"] not in RETIRED]
    return [{"id": b["id"], "name": b["name"], "desc": b["desc"]} for b in _builtin()]


def details_map():
    """构建器「查看详情」弹层：id -> detail。"""
    try:
        items = list_engines()
    except Exception:
        items = []
    out = {}
    if items:
        for e in items:
            spec = _spec_of(e["id"], e["version"]) or {}
            out[e["id"]] = dict(spec.get("detail") or {})
        return out
    for b in _builtin():
        out[b["id"]] = dict(b["detail"])
    return out
