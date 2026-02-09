from app.rag.engine import get_rag_engine
from app.database import DocumentModel, SessionLocal
from app.core import globals

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
    db = SessionLocal()
    try:
        doc = db.query(DocumentModel).filter(DocumentModel.filename == filename).first()
        if not doc:
            print(f"⚠️ [Background] 数据库中未找到文档记录: {filename}")
            return
        doc.status = "indexing"
        db.commit()

        if globals.rag_engine:
            await globals.rag_engine.ainsert(text_content, file_paths=[filename])
            print(f"✅ [Background] 文档插入 LightRAG 成功: {filename}")
            doc.status = "completed"
            db.commit()
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
        db.close()
