# -*- coding: utf-8 -*-
"""Widget 组件库：对话内交互卡片（参照腾讯云 ADP Widget 设计）。

核心概念（与 ADP 一致）：
- Widget 是嵌入对话窗口的可交互卡片，智能体按协议输出结构化 JSON，
  前端渲染为卡片；用户在卡片上的操作（提交/确认/点击）转成文本回传智能体。
- 五种类型：form 澄清/表单、confirm 操作确认、info 信息展示、
  list 列表信息、progress 过程反馈。
- Action：sys.chat（回传对话，等价表单提交/确认）、sys.go_to_url（前端跳转）、
  sys.download（文件下载）。在本平台实现中：form/confirm 走 sys.chat，
  info.buttons 走 go_to_url/download。
"""
import json
import time
from .db import execute, query, query_one

_SCHEMA = """
CREATE TABLE IF NOT EXISTS widgets(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  category TEXT DEFAULT '简单信息',
  description TEXT DEFAULT '',
  spec_json TEXT DEFAULT '{}',
  builtin INTEGER DEFAULT 0,
  creator TEXT DEFAULT '',
  copy_count INTEGER DEFAULT 0,
  created_at REAL,
  updated_at REAL
);
CREATE INDEX IF NOT EXISTS idx_widget_cat ON widgets(category);
"""

WIDGET_TYPES = ("form", "confirm", "info", "list", "progress")

# 注入到对话 system prompt 的 Widget 输出协议（打通聊天的关键）
WIDGET_PROMPT = """【交互卡片（Widget）能力】
当需要向用户收集信息、请求确认或展示结构化内容时，你可以在回复中输出一张交互卡片：用 ```widget 和 ``` 围栏包裹一段 JSON，可与普通文字混排，每次回复最多一张。用户在卡片上的操作会以文本形式回传到对话，你再据此继续处理。type 五选一：
- form 澄清/表单（收集信息）：{"type":"form","title":"标题","desc":"可选说明","fields":[{"name":"姓名","kind":"input|textarea|select|radio|checkbox","placeholder":"请输入姓名","options":["选项1","选项2"]}],"submit":"按钮文字(可选)"}
- confirm 操作确认：{"type":"confirm","title":"标题","desc":"可选说明","ok":"确认按钮文字","cancel":"取消按钮文字"}
- info 信息展示：{"type":"info","icon":"一个表情符号","title":"标题","desc":"说明","buttons":[{"text":"按钮文字","url":"https://...","download":false}]}
- list 列表展示：{"type":"list","title":"标题","items":[{"icon":"表情符号","title":"条目标题","subtitle":"副标题"}]}
- progress 过程反馈：{"type":"progress","title":"标题","desc":"可选说明","steps":["步骤1","步骤2","步骤3"],"current":1}
使用原则：需要用户补信息→form；需要用户拍板→confirm；结果/状态→info 或 list；任务进行中→progress。不需要交互时正常输出文字即可，不要滥用卡片。围栏标记必须是三个反引号后紧跟 widget（即 ```widget），不要用 ```json 或其他标记，否则卡片无法渲染。"""

# ------------------------------------------------------------------ 内置模板（参照 ADP Widget 模板库）
BUILTIN_WIDGETS = [
    {
        "name": "基础表单澄清",
        "category": "澄清询问",
        "description": "收集姓名、手机号等基础信息，一次完成澄清，避免多轮追问。",
        "spec": {"type": "form", "title": "请完善以下信息", "desc": "补充以下信息以便继续为您服务",
                 "fields": [{"name": "姓名", "kind": "input", "placeholder": "请输入姓名"},
                            {"name": "手机号", "kind": "input", "placeholder": "请输入手机号"}],
                 "submit": "提交"},
    },
    {
        "name": "单选澄清",
        "category": "澄清询问",
        "description": "单选场景澄清，如视频清晰度、方案二选一等。",
        "spec": {"type": "form", "title": "请选择你希望的视频清晰度",
                 "fields": [{"name": "清晰度", "kind": "radio", "options": ["标清", "高清", "超清", "蓝光"]}],
                 "submit": "提交"},
    },
    {
        "name": "多选澄清",
        "category": "澄清询问",
        "description": "多选场景澄清，如内容类型、兴趣标签等。",
        "spec": {"type": "form", "title": "您想要下载哪种类型的红楼梦相关内容",
                 "fields": [{"name": "内容类型", "kind": "checkbox", "options": ["影视作品", "书籍", "音频", "软件"]}],
                 "submit": "提交"},
    },
    {
        "name": "复合表单澄清",
        "category": "表单提交",
        "description": "多字段复合表单，适合一次收集成组的业务信息。",
        "spec": {"type": "form", "title": "请完善以下信息", "desc": "以下信息将用于生成方案",
                 "fields": [{"name": "公司名称", "kind": "input", "placeholder": "请输入公司名称"},
                            {"name": "行业", "kind": "select", "options": ["互联网", "金融", "制造业", "教育", "医疗"]},
                            {"name": "需求描述", "kind": "textarea", "placeholder": "请简要描述需求"}],
                 "submit": "提交"},
    },
    {
        "name": "操作确认提示",
        "category": "操作确认",
        "description": "执行敏感/不可逆操作前请用户确认，点击即完成确认。",
        "spec": {"type": "confirm", "title": "打开网页", "desc": "即将打开外部网页，是否继续？",
                 "ok": "打开", "cancel": "取消"},
    },
    {
        "name": "简单信息卡片",
        "category": "简单信息",
        "description": "结果/状态展示，支持跳转与下载按钮。",
        "spec": {"type": "info", "icon": "✅", "title": "操作成功", "desc": "文件已生成完毕，可点击下方按钮查看或下载",
                 "buttons": [{"text": "查看详情", "url": "https://example.com/detail"},
                             {"text": "下载文件", "url": "https://example.com/file.docx", "download": True}]},
    },
    {
        "name": "歌曲列表",
        "category": "列表信息",
        "description": "结构化列表展示，适合检索结果、候选清单。",
        "spec": {"type": "list", "title": "为你找到的歌曲",
                 "items": [{"icon": "🎵", "title": "晴天", "subtitle": "周杰伦 · 叶惠美"},
                           {"icon": "🎵", "title": "七里香", "subtitle": "周杰伦 · 七里香"},
                           {"icon": "🎵", "title": "稻香", "subtitle": "周杰伦 · 魔杰座"}]},
    },
    {
        "name": "过程反馈",
        "category": "过程反馈",
        "description": "任务执行中的进度展示，让用户直观了解进展，避免误判卡顿。",
        "spec": {"type": "progress", "title": "播客生成中", "desc": "正在合成音频，请稍候",
                 "steps": ["解析文本", "生成脚本", "合成音频"], "current": 2},
    },
]


def _now():
    return time.time()


def _validate_spec(spec_json):
    try:
        spec = json.loads(spec_json)
    except Exception:
        raise ValueError("spec 不是合法 JSON")
    if not isinstance(spec, dict) or spec.get("type") not in WIDGET_TYPES:
        raise ValueError("spec.type 必须是 " + "/".join(WIDGET_TYPES) + " 之一")
    return json.dumps(spec, ensure_ascii=False)


def ensure_tables():
    from .db import conn as _conn, _lock
    with _lock:
        c = _conn()
        c.executescript(_SCHEMA)
        n = c.execute("SELECT COUNT(*) FROM widgets WHERE builtin=1").fetchone()[0]
        if n == 0:
            t = _now()
            for w in BUILTIN_WIDGETS:
                c.execute(
                    "INSERT INTO widgets(name,category,description,spec_json,builtin,creator,"
                    "created_at,updated_at) VALUES(?,?,?,?,1,'AgentForge 平台',?,?)",
                    (w["name"], w["category"], w["description"],
                     json.dumps(w["spec"], ensure_ascii=False), t, t))
        c.commit()
        c.close()


def _row_to_widget(r):
    w = dict(r)
    try:
        w["spec"] = json.loads(w.pop("spec_json") or "{}")
    except Exception:
        w["spec"] = {}
    return w


def list_widgets(tab=None, cat=None, q=None, page=1, size=12):
    where, args = ["1=1"], []
    if tab == "builtin":
        where.append("builtin=1")
    elif tab == "custom":
        where.append("builtin=0")
    if cat and cat != "全部":
        where.append("category=?")
        args.append(cat)
    if q:
        where.append("(name LIKE ? OR description LIKE ?)")
        args += ["%" + q + "%", "%" + q + "%"]
    cond = " AND ".join(where)
    total = query_one(f"SELECT COUNT(*) AS c FROM widgets WHERE {cond}", args)["c"]
    size = max(1, min(60, int(size or 12)))
    page = max(1, int(page or 1))
    rows = query(f"SELECT * FROM widgets WHERE {cond} ORDER BY builtin DESC,updated_at DESC "
                 f"LIMIT ? OFFSET ?", args + [size, (page - 1) * size])
    return {"items": [_row_to_widget(r) for r in rows], "total": total, "page": page, "size": size}


def get_widget(wid):
    r = query_one("SELECT * FROM widgets WHERE id=?", (wid,))
    return _row_to_widget(r) if r else None


def create_widget(name, category="简单信息", description="", spec_json="{}", creator=""):
    spec = _validate_spec(spec_json)
    t = _now()
    wid = execute("INSERT INTO widgets(name,category,description,spec_json,builtin,creator,"
                  "created_at,updated_at) VALUES(?,?,?,?,0,?,?,?)",
                  (name, category, description, spec, creator or "", t, t))
    return wid


def update_widget(wid, name=None, category=None, description=None, spec_json=None):
    r = query_one("SELECT * FROM widgets WHERE id=?", (wid,))
    if not r:
        raise ValueError("Widget 不存在")
    sets, args = ["updated_at=?"], [_now()]
    if name is not None:
        sets.append("name=?"); args.append(name)
    if category is not None:
        sets.append("category=?"); args.append(category)
    if description is not None:
        sets.append("description=?"); args.append(description)
    if spec_json is not None:
        sets.append("spec_json=?"); args.append(_validate_spec(spec_json))
    execute("UPDATE widgets SET " + ",".join(sets) + " WHERE id=?", args + [wid])
    return get_widget(wid)


def delete_widget(wid):
    r = query_one("SELECT * FROM widgets WHERE id=?", (wid,))
    if not r:
        return False
    if r["builtin"]:
        raise ValueError("内置 Widget 不可删除，可「另存为」后编辑副本")
    execute("DELETE FROM widgets WHERE id=?", (wid,))
    return True


def mark_copy(wid):
    execute("UPDATE widgets SET copy_count=copy_count+1 WHERE id=?", (wid,))


def duplicate(wid, creator=""):
    w = get_widget(wid)
    if not w:
        raise ValueError("Widget 不存在")
    nid = create_widget(w["name"], w["category"], w["description"],
                        json.dumps(w["spec"], ensure_ascii=False), creator or "")
    return get_widget(nid)
