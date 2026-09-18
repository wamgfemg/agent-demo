# -*- coding: utf-8 -*-
"""应用评测模块（参照腾讯云 ADP 应用评测设计）。

评测集（用例：输入 + 参考答案/评分要点）→ 批量运行（按智能体真实配置生成回复，
含知识库注入）→ LLM 裁判对比打分（0-10 + 理由）→ 平均分与历史记录。
"""
import json, time
from .db import execute, query, query_one

_SCHEMA = """
CREATE TABLE IF NOT EXISTS eval_cases(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  agent_id TEXT NOT NULL,
  name TEXT DEFAULT '',
  input TEXT NOT NULL,
  expected TEXT DEFAULT '',
  created_at REAL
);
CREATE INDEX IF NOT EXISTS idx_ecase_agent ON eval_cases(agent_id);
CREATE TABLE IF NOT EXISTS eval_runs(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  agent_id TEXT NOT NULL,
  model TEXT DEFAULT '',
  status TEXT DEFAULT 'running',
  case_count INTEGER DEFAULT 0,
  avg_score REAL DEFAULT 0,
  results_json TEXT DEFAULT '[]',
  error TEXT DEFAULT '',
  created_at REAL,
  finished_at REAL
);
CREATE INDEX IF NOT EXISTS idx_erun_agent ON eval_runs(agent_id);
"""


def _now():
    return time.time()


def ensure_tables():
    from .db import conn as _conn, _lock
    with _lock:
        c = _conn()
        c.executescript(_SCHEMA)
        c.commit()
        c.close()


# ------------------------------------------------------------------ 用例 CRUD
def list_cases(agent_id):
    return query("SELECT * FROM eval_cases WHERE agent_id=? ORDER BY id", (agent_id,))


def add_case(agent_id, name, inp, expected=""):
    if not (inp or "").strip():
        raise ValueError("输入不能为空")
    return execute("INSERT INTO eval_cases(agent_id,name,input,expected,created_at) "
                   "VALUES(?,?,?,?,?)",
                   (agent_id, (name or "").strip() or inp[:30], inp.strip(), expected.strip(),
                    _now()))


def update_case(cid, name=None, inp=None, expected=None):
    r = query_one("SELECT * FROM eval_cases WHERE id=?", (cid,))
    if not r:
        raise ValueError("用例不存在")
    sets, args = [], []
    if name is not None:
        sets.append("name=?"); args.append(name)
    if inp is not None:
        if not inp.strip():
            raise ValueError("输入不能为空")
        sets.append("input=?"); args.append(inp.strip())
    if expected is not None:
        sets.append("expected=?"); args.append(expected.strip())
    if sets:
        execute("UPDATE eval_cases SET " + ",".join(sets) + " WHERE id=?", args + [cid])


def delete_case(cid):
    execute("DELETE FROM eval_cases WHERE id=?", (cid,))


# ------------------------------------------------------------------ 批量运行
def run_eval(agent_id, llm_chat, kb_search=None, agent_spec=None):
    """执行评测。llm_chat(system,user)->result；kb_search(ids,q,k)->context_str。
    agent_spec: registry.get_published_or_draft(agent_id)，为 None 时尝试用草稿。"""
    cases = list_cases(agent_id)
    if not cases:
        raise ValueError("该智能体还没有评测用例")
    persona = (agent_spec or {}).get("persona") or "你是一个有帮助的AI助手。请用中文回答。"
    kb_ids = (agent_spec or {}).get("knowledge_bases") or []
    t0 = _now()
    rid = execute("INSERT INTO eval_runs(agent_id,status,case_count,created_at) "
                  "VALUES(?, 'running', ?, ?)", (agent_id, len(cases), t0))
    results = []
    scores = []
    try:
        for c in cases:
            ts = time.time()
            system = persona
            if kb_ids and kb_search:
                try:
                    ctx = kb_search(kb_ids, c["input"], 5)
                    if ctx:
                        system += "\n\n" + ctx
                except Exception:
                    pass
            r = llm_chat(system, c["input"])
            reply = r.content or ""
            # 裁判打分
            if (c["expected"] or "").strip():
                judge_sys = ("你是严格的评测裁判。对比【参考答案/评分要点】与【模型回答】，"
                             "输出严格 JSON：{\"score\": 0-10整数, \"reason\": \"一句话点评\"}。"
                             "关键点全部覆盖且无误=9-10；覆盖但有瑕疵=7-8；"
                             "遗漏或错误=0-6。只输出 JSON。")
                judge_user = ("【参考答案/评分要点】\n%s\n\n【模型回答】\n%s"
                              % (c["expected"], reply[:3000]))
                try:
                    jr = llm_chat(judge_sys, judge_user)
                    txt = (jr.content or "").strip()
                    if txt.startswith("```"):
                        txt = txt.split("```")[1]
                        if txt.startswith("json"):
                            txt = txt[4:]
                    s, e = txt.find("{"), txt.rfind("}")
                    j = json.loads(txt[s:e + 1])
                    score = max(0, min(10, int(j.get("score", 0))))
                    reason = (j.get("reason") or "")[:200]
                except Exception:
                    score, reason = -1, "裁判解析失败"
            else:
                score, reason = -1, "（无参考答案，未评分）"
            if score >= 0:
                scores.append(score)
            results.append({"case_id": c["id"], "name": c["name"],
                            "input": c["input"][:300], "reply": reply[:1000],
                            "expected": (c["expected"] or "")[:300],
                            "score": score, "reason": reason,
                            "ms": int((time.time() - ts) * 1000),
                            "tokens_in": r.tokens_in, "tokens_out": r.tokens_out})
        avg = round(sum(scores) / len(scores), 2) if scores else -1
        execute("UPDATE eval_runs SET status='done',model=?,avg_score=?,results_json=?,"
                "finished_at=? WHERE id=?",
                (r.model, avg, json.dumps(results, ensure_ascii=False), _now(), rid))
        return {"ok": True, "run_id": rid, "avg_score": avg,
                "case_count": len(cases), "results": results,
                "ms": int((time.time() - t0) * 1000)}
    except Exception as e:
        execute("UPDATE eval_runs SET status='failed',error=?,finished_at=? WHERE id=?",
                (str(e)[:400], _now(), rid))
        raise


def list_runs(agent_id, limit=20):
    return query("SELECT id,agent_id,model,status,case_count,avg_score,error,created_at,"
                 "finished_at FROM eval_runs WHERE agent_id=? ORDER BY id DESC LIMIT ?",
                 (agent_id, limit))


def get_run(run_id):
    r = query_one("SELECT * FROM eval_runs WHERE id=?", (run_id,))
    if not r:
        return None
    try:
        r["results"] = json.loads(r.pop("results_json") or "[]")
    except Exception:
        r["results"] = []
    return r


def delete_run(run_id):
    execute("DELETE FROM eval_runs WHERE id=?", (run_id,))
