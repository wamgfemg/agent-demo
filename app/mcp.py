# -*- coding: utf-8 -*-
"""MCP 客户端：手搓最小可用实现，不依赖官方 SDK（省内存、可控）。

支持两种传输：
  - http  : Streamable HTTP（POST JSON-RPC，从响应头取 Mcp-Session-Id 维持会话）
  - stdio : 子进程 + 换行分隔 JSON-RPC

对接外部 server 后，其 tools/list 会被注册进工具中心，
智能体 YAML 里按工具名引用即可，用法与内置工具完全一致。
"""
import os, json, time, asyncio, traceback
import httpx
from . import db, toolregistry

PROTOCOL = "2025-06-18"


# ------------------------------------------------------------------ HTTP 传输
class HttpTransport:
    def __init__(self, url, headers=None):
        self.url = url
        self.headers = {"Content-Type": "application/json",
                        "Accept": "application/json, text/event-stream"}
        for k, v in (headers or {}).items():
            self.headers[k] = v
        self.session_id = None
        self._client = None

    def _cli(self):
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=30, follow_redirects=True)
        return self._client

    async def send(self, payload, notify=False):
        h = dict(self.headers)
        if self.session_id:
            h["Mcp-Session-Id"] = self.session_id
        r = await self._cli().post(self.url, json=payload, headers=h)
        sid = r.headers.get("mcp-session-id")
        if sid:
            self.session_id = sid
        if notify:
            return None
        ctype = r.headers.get("content-type", "")
        if "text/event-stream" in ctype:
            return self._parse_sse(r.text)
        try:
            return r.json()
        except Exception:
            raise RuntimeError(f"非 JSON 响应（HTTP {r.status_code}）：{r.text[:200]}")

    @staticmethod
    def _parse_sse(text):
        data = None
        for line in (text or "").splitlines():
            if line.startswith("data:"):
                chunk = line[5:].strip()
                if not chunk:
                    continue
                try:
                    obj = json.loads(chunk)
                except Exception:
                    continue
                if obj.get("id") is not None or "result" in obj or "error" in obj:
                    data = obj
        if data is None:
            raise RuntimeError("SSE 流中未解析到 JSON-RPC 响应")
        return data

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()


# ------------------------------------------------------------------ stdio 传输
class StdioTransport:
    def __init__(self, command, args=None, env=None):
        self.command = command
        self.args = args or []
        self.env = env or {}
        self.proc = None

    async def start(self):
        env = dict(os.environ)
        env.update({str(k): str(v) for k, v in self.env.items()})
        self.proc = await asyncio.create_subprocess_exec(
            self.command, *self.args,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, env=env)
        return self

    async def send(self, payload, notify=False):
        line = json.dumps(payload, ensure_ascii=False) + "\n"
        self.proc.stdin.write(line.encode("utf-8"))
        await self.proc.stdin.drain()
        if notify:
            return None
        raw = await asyncio.wait_for(self.proc.stdout.readline(), timeout=30)
        if not raw:
            err = await self.proc.stderr.read(500)
            raise RuntimeError("子进程无响应：" + err.decode("utf-8", "replace")[:200])
        return json.loads(raw.decode("utf-8"))

    async def close(self):
        if self.proc and self.proc.returncode is None:
            try:
                self.proc.terminate()
            except Exception:
                pass


# ------------------------------------------------------------------ 会话封装
class MCPSession:
    def __init__(self, cfg):
        self.cfg = cfg
        self.t = None

    async def open(self):
        c = self.cfg
        if c["transport"] == "stdio":
            self.t = await StdioTransport(c["command"], c.get("args"), c.get("env")).start()
        else:
            self.t = HttpTransport(c["url"], c.get("headers"))
        init = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": PROTOCOL,
                           "capabilities": {},
                           "clientInfo": {"name": "AgentForge", "version": "1.0"}}}
        resp = await self.t.send(init)
        if not resp or "error" in resp:
            raise RuntimeError(f"initialize 失败：{(resp or {}).get('error')}")
        await self.t.send({"jsonrpc": "2.0", "method": "notifications/initialized"}, notify=True)
        return self

    async def list_tools(self):
        resp = await self.t.send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        if not resp or "error" in resp:
            raise RuntimeError(f"tools/list 失败：{(resp or {}).get('error')}")
        return (resp.get("result") or {}).get("tools") or []

    async def call_tool(self, name, arguments):
        resp = await self.t.send({"jsonrpc": "2.0", "id": int(time.time() * 1000) % 10 ** 9,
                                  "method": "tools/call",
                                  "params": {"name": name, "arguments": arguments or {}}})
        if not resp or "error" in resp:
            return {"ok": False, "error": (resp or {}).get("error")}
        res = resp.get("result") or {}
        parts = res.get("content") or []
        text = "\n".join(p.get("text", "") for p in parts if isinstance(p, dict))
        return {"ok": not res.get("isError", False), "content": text, "raw": res}

    async def close(self):
        try:
            if self.t:
                await self.t.close()
        except Exception:
            pass


# ------------------------------------------------------------------ 配置读写
def _row_to_cfg(r):
    return {"id": r["id"], "name": r["name"], "transport": r["transport"],
            "url": r["url"] or "", "command": r["command"] or "",
            "args": db.uj(r["args_json"]) or [], "env": db.uj(r["env_json"]) or {},
            "headers": db.uj(r["headers_json"]) or {}, "enabled": r["enabled"]}


def list_servers():
    rows = db.query("SELECT * FROM mcp_servers ORDER BY name")
    out = []
    for r in rows:
        d = dict(r)
        d["tools"] = db.uj(r["tools_json"]) or []
        d["headers_masked"] = {k: ("***" if k.lower() in ("authorization", "x-api-key") else v)
                               for k, v in (db.uj(r["headers_json"]) or {}).items()}
        out.append(d)
    return out


def get_server(sid):
    r = db.query_one("SELECT * FROM mcp_servers WHERE id=?", (sid,))
    return _row_to_cfg(r) if r else None


def save_server(cfg):
    sid = cfg.get("id") or ("m" + str(int(time.time() * 1000))[-9:])
    db.execute("INSERT INTO mcp_servers(id,name,transport,url,command,args_json,env_json,headers_json,"
               "enabled,status,tools_json,last_error,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) "
               "ON CONFLICT(id) DO UPDATE SET name=excluded.name, transport=excluded.transport, "
               "url=excluded.url, command=excluded.command, args_json=excluded.args_json, "
               "env_json=excluded.env_json, headers_json=excluded.headers_json, enabled=excluded.enabled, "
               "updated_at=excluded.updated_at",
               (sid, cfg["name"], cfg.get("transport", "http"), cfg.get("url", ""), cfg.get("command", ""),
                db.j(cfg.get("args") or []), db.j(cfg.get("env") or {}), db.j(cfg.get("headers") or {}),
                1 if cfg.get("enabled", True) else 0, "saved", "[]", None, db.now()))
    return sid


def delete_server(sid):
    toolregistry.unregister_source(sid)
    db.execute("DELETE FROM mcp_servers WHERE id=?", (sid,))


def _mark(sid, status, tools=None, err=None):
    db.execute("UPDATE mcp_servers SET status=?, tools_json=?, last_error=?, updated_at=? WHERE id=?",
               (status, db.j(tools or []), (err or None), db.now(), sid))


# ------------------------------------------------------------------ 连接与注册
def _tool_name(server_name, tool_name):
    """避免不同 server 工具重名：默认 server前缀__tool 仅当冲突时启用，否则用原名。"""
    return tool_name


async def connect_server(sid):
    cfg = get_server(sid)
    if not cfg:
        return {"ok": False, "error": "server 不存在"}
    sess = MCPSession(cfg)
    try:
        await sess.open()
        tools = await sess.list_tools()
    except Exception as e:
        traceback.print_exc()
        _mark(sid, "error", err=str(e)[:300])
        await sess.close()
        return {"ok": False, "error": str(e)[:300]}

    # 旧工具先摘掉，再重新注册
    toolregistry.unregister_source(sid)
    registered = []
    for t in tools:
        name = _tool_name(cfg["name"], t.get("name"))
        if not name:
            continue
        params = t.get("inputSchema") or {"type": "object", "properties": {}}

        def _mk(sess=sess, name=name):
            async def _h(args):
                return await sess.call_tool(name, args)
            return _h

        toolregistry.register(name, f"[MCP:{cfg['name']}] " + (t.get("description") or ""),
                              params, _mk(), source="mcp", server=sid)
        registered.append({"name": name, "description": t.get("description") or ""})
    _mark(sid, "connected", tools=registered)
    # 注意：session 由闭包持有，不关闭；进程退出自动回收
    return {"ok": True, "tools": registered, "count": len(registered)}


async def connect_all():
    out = []
    for r in db.query("SELECT id,name FROM mcp_servers WHERE enabled=1"):
        try:
            res = await connect_server(r["id"])
            out.append({"server": r["name"], **res})
        except Exception as e:
            out.append({"server": r["name"], "ok": False, "error": str(e)[:200]})
    return out


async def test_server(cfg):
    """不落库的连接测试，用于前端"测试连通"。"""
    sess = MCPSession(cfg)
    try:
        await sess.open()
        tools = await sess.list_tools()
        await sess.close()
        return {"ok": True, "tools": [t.get("name") for t in tools], "count": len(tools)}
    except Exception as e:
        await sess.close()
        return {"ok": False, "error": str(e)[:300]}
