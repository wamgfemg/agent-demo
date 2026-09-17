# -*- coding: utf-8 -*-
"""Skill（技能包）：可复用的领域知识与作业规范，以 Markdown 承载。

一个 skill 就是一个 skills/*.md 文件：
    ---
    name: 投标技术方案编写规范
    description: 标书类文档的章节组织、表述口径与禁用写法
    version: 1.0
    ---
    （正文：写给模型看的作业指令）

智能体 YAML 里 `skills: [bid_writing, ...]` 引用文件名（不含扩展名），
编排时正文会被拼进 system prompt。改 md 即改智能体行为，无需动代码。
"""
import os, re, time
from .config import SKILL_DIR

_FM = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.S)


def _parse(path):
    raw = open(path, "r", encoding="utf-8").read()
    meta, body = {}, raw
    m = _FM.match(raw)
    if m:
        for line in m.group(1).splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                meta[k.strip()] = v.strip().strip('"').strip("'")
        body = raw[m.end():]
    return meta, body.strip()


def list_skills():
    out = []
    if not os.path.isdir(SKILL_DIR):
        return out
    for fn in sorted(os.listdir(SKILL_DIR)):
        if not fn.endswith((".md", ".markdown")):
            continue
        sid = os.path.splitext(fn)[0]
        try:
            meta, body = _parse(os.path.join(SKILL_DIR, fn))
        except Exception as e:
            out.append({"id": sid, "file": fn, "name": sid, "description": f"解析失败：{e}",
                        "chars": 0, "version": "-"})
            continue
        out.append({"id": sid, "file": fn,
                    "name": meta.get("name") or sid,
                    "description": meta.get("description") or "",
                    "version": meta.get("version") or "1.0",
                    "chars": len(body), "updated_at": os.path.getmtime(os.path.join(SKILL_DIR, fn))})
    return out


def get_skill(sid):
    for ext in (".md", ".markdown"):
        p = os.path.join(SKILL_DIR, sid + ext)
        if os.path.exists(p):
            meta, body = _parse(p)
            return {"id": sid, "meta": meta, "body": body, "file": os.path.basename(p)}
    return None


def render(skill_ids):
    """把若干 skill 正文拼成 system prompt 片段。未找到的 skill 明确标注，便于排查。"""
    if not skill_ids:
        return ""
    blocks = []
    for sid in skill_ids:
        s = get_skill(sid)
        if not s:
            blocks.append(f"<!-- 技能缺失：{sid} -->")
            continue
        blocks.append(f"【技能：{s['meta'].get('name') or sid}】\n{s['body']}")
    return "\n\n".join(blocks)


def save_skill(sid, name, description, body, version="1.0"):
    os.makedirs(SKILL_DIR, exist_ok=True)
    p = os.path.join(SKILL_DIR, sid + ".md")
    text = f"---\nname: {name or sid}\ndescription: {description or ''}\nversion: {version}\n---\n\n{body or ''}\n"
    with open(p, "w", encoding="utf-8") as f:
        f.write(text)
    return {"id": sid, "path": p, "chars": len(body or "")}


def delete_skill(sid):
    for ext in (".md", ".markdown"):
        p = os.path.join(SKILL_DIR, sid + ext)
        if os.path.exists(p):
            os.remove(p)
            return True
    return False
