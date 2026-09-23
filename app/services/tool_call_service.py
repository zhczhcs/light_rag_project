"""
工具调用链路（Phase 1：最小可演示闭环）

链路：
  用户输入 → LLM 输出结构化工具意图（JSON）→ parse_tool_intent（解析点）
          → execute_tool_intent（执行点，本阶段无网关、直接执行）
          → mock_tools.execute_mock_tool（单一执行入口 + 审计日志）
          → 结果回注对话回答

插桩说明（给 Phase 5 / 任务包 E）：
  - 解析点：本模块 parse_tool_intent()。若网关需要审查"模型原始意图文本"，
    在此处之前拦截即可。
  - 执行点：本模块 execute_tool_intent()。权限网关（身份→角色→参数校验→
    预览确认）插在这里，对 intent 放行/拒绝后，再放行到
    mock_tools.execute_mock_tool() 这一唯一真实执行入口。
  - 本模块不改写 chat.py 原有 RAG 流程：仅当检测到合法工具意图时才走
    工具分支，否则完全退回原流程。
"""

import os
import re
import json

from openai import AsyncOpenAI

from app.services.mock_tools import MOCK_TOOL_REGISTRY, execute_mock_tool

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

        client = AsyncOpenAI(api_key=api_key, base_url=base_url)
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
# 3. 【执行点】意图执行（Phase 5 网关插入位置）
# ============================================================

def execute_tool_intent(intent: dict, user_context: dict | None = None) -> dict:
    """
    工具意图的唯一执行入口（本阶段无网关，直接执行）。

    Phase 5（任务包 E）将在此处前置权限网关：
      用户身份 → 角色/部门 → 工具白名单 → 参数 JSON Schema 校验
      → 高风险"预览-确认-执行" → 审计（放行/拒绝都记）。
    """
    tool_name = intent["tool"]
    params = intent.get("params") or {}
    return execute_mock_tool(tool_name, params, user_context=user_context)


def build_tool_answer(intent: dict, result: dict) -> str:
    """把工具执行结果回注为自然语言回答。"""
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
