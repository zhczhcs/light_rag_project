"""
Agentic RAG 编排服务

Phase 1: QueryResolver（查询消解）
Phase 2: RetrievalGrader（检索评分）+ QueryRewriter（查询重写）
HyDE: 假设文档生成 + 向量检索增强

完整闭环：理解 -> 检索 -> 验证 -> 纠正 -> 生成
"""

import os
import re
import time
import json
from openai import AsyncOpenAI
from app.core.llm_client import get_llm_client
from app.utils.table_printer import print_kv_table, print_simple_table


# ============================================================
# QueryResolver — 查询消解与改写（Phase 1）
# ============================================================

class QueryResolver:
    """
    结合对话历史，将包含指代/省略/歧义的查询改写为独立明确的查询。
    """

    def __init__(self, model: str = None):
        self.model = model or os.environ.get("AGENTIC_RESOLVER_MODEL", "qwen-turbo-latest")
        self._client = None

    def _get_client(self) -> AsyncOpenAI:
        if self._client is None:
            api_key = os.environ.get("ALI_API_KEY")
            base_url = os.environ.get("ALI_BASE_URL")
            if not api_key or not base_url:
                raise ValueError("ALI_API_KEY or ALI_BASE_URL not configured")
            self._client = get_llm_client(api_key, base_url)
        return self._client

    async def resolve(
        self,
        query: str,
        conversation_history: list[dict] | None = None,
    ) -> dict:
        if not query or not query.strip():
            return {"resolved_query": query, "was_rewritten": False, "reason": "empty query"}

        recent_history = self._extract_recent_turns(conversation_history, max_turns=3) if conversation_history else []

        prompt = self._build_prompt(query, recent_history)

        try:
            client = self._get_client()
            response = await client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0,
                max_tokens=200,
                stream=True,
                extra_body={"enable_thinking": False},
            )

            parts = []
            async for chunk in response:
                if chunk.choices and chunk.choices[0].delta and chunk.choices[0].delta.content:
                    parts.append(chunk.choices[0].delta.content)
            resolved = "".join(parts).strip()

            return self._parse_result(query, resolved)

        except Exception as e:
            print(f"[WARN] [Agentic] QueryResolver failed, fallback to original: {e}")
            return {"resolved_query": query, "was_rewritten": False, "reason": f"error: {e}"}

    def _extract_recent_turns(
        self, history: list[dict], max_turns: int = 3
    ) -> list[dict]:
        clean = [
            h for h in history
            if h.get("role") in ("user", "assistant") and h.get("content", "").strip()
        ]
        result = []
        turn_count = 0
        for msg in reversed(clean):
            result.insert(0, msg)
            if msg["role"] == "user":
                turn_count += 1
                if turn_count >= max_turns:
                    break
        while result and result[0]["role"] == "assistant":
            result.pop(0)
        return result

    def _build_prompt(self, query: str, history: list[dict]) -> str:
        history_text = "\n".join(
            f"{'User' if msg['role'] == 'user' else 'AI'}: {msg['content']}"
            for msg in history
        )

        prompt = f"""You are a query resolution expert. The user's latest query may contain pronouns (that, this, it, he, just now, before, etc.), omissions, or ambiguity. It may also be a complex query covering multiple aspects or a sequence of entities.

[Task 1: Query Resolution]
Based on the conversation history, determine if the query needs rewriting to resolve pronouns/omissions.
- If needed: output the rewritten standalone query.
- If not needed: keep the original text.

[Task 2: Query Decomposition]
Analyze if the query involves one of the following patterns:
- Sequence: "from A to B evolution/development", "history of X", "how X developed"
- Multi-aspect: "why X in both Y and Z", "X's role in A and B", "X is both P and Q"
- Comparison: "compare A and B", "differences between A and B"

If YES, decompose into 2-4 sub-queries following these CRITICAL rules:
1. Each sub-query MUST target a DIFFERENT specific entity, aspect, or time period (NO overlap)
2. For sequence questions, you MUST enumerate EVERY intermediate step (e.g., "from A to D" requires sub-queries for A, B, C, D - do NOT skip B or C)
3. Each sub-query MUST be independently searchable (contain complete context, no pronouns)
4. Sub-queries should NOT repeat the main query's overall question

If NO, leave sub-queries empty.

[Conversation History]
{history_text}

[User's Latest Query]
{query}

[Output Format] (strictly follow)
Resolved: <resolved query text>
SubQueries:
- <sub-query 1 if needed>
- <sub-query 2 if needed>
- <sub-query 3 if needed>
- <sub-query 4 if needed>

[Rules]
- If no sub-queries needed, write "SubQueries:" followed by nothing (or "None")
- Do NOT output explanations, notes, or quotes around the text
- Keep sub-queries concise (under 30 words each)
- CRITICAL: Ensure sub-queries are mutually exclusive and collectively exhaustive
"""
        return prompt

    def _parse_result(self, original_query: str, resolved: str) -> dict:
        text = resolved.strip()

        # Extract Resolved: block
        resolved_query = original_query
        sub_queries = []

        # Try to parse structured format (Resolved: ... SubQueries: ...)
        resolved_match = re.search(r"Resolved:\s*(.+?)(?=\nSubQueries:|$)", text, re.DOTALL | re.IGNORECASE)
        if resolved_match:
            resolved_query = resolved_match.group(1).strip()

            # Extract SubQueries list
            sub_match = re.search(r"SubQueries:\s*(.*)", text, re.DOTALL | re.IGNORECASE)
            if sub_match:
                sub_block = sub_match.group(1).strip()
                if sub_block and not sub_block.lower().startswith("none"):
                    # Parse bullet list
                    for line in sub_block.split("\n"):
                        line = line.strip()
                        if line.startswith("-") or line.startswith("*"):
                            sq = line[1:].strip()
                            if sq:
                                sub_queries.append(sq)
                        elif line and not line.lower().startswith("subqueries"):
                            # Handle cases where bullets are missing
                            sub_queries.append(line)

        def _normalize(text: str) -> str:
            return re.sub(r"[\s，。！？、；：\"'']", "", text).lower()

        # Fallback: if no structured format detected, treat entire response as resolved query
        if not resolved_match:
            cleaned = text.strip('"').strip("'").strip()
            no_rewrite_signals = ["no rewrite needed", "no need to rewrite", "original text", "original query", "无需改写", "不需要改写", "原文如下"]
            if any(s in cleaned for s in no_rewrite_signals):
                return {
                    "resolved_query": original_query,
                    "was_rewritten": False,
                    "reason": "LLM judged no rewrite needed",
                    "sub_queries": [],
                }
            resolved_query = cleaned

        was_rewritten = _normalize(resolved_query) != _normalize(original_query.strip())

        return {
            "resolved_query": resolved_query,
            "was_rewritten": was_rewritten,
            "reason": "pronoun resolution/query rewrite + decomposition" if sub_queries else ("pronoun resolution/query rewrite" if was_rewritten else "no rewrite needed"),
            "sub_queries": sub_queries,
        }


# ============================================================
# RetrievalGrader — 检索结果评分（Phase 2）
# ============================================================

class RetrievalGrader:
    """
    对检索到的 chunks 进行相关性批量评分。
    使用轻量模型，一次调用完成所有 chunks 的评分。
    """

    def __init__(self, model: str = None):
        self.model = model or os.environ.get("AGENTIC_GRADER_MODEL", "qwen3.5-35b-a3b")
        self._client = None

    def _get_client(self) -> AsyncOpenAI:
        if self._client is None:
            api_key = os.environ.get("ALI_API_KEY")
            base_url = os.environ.get("ALI_BASE_URL")
            if not api_key or not base_url:
                raise ValueError("ALI_API_KEY or ALI_BASE_URL not configured")
            self._client = get_llm_client(api_key, base_url)
        return self._client

    async def grade(self, query: str, chunks: list[dict]) -> dict:
        """
        批量评分 chunks 相关性。

        Args:
            query: 当前查询
            chunks: LightRAG 返回的 chunks，每个元素是 dict，包含 content

        Returns:
            dict: {
                "passed": bool,       # 是否通过（至少有一个直接相关）
                "reason": str,        # 原因说明
            }
        """
        if not chunks:
            return {"passed": False, "reason": "no chunks retrieved"}

        # 拼接 chunks 内容（限制总长度，避免 prompt 过长）
        chunk_texts = []
        total_len = 0
        max_total_len = 3000
        for i, chunk in enumerate(chunks[:6], 1):
            content = chunk.get("content", "")[:500]
            text = f"[Document {i}]\n{content}\n"
            if total_len + len(text) > max_total_len:
                break
            chunk_texts.append(text)
            total_len += len(text)

        chunks_block = "\n".join(chunk_texts)

        prompt = f"""You are a document relevance grader. Judge whether the retrieved document snippets can help answer the user's question.

[User Question]
{query}

[Retrieved Document Snippets]
{chunks_block}

[Task]
Determine if at least one snippet directly contains information needed to answer the question.
- YES: At least one snippet directly relevant, contains key info
- NO: All snippets are irrelevant, only marginally related, or repetitive

[Output Format] (strictly follow)
Result: YES or NO
Reason: one sentence explanation
"""

        try:
            client = self._get_client()
            response = await client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0,
                max_tokens=100,
                stream=True,
                extra_body={"enable_thinking": False},
            )

            parts = []
            async for chunk in response:
                if chunk.choices and chunk.choices[0].delta and chunk.choices[0].delta.content:
                    parts.append(chunk.choices[0].delta.content)
            raw = "".join(parts).strip()

            # 解析结果
            passed = "Result: YES" in raw or raw.upper().startswith("YES")
            reason = ""
            if "Reason:" in raw:
                reason = raw.split("Reason:", 1)[1].strip()
            else:
                reason = raw[:100]

            return {"passed": passed, "reason": reason}

        except Exception as e:
            print(f"[WARN] [Agentic] RetrievalGrader failed, default pass: {e}")
            return {"passed": True, "reason": f"grading failed, default pass: {e}"}


# ============================================================
# QueryRewriter — 查询重写（Phase 2）
# ============================================================

class QueryRewriter:
    """
    当检索结果质量不佳时，改写查询以提高检索质量。
    """

    def __init__(self, model: str = None):
        self.model = model or os.environ.get("AGENTIC_REWRITER_MODEL", "qwen3.6-plus-2026-04-02")
        self._client = None

    def _get_client(self) -> AsyncOpenAI:
        if self._client is None:
            api_key = os.environ.get("ALI_API_KEY")
            base_url = os.environ.get("ALI_BASE_URL")
            if not api_key or not base_url:
                raise ValueError("ALI_API_KEY or ALI_BASE_URL not configured")
            self._client = get_llm_client(api_key, base_url)
        return self._client

    async def rewrite(
        self,
        original_query: str,
        current_query: str,
        failure_reason: str,
    ) -> str:
        """
        根据失败原因改写查询。

        Returns:
            str: 改写后的查询
        """
        prompt = f"""你是搜索查询优化专家。之前的检索没有找到足够相关的文档。

【原始查询】
{original_query}

【上一次搜索查询】
{current_query}

【失败原因】
{failure_reason}

【任务】
分析为什么搜索失败，并给出一个改进的搜索查询：
- 使用更具体或更通用的关键词
- 尝试同义词或相关术语
- 去掉可能导致歧义的词
- 如果可能，将复杂问题拆分为更直接的子问题
- 保持使用中文（如果原始查询是中文）

只输出新的搜索查询文本，不要解释。
"""

        try:
            client = self._get_client()
            response = await client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3,
                max_tokens=200,
                stream=True,
                extra_body={"enable_thinking": False},
            )

            parts = []
            async for chunk in response:
                if chunk.choices and chunk.choices[0].delta and chunk.choices[0].delta.content:
                    parts.append(chunk.choices[0].delta.content)
            rewritten = "".join(parts).strip().strip('"').strip("'").strip()

            if not rewritten:
                return current_query
            return rewritten

        except Exception as e:
            print(f"[WARN] [Agentic] QueryRewriter failed, fallback: {e}")
            return current_query


# ============================================================
# HyDEGenerator — 假设文档生成器
# ============================================================

class HyDEGenerator:
    """
    HyDE (Hypothetical Document Embedding) 假设文档生成器。
    对泛化/口语化查询，生成一段假设的理想回答文档，
    用于增强向量检索的召回率。
    """

    def __init__(self, model: str = None):
        self.model = model or os.environ.get("AGENTIC_RESOLVER_MODEL", "qwen-turbo-latest")
        self._client = None

    def _get_client(self) -> AsyncOpenAI:
        if self._client is None:
            api_key = os.environ.get("ALI_API_KEY")
            base_url = os.environ.get("ALI_BASE_URL")
            if not api_key or not base_url:
                raise ValueError("ALI_API_KEY or ALI_BASE_URL not configured")
            self._client = get_llm_client(api_key, base_url)
        return self._client

    async def generate(self, query: str) -> str:
        """
        根据查询生成假设回答文档。

        Returns:
            str: 假设文档内容（中文，陈述性语气）
        """
        prompt = f"""你是一个知识库文档生成专家。请根据用户的问题，生成一段假设的知识库文档片段。

要求：
- 文档应该像是从学术论文或技术综述中提取的内容
- 使用中文，以陈述性语句写成
- 紧扣用户问题的主题，生成相关专业内容，不要跑题到无关领域
- 包含用户问题可能涉及的关键概念、原理、方法和结论
- 不要加"根据知识库""据我所知"等引用词，直接陈述事实
- 长度控制在 300-500 字

用户问题：
{query}

假设文档："""

        try:
            client = self._get_client()
            response = await client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3,
                max_tokens=800,
                stream=True,
                extra_body={"enable_thinking": False},
            )

            parts = []
            async for chunk in response:
                if chunk.choices and chunk.choices[0].delta and chunk.choices[0].delta.content:
                    parts.append(chunk.choices[0].delta.content)
            doc = "".join(parts).strip()

            if not doc:
                return ""

            # 清理常见的 LLM 废话前缀
            noise_prefixes = [
                "假设文档：", "假设文档:", "文档：", "文档:",
                "以下是", "以下是一段", "这段文档",
            ]
            for prefix in noise_prefixes:
                if doc.startswith(prefix):
                    doc = doc[len(prefix):].strip()

            return doc

        except Exception as e:
            print(f"[WARN] [HyDE] 生成假设文档失败: {e}")
            return ""


# ============================================================
# AgenticOrchestrator — 编排器（Phase 1+2+HyDE）
# ============================================================

class _ChunkNeighborExpander:
    """相邻 chunk 扩展器：根据 chunk_order_index 扩展前后邻居"""

    def __init__(self, workspace: str):
        self.workspace = workspace
        self._index = None
        self._loaded = False

    def _load_index(self):
        if self._loaded:
            return
        self._loaded = True
        if not self.workspace:
            return

        # 计算项目根目录（agentic_rag_service.py 位于 app/services/）
        base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        kv_path = os.path.join(base_dir, "data", self.workspace, "kv_store_text_chunks.json")
        if not os.path.exists(kv_path):
            return

        try:
            with open(kv_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self._index = {}
            for chunk_id, chunk in data.items():
                doc_id = chunk.get("full_doc_id")
                order_idx = chunk.get("chunk_order_index")
                if doc_id is not None and order_idx is not None:
                    self._index[(doc_id, order_idx)] = chunk
        except Exception as e:
            print(f"[WARN] [NeighborExpander] 加载 chunk 索引失败: {e}")

    def expand(self, chunks: list[dict], max_neighbors: int = 1) -> list[dict]:
        self._load_index()
        if not self._index or not chunks:
            return chunks

        existing_keys = set()
        for chunk in chunks:
            doc_id = chunk.get("full_doc_id")
            order_idx = chunk.get("chunk_order_index")
            if doc_id is not None and order_idx is not None:
                existing_keys.add((doc_id, order_idx))

        neighbor_chunks = []
        for chunk in chunks:
            doc_id = chunk.get("full_doc_id")
            order_idx = chunk.get("chunk_order_index")
            if doc_id is None or order_idx is None:
                continue
            for delta in range(-max_neighbors, max_neighbors + 1):
                if delta == 0:
                    continue
                neighbor_key = (doc_id, order_idx + delta)
                if neighbor_key in self._index and neighbor_key not in existing_keys:
                    neighbor = self._index[neighbor_key]
                    neighbor_chunks.append({
                        "content": neighbor.get("content", ""),
                        "full_doc_id": neighbor.get("full_doc_id", ""),
                        "file_path": neighbor.get("file_path", ""),
                        "chunk_order_index": neighbor.get("chunk_order_index", 0),
                    })
                    existing_keys.add(neighbor_key)

        if neighbor_chunks:
            print(f"[NeighborExpander] 扩展 {len(neighbor_chunks)} 个相邻 chunks")
            return chunks + neighbor_chunks
        return chunks


class AgenticOrchestrator:
    """
    Agentic RAG 编排器。
    Phase 1: QueryResolver（查询消解）
    Phase 2: RetrievalGrader（检索评分）+ QueryRewriter（查询重写）
    """

    def __init__(self, max_retries: int = None):
        self.resolver = QueryResolver()
        self.grader = RetrievalGrader()
        self.rewriter = QueryRewriter()
        self.max_retries = max_retries if max_retries is not None else int(
            os.environ.get("AGENTIC_MAX_RETRIES", "1")
        )

    async def execute(
        self,
        user_query: str,
        conversation_history: list[dict] | None = None,
        engine=None,
        param=None,
        use_hyde: bool = False,
        workspace: str = None,
    ) -> dict:
        """
        执行完整的 Agentic RAG 流程。

        Args:
            user_query: 用户原始查询
            conversation_history: 对话历史
            engine: LightRAG 引擎实例
            param: QueryParam 参数
            use_hyde: 是否启用 HyDE 假设文档增强

        Returns:
            dict: {
                "final_query": str,          # 最终用于检索的查询
                "original_query": str,       # 原始查询
                "result": dict,              # LightRAG aquery_llm 的结果
                "was_rewritten": bool,       # QueryResolver 是否改写
                "resolver_reason": str,      # QueryResolver 改写原因
                "retries": int,              # Phase 2 重试次数
                "graded": bool,              # 是否经过 Grader
                "grade_passed": bool,        # Grader 是否通过
                "use_hyde": bool,            # 是否使用了 HyDE
                "hyde_doc": str,             # HyDE 假设文档内容
                "hyde_chunks_count": int,    # HyDE 检索到的 chunk 数
                "metadata": dict,            # 完整调试信息
            }
        """
        metadata = {
            "phase": "2",
            "steps": [],
        }

        p1_start = time.time()

        # ========== Phase 1: Query Resolution ==========
        resolver_result = await self.resolver.resolve(user_query, conversation_history)
        metadata["steps"].append({
            "step": "query_resolution",
            "input": user_query,
            "output": resolver_result["resolved_query"],
            "was_rewritten": resolver_result["was_rewritten"],
            "reason": resolver_result["reason"],
        })

        resolved_query = resolver_result["resolved_query"]
        was_rewritten = resolver_result["was_rewritten"]
        sub_queries = resolver_result.get("sub_queries", [])

        # 查询分解：如有子查询，拼接到消解后查询以扩展语义覆盖
        final_query_for_retrieval = resolved_query
        if sub_queries:
            final_query_for_retrieval = resolved_query + " " + " ".join(sub_queries)
            metadata["steps"].append({
                "step": "query_decomposition",
                "sub_queries": sub_queries,
                "combined_query": final_query_for_retrieval,
            })
            print_kv_table(
                "🔀 QueryDecomposer",
                {
                    "消解后查询": resolved_query[:50] + ("..." if len(resolved_query) > 50 else ""),
                    "子查询数量": str(len(sub_queries)),
                    "拼接后查询": final_query_for_retrieval[:80] + ("..." if len(final_query_for_retrieval) > 80 else ""),
                },
                key_width=16, val_width=50,
            )

        # Phase 1 汇总表格
        print_kv_table(
            "🤖 Phase 1: QueryResolver",
            {
                "原始查询": user_query[:50] + ("..." if len(user_query) > 50 else ""),
                "消解后查询": resolved_query[:50] + ("..." if len(resolved_query) > 50 else ""),
                "是否改写": "是" if was_rewritten else "否",
                "改写原因": resolver_result.get("reason", "-"),
            },
            key_width=16, val_width=50,
        )

        # ========== HyDE: 假设文档生成 + 向量检索增强 ==========
        hyde_doc = ""
        hyde_chunks_count = 0
        if use_hyde and engine and param:
            try:
                print(f"[HyDE] 开始生成假设文档 (查询: '{final_query_for_retrieval}')")
                hyde_start = time.time()

                # 1. 生成假设文档
                hyde_generator = HyDEGenerator()
                hyde_doc = await hyde_generator.generate(final_query_for_retrieval)

                if hyde_doc:
                    # 清理不可见字符，防止 Embedding API 拒绝
                    hyde_doc_clean = ''.join(c for c in hyde_doc if c.isprintable() or c in '\n\t ')
                    if len(hyde_doc_clean) < len(hyde_doc):
                        print(f"[HyDE] 清理了 {len(hyde_doc) - len(hyde_doc_clean)} 个不可见字符")
                        hyde_doc = hyde_doc_clean

                    # HyDE 生成结果表格
                    print_kv_table(
                        "🧬 HyDE: 假设文档生成",
                        {
                            "文档长度": f"{len(hyde_doc)} 字",
                            "生成耗时": f"{time.time()-hyde_start:.2f}s",
                            "文档预览": hyde_doc[:80] + ("..." if len(hyde_doc) > 80 else ""),
                        },
                        key_width=16, val_width=50,
                    )

                    # 2. 用假设文档文本直接检索 Qdrant chunks
                    # chunks_vdb.query() 内部会自动调用 embedding_func 做向量化
                    retrieval_start = time.time()
                    top_k = getattr(param, "chunk_top_k", 6)
                    try:
                        hyde_results = await engine.chunks_vdb.query(hyde_doc, top_k=top_k)
                    except Exception as qe:
                        print(f"[WARN] [HyDE] Qdrant 检索异常: {qe}")
                        hyde_results = []

                    if hyde_results:
                        # HyDE chunks 存到 param.hyde_extra_chunks，在 mix 模式 round-robin 时合并进 vector_chunks 参与 rerank
                        param.hyde_extra_chunks = hyde_results[:top_k]
                        hyde_chunks_count = len(param.hyde_extra_chunks)
                    else:
                        hyde_results = []

                    # HyDE 检索结果表格
                    print_kv_table(
                        "🧬 HyDE: 向量检索结果",
                        {
                            "检索耗时": f"{time.time()-retrieval_start:.2f}s",
                            "检索到 chunks": f"{hyde_chunks_count} 个",
                            "user_prompt 注入": "已注入" if hyde_chunks_count > 0 else "未注入",
                        },
                        key_width=16, val_width=50,
                    )
                else:
                    print_kv_table(
                        "🧬 HyDE: 假设文档生成",
                        {"状态": "生成失败或为空，跳过 HyDE 检索"},
                        key_width=16, val_width=50,
                    )

            except Exception as e:
                print(f"[WARN] [HyDE] 执行异常，跳过: {e}")
                hyde_doc = ""
                hyde_chunks_count = 0

        # ========== Phase 2: Retrieve -> Grade -> Rewrite Loop ==========
        final_query = final_query_for_retrieval
        final_result = None
        retries = 0
        graded = False
        grade_passed = True

        if engine and param:
            # ===== 子查询独立检索 =====
            # 如果有子查询，对每个子查询独立检索，然后合并结果
            all_subquery_chunks = []
            if sub_queries:
                print(f"[Agentic] 子查询独立检索: {len(sub_queries)} 个子查询")
                for idx, sq in enumerate(sub_queries, 1):
                    print(f"[Agentic] 子查询 {idx}/{len(sub_queries)}: '{sq[:50]}...'")
                    try:
                        # 为子查询创建独立的 param 副本（避免 hyde_extra_chunks 污染）
                        from copy import deepcopy
                        sq_param = deepcopy(param)
                        sq_param.hyde_extra_chunks = None  # 子查询不启用 HyDE，避免噪声
                        sq_result = await engine.aquery_llm(sq, param=sq_param)
                        sq_chunks = sq_result.get("data", {}).get("chunks", [])
                        # 标记子查询来源
                        for ch in sq_chunks:
                            ch["_subquery_source"] = sq[:30]
                        all_subquery_chunks.extend(sq_chunks)
                        print(f"[Agentic] 子查询 {idx} 检索到 {len(sq_chunks)} 个 chunks")
                    except Exception as e:
                        print(f"[WARN] [Agentic] 子查询 {idx} 检索失败: {e}")

                if all_subquery_chunks:
                    # 去重：按 chunk_id 去重，保留第一个出现的
                    seen_ids = set()
                    unique_subquery_chunks = []
                    for ch in all_subquery_chunks:
                        cid = ch.get("chunk_id") or ch.get("id")
                        if cid and cid not in seen_ids:
                            seen_ids.add(cid)
                            unique_subquery_chunks.append(ch)
                        elif not cid:
                            unique_subquery_chunks.append(ch)
                    print(f"[Agentic] 子查询合并去重后: {len(unique_subquery_chunks)} 个 chunks (原始 {len(all_subquery_chunks)})")
                    metadata["steps"].append({
                        "step": "subquery_retrieval",
                        "subquery_count": len(sub_queries),
                        "total_chunks_before_dedup": len(all_subquery_chunks),
                        "total_chunks_after_dedup": len(unique_subquery_chunks),
                    })

            for attempt in range(self.max_retries + 1):
                print(f"[Agentic] Retrieval attempt {attempt + 1}/{self.max_retries + 1}")

                # 调用 LightRAG 检索+生成（主查询）
                result = await engine.aquery_llm(final_query, param=param)
                final_result = result

                # 如果有子查询结果，合并到主查询结果中
                if sub_queries and unique_subquery_chunks:
                    main_chunks = result.get("data", {}).get("chunks", [])
                    # 合并：子查询 chunks 放在前面（更相关），然后主查询 chunks
                    combined_chunks = unique_subquery_chunks[:]
                    main_seen = set(ch.get("chunk_id") or ch.get("id") for ch in unique_subquery_chunks)
                    for ch in main_chunks:
                        cid = ch.get("chunk_id") or ch.get("id")
                        if cid not in main_seen:
                            combined_chunks.append(ch)
                    # 更新 result 中的 chunks
                    if "data" in result:
                        result["data"]["chunks"] = combined_chunks
                    final_result = result
                    print(f"[Agentic] 主查询 {len(main_chunks)} + 子查询 {len(unique_subquery_chunks)} → 合并后 {len(combined_chunks)} 个 chunks")

                # 提取 chunks
                data = result.get("data", {})
                chunks = data.get("chunks", [])

                # 检索结果简要表格
                print_kv_table(
                    "📄 检索结果",
                    {
                        "检索查询": final_query[:45] + ("..." if len(final_query) > 45 else ""),
                        "检索到 chunks": f"{len(chunks)} 个",
                    },
                    key_width=16, val_width=50,
                )

                # 0 chunks -> 直接触发重写
                if not chunks:
                    if attempt < self.max_retries:
                        failure = "no chunks retrieved"
                        print(f"[Agentic] 0 chunks, triggering rewrite: {failure}")
                        final_query = await self.rewriter.rewrite(
                            original_query=user_query,
                            current_query=final_query,
                            failure_reason=failure,
                        )
                        retries += 1
                        metadata["steps"].append({
                            "step": "rewrite",
                            "trigger": "0_chunks",
                            "input": final_query,
                            "output": final_query,
                            "attempt": attempt + 1,
                        })
                        continue
                    else:
                        grade_passed = False
                        break

                # 有 chunks -> Grader 评分
                grade_result = await self.grader.grade(final_query, chunks)
                graded = True
                grade_passed = grade_result["passed"]

                metadata["steps"].append({
                    "step": "grade",
                    "query": final_query,
                    "chunk_count": len(chunks),
                    "passed": grade_result["passed"],
                    "reason": grade_result["reason"],
                    "attempt": attempt + 1,
                })

                # Grader 评分结果表格
                grader_data = {
                    "验证结果": "✅ 通过" if grade_result["passed"] else "❌ 未通过",
                    "判定理由": grade_result.get("reason", "-"),
                    "当前检索 chunks": f"{len(chunks)} 个",
                    "重试次数": f"{retries}/{self.max_retries}",
                }
                print_kv_table(
                    f"🔍 Grader 评分 (attempt {attempt+1})",
                    grader_data,
                    key_width=16, val_width=50,
                )

                if grade_result["passed"]:
                    break

                # ===== 相邻 chunk 扩展：Grader 不通过时，尝试扩展邻居重新评分 =====
                neighbor_expander = _ChunkNeighborExpander(workspace)
                expanded_chunks = neighbor_expander.expand(chunks, max_neighbors=1)
                if len(expanded_chunks) > len(chunks):
                    regrade_result = await self.grader.grade(final_query, expanded_chunks)
                    if regrade_result["passed"]:
                        grade_passed = True
                        # 更新最终 result 中的 chunks 为扩展后版本
                        if final_result and "data" in final_result:
                            final_result["data"]["chunks"] = expanded_chunks
                        metadata["steps"].append({
                            "step": "neighbor_expansion",
                            "original_chunks": len(chunks),
                            "expanded_chunks": len(expanded_chunks),
                            "regrade_passed": True,
                            "attempt": attempt + 1,
                        })
                        print_kv_table(
                            "🔗 Neighbor Chunk 扩展",
                            {
                                "验证结果": "✅ 通过",
                                "原始 chunks": f"{len(chunks)} 个",
                                "扩展后": f"{len(expanded_chunks)} 个",
                            },
                            key_width=16, val_width=50,
                        )
                        break
                    else:
                        metadata["steps"].append({
                            "step": "neighbor_expansion",
                            "original_chunks": len(chunks),
                            "expanded_chunks": len(expanded_chunks),
                            "regrade_passed": False,
                            "attempt": attempt + 1,
                        })
                        print_kv_table(
                            "🔗 Neighbor Chunk 扩展",
                            {
                                "验证结果": "❌ 未通过",
                                "原始 chunks": f"{len(chunks)} 个",
                                "扩展后": f"{len(expanded_chunks)} 个",
                            },
                            key_width=16, val_width=50,
                        )
                        # 扩展后仍不通过，继续走 Rewriter 流程

                # 评分不通过 -> 尝试重写（如果还有重试次数）
                if attempt < self.max_retries:
                    final_query = await self.rewriter.rewrite(
                        original_query=user_query,
                        current_query=final_query,
                        failure_reason=grade_result["reason"],
                    )
                    retries += 1
                    metadata["steps"].append({
                        "step": "rewrite",
                        "trigger": "grade_failed",
                        "input": final_query,
                        "output": final_query,
                        "attempt": attempt + 1,
                    })
                else:
                    break
        else:
            print_kv_table(
                "🔍 Phase 2: 检索验证",
                {"状态": "未提供 engine/param，跳过 Phase 2"},
                key_width=16, val_width=50,
            )

        # Agentic 执行汇总表格
        total_time = time.time() - p1_start
        print_kv_table(
            "🎯 Agentic RAG 执行汇总",
            {
                "原始查询": user_query[:45] + ("..." if len(user_query) > 45 else ""),
                "消解后查询": resolved_query[:45] + ("..." if len(resolved_query) > 45 else ""),
                "Phase 1 改写": "是" if was_rewritten else "否",
                "Phase 2 重试": f"{retries} 次",
                "Grader 验证": "通过" if grade_passed else "未通过",
                "HyDE 增强": f"是 ({hyde_chunks_count} chunks)" if use_hyde and hyde_chunks_count > 0 else ("是 (无结果)" if use_hyde else "否"),
                "总耗时": f"{total_time:.2f}s",
            },
            key_width=16, val_width=50,
        )

        return {
            "final_query": final_query,
            "original_query": user_query,
            "result": final_result,
            "was_rewritten": was_rewritten,
            "resolver_reason": resolver_result["reason"],
            "retries": retries,
            "graded": graded,
            "grade_passed": grade_passed,
            "use_hyde": use_hyde,
            "hyde_doc": hyde_doc,
            "hyde_chunks_count": hyde_chunks_count,
            "sub_queries": sub_queries,
            "metadata": metadata,
        }
