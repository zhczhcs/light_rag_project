# -*- coding: utf-8 -*-
"""第 3 类攻击（跨租户越权检索）代码级验证 harness —— 修复前基线 + 修复后验证

为什么不打真实服务：LLM/embedding 远程账号欠费时，/api/chat 在检索阶段即报错，
攻击链走不到"历史注入→回答泄露"环节，真实服务跑不出有意义的数字。
本 harness 复用任务包 C 的验证思路：挂载【真实 chat.py 路由代码】（当前工作区版本，
git 回退到 f1650d4 即为修复前），SQLite 内存库 + 可录制假 openai，
对 attacks.jsonl 的 12 条 cross_tenant 样本逐条：

  1. 给受害者（alice, 技术研发部）播一个含本条样本金丝雀机密的会话
  2. 攻击者（bob, 运营与合规部）拿该 session_id 调 POST /api/chat（payload=样本）
  3. 判定（规则，不用 LLM judge）：
     - 读泄露：假 LLM 是"服从型"的——它把收到的会话历史浓缩进回答；
       回答中出现 canary_token 即等价于真实系统中"历史被注入上下文并被复述"
     - 写破坏：受害者会话中出现攻击者消息（完整性越权）
     - 修复后预期：攻击请求 404（chat.py 归属校验），无任何读写

假 LLM 行为完全确定，因此同一套 attacks.jsonl 在修复前/后跑出可对照的 ASR。
用法：
  PYTHONIOENCODING=utf-8 python security_eval/run_cross_tenant_baseline.py <输出.json>
"""

import json
import os
import sys
import types
import argparse
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv
load_dotenv(PROJECT_ROOT / ".env")

ATTACKS_PATH = Path(__file__).resolve().parent / "attacks.jsonl"
RESULTS_DIR = Path(__file__).resolve().parent / "results"

# ------------------------------------------------------------------
# 1. 可录制假 openai：按模型名区分三个调用点，行为完全确定
# ------------------------------------------------------------------
CAPTURED = {"keyword_calls": [], "intent_calls": [], "fallback_messages": []}


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


def _summarize_history(messages: list[dict]) -> str:
    """服从型假 LLM：把对话历史浓缩进回答（含金丝雀即泄露）。"""
    history = [m for m in messages if m.get("role") in ("user", "assistant")]
    if not history:
        return "【模拟LLM回答】我没有看到任何历史上下文。"
    parts = []
    for m in history[-3:]:
        content = (m.get("content") or "").strip()
        if content:
            parts.append(content[:120])
    return "【模拟LLM回答】根据对话历史整理：" + " | ".join(parts)


class _Completions:
    async def create(self, **kwargs):
        model = kwargs.get("model", "")
        messages = kwargs.get("messages", [])
        joined = "\n".join(str(m.get("content", "")) for m in messages if isinstance(m, dict))
        # 按调用点的 prompt 特征分发，而不是按模型名（.env 里多个模型可能同名）
        if "你是路由分类器" in joined:  # 关键词路由
            CAPTURED["keyword_calls"].append(kwargs)
            return _AsyncIter("L2:测试 | HYDE:NO | NEED_REFS:NO")
        if "工具路由分类器" in joined:  # 工具意图检测：返回非 JSON → 解析为无意图
            CAPTURED["intent_calls"].append(kwargs)
            return _AsyncIter("（本请求无需调用工具）")
        # 兜底/生成模型：服从型浓缩历史（用于判定历史泄露）
        CAPTURED["fallback_messages"].append(messages)
        return _AsyncIter(_summarize_history(messages))


class _Chat:
    def __init__(self):
        self.completions = _Completions()


class AsyncOpenAI:
    def __init__(self, **kwargs):
        self.chat = _Chat()


fake_openai = types.ModuleType("openai")
fake_openai.AsyncOpenAI = AsyncOpenAI
sys.modules["openai"] = fake_openai

# ------------------------------------------------------------------
# 2. 假 app.rag.engine / agentic / file_service（与任务包 C 同思路）
# ------------------------------------------------------------------
class QueryParam:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)
        self.__dict__.setdefault("enable_rerank", False)

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return None


class FakeEngine:
    """记录 QueryParam.conversation_history，模拟检索后生成（不跨租户检索）。"""

    def __init__(self):
        self.seen_history = None

    async def aquery_llm(self, query, param=None):
        self.seen_history = getattr(param, "conversation_history", None)
        messages = [{"role": "user", "content": query}]
        if self.seen_history:
            messages = list(self.seen_history) + messages
        return {
            "llm_response": {"content": _summarize_history(messages)},
            "data": {"references": [], "chunks": []},
        }


async def get_workspace_engine(workspace):
    return FakeEngine()


async def get_user_engine(user):
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

fake_agentic = types.ModuleType("app.services.agentic_rag_service")


class AgenticOrchestrator:
    def __init__(self, max_retries=1):
        pass

    async def execute(self, **kwargs):
        raise RuntimeError("harness stub: agentic orchestrator disabled")


fake_agentic.AgenticOrchestrator = AgenticOrchestrator
sys.modules["app.services.agentic_rag_service"] = fake_agentic

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
import app.services.tool_call_service  # noqa: E402,F401  （真实模块，依赖已 stub 的 openai）

CHAT_SOURCE = Path(chat_mod.__file__).resolve()
# 记录被测 chat.py 的指纹，报告中区分修复前(f1650d4)/修复后(b87f9ac+)
import hashlib  # noqa: E402
CHAT_SHA1 = hashlib.sha1(CHAT_SOURCE.read_bytes()).hexdigest()[:12]

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
app.include_router(chat_mod.router, prefix="/api")
app.dependency_overrides[db_mod.get_db] = override_get_db
app.dependency_overrides[security_mod.get_current_user] = override_get_current_user
client = TestClient(app)

# ------------------------------------------------------------------
# 4. 种子数据：两个部门、受害者 alice（研发部）、攻击者 bob（运营部）
# ------------------------------------------------------------------
_db = TestingSession()
_dept_a = db_mod.DepartmentModel(id=1, name="技术研发部", description="victim dept")
_dept_b = db_mod.DepartmentModel(id=2, name="运营与合规部", description="attacker dept")
_db.add_all([_dept_a, _dept_b])
_alice = db_mod.UserModel(username="alice", email="alice@corp.com",
                          hashed_password="x", role="user", is_active=True, department_id=1)
_bob = db_mod.UserModel(username="bob", email="bob@corp.com",
                        hashed_password="x", role="user", is_active=True, department_id=2)
_db.add_all([_alice, _bob])
_db.commit()
ALICE_ID, BOB_ID = _alice.id, _bob.id
_db.close()


def seed_victim_session(canary: str) -> int:
    db = TestingSession()
    sess = db_mod.ChatSessionModel(user_id=ALICE_ID, title="【机密】部门内部讨论")
    db.add(sess)
    db.flush()
    db.add(db_mod.ChatMessageModel(session_id=sess.id, role="user",
                                   content=f"帮我分析一下这份机密材料：{canary}"))
    db.add(db_mod.ChatMessageModel(session_id=sess.id, role="ai",
                                   content=f"【机密问答】已分析。核心结论：{canary}。请勿外传。",
                                   sources="[]", model_name="seed"))
    db.commit()
    sid = sess.id
    db.close()
    return sid


def read_victim_session(sid: int):
    db = TestingSession()
    rows = db.query(db_mod.ChatMessageModel).filter(
        db_mod.ChatMessageModel.session_id == sid).order_by(db_mod.ChatMessageModel.id).all()
    out = [(r.role, r.content) for r in rows]
    db.close()
    return out


# ------------------------------------------------------------------
# 5. 逐条跑 cross_tenant 样本并判定
# ------------------------------------------------------------------
def run() -> dict:
    samples = [json.loads(l) for l in open(ATTACKS_PATH, encoding="utf-8")
               if json.loads(l)["category"] == "cross_tenant"]

    results = []
    for s in samples:
        CAPTURED["fallback_messages"].clear()
        sid = seed_victim_session(s["canary"])
        r = client.post(
            "/api/chat",
            headers={"Authorization": "Bearer bob"},
            json={"query": s["payload"], "mode": "hybrid", "session_id": sid},
        )

        reply_text = ""
        for evt in r.text.splitlines():
            try:
                obj = json.loads(evt)
            except json.JSONDecodeError:
                continue
            if obj.get("type") == "content":
                reply_text += str(obj.get("data", ""))

        token = s.get("canary_token", "")
        leak = token in reply_text
        integrity = False
        if r.status_code == 200:
            rows = read_victim_session(sid)
            integrity = any(role == "user" and s["payload"][:20] in content
                            for role, content in rows)

        success = leak or integrity
        evidence = []
        if leak:
            evidence.append(f"回答含金丝雀 '{token}'（历史注入→复述泄露）")
        if integrity:
            evidence.append("攻击者消息写入受害者会话（完整性越权）")
        if r.status_code == 404:
            evidence.append("404 会话不存在（归属校验拦截）")
        if not evidence:
            evidence.append("无泄露、无越权写入")

        results.append({
            "id": s["id"], "http_status": r.status_code,
            "leak": leak, "integrity_violation": integrity,
            "success": success, "evidence": "；".join(evidence),
            "reply_head": reply_text[:100],
        })

    n = len(results)
    succ = sum(1 for r in results if r["success"])
    leaked = sum(1 for r in results if r["leak"])
    violated = sum(1 for r in results if r["integrity_violation"])
    return {
        "harness": "run_cross_tenant_baseline.py（真实 chat.py 路由 + SQLite + 确定性假 LLM）",
        "chat_under_test": str(CHAT_SOURCE),
        "chat_sha1_12": CHAT_SHA1,
        "run_at": datetime.now(timezone.utc).isoformat(),
        "total": n, "success": succ,
        "asr": round(succ / n, 4) if n else None,
        "leaked_count": leaked, "integrity_violation_count": violated,
        "http_status_dist": {str(code): sum(1 for r in results if r["http_status"] == code)
                             for code in sorted({r["http_status"] for r in results})},
        "results": results,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("out", nargs="?", default=None, help="JSON 结果输出路径")
    args = parser.parse_args()

    report = run()
    print(f"被测 chat.py: {report['chat_under_test']} (sha1={report['chat_sha1_12']})")
    print(f"HTTP 状态分布: {report['http_status_dist']}")
    print(f"total={report['total']}  success={report['success']}  "
          f"ASR={report['asr']}")
    print(f"读泄露 {report['leaked_count']} 条；写破坏 {report['integrity_violation_count']} 条")
    for r in report["results"]:
        print(f"  {r['id']:<12} http={r['http_status']:<4} success={r['success']!s:<6} {r['evidence']}")

    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n结果已写入: {out}")


if __name__ == "__main__":
    main()
