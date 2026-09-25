"""
工具调用链路（Phase 1 最小闭环 + Phase 5 权限网关）

链路：
  用户输入 → LLM 输出结构化工具意图（JSON）→ parse_tool_intent（解析点）
          → execute_tool_intent（执行点，Phase 5 起前置权限网关）
          → tool_gateway.gate_tool_call（身份→白名单→Schema→范围→限流→预览确认）
          → mock_tools.execute_mock_tool（唯一真实执行入口）
          → 结果回注对话回答

高风险工具"预览→确认→执行"的确认往返（纯后端实现，SSE 无需改动）：
  1. 用户提出高风险请求 → 网关返回 preview 消息（meta 事件带 gateway=pending），
     意图暂存网关内存（key=(user_id, session_id)）。
  2. 用户回复「确认」→ resolve_pending_confirmation() 在 LLM 意图检测【之前】
     拦截，重新跑完整校验链后执行，返回带 receipt 的执行结果。
  3. 用户回复「取消」→ 丢弃暂存意图。

插桩说明：
  - 解析点：本模块 parse_tool_intent()。
  - 执行点：本模块 execute_tool_intent()——内部委托 tool_gateway.gate_tool_call()，
    模型意图从此不再可能直达 mock_tools.execute_mock_tool()。
  - 本模块不改写 chat.py 原有 RAG 流程：仅当检测到合法工具意图时才走
    工具分支，否则完全退回原流程。
"""

import os
import re
import json

from openai import AsyncOpenAI

from app.core.llm_client import get_llm_client
from app.services.mock_tools import MOCK_TOOL_REGISTRY
from app.services import tool_gateway

# 工具意图检测用的轻量模型（可用环境变量覆盖）
_TOOL_INTENT_MODEL = os.environ.get("TOOL_INTENT_MODEL", "qwen-turbo-latest")


# ============================================================
# 1. LLM 工具意图检测：让模型输出结构化 JSON 意图
# ============================================================

_TOOL_INTENT_PROMPT = """你是企业知识库助手的工具路由分类器。判断用户请求是否需要调用以下工具之一：

1. send_email(to, subject, body) —— 发送邮件
2. delete_document(doc_id) —— 删除知识库文档
3. export_department_data(dept_id) —— 导出部门数据

【判定规则】
- 仅当用户明确请求"发送/邮件""删除文档/资料""导出/下载部门数据"这类工具操作时，才输出工具意图。
- 普通的知识问答、寒暄、继续上文的问题 → 不调用工具。
- 注意：用户可能在提问中夹带工具请求，要识别出来。

【输出格式】（只输出一行 JSON，不要任何解释）
若需要调用工具：{"tool": "<工具名>", "params": {<参数>}, "reason": "<一句判断理由>"}
若不需要：{"tool": null, "params": {}, "reason": "<一句判断理由>"}"""


async def detect_tool_intent(query: str, conversation_history: list[dict] | None = None) -> dict | None:
    """
    用轻量 LLM 检测用户输入中的工具调用意图。

    Returns:
        合法意图 dict {"tool": name, "params": {...}}，未检测到/解析失败/未知工具返回 None。
        任何异常（网络、Key 缺失）都静默降级为 None，不影响正常问答流程。
    """
    try:
        api_key = os.environ.get("ALI_API_KEY")
        base_url = os.environ.get("ALI_BASE_URL")
        if not api_key or not base_url:
            return None

        client = get_llm_client(api_key, base_url)
        messages = [{"role": "system", "content": _TOOL_INTENT_PROMPT}]
        if conversation_history:
            messages.extend(conversation_history[-6:])
        messages.append({"role": "user", "content": query})

        response = await client.chat.completions.create(
            model=_TOOL_INTENT_MODEL,
            messages=messages,
            temperature=0,
            max_tokens=200,
            stream=True,
            extra_body={"enable_thinking": False},
        )
        parts = []
        async for chunk in response:
            if chunk.choices and chunk.choices[0].delta and chunk.choices[0].delta.content:
                parts.append(chunk.choices[0].delta.content)
        raw = "".join(parts).strip()

        intent = parse_tool_intent(raw)
        if intent:
            print(f"[TOOL-CALL] [HIT] 检测到工具意图: {intent['tool']} params={intent.get('params')} (LLM原始输出: {raw[:120]})")
        return intent
    except Exception as e:
        print(f"[WARN] [TOOL-CALL] 工具意图检测失败，按无工具处理: {e}")
        return None


# ============================================================
# 2. 【解析点】结构化意图解析（单一位置，可插桩）
# ============================================================

def parse_tool_intent(text: str) -> dict | None:
    """
    从 LLM 输出中解析结构化工具意图。

    解析策略：优先整段 JSON；失败则退化为提取第一个 {...} 块；
    工具名必须在 MOCK_TOOL_REGISTRY 中，否则视为无意图。
    """
    if not text:
        return None

    candidates = [text.strip()]
    # 退化策略：抓取第一个完整的 {...} 块
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        candidates.append(m.group(0))

    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(data, dict):
            continue
        tool = data.get("tool")
        if tool is None or tool == "null":
            return None
        tool = str(tool).strip()
        if tool not in MOCK_TOOL_REGISTRY:
            print(f"[TOOL-CALL] [WARN] LLM 提出了未注册工具 '{tool}'，忽略（未执行）")
            return None
        params = data.get("params") or {}
        if not isinstance(params, dict):
            params = {"_raw": str(params)}
        return {"tool": tool, "params": params, "reason": str(data.get("reason", ""))}

    return None


# ============================================================
# 3. 【执行点】意图执行：权限网关前置（Phase 5 / 任务包 E）
# ============================================================

def execute_tool_intent(intent: dict, user_context: dict | None = None,
                        session_id=None) -> dict:
    """
    工具意图的唯一执行入口。模型意图一律先过 tool_gateway.gate_tool_call()：
      用户身份 → 角色×工具白名单 → 参数 JSON Schema → 租户范围 → 调用次数限制
      → 高风险"预览→确认→执行" → 审计（放行/拒绝/待确认都记）。

    Returns:
        网关决策 dict，decision ∈ {"deny", "pending", "allow"}，
        含 checks / decision_reason / executed / receipt / preview_text 等字段。
    """
    return tool_gateway.gate_tool_call(intent, user_context=user_context, session_id=session_id)


def resolve_pending_confirmation(query: str, user_context: dict | None = None,
                                 session_id=None) -> dict | None:
    """
    高风险"预览→确认→执行"的确认往返（在 LLM 意图检测之前调用）。

    - 无待确认意图 → None（走正常流程）。
    - 命中确认短语 → 弹出暂存意图，重新跑完整网关校验链（skip_preview=True）
      后执行，返回 decision="allow"（confirmed=True，带 receipt）。
    - 命中取消短语 → 丢弃暂存意图，返回 decision="cancelled"。
    - 其他输入 → None（保留待确认，走正常问答流程）。

    确认用确定性短语匹配而非 LLM：确认环节不能引入模型随机性/注入面。
    """
    user = tool_gateway._normalize_user(user_context)
    pending = tool_gateway.get_pending(user, session_id)
    if pending is None:
        return None
    intent = pending["intent"]

    if tool_gateway.is_cancel(query):
        popped = tool_gateway.pop_pending(user, session_id)
        if popped:
            tool_gateway.audit_cancel(popped["intent"]["tool"], popped["intent"]["params"],
                                      user, session_id)
        return {
            "decision": "cancelled",
            "tool": intent["tool"],
            "params": intent["params"],
            "user": user,
            "session_id": session_id,
            "executed": False,
            "confirmed": False,
            "decision_reason": "用户取消，未执行",
        }
    if tool_gateway.is_confirm(query):
        popped = tool_gateway.pop_pending(user, session_id)
        if popped is None:
            return None
        # 确认时重新跑完整校验链（防 preview→confirm 之间的 TOCTOU），然后执行。
        # 执行链异常（如 DB 抖动）时把暂存意图放回去，让用户可以重试确认。
        try:
            return tool_gateway.gate_tool_call(
                popped["intent"], user_context=user_context, session_id=session_id,
                skip_preview=True,
            )
        except Exception as e:  # noqa: BLE001
            tool_gateway.restore_pending(user, session_id, popped)
            print(f"[GATEWAY] [ERR] 确认执行异常，已恢复待确认意图: {e}")
            return {
                "decision": "error",
                "tool": popped["intent"]["tool"],
                "params": popped["intent"]["params"],
                "user": user,
                "session_id": session_id,
                "executed": False,
                "confirmed": False,
                "decision_reason": f"执行链路异常，本次未执行: {e}",
            }
    return None


def build_gateway_answer(out: dict) -> str:
    """把网关决策渲染为面向用户的自然语言回答（拒绝原因细节只进审计日志）。"""
    decision = out.get("decision")
    tool_name = out.get("tool", "-")
    desc = MOCK_TOOL_REGISTRY.get(tool_name, {}).get("description", tool_name)

    if decision in ("allow", "executed"):
        result = out.get("result") or {}
        confirmed_note = "（用户已确认）" if out.get("confirmed") else ""
        return "\n".join([
            f"✅ 已执行高风险操作 `{tool_name}`{confirmed_note}。",
            "",
            f"- 工具用途：{desc}",
            f"- 执行状态：{result.get('status', 'unknown')}",
            f"- 回执编号：{out.get('receipt') or result.get('receipt') or '-'}",
            f"- 说明：{result.get('detail', '-')}",
        ])
    if decision == "pending":
        return out.get("preview_text") or "该操作需要确认。"
    if decision == "cancelled":
        return f"已取消操作 `{tool_name}`，未执行任何动作。"
    if decision == "deny":
        return (
            f"⛔ 操作被拒绝：您没有权限执行 `{tool_name}`。\n"
            f"该请求已被权限网关记录并审计。如有工作需要，请联系系统管理员。"
        )
    if decision == "error":
        return (f"⚠️ 操作 `{tool_name}` 本次未执行（执行链路异常，已保留待确认状态，"
                f"可回复「确认」重试）。详情已记录审计日志。")
    return "操作未完成。"


def build_tool_answer(intent: dict, result: dict) -> str:
    """把工具执行结果回注为自然语言回答（保留给旧调用方/测试）。"""
    tool_name = intent["tool"]
    desc = MOCK_TOOL_REGISTRY.get(tool_name, {}).get("description", tool_name)
    lines = [
        f"已调用工具 `{tool_name}`（{desc}）。",
        "",
        f"- 执行状态：{result.get('status', 'unknown')}",
        f"- 回执编号：{result.get('receipt', '-')}",
        f"- 说明：{result.get('detail', '-')}",
    ]
    return "\n".join(lines)
