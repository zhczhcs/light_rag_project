"""
对话上下文管理服务

功能：
1. 从数据库读取当前会话的历史消息
2. 按 token 数量截断（保守策略，保留最近的对话）
3. 返回 LightRAG QueryParam 所需的 conversation_history 格式

上下文长度策略：
- 统一固定预算：历史上下文 6000 token
  
注意：LightRAG 检索 + system prompt + 当前 query 本身也会占用大量 token，
所以给历史上下文的配额要保守，避免超出模型限制导致截断或报错。
"""

import re
import os
import json
import time
from sqlalchemy.orm import Session
from openai import AsyncOpenAI
from app.database import ChatMessageModel
from app.utils.table_printer import print_kv_table, print_simple_table

try:
    import jieba
    from rank_bm25 import BM25Okapi
    BM25_ENABLED = True
except Exception:
    jieba = None
    BM25Okapi = None
    BM25_ENABLED = False

# =============================================
# 历史上下文 token 预算（统一固定值）
# =============================================
FIXED_CONTEXT_BUDGET = 8000

# 每轮对话最多保留多少轮（安全上限，防止极端情况）
MAX_HISTORY_TURNS = 10

# 增强上下文策略（稳健默认值）
RECENT_HISTORY_RATIO = 0.65
RETRIEVAL_HISTORY_RATIO = 0.20
SUMMARY_RATIO = 0.15
MAX_RECENT_TURNS = 6
MAX_RETRIEVED_MESSAGES = 4

# LLM 摘要配置（仅在历史超预算时触发）
CONTEXT_SUMMARY_MODEL = os.environ.get("CONTEXT_SUMMARY_MODEL", "glm-4.5-air")
# 8k 总预算下，按 15% 比例对应约 1200
SUMMARY_MIN_TOKENS = 1200
SUMMARY_MAX_TOKENS = 2500
SUMMARY_MAX_NEW_POINTS = 8
SUMMARY_STORE_PATH = os.path.join("./data", "session_context_summaries.json")

# 混合召回权重（Embedding + BM25 + 时间）
EMBEDDING_WEIGHT = 0.60
BM25_WEIGHT = 0.30
RECENCY_WEIGHT = 0.10


def _load_summary_store() -> dict[str, str]:
    try:
        if not os.path.exists(SUMMARY_STORE_PATH):
            return {}
        with open(SUMMARY_STORE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception as e:
        print(f"⚠️ [Context+] 读取摘要存储失败: {e}")
        return {}


def _save_summary_store(store: dict[str, str]) -> None:
    try:
        os.makedirs(os.path.dirname(SUMMARY_STORE_PATH), exist_ok=True)
        with open(SUMMARY_STORE_PATH, "w", encoding="utf-8") as f:
            json.dump(store, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"⚠️ [Context+] 写入摘要存储失败: {e}")


def _get_persisted_summary(session_id: int) -> str:
    store = _load_summary_store()
    return (store.get(str(session_id), "") or "").strip()


def _set_persisted_summary(session_id: int, summary_text: str) -> None:
    store = _load_summary_store()
    store[str(session_id)] = summary_text
    _save_summary_store(store)


def _fallback_rule_summary(previous_summary: str, new_points: list[str], token_budget: int) -> str:
    parts = []
    if previous_summary:
        parts.append("上一版摘要：" + previous_summary)
    if new_points:
        joined = "；".join(new_points[-SUMMARY_MAX_NEW_POINTS:])
        parts.append("新增历史：" + joined)
    text = "；".join(parts).strip()
    if not text:
        return ""
    return _trim_to_token_budget(text, token_budget)


async def _generate_summary_with_llm(
    session_id: int,
    query_text: str,
    previous_summary: str,
    new_points: list[str],
    token_budget: int,
) -> str:
    api_key = os.environ.get("ALI_API_KEY")
    base_url = os.environ.get("ALI_BASE_URL")
    if not api_key or not base_url:
        raise ValueError("ALI_API_KEY 或 ALI_BASE_URL 未配置")

    source_sections = []
    if previous_summary:
        source_sections.append("[上一版摘要]\n" + previous_summary)
    if new_points:
        numbered = "\n".join(f"- {p}" for p in new_points[-SUMMARY_MAX_NEW_POINTS:])
        source_sections.append("[新增历史对话要点]\n" + numbered)
    source_text = "\n\n".join(source_sections).strip()

    # 控制输入长度，避免摘要调用本身失控
    source_text = _trim_to_token_budget(source_text, 4500)

    system_prompt = (
        "你是对话记忆压缩器。"
        "请把历史对话压缩为后续问答可复用的中文摘要，"
        "重点保留人物/实体、事实、结论、未解决问题、用户偏好与约束。"
        "不要编造，不要扩写。"
    )
    user_prompt = (
        f"当前问题：{query_text}\n\n"
        "请输出新的滚动摘要（用于替换旧摘要）。\n"
        f"长度目标：约 {SUMMARY_MIN_TOKENS}-{SUMMARY_MAX_TOKENS} tokens。"
        "若源信息不足，可低于目标长度，但必须完整保留关键事实。\n"
        "输出纯摘要正文，不要加标题。\n\n"
        "待压缩内容：\n"
        f"{source_text}"
    )

    client = AsyncOpenAI(api_key=api_key, base_url=base_url)
    response = await client.chat.completions.create(
        model=CONTEXT_SUMMARY_MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.1,
        max_tokens=2200,
        stream=True,
        extra_body={"enable_thinking": False},
    )

    chunks = []
    async for chunk in response:
        if chunk.choices and chunk.choices[0].delta and chunk.choices[0].delta.content:
            chunks.append(chunk.choices[0].delta.content)

    summary_text = "".join(chunks).strip()
    if not summary_text:
        raise ValueError("LLM 摘要为空")

    return _trim_to_token_budget(summary_text, token_budget)


def estimate_tokens(text: str) -> int:
    """
    粗略估算中英文混合文本的 token 数。
    
    规则：
    - 中文：约 1.5 token/字（实际 tokenizer 因模型而异，取保守值）
    - 英文：约 1 token / 4 字符
    - 综合：按 len(text) * 0.75 估算（偏保守）
    
    这个估算故意偏高，确保不会超出限制。
    """
    if not text:
        return 0
    
    # 统计中文字符数
    chinese_chars = sum(1 for c in text if '\u4e00' <= c <= '\u9fff')
    other_chars = len(text) - chinese_chars
    
    # 中文 ~1.5 token/字，英文 ~0.25 token/字符
    estimated = chinese_chars * 1.5 + other_chars * 0.35
    return int(estimated) + 1  # +1 避免下取整


def build_conversation_history(
    db: Session,
    session_id: int,
    model_name: str,
    exclude_last_user_msg: bool = True,
) -> list[dict[str, str]]:
    """
    从数据库构建对话历史，用于传入 QueryParam.conversation_history。
    
    策略：
    1. 从 DB 读取当前 session 的所有消息（按时间正序）
    2. 排除最后一条 user 消息（因为它就是当前 query，会单独传）
    3. 从最新往最旧遍历，累计 token 数，超出预算则截断
    4. 最终返回按时间正序排列的历史（LLM 需要正序）
    
    Args:
        db: 数据库 session
        session_id: 当前对话 session ID
        model_name: 当前使用的模型名（决定 token 预算）
        exclude_last_user_msg: 是否排除最后一条 user 消息（默认 True）
    
    Returns:
        list[dict]: [{"role": "user"/"assistant", "content": "..."}]
        空列表如果没有历史或 session_id 无效
    """
    if not session_id:
        return []
    
    try:
        # 1. 查询该 session 的所有消息，按创建时间正序
        messages = db.query(ChatMessageModel).filter(
            ChatMessageModel.session_id == session_id
        ).order_by(ChatMessageModel.created_at.asc()).all()
        
        if not messages:
            return []
        
        # 2. 排除最后一条 user 消息（它就是当前正在问的问题）
        if exclude_last_user_msg and messages and messages[-1].role == "user":
            messages = messages[:-1]
        
        if not messages:
            return []
        
        # 3. 转换为标准格式
        #    数据库中 role 为 "ai"，LLM 需要 "assistant"
        formatted = []
        for msg in messages:
            if not msg.content or not msg.content.strip():
                continue
            role = "assistant" if msg.role == "ai" else "user"
            formatted.append({
                "role": role,
                "content": msg.content.strip()
            })
        
        if not formatted:
            return []
        
        # 4. 使用统一固定 token 预算
        budget = FIXED_CONTEXT_BUDGET
        
        # 5. 从最新往最旧累计 token，超出预算则截断
        #    保留最近的对话，丢弃最远古的
        selected = []
        total_tokens = 0
        
        for msg in reversed(formatted):
            msg_tokens = estimate_tokens(msg["content"])
            if total_tokens + msg_tokens > budget:
                break  # 超出预算，停止添加更早的消息
            selected.append(msg)
            total_tokens += msg_tokens
        
        # 限制最大轮数
        if len(selected) > MAX_HISTORY_TURNS * 2:
            selected = selected[:MAX_HISTORY_TURNS * 2]
        
        # 6. 反转回正序（LLM 需要按时间正序）
        selected.reverse()
        
        # 7. 确保对话历史以 user 消息开头（避免格式错误）
        while selected and selected[0]["role"] == "assistant":
            selected.pop(0)
        
        history_tokens = sum(estimate_tokens(m["content"]) for m in selected)
        turn_count = sum(1 for m in selected if m["role"] == "user")
        print(f"📝 [Context] 构建历史上下文: {len(selected)} 条消息, "
              f"~{history_tokens} tokens, {turn_count} 轮对话, "
              f"预算 {budget} tokens ({model_name})")
        
        return selected
        
    except Exception as e:
        # 上下文构建失败不应阻塞主流程，降级为无上下文
        print(f"⚠️ [Context] 构建历史上下文失败，降级为无上下文: {e}")
        return []


def _normalize_text_for_match(text: str) -> str:
    if not text:
        return ""
    return text.lower().strip()


def _tokenize_for_bm25(text: str) -> list[str]:
    if not text:
        return []
    if not BM25_ENABLED or jieba is None:
        # 无 jieba 时降级到最朴素切词，保证流程不中断
        return [tok for tok in re.split(r"\s+", text.strip()) if tok]
    return [tok.strip() for tok in jieba.lcut(text) if tok and tok.strip()]


def _minmax_normalize(values: list[float]) -> list[float]:
    if not values:
        return []
    v_min = min(values)
    v_max = max(values)
    if abs(v_max - v_min) < 1e-12:
        return [0.0 for _ in values]
    return [(v - v_min) / (v_max - v_min) for v in values]


def _trim_to_token_budget(text: str, budget: int) -> str:
    if not text:
        return ""
    if budget <= 0:
        return ""
    if estimate_tokens(text) <= budget:
        return text

    # 按字符二分逼近预算，避免复杂 tokenizer 依赖
    lo, hi = 0, len(text)
    best = ""
    while lo <= hi:
        mid = (lo + hi) // 2
        cand = text[:mid].rstrip()
        if estimate_tokens(cand) <= budget:
            best = cand
            lo = mid + 1
        else:
            hi = mid - 1
    return best


def _ctx_trace_prefix(trace_id: str) -> str:
    return f"[trace={trace_id}] " if trace_id else ""


def _extract_recent_context(msg_pool: list[dict], recent_budget: int, trace_id: str = "") -> tuple[list[dict], list[dict]]:
    """
    从 msg_pool 尾部（最新）向首部遍历，按 recent_budget 和 MAX_RECENT_TURNS 提取消息。
    返回: (选中的 recent_msgs, 剩余未被选中的 msg_pool)
    """
    recent_msgs = []
    selected_indices = set()
    recent_tokens = 0
    user_turns = 0
    recent_break_reason = "normal_end"

    for idx in range(len(msg_pool) - 1, -1, -1):
        msg = msg_pool[idx]
        tks = estimate_tokens(msg["content"])
        if recent_tokens + tks > recent_budget:
            recent_break_reason = (
                f"budget_limit(idx={msg.get('_seq_idx')}, id={msg.get('msg_id')}, role={msg['role']}, "
                f"msg_tokens={tks}, recent_tokens={recent_tokens})"
            )
            break
        if msg["role"] == "user" and user_turns >= MAX_RECENT_TURNS:
            recent_break_reason = (
                f"max_recent_turns(idx={msg.get('_seq_idx')}, id={msg.get('msg_id')}, user_turns={user_turns}, "
                f"max={MAX_RECENT_TURNS})"
            )
            break
        recent_msgs.append(msg)
        selected_indices.add(idx)
        recent_tokens += tks
        if msg["role"] == "user":
            user_turns += 1

    recent_msgs.reverse()
    remaining_pool = [m for i, m in enumerate(msg_pool) if i not in selected_indices]

    print_kv_table(
        f"📌 Context+: Recent 窗口 [trace={trace_id}]",
        {
            "选中消息数": f"{len(recent_msgs)} 条",
            "选中 tokens": str(recent_tokens),
            "选中用户轮数": str(user_turns),
            "中断原因": recent_break_reason,
            "入池消息数": f"{len(msg_pool)} 条",
            "出池消息数": f"{len(remaining_pool)} 条",
        },
        key_width=16, val_width=44,
    )
    return recent_msgs, remaining_pool


async def _extract_retrieved_context(
    msg_pool: list[dict],
    query_text: str,
    retrieval_budget: int,
    total_msg_count: int,
    trace_id: str = "",
) -> tuple[list[dict], list[dict]]:
    """
    对剩余 msg_pool 进行 Embedding + BM25 打分并执行 QA 原子化召回。
    返回: (选中的 retrieved_msgs, 再次剩余未被选中的 msg_pool)
    """
    print_kv_table(
        f"📌 Context+: Retrieved 判定 [trace={trace_id}]",
        {
            "older_msgs": f"{len(msg_pool)} 条",
            "检索预算": str(retrieval_budget),
            "是否触发": "是" if (len(msg_pool) > 0 and retrieval_budget > 0) else "否",
        },
        key_width=16, val_width=44,
    )

    if not msg_pool:
        print_kv_table(
            f"🔎 Context+: 混合召回 [trace={trace_id}]",
            {"状态": "未触发", "原因": "没有 older_msgs（全部历史都在 recent 窗口内）"},
            key_width=16, val_width=44,
        )
        return [], msg_pool

    from app.rag.engine import bailian_embedding
    import numpy as np

    texts_to_embed = [query_text] + [m["content"] for m in msg_pool]
    older_candidates = []
    try:
        print_kv_table(
            f"🔎 Context+: 混合召回启动 [trace={trace_id}]",
            {
                "候选数": f"{len(msg_pool)} 条",
                "BM25": "启用" if BM25_ENABLED else "禁用",
                "策略": "Embedding + BM25",
            },
            key_width=16, val_width=44,
        )

        embeddings = await bailian_embedding(texts_to_embed)
        query_vec = np.array(embeddings[0])
        doc_vecs = [np.array(vec) for vec in embeddings[1:]]

        emb_raw_scores = []
        for i, _msg in enumerate(msg_pool):
            doc_vec = doc_vecs[i]
            norm_q = np.linalg.norm(query_vec)
            norm_d = np.linalg.norm(doc_vec)
            if norm_q < 1e-9 or norm_d < 1e-9:
                cos_sim = 0.0
            else:
                cos_sim = float(np.dot(query_vec, doc_vec) / (norm_q * norm_d))
            emb_raw_scores.append(max(0.0, cos_sim))
        emb_norm_scores = _minmax_normalize(emb_raw_scores)

        bm25_raw_scores = [0.0 for _ in msg_pool]
        bm25_norm_scores = [0.0 for _ in msg_pool]
        if BM25_ENABLED and BM25Okapi is not None:
            corpus_tokens = [_tokenize_for_bm25(m["content"]) for m in msg_pool]
            query_tokens = _tokenize_for_bm25(query_text)
            if query_tokens and any(corpus_tokens):
                bm25 = BM25Okapi(corpus_tokens)
                bm25_raw_scores = [max(0.0, float(x)) for x in bm25.get_scores(query_tokens)]
                bm25_norm_scores = _minmax_normalize(bm25_raw_scores)
                print(f"   [BM25] query_tokens={len(query_tokens)}")
            else:
                print("   [BM25] query/corpus 分词为空，降级为 0 分")
        else:
            print("   [BM25] 依赖不可用，自动降级为纯 Embedding")

        for i, msg in enumerate(msg_pool):
            seq_idx = msg.get("_seq_idx", i)
            recency_score = (seq_idx + 1) / max(1, total_msg_count)
            final_score = (
                EMBEDDING_WEIGHT * emb_norm_scores[i]
                + BM25_WEIGHT * bm25_norm_scores[i]
                + RECENCY_WEIGHT * recency_score
            )
            older_candidates.append(
                (
                    final_score,
                    i,
                    msg,
                    msg.get("msg_id"),
                    emb_raw_scores[i],
                    emb_norm_scores[i],
                    bm25_raw_scores[i],
                    bm25_norm_scores[i],
                    recency_score,
                )
            )
    except Exception as e:
        print(f"{_ctx_trace_prefix(trace_id)}⚠️ [Context+] 向量化检索异常，跳过本轮轻历史增强: {e}")

    older_candidates.sort(key=lambda x: x[0], reverse=True)
    retrieved_raw = older_candidates[:MAX_RETRIEVED_MESSAGES]
    retrieved_raw.sort(key=lambda x: x[2].get("_seq_idx", x[1]))

    if older_candidates:
        hit_rows = []
        for score, _local_idx, msg, msg_id, emb_raw, emb_norm, bm25_raw, bm25_norm, recency in older_candidates[:8]:
            preview = msg["content"].replace("\n", " ")[:30]
            hit_rows.append([
                str(msg.get("_seq_idx", "-")),
                msg["role"],
                f"{score:.3f}",
                f"{emb_raw:.3f}",
                f"{bm25_raw:.3f}",
                preview,
            ])
        print_simple_table(
            f"🔎 Context+: 混合召回排序 (Top 8) [trace={trace_id}]",
            ["序号", "角色", "最终分", " emb_raw", " bm25_raw", "预览"],
            hit_rows,
            col_widths=[6, 6, 8, 9, 9, 20],
        )
    else:
        print_kv_table(
            f"🔎 Context+: 混合召回排序 [trace={trace_id}]",
            {"状态": "无可用历史候选"},
            key_width=16, val_width=44,
        )

    retrieved_msgs = []
    retrieved_tokens = 0
    selected_local_indices = set()

    for _, local_idx, msg, msg_id, _, _, _, _, _ in retrieved_raw:
        pair_indices = [local_idx]
        if msg["role"] == "user":
            next_idx = local_idx + 1
            if next_idx < len(msg_pool) and msg_pool[next_idx]["role"] == "assistant":
                pair_indices.append(next_idx)

        if len(pair_indices) == 2:
            p0 = msg_pool[pair_indices[0]]
            p1 = msg_pool[pair_indices[1]]
            pair_tokens = estimate_tokens(p0["content"]) + estimate_tokens(p1["content"])

            if pair_indices[0] in selected_local_indices or pair_indices[1] in selected_local_indices:
                pass
            elif retrieved_tokens + pair_tokens <= retrieval_budget:
                retrieved_msgs.append(p0)
                retrieved_msgs.append(p1)
                selected_local_indices.add(pair_indices[0])
                selected_local_indices.add(pair_indices[1])
                retrieved_tokens += pair_tokens
            else:
                print(
                    f"   - atomic_skip user_id={msg_id} + assistant_id={p1.get('msg_id')} "
                    f"(pair_tokens={pair_tokens}, remain={retrieval_budget - retrieved_tokens})"
                )
        else:
            pi = pair_indices[0]
            if pi not in selected_local_indices:
                candidate = msg_pool[pi]
                tks = estimate_tokens(candidate["content"])
                if retrieved_tokens + tks <= retrieval_budget:
                    retrieved_msgs.append(candidate)
                    selected_local_indices.add(pi)
                    retrieved_tokens += tks
                else:
                    print(
                        f"   - single_skip id={candidate.get('msg_id')} role={candidate['role']} "
                        f"(msg_tokens={tks}, remain={retrieval_budget - retrieved_tokens})"
                    )

        if len(retrieved_msgs) >= MAX_RETRIEVED_MESSAGES * 2:
            break

        if msg["role"] == "user":
            if len(pair_indices) == 2 and pair_indices[1] in selected_local_indices:
                print(f"   - paired user_id={msg_id} -> assistant_id={msg_pool[pair_indices[1]].get('msg_id')}")
            else:
                print(f"   - paired user_id={msg_id} -> assistant_id=None（未找到或超预算）")

    remaining_pool = [m for i, m in enumerate(msg_pool) if i not in selected_local_indices]

    if retrieved_msgs:
        selected_rows = []
        for i, msg in enumerate(retrieved_msgs, start=1):
            preview = msg["content"].replace("\n", " ")[:35]
            selected_rows.append([f"#{i}", str(msg.get("msg_id", "-")), msg["role"], preview])
        print_simple_table(
            f"🔎 Context+: 混合召回命中 ({len(retrieved_msgs)} 条, 预算 {retrieval_budget} tokens) [trace={trace_id}]",
            ["序号", "消息ID", "角色", "内容预览"],
            selected_rows,
            col_widths=[6, 10, 6, 28],
        )
    else:
        print_kv_table(
            f"🔎 Context+: 混合召回命中 [trace={trace_id}]",
            {"状态": "未命中", "命中数": "0 条"},
            key_width=16, val_width=44,
        )

    print_kv_table(
        f"📌 Context+: Retrieved 出池 [trace={trace_id}]",
        {
            "选中消息数": f"{len(retrieved_msgs)} 条",
            "剩余消息数": f"{len(remaining_pool)} 条",
        },
        key_width=16, val_width=44,
    )

    return retrieved_msgs, remaining_pool


async def _extract_summary_context(
    msg_pool: list[dict],
    session_id: int,
    query_text: str,
    summary_budget: int,
    overflowed: bool,
    trace_id: str = "",
) -> dict | None:
    """
    对剩余 msg_pool 中 role=user 的内容构建摘要输入。
    overflowed 为 True 时触发 LLM 摘要，否则返回 None。
    """
    remain_indices = sorted([int(m["_seq_idx"]) for m in msg_pool if m.get("_seq_idx") is not None])
    print_kv_table(
        f"📌 Context+: Summary 输入池 [trace={trace_id}]",
        {
            "剩余消息数": f"{len(msg_pool)} 条",
            "剩余索引": str(remain_indices[:10]) + ("..." if len(remain_indices) > 10 else ""),
        },
        key_width=16, val_width=44,
    )

    summary_points = []
    summary_source_ids = []
    for msg in msg_pool:
        if msg["role"] != "user":
            continue
        point = msg["content"].replace("\n", " ").strip()
        if point:
            summary_points.append(point[:60])
            if msg.get("msg_id") is not None:
                summary_source_ids.append(msg.get("msg_id"))

    if not overflowed:
        print_kv_table(
            f"🧾 Context+: Summary 判定 [trace={trace_id}]",
            {"是否触发": "否", "原因": "历史未超预算 (total_history_tokens <= budget)"},
            key_width=16, val_width=44,
        )
        return None

    prev_summary = _get_persisted_summary(session_id)
    print_kv_table(
        f"🧾 Context+: Summary 判定 [trace={trace_id}]",
        {
            "是否触发": "是",
            "原因": "历史超预算 (total_history_tokens > budget)",
            "已有摘要": "是" if prev_summary else "否",
            "新要点数": str(len(summary_points)),
        },
        key_width=16, val_width=44,
    )

    if summary_points:
        pt_rows = []
        for i, p in enumerate(summary_points[:8], start=1):
            sid = summary_source_ids[i - 1] if i - 1 < len(summary_source_ids) else "-"
            pt_rows.append([f"#{i}", str(sid), p[:35]])
        print_simple_table(
            f"🧾 Context+: Summary 要点 [trace={trace_id}]",
            ["序号", "消息ID", "内容"],
            pt_rows,
            col_widths=[6, 10, 36],
        )

    raw_summary = ""
    try:
        raw_summary = await _generate_summary_with_llm(
            session_id=session_id,
            query_text=query_text,
            previous_summary=prev_summary,
            new_points=summary_points,
            token_budget=summary_budget,
        )
    except Exception as e:
        print(f"{_ctx_trace_prefix(trace_id)}⚠️ [Context+] LLM 摘要失败，降级规则摘要: {e}")
        raw_summary = _fallback_rule_summary(prev_summary, summary_points, summary_budget)

    if raw_summary:
        _set_persisted_summary(session_id, raw_summary)
        summary_text = "历史摘要：" + raw_summary
        print(f"{_ctx_trace_prefix(trace_id)}🧾 [Context+] 本轮摘要(~{estimate_tokens(summary_text)} tokens):\n{summary_text}")
        return {"role": "user", "content": summary_text}

    print(f"{_ctx_trace_prefix(trace_id)}🧾 [Context+] 本轮未生成有效摘要")
    return None


async def build_conversation_history_enhanced(
    db: Session,
    session_id: int,
    model_name: str,
    query_text: str,
    exclude_last_user_msg: bool = True,
) -> list[dict[str, str]]:
    """
    Pipeline 主流程：
    recent -> retrieved -> summary
    """
    if not session_id:
        return []

    try:
        messages = db.query(ChatMessageModel).filter(
            ChatMessageModel.session_id == session_id
        ).order_by(ChatMessageModel.created_at.asc()).all()

        if not messages:
            return []

        if exclude_last_user_msg and messages and messages[-1].role == "user":
            messages = messages[:-1]

        if not messages:
            return []

        formatted = []
        for idx, msg in enumerate(messages):
            content = (msg.content or "").strip()
            if not content:
                continue
            role = "assistant" if msg.role == "ai" else "user"
            formatted.append({
                "role": role,
                "content": content,
                "msg_id": msg.id,
                "_seq_idx": idx,
            })

        if not formatted:
            return []

        budget = FIXED_CONTEXT_BUDGET
        recent_budget = int(budget * RECENT_HISTORY_RATIO)
        retrieval_budget = int(budget * RETRIEVAL_HISTORY_RATIO)
        summary_budget = max(SUMMARY_MIN_TOKENS, int(budget * SUMMARY_RATIO))
        summary_budget = min(SUMMARY_MAX_TOKENS, summary_budget)

        total_history_tokens = sum(estimate_tokens(m["content"]) for m in formatted)
        overflowed = total_history_tokens > budget
        trace_id = f"{session_id}-{int(time.time() * 1000) % 100000}"

        print_kv_table(
            f"📏 Context+: 总体预算判定 [trace={trace_id}]",
            {
                "历史总 tokens": str(total_history_tokens),
                "预算上限": str(budget),
                "是否溢出": "是" if overflowed else "否",
                "历史消息数": f"{len(formatted)} 条",
            },
            key_width=16, val_width=44,
        )
        print_kv_table(
            f"📏 Context+: 子预算分配 [trace={trace_id}]",
            {
                "Recent 预算": str(recent_budget),
                "Retrieval 预算": str(retrieval_budget),
                "Summary 预算": str(summary_budget),
            },
            key_width=16, val_width=44,
        )

        msg_pool = formatted
        recent_msgs, msg_pool = _extract_recent_context(msg_pool, recent_budget, trace_id=trace_id)
        retrieved_msgs, msg_pool = await _extract_retrieved_context(
            msg_pool, query_text, retrieval_budget, len(formatted), trace_id=trace_id
        )
        summary_msg = await _extract_summary_context(
            msg_pool, session_id, query_text, summary_budget, overflowed, trace_id=trace_id
        )

        assembled = []
        if summary_msg:
            assembled.append(summary_msg)
        assembled.extend(retrieved_msgs)
        assembled.extend(recent_msgs)

        deduped = []
        seen = set()
        for m in assembled:
            key = m.get("msg_id") if m.get("msg_id") is not None else (m["role"], m["content"])
            if key in seen:
                continue
            seen.add(key)
            deduped.append(m)

        final_msgs = []
        total_tokens = 0
        for msg in reversed(deduped):
            tks = estimate_tokens(msg["content"])
            if total_tokens + tks > budget:
                continue
            final_msgs.append(msg)
            total_tokens += tks
        final_msgs.reverse()

        while final_msgs and final_msgs[0]["role"] == "assistant":
            final_msgs.pop(0)

        turn_count = sum(1 for m in final_msgs if m["role"] == "user")
        print_kv_table(
            f"🧠 Context+: 增强上下文汇总 [trace={trace_id}]",
            {
                "最终消息数": f"{len(final_msgs)} 条",
                "实际消耗 tokens": str(total_tokens),
                "用户轮数": str(turn_count),
                "预算上限": str(budget),
                "模型": model_name,
                "Recent 来源": f"{len(recent_msgs)} 条",
                "Retrieved 来源": f"{len(retrieved_msgs)} 条",
                "Summary 来源": "是" if summary_msg else "否",
            },
            key_width=16, val_width=44,
        )

        return [{"role": m["role"], "content": m["content"]} for m in final_msgs]

    except Exception as e:
        print_kv_table(
            f"⚠️ Context+: 异常回退 [trace={trace_id}]",
            {"错误": str(e)[:50], "回退策略": "基础窗口"},
            key_width=16, val_width=44,
        )
        return build_conversation_history(
            db=db,
            session_id=session_id,
            model_name=model_name,
            exclude_last_user_msg=exclude_last_user_msg,
        )
