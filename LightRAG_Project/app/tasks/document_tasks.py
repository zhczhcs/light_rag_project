"""
文档处理 Celery 任务
将原来的 BackgroundTasks 异步后台任务改为 Celery 任务，支持：
- 持久化（Redis 存储），服务器重启不丢任务
- 自动重试（最多 3 次，间隔 60 秒）
- Flower 可视化监控
"""
import asyncio
from typing import Optional
from app.tasks.celery_app import celery_app

# Worker 进程内复用同一个事件循环（避免 RAG 引擎 Lock 绑定循环不一致的问题）
_worker_loop: asyncio.AbstractEventLoop | None = None


def _get_worker_loop() -> asyncio.AbstractEventLoop:
    """获取或创建 Worker 专用事件循环，整个 Worker 进程生命周期内只创建一次"""
    global _worker_loop
    if _worker_loop is None or _worker_loop.is_closed():
        _worker_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(_worker_loop)
    return _worker_loop


@celery_app.task(
    name="process_document",
    bind=True,
    max_retries=3,
    default_retry_delay=60,
    acks_late=True,
)
def process_document_task(self, text_content: str, filename: str, doc_id: int = -1, user_id: int = -1, workspace: Optional[str] = None):
    """
    文档处理主任务（同步入口，内部运行异步逻辑）

    参数：
        text_content: 已解析的文档文本内容
        filename:     原始文件名（用于数据库状态更新）
        doc_id:       数据库主键（防重：若文档已被删除则跳过）
        user_id:      上传用户ID（用于数据库过滤）
        workspace:    工作空间标识（dept_X 或 user_X，用于 RAG 引擎隔离）
    """
    try:
        print(f"📋 [Celery] 任务开始: {filename} (task_id={self.request.id}, doc_id={doc_id}, user_id={user_id}, workspace={workspace})")
        loop = _get_worker_loop()
        loop.run_until_complete(_run_processing(text_content, filename, doc_id, user_id, workspace))
        print(f"✅ [Celery] 任务完成: {filename}")
    except Exception as exc:
        print(f"❌ [Celery] 任务失败: {filename}, 错误: {exc}")
        print(f"🔄 [Celery] 将在 60 秒后重试 (第 {self.request.retries + 1} 次)")
        raise self.retry(exc=exc, countdown=60)


async def _run_processing(text_content: str, filename: str, doc_id: int = -1, user_id: int = -1, workspace: Optional[str] = None):
    """
    异步处理逻辑（在 Celery worker 进程中执行）

    注意：Celery worker 是独立进程，globals.rag_engine 默认为 None，
    使用 get_workspace_engine 按 workspace 懒初始化引擎，实现数据隔离。
    """
    from app.rag.engine import get_workspace_engine, invalidate_workspace_engine, mark_engine_dirty_workspace
    from app.rag.engine import get_user_engine, invalidate_user_engine, mark_engine_dirty
    from app.services.rag_service import process_doc_background
    from app.database import SessionLocal, DocumentModel

    # 防重检查：如果文档已被删除（doc_id 不存在），直接跳过，不污染 LightRAG 索引
    if doc_id != -1:
        db_check = SessionLocal()
        try:
            doc = db_check.query(DocumentModel).filter(DocumentModel.id == doc_id).first()
            if not doc:
                print(f"⚠️ [Celery Worker] 文档已被删除，跳过处理: {filename} (id={doc_id})")
                return
        finally:
            db_check.close()

    # 优先使用 workspace 引擎（部门隔离），其次回退到用户引擎
    if workspace:
        # ⚠️ 关键修复：先 invalidate 再 get，强制从磁盘重建引擎
        invalidate_workspace_engine(workspace)
        await get_workspace_engine(workspace)
        print(f"✅ [Celery Worker] workspace '{workspace}' 的引擎已就绪")
    elif user_id != -1:
        invalidate_user_engine(user_id)
        await get_user_engine(user_id)
        print(f"✅ [Celery Worker] 用户 {user_id} 的专属引擎已就绪")
    else:
        # 兼容旧任务（无 user_id/workspace）：回退到全局引擎
        from app.core import globals
        from app.rag.engine import get_rag_engine
        if globals.rag_engine is None:
            print("🔄 [Celery Worker] 初始化全局 RAG 引擎（旧任务兼容）...")
            globals.rag_engine = get_rag_engine()
            await globals.rag_engine.initialize_storages()

    # 调用文档处理逻辑（传入 workspace 或 user_id 让它使用正确的引擎）
    await process_doc_background(
        text_content,
        filename,
        user_id if user_id != -1 else None,
        workspace,
        doc_id if doc_id != -1 else None,
    )

    # 🏴 索引完成后标记 dirty，通知 FastAPI 进程下次查询时重建引擎
    if workspace:
        mark_engine_dirty_workspace(workspace)
    elif user_id != -1:
        mark_engine_dirty(user_id)
