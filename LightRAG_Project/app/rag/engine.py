import os
import logging
import numpy as np
from lightrag import LightRAG, QueryParam
from lightrag.utils import EmbeddingFunc
from openai import AsyncOpenAI
from app.core.globals import model_context  # ✅ 1. 引入上下文变量

# 设置日志
logging.basicConfig(format="%(levelname)s:%(message)s", level=logging.INFO)

# ==========================================
# 1. 阿里百炼 LLM 适配函数 (支持动态模型)
# ==========================================
async def bailian_llm(prompt, system_prompt=None, history_messages=[], **kwargs) -> str:
    api_key = os.environ.get("ALI_API_KEY")
    base_url = os.environ.get("ALI_BASE_URL")
    
    # ✅ 2. 动态获取模型名称 (优先使用 ContextVar，其次环境变量，最后默认 qwen-max)
    dynamic_model = model_context.get()
    model_name = dynamic_model if dynamic_model else os.environ.get("LLM_MODEL", "qwen-max")

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
    # ✅ 3. 升级 Embedding 模型为 text-embedding-v3
    model_name = os.environ.get("EMBEDDING_MODEL", "text-embedding-v3")

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
                dimensions=1024  # 显式指定维度
            )
            # 强制只取第一个向量，无论它返回多少个
            if response.data:
                results.append(response.data[0].embedding)
            else:
                # 理论上不应发生，兜底全零
                results.append(np.zeros(1024))
                
        except Exception as e:
            print(f"❌ [Embedding] 单条处理失败: {e}")
            # 兜底：生成一个全零向量防止程序崩溃，保持对齐
            results.append(np.zeros(1024))
            
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
            embedding_dim=1024, # ✅ 对应 text-embedding-v3 的维度
            max_token_size=8192,
            func=bailian_embedding
        )
    )
    return rag