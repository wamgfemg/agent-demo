# AgentForge · 智能体开发管理平台

自建智能体开发与运行平台（FastAPI + SQLite + 原生 JS 单文件前端）。

## 功能
- **智能体构建**：角色/persona、技能、工具与 MCP Server 接入，版本管理与发布
- **业务工作台**：多轮对话（SSE 流式逐字输出）、对话历史
- **Widget**：对话中的交互卡片（表单/确认/信息/列表/进度五类），操作结果回传对话继续推理，内置 8 个模板
- **提示词模板**：21 个内置模板 + 自定义模板，自动版本管理与回滚
- **技能库 / 工具与 MCP**：内置工具注册 + MCP Server 管理
- **任务中心**：多单元并行执行、产物下载、重试/取消
- **运行监控**：token 用量、模型耗时、每日统计

## 目录结构
```
app/          后端（FastAPI）：api.py 路由 / db.py SQLite / llm.py OpenRouter 客户端 /
              ptpl.py 提示词模板 / widgetlib.py Widget / pipeline.py 任务编排
web/          前端（单文件 index.html）
agents/       智能体定义（YAML）
skills/       技能文档
knowledge/    内置知识库文档
mcp_demo/     演示 MCP Server
deploy/       systemd 单元参考
```

## 部署
```bash
python3 -m venv venv && venv/bin/pip install fastapi uvicorn httpx pydantic
sudo cp deploy/agentforge.service /etc/systemd/system/  # 修改 token
sudo systemctl enable --now agentforge
```
数据（SQLite、日志、产物）落在 data/ 与 logs/，不入库。

## LLM 配置
登录后在「设置」里配置 OpenRouter API Key 与模型（存 data/agentforge.db）。
# sync test 1789651264
