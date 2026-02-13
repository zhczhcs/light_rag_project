# 全局变量存储
# 用于避免循环引用
from contextvars import ContextVar
from typing import Optional

rag_engine = None

# 【新增】上下文变量，用于在每次请求中传递选定的模型名称
# 默认使用 qwen-turbo
model_context: ContextVar[str] = ContextVar("model_context", default="qwen-turbo")

# 【性能监控】上下文变量，用于传递 MetricsCollector
# 在索引/问答流程中设置，engine.py 中读取并记录指标
metrics_context: ContextVar[Optional["MetricsCollector"]] = ContextVar("metrics_context", default=None)
