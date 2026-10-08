"""
自研工具权限网关（Phase 5 / 任务包 E）—— 全项目唯一自研安全模块。

插在"模型工具意图"与"真实执行"之间：LLM 只能提出意图，是否执行由本网关
用确定性规则决定，对应大厂 JD 的"工具调用权限沙箱 / 越权工具调用防护"。

设计取舍（面试口径，详见 security_eval/phase5_report.md）：

1. 【为什么建在执行前，而不是靠 system prompt 约束模型】
   system prompt 不是访问控制：模型输出天然不可信（提示注入、越狱、
   temperature=1 随机性都会绕过"请遵守权限"这类软约束）。确定性的权限
   边界必须建在提示词之外——网关校验的是请求里携带的真实身份
   （JWT 解出的当前用户 + 数据库里的角色/部门），模型无法伪造。

2. 【校验链顺序：最便宜且最能挡的在前，命中即拒（fail-closed）】
   身份（user_context 必须带真实用户 id）
   → 角色×工具白名单（注册表 allowed_roles）
   → 参数 JSON Schema 校验（内置迷你校验器：required/类型/枚举/拒绝未知参数，
      零新增依赖；未知参数一律拒——防借多余字段夹带数据）
   → 参数租户范围校验（dept_id 必须是存在的部门且非 admin 必须本部门；
      doc_id 必须存在且非 admin 必须本部门文档；邮件格式+非 admin 仅内网域）
   → 每用户每工具调用次数限制（滑动窗口，仅统计真实执行）。
   前面的检查失败，后面记 "skip"——攻击在最早一层被确定性拦截，
   深层规则是纵深防御，不依赖白名单先挡住。

3. 【高风险"预览→确认→执行"】
   模型只能提出意图。高风险工具先给用户看"要做什么、参数是什么"，
   用户明确回复确认后才执行，执行后返回 receipt。
   确认是确定性的短语匹配（不是再过一次 LLM）——确认环节不能引入
   模型随机性/注入面。确认时【重新】跑完整校验链再执行，防
   preview→confirm 之间的 TOCTOU（文档被删、配额变化）。

4. 【状态存内存】待确认意图存进程内 dict（key=(user_id, session_id)，TTL）。
   单进程 uvicorn 演示足够；生产形态是 Redis + 签名 preview token，
   多副本一致且防篡改——面试时主动说明这条边界。

5. 【审计】每次决策（deny/pending/allow）都写 security_eval/tool_calls.jsonl：
   gateway ∈ {deny, pending, allow}、executed 布尔、checks 各链路结果、
   decision_reason、confirmed、receipt。评测 runner 据此区分
   "真的执行了"（none/allow）和"被网关拦了"（deny/pending）。
"""

from __future__ import annotations

import json
import os
import re
import secrets
import threading
import time
from datetime import datetime, timezone

from app.services.mock_tools import MOCK_TOOL_REGISTRY, execute_mock_tool, record_tool_audit

# ============================================================
# 0. 配置
# ============================================================

# 每用户每工具：窗口内最多真实执行次数（仅统计 allow，预览不耗配额）
_RATE_LIMIT_MAX = int(os.environ.get("TOOL_GATEWAY_RATE_LIMIT_MAX", "5"))
_RATE_LIMIT_WINDOW_SEC = int(os.environ.get("TOOL_GATEWAY_RATE_LIMIT_WINDOW_SEC", "600"))

# 会话级工具调用总预算：单会话窗口内所有工具的真实执行总数上限（超限熔断，防 Agent 循环失控）
_SESSION_BUDGET_MAX = int(os.environ.get("TOOL_GATEWAY_SESSION_BUDGET_MAX", "10"))
_SESSION_BUDGET_WINDOW_SEC = int(os.environ.get("TOOL_GATEWAY_SESSION_BUDGET_WINDOW_SEC", "3600"))

# 待确认意图有效期（秒）
_PENDING_TTL_SEC = int(os.environ.get("TOOL_GATEWAY_PENDING_TTL_SEC", "600"))

# 非 admin 角色的外发邮件白名单域（本系统工具为 admin-only，这是纵深防御配置）
_INTERNAL_EMAIL_DOMAINS = {
    d.strip().lower()
    for d in os.environ.get("TOOL_GATEWAY_INTERNAL_DOMAINS", "sec-eval.local,lightrag.local").split(",")
    if d.strip()
}

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# 确认/取消短语（标准化后精确匹配；刻意不做语义理解，避免确认环节引入 LLM）
_CONFIRM_PHRASES = {
    "确认", "确认执行", "确认删除", "确认导出", "确认发送", "是的", "好的", "可以",
    "同意", "执行", "执行吧", "发吧", "删吧", "导出吧",
    "confirm", "yes", "y", "ok", "okay", "approve", "approved", "proceed", "execute",
}
_CANCEL_PHRASES = {
    "取消", "取消执行", "不执行", "不用了", "算了", "否", "不", "先不要",
    "cancel", "no", "n", "abort", "stop", "dont", "don't",
}

# 校验链每一步的名字（顺序即短路顺序）
_CHECK_NAMES = ("identity", "role", "schema", "scope", "rate_limit", "session_budget")


# ============================================================
# 1. 迷你 JSON Schema 校验器（零依赖：required / type / enum / 未知参数拒绝）
# ============================================================

_TYPE_MATCHERS = {
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "array": lambda v: isinstance(v, list),
    "object": lambda v: isinstance(v, dict),
    "null": lambda v: v is None,
}


def validate_params(schema: dict, params: dict) -> list[str]:
    """返回错误列表；空列表 = 通过。拒绝未知参数（防夹带）。"""
    errors: list[str] = []
    if not isinstance(params, dict):
        return [f"params 非对象: {type(params).__name__}"]

    properties = schema.get("properties", {}) if isinstance(schema, dict) else {}
    required = schema.get("required", []) if isinstance(schema, dict) else []

    for key in required:
        if key not in params:
            errors.append(f"缺少必填参数 {key}")
    for key in params:
        if key not in properties:
            errors.append(f"未知参数 {key}（注册表未声明，拒绝执行）")

    for key, spec in properties.items():
        if key not in params or not isinstance(spec, dict):
            continue
        value = params[key]
        types = spec.get("type")
        if types:
            if isinstance(types, str):
                types = [types]
            if not any(_TYPE_MATCHERS.get(t, lambda _v: True)(value) for t in types):
                errors.append(f"参数 {key} 类型错误：期望 {types}，实得 {type(value).__name__}")
                continue
        enum = spec.get("enum")
        if enum is not None and value not in enum:
            errors.append(f"参数 {key} 越出允许范围 {enum}")
    return errors


# ============================================================
# 2. 租户/参数范围校验（需要 DB：部门存在性、文档归属）
# ============================================================

def _to_int(value):
    """LLM 可能给 '12' 或 12，统一转 int 做范围校验；失败返回 None。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    return None


def _scope_check(tool_name: str, params: dict, user: dict) -> str | None:
    """
    参数租户范围校验。返回拒绝原因字符串；None = 通过。
    规则对 admin 与非 admin 分层：admin 是全系统管理员（任何存在的部门/文档），
    非 admin（纵深防御路径，正常被白名单挡住）强制本部门边界。
    """
    role = user.get("role") or "user"
    is_admin = role == "admin"
    uid = user.get("id")
    user_dept = user.get("department_id")

    # —— 惰性引入，避免模块导入即连库（与 get_current_user 同款用法）——
    from app.database import SessionLocal, DepartmentModel, DocumentModel

    if tool_name == "export_department_data":
        dept_id = _to_int(params.get("dept_id"))
        if dept_id is None:
            return f"dept_id 必须是整数，实得 {params.get('dept_id')!r}"
        db = SessionLocal()
        try:
            dept = db.query(DepartmentModel).filter(DepartmentModel.id == dept_id).first()
        finally:
            db.close()
        if dept is None:
            return f"部门 {dept_id} 不存在（拒绝探测/伪造 dept_id）"
        if not is_admin and user_dept is not None and dept_id != user_dept:
            return f"越权：部门 {dept_id} 非本部门（本部门 {user_dept}）"
        return None

    if tool_name == "delete_document":
        doc_id = _to_int(params.get("doc_id"))
        if doc_id is None:
            return f"doc_id 必须是整数，实得 {params.get('doc_id')!r}"
        db = SessionLocal()
        try:
            doc = db.query(DocumentModel).filter(DocumentModel.id == doc_id).first()
        finally:
            db.close()
        if doc is None:
            return f"文档 {doc_id} 不存在（拒绝删除幽灵文档）"
        if not is_admin:
            if doc.department_id is not None and user_dept is not None and doc.department_id != user_dept:
                return f"越权：文档 {doc_id} 属于部门 {doc.department_id}，非本部门 {user_dept}"
            if (doc.department_id is None or user_dept is None) and doc.user_id != uid:
                return f"越权：文档 {doc_id} 属于其他用户"
        return None

    if tool_name == "send_email":
        to = str(params.get("to", "")).strip()
        if not _EMAIL_RE.match(to):
            return f"收件人邮箱格式非法: {to!r}"
        if not is_admin:
            domain = to.rsplit("@", 1)[-1].lower()
            if domain not in _INTERNAL_EMAIL_DOMAINS:
                return f"越权：非管理员仅允许外发至内网域 {_INTERNAL_EMAIL_DOMAINS}，实得 {domain}"
        return None

    return None  # 未声明范围规则的工具：不附加限制


# ============================================================
# 3. 每用户每工具调用次数限制（滑动窗口，仅统计真实执行）
# ============================================================

class _ToolRateLimiter:
    def __init__(self, max_calls: int, window_sec: int):
        self._max = max_calls
        self._window = window_sec
        self._lock = threading.Lock()
        self._hits: dict[tuple[str, str], list[float]] = {}

    def check(self, username: str, tool: str) -> bool:
        """纯检查，不消耗配额。"""
        now = time.monotonic()
        with self._lock:
            hits = [t for t in self._hits.get((username, tool), []) if now - t < self._window]
            return len(hits) < self._max

    def consume(self, username: str, tool: str) -> None:
        """真实执行成功后记账。"""
        now = time.monotonic()
        with self._lock:
            key = (username, tool)
            hits = [t for t in self._hits.get(key, []) if now - t < self._window]
            hits.append(now)
            self._hits[key] = hits


_rate_limiter = _ToolRateLimiter(_RATE_LIMIT_MAX, _RATE_LIMIT_WINDOW_SEC)


class _SessionBudgetLimiter:
    """会话级总预算：key=(user_id, session_id)，统计窗口内所有工具的真实执行总数。"""

    def __init__(self, max_calls: int, window_sec: int):
        self._max = max_calls
        self._window = window_sec
        self._lock = threading.Lock()
        self._hits: dict[tuple[int, int], list[float]] = {}

    def _key(self, user: dict, session_id) -> tuple[int, int]:
        try:
            uid = int(user.get("id") or 0)
        except (TypeError, ValueError):
            uid = 0
        try:
            sid = int(session_id or 0)
        except (TypeError, ValueError):
            sid = 0
        return (uid, sid)

    def check(self, user: dict, session_id) -> bool:
        """纯检查，不消耗预算。"""
        now = time.monotonic()
        key = self._key(user, session_id)
        with self._lock:
            hits = [t for t in self._hits.get(key, []) if now - t < self._window]
            return len(hits) < self._max

    def consume(self, user: dict, session_id) -> None:
        """真实执行成功后记账。"""
        now = time.monotonic()
        key = self._key(user, session_id)
        with self._lock:
            hits = [t for t in self._hits.get(key, []) if now - t < self._window]
            hits.append(now)
            self._hits[key] = hits


_session_budget = _SessionBudgetLimiter(_SESSION_BUDGET_MAX, _SESSION_BUDGET_WINDOW_SEC)


# ============================================================
# 4. 待确认意图存储（内存，key=(user_id, session_id)，TTL）
# ============================================================

_pending_lock = threading.Lock()
_pending: dict[tuple[int, int], dict] = {}


def _pending_key(user: dict, session_id) -> tuple[int, int]:
    try:
        uid = int(user.get("id") or 0)
    except (TypeError, ValueError):
        uid = 0
    try:
        sid = int(session_id or 0)
    except (TypeError, ValueError):
        sid = 0
    return (uid, sid)


def _cleanup_expired() -> None:
    now = time.time()
    with _pending_lock:
        dead = [k for k, v in _pending.items() if v["expires_at"] < now]
        for k in dead:
            _pending.pop(k, None)


def _put_pending(user: dict, session_id, intent: dict) -> dict:
    preview_id = f"pv-{int(time.time() * 1000)}-{secrets.token_hex(4)}"
    item = {
        "preview_id": preview_id,
        "intent": {"tool": intent["tool"], "params": intent.get("params") or {}},
        "created_at": time.time(),
        "expires_at": time.time() + _PENDING_TTL_SEC,
    }
    with _pending_lock:
        _pending[_pending_key(user, session_id)] = item
    return item


def restore_pending(user: dict, session_id, item: dict) -> None:
    """确认执行异常时把暂存意图放回去（用户可重试），仅当该 key 当前无新暂存时。"""
    if item.get("expires_at", 0) < time.time():
        return
    with _pending_lock:
        _pending.setdefault(_pending_key(user, session_id), item)


def get_pending(user: dict, session_id) -> dict | None:
    _cleanup_expired()
    with _pending_lock:
        item = _pending.get(_pending_key(user, session_id))
    return dict(item) if item else None


def pop_pending(user: dict, session_id) -> dict | None:
    _cleanup_expired()
    with _pending_lock:
        item = _pending.pop(_pending_key(user, session_id), None)
    return item


# ============================================================
# 5. 审计
# ============================================================

def _write_audit(record: dict) -> None:
    record.setdefault("ts", datetime.now(timezone.utc).isoformat())
    record_tool_audit(record)


def audit_cancel(tool_name: str, params: dict, user: dict, session_id,
                 reason: str = "用户取消，未执行") -> None:
    """取消也是一次决策，同样落审计（gateway=cancelled, executed=false）。"""
    _write_audit({
        "tool": tool_name,
        "params": params,
        "risk_level": MOCK_TOOL_REGISTRY.get(tool_name, {}).get("risk_level", "unknown"),
        "user": user,
        "session_id": session_id,
        "gateway": "cancelled",
        "executed": False,
        "confirmed": False,
        "decision_reason": reason,
        "checks": {},
    })


# ============================================================
# 6. 网关主入口
# ============================================================

def _normalize_user(user_context: dict | None) -> dict:
    ctx = user_context or {}
    return {
        "id": ctx.get("id"),
        "username": ctx.get("username") or "anonymous",
        "role": ctx.get("role") or "user",
        "department_id": ctx.get("department_id"),
    }


def _build_preview_text(tool_name: str, params: dict, user: dict) -> str:
    entry = MOCK_TOOL_REGISTRY.get(tool_name, {})
    desc = entry.get("description", tool_name)
    risk = entry.get("risk_level", "unknown")
    params_display = json.dumps(params, ensure_ascii=False)
    return (
        f"🔐 检测到高风险操作，需要您确认：\n"
        f"- 工具：{tool_name}（{desc}）\n"
        f"- 风险等级：{risk}\n"
        f"- 执行参数：{params_display}\n"
        f"- 申请人：{user.get('username')}（role={user.get('role')}）\n\n"
        f"该操作执行后不可撤销（本演示为 Mock，无真实副作用）。\n"
        f"确认执行请回复「确认」，放弃请回复「取消」。"
    )


def _execute_allow(tool_name: str, params: dict, user: dict, session_id, checks: dict,
                   confirmed: bool, started_at: float, extra: dict | None = None) -> dict:
    """白名单内放行的唯一出口：真正调用 Mock 工具 + 写 allow 审计。"""
    result = execute_mock_tool(tool_name, params, user_context=user, write_audit=False)
    receipt = result.get("receipt") if isinstance(result, dict) else None
    if result.get("status") == "mock_success":
        _rate_limiter.consume(user["username"], tool_name)
        _session_budget.consume(user, session_id)

    out = {
        "decision": "allow",
        "tool": tool_name,
        "params": params,
        "risk_level": MOCK_TOOL_REGISTRY.get(tool_name, {}).get("risk_level", "unknown"),
        "user": user,
        "session_id": session_id,
        "checks": checks,
        "decision_reason": "已确认，放行执行" if confirmed else "白名单内低风险工具，直接放行",
        "executed": result.get("status") == "mock_success",
        "confirmed": confirmed,
        "result": result,
        "receipt": receipt,
    }
    if extra:
        out.update(extra)
    _write_audit({
        "tool": tool_name,
        "params": params,
        "risk_level": out["risk_level"],
        "user": user,
        "session_id": session_id,
        "gateway": "allow",
        "executed": out["executed"],
        "confirmed": confirmed,
        "decision_reason": out["decision_reason"],
        "checks": checks,
        "latency_ms": int((time.time() - started_at) * 1000),
        "receipt": receipt,
        "result": result,
    })
    return out


def gate_tool_call(intent: dict, user_context: dict | None = None, session_id=None,
                   *, skip_preview: bool = False) -> dict:
    """
    网关主入口：对模型提出的工具意图跑完整校验链。

    Args:
        intent: {"tool": name, "params": {...}}
        user_context: 真实登录用户上下文（id/username/role/department_id）
        session_id: 会话 ID（待确认意图按 (user_id, session_id) 绑定）
        skip_preview: True 表示这是"用户确认后"的复检调用——不再进入预览，
                      直接执行（仍会重跑身份/白名单/Schema/范围/限流全链）。

    Returns:
        决策 dict，decision ∈ {"deny", "pending", "allow"}。
    """
    started_at = time.time()
    tool_name = str(intent.get("tool") or "")
    params = intent.get("params") or {}
    user = _normalize_user(user_context)
    entry = MOCK_TOOL_REGISTRY.get(tool_name)
    checks = {name: "skip" for name in _CHECK_NAMES}

    def _deny(reason: str, failed_check: str) -> dict:
        checks[failed_check] = "fail"
        risk = entry.get("risk_level", "unknown") if entry else "unknown"
        out = {
            "decision": "deny",
            "tool": tool_name,
            "params": params,
            "risk_level": risk,
            "user": user,
            "session_id": session_id,
            "checks": checks,
            "decision_reason": reason,
            "executed": False,
            "confirmed": False,
            "result": None,
            "receipt": None,
        }
        _write_audit({
            "tool": tool_name,
            "params": params,
            "risk_level": risk,
            "user": user,
            "session_id": session_id,
            "gateway": "deny",
            "executed": False,
            "confirmed": False,
            "decision_reason": reason,
            "checks": checks,
            "latency_ms": int((time.time() - started_at) * 1000),
        })
        print(f"[GATEWAY] [DENY] tool={tool_name} user={user['username']} check={failed_check} reason={reason}")
        return out

    # —— 1. 身份：必须携带来自 JWT/DB 的真实用户 id ——
    checks["identity"] = "pass" if user.get("id") else "fail"
    if not user.get("id"):
        return _deny("无法识别调用者身份（匿名/伪造上下文）", "identity")

    # —— 2. 角色×工具白名单 ——
    if entry is None:
        checks["role"] = "fail"
        return _deny(f"工具 {tool_name!r} 未注册", "role")
    allowed_roles = entry.get("allowed_roles") or []
    checks["role"] = "pass" if user["role"] in allowed_roles else "fail"
    if checks["role"] == "fail":
        return _deny(
            f"角色 {user['role']!r} 不在工具 {tool_name} 的允许角色 {allowed_roles} 内",
            "role",
        )

    # —— 3. 参数 JSON Schema 校验 ——
    schema_errors = validate_params(entry.get("param_schema", {}), params)
    checks["schema"] = "fail" if schema_errors else "pass"
    if schema_errors:
        return _deny(f"参数 Schema 校验失败: {'; '.join(schema_errors)}", "schema")

    # —— 4. 参数租户范围校验 ——
    scope_reason = _scope_check(tool_name, params, user)
    checks["scope"] = "fail" if scope_reason else "pass"
    if scope_reason:
        return _deny(f"参数范围校验失败: {scope_reason}", "scope")

    # —— 5. 每用户每工具调用次数限制（预览不耗配额，真实执行才 consume）——
    checks["rate_limit"] = "pass" if _rate_limiter.check(user["username"], tool_name) else "fail"
    if checks["rate_limit"] == "fail":
        return _deny(
            f"调用次数超限：{_RATE_LIMIT_WINDOW_SEC}s 内最多 {_RATE_LIMIT_MAX} 次",
            "rate_limit",
        )

    # —— 5b. 会话级工具调用总预算（防 Agent 循环失控的硬熔断；预览不耗预算）——
    checks["session_budget"] = "pass" if _session_budget.check(user, session_id) else "fail"
    if checks["session_budget"] == "fail":
        return _deny(
            f"会话工具预算耗尽：{_SESSION_BUDGET_WINDOW_SEC}s 内本会话最多执行 {_SESSION_BUDGET_MAX} 次工具调用",
            "session_budget",
        )

    # —— 6. 高风险：预览→确认→执行；其余风险等级直通 ——
    if entry.get("risk_level") == "high" and not skip_preview:
        pending_item = _put_pending(user, session_id, intent)
        checks_out = dict(checks)
        out = {
            "decision": "pending",
            "tool": tool_name,
            "params": params,
            "risk_level": "high",
            "user": user,
            "session_id": session_id,
            "checks": checks_out,
            "decision_reason": "高风险操作，等待用户确认",
            "executed": False,
            "confirmed": False,
            "result": None,
            "receipt": None,
            "preview_id": pending_item["preview_id"],
            "preview_text": _build_preview_text(tool_name, params, user),
        }
        _write_audit({
            "tool": tool_name,
            "params": params,
            "risk_level": "high",
            "user": user,
            "session_id": session_id,
            "gateway": "pending",
            "executed": False,
            "confirmed": False,
            "decision_reason": out["decision_reason"],
            "checks": checks_out,
            "latency_ms": int((time.time() - started_at) * 1000),
            "preview_id": pending_item["preview_id"],
        })
        print(f"[GATEWAY] [PENDING] tool={tool_name} user={user['username']} preview_id={pending_item['preview_id']}")
        return out

    return _execute_allow(tool_name, params, user, session_id, dict(checks),
                          confirmed=skip_preview, started_at=started_at)


def normalize_confirmation_text(query: str) -> str:
    """确认短语标准化：去空白、去常见中英文标点、小写。刻意不做语义理解。"""
    text = re.sub(r"[\s。！？!?,，.、~～;；:：\"'“”‘’（）()\[\]【】]+", "", (query or "").lower())
    return text


def is_confirm(query: str) -> bool:
    return normalize_confirmation_text(query) in _CONFIRM_PHRASES


def is_cancel(query: str) -> bool:
    return normalize_confirmation_text(query) in _CANCEL_PHRASES
