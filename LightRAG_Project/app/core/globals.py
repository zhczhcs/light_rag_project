# 全局变量存储
# 用于避免循环引用
from contextvars import ContextVar
from typing import Optional

rag_engine = None

# 【新增】上下文变量，用于在每次请求中传递选定的模型名称
# default="" 而非 "qwen-turbo"：空字符串为 falsy，engine.py 中 `if dynamic_model` 可正确区分
# - 对话阶段：chat.py 会显式 set 为具体模型名（三级路由）
# - 索引阶段：未设置，get() 返回 ""，走 LLM_INDEXING_MODEL 分支
model_context: ContextVar[str] = ContextVar("model_context", default="")

# 【性能监控】上下文变量，用于传递 MetricsCollector
# 在索引/问答流程中设置，engine.py 中读取并记录指标
metrics_context: ContextVar[Optional["MetricsCollector"]] = ContextVar("metrics_context", default=None)
