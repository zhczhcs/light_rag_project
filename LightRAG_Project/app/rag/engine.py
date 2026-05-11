import os
import logging
import json
import re
import time
import asyncio
import contextvars
import numpy as np
from functools import partial
from lightrag import LightRAG, QueryParam
from lightrag.utils import EmbeddingFunc
from lightrag.prompt import PROMPTS
import aiohttp

from lightrag.rerank import ali_rerank, generic_rerank_api
from lightrag.kg.shared_storage import _init_flags, _shared_dicts, get_final_namespace
from openai import AsyncOpenAI
from app.core.globals import model_context, metrics_context  # ✅ 引入监控上下文
from app.core.runtime_config import get_query_runtime_config
from app.utils.table_printer import print_kv_table, print_simple_table

_QDRANT_HOST = os.environ.get("QDRANT_HOST")
_QDRANT_PORT = os.environ.get("QDRANT_PORT")
_QDRANT_URL = os.environ.get("QDRANT_URL")
_QDRANT_API_KEY = os.environ.get("QDRANT_API_KEY")

# ── 覆盖 LightRAG 默认英文无结果提示 → 中文 ──
PROMPTS["fail_response"] = "抱歉，知识库中没有找到与您问题相关的内容，无法回答。[no-context]"


# ── DS-V3 引用锚点注入 ──
# DeepSeek-V3 的"事实锚定"逻辑要求引用标记 [n] 物理上紧贴正文内容。
# LightRAG 默认将 reference_id 作为 JSON 字段放在 chunk 元数据中，但 DS-V3 不将其
# 视为引用锚点。此函数在 chunk 的 content 文本开头注入 [n] 标记，使 DS-V3 能看到
# 明确的内容-来源对应关系并自然地在回答中输出 [n] 引用。
#
# 同时，函数利用 operate.py 保留的 file_path 字段，将 Reference Document List 中的
# 文件名→引用编号映射注入到 Knowledge Graph 实体和关系的 JSON 中（source_refs 字段），
# 让 LLM 能够为图谱路径得出的结论提供可追溯的引用。
_GRAPH_FIELD_SEP = "<SEP>"

def _inject_ref_ids_into_chunks(system_prompt: str) -> str:
    """将 [n] 引用标记注入到 Document Chunks 的 content 字段开头，
    并将 source_refs 注入到 Knowledge Graph 实体/关系 JSON 中。

    Chunk 注入：
      输入：{"reference_id": "1", "content": "辗转相除法的核心是..."}
      输出：{"reference_id": "1", "content": "[1] 辗转相除法的核心是..."}

    图谱实体/关系注入（依赖 operate.py 保留的 file_path 字段）：
      输入：{"entity": "Lead Frame", "type": "component", "description": "...", "file_path": "xxx.pdf"}
      输出：{"entity": "Lead Frame", ..., "file_path": "xxx.pdf", "source_refs": ["1", "3"]}

    仅对包含 Reference Document List 的 RAG 回答 system_prompt 生效。
    """
    if "Reference Document List" not in system_prompt:
        return system_prompt

    # ── 第一步：从 Reference Document List 构建 filename → ref_id 映射 ──
    import re as _re
    ref_map: dict[str, str] = {}
    lines_for_ref = system_prompt.split('\n')
    in_ref_section = False
    in_ref_code_fence = False
    for raw_line in lines_for_ref:
        line = raw_line.strip()
        if line.startswith("Reference Document List"):
            in_ref_section = True
            continue

        if in_ref_section and line.startswith("```") and not in_ref_code_fence:
            in_ref_code_fence = True
            continue
        if in_ref_section and line.startswith("```") and in_ref_code_fence:
            break

        if in_ref_section and in_ref_code_fence and line:
            m = _re.match(r'\[(\d+)\]\s+(.+)', line)
            if m:
                ref_id_str, filepath = m.group(1), m.group(2).strip()
                ref_map[filepath] = ref_id_str
                # 也用 basename 作为备选 key，方便匹配图谱里的短路径
                ref_map[os.path.basename(filepath)] = ref_id_str

    injected_chunks = 0
    injected_entities = 0
    injected_relations = 0
    lines = system_prompt.split('\n')
    modified_lines = []

    # 跟踪当前在哪个段落（用于判断是图谱区还是 Chunk 区）
    in_entity_section = False
    in_relation_section = False

    for line in lines:
        stripped = line.strip()

        # ── 段落切换跟踪 ──
        if 'Knowledge Graph Data (Entity)' in line:
            in_entity_section = True
            in_relation_section = False
        elif 'Knowledge Graph Data (Relationship)' in line:
            in_entity_section = False
            in_relation_section = True
        elif 'Document Chunks' in line:
            in_entity_section = False
            in_relation_section = False

        # ── Chunk 引用注入（保持原逻辑） ──
        if stripped.startswith('{"reference_id":') and '"content":' in stripped:
            try:
                chunk = json.loads(stripped)
                ref_id = chunk.get("reference_id", "")
                content = chunk.get("content", "")
                if ref_id and not content.startswith(f"[{ref_id}]"):
                    chunk["content"] = f"[{ref_id}] {content}"
                    injected_chunks += 1
                modified_lines.append(json.dumps(chunk, ensure_ascii=False))
            except json.JSONDecodeError:
                modified_lines.append(line)

        # ── 实体 source_refs 注入 ──
        elif in_entity_section and ref_map and stripped.startswith('{"entity":'):
            try:
                entity = json.loads(stripped)
                file_path = entity.get("file_path", "")
                refs: list[str] = []
                for fp in file_path.split(_GRAPH_FIELD_SEP):
                    fp = fp.strip()
                    if fp in ref_map:
                        refs.append(ref_map[fp])
                    elif os.path.basename(fp) in ref_map:
                        refs.append(ref_map[os.path.basename(fp)])
                if refs:
                    entity["source_refs"] = sorted(set(refs), key=lambda x: int(x))
                    injected_entities += 1
                modified_lines.append(json.dumps(entity, ensure_ascii=False))
            except json.JSONDecodeError:
                modified_lines.append(line)

        # ── 关系 source_refs 注入 ──
        elif in_relation_section and ref_map and stripped.startswith('{"entity1":'):
            try:
                relation = json.loads(stripped)
                file_path = relation.get("file_path", "")
                refs = []
                for fp in file_path.split(_GRAPH_FIELD_SEP):
                    fp = fp.strip()
                    if fp in ref_map:
                        refs.append(ref_map[fp])
                    elif os.path.basename(fp) in ref_map:
                        refs.append(ref_map[os.path.basename(fp)])
                if refs:
                    relation["source_refs"] = sorted(set(refs), key=lambda x: int(x))
                    injected_relations += 1
                modified_lines.append(json.dumps(relation, ensure_ascii=False))
            except json.JSONDecodeError:
                modified_lines.append(line)

        else:
            modified_lines.append(line)

    if injected_chunks > 0 or injected_entities > 0 or injected_relations > 0:
        print_kv_table(
            "📌 RefInject: 引用锚点注入",
            {
                "注入 chunk 数": f"{injected_chunks} 个",
                "注入实体数": f"{injected_entities} 个",
                "注入关系数": f"{injected_relations} 个",
                "锚点格式": "[n]",
            },
            key_width=16, val_width=44,
        )

    return '\n'.join(modified_lines)

# ── Rerank 配置 ──
# qwen3-rerank：更便宜(0.0005/千token)，支持 instruct 参数
_RERANK_MODEL = os.environ.get("RERANK_MODEL")
_RERANK_BASE_URL = "https://dashscope.aliyuncs.com/compatible-api/v1/reranks"
_MAX_RERANK_CHUNKS = get_query_runtime_config().chunk_top_k  # Rerank 后最多保留的 chunk 数，减少 LLM 上下文 token 消耗

# ── 参考文献过滤上下文变量 ──
# 使用 contextvars 保证 asyncio 并发安全
_need_references_var = contextvars.ContextVar('need_references', default=True)

def set_need_references_flag(flag: bool):
    """设置是否需要参考文献切片的标志。由 chat.py 在调用 aquery 前设置。"""
    _need_references_var.set(flag)


def _is_reference_chunk(text: str, threshold: float = 0.55) -> bool:
    """
    基于统计密度判断切片是否以参考文献为主。

    统计每行中参考文献特征的密度，而非简单正则截断。
    特征包括：[数字] 编号、(Author, Year) 格式、纯URL、DOI、参考文献标题等。

    Args:
        text: 切片文本
        threshold: 密度阈值，超过此值则判定为参考文献切片（默认 0.55）

    Returns:
        True 表示该切片主要是参考文献列表
    """
    if not text or len(text.strip()) < 20:
        return False

    lines = [l.strip() for l in text.split('\n') if l.strip()]
    if not lines:
        return False

    # 如果总行数很少（<3行），不判定为参考文献（可能是正文中的少量引用）
    if len(lines) < 3:
        return False

    ref_indicators = 0.0

    for line in lines:
        line_lower = line.lower()
        # 指标1: [数字] 或 [数字-数字] 开头（如 [1], [12-15]）
        if re.match(r'^\[\d+(?:-\d+)?\]', line):
            ref_indicators += 1.0
            continue
        # 指标2: (Author, Year) 或 Author et al., Year 格式
        if re.search(r'\([A-Z][a-z]+(?:\s+et\s+al\.?)?,\s*\d{4}[a-z]?\)', line):
            ref_indicators += 1.0
            continue
        # 指标3: 纯URL行（参考文献常见格式）
        if re.match(r'^https?://\S+$', line):
            ref_indicators += 0.8
            continue
        # 指标4: DOI行
        if 'doi.org/' in line_lower or line_lower.startswith('doi:'):
            ref_indicators += 0.8
            continue
        # 指标5: "参考文献" 标题行
        if line in ('参考文献', 'References', 'Bibliography', 'REFERENCES', 'BIBLIOGRAPHY'):
            ref_indicators += 1.0
            continue
        # 指标6: Author. Title. Journal, Year. 格式（简单检测）
        if re.search(r'[A-Z][a-z]+\s+[A-Z]\.\s+.*\d{4}\.', line):
            ref_indicators += 0.6
            continue

    density = ref_indicators / len(lines)
    return density >= threshold


async def _logged_rerank(**kwargs):
    """
    直接调用 DashScope compatible rerank API，绕过 generic_rerank_api
    以完全控制请求/响应格式，并打印分数日志。
    
    增强：支持参考文献切片过滤（当 need_references=False 时，在 Rerank 前
    过滤掉以参考文献为主的切片，避免 Rerank 对文献列表给出虚高分数）。
    """
    api_key = kwargs.get("api_key") or os.environ.get("ALI_API_KEY") or os.environ.get("DASHSCOPE_API_KEY")
    model = kwargs.get("model", _RERANK_MODEL)
    base_url = kwargs.get("base_url", _RERANK_BASE_URL)
    query = kwargs["query"]
    documents = kwargs["documents"]
    top_n = kwargs.get("top_n") or _MAX_RERANK_CHUNKS  # 默认用 _MAX_RERANK_CHUNKS 限制返回数量

    # ── 参考文献切片过滤 ──
    need_references = kwargs.get("need_references", _need_references_var.get())
    filtered_docs = []
    filtered_indices = []
    
    if not need_references:
        for i, doc in enumerate(documents):
            if isinstance(doc, str) and not _is_reference_chunk(doc):
                filtered_docs.append(doc)
                filtered_indices.append(i)
        if len(filtered_docs) < len(documents):
            removed = [i for i in range(len(documents)) if i not in filtered_indices]
            print_kv_table(
                "📚 Rerank: 参考文献切片过滤",
                {
                    "原始切片数": str(len(documents)),
                    "过滤后切片数": str(len(filtered_docs)),
                    "移除索引": ", ".join(str(i) for i in removed[:5]) + ("..." if len(removed) > 5 else ""),
                    "need_references": "NO",
                },
                key_width=16, val_width=44,
            )
    else:
        filtered_docs = documents
        filtered_indices = list(range(len(documents)))
    
    # 如果过滤后无可用切片，直接返回空结果
    if not filtered_docs:
        print("⚠️ [Rerank] 过滤后无可用切片，返回空结果")
        return []

    # 构建扁平请求体（qwen3-rerank compatible API 格式）
    payload = {
        "model": model,
        "query": query,
        "documents": filtered_docs,
    }
    if top_n is not None:
        payload["top_n"] = top_n

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(base_url, headers=headers, json=payload) as resp:
                if resp.status != 200:
                    error_text = await resp.text()
                    print(f"⚠️ [Rerank] API error {resp.status}: {error_text[:300]}")
                    return []

                data = await resp.json()

                # 尝试两种响应格式：aliyun (output.results) 和 standard (results)
                results_raw = data.get("output", {}).get("results") or data.get("results") or []

                if not results_raw:
                    print(f"⚠️ [Rerank] 空结果，响应体 keys: {list(data.keys())}")
                    return []

                # 标准化，将过滤后的索引映射回原始索引
                results = [
                    {"index": filtered_indices[r["index"]], "relevance_score": r["relevance_score"]}
                    for r in results_raw
                ]

                # 安全截断（双保险：即使 API 未正确执行 top_n，也在本地截断）
                total_before = len(results)
                if _MAX_RERANK_CHUNKS and len(results) > _MAX_RERANK_CHUNKS:
                    results = results[:_MAX_RERANK_CHUNKS]

                # 打印分数日志（表格形式），增加内容预览列
                kept = len(results)
                score_rows = []
                for r in results:
                    idx = r["index"]
                    doc_preview = ""
                    if isinstance(documents, list) and 0 <= idx < len(documents):
                        doc_text = documents[idx]
                        if isinstance(doc_text, str):
                            doc_preview = doc_text.replace("\n", " ")[:20]
                    score_rows.append([
                        str(idx),
                        f"{r['relevance_score']:.4f}",
                        doc_preview,
                    ])
                print_simple_table(
                    f"📊 Rerank: 重排序结果 (query: {query[:35]}...)",
                    ["索引", "相关分数", "内容预览"],
                    score_rows,
                    col_widths=[8, 12, 22],
                )
                if total_before > kept:
                    print(f"   [Rerank] 截断: {total_before} → {kept} chunks")

                return results

    except Exception as e:
        print(f"⚠️ [Rerank] 异常: {e}")
        return []

# 设置日志
logging.basicConfig(format="%(levelname)s:%(message)s", level=logging.INFO)

# 📊 全局计数器（用于跨线程统计，LightRAG使用线程池时ContextVar无法传递）
_global_stats = {
    "embedding_calls": 0,
    "embedding_time": 0.0,
    "llm_calls": 0,
    "llm_time": 0.0,
    "total_tokens": 0,
    "last_response_tokens": 0
}

def reset_global_stats():
    """重置全局统计数据"""
    global _global_stats
    _global_stats = {
        "embedding_calls": 0,
        "embedding_time": 0.0,
        "llm_calls": 0,
        "llm_time": 0.0,
        "total_tokens": 0,
        "last_response_tokens": 0
    }

def get_global_stats():
    """获取全局统计数据"""
    return _global_stats.copy()

# ==========================================
# 1. 阿里百炼 LLM 适配函数 (支持动态模型)
# ==========================================
async def bailian_llm(prompt, system_prompt=None, history_messages=[], **kwargs) -> str:
    api_key = os.environ.get("ALI_API_KEY")
    base_url = os.environ.get("ALI_BASE_URL")
    
    # ✅ 动态模型选择策略（三段优先级）：
    #   1. 对话阶段：model_context 由 chat.py 的三级路由设置（turbo/max/deepseek）
    #   2. 索引阶段：model_context 为空（Celery Worker 进程），使用专用索引模型（默认 qwen-turbo）
    #      → 实体提取对模型智力要求不高，turbo 速度快 3-5x，显著缩短索引耗时
    #   3. 兜底：qwen-max（理论上不会走到这里）
    dynamic_model = model_context.get()
    if dynamic_model:
        # 对话阶段：使用三级路由指定的模型（chat.py 的 analyze_query_complexity 设置）
        model_name = dynamic_model
    else:
        # 索引阶段：读 LLM_INDEXING_MODEL 环境变量，默认 qwen3.5-flash
        model_name = os.environ.get("LLM_INDEXING_MODEL")
        if not hasattr(bailian_llm, "_indexing_model_logged"):
            print(f"🏗️  [Indexing LLM] 实体提取使用模型: {model_name}")
            bailian_llm._indexing_model_logged = True

    # 📊 性能监控：记录开始时间
    start_time = time.time()
    
    client = AsyncOpenAI(api_key=api_key, base_url=base_url)
    
    messages = []
    if system_prompt:
        # ── DS-V3 兼容：将 [n] 注入 chunk content 开头 ──
        system_prompt = _inject_ref_ids_into_chunks(system_prompt)
        messages.append({"role": "system", "content": system_prompt})
    if history_messages:
        messages.extend(history_messages)
    messages.append({"role": "user", "content": prompt})

    stream = kwargs.get("stream", False)

    create_kwargs = dict(
        model=model_name,
        messages=messages,
        temperature=kwargs.get("temperature", 0.1),
        top_p=kwargs.get("top_p", 1),
        n=kwargs.get("n", 1),
        stream=stream,
    )
    if stream:
        create_kwargs["stream_options"] = {"include_usage": True}  # 仅流式时传入，非流式不传（API 会报错）

    # 🔧 Qwen3 / Kimi 系列模型必须显式关闭 enable_thinking：
    #    - Qwen3: 非流式不传会 400，流式不传会生成无用思考链
    #    - Kimi K2.5: 默认开启思考模式，需显式关闭以直答
    #    注意：DeepSeek 系列没有 enable_thinking 参数，传了可能报错
    _model_lower = model_name.lower()
    _needs_disable_thinking = (
        _model_lower.startswith("qwen3") or
        _model_lower.startswith("kimi")
    )
    if _needs_disable_thinking:
        create_kwargs["extra_body"] = {"enable_thinking": False}

    response = await client.chat.completions.create(**create_kwargs)

    # 📊 性能监控：记录 LLM 调用
    duration = time.time() - start_time
    estimated_tokens = (len(prompt) + (len(system_prompt) if system_prompt else 0)) // 2
    
    # 尝试获取 ContextVar collector（对话阶段可用）
    collector = metrics_context.get()
    if collector:
        collector.add_llm_call(duration, estimated_tokens)
    else:
        # 如果 collector 为空（索引阶段，跨线程），记录到全局统计
        _global_stats["llm_calls"] += 1
        _global_stats["llm_time"] += duration
        _global_stats["total_tokens"] += estimated_tokens

    if stream:
        _captured_collector = collector  # 在创建时闭包捕获，避免 ContextVar 被 reset 后丢失
        async def stream_generator():
            async for chunk in response:
                # 正常内容块
                if chunk.choices and chunk.choices[0].delta.content:
                    yield chunk.choices[0].delta.content
                # 捕获最终 usage 块（stream_options include_usage=True 时阿里云会在最后一块附带）
                if hasattr(chunk, "usage") and chunk.usage and chunk.usage.total_tokens:
                    actual_tokens = chunk.usage.total_tokens
                    if _captured_collector:
                        _captured_collector.last_response_tokens = actual_tokens
                    _global_stats["total_tokens"] = actual_tokens
                    _global_stats["last_response_tokens"] = actual_tokens
        return stream_generator()
    else:
        return response.choices[0].message.content

# ==========================================
# 2. 阿里百炼 Embedding 适配函数 (批量化 + 容错降级)
# ==========================================
# 阿里云 text-embedding-v3/v4 API 限制：单次最多 10 条文本
EMBEDDING_BATCH_SIZE = 10
EMBEDDING_DIM = 1536


async def _embed_batch(client, batch_texts: list[str], model_name: str) -> list:
    """
    批量调用 Embedding API（单批最多 EMBEDDING_BATCH_SIZE 条）。
    返回与 batch_texts 等长的向量列表。
    如果批量调用失败，自动降级为逐条调用。
    """
    try:
        response = await client.embeddings.create(
            input=batch_texts,
            model=model_name,
            dimensions=EMBEDDING_DIM
        )
        # 校验返回数量：必须严格 == 输入数量
        if len(response.data) == len(batch_texts):
            return [item.embedding for item in response.data]
        else:
            # 数量不匹配（阿里云偶发的 Auto-Chunking 问题）→ 降级为逐条
            print(f"⚠️ [Embedding] 批量返回数量不匹配: 输入 {len(batch_texts)} 条, 返回 {len(response.data)} 条 → 降级逐条处理")
            return await _embed_batch_fallback(client, batch_texts, model_name)
    except Exception as e:
        print(f"⚠️ [Embedding] 批量调用失败: {e} → 降级逐条处理")
        return await _embed_batch_fallback(client, batch_texts, model_name)


async def _embed_batch_fallback(client, batch_texts: list[str], model_name: str) -> list:
    """逐条调用 Embedding API（批量失败时的降级方案）"""
    results = []
    for text in batch_texts:
        try:
            response = await client.embeddings.create(
                input=[text],
                model=model_name,
                dimensions=EMBEDDING_DIM
            )
            if response.data:
                results.append(response.data[0].embedding)
            else:
                results.append(np.zeros(EMBEDDING_DIM).tolist())
        except Exception as e:
            print(f"❌ [Embedding] 单条处理失败: {e}")
            results.append(np.zeros(EMBEDDING_DIM).tolist())
    return results


async def bailian_embedding(texts: list[str]) -> np.ndarray:
    api_key = os.environ.get("ALI_API_KEY")
    base_url = os.environ.get("ALI_BASE_URL")
    
    # ✅ 强制从环境变量读取 Embedding 模型（无默认值）
    model_name = os.environ.get("EMBEDDING_MODEL")
    if not model_name:
        raise ValueError("❌ 环境变量 EMBEDDING_MODEL 未设置，请在 .env 中配置（如 text-embedding-v2 或 text-embedding-v4）")
    
    # 🔍 日志：显示实际使用的 Embedding 模型和批量配置（仅首次）
    if not hasattr(bailian_embedding, "_logged"):
        print(f"📊 [Embedding] 使用模型: {model_name}, 批量大小: {EMBEDDING_BATCH_SIZE}")
        bailian_embedding._logged = True

    # 📊 性能监控：记录开始时间
    start_time = time.time()
    
    client = AsyncOpenAI(api_key=api_key, base_url=base_url)
    
    # 🚀 批量处理：每 EMBEDDING_BATCH_SIZE 条为一批调用 API
    # 相比逐条调用，N 条文本从 N 次网络往返降至 ceil(N/10) 次
    results = []
    api_calls = 0
    
    for i in range(0, len(texts), EMBEDDING_BATCH_SIZE):
        batch = texts[i:i + EMBEDDING_BATCH_SIZE]
        batch_vectors = await _embed_batch(client, batch, model_name)
        results.extend(batch_vectors)
        api_calls += 1
    
    # 🔍 批量效率日志（仅当文本数 > 1 时打印，避免对话阶段单条查询刷屏）
    if len(texts) > 1:
        print(f"📊 [Embedding] {len(texts)} 条文本 → {api_calls} 次 API 调用 (批量大小 {EMBEDDING_BATCH_SIZE})")
    
    # 📊 性能监控：记录 Embedding 调用
    duration = time.time() - start_time
    
    # 尝试获取 ContextVar collector（对话阶段可用）
    collector = metrics_context.get()
    if collector:
        collector.add_embedding_call(duration, len(texts))
    else:
        # 如果 collector 为空（索引阶段，跨线程），记录到全局统计
        # 用实际字符数估算 token（中英混合约 0.75 token/字符），比固定值 100 更准确
        actual_chars = sum(len(t) for t in texts)
        estimated_tokens = max(1, int(actual_chars * 0.75))
        _global_stats["embedding_calls"] += len(texts)
        _global_stats["embedding_time"] += duration
        _global_stats["total_tokens"] += estimated_tokens
            
    return np.array(results)

# ==========================================
# 3. 初始化 RAG 引擎
# ==========================================
WORKING_DIR = "./data"

def get_rag_engine():
    if not os.path.exists(WORKING_DIR):
        os.mkdir(WORKING_DIR)

    # 注入环境变量
    if _QDRANT_URL:
        os.environ["QDRANT_URL"] = _QDRANT_URL
    if _QDRANT_API_KEY is not None:
        os.environ["QDRANT_API_KEY"] = _QDRANT_API_KEY
    os.environ["VECTOR_STORAGE"] = "QdrantVectorDBStorage"

    print(f"🌍 [System] 已配置远程数据库: {os.environ['QDRANT_URL']}")

    rag = LightRAG(
        working_dir=WORKING_DIR,
        chunk_token_size=1600,             # 提高 chunk 尺寸，减少语义链路被过度切碎，便于图谱抽取保留完整证据
        chunk_overlap_token_size=100,      # 保持默认 100，相邻 chunk 有 100 token 重叠保证上下文连贯
        vector_storage="QdrantVectorDBStorage",
        vector_db_storage_cls_kwargs={
            "url": os.environ["QDRANT_URL"],
            "api_key": os.environ["QDRANT_API_KEY"],
            "collection_name": "lightrag_vdb",
            "prefer_grpc": False
        },
        llm_model_func=bailian_llm,
        llm_model_max_async=6,   # 默认 4，提升到 6 让多 chunk 并行提取，对大文档有明显收益
        embedding_func=EmbeddingFunc(
            embedding_dim=1536,
            max_token_size=8192,
            func=bailian_embedding,
        ),
        # 🔄 Rerank：用阿里云 qwen3-rerank 对检索结果重排序，高分 chunk 排前面
        rerank_model_func=partial(
            _logged_rerank,
            api_key=os.environ.get("ALI_API_KEY"),
            model=_RERANK_MODEL,
            base_url=_RERANK_BASE_URL,
        ),
        min_rerank_score=float(os.environ.get("MIN_RERANK_SCORE", 0.25)),  # 丢弃 Rerank 分数低于此阈值的 chunk
        related_chunk_number=int(os.environ.get("RELATED_CHUNK_NUMBER", 5)),  # 图谱路径每实体均摊目标 chunk 数
    )
    return rag


# ==========================================
# 4. Workspace 级 RAG 引擎隔离（部门共享或用户独占）
#    workspace 格式：
#      - 有部门用户：dept_{dept_id}  （同部门共享知识库）
#      - 无部门用户：user_{user_id}  （仅自己访问）
# ==========================================
_user_engines: dict[str, LightRAG] = {}
_engine_dirty_revisions: dict[str, str | None] = {}
_engine_lock = asyncio.Lock()

# Qdrant 服务器配置（复用模块顶部统一配置）


# ── 旧的 user_id 版函数保留为向后兼容的包装器 ──

def _create_engine_for_user(user_id: int) -> LightRAG:
    """向后兼容：按用户ID创建引擎（内部委托 workspace 版）"""
    return _create_engine_for_workspace(f"user_{user_id}")


def _create_engine_for_workspace(workspace: str) -> LightRAG:
    """为指定 workspace 创建独立的 LightRAG 引擎（共享 working_dir，通过 workspace 隔离）

    Args:
        workspace: 隔离标识，格式为 dept_{id} 或 user_{id}
    """
    os.makedirs(WORKING_DIR, exist_ok=True)

    if _QDRANT_URL:
        os.environ["QDRANT_URL"] = _QDRANT_URL
    if _QDRANT_API_KEY is not None:
        os.environ["QDRANT_API_KEY"] = _QDRANT_API_KEY
    os.environ["VECTOR_STORAGE"] = "QdrantVectorDBStorage"

    return LightRAG(
        working_dir=WORKING_DIR,
        workspace=workspace,
        chunk_token_size=1600,
        chunk_overlap_token_size=100,
        vector_storage="QdrantVectorDBStorage",
        vector_db_storage_cls_kwargs={
            "url": _QDRANT_URL,
            "api_key": _QDRANT_API_KEY,
            "prefer_grpc": False
        },
        llm_model_func=bailian_llm,
        llm_model_max_async=6,
        embedding_func=EmbeddingFunc(
            embedding_dim=1536,
            max_token_size=8192,
            func=bailian_embedding,
        ),
        rerank_model_func=partial(
            _logged_rerank,
            api_key=os.environ.get("ALI_API_KEY"),
            model=_RERANK_MODEL,
            base_url=_RERANK_BASE_URL,
        ),
        min_rerank_score=float(os.environ.get("MIN_RERANK_SCORE", 0.25)),
        related_chunk_number=int(os.environ.get("RELATED_CHUNK_NUMBER", 5)),  # 图谱路径每实体均摊目标 chunk 数
    )


def _dirty_flag_path(user_id: int) -> str:
    """向后兼容：返回用户引擎脏标记文件路径"""
    return _dirty_flag_path_workspace(f"user_{user_id}")


def _dirty_flag_path_workspace(workspace: str) -> str:
    """返回 workspace 引擎脏标记文件路径"""
    return os.path.join(WORKING_DIR, workspace, ".engine_dirty")


def _read_dirty_revision_workspace(workspace: str) -> str | None:
    """读取 workspace 当前 dirty 版本戳。"""
    flag_path = _dirty_flag_path_workspace(workspace)
    if not os.path.exists(flag_path):
        return None

    try:
        with open(flag_path, "r", encoding="utf-8") as f:
            revision = f.read().strip()
    except OSError:
        return None

    return revision or None


def _sync_dirty_revision_workspace(workspace: str):
    """将进程内 last_seen 对齐到当前 dirty 版本戳。"""
    _engine_dirty_revisions[workspace] = _read_dirty_revision_workspace(workspace)


def mark_engine_dirty(user_id: int):
    """向后兼容：标记用户引擎 dirty"""
    mark_engine_dirty_workspace(f"user_{user_id}")


def mark_engine_dirty_workspace(workspace: str):
    """标记 workspace 引擎数据已变更（跨进程信号）"""
    flag_path = _dirty_flag_path_workspace(workspace)
    os.makedirs(os.path.dirname(flag_path), exist_ok=True)
    revision = f"{time.time_ns()}:{os.getpid()}"
    with open(flag_path, "w") as f:
        f.write(revision)
    print(f"🏴 [Engine] workspace '{workspace}' 的引擎已标记为 dirty（rev={revision}）")


def _check_and_clear_dirty(user_id: int) -> bool:
    """向后兼容：检查用户引擎 dirty 标记"""
    return _check_and_clear_dirty_workspace(f"user_{user_id}")


def _check_and_clear_dirty_workspace(workspace: str) -> bool:
    """检查 workspace dirty 版本戳是否变化，如果变化则返回 True。"""
    revision = _read_dirty_revision_workspace(workspace)
    previous_revision = _engine_dirty_revisions.get(workspace)
    if revision is None:
        _engine_dirty_revisions[workspace] = None
        return False
    if revision != previous_revision:
        _engine_dirty_revisions[workspace] = revision
        return True
    return False


def _clear_workspace_shared_data(user_id: int):
    """向后兼容：清除用户 workspace 缓存"""
    _clear_workspace_shared_data_workspace(f"user_{user_id}")


def _clear_workspace_shared_data_workspace(workspace: str):
    """清除指定 workspace 在 LightRAG shared_storage 中的所有缓存"""
    namespaces = [
        "text_chunks", "full_docs", "full_entities", "full_relations",
        "entity_chunks", "relation_chunks", "llm_response_cache",
        "doc_status", "pipeline_status",
    ]
    cleared = 0
    for ns in namespaces:
        key = f"{workspace}:{ns}" if workspace else ns
        if _init_flags is not None and key in _init_flags:
            del _init_flags[key]
            cleared += 1
        if _shared_dicts is not None and key in _shared_dicts:
            del _shared_dicts[key]
    if cleared > 0:
        print(f"🧹 [Engine] 已清除 workspace '{workspace}' 的 {cleared} 个 namespace 缓存标记")


async def get_workspace_engine(workspace: str) -> LightRAG:
    """获取或懒加载指定 workspace 的 RAG 引擎（线程安全 + 跨进程脏标记检测）

    Args:
        workspace: 隔离标识，格式为 dept_{id}（部门共享）或 user_{id}（用户独占）
    """
    import time
    if workspace in _user_engines and _check_and_clear_dirty_workspace(workspace):
        print(f"🔄 [Engine] 检测到 workspace '{workspace}' 的 dirty 标记，丢弃旧引擎并重建...")
        del _user_engines[workspace]
        _clear_workspace_shared_data_workspace(workspace)

    if workspace in _user_engines:
        return _user_engines[workspace]

    async with _engine_lock:
        if workspace not in _user_engines:
            _engine_start = time.time()
            print(f"🔄 [Engine] 为 workspace '{workspace}' 创建独立 RAG 引擎...")
            engine = _create_engine_for_workspace(workspace)
            _init_start = time.time()
            await engine.initialize_storages()
            _init_cost = time.time() - _init_start
            _total_cost = time.time() - _engine_start
            _user_engines[workspace] = engine
            _sync_dirty_revision_workspace(workspace)
            print(f"✅ [Engine] workspace '{workspace}' 的引擎已就绪 (create={_total_cost - _init_cost:.2f}s, init={_init_cost:.2f}s, total={_total_cost:.2f}s)")

    return _user_engines[workspace]


async def get_user_engine(user_id: int) -> LightRAG:
    """向后兼容：按用户ID获取引擎（无部门场景）"""
    return await get_workspace_engine(f"user_{user_id}")


def invalidate_workspace_engine(workspace: str):
    """删除文档后清除 workspace 引擎缓存，下次访问时重新初始化"""
    if workspace in _user_engines:
        del _user_engines[workspace]
        _clear_workspace_shared_data_workspace(workspace)
        print(f"🔄 [Engine] workspace '{workspace}' 的引擎缓存已清除")
    _sync_dirty_revision_workspace(workspace)


def invalidate_user_engine(user_id: int):
    """向后兼容：按用户ID清除引擎缓存"""
    invalidate_workspace_engine(f"user_{user_id}")