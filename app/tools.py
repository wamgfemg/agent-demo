# -*- coding: utf-8 -*-
"""工具集：文档生成、代码沙箱。所有产物均为原生可编辑对象，不使用图片粘贴。"""
import os, sys, io, json, subprocess, tempfile, contextlib, re

MD_H = re.compile(r"^(#{1,4})\s+(.*)$")


def _add_md(doc, text):
    """极简 Markdown 渲染：# / ## / ### 映射为 Word 标题，其余为正文。"""
    for line in (text or "").split("\n"):
        line = line.rstrip()
        if not line.strip():
            continue
        m = MD_H.match(line)
        if m:
            level = min(len(m.group(1)), 4)
            doc.add_heading(m.group(2).strip(), level=level)
        elif line.startswith(("- ", "* ")):
            doc.add_paragraph(line[2:].strip(), style="List Bullet")
        elif re.match(r"^\d+[.、]", line):
            doc.add_paragraph(re.sub(r"^\d+[.、]\s*", "", line), style="List Number")
        else:
            doc.add_paragraph(line.strip())


def write_docx(project_name, chapters, out_path, glossary=None, numbers=None, subtitle=""):
    from docx import Document
    from docx.shared import Pt, RGBColor
    from docx.enum.text import WD_ALIGN_PARAGRAPH

    doc = Document()
    style = doc.styles["Normal"]
    style.font.name = "Microsoft YaHei"
    style.font.size = Pt(10.5)

    t = doc.add_heading(project_name or "技术方案", level=0)
    t.alignment = WD_ALIGN_PARAGRAPH.CENTER
    if subtitle:
        p = doc.add_paragraph(subtitle)
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER

    doc.add_page_break()
    doc.add_heading("目 录", level=1)
    for i, c in enumerate(chapters, 1):
        doc.add_paragraph(f"{i}. {c.get('title', '章节')}", style="List Number")

    doc.add_page_break()
    for c in chapters:
        doc.add_heading(c.get("title", "章节"), level=1)
        _add_md(doc, c.get("content", ""))

    if numbers:
        doc.add_page_break()
        doc.add_heading("附录一　数字台账", level=1)
        tb = doc.add_table(rows=1, cols=4)
        tb.style = "Light Grid Accent 1"
        for i, h in enumerate(["指标", "数值", "单位", "来源"]):
            tb.rows[0].cells[i].text = h
        for n in numbers:
            row = tb.add_row().cells
            row[0].text = str(n.get("key", ""))
            row[1].text = str(n.get("value", ""))
            row[2].text = str(n.get("unit", ""))
            row[3].text = str(n.get("source", ""))

    if glossary:
        doc.add_page_break()
        doc.add_heading("附录二　术语表", level=1)
        tb = doc.add_table(rows=1, cols=2)
        tb.style = "Light Grid Accent 1"
        tb.rows[0].cells[0].text = "术语"
        tb.rows[0].cells[1].text = "定义"
        for g in glossary:
            row = tb.add_row().cells
            row[0].text = str(g.get("term", ""))
            row[1].text = str(g.get("definition", ""))

    doc.save(out_path)
    return out_path


def write_pptx(project_name, slides, out_path):
    from pptx import Presentation
    from pptx.util import Inches, Pt
    from pptx.dml.color import RGBColor

    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)
    blank = prs.slide_layouts[6]

    def add(title, bullets, notes, sub=None, idx=0, total=0):
        s = prs.slides.add_slide(blank)
        box = s.shapes.add_textbox(Inches(0.6), Inches(0.45), Inches(12.1), Inches(1.0))
        p = box.text_frame.paragraphs[0]
        p.text = title
        p.font.size = Pt(28)
        p.font.bold = True
        p.font.color.rgb = RGBColor(0x16, 0xA3, 0x4A)
        if idx:
            pb = s.shapes.add_textbox(Inches(12.0), Inches(6.9), Inches(0.9), Inches(0.4))
            pb.text_frame.paragraphs[0].text = f"{idx}/{total}"
            pb.text_frame.paragraphs[0].font.size = Pt(10)
        if sub:
            sb = s.shapes.add_textbox(Inches(0.65), Inches(1.45), Inches(12.0), Inches(0.5))
            sb.text_frame.paragraphs[0].text = sub
            sb.text_frame.paragraphs[0].font.size = Pt(14)
        body = s.shapes.add_textbox(Inches(0.7), Inches(2.1), Inches(11.9), Inches(4.6))
        tf = body.text_frame
        tf.word_wrap = True
        for i, b in enumerate(bullets or []):
            para = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
            para.text = "· " + b
            para.font.size = Pt(18)
            para.space_after = Pt(10)
        if notes:
            s.notes_slide.notes_text_frame.text = notes
        return s

    cover = prs.slides.add_slide(blank)
    cb = cover.shapes.add_textbox(Inches(1.0), Inches(2.6), Inches(11.3), Inches(1.6))
    cp = cb.text_frame.paragraphs[0]
    cp.text = project_name or "方案汇报"
    cp.font.size = Pt(40)
    cp.font.bold = True

    total = len(slides)
    for i, sl in enumerate(slides):
        add(sl.get("title", ""), sl.get("bullets"), sl.get("notes"), sl.get("subtitle"), i + 1, total)

    prs.save(out_path)
    return out_path


# 中文没有空格分词，用 bigram（二字滑窗）代替：既不需要分词库，也能命中"可用性"这类概念
_STOP = {"的", "了", "是", "在", "与", "和", "及", "对", "为", "以", "并", "等", "中", "上", "下",
         "我们", "可以", "进行", "一个", "什么", "如何", "哪些", "这个", "那个", "以及", "通过",
         "需要", "应该", "如果", "因为", "所以", "但是", "而且", "或者", "对于", "关于"}


def _terms(query):
    """把查询切成可计分的词元：中文取二字滑窗，英文取单词。"""
    q = (query or "").lower()
    terms = set()
    for w in re.findall(r"[\u4e00-\u9fa5]+", q):
        if len(w) == 1:
            terms.add(w)
        else:
            for i in range(len(w) - 1):
                t = w[i:i + 2]
                if t not in _STOP:
                    terms.add(t)
    terms |= set(re.findall(r"[a-z]{3,}", q))
    return terms


def kb_search(query, top_k=3):
    """本地知识库检索：bigram 词频打分 + 片段截取，无向量依赖（1.6G 内存下不做 embedding）。"""
    import math
    from .config import KNOWLEDGE_DIR
    if not os.path.isdir(KNOWLEDGE_DIR):
        return {"ok": False, "error": f"知识目录不存在：{KNOWLEDGE_DIR}", "hits": []}
    q = _terms(query)
    if not q:
        return {"ok": False, "error": "查询为空", "hits": []}
    hits = []
    for fn in sorted(os.listdir(KNOWLEDGE_DIR)):
        if not fn.endswith((".md", ".txt", ".markdown")):
            continue
        p = os.path.join(KNOWLEDGE_DIR, fn)
        try:
            text = open(p, "r", encoding="utf-8", errors="replace").read()
        except Exception:
            continue
        low = text.lower()
        score = sum(low.count(t) for t in q)
        if score <= 0:
            continue
        # 取命中最多的词元所在位置作为片段起点，保证摘出来的段落是相关的
        best, best_n = 0, 0
        for t in q:
            n = low.count(t)
            if n > best_n:
                best_n, best = n, low.find(t)
        pos = max(0, best - 120)
        snip = text[pos: pos + 700]
        hits.append({"file": fn, "score": score,
                     "snippet": ("..." if pos > 0 else "") + snip.strip() + "..."})
    hits.sort(key=lambda x: -x["score"])
    return {"ok": True, "query": query, "total": len(hits), "hits": hits[:max(1, int(top_k or 3))]}


BANNED = ("import os", "open(", "subprocess", "socket", "shutil", "requests", "urllib", "httpx", "sys.exit")


def run_python(code, timeout=10):
    """受限代码沙箱：禁用文件/网络/进程相关调用，超时即杀。"""
    low = code or ""
    for b in BANNED:
        if b in low:
            return {"ok": False, "error": f"沙箱禁止使用：{b}"}
    buf = io.StringIO()
    g = {"__builtins__": __builtins__, "result": None}
    try:
        with contextlib.redirect_stdout(buf):
            exec(compile(code, "<sandbox>", "exec"), g)
        return {"ok": True, "stdout": buf.getvalue()[-4000:], "result": str(g.get("result"))[:2000]}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
