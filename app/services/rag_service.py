import time
import json
import os
from app.rag.engine import get_rag_engine, get_user_engine, get_workspace_engine, reset_global_stats, get_global_stats
from app.database import DocumentModel, SessionLocal
from app.core import globals
from app.utils.metrics import monitor


def _log_chunk_sizes(engine, filename: str):
    """索引完成后，读取 kv_store_text_chunks.json 打印所有 chunk 的 token 大小"""
    try:
        # LightRAG workspace: working_dir(./data)/user_X/kv_store_text_chunks.json
        base_dir = engine.working_dir
        workspace = getattr(engine, "workspace", None)
        if workspace:
            chunks_path = os.path.join(base_dir, workspace, "kv_store_text_chunks.json")
        else:
            chunks_path = os.path.join(base_dir, "kv_store_text_chunks.json")
        
        if not os.path.exists(chunks_path):
            print(f"⚠️ [ChunkLog] 未找到 {chunks_path}")
            return

        with open(chunks_path, "r", encoding="utf-8") as f:
            all_chunks = json.load(f)

        # 收集每个 chunk 的 token 数和内容长度
        token_sizes = []
        char_sizes = []
        for chunk_id, chunk_data in all_chunks.items():
            tokens = chunk_data.get("tokens", 0)
            content_len = len(chunk_data.get("content", ""))
            token_sizes.append(tokens)
            char_sizes.append(content_len)

        if not token_sizes:
            print(f"📊 [ChunkLog] {filename}: 无 chunk 数据")
            return

        avg_tokens = sum(token_sizes) / len(token_sizes)
        max_tokens = max(token_sizes)
        min_tokens = min(token_sizes)
        avg_chars = sum(char_sizes) / len(char_sizes)

        print(f"📊 [ChunkLog] {filename} 索引完成 — 共 {len(token_sizes)} 个 chunk")
        print(f"   Token 统计: avg={avg_tokens:.0f}, min={min_tokens}, max={max_tokens}")
        print(f"   字符统计: avg={avg_chars:.0f} 字符/chunk")
        print(f"   各 chunk token 数: {token_sizes}")

    except Exception as e:
        print(f"⚠️ [ChunkLog] 读取 chunk 统计失败: {e}")


async def init_rag_engine():
    """启动时验证环境配置（引擎按用户懒加载，不再全局初始化）"""
    print("🔄 [System] 验证 RAG 环境配置...")
    # 保留旧的 get_rag_engine 做环境变量注入（QDRANT_URL 等）
    _temp = get_rag_engine()
    globals.rag_engine = _temp  # 兼容旧代码引用，实际查询用 get_user_engine
    try:
        await _temp.initialize_storages()
        print("✅ [System] 存储层连通性验证成功！")
    except Exception as e:
        print(f"⚠️ [System] 存储层验证失败 (Qdrant 可能未启动): {e}")
    print("✅ [System] 环境就绪，引擎将按用户懒加载")

async def process_doc_background(text_content: str, filename: str, user_id: int = None, workspace: str = None):
    print(f"🔄 [Background] 开始处理文档: {filename} (workspace={workspace})")
    
    # 📊 创建性能指标收集器
    session_id = f"indexing_{filename}_{time.time()}"
    collector = monitor.create_collector(session_id)
    
    # ⏱️ 记录总耗时
    total_start_time = time.time()
    
    # � 快照差分：记录索引前的全局统计基准值（避免 reset 污染并发任务）
    stats_before = get_global_stats()
    
    db = SessionLocal()
    try:
        # 同时过滤 user_id，防止同名文件时更新到错误用户的记录
        query = db.query(DocumentModel).filter(DocumentModel.filename == filename)
        if user_id is not None:
            query = query.filter(DocumentModel.user_id == user_id)
        doc = query.first()
        if not doc:
            print(f"⚠️ [Background] 数据库中未找到文档记录: {filename} (user_id={user_id})")
            return
        doc.status = "indexing"
        db.commit()

        # 获取 workspace 引擎（优先）或用户专属引擎，否则回退到全局引擎
        engine = None
        if workspace is not None:
            engine = await get_workspace_engine(workspace)
        elif user_id is not None:
            engine = await get_user_engine(user_id)
        elif globals.rag_engine:
            engine = globals.rag_engine

        if engine:
            # 设置 metrics 上下文，让 engine.py 能够记录
            token = globals.metrics_context.set(collector)
            
            try:
                # 预估分片数量（粗略计算：500-1000字/片）
                estimated_chunks = max(1, len(text_content) // 750)
                collector.total_chunks = estimated_chunks
                collector.details['文档长度'] = f"{len(text_content):,} 字符"
                collector.details['预估分片'] = f"{estimated_chunks} 个"
                
                # 执行索引（LightRAG 内部会调用 embedding 和 llm 函数）
                await engine.ainsert(text_content, file_paths=[filename])
                
                # 📊 索引后打印所有 chunk 的 token 大小
                _log_chunk_sizes(engine, filename)
                
                print(f"✅ [Background] 文档插入 LightRAG 成功: {filename}")
                doc.status = "completed"
                db.commit()
                
            finally:
                # 清理上下文
                globals.metrics_context.reset(token)
        else:
            doc.status = "failed"
            db.commit()
            
    except Exception as e:
        print(f"❌ [Background] 处理文档失败: {str(e)}")
        try:
            db.rollback()
            query = db.query(DocumentModel).filter(DocumentModel.filename == filename)
            if user_id is not None:
                query = query.filter(DocumentModel.user_id == user_id)
            doc = query.first()
            if doc:
                doc.status = "failed"
                db.commit()
        except:
            pass
    finally:
        # 📊 记录总耗时并打印报告
        collector.total_time = time.time() - total_start_time
        
        # 📊 快照差分：本次任务实际消耗 = 索引后全局值 - 索引前基准值
        # （串行场景完全准确；并发场景可能有轻微混入，但比 reset 方案更安全）
        stats_after = get_global_stats()
        delta_embedding_calls = stats_after["embedding_calls"] - stats_before["embedding_calls"]
        delta_llm_calls       = stats_after["llm_calls"]       - stats_before["llm_calls"]
        if delta_embedding_calls > 0 or delta_llm_calls > 0:
            collector.embedding_calls         = delta_embedding_calls
            collector.embedding_time          = stats_after["embedding_time"]  - stats_before["embedding_time"]
            collector.llm_calls               = delta_llm_calls
            collector.llm_time                = stats_after["llm_time"]        - stats_before["llm_time"]
            collector.total_tokens_estimated  = stats_after["total_tokens"]    - stats_before["total_tokens"]
        
        collector.print_indexing_report(filename)
        
        # 清理收集器
        monitor.remove_collector(session_id)
        
        db.close()
