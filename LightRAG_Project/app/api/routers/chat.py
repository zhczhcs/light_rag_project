import os
import re
import json
import time
import asyncio
import builtins
from fastapi import APIRouter, HTTPException, Depends
from fastapi.responses import StreamingResponse
from openai import AsyncOpenAI
from sqlalchemy.orm import Session

from app.rag.engine import QueryParam, get_user_engine, get_workspace_engine, reset_global_stats, get_global_stats, set_need_references_flag
from app.schemas.models import ChatRequest
from app.services.file_service import build_snippet_around_query
from app.services.context_service import build_conversation_history_enhanced
from app.core import globals
from app.core.runtime_config import get_agentic_runtime_config, get_query_runtime_config
from app.utils.metrics import monitor
from app.utils.table_printer import print_kv_table
from app.core.security import get_current_user
from app.database import UserModel, get_db, ChatSessionModel, ChatMessageModel, get_user_workspace


# 临时日志过滤：只保留系统关键日志，其他 print 全部静音。
# 恢复方式：删除本函数与下一行 print 绑定即可。
_KEEP_PATTERNS = ("[Context", "[Keywords", "[Agentic", "[HyDE", "[Rerank", "[RefInject", "[Engine")

def _context_only_print(*args, **kwargs):
    msg = " ".join(str(a) for a in args)
    if any(p in msg for p in _KEEP_PATTERNS):
        builtins.print(*args, **kwargs)


print = _context_only_print


async def extract_keywords_via_llm(query: str) -> tuple[list[str], bool, str, bool, bool]:
    """
    用轻量级 LLM 一次调用完成五件事:
    1. 判断是否需要知识库检索(bypass 判断)
    2. 如果需要检索, 提取 1-3 个核心关键词用于高亮显示
    3. 判断问题复杂度(L1/L2/L3), 用于动态模型路由
    4. 判断是否需要 HyDE(假设文档生成)辅助检索
    5. 判断问题是否涉及参考文献查询（NEED_REFS）
    """
    BYPASS_SIGNAL = "BYPASS"
    DEFAULT_LEVEL = "L2"

    try:
        api_key = os.environ.get("ALI_API_KEY")
        base_url = os.environ.get("ALI_BASE_URL")
        client = AsyncOpenAI(api_key=api_key, base_url=base_url)

        prompt_lines = [
            "你是路由分类器。根据用户输入，输出一条指令。只输出指令，不要解释。",
            "",
            "【路由规则】",
            "1. BYPASS：仅限「你好、早上好、在吗、好的、收到、明白了」等纯寒暄或确认。",
            "2. L1:关键词：查询单一事实或概念的定义。",
            "3. L2:关键词：教学、解释、对比、评价、建议、列举、总结、代码编写、文档分析。",
            "4. L3:关键词：跨文档深度逻辑推导、复杂的代码生成、系统架构设计、复杂数学证明。",
            "",
            "【关键词提取规则（极其重要）】",
            "- 提取问题中的核心实质名词：专业、领域、人名、术语、文档名、组织名、考试名、学位名。",
            "- 如果用户提到了自己的专业/身份/背景，必须将其作为关键词提取（如「计算机专业」->计算机，「金融学」->金融学，「医学生」->医学）。",
            "- 如果用户提到了目标/动作，提取对应的实体名词（如「考公」->考公，「考研」->考研，「找工作」->就业）。",
            "- 多个关键词用英文逗号分隔。",
            "",
            "【HyDE判断规则】",
            "- L1级别 → HYDE:NO（简单事实不需要假设文档）",
            "- 查询含明确实体/术语名 → HYDE:NO（实体明确直接检索即可）",
            "- 查询很泛（最新进展、优缺点、对比、解决方案）→ HYDE:YES",
            "- 查询口语化/省略/指代模糊 → HYDE:YES",
            "- L3级别 → HYDE:YES（复杂问题需要扩展语义）",
            "",
            "【参考文献判断规则（NEED_REFS）】",
            "- 问题涉及'参考文献、引用、来源、参考、reference、bibliography'等词 → NEED_REFS:YES",
            "- 问题问'这篇论文有几个参考文献、引用了哪些' → NEED_REFS:YES",
            "- 问题问'参考了什么文献' → NEED_REFS:YES",
            "- 其他一般性技术问题 → NEED_REFS:NO",
            "",
            "【输出格式（严格遵循）】",
            "BYPASS | HYDE:NO | NEED_REFS:NO",
            "L1:关键词 | HYDE:NO | NEED_REFS:NO",
            "L2:关键词 | HYDE:YES | NEED_REFS:NO",
            "L3:关键词 | HYDE:NO | NEED_REFS:NO",
            "",
            "【标准示范】",
            "输入：你好 -> 输出：BYPASS | HYDE:NO | NEED_REFS:NO",
            "输入：收到，明白了 -> 输出：BYPASS | HYDE:NO | NEED_REFS:NO",
            "输入：什么是半导体 -> 输出：L1:半导体 | HYDE:NO | NEED_REFS:NO",
            "输入：CMOS是什么 -> 输出：L1:CMOS | HYDE:NO | NEED_REFS:NO",
            "输入：广大网安怎么样 -> 输出：L2:广大,网安 | HYDE:NO | NEED_REFS:NO",
            "输入：半导体有哪些类型 -> 输出：L2:半导体 | HYDE:NO | NEED_REFS:NO",
            "输入：对比CMOS和TTL的优缺点 -> 输出：L2:CMOS,TTL | HYDE:YES | NEED_REFS:NO",
            "输入：网络包分类最新进展 -> 输出：L2:网络包分类 | HYDE:YES | NEED_REFS:NO",
            "输入：那个技术怎么用 -> 输出：L2:技术 | HYDE:YES | NEED_REFS:NO",
            "输入：用C++手写红黑树 -> 输出：L3:C++,红黑树 | HYDE:NO | NEED_REFS:NO",
            "输入：综合文档推导量子计算对RSA影响 -> 输出：L3:量子计算,RSA | HYDE:YES | NEED_REFS:NO",
            "输入：这篇论文有哪些参考文献 -> 输出：L2:论文,参考文献 | HYDE:NO | NEED_REFS:YES",
            "输入：这篇文章引用了什么 -> 输出：L2:引用 | HYDE:NO | NEED_REFS:YES",
            "",
        ]
        prompt_lines.append("输入：" + query + " -> 输出：")
        prompt_text = "\n".join(prompt_lines)

        _keyword_model = os.environ.get("KEYWORD_EXTRACTION_MODEL", "qwen3.5-35b-a3b")
        response = await client.chat.completions.create(
            model=_keyword_model,
            messages=[{"role": "user", "content": prompt_text}],
            temperature=0,
            max_tokens=100,
            stream=True,
            extra_body={"enable_thinking": False},
        )

        # 收集流式输出
        raw_parts = []
        async for chunk in response:
            if chunk.choices and chunk.choices[0].delta and chunk.choices[0].delta.content:
                raw_parts.append(chunk.choices[0].delta.content)
        raw = "".join(raw_parts).strip()

        # 解析 NEED_REFS 标志
        need_references = True  # 默认保守策略：需要参考文献
        refs_match = re.search(r'NEED_REFS\s*[:：]\s*(YES|NO)', raw, re.IGNORECASE)
        if refs_match:
            need_references = refs_match.group(1).upper() == "YES"
            # 去掉 NEED_REFS 部分，方便后续解析
            raw = re.sub(r'\s*\|\s*NEED_REFS\s*[:：]\s*(YES|NO)', '', raw, flags=re.IGNORECASE).strip()

        # 解析 HYDE 标志
        use_hyde = False
        hyde_match = re.search(r'HYDE\s*[:：]\s*(YES|NO)', raw, re.IGNORECASE)
        if hyde_match:
            use_hyde = hyde_match.group(1).upper() == "YES"
            # 去掉 HYDE 部分，方便后续解析
            raw = re.sub(r'\s*\|\s*HYDE\s*[:：]\s*(YES|NO)', '', raw, flags=re.IGNORECASE).strip()

        if raw.upper().strip() == BYPASS_SIGNAL:
            print_kv_table(
                "🔑 Keywords: 查询分类",
                {"判定结果": "BYPASS (闲聊/元对话)", "HYDE": "NO", "NEED_REFS": "NO", "原始输出": raw[:40]},
                key_width=14, val_width=46,
            )
            return ([], True, "L1", False, False)

        level = DEFAULT_LEVEL
        keyword_part = raw
        level_match = re.match(r'^(L[123])\s*[:：]\s*(.+)', raw, re.IGNORECASE)
        if level_match:
            level = level_match.group(1).upper()
            keyword_part = level_match.group(2)

        terms = []
        for t in keyword_part.replace("，", ",").split(","):
            t = t.strip().strip('"').strip("'").strip("、")
            if len(t) >= 2 and t.upper() != BYPASS_SIGNAL:
                terms.append(t)

        print_kv_table(
            "🔑 Keywords: LLM 提取结果",
            {
                "复杂度等级": level,
                "关键词": ", ".join(terms[:3]) if terms else "无",
                "HYDE": "YES" if use_hyde else "NO",
                "NEED_REFS": "YES" if need_references else "NO",
                "原始输出": raw[:40] + ("..." if len(raw) > 40 else ""),
            },
            key_width=14, val_width=46,
        )
        return (terms[:3], False, level, use_hyde, need_references)

    except Exception as e:
        print_kv_table(
            "⚠️ Keywords: 异常降级",
            {"错误": str(e)[:50], "降级策略": "规则引擎", "默认等级": DEFAULT_LEVEL},
            key_width=14, val_width=46,
        )
        return ([], False, DEFAULT_LEVEL, False, True)


router = APIRouter()

# -- 并发限流 --
_MAX_CONCURRENT_QUERIES = 10
_chat_semaphore = asyncio.Semaphore(_MAX_CONCURRENT_QUERIES)

# ===========================
# 动态模型路由策略配置
# ===========================
_model_l1 = os.environ.get("MODEL_L1", "qwen3.6-flash")
_model_l2 = os.environ.get("MODEL_L2", "qwen3.6-plus-2026-04-02")
_model_l3 = os.environ.get("MODEL_L3", "kimi-k2.5")

MODEL_ROUTING = {
    "L1": {"model": _model_l1, "desc": "Cost-effective, Fast"},
    "L2": {"model": _model_l2, "desc": "Balanced Performance"},
    "L3": {"model": _model_l3, "desc": "High Intelligence, Reasoning"},
}


def _level_to_model(level):
    return MODEL_ROUTING.get(level, MODEL_ROUTING["L2"])["model"]


def analyze_query_complexity_fallback(query):
    query_len = len(query)
    complex_keywords = [
        "为什么", "如何", "分析", "评价", "对比", "区别",
        "设计", "代码", "算法", "优化", "重构", "翻译",
        "reason", "analysis", "compare", "code", "design"
    ]
    if query_len > 50 or any(k in query.lower() for k in complex_keywords):
        return "L3"
    simple_keywords = ["你好", "在吗", "hi", "hello", "是谁", "什么时间", "weather"]
    if query_len < 10 or any(k in query.lower() for k in simple_keywords):
        return "L1"
    return "L2"


@router.post("/chat", summary="对话接口 (流式+引用)")
async def chat_with_rag(
    request: ChatRequest,
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    user_engine = await get_workspace_engine(get_user_workspace(current_user))

    query_text = request.query
    if isinstance(query_text, list):
        query_text = " ".join([str(item) for item in query_text])
    elif not isinstance(query_text, str):
        query_text = str(query_text)
    query_text = query_text.strip()
    if not query_text:
        raise HTTPException(status_code=400, detail="query 不能为空")

    session_id = "query_" + str(time.time())
    collector = monitor.create_collector(session_id)

    total_start_time = time.time()

    def _elapsed():
        return "[+" + format(time.time() - total_start_time, ".2f") + "s]"

    print("⏱️ " + _elapsed() + " 💬 [Chat] 收到问题: " + query_text)

    extracted_keywords, use_bypass, complexity_level, use_hyde, need_references = await extract_keywords_via_llm(query_text)
    query_mode = "bypass" if use_bypass else (request.mode or "mix")
    print("⏱️ " + _elapsed(), end=" ")
    print_kv_table(
        "🔑 Keywords: 分类完成",
        {
            "复杂度": complexity_level,
            "BYPASS": "是" if use_bypass else "否",
            "关键词": ", ".join(extracted_keywords) if extracted_keywords else "无",
            "HYDE": "是" if use_hyde else "否",
            "NEED_REFS": "是" if need_references else "否",
        },
        key_width=14, val_width=46,
    )

    selected_model = _level_to_model(complexity_level)

    print("⏱️ " + _elapsed(), end=" ")
    print_kv_table(
        "🧠 Router: 智能路由",
        {
            "复杂度等级": complexity_level,
            "选用模型": selected_model,
            "查询模式": query_mode,
            "备注": "BYPASS-闲聊" if use_bypass else f"关键词: {extracted_keywords}",
        },
        key_width=14, val_width=46,
    )

    model_token = globals.model_context.set(selected_model)
    metrics_token = globals.metrics_context.set(collector)

    try:
        if request.session_id:
            user_msg = ChatMessageModel(
                session_id=request.session_id,
                role="user",
                content=query_text
            )
            db.add(user_msg)
            db.commit()
            print("⏱️ " + _elapsed() + " 💾 [DB] 用户消息已保存")

        conversation_history = []
        if request.session_id:
            conversation_history = await build_conversation_history_enhanced(
                db=db,
                session_id=request.session_id,
                model_name=selected_model,
                query_text=query_text,
                exclude_last_user_msg=True,
            )

        print("⏱️ " + _elapsed() + " 📝 [Context] 历史上下文构建完成, " + str(len(conversation_history)) + " 条消息")

        # =========================================================
        # 🛠️ LLM Agent 工具调用 (Function Calling) 拦截层 — 已禁用
        # =========================================================
        tool_call_result = None
        _agent_client = None
        # 注：工具调用为实验性功能，当前已全局禁用
        # =========================================================

        retrieval_start_time = time.time()
        reset_global_stats()

        _user_prompt = (
            "用中文回答。引用规则（必须严格遵守）：\n"
            "1. 正文中使用 [n] 标注引用，n 必须对应 Document Chunks 的 reference_id。\n"
            "2. Knowledge Graph Data（Entity/Relationship）仅可使用其 source_refs 中给出的编号引用，"
            "严禁编造引用编号。\n"
            "3. References 列表的条目必须使用 Reference Document List 中的真实文档名。\n"
            "4. 如果某个观点既没有 Document Chunks 证据，也没有 Knowledge Graph 的 source_refs 证据，不要加 [n] 标注。"
        )

        query_runtime = get_query_runtime_config()
        param = QueryParam(
            mode=query_mode,
            stream=True,
            chunk_top_k=query_runtime.chunk_top_k,
            top_k=query_runtime.top_k,
            max_entity_tokens=query_runtime.max_entity_tokens,
            max_relation_tokens=query_runtime.max_relation_tokens,
            max_total_tokens=query_runtime.max_total_tokens,
            conversation_history=conversation_history,
            user_prompt=_user_prompt,
        )

        print("⏱️ " + _elapsed() + " 📋 [QueryParam] top_k=" + str(param.top_k) + ", chunk_top_k=" + str(param.chunk_top_k)
              + ", max_entity=" + str(param.max_entity_tokens) + ", max_relation=" + str(param.max_relation_tokens)
              + ", max_total=" + str(param.max_total_tokens) + ", rerank=" + str(param.enable_rerank))

        # =========================================================
        # 🤖 Phase 1+2: Agentic RAG — QueryResolver + RetrievalGrader + QueryRewriter
        # =========================================================
        # 设置参考文献过滤标志（供 Rerank 使用）
        set_need_references_flag(need_references)

        resolved_query = query_text
        agentic_metadata = {"phase": "2", "rewritten": False, "reason": "", "retries": 0, "graded": False, "grade_passed": True}
        agentic_result = None

        if not use_bypass and request.session_id and conversation_history:
            try:
                from app.services.agentic_rag_service import AgenticOrchestrator
                orchestrator = AgenticOrchestrator(max_retries=get_agentic_runtime_config().max_retries)
                agentic_result = await orchestrator.execute(
                    user_query=query_text,
                    conversation_history=conversation_history,
                    engine=user_engine,
                    param=param,
                    use_hyde=use_hyde,
                    workspace=get_user_workspace(current_user),
                )
                resolved_query = agentic_result["final_query"]
                agentic_metadata = {
                    "phase": "2",
                    "rewritten": agentic_result["was_rewritten"],
                    "reason": agentic_result["resolver_reason"],
                    "original_query": agentic_result["original_query"],
                    "retries": agentic_result.get("retries", 0),
                    "graded": agentic_result.get("graded", False),
                    "grade_passed": agentic_result.get("grade_passed", True),
                }
                if agentic_result["was_rewritten"]:
                    print("⏱️ " + _elapsed() + " 🔄 [Agentic] QueryResolver 改写: '" + query_text + "' → '" + resolved_query + "'")
                if agentic_result.get("use_hyde"):
                    hyde_count = agentic_result.get("hyde_chunks_count", 0)
                    print("⏱️ " + _elapsed() + " 🧬 [HyDE] 已启用，检索到 " + str(hyde_count) + " 个补充 chunks")
                if agentic_result.get("retries", 0) > 0:
                    print("⏱️ " + _elapsed() + " 🔄 [Agentic] Phase2 重试次数: " + str(agentic_result["retries"]))
                if agentic_result.get("graded", False):
                    grade_status = "通过" if agentic_result.get("grade_passed") else "未通过"
                    print("⏱️ " + _elapsed() + " 📊 [Agentic] Grader: " + grade_status)
            except Exception as e:
                print("⚠️ [Agentic] Orchestrator 异常，使用原文: " + str(e))
                resolved_query = query_text
        else:
            print("⏱️ " + _elapsed() + " ⏭️ [Agentic] 跳过 (bypass/无session/无历史)")

        # AgenticOrchestrator 内部已调用 aquery_llm，直接使用其结果
        if agentic_result and agentic_result.get("result"):
            result = agentic_result["result"]
            print("⏱️ " + _elapsed() + " 🚀 [RAG] 使用 AgenticOrchestrator 结果 (查询: '" + resolved_query + "')")
        else:
            print("⏱️ " + _elapsed() + " 🚀 [RAG] aquery_llm 开始... (查询: '" + resolved_query + "')")
            async with _chat_semaphore:
                result = await user_engine.aquery_llm(resolved_query, param=param)

        collector.retrieval_time = time.time() - retrieval_start_time
        print("⏱️ " + _elapsed() + " ✅ [RAG] aquery_llm 返回, 检索耗时 " + format(collector.retrieval_time, ".2f") + "s")

        async def event_generator():
            generation_start_time = time.time()
            ttft_recorded = False

            try:
                print("⏱️ " + _elapsed() + " 📡 [Stream] event_generator 开始执行")
                _meta_data = {"model": selected_model, "mode": query_mode}
                if agentic_metadata.get("rewritten"):
                    _meta_data["agentic"] = {
                        "rewritten": True,
                        "original_query": agentic_metadata.get("original_query", query_text),
                        "resolved_query": resolved_query,
                        "reason": agentic_metadata.get("reason", ""),
                    }
                yield json.dumps({"type": "meta", "data": _meta_data}, ensure_ascii=False) + "\n"

                llm_response = result.get("llm_response", {})

                sources = []
                if not use_bypass:
                    data = result.get("data", {})
                    references = data.get("references", [])
                    chunks = data.get("chunks", [])

                    # 构建 reference_id -> file_path 映射
                    ref_id_to_file = {}
                    for ref in references:
                        ref_id = ref.get("reference_id", "")
                        file_path = ref.get("file_path", "未知来源")
                        if ref_id:
                            ref_id_to_file[ref_id] = file_path

                    collector.retrieved_chunks = len(chunks)
                    retrieval_sources = []

                    # 按 rerank_score 降序排序，确保高分 chunk 优先展示
                    chunks_sorted = sorted(
                        chunks,
                        key=lambda c: c.get("rerank_score") if c.get("rerank_score") is not None else -1,
                        reverse=True
                    )
                    for idx, chunk in enumerate(chunks_sorted):
                        ref_id = chunk.get("reference_id", str(idx))
                        content = chunk.get("content", "")
                        file_path = ref_id_to_file.get(ref_id, "未知来源")
                        retrieval_sources.append(os.path.basename(file_path) if file_path else "未知来源")

                        # 清理内容中的【来源文档：】标记
                        clean_content = content
                        if "【来源文档：" in clean_content:
                            lines = clean_content.split("\n")
                            clean_lines = [l for l in lines if "【来源文档：" not in l]
                            clean_content = "\n".join(clean_lines)

                        if extracted_keywords:
                            snippet = build_snippet_around_query(clean_content, extracted_keywords[0], window=200) if clean_content else ""
                        else:
                            snippet = build_snippet_around_query(clean_content, query_text, window=200) if clean_content else ""

                        sources.append({
                            "id": int(ref_id) if ref_id.isdigit() else 0,
                            "reference_id": int(ref_id) if ref_id.isdigit() else 0,
                            "content": snippet,
                            "content_full": clean_content,
                            "highlight_terms": extracted_keywords,
                            "source_filename": os.path.basename(file_path) if file_path else "未知来源",
                            "score": chunk.get("rerank_score"),
                        })

                    collector.details['retrieval_sources'] = retrieval_sources
                    print("⏱️ " + _elapsed() + " 📎 [Sources] 引用构造完成: " + str(len(sources)) + " 条 (来自 " + str(len(references)) + " 个文档)")

                else:
                    print("⏱️ " + _elapsed() + " ⏭️ [Chat] bypass 模式，跳过关键词提取和引用构造")

                # 2f. 无文档 chunk 时前置提示
                _no_doc_notice = ""
                _use_fallback_llm = not use_bypass and len(sources) == 0
                if _use_fallback_llm:
                    _no_doc_notice = "> *由于不存在相关性强的知识库内容，以下基于 AI 自身知识回答，仅供参考。*\n\n"
                    yield json.dumps({"type": "content", "data": _no_doc_notice}, ensure_ascii=False) + "\n"
                    print("⏱️ " + _elapsed() + " ⚠️ [Notice] 0 doc chunks, 发送无文档提示")

                # 3. 流式发送 LLM 内容
                full_ai_response = _no_doc_notice
                in_think_block = False

                if _use_fallback_llm:
                    # chunks=0 → 丢弃 LightRAG 受限回复，直接调 LLM（无 system prompt 约束）
                    print("⏱️ " + _elapsed() + " 🔄 [Fallback] 0 doc chunks, 绕过 LightRAG 受限回复, 直接调 LLM")
                    _fb_api_key = os.environ.get("ALI_API_KEY")
                    _fb_base_url = os.environ.get("ALI_BASE_URL")
                    _fb_client = AsyncOpenAI(api_key=_fb_api_key, base_url=_fb_base_url)
                    _fb_messages = [{"role": "system", "content": "你是一个知识渊博的AI助手，用中文回答用户的问题。"}]
                    if conversation_history:
                        _fb_messages.extend(conversation_history)
                    _fb_messages.append({"role": "user", "content": query_text})

                    _fb_create_kwargs = {
                        "model": selected_model,
                        "messages": _fb_messages,
                        "temperature": 0.3,
                        "stream": True,
                        "stream_options": {"include_usage": True},
                    }
                    _ml = selected_model.lower()
                    if _ml.startswith("qwen3") or _ml.startswith("kimi"):
                        _fb_create_kwargs["extra_body"] = {"enable_thinking": False}

                    _fb_response = await _fb_client.chat.completions.create(**_fb_create_kwargs)
                    async for _fb_chunk in _fb_response:
                        if _fb_chunk.choices and _fb_chunk.choices[0].delta and _fb_chunk.choices[0].delta.content:
                            _fb_text = _fb_chunk.choices[0].delta.content
                            full_ai_response += _fb_text
                            _send = _fb_text
                            if "<think>" in _send:
                                in_think_block = True
                            if in_think_block:
                                if "</think>" in _send:
                                    _send = _send.split("</think>", 1)[1]
                                    in_think_block = False
                                else:
                                    _send = ""
                            if _send:
                                if not ttft_recorded:
                                    ttft = time.time() - generation_start_time
                                    collector.details['ttft'] = ttft
                                    ttft_recorded = True
                                yield json.dumps({"type": "content", "data": _send}, ensure_ascii=False) + "\n"
                        if hasattr(_fb_chunk, "usage") and _fb_chunk.usage and _fb_chunk.usage.total_tokens:
                            collector.last_response_tokens = _fb_chunk.usage.total_tokens

                elif llm_response.get("is_streaming"):
                    response_stream = llm_response.get("response_iterator")
                    if response_stream:
                        async for chunk_text in response_stream:
                            if chunk_text:
                                full_ai_response += chunk_text
                                send_text = chunk_text
                                if "<think>" in send_text:
                                    in_think_block = True
                                if in_think_block:
                                    if "</think>" in send_text:
                                        send_text = send_text.split("</think>", 1)[1]
                                        in_think_block = False
                                    else:
                                        send_text = ""
                                if send_text:
                                    if not ttft_recorded:
                                        ttft = time.time() - generation_start_time
                                        collector.details['ttft'] = ttft
                                        ttft_recorded = True
                                    yield json.dumps({"type": "content", "data": send_text}, ensure_ascii=False) + "\n"
                else:
                    content = llm_response.get("content", "")
                    if content:
                        full_ai_response += content
                        yield json.dumps({"type": "content", "data": content}, ensure_ascii=False) + "\n"

                collector.generation_time = time.time() - generation_start_time
                print("⏱️ " + _elapsed() + " ✅ [Stream] 流式输出完成, 生成耗时 " + format(collector.generation_time, ".2f") + "s, 总字数 " + str(len(full_ai_response)))

                # 4. 引用过滤
                # 注意：原始文档中可能包含 [3] 等学术引用标记，
                # 这些会被正则误匹配。由于同一文档的多个 chunk 共享
                # reference_id，id_remap 会导致编号混乱。
                # 策略：保留全部 sources，仅清理 LLM 正文中明显的孤儿引用。
                if sources and len(sources) > 0:
                    # 获取 LLM 实际引用的 source IDs（基于 reference_id）
                    cited_ids = set(int(m) for m in re.findall(r'\[(\d+)\]', full_ai_response))
                    available_ref_ids = set(s["reference_id"] for s in sources)
                    valid_cited = cited_ids & available_ref_ids

                    # 如果 LLM 确实引用了某个文档，保留该文档的所有 chunks
                    if valid_cited:
                        kept_sources = [s for s in sources if s["reference_id"] in valid_cited]
                        removed = len(sources) - len(kept_sources)
                        if removed > 0:
                            print("🔍 [Citation Filter] 保留引用文档 " + str(valid_cited) + "，移除 " + str(len(sources) - len(kept_sources)) + " 条")
                        sources = kept_sources
                    else:
                        print("⚠️ [Citation Filter] LLM 未引用任何文档，保留全部 " + str(len(sources)) + " 条")

                    # 与 chunk_top_k / _MAX_RERANK_CHUNKS 同步（当前为 6）
                    MAX_SOURCES = 6
                    if len(sources) > MAX_SOURCES:
                        sources = sources[:MAX_SOURCES]
                        print("📏 [Limit] 引用数量限制为 " + str(MAX_SOURCES) + "，已截断")

                    # id 保持为 reference_id，不再重映射为 1,2,3...
                    # 前端用 idx+1 做展示编号

                    # 4f. 孤儿引用清理：删除不在最终 source reference_ids 中的 [n]
                    final_ref_ids = set(s["reference_id"] for s in sources)
                    def _strip_orphan_refs(text, valid_ids):
                        def _repl(m):
                            return m.group(0) if int(m.group(1)) in valid_ids else ""
                        return re.compile(r'\[(\d+)\]').sub(_repl, text)
                    cleaned = _strip_orphan_refs(full_ai_response, final_ref_ids)
                    cleaned = re.sub(r'\n\s*References\s*\n.*', '', cleaned, flags=re.DOTALL)
                    cleaned = re.sub(r'<think>.*?</think>', '', cleaned, flags=re.DOTALL)
                    if cleaned != full_ai_response:
                        full_ai_response = cleaned
                        print("🧹 [Orphan Cleanup] 清除孤儿引用 [n]（不在最终保留列表 " + str(final_ref_ids) + " 中）")
                        yield json.dumps({"type": "content_correction", "data": cleaned}, ensure_ascii=False) + "\n"

                # 5. 兜底清理 + 发送引用
                if not sources:
                    if re.search(r'\[\d+\]', full_ai_response) or re.search(r'References', full_ai_response):
                        corrected = full_ai_response
                        corrected = re.sub(r'\[\d+\]', '', corrected)
                        corrected = re.sub(r'\n\s*References\s*\n.*', '', corrected, flags=re.DOTALL)
                        corrected = re.sub(r'<think>.*?</think>', '', corrected, flags=re.DOTALL)
                        if corrected != full_ai_response:
                            full_ai_response = corrected
                            print("🧹 [Cleanup] 兜底清除 LLM 编造的 [n] 和 References（0 chunks 场景）")
                            yield json.dumps({"type": "content_correction", "data": corrected}, ensure_ascii=False) + "\n"

                # 6. 强制 strip References 块（前端用 source cards 展示引用，LLM 的 References 块是多余的）
                _ref_stripped = re.sub(r'\n\s*#{0,3}\s*References\s*\n.*', '', full_ai_response, flags=re.DOTALL)
                _ref_stripped = re.sub(r'<think>.*?</think>', '', _ref_stripped, flags=re.DOTALL)
                _ref_stripped = _ref_stripped.rstrip()
                if _ref_stripped != full_ai_response:
                    full_ai_response = _ref_stripped
                    print("✂️ [Strip] 强制移除 LLM References 块（由前端引用卡片替代）")
                    yield json.dumps({"type": "content_correction", "data": _ref_stripped}, ensure_ascii=False) + "\n"

                print("⏱️ " + _elapsed() + " 📤 [Sources] 发送 " + str(len(sources)) + " 条引用给前端")
                yield json.dumps({"type": "sources", "data": sources}, ensure_ascii=False) + "\n"

                # 获取实际 token：优先从 collector（bypass模式可达），兜底从 _global_stats（RAG模式 Worker Pool）
                actual_tokens = collector.last_response_tokens if collector and collector.last_response_tokens > 0 else 0
                if actual_tokens == 0:
                    gstats_tokens = get_global_stats().get("last_response_tokens", 0)
                    if gstats_tokens > 0:
                        actual_tokens = gstats_tokens
                print("🔢 [Token] collector=" + str(collector.last_response_tokens) + ", global=" + str(get_global_stats().get("last_response_tokens", 0)) + ", final=" + str(actual_tokens))
                yield json.dumps({"type": "done", "data": {"model": selected_model, "tokens": actual_tokens}}, ensure_ascii=False) + "\n"

                print("⏱️ " + _elapsed() + " 🏁 [Done] 发送完成信号")

                if request.session_id:
                    actual_tokens_to_save = collector.last_response_tokens if collector and collector.last_response_tokens > 0 else None
                    ai_msg = ChatMessageModel(
                        session_id=request.session_id,
                        role="ai",
                        content=full_ai_response,
                        sources=json.dumps(sources, ensure_ascii=False),
                        model_name=selected_model,
                        tokens=actual_tokens_to_save,
                    )
                    db.add(ai_msg)
                    db.commit()
                    db.refresh(ai_msg)
                    print("⏱️ " + _elapsed() + " 💾 [DB] AI 消息已保存 (id=" + str(ai_msg.id) + ")")
                    yield json.dumps({"type": "message_id", "data": ai_msg.id}, ensure_ascii=False) + "\n"

                    try:
                        session_obj = db.query(ChatSessionModel).filter(
                            ChatSessionModel.id == request.session_id
                        ).first()
                        if session_obj and session_obj.title == "新对话":
                            if extracted_keywords:
                                new_title = "、".join(extracted_keywords[:3]) + " 相关的问题"
                            else:
                                new_title = query_text[:15] + ("..." if len(query_text) > 15 else "")
                            session_obj.title = new_title
                            db.commit()
                            print("🏷️ [Title] 会话标题自动更新: '" + new_title + "'")
                            yield json.dumps({"type": "session_title_update", "data": {"session_id": request.session_id, "title": new_title}}, ensure_ascii=False) + "\n"
                    except Exception as title_err:
                        print("⚠️ [Title] 更新标题失败: " + str(title_err))

            except Exception as e:
                print("❌ [Chat] 流式输出异常: " + str(e))
                yield json.dumps({"type": "error", "data": str(e)}, ensure_ascii=False) + "\n"

            finally:
                gstats = get_global_stats()
                if collector.llm_calls == 0 and gstats["llm_calls"] > 0:
                    collector.llm_calls = gstats["llm_calls"]
                    collector.llm_time = gstats["llm_time"]
                if collector.embedding_calls == 0 and gstats["embedding_calls"] > 0:
                    collector.embedding_calls = gstats["embedding_calls"]
                    collector.embedding_time = gstats["embedding_time"]
                if collector.total_tokens_estimated == 0 and gstats["total_tokens"] > 0:
                    collector.total_tokens_estimated = gstats["total_tokens"]

                collector.total_time = time.time() - total_start_time
                print("⏱️ " + _elapsed() + " 📊 [Final] event_generator 结束, 总耗时 " + format(collector.total_time, ".2f") + "s")
                collector.print_query_report(query_text, selected_model)
                monitor.remove_collector(session_id)

        return StreamingResponse(event_generator(), media_type="application/x-ndjson")

    finally:
        globals.model_context.reset(model_token)
        globals.metrics_context.reset(metrics_token)

