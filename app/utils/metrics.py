"""
性能指标监控工具
用于记录和打印 RAG 系统各个环节的性能数据
"""
import time
from typing import Dict, Any, Optional
from contextlib import contextmanager
from dataclasses import dataclass, field
from collections import defaultdict
from app.utils.table_printer import print_kv_table, print_simple_table


@dataclass
class MetricsCollector:
    """性能指标收集器"""
    
    # API 调用统计
    embedding_calls: int = 0
    llm_calls: int = 0
    
    # 时间统计（秒）
    total_time: float = 0.0
    embedding_time: float = 0.0
    llm_time: float = 0.0
    retrieval_time: float = 0.0
    generation_time: float = 0.0
    
    # 数据统计
    total_chunks: int = 0
    retrieved_chunks: int = 0
    total_tokens_estimated: int = 0
    last_response_tokens: int = 0   # 最近一次 LLM 调用的实际 Token 数（来自 API usage 字段）
    
    # 详细记录
    details: Dict[str, Any] = field(default_factory=dict)
    
    def reset(self):
        """重置所有计数器"""
        self.embedding_calls = 0
        self.llm_calls = 0
        self.total_time = 0.0
        self.embedding_time = 0.0
        self.llm_time = 0.0
        self.retrieval_time = 0.0
        self.generation_time = 0.0
        self.total_chunks = 0
        self.retrieved_chunks = 0
        self.total_tokens_estimated = 0
        self.last_response_tokens = 0
        self.details = {}
    
    def add_embedding_call(self, duration: float, texts_count: int):
        """记录一次 Embedding 调用"""
        self.embedding_calls += 1
        self.embedding_time += duration
        self.total_tokens_estimated += texts_count * 100  # 粗略估算
    
    def add_llm_call(self, duration: float, estimated_tokens: int = 0):
        """记录一次 LLM 调用"""
        self.llm_calls += 1
        self.llm_time += duration
        self.total_tokens_estimated += estimated_tokens
    
    def print_indexing_report(self, filename: str):
        """打印索引阶段性能报告（表格版）"""
        print_kv_table(
            f"📊 索引性能报告 — {filename}",
            {
                "总耗时": f"{self.total_time:.2f} s",
                "分片数量": f"{self.total_chunks} 个",
                "Embedding 调用": f"{self.embedding_calls} 次 / {self.embedding_time:.2f}s",
                "LLM 调用 (实体提取)": f"{self.llm_calls} 次 / {self.llm_time:.2f}s",
                "预估 Token 消耗": f"~{self.total_tokens_estimated:,} tokens",
            },
            prefix_newline=True,
        )
        if self.total_chunks > 0 and self.embedding_calls > 0:
            print_simple_table(
                "📈 平均耗时明细",
                ["阶段", "每次耗时"],
                [
                    ["Embedding", f"{self.embedding_time / self.embedding_calls:.3f}s"],
                    ["LLM 提取", f"{self.llm_time / self.llm_calls:.3f}s"],
                ],
                col_widths=[18, 18],
                prefix_newline=True,
            )
        if self.details:
            detail_rows = [[str(k), str(v)] for k, v in self.details.items()]
            print_simple_table(
                "📝 详细信息", ["字段", "值"], detail_rows, col_widths=[22, 38],
                prefix_newline=True,
            )

    def print_query_report(self, query: str, model_name: str):
        """打印问答阶段性能报告（表格版）"""
        # 1. 基础信息
        print_kv_table(
            "💬 问答性能报告",
            {
                "问题": query[:60] + ("..." if len(query) > 60 else ""),
                "使用模型": model_name,
                "总耗时": f"{self.total_time:.2f} s",
                "检索阶段": f"{self.retrieval_time:.2f} s",
                "生成阶段": f"{self.generation_time:.2f} s",
            },
            key_width=18, val_width=52,
        )

        # 2. 检索统计
        retrieval_data = {"召回文档块": f"{self.retrieved_chunks} 个"}
        if self.details.get('retrieval_sources'):
            src = ", ".join(set(self.details['retrieval_sources']))
            retrieval_data["来源文件"] = src[:50] + ("..." if len(src) > 50 else "")
        if self.retrieved_chunks > 0:
            retrieval_data["平均相似度"] = str(self.details.get('avg_similarity', 'N/A'))
        print_kv_table("🔍 检索统计", retrieval_data, key_width=18, val_width=52)

        # 3. API 调用统计
        print_simple_table(
            "🔢 API 调用统计",
            ["类型", "次数", "耗时"],
            [
                ["Embedding", str(self.embedding_calls), f"{self.embedding_time:.2f}s"],
                ["LLM", str(self.llm_calls), f"{self.llm_time:.2f}s"],
                ["预估 Token", f"~{self.total_tokens_estimated:,}", "-"],
            ],
            col_widths=[18, 12, 12],
        )

        # 4. 流式指标
        if self.details.get('ttft'):
            print_kv_table(
                "⚡ 流式指标",
                {"TTFT (首字延迟)": f"{self.details['ttft']:.2f}s"},
                key_width=18, val_width=52,
            )


class PerformanceMonitor:
    """全局性能监控器（单例模式）"""
    
    _instance = None
    _collectors: Dict[str, MetricsCollector] = {}
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance
    
    def create_collector(self, session_id: str) -> MetricsCollector:
        """创建一个新的指标收集器"""
        collector = MetricsCollector()
        self._collectors[session_id] = collector
        return collector
    
    def get_collector(self, session_id: str) -> Optional[MetricsCollector]:
        """获取指定会话的收集器"""
        return self._collectors.get(session_id)
    
    def remove_collector(self, session_id: str):
        """移除收集器"""
        self._collectors.pop(session_id, None)
    
    @contextmanager
    def track_time(self, collector: MetricsCollector, metric_name: str):
        """
        上下文管理器：跟踪代码块执行时间
        
        使用示例:
            with monitor.track_time(collector, 'retrieval_time'):
                # 执行检索操作
                results = await engine.search(query)
        """
        start_time = time.time()
        try:
            yield
        finally:
            duration = time.time() - start_time
            if hasattr(collector, metric_name):
                setattr(collector, metric_name, getattr(collector, metric_name) + duration)
            collector.details[metric_name] = duration


# 全局监控器实例
monitor = PerformanceMonitor()


@contextmanager
def track_api_call(collector: MetricsCollector, api_type: str, **kwargs):
    """
    跟踪 API 调用（装饰器）
    
    参数:
        collector: 指标收集器
        api_type: 'embedding' 或 'llm'
        **kwargs: 额外参数（如 texts_count, estimated_tokens）
    
    使用示例:
        with track_api_call(collector, 'embedding', texts_count=10):
            result = await embedding_func(texts)
    """
    start_time = time.time()
    try:
        yield
    finally:
        duration = time.time() - start_time
        if api_type == 'embedding':
            texts_count = kwargs.get('texts_count', 1)
            collector.add_embedding_call(duration, texts_count)
        elif api_type == 'llm':
            estimated_tokens = kwargs.get('estimated_tokens', 500)
            collector.add_llm_call(duration, estimated_tokens)
        else:
            print(f"⚠️ 未知的 API 类型: {api_type}")
