import os
import logging
import time
import numpy as np
from lightrag import LightRAG, QueryParam
from lightrag.utils import EmbeddingFunc
from openai import AsyncOpenAI
from app.core.globals import model_context, metrics_context  # ✅ 引入监控上下文

# 设置日志
logging.basicConfig(format="%(levelname)s:%(message)s", level=logging.INFO)

# 📊 全局计数器（用于跨线程统计，LightRAG使用线程池时ContextVar无法传递）
_global_stats = {
    "embedding_calls": 0,
    "embedding_time": 0.0,
    "llm_calls": 0,
    "llm_time": 0.0,
    "total_tokens": 0
}

def reset_global_stats():
    """重置全局统计数据"""
    global _global_stats
    _global_stats = {
        "embedding_calls": 0,
        "embedding_time": 0.0,
        "llm_calls": 0,
        "llm_time": 0.0,
        "total_tokens": 0
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
    
    # ✅ 2. 动态获取模型名称 (优先使用 ContextVar，其次环境变量，最后默认 qwen-max)
    dynamic_model = model_context.get()
    model_name = dynamic_model if dynamic_model else os.environ.get("LLM_MODEL", "qwen-max")

    # 📊 性能监控：记录开始时间
    start_time = time.time()
    
    client = AsyncOpenAI(api_key=api_key, base_url=base_url)
    
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    if history_messages:
        messages.extend(history_messages)
    messages.append({"role": "user", "content": prompt})

    stream = kwargs.get("stream", False)

    response = await client.chat.completions.create(
        model=model_name,
        messages=messages,
        temperature=kwargs.get("temperature", 0.1),
        top_p=kwargs.get("top_p", 1),
        n=kwargs.get("n", 1),
        stream=stream,
    )

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
        async def stream_generator():
            async for chunk in response:
                if chunk.choices and chunk.choices[0].delta.content:
                    yield chunk.choices[0].delta.content
        return stream_generator()
    else:
        return response.choices[0].message.content

# ==========================================
# 2. 阿里百炼 Embedding 适配函数 (升级到 v3 + 增强容错)
# ==========================================
async def bailian_embedding(texts: list[str]) -> np.ndarray:
    api_key = os.environ.get("ALI_API_KEY")
    base_url = os.environ.get("ALI_BASE_URL")
    
    # ✅ 强制从环境变量读取 Embedding 模型（无默认值）
    model_name = os.environ.get("EMBEDDING_MODEL")
    if not model_name:
        raise ValueError("❌ 环境变量 EMBEDDING_MODEL 未设置，请在 .env 中配置（如 text-embedding-v2 或 text-embedding-v4）")
    
    # 🔍 日志：显示实际使用的 Embedding 模型（仅在首次调用时打印，避免刷屏）
    if not hasattr(bailian_embedding, "_logged"):
        print(f"📊 [Embedding] 使用模型: {model_name}")
        bailian_embedding._logged = True

    # 📊 性能监控：记录开始时间
    start_time = time.time()
    
    client = AsyncOpenAI(api_key=api_key, base_url=base_url)
    
    # 强制串行处理，确保绝对稳定 (1:1 关系)
    # 虽然慢一点，但能百分百避免阿里云目前 v3 模型偶尔出现的 Auto-Chunking 或双倍返回问题
    results = []
    
    for text in texts:
        try:
            # 针对单个文本请求
            response = await client.embeddings.create(
                input=[text],  # 必须包裹在列表中
                model=model_name,
                dimensions=1536  # 显式指定维度
            )
            # 强制只取第一个向量，无论它返回多少个
            if response.data:
                results.append(response.data[0].embedding)
            else:
                # 理论上不应发生，兜底全零
                results.append(np.zeros(1536))
                
        except Exception as e:
            print(f"❌ [Embedding] 单条处理失败: {e}")
            # 兜底：生成一个全零向量防止程序崩溃，保持对齐
            results.append(np.zeros(1536))
    
    # 📊 性能监控：记录 Embedding 调用
    duration = time.time() - start_time
    
    # 尝试获取 ContextVar collector（对话阶段可用）
    collector = metrics_context.get()
    if collector:
        collector.add_embedding_call(duration, len(texts))
    else:
        # 如果 collector 为空（索引阶段，跨线程），记录到全局统计
        _global_stats["embedding_calls"] += len(texts)
        _global_stats["embedding_time"] += duration
        _global_stats["total_tokens"] += len(texts) * 100
            
    return np.array(results)

# ==========================================
# 3. 初始化 RAG 引擎
# ==========================================
WORKING_DIR = "./data"

def get_rag_engine():
    if not os.path.exists(WORKING_DIR):
        os.mkdir(WORKING_DIR)

    # 服务器配置
    server_ip = "YOUR_SERVER_IP"
    
    # 注入环境变量
    os.environ["QDRANT_URL"] = f"http://{server_ip}:6333"
    os.environ["QDRANT_API_KEY"] = "YOUR_PASSWORD_PLACEHOLDER"
    os.environ["VECTOR_STORAGE"] = "QdrantVectorDBStorage"

    print(f"🌍 [System] 已配置远程数据库: {os.environ['QDRANT_URL']}")

    rag = LightRAG(
        working_dir=WORKING_DIR,
        vector_storage="QdrantVectorDBStorage",
        vector_db_storage_cls_kwargs={
            "url": os.environ["QDRANT_URL"],
            "api_key": os.environ["QDRANT_API_KEY"],
            "collection_name": "lightrag_vdb",
            "prefer_grpc": False
        },
        llm_model_func=bailian_llm,
        embedding_func=EmbeddingFunc(
            embedding_dim=1536, 
            max_token_size=8192,
            func=bailian_embedding
        )
    )
    return rag