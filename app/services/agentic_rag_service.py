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
from openai import AsyncOpenAI
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
            self._client = AsyncOpenAI(api_key=api_key, base_url=base_url)
        return self._client

    async def resolve(
        self,
        query: str,
        conversation_history: list[dict] | None = None,
    ) -> dict:
        if not query or not query.strip():
            return {"resolved_query": query, "was_rewritten": False, "reason": "empty query"}

        if not conversation_history:
            return {"resolved_query": query, "was_rewritten": False, "reason": "no history"}

        recent_history = self._extract_recent_turns(conversation_history, max_turns=3)
        if not recent_history:
            return {"resolved_query": query, "was_rewritten": False, "reason": "no valid history"}

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

        prompt = f"""You are a query resolution expert. The user's latest query may contain pronouns (that, this, it, he, just now, before, etc.), omissions, or ambiguity.

[Criteria]
- If query contains "that" "this" "it" "he" "just now" "before" "the above" "what you said" etc. -> rewrite needed
- If query is a short response ("right" "correct" "then what" "how to solve" "why") and depends on context -> rewrite needed
- If query itself is complete and clear -> no rewrite needed

[Task]
Based on the conversation history below, determine if the user's latest query needs rewriting.
If needed, output the rewritten complete query (understandable without history).
If not needed, output the original text unchanged.

[Conversation History]
{history_text}

[User's Latest Query]
{query}

[Output Rules]
- If rewriting needed: output rewritten query text directly, no quotes, no explanation
- If no rewrite needed: output original text exactly as-is
- Absolutely do NOT output "no rewrite needed" "original is" etc.
"""
        return prompt

    def _parse_result(self, original_query: str, resolved: str) -> dict:
        cleaned = resolved.strip().strip('"').strip("'").strip()

        def _normalize(text: str) -> str:
            return re.sub(r"[\s，。！？、；：\"'']", "", text).lower()

        if _normalize(cleaned) == _normalize(original_query.strip()):
            return {
                "resolved_query": original_query,
                "was_rewritten": False,
                "reason": "no rewrite needed",
            }

        no_rewrite_signals = ["no rewrite needed", "no need to rewrite", "original text", "original query", "无需改写", "不需要改写", "原文如下"]
        if any(s in cleaned for s in no_rewrite_signals):
            return {
                "resolved_query": original_query,
                "was_rewritten": False,
                "reason": "LLM judged no rewrite needed",
            }

        return {
            "resolved_query": cleaned,
            "was_rewritten": True,
            "reason": "pronoun resolution/query rewrite",
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
            self._client = AsyncOpenAI(api_key=api_key, base_url=base_url)
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
            self._client = AsyncOpenAI(api_key=api_key, base_url=base_url)
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
            self._client = AsyncOpenAI(api_key=api_key, base_url=base_url)
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

class AgenticOrchestrator:
    """
    Agentic RAG 编排器。
    Phase 1: QueryResolver（查询消解）
    Phase 2: RetrievalGrader（检索评分）+ QueryRewriter（查询重写）
    """

    def __init__(self, max_retries: int = 1):
        self.resolver = QueryResolver()
        self.grader = RetrievalGrader()
        self.rewriter = QueryRewriter()
        self.max_retries = max_retries

    async def execute(
        self,
        user_query: str,
        conversation_history: list[dict] | None = None,
        engine=None,
        param=None,
        use_hyde: bool = False,
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
                print(f"[HyDE] 开始生成假设文档 (查询: '{resolved_query}')")
                hyde_start = time.time()

                # 1. 生成假设文档
                hyde_generator = HyDEGenerator()
                hyde_doc = await hyde_generator.generate(resolved_query)

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
                        # 格式化 HyDE chunks 为文本
                        hyde_texts = []
                        seen_contents = set()
                        for i, item in enumerate(hyde_results[:top_k], 1):
                            content = item.get("content", "") if isinstance(item, dict) else str(item)
                            # 简单去重
                            content_key = content[:100] if len(content) > 100 else content
                            if content_key in seen_contents:
                                continue
                            seen_contents.add(content_key)
                            hyde_texts.append(f"[HyDE Document {i}]\n{content}")

                        hyde_chunks_count = len(hyde_texts)
                        if hyde_texts:
                            hyde_context = "\n\n".join(hyde_texts)
                            # 4. 注入到 param.user_prompt 中
                            original_prompt = getattr(param, "user_prompt", "") or ""
                            hyde_injection = f"\n\n---\n[HyDE 补充上下文] 以下文档片段基于用户问题生成的假设回答检索得到，可能包含额外相关信息：\n\n{hyde_context}\n---"
                            param.user_prompt = original_prompt + hyde_injection
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
        final_query = resolved_query
        final_result = None
        retries = 0
        graded = False
        grade_passed = True

        if engine and param:
            for attempt in range(self.max_retries + 1):
                print(f"[Agentic] Retrieval attempt {attempt + 1}/{self.max_retries + 1}")

                # 调用 LightRAG 检索+生成
                result = await engine.aquery_llm(final_query, param=param)
                final_result = result

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
            "metadata": metadata,
        }
