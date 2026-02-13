import os
import re
import json
import time
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from openai import AsyncOpenAI

from app.rag.engine import QueryParam
from app.schemas.models import ChatRequest
from app.services.file_service import build_snippet_around_query
from app.core import globals
from app.utils.metrics import monitor


async def extract_keywords_via_llm(query: str) -> list[str]:
    """
    用轻量级 LLM 从用户问题中提取 1-3 个核心关键词，用于：
    1. 前端高亮显示
    2. 后端引用摘要的关键词定位
    
    使用 qwen-flash（最便宜最快），每次调用约 50-100 token。
    """
    try:
        api_key = os.environ.get("ALI_API_KEY")
        base_url = os.environ.get("ALI_BASE_URL")
        client = AsyncOpenAI(api_key=api_key, base_url=base_url)
        
        response = await client.chat.completions.create(
            model="qwen-flash",
            messages=[{
                "role": "user",
                "content": (
                    "从以下用户问题中提取1-3个核心关键词，用于在文档中高亮显示。\n"
                    "规则：\n"
                    "1. 只提取实质性的名词或短语（如成语、专业术语、人名、概念名）\n"
                    "2. 不要提取疑问词（什么、如何、怎么）和语气词\n"
                    "3. 只返回关键词，用中文逗号分隔，不要解释\n"
                    "4. 如果问题本身就是一个词（如一个成语），直接返回该词\n\n"
                    f"问题：{query}"
                )
            }],
            temperature=0,
            max_tokens=50
        )
        
        raw = response.choices[0].message.content.strip()
        # 解析：支持中文逗号和英文逗号
        terms = []
        for t in raw.replace("，", ",").split(","):
            t = t.strip().strip('"').strip("'").strip("、")
            if len(t) >= 2:
                terms.append(t)
        
        print(f"🔑 [Keywords] LLM 提取关键词: {terms} (原始: {raw})")
        return terms[:3]  # 最多3个
        
    except Exception as e:
        print(f"⚠️ [Keywords] LLM 提取失败，回退为空: {e}")
        return []


router = APIRouter()

# ===========================
# 💡 动态模型路由策略配置
# ===========================
MODEL_ROUTING = {
    "level_1_simple": {
        "model": "qwen-turbo",   # [简单]：问候、闲聊、简单事实
        "desc": "Cost-effective, Fast"
    },
    "level_2_medium": {
        "model": "qwen-max",     # [中等]：标准检索与分析 (替换已用完的qwen-plus)
        "desc": "Balanced Performance"
    },
    "level_3_complex": {
        "model": "deepseek-v3",  # [复杂]：深度推理、创意写作、复杂逻辑 ("DeepSeek选一个")
                                 # 备选: deepseek-r1-0528 (推理增强) 但 v3 用于通用对话更稳
        "desc": "High Intelligence, Reasoning"
    }
}

def analyze_query_complexity(query: str) -> str:
    """
    智能分析问题复杂度，决定使用哪个模型。
    TODO: 未来可接入 fasttext 或 qwen-turbo 进行 AI 分类
    目前使用规则引擎 (Heuristic Rule Engine)
    """
    query_len = len(query)
    
    # === Level 3: 复杂 (DeepSeek) ===
    # 关键词特征：推理、代码、分析、对比、深度、创新
    complex_keywords = [
        "为什么", "如何", "分析", "评价", "对比", "区别", 
        "设计", "代码", "算法", "优化", "重构", "翻译",
        "reason", "analysis", "compare", "code", "design"
    ]
    if query_len > 50 or any(k in query.lower() for k in complex_keywords):
        return MODEL_ROUTING["level_3_complex"]["model"]

    # === Level 1: 简单 (Qwen-Turbo) ===
    # 关键词特征：问候、简单的是非题、甚至不需要检索的
    simple_keywords = ["你好", "在吗", "hi", "hello", "是谁", "什么时间", "weather"]
    if query_len < 10 or any(k in query.lower() for k in simple_keywords):
        return MODEL_ROUTING["level_1_simple"]["model"]

    # === Level 2: 中等 (Qwen-Max) ===
    # 默认走中等模型
    return MODEL_ROUTING["level_2_medium"]["model"]


@router.post("/chat", summary="对话接口 (流式+引用)")
async def chat_with_rag(request: ChatRequest):
    if not globals.rag_engine:
        raise HTTPException(status_code=500, detail="RAG 引擎未初始化")

    query_text = request.query
    if isinstance(query_text, list):
        query_text = " ".join([str(item) for item in query_text])
    elif not isinstance(query_text, str):
        query_text = str(query_text)
    query_text = query_text.strip()
    if not query_text:
        raise HTTPException(status_code=400, detail="query 不能为空")

    # � 创建性能指标收集器
    session_id = f"query_{time.time()}"
    collector = monitor.create_collector(session_id)
    
    # ⏱️ 记录总耗时开始
    total_start_time = time.time()

    # 🚀 Step 1: 智能路由决策
    selected_model = analyze_query_complexity(query_text)
    print(f"💬 [Chat] 收到问题: {query_text}")
    print(f"🧠 [Router] 智能路由: 复杂度分析后选择模型 -> {selected_model}")

    # 🚀 Step 2: 设置上下文变量（影响本次请求的 engine 调用）
    model_token = globals.model_context.set(selected_model)
    metrics_token = globals.metrics_context.set(collector)

    try:
        # ⏱️ 记录检索阶段开始
        retrieval_start_time = time.time()
        
        # 使用 LightRAG 原生 aquery_llm，一步完成：检索 + LLM 生成 + 引用
        param = QueryParam(
            mode=request.mode or "hybrid",
            stream=True,
        )

        result = await globals.rag_engine.aquery_llm(query_text, param=param)
        
        # ⏱️ 记录检索阶段结束（注意：这里实际包含了部分生成，但大致可以这样分）
        collector.retrieval_time = time.time() - retrieval_start_time

        async def event_generator():
            # ⏱️ 记录生成阶段开始（首字延迟）
            generation_start_time = time.time()
            ttft_recorded = False
            
            try:
                # 初始发送：通知前端当前使用的模型 (可选，用于调试)
                yield json.dumps({"type": "meta", "data": {"model": selected_model}}, ensure_ascii=False) + "\n"

                # 用 LLM 提取关键词（用于高亮和摘要定位）
                extracted_keywords = await extract_keywords_via_llm(query_text)

                # 1. 从 result 中提取引用和 LLM 响应
                data = result.get("data", {})
                references = data.get("references", [])
                chunks = data.get("chunks", [])
                llm_response = result.get("llm_response", {})

                # 2. 构造 sources 列表（适配前端 SourceItem 接口）
                sources = []
                # 构建 reference_id -> chunk content 的映射
                ref_id_to_chunks = {}
                for chunk in chunks:
                    ref_id = chunk.get("reference_id", "")
                    content = chunk.get("content", "")
                    if ref_id and content:
                        ref_id_to_chunks.setdefault(ref_id, []).append(content)

                # 📊 记录检索统计
                collector.retrieved_chunks = len(chunks)
                retrieval_sources = []
                
                for ref in references:
                    ref_id = ref.get("reference_id", "0")
                    file_path = ref.get("file_path", "未知来源")
                    retrieval_sources.append(os.path.basename(file_path) if file_path else "未知来源")
                    
                    # 获取该引用对应的 chunk 内容
                    chunk_contents = ref_id_to_chunks.get(ref_id, [])
                    # 清洗内容：移除可能存在的元数据行
                    clean_contents = [c for c in chunk_contents if "【来源文档：" not in c]
                    full_content = "\n\n".join(clean_contents) if clean_contents else ""
                    
                    # 摘要：优先使用 LLM 提取的关键词定位
                    if extracted_keywords:
                        snippet = build_snippet_around_query(full_content, extracted_keywords[0], window=200) if full_content else ""
                    else:
                        snippet = build_snippet_around_query(full_content, query_text, window=200) if full_content else ""

                    sources.append({
                        "id": int(ref_id) if ref_id.isdigit() else 0,
                        "content": snippet,
                        "content_full": full_content,
                        "highlight_terms": extracted_keywords,  # ✅ 使用 LLM 提取的关键词，替代空列表
                        "source_filename": os.path.basename(file_path) if file_path else "未知来源",
                        "score": 1.0,
                    })
                
                collector.details['retrieval_sources'] = retrieval_sources

                # 3. 流式发送 LLM 内容
                if llm_response.get("is_streaming"):
                    response_stream = llm_response.get("response_iterator")
                    if response_stream:
                        async for chunk_text in response_stream:
                            if chunk_text:
                                # ⏱️ 记录首字延迟（TTFT - Time To First Token）
                                if not ttft_recorded:
                                    ttft = time.time() - generation_start_time
                                    collector.details['ttft'] = ttft
                                    ttft_recorded = True
                                
                                yield json.dumps({"type": "content", "data": chunk_text}, ensure_ascii=False) + "\n"
                else:
                    # 非流式回退
                    content = llm_response.get("content", "")
                    if content:
                        yield json.dumps({"type": "content", "data": content}, ensure_ascii=False) + "\n"
                
                # ⏱️ 记录生成阶段结束
                collector.generation_time = time.time() - generation_start_time

                # 4. 最后发送引用信息
                yield json.dumps({"type": "sources", "data": sources}, ensure_ascii=False) + "\n"

            except Exception as e:
                print(f"❌ [Chat] 流式输出异常: {str(e)}")
                yield json.dumps({"type": "error", "data": str(e)}, ensure_ascii=False) + "\n"
            
            finally:
                # 📊 记录总耗时并打印报告
                collector.total_time = time.time() - total_start_time
                collector.print_query_report(query_text, selected_model)
                
                # 清理收集器
                monitor.remove_collector(session_id)

        return StreamingResponse(event_generator(), media_type="application/x-ndjson")
    
    finally:
        # 🚀 Step 3: 清理上下文（虽然 asyncio task 隔离，但好习惯）
        globals.model_context.reset(model_token)
        globals.metrics_context.reset(metrics_token)

