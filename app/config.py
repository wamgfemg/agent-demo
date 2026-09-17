# -*- coding: utf-8 -*-
"""AgentForge 全局配置。所有路径与运行参数集中在此，便于单机迁移。"""
import os

BASE_DIR = os.environ.get("AGENTFORGE_HOME", "/opt/agentforge")
DATA_DIR = os.path.join(BASE_DIR, "data")
ARTIFACT_DIR = os.path.join(DATA_DIR, "artifacts")
AGENT_DIR = os.path.join(BASE_DIR, "agents")
WEB_DIR = os.path.join(BASE_DIR, "web")
SKILL_DIR = os.path.join(BASE_DIR, "skills")
KNOWLEDGE_DIR = os.path.join(DATA_DIR, "knowledge")
DB_PATH = os.path.join(DATA_DIR, "agentforge.db")

DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_MODEL = "openai/gpt-4o-mini"

MAX_PARALLEL_UNITS = int(os.environ.get("AF_MAX_PARALLEL", "3"))
UNIT_TIMEOUT_SEC = int(os.environ.get("AF_UNIT_TIMEOUT", "300"))
MAX_RETRY_PER_UNIT = 2
SANDBOX_TIMEOUT_SEC = 10

for d in (DATA_DIR, ARTIFACT_DIR, AGENT_DIR, WEB_DIR, SKILL_DIR, KNOWLEDGE_DIR):
    os.makedirs(d, exist_ok=True)
