# -*- coding: utf-8 -*-
"""长期记忆模块（参照腾讯云 ADP 长期记忆设计）。

机制：每个「操作者 × 智能体」一份唯一记忆；对话结束后异步让 LLM 增量总结沉淀；
新对话发起时召回注入 system prompt。
"""
import time
from .db import execute, query_one

_SCHEMA = """
CREATE TABLE IF NOT EXISTS memories(
  scope TEXT PRIMARY KEY,
  agent_id TEXT DEFAULT '',
  operator_id TEXT DEFAULT '',
  content TEXT DEFAULT '',
  updated_at REAL
);
"""

MAX_MEMORY_CHARS = 1200


def _now():
    return time.time()


def scope_key(operator_id, agent_id):
    return "%s::%s" % ((operator_id or "default"), (agent_id or "default"))


def ensure_tables():
    from .db import conn as _conn, _lock
    with _lock:
        c = _conn()
        c.executescript(_SCHEMA)
        c.commit()
        c.close()


def get_memory(operator_id, agent_id):
    r = query_one("SELECT content,updated_at FROM memories WHERE scope=?",
                  (scope_key(operator_id, agent_id),))
    return r["content"] if r else ""


def set_memory(operator_id, agent_id, content):
    sc = scope_key(operator_id, agent_id)
    t = _now()
    execute("INSERT INTO memories(scope,agent_id,operator_id,content,updated_at) "
            "VALUES(?,?,?,?,?) ON CONFLICT(scope) DO UPDATE SET content=excluded.content,"
            "updated_at=excluded.updated_at",
            (sc, agent_id or "", operator_id or "", (content or "")[:MAX_MEMORY_CHARS * 2], t))


def format_context(content):
    if not (content or "").strip():
        return ""
    return ("【用户长期记忆】以下是系统沉淀的该用户画像与偏好，回答时自然利用，"
            "不要向用户复述记忆本身：\n" + content.strip())


def summarize_update(operator_id, agent_id, user_msg, assistant_msg, llm_chat):
    """对话后异步调用：由 LLM 增量更新记忆。llm_chat = llm.chat(system, user)。"""
    old = get_memory(operator_id, agent_id)
    system = ("你是记忆管理器。根据【现有记忆】和【最新对话】，输出更新后的完整记忆。"
              "只保留长期有价值的信息（用户画像、偏好、业务背景、重要事实），"
              "删除过时内容，控制在500字以内，用简洁的条目式中文，不要客套。"
              "若最新对话没有值得沉淀的信息，原样输出现有记忆。")
    user = ("【现有记忆】\n%s\n\n【最新对话】\n用户：%s\n助手：%s\n\n请输出更新后的完整记忆："
            % (old or "（无）", (user_msg or "")[:1500], (assistant_msg or "")[:1500]))
    try:
        r = llm_chat(system, user)
        new = (r.content or "").strip()
        if new and len(new) < MAX_MEMORY_CHARS * 2:
            set_memory(operator_id, agent_id, new)
            return new
    except Exception:
        pass
    return old
