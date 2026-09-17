# -*- coding: utf-8 -*-
"""知识库模块（参照腾讯云 ADP 知识库设计）。

RAG 流程：文档导入 → 切分 → 词法索引（bigram+英文词，无需 embedding）→
检索召回 → 注入对话上下文 → 带来源引用回答。

支持类型：md/txt（直接读）、docx（python-docx）、pdf（pypdf）、html（去标签）。
"""
import os, io, json, re, time, math
from .db import execute, query, query_one

_SCHEMA = """
CREATE TABLE IF NOT EXISTS kbs(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  description TEXT DEFAULT '',
  created_at REAL,
  updated_at REAL
);
CREATE TABLE IF NOT EXISTS kb_docs(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kb_id INTEGER NOT NULL,
  name TEXT NOT NULL,
  ext TEXT DEFAULT '',
  size INTEGER DEFAULT 0,
  chunk_count INTEGER DEFAULT 0,
  status TEXT DEFAULT 'ready',
  created_at REAL
);
CREATE TABLE IF NOT EXISTS kb_chunks(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kb_id INTEGER NOT NULL,
  doc_id INTEGER NOT NULL,
  seq INTEGER DEFAULT 0,
  heading TEXT DEFAULT '',
  content TEXT DEFAULT '',
  terms TEXT DEFAULT '{}',
  created_at REAL
);
CREATE INDEX IF NOT EXISTS idx_kbc_kb ON kb_chunks(kb_id);
CREATE INDEX IF NOT EXISTS idx_kbd_kb ON kb_docs(kb_id);
"""

_STOP = {"的", "了", "是", "在", "与", "和", "及", "对", "为", "以", "并", "等", "中", "上", "下",
         "我们", "可以", "进行", "一个", "什么", "如何", "哪些", "这个", "那个", "以及", "通过",
         "请", "吗", "呢", "吧", "有", "和", "或", "不", "人", "在"}

MAX_KB = 20          # 知识库上限
MAX_DOC_MB = 20      # 单文档上限
CHUNK_SIZE = 600     # 目标块大小（字符）
CHUNK_OVERLAP = 100


def _now():
    return time.time()


def _terms(text):
    """中文二元词 + 英文/数字词，返回集合。"""
    out = set()
    for w in re.findall(r"[\u4e00-\u9fa5]+", (text or "").lower()):
        if len(w) == 1:
            out.add(w)
        else:
            for i in range(len(w) - 1):
                t = w[i:i + 2]
                if t not in _STOP:
                    out.add(t)
    out |= set(re.findall(r"[a-z0-9]{2,}", (text or "").lower()))
    return out


# ------------------------------------------------------------------ 建表与种子
def ensure_tables():
    from .db import conn as _conn, _lock
    from .config import KNOWLEDGE_DIR
    with _lock:
        c = _conn()
        c.executescript(_SCHEMA)
        # 种子：平台内置知识库（导入 knowledge/*.md，仅首次）
        n = c.execute("SELECT COUNT(*) FROM kbs").fetchone()[0]
        if n == 0 and os.path.isdir(KNOWLEDGE_DIR):
            t = _now()
            kid = c.execute("INSERT INTO kbs(name,description,created_at,updated_at) "
                            "VALUES(?,?,?,?)",
                            ("平台内置知识库", "内置的售前交付知识（knowledge/ 目录）", t, t)).lastrowid
            for fn in sorted(os.listdir(KNOWLEDGE_DIR)):
                if fn.endswith((".md", ".txt", ".markdown")):
                    try:
                        text = open(os.path.join(KNOWLEDGE_DIR, fn), encoding="utf-8",
                                    errors="replace").read()
                    except Exception:
                        continue
                    chunks = _chunk_text(text)
                    did = c.execute("INSERT INTO kb_docs(kb_id,name,ext,size,chunk_count,"
                                    "status,created_at) VALUES(?,?,?,?,?,?,?)",
                                    (kid, fn, ".md", len(text), len(chunks), "ready", t)).lastrowid
                    for i, (heading, body) in enumerate(chunks):
                        c.execute("INSERT INTO kb_chunks(kb_id,doc_id,seq,heading,content,terms,"
                                  "created_at) VALUES(?,?,?,?,?,?,?)",
                                  (kid, did, i, heading, body,
                                   json.dumps(_term_counts(body), ensure_ascii=False), t))
        c.commit()
        c.close()


def _term_counts(text):
    cnt = {}
    for t in _terms(text):
        cnt[t] = cnt.get(t, 0) + 1
    return cnt


def _chunk_text(text):
    """按标题/空行切块，合并到 CHUNK_SIZE，带 overlap。返回 [(heading, body), ...]"""
    lines = (text or "").split("\n")
    blocks, cur, cur_head = [], [], ""
    def flush():
        if cur:
            body = "\n".join(cur).strip()
            if body:
                blocks.append((cur_head, body))
    for ln in lines:
        m = re.match(r"^(#{1,4})\s+(.*)$", ln.strip())
        if m:
            flush()
            cur, cur_head = [], m.group(2).strip()
            continue
        if not ln.strip():
            flush()
            cur = []
            continue
        cur.append(ln)
    flush()
    # 合并过短块 / 拆分过长块
    merged, buf, buf_head = [], [], ""
    for head, body in blocks:
        if buf and len("\n".join(buf)) + len(body) > CHUNK_SIZE:
            merged.append((buf_head, "\n".join(buf)))
            buf = buf[-1:]  # 保留一段做 overlap
            buf_head = head
        if not buf:
            buf_head = head
        buf.append(body)
        while len("\n".join(buf)) > CHUNK_SIZE * 2:  # 单块超长，硬切
            big = "\n".join(buf)
            merged.append((buf_head, big[:CHUNK_SIZE * 2]))
            buf = [big[CHUNK_SIZE * 2 - CHUNK_OVERLAP:]]
    if buf:
        merged.append((buf_head, "\n".join(buf)))
    return [(h, b[:2000]) for h, b in merged if b.strip()]


# ------------------------------------------------------------------ 文档解析
def extract_text(filename, data):
    """按扩展名解析文件字节 → 纯文本。失败抛 ValueError。"""
    name = (filename or "").lower()
    if name.endswith((".md", ".txt", ".markdown", ".log", ".csv")):
        return data.decode("utf-8", "replace")
    if name.endswith(".docx"):
        import docx
        d = docx.Document(io.BytesIO(data))
        parts = []
        for p in d.paragraphs:
            if p.text and p.text.strip():
                parts.append(p.text.strip())
        for tb in d.tables:  # 表格转行文本
            for row in tb.rows:
                cells = [c.text.strip() for c in row.cells if c.text.strip()]
                if cells:
                    parts.append(" | ".join(cells))
        if not parts:
            raise ValueError("docx 无可提取文本（可能为扫描件）")
        return "\n\n".join(parts)
    if name.endswith(".pdf"):
        try:
            from pypdf import PdfReader
        except ImportError:
            raise ValueError("服务器未安装 pypdf，暂不支持 PDF")
        r = PdfReader(io.BytesIO(data))
        pages = []
        for pg in r.pages:
            try:
                pages.append(pg.extract_text() or "")
            except Exception:
                continue
        text = "\n\n".join(p for p in pages if p.strip())
        if not text.strip():
            raise ValueError("PDF 无可提取文本（可能为扫描件，需 OCR）")
        return text
    if name.endswith((".html", ".htm")):
        text = data.decode("utf-8", "replace")
        text = re.sub(r"<script[\s\S]*?</script>|<style[\s\S]*?</style>", "", text, flags=re.I)
        text = re.sub(r"<[^>]+>", " ", text)
        import html as _h
        return _h.unescape(re.sub(r"\s{2,}", " ", text))
    if name.endswith(".doc"):
        raise ValueError("旧版 .doc 不支持，请另存为 .docx 后上传")
    raise ValueError("不支持的文件类型：" + os.path.splitext(name)[1])


# ------------------------------------------------------------------ CRUD
def list_kbs():
    kbs = query("SELECT k.*,(SELECT COUNT(*) FROM kb_docs d WHERE d.kb_id=k.id) AS doc_count,"
                "(SELECT COUNT(*) FROM kb_chunks c WHERE c.kb_id=k.id) AS chunk_count "
                "FROM kbs k ORDER BY k.id")
    return kbs


def create_kb(name, description=""):
    if query_one("SELECT COUNT(*) AS c FROM kbs")["c"] >= MAX_KB:
        raise ValueError("知识库数量已达上限（%d）" % MAX_KB)
    t = _now()
    return execute("INSERT INTO kbs(name,description,created_at,updated_at) VALUES(?,?,?,?)",
                   (name.strip(), description.strip(), t, t))


def get_kb(kid):
    return query_one("SELECT * FROM kbs WHERE id=?", (kid,))


def update_kb(kid, name=None, description=None):
    sets, args = ["updated_at=?"], [_now()]
    if name is not None:
        sets.append("name=?"); args.append(name.strip())
    if description is not None:
        sets.append("description=?"); args.append(description.strip())
    execute("UPDATE kbs SET " + ",".join(sets) + " WHERE id=?", args + [kid])


def delete_kb(kid):
    execute("DELETE FROM kb_chunks WHERE kb_id=?", (kid,))
    execute("DELETE FROM kb_docs WHERE kb_id=?", (kid,))
    execute("DELETE FROM kbs WHERE id=?", (kid,))


def list_docs(kid):
    return query("SELECT id,name,ext,size,chunk_count,status,created_at FROM kb_docs "
                 "WHERE kb_id=? ORDER BY id DESC", (kid,))


def add_doc(kid, filename, data):
    """解析 + 切分 + 入库。返回 doc_id, chunk 数。"""
    if not get_kb(kid):
        raise ValueError("知识库不存在")
    if len(data) > MAX_DOC_MB * 1024 * 1024:
        raise ValueError("文件超过 %dMB 上限" % MAX_DOC_MB)
    text = extract_text(filename, data)
    chunks = _chunk_text(text)
    if not chunks:
        raise ValueError("未解析出有效内容")
    t = _now()
    did = execute("INSERT INTO kb_docs(kb_id,name,ext,size,chunk_count,status,created_at) "
                  "VALUES(?,?,?,?,?,?,?)",
                  (kid, os.path.basename(filename), os.path.splitext(filename)[1].lower(),
                   len(data), len(chunks), "ready", t))
    for i, (heading, body) in enumerate(chunks):
        execute("INSERT INTO kb_chunks(kb_id,doc_id,seq,heading,content,terms,created_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (kid, did, i, heading, body,
                 json.dumps(_term_counts(body), ensure_ascii=False), t))
    execute("UPDATE kbs SET updated_at=? WHERE id=?", (t, kid))
    return did, len(chunks)


def delete_doc(kid, did):
    execute("DELETE FROM kb_chunks WHERE doc_id=?", (did,))
    execute("DELETE FROM kb_docs WHERE id=? AND kb_id=?", (did, kid))


# ------------------------------------------------------------------ 检索
def search(kb_ids, query_text, top_k=5):
    """多库检索：bigram 词频 × IDF 打分。返回 [{doc,heading,score,snippet}]"""
    if isinstance(kb_ids, int):
        kb_ids = [kb_ids]
    kb_ids = [int(k) for k in (kb_ids or []) if k]
    if not kb_ids or not (query_text or "").strip():
        return []
    qterms = _terms(query_text)
    if not qterms:
        return []
    ph = ",".join("?" * len(kb_ids))
    rows = query(f"SELECT c.content,c.terms,c.heading,d.name AS doc FROM kb_chunks c "
                 f"JOIN kb_docs d ON d.id=c.doc_id WHERE c.kb_id IN ({ph})", kb_ids)
    if not rows:
        return []
    total = len(rows)
    df = {}
    for r in rows:
        try:
            tc = json.loads(r["terms"])
        except Exception:
            tc = {}
        r["_tc"] = tc
        for t in qterms:
            if t in tc:
                df[t] = df.get(t, 0) + 1
    scored = []
    for r in rows:
        s = 0.0
        for t in qterms:
            cnt = r["_tc"].get(t, 0)
            if cnt:
                idf = math.log(1 + total / df[t])
                s += cnt * idf
        if s > 0:
            scored.append((s, r))
    scored.sort(key=lambda x: -x[0])
    out = []
    for s, r in scored[:max(1, min(20, top_k))]:
        content = r["content"]
        # 摘录命中片段
        pos = -1
        for t in qterms:
            p = content.lower().find(t)
            if p >= 0 and (pos < 0 or p < pos):
                pos = p
        if pos < 0:
            pos = 0
        snip = content[max(0, pos - 80): pos + 500]
        out.append({"doc": r["doc"], "heading": r["heading"], "score": round(s, 2),
                    "snippet": snip})
    return out


def format_context(hits):
    """把检索结果编排成注入 system 的参考材料文本。"""
    if not hits:
        return ""
    parts = ["【参考资料】以下是从知识库检索到的内容，回答时优先依据这些材料，"
             "并在关键结论后标注来源文档名；材料中没有的信息如实说明。"]
    for i, h in enumerate(hits, 1):
        parts.append("[{i}] 来源：{doc}{head}\n{snip}".format(
            i=i, doc=h["doc"], head=(" · " + h["heading"]) if h["heading"] else "",
            snip=h["snippet"]))
    return "\n\n".join(parts)
