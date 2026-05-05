#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LightRAG Benchmark 执行脚本

用法:
    cd <项目根目录>
    python benchmark/run_benchmark.py

功能:
    1. 读取 benchmark/benchmark_config.json 中的测试题目
    2. 对每个题目调用 RAG 引擎进行查询（使用与生产环境相同的 QueryParam）
    3. 计算检索层指标（Recall@K, Precision@K, Hit Rate）
    4. 使用 LLM-as-a-Judge 评估生成层指标（Faithfulness, Answer Relevance, Completeness）
    5. 生成 Markdown 报告并保存
"""

import asyncio
import json
import os
import re
import sys
import time
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path

# 将项目根目录加入 Python 路径
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# 加载环境变量
from dotenv import load_dotenv

load_dotenv(PROJECT_ROOT / ".env")

# 导入项目模块
from app.rag.engine import get_user_engine, bailian_llm
from app.api.routers.chat import extract_keywords_via_llm, set_need_references_flag
from app.services.agentic_rag_service import AgenticOrchestrator
from app.core.globals import model_context
from lightrag import QueryParam

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
REPORT_DIR = PROJECT_ROOT / "benchmark" / "reports"
CHUNK_STORE_PATH = PROJECT_ROOT / "data" / "user_{user_id}" / "kv_store_text_chunks.json"
JUDGE_SYSTEM_PROMPT = (
    "你是一个严格的RAG系统评判专家。请根据评分标准给出0到1之间的分数。"
    "只输出数字（保留3位小数），不要输出任何解释文字。"
)
SIMILARITY_THRESHOLD = 0.55  # chunk内容匹配阈值


# ---------------------------------------------------------------------------
# Prompt 模板
# ---------------------------------------------------------------------------
def _faithfulness_prompt(query: str, answer: str, context: str) -> str:
    return (
        f"【问题】\n{query}\n\n"
        f"【检索上下文】\n{context[:3000]}\n\n"
        f"【生成的回答】\n{answer}\n\n"
        "请评估上述回答的'忠实度'（Faithfulness）：\n"
        "- 忠实度衡量回答中的每个事实性陈述是否都能在检索上下文中找到依据\n"
        "- 如果回答包含了检索上下文中没有的信息（幻觉），忠实度降低\n"
        "- 如果回答严格基于检索上下文，没有编造信息，忠实度高\n"
        "\n请给出0到1之间的分数，只输出数字：\n"
        "1.000 = 完全忠实，所有陈述都有上下文依据\n"
        "0.500 = 部分忠实，有一些无依据的陈述\n"
        "0.000 = 完全不忠实，大量编造信息\n\n"
        "分数："
    )


def _relevance_prompt(query: str, answer: str) -> str:
    return (
        f"【问题】\n{query}\n\n"
        f"【生成的回答】\n{answer}\n\n"
        "请评估上述回答的'回答相关性'（Answer Relevance）：\n"
        "- 回答是否直接、完整地回应了问题\n"
        "- 回答是否紧扣问题主题，没有跑题或回避问题\n"
        "- 回答是否提供了问题要求的具体信息\n"
        "\n请给出0到1之间的分数，只输出数字：\n"
        "1.000 = 完全相关，直接回答了问题的所有方面\n"
        "0.500 = 部分相关，回答了部分内容但遗漏关键信息\n"
        "0.000 = 完全不相关，没有回答问题的核心内容\n\n"
        "分数："
    )


def _completeness_prompt(query: str, answer: str, reference: str) -> str:
    return (
        f"【问题】\n{query}\n\n"
        f"【参考答案（包含应覆盖的关键信息）】\n{reference}\n\n"
        f"【生成的回答】\n{answer}\n\n"
        "请评估上述回答的'完整性'（Completeness）：\n"
        "- 完整性衡量回答覆盖了多少参考答案中的关键信息点\n"
        "- 不苛求表述完全一致，但关键事实、数据、观点都应被覆盖\n"
        "\n请给出0到1之间的分数，只输出数字：\n"
        "1.000 = 非常完整，覆盖了所有关键信息点\n"
        "0.500 = 部分完整，覆盖了一些但遗漏不少\n"
        "0.000 = 非常不完整，几乎没有覆盖关键信息\n\n"
        "分数："
    )


# ---------------------------------------------------------------------------
# Benchmark Runner
# ---------------------------------------------------------------------------
class BenchmarkRunner:
    def __init__(self, config_path: str, user_id: int = 10):
        with open(config_path, "r", encoding="utf-8") as f:
            self.config = json.load(f)
        self.user_id = user_id
        self.engine = None
        self.chunk_store: dict[str, str] = {}  # chunk_id -> content
        self._load_chunk_store()
        self._ensure_report_dir()

    # ---- 初始化 -----------------------------------------------------------
    def _load_chunk_store(self) -> None:
        path = str(CHUNK_STORE_PATH).format(user_id=self.user_id)
        if not os.path.exists(path):
            print(f"⚠️ 警告: Chunk store 不存在: {path}")
            return
        with open(path, "r", encoding="utf-8") as f:
            store = json.load(f)
        for chunk_id, data in store.items():
            self.chunk_store[chunk_id] = data.get("content", "")
        print(f"✅ 已加载 {len(self.chunk_store)} 个 chunks")

    def _ensure_report_dir(self) -> None:
        REPORT_DIR.mkdir(parents=True, exist_ok=True)

    async def _get_engine(self):
        if self.engine is None:
            print("⏳ 初始化 RAG 引擎...")
            self.engine = await get_user_engine(self.user_id)
            print("✅ 引擎就绪")
        return self.engine

    # ---- 查询 --------------------------------------------------------------
    async def query_rag(self, query: str) -> dict:
        """执行完整的 RAG 查询，复用 chat.py 的全部优化流程。"""
        engine = await self._get_engine()

        # 1. Phase 1: 关键词提取 + 复杂度分类 + 模型路由
        extracted_keywords, use_bypass, complexity_level, use_hyde, need_references = await extract_keywords_via_llm(query)
        query_mode = "hybrid"  # Benchmark 强制走检索模式
        selected_model = self._level_to_model(complexity_level)

        model_token = model_context.set(selected_model)
        print(f"   🔑 Keywords={extracted_keywords}, Level={complexity_level}, Model={selected_model}, HyDE={use_hyde}")

        try:
            # 2. 设置参考文献过滤标志
            set_need_references_flag(need_references)

            # 3. 构建 QueryParam
            param = QueryParam(
                mode=query_mode,
                stream=False,
                chunk_top_k=int(os.environ.get("QUERY_CHUNK_TOP_K", "6")),
                top_k=int(os.environ.get("QUERY_TOP_K", "10")),
                max_entity_tokens=int(os.environ.get("QUERY_MAX_ENTITY_TOKENS", "2000")),
                max_relation_tokens=int(os.environ.get("QUERY_MAX_RELATION_TOKENS", "3000")),
                max_total_tokens=int(os.environ.get("QUERY_MAX_TOTAL_TOKENS", "15000")),
                conversation_history=[],
                user_prompt=(
                    "用中文回答。引用规则（必须严格遵守）：\n"
                    "1. 正文中使用 [n] 标注引用，n 必须对应 Document Chunks 的 reference_id。\n"
                    "2. 严禁从 Knowledge Graph Data（Entity/Relationship）编造引用编号。"
                    "Knowledge Graph 信息仅供辅助理解，不能作为引用来源。\n"
                    "3. References 列表的条目必须使用 Reference Document List 中的真实文档名。\n"
                    "4. 如果某个观点不是直接来自 Document Chunks 的内容，不要加 [n] 标注。"
                ),
            )

            # 4. Phase 2: Agentic RAG（QueryResolver + RetrievalGrader + QueryRewriter + HyDE）
            # 传入空对话历史，QueryResolver 不会改写，但 HyDE/Grader/Rewriter 仍会工作
            resolved_query = query
            agentic_result = None
            try:
                orchestrator = AgenticOrchestrator(max_retries=1)
                agentic_result = await orchestrator.execute(
                    user_query=query,
                    conversation_history=[],
                    engine=engine,
                    param=param,
                    use_hyde=use_hyde,
                )
                resolved_query = agentic_result.get("final_query", query)
                if agentic_result.get("was_rewritten"):
                    print(f"   🔄 QueryResolver改写: '{query[:40]}...' → '{resolved_query[:40]}...'")
                if agentic_result.get("use_hyde"):
                    print(f"   🧬 HyDE已启用, 补充chunks={agentic_result.get('hyde_chunks_count', 0)}")
                if agentic_result.get("graded"):
                    grade_status = "通过" if agentic_result.get("grade_passed") else "未通过"
                    print(f"   📊 Grader: {grade_status}")
            except Exception as e:
                print(f"   ⚠️ Agentic异常，使用原文: {e}")

            # 5. 使用 Agentic 结果或 fallback 到 aquery_llm
            if agentic_result and agentic_result.get("result"):
                result = agentic_result["result"]
                print(f"   🚀 使用Agentic结果")
            else:
                print(f"   🚀 aquery_llm查询: '{resolved_query[:50]}...'")
                result = await engine.aquery_llm(resolved_query, param=param)

            result["_benchmark_meta"] = {
                "was_bypass_classified": use_bypass,
                "complexity_level": complexity_level,
                "selected_model": selected_model,
                "hyde_enabled": use_hyde,
            }
            return result
        finally:
            model_context.reset(model_token)

    @staticmethod
    def _level_to_model(level: str) -> str:
        """模型路由，复用 chat.py 的 MODEL_ROUTING。"""
        from app.api.routers.chat import _level_to_model as chat_level_to_model
        return chat_level_to_model(level)

    # ---- 检索指标 ----------------------------------------------------------
    def _match_chunk(self, retrieved_content: str, expected_ids: list[str]) -> str | None:
        """通过内容相似度将检索结果匹配到 ground-truth chunk_id。"""
        best_id = None
        best_score = 0.0
        # 清理检索内容中的【来源文档：】标记
        clean_retrieved = retrieved_content
        if "【来源文档：" in clean_retrieved:
            clean_retrieved = "\n".join(
                line for line in clean_retrieved.split("\n") if "【来源文档：" not in line
            )

        for chunk_id in expected_ids:
            expected_content = self.chunk_store.get(chunk_id, "")
            if not expected_content:
                continue
            # 取前800字符进行快速相似度计算（足够覆盖绝大多数内容特征）
            score = SequenceMatcher(
                None,
                clean_retrieved[:800],
                expected_content[:800],
            ).ratio()
            if score > best_score and score >= SIMILARITY_THRESHOLD:
                best_score = score
                best_id = chunk_id
        return best_id

    def compute_retrieval_metrics(
        self, result: dict, expected_chunks: list[str], k: int = 10
    ) -> dict:
        """计算 Chunk Recall@K / Precision@K / Hit Rate。"""
        data = result.get("data", {})
        chunks = data.get("chunks", [])
        top_k = chunks[:k]

        matched_ids = set()
        for chunk in top_k:
            content = chunk.get("content", "")
            matched = self._match_chunk(content, expected_chunks)
            if matched:
                matched_ids.add(matched)

        recall = len(matched_ids) / len(expected_chunks) if expected_chunks else 0.0
        precision = len(matched_ids) / len(top_k) if top_k else 0.0
        hit_rate = 1.0 if matched_ids else 0.0

        return {
            "recall_at_k": round(recall, 3),
            "precision_at_k": round(precision, 3),
            "hit_rate": round(hit_rate, 3),
            "retrieved_count": len(top_k),
            "matched_count": len(matched_ids),
            "expected_count": len(expected_chunks),
            "matched_ids": list(matched_ids),
        }

    # ---- LLM-as-a-Judge ----------------------------------------------------
    @staticmethod
    def _parse_score(text: str) -> float:
        """从 LLM 输出中提取 0-1 分数。"""
        text = text.strip()
        # 优先匹配 1.000 / 0.xxx / .xxx 形式（要求前面有数字边界或行首）
        if m := re.search(r"(?<![\d.])\d?\.\d{1,3}(?![\d.])", text):
            score = float(m.group())
            return round(min(max(score, 0.0), 1.0), 3)
        # 其次匹配 0-100 整数并归一化
        if m := re.search(r"\b(\d{1,3})\b", text):
            score = int(m.group(1))
            if score > 1:
                score = score / 100.0
            return round(min(max(score, 0.0), 1.0), 3)
        return 0.0

    async def llm_judge(self, metric: str, query: str, answer: str, **kwargs) -> float:
        """调用 bailian_llm 进行单项评分（使用独立的评判模型）。"""
        prompt_builders = {
            "faithfulness": lambda: _faithfulness_prompt(query, answer, kwargs["context"]),
            "answer_relevance": lambda: _relevance_prompt(query, answer),
            "completeness": lambda: _completeness_prompt(query, answer, kwargs["reference"]),
        }
        builder = prompt_builders.get(metric)
        if not builder:
            raise ValueError(f"未知评判指标: {metric}")

        prompt = builder()
        judge_model = self.config.get("llm_judge_model", "qwen-turbo")
        # bailian_llm 通过 model_context 读取模型，需要临时切换
        prev_model = None
        try:
            prev_model = model_context.get()
        except LookupError:
            pass
        token = model_context.set(judge_model)
        try:
            raw = await bailian_llm(
                prompt=prompt,
                system_prompt=JUDGE_SYSTEM_PROMPT,
                temperature=0.1,
            )
        finally:
            model_context.reset(token)
        score = self._parse_score(raw)
        print(f"   🎯 {metric} = {score:.3f} (raw: {raw[:40].strip()})")
        return score

    # ---- 主流程 ------------------------------------------------------------
    async def run(self) -> dict:
        """执行完整 Benchmark。"""
        questions = self.config["questions"]
        weights = self.config["metrics_weights"]
        results: list[dict] = []

        print("\n" + "=" * 70)
        print("🚀 LightRAG Benchmark 开始")
        print(f"   题目数量: {len(questions)} (intra: {sum(1 for q in questions if q['type']=='intra')}, cross: {sum(1 for q in questions if q['type']=='cross')})")
        print("=" * 70)

        for idx, q in enumerate(questions, 1):
            qid = q["id"]
            qtype = q["type"]
            query_text = q["query"]
            expected = q["expected_chunks"]
            reference = q["reference_answer"]

            print(f"\n[{idx}/{len(questions)}] {qid} ({qtype})")
            print(f"   Q: {query_text[:60]}...")

            # 1) 查询 RAG
            t0 = time.time()
            rag_result = await self.query_rag(query_text)
            query_time = round(time.time() - t0, 2)

            # 2) 提取回答与上下文
            llm_resp = rag_result.get("llm_response", {})
            answer = llm_resp.get("content", "")
            if not answer:
                # fallback: 某些版本放在 data.response
                answer = rag_result.get("data", {}).get("response", "")

            data = rag_result.get("data", {})
            chunks = data.get("chunks", [])
            context = "\n\n---\n\n".join(
                c.get("content", "") for c in chunks[:6]
            )

            # 3) 检索指标
            retrieval = self.compute_retrieval_metrics(rag_result, expected, k=10)
            print(
                f"   📎 Retrieved={retrieval['retrieved_count']}, "
                f"Matched={retrieval['matched_count']}/{retrieval['expected_count']}, "
                f"Recall={retrieval['recall_at_k']}, Precision={retrieval['precision_at_k']}, "
                f"Hit={retrieval['hit_rate']}"
            )

            # 4) LLM 评判（生成层）
            faithfulness = await self.llm_judge(
                "faithfulness", query_text, answer, context=context
            )
            relevance = await self.llm_judge(
                "answer_relevance", query_text, answer
            )
            completeness = await self.llm_judge(
                "completeness", query_text, answer, reference=reference
            )

            # 5) 加权总分
            overall = (
                retrieval["recall_at_k"] * weights["chunk_recall_at_k"]
                + retrieval["precision_at_k"] * weights["chunk_precision_at_k"]
                + retrieval["hit_rate"] * weights["chunk_hit_rate"]
                + faithfulness * weights["faithfulness"]
                + relevance * weights["answer_relevance"]
                + completeness * weights["completeness"]
            )
            overall = round(overall, 3)

            print(f"   ⭐ Overall = {overall:.3f} | ⏱️ {query_time}s")

            results.append({
                "id": qid,
                "type": qtype,
                "query": query_text,
                "query_time": query_time,
                "retrieval": retrieval,
                "faithfulness": faithfulness,
                "answer_relevance": relevance,
                "completeness": completeness,
                "overall": overall,
                "answer_preview": answer[:300] + "..." if len(answer) > 300 else answer,
            })

        # 汇总
        summary = self._summarize(results)
        return {"results": results, "summary": summary}

    def _summarize(self, results: list[dict]) -> dict:
        intra = [r for r in results if r["type"] == "intra"]
        cross = [r for r in results if r["type"] == "cross"]

        def _avg(items: list[dict], key: str) -> float:
            return round(sum(r[key] for r in items) / len(items), 3) if items else 0.0

        def _retrieval_avg(items: list[dict], key: str) -> float:
            return round(
                sum(r["retrieval"][key] for r in items) / len(items), 3
            ) if items else 0.0

        return {
            "total_questions": len(results),
            "overall_avg": _avg(results, "overall"),
            "intra": {
                "count": len(intra),
                "avg_overall": _avg(intra, "overall"),
                "avg_recall_at_k": _retrieval_avg(intra, "recall_at_k"),
                "avg_precision_at_k": _retrieval_avg(intra, "precision_at_k"),
                "avg_hit_rate": _retrieval_avg(intra, "hit_rate"),
                "avg_faithfulness": _avg(intra, "faithfulness"),
                "avg_answer_relevance": _avg(intra, "answer_relevance"),
                "avg_completeness": _avg(intra, "completeness"),
            },
            "cross": {
                "count": len(cross),
                "avg_overall": _avg(cross, "overall"),
                "avg_recall_at_k": _retrieval_avg(cross, "recall_at_k"),
                "avg_precision_at_k": _retrieval_avg(cross, "precision_at_k"),
                "avg_hit_rate": _retrieval_avg(cross, "hit_rate"),
                "avg_faithfulness": _avg(cross, "faithfulness"),
                "avg_answer_relevance": _avg(cross, "answer_relevance"),
                "avg_completeness": _avg(cross, "completeness"),
            },
        }

    # ---- 报告生成 ----------------------------------------------------------
    def generate_report(self, data: dict) -> str:
        r = data["results"]
        s = data["summary"]
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        lines = [
            "# LightRAG Benchmark 测试报告",
            "",
            f"**生成时间**: {ts}",
            f"**用户ID**: {self.user_id}",
            f"**评判模型**: {self.config.get('llm_judge_model', 'qwen-turbo')}",
            "",
            "## 一、汇总统计",
            "",
            f"| 指标 | 全部 ({s['total_questions']}题) | Intra-document ({s['intra']['count']}题) | Cross-document ({s['cross']['count']}题) |",
            "|------|-------------------------------|----------------------------------------|-----------------------------------------|",
        ]

        metrics_rows = [
            ("平均总分", "overall", True),
            ("检索 Recall@10", "recall", False),
            ("检索 Precision@10", "precision", False),
            ("检索 Hit Rate", "hit_rate", False),
            ("忠实度 Faithfulness", "faithfulness", True),
            ("回答相关性 Answer Relevance", "answer_relevance", True),
            ("完整性 Completeness", "completeness", True),
        ]

        for label, key, is_gen in metrics_rows:
            if is_gen:
                v_all = s["overall_avg"] if key == "overall" else _avg_key(r, key)
                v_intra = s["intra"][f"avg_{key}"]
                v_cross = s["cross"][f"avg_{key}"]
            else:
                v_all = _retrieval_avg(r, key.replace("recall", "recall_at_k").replace("precision", "precision_at_k"))
                v_intra = s["intra"][f"avg_{key.replace('recall', 'recall_at_k').replace('precision', 'precision_at_k')}"]
                v_cross = s["cross"][f"avg_{key.replace('recall', 'recall_at_k').replace('precision', 'precision_at_k')}"]
            lines.append(f"| {label} | {v_all:.3f} | {v_intra:.3f} | {v_cross:.3f} |")

        lines.extend([
            "",
            "## 二、指标权重",
            "",
            "| 指标 | 权重 |",
            "|------|------|",
        ])
        for k, v in self.config["metrics_weights"].items():
            lines.append(f"| {k} | {v:.0%} |")

        lines.extend([
            "",
            "## 三、逐题详细结果",
            "",
            "| 题目 | 类型 | Recall@10 | Precision@10 | Hit Rate | Faithfulness | Relevance | Completeness | **总分** | 耗时 |",
            "|------|------|-----------|--------------|----------|--------------|-----------|--------------|---------|------|",
        ])
        for item in r:
            ret = item["retrieval"]
            lines.append(
                f"| {item['id']} | {item['type']} | {ret['recall_at_k']:.3f} | "
                f"{ret['precision_at_k']:.3f} | {ret['hit_rate']:.3f} | "
                f"{item['faithfulness']:.3f} | {item['answer_relevance']:.3f} | "
                f"{item['completeness']:.3f} | **{item['overall']:.3f}** | {item['query_time']}s |"
            )

        lines.extend([
            "",
            "## 四、回答预览",
            "",
        ])
        for item in r:
            lines.extend([
                f"### {item['id']} ({item['type']})",
                "",
                f"**问题**: {item['query']}",
                "",
                f"**总分**: {item['overall']:.3f} | **耗时**: {item['query_time']}s",
                "",
                f"**回答预览**:",
                "```",
                item["answer_preview"],
                "```",
                "",
                "---",
                "",
            ])

        return "\n".join(lines)


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------
def _avg_key(results: list[dict], key: str) -> float:
    return round(sum(r[key] for r in results) / len(results), 3) if results else 0.0


def _retrieval_avg(results: list[dict], key: str) -> float:
    return round(
        sum(r["retrieval"][key] for r in results) / len(results), 3
    ) if results else 0.0


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
async def main():
    config_path = PROJECT_ROOT / "benchmark" / "benchmark_config.json"
    runner = BenchmarkRunner(str(config_path), user_id=10)
    result = await runner.run()

    report = runner.generate_report(result)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_path = REPORT_DIR / f"report_{ts}.md"
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report)

    print("\n" + "=" * 70)
    print("📊 Benchmark 执行完毕")
    print(f"   报告路径: {report_path}")
    print(f"   平均总分: {result['summary']['overall_avg']:.3f}")
    print(f"   Intra 平均: {result['summary']['intra']['avg_overall']:.3f}")
    print(f"   Cross 平均: {result['summary']['cross']['avg_overall']:.3f}")
    print("=" * 70)


if __name__ == "__main__":
    asyncio.run(main())
