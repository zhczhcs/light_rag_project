# -*- coding: utf-8 -*-
"""
Phase 3 租户边界修复 —— 漏洞复现 / 修复验证脚本
================================================

用途
----
在无法启动完整服务（本机无 MySQL/Qdrant/LLM Key）的环境下，用 FastAPI TestClient
挂载【真实路由代码】（app/api/routers/chat.py、auth.py、chat_history.py），将
get_db 重定向到 SQLite 内存库、将 openai / app.rag.engine 等重依赖替换为可录制的
假实现，从而端到端复现两个越权漏洞：

  漏洞一（会话 IDOR）：user B 拿 user A 的 session_id 调 POST /api/chat，
      修复前可读取 A 的会话历史并写入消息/篡改标题。
  漏洞二（注册自选部门）：任意注册请求自带 department_id 直接落入该部门。

判定方式
--------
脚本只打印【观察到的行为】，不写死预期。修复前/后各运行一次，对比输出：

  修复前预期：攻击请求 200；受害者会话出现攻击者消息；标题被改；
              攻击者请求的上下文里出现受害者历史消息。
  修复后预期：攻击请求 404（会话不属于当前用户）；
              注册带 department_id 返回 422；新用户 department_id 为 NULL；
              正常问答（本人会话）不受影响。

运行
----
  python reproduce_tenant_bypass.py <输出报告路径.txt>

依赖：fastapi sqlalchemy pymysql pydantic[email] python-jose bcrypt httpx
（openai / lightrag / qdrant / celery 均被 stub，不需要安装）
"""

import os
import sys
import types
import json
import argparse
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# ------------------------------------------------------------------
# 1. dummy 环境变量：仅用于让 app.database / app.core.security 完成导入
#    （模块级 MySQL 连接失败均被源码内 try/except 捕获，不影响后续）
# ------------------------------------------------------------------
os.environ.setdefault("MYSQL_HOST", "127.0.0.1")
os.environ.setdefault("MYSQL_PORT", "3306")
os.environ.setdefault("MYSQL_USER", "repro_dummy")
os.environ.setdefault("MYSQL_PASSWORD", "repro_dummy")
os.environ.setdefault("MYSQL_DB", "repro_dummy")
os.environ.setdefault("SECRET_KEY", "repro" + "x" * 40)

# ------------------------------------------------------------------
# 2. stub 重依赖模块（必须在 import app.* 之前注入 sys.modules）
# ------------------------------------------------------------------

# ---- 2.1 假 openai：可录制调用入参，并按模型名区分返回内容 ----
CAPTURED = {"keyword_calls": [], "fallback_messages": []}


class _Delta:
    def __init__(self, content):
        self.content = content


class _Choice:
    def __init__(self, content):
        self.delta = _Delta(content)
        self.index = 0


class _Usage:
    total_tokens = 7


class _Chunk:
    def __init__(self, content):
        self.choices = [_Choice(content)]
        self.usage = _Usage()


class _AsyncIter:
    def __init__(self, content):
        self._chunks = [_Chunk(content)]

    def __aiter__(self):
        async def _gen():
            for c in self._chunks:
                yield c
        return _gen()


class _Completions:
    async def create(self, **kwargs):
        model = kwargs.get("model", "")
        if "35b" in model:  # 关键词路由模型
            CAPTURED["keyword_calls"].append(kwargs)
            return _AsyncIter("L2:测试 | HYDE:NO | NEED_REFS:NO")
        # 兜底对话模型：录制完整 messages（含会话历史 → 用于证明历史泄露）
        CAPTURED["fallback_messages"].append(kwargs.get("messages", []))
        return _AsyncIter("【模拟LLM回答】这是一个模拟回答。")


class _Chat:
    def __init__(self):
        self.completions = _Completions()


class AsyncOpenAI:
    def __init__(self, **kwargs):
        self.chat = _Chat()


fake_openai = types.ModuleType("openai")
fake_openai.AsyncOpenAI = AsyncOpenAI
sys.modules["openai"] = fake_openai

# ---- 2.2 假 app.rag.engine ----
class QueryParam:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)
        self.__dict__.setdefault("enable_rerank", False)

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return None


class FakeEngine:
    """记录 QueryParam.conversation_history（证明受害者历史被读入攻击者请求）"""

    def __init__(self):
        self.seen_history = None

    async def aquery_llm(self, query, param=None):
        self.seen_history = getattr(param, "conversation_history", None)
        return {
            "llm_response": {"content": "【模拟LLM回答】这是一个模拟回答。"},
            "data": {"references": [], "chunks": []},
        }


async def get_workspace_engine(workspace):
    return FakeEngine()


def get_user_engine(user):
    return FakeEngine()


def reset_global_stats():
    pass


def get_global_stats():
    return {"llm_calls": 0, "llm_time": 0.0, "embedding_calls": 0,
            "embedding_time": 0.0, "total_tokens": 0, "last_response_tokens": 0}


def set_need_references_flag(value):
    pass


fake_rag_pkg = types.ModuleType("app.rag")
fake_rag_pkg.__path__ = []
fake_engine_mod = types.ModuleType("app.rag.engine")
fake_engine_mod.QueryParam = QueryParam
fake_engine_mod.get_workspace_engine = get_workspace_engine
fake_engine_mod.get_user_engine = get_user_engine
fake_engine_mod.reset_global_stats = reset_global_stats
fake_engine_mod.get_global_stats = get_global_stats
fake_engine_mod.set_need_references_flag = set_need_references_flag
sys.modules["app.rag"] = fake_rag_pkg
sys.modules["app.rag.engine"] = fake_engine_mod

# ---- 2.3 假 agentic 编排器（非 bypass 路径会 import；抛错后源码会 fallback）----
fake_agentic = types.ModuleType("app.services.agentic_rag_service")


class AgenticOrchestrator:
    def __init__(self, max_retries=1):
        pass

    async def execute(self, **kwargs):
        raise RuntimeError("harness stub: agentic orchestrator disabled")


fake_agentic.AgenticOrchestrator = AgenticOrchestrator
sys.modules["app.services.agentic_rag_service"] = fake_agentic

# ---- 2.4 假 file_service（只需提供函数符号）----
fake_fs = types.ModuleType("app.services.file_service")


def build_snippet_around_query(content, query, window=200):
    return content[: window * 2] if content else ""


fake_fs.build_snippet_around_query = build_snippet_around_query
sys.modules["app.services.file_service"] = fake_fs

# ------------------------------------------------------------------
# 3. 真实模块导入 + SQLite 内存库
# ------------------------------------------------------------------
from fastapi import FastAPI, HTTPException, Request  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

import app.database as db_mod  # noqa: E402
from app.core import security as security_mod  # noqa: E402
from app.api.routers import chat as chat_mod  # noqa: E402
from app.api.routers import auth as auth_mod  # noqa: E402
from app.api.routers import chat_history as history_mod  # noqa: E402
from app.api.routers import admin as admin_mod  # noqa: E402

sqlite_engine = create_engine(
    "sqlite://",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
db_mod.Base.metadata.create_all(bind=sqlite_engine)
TestingSession = sessionmaker(bind=sqlite_engine, autocommit=False, autoflush=False)


def override_get_db():
    db = TestingSession()
    try:
        yield db
    finally:
        db.close()


def override_get_current_user(request: Request):
    """测试桩：Bearer <username> 直接映射到测试用户（绕过 JWT，专注授权逻辑）"""
    auth = request.headers.get("Authorization", "")
    username = auth.replace("Bearer", "").strip()
    if not username:
        raise HTTPException(status_code=401, detail="test: missing test user")
    db = TestingSession()
    try:
        user = db.query(db_mod.UserModel).filter(db_mod.UserModel.username == username).first()
        if user is None:
            raise HTTPException(status_code=401, detail="test: unknown user")
        return user
    finally:
        db.close()


app = FastAPI()
app.include_router(chat_mod.router, prefix="/api")       # → /api/chat
app.include_router(auth_mod.router, prefix="/api")       # → /api/auth/*
app.include_router(history_mod.router, prefix="/api")    # → /api/chat-history/*
app.include_router(admin_mod.router, prefix="/api")      # → /api/admin/*

app.dependency_overrides[db_mod.get_db] = override_get_db
app.dependency_overrides[security_mod.get_current_user] = override_get_current_user

client = TestClient(app)

# ------------------------------------------------------------------
# 4. 种子数据：两个部门、两名用户、受害者会话（含"敏感"历史消息）
# ------------------------------------------------------------------
db = TestingSession()
dept_a = db_mod.DepartmentModel(id=1, name="技术研发部", description="victim dept")
dept_b = db_mod.DepartmentModel(id=2, name="运营与合规部", description="attacker dept")
db.add_all([dept_a, dept_b])

alice = db_mod.UserModel(username="alice", email="alice@corp.com",
                         hashed_password="x", role="user", is_active=True, department_id=1)
bob = db_mod.UserModel(username="bob", email="bob@corp.com",
                       hashed_password="x", role="user", is_active=True, department_id=2)
db.add_all([alice, bob])
db.flush()

session_s = db_mod.ChatSessionModel(id=100, user_id=alice.id, title="新对话")
db.add(session_s)
db.flush()
db.add_all([
    db_mod.ChatMessageModel(session_id=session_s.id, role="user",
                            content="【敏感】技术研发部第四季度薪资架构与期权方案"),
    db_mod.ChatMessageModel(session_id=session_s.id, role="ai",
                            content="【敏感】这是薪资架构的详细分析……", model_name="m"),
])
db.commit()
ALICE_ID, BOB_ID, SESSION_S = alice.id, bob.id, session_s.id
db.close()

# ------------------------------------------------------------------
# 5. 攻击与回归用例
# ------------------------------------------------------------------
REPORT = []


def report(section, **kv):
    REPORT.append((section, kv))
    print(f"\n===== {section} =====")
    for k, v in kv.items():
        print(f"  {k}: {v}")


def post_chat(username, session_id):
    return client.post(
        "/api/chat",
        headers={"Authorization": f"Bearer {username}"},
        json={"query": "测试问题", "mode": "hybrid", "session_id": session_id},
    )


def read_session_rows(sid):
    db = TestingSession()
    rows = db.query(db_mod.ChatMessageModel).filter(
        db_mod.ChatMessageModel.session_id == sid).order_by(db_mod.ChatMessageModel.id).all()
    out = [(r.role, r.content[:60]) for r in rows]
    title = db.query(db_mod.ChatSessionModel).filter(
        db_mod.ChatSessionModel.id == sid).first().title
    db.close()
    return out, title


def find_user(username):
    db = TestingSession()
    u = db.query(db_mod.UserModel).filter(db_mod.UserModel.username == username).first()
    out = None if u is None else {"id": u.id, "department_id": u.department_id}
    db.close()
    return out


# ---- 攻击一：会话 IDOR ----
r = post_chat("bob", SESSION_S)
llm_received = None
if CAPTURED["fallback_messages"]:
    msgs = CAPTURED["fallback_messages"][-1]
    llm_received = [(m.get("role"), m.get("content", "")[:50]) for m in msgs]
rows, title = read_session_rows(SESSION_S)
report(
    "ATTACK-1 会话IDOR: bob 用 alice 的 session_id 调用 POST /api/chat",
    http_status=r.status_code,
    response_head=r.text[:200].replace("\n", " "),
    victim_session_messages_after=rows,
    victim_session_title_after=title,
    llm_received_messages=llm_received,
    evidence_write="受害者会话出现攻击者写入的(user, 测试问题)" if any(c == "测试问题" for _, c in rows) else "未见写入",
    evidence_read="LLM上下文出现受害者历史(敏感)" if llm_received and any("敏感" in c for _, c in llm_received) else "未见历史泄露",
    evidence_title="标题被篡改" if title != "新对话" else "标题未变",
)

# ---- 攻击二：注册自选部门 ----
r = client.post("/api/auth/register", json={
    "username": "mallory", "email": "mallory@evil.com",
    "password": "Passw0rd!", "department_id": 1,  # 攻击者指定受害者部门
})
mallory = find_user("mallory")
report(
    "ATTACK-2 注册自选部门: mallory 注册时指定 department_id=1(技术研发部)",
    http_status=r.status_code,
    response=r.text[:200],
    mallory_row=mallory,
    evidence="mallory.department_id == 1（自选部门成功）" if mallory and mallory["department_id"] == 1 else "自选部门未生效",
)

# ---- 回归：正常问答（本人会话）----
CAPTURED["fallback_messages"].clear()
r = post_chat("alice", SESSION_S)
rows_own, title_own = read_session_rows(SESSION_S)
report(
    "REG-1 正常问答: alice 用自己的 session_id 调 /api/chat",
    http_status=r.status_code,
    stream_has_done='"type": "done"' in r.text or '"type":"done"' in r.text,
    session_message_count=len(rows_own),
)

# ---- 回归：会话历史读取（已有归属校验）----
r = client.get(f"/api/chat-history/sessions/{SESSION_S}/messages",
               headers={"Authorization": "Bearer alice"})
r2 = client.get(f"/api/chat-history/sessions/{SESSION_S}/messages",
                headers={"Authorization": "Bearer bob"})
report(
    "REG-2 会话历史读取: alice(所有者) vs bob(非所有者)",
    alice_status=r.status_code,
    bob_status=r2.status_code,
)

# ---- 回归：不传 session_id 的纯问答模式 ----
r = client.post(
    "/api/chat",
    headers={"Authorization": "Bearer alice"},
    json={"query": "测试问题", "mode": "hybrid"},
)
report(
    "REG-5 无session纯问答: alice 不传 session_id 调 /api/chat",
    http_status=r.status_code,
    stream_has_done='"type": "done"' in r.text or '"type":"done"' in r.text,
)

# ---- 回归（修复后）：注册不传 department_id 应成功且落 NULL 部门 ----
r = client.post("/api/auth/register", json={
    "username": "carol", "email": "carol@corp.com", "password": "Passw0rd!",
})
carol = find_user("carol")
report(
    "REG-3 注册(无部门字段): carol 注册时不传 department_id",
    http_status=r.status_code,
    carol_row=carol,
)

# ---- 回归（修复后）：管理员分配部门接口可用 ----
if carol:
    db = TestingSession()
    exists = db.query(db_mod.UserModel).filter(db_mod.UserModel.username == "admin").first()
    if not exists:
        db.add(db_mod.UserModel(username="admin", email="admin@corp.com",
                                hashed_password="x", role="admin", is_active=True))
        db.commit()
    db.close()
    r = client.patch(f"/api/admin/users/{carol['id']}/department?dept_id=1",
                     headers={"Authorization": "Bearer admin"})
    carol_after = find_user("carol")
    report(
        "REG-4 管理员改部门: PATCH /api/admin/users/{id}/department?dept_id=1",
        http_status=r.status_code,
        carol_row_after=carol_after,
    )
else:
    report("REG-4 管理员改部门: 跳过（carol 注册失败，无测试对象）")

# ------------------------------------------------------------------
# 6. 输出报告文件
# ------------------------------------------------------------------
if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("out", nargs="?", default=None, help="报告输出路径")
    args = parser.parse_args()
    if args.out:
        Path(args.out).write_text(
            "Phase3 tenant-boundary repro report\n"
            f"project HEAD: see tenant_fix_notes.md\n\n" +
            "\n\n".join(f"[{s}]\n" + "\n".join(f"{k} = {v}" for k, v in kv.items())
                        for s, kv in REPORT),
            encoding="utf-8",
        )
        print(f"\n报告已写入: {args.out}")
