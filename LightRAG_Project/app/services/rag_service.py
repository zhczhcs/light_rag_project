import time
from app.rag.engine import get_rag_engine, reset_global_stats, get_global_stats
from app.database import DocumentModel, SessionLocal
from app.core import globals
from app.utils.metrics import monitor

async def init_rag_engine():
    print("🔄 [System] 正在加载 LightRAG 引擎...")
    globals.rag_engine = get_rag_engine()
    try:
        await globals.rag_engine.initialize_storages()
        print("✅ [System] 存储层 (Storage) 初始化成功！")
    except Exception as e:
        print(f"❌ [System] 存储层初始化失败: {e}")
    print("✅ [System] 引擎加载完成！")

async def process_doc_background(text_content: str, filename: str):
    print(f"🔄 [Background] 开始处理文档: {filename}")
    
    # 📊 创建性能指标收集器
    session_id = f"indexing_{filename}_{time.time()}"
    collector = monitor.create_collector(session_id)
    
    # ⏱️ 记录总耗时
    total_start_time = time.time()
    
    # 🔄 重置全局统计（用于跨线程统计）
    reset_global_stats()
    
    db = SessionLocal()
    try:
        doc = db.query(DocumentModel).filter(DocumentModel.filename == filename).first()
        if not doc:
            print(f"⚠️ [Background] 数据库中未找到文档记录: {filename}")
            return
        doc.status = "indexing"
        db.commit()

        if globals.rag_engine:
            # 设置 metrics 上下文，让 engine.py 能够记录
            token = globals.metrics_context.set(collector)
            
            try:
                # 预估分片数量（粗略计算：500-1000字/片）
                estimated_chunks = max(1, len(text_content) // 750)
                collector.total_chunks = estimated_chunks
                collector.details['文档长度'] = f"{len(text_content):,} 字符"
                collector.details['预估分片'] = f"{estimated_chunks} 个"
                
                # 执行索引（LightRAG 内部会调用 embedding 和 llm 函数）
                await globals.rag_engine.ainsert(text_content, file_paths=[filename])
                
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
            doc = db.query(DocumentModel).filter(DocumentModel.filename == filename).first()
            if doc:
                doc.status = "failed"
                db.commit()
        except:
            pass
    finally:
        # 📊 记录总耗时并打印报告
        collector.total_time = time.time() - total_start_time
        
        # 📊 从全局统计中补充数据（索引阶段通过线程池调用，ContextVar无法传递）
        global_stats = get_global_stats()
        if global_stats["embedding_calls"] > 0 or global_stats["llm_calls"] > 0:
            collector.embedding_calls = global_stats["embedding_calls"]
            collector.embedding_time = global_stats["embedding_time"]
            collector.llm_calls = global_stats["llm_calls"]
            collector.llm_time = global_stats["llm_time"]
            collector.total_tokens_estimated = global_stats["total_tokens"]
        
        collector.print_indexing_report(filename)
        
        # 清理收集器
        monitor.remove_collector(session_id)
        
        db.close()
