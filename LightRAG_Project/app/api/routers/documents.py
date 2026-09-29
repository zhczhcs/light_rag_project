from typing import List
from fastapi import APIRouter, UploadFile, File, HTTPException, Depends, BackgroundTasks
from sqlalchemy.orm import Session

from sqlalchemy import or_
from app.database import get_db, DocumentModel, UserModel, get_user_workspace
from app.schemas.models import DocResponse
from app.services.file_service import parse_file_content
from app.services.rag_service import process_doc_background
from app.services.cleanup_service import perform_delete_all_documents, perform_delete_document
from app.core import globals
from app.core.security import get_current_user

# ===== Celery 任务（可选，Redis 不可用时自动降级到 BackgroundTasks）=====
def _dispatch_task(background_tasks: BackgroundTasks, text_content: str, filename: str, doc_id: int, user_id: int, workspace: str) -> dict:
    """
    优先使用 Celery 任务队列；若 Redis 不可用则降级到 FastAPI BackgroundTasks。
    传入 doc_id 用于防重：旧队列任务执行时若文档已被删除，直接跳过。
    传入 workspace 用于连接正确的 RAG 引擎（部门隔离或用户隔离）。
    返回：包含调度方式和可选 task_id 的字典
    """
    try:
        from app.tasks.document_tasks import process_document_task
        result = process_document_task.apply_async(
            args=[text_content, filename, doc_id, user_id, workspace],
            queue="local"
        )
        print(f"✅ [Celery] 任务已入队: {filename}, task_id={result.id}, workspace={workspace}")
        return {"mode": "celery", "task_id": result.id}
    except Exception as e:
        print(f"⚠️ [Celery] 不可用（{e}），降级到 BackgroundTasks")
        background_tasks.add_task(process_doc_background, text_content, filename, user_id, workspace)
        return {"mode": "background", "task_id": None}

router = APIRouter()

@router.get("/documents", response_model=List[DocResponse], summary="获取文件列表")
def get_documents(
    db: Session = Depends(get_db),
    current_user: UserModel = Depends(get_current_user)
):
    if current_user.department_id:
        # 有部门：返回本部门文档 + 自己上传的无部门文档（向后兼容）
        docs = db.query(DocumentModel).filter(
            or_(
                DocumentModel.department_id == current_user.department_id,
                (DocumentModel.user_id == current_user.id) & (DocumentModel.department_id == None)
            )
        ).order_by(DocumentModel.upload_time.desc()).all()
    else:
        # 无部门：仅返回自己的文档
        docs = db.query(DocumentModel).filter(DocumentModel.user_id == current_user.id).order_by(DocumentModel.upload_time.desc()).all()
    return [
        DocResponse(
            id=d.id,
            filename=d.filename,
            upload_time=d.upload_time.strftime("%Y-%m-%d %H:%M"),
            file_size=d.file_size,
            status=d.status
        )
        for d in docs
    ]

# ── 允许上传的文件格式白名单 ──
_ALLOWED_EXTENSIONS = {".pdf", ".txt", ".md", ".docx"}

@router.post("/upload", summary="上传文件")
async def upload_document(
    background_tasks: BackgroundTasks, 
    file: UploadFile = File(...), 
    db: Session = Depends(get_db),
    current_user: UserModel = Depends(get_current_user)
):
    # ── 格式校验：不支持的扩展名直接拒绝 ──
    import os
    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in _ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"不支持的文件格式: {ext}。仅支持 {', '.join(sorted(_ALLOWED_EXTENSIONS))}"
        )

    file.file.seek(0, 2)
    size_mb = f"{file.file.tell() / 1024 / 1024:.2f} MB"
    file.file.seek(0)

    # 解析文件内容
    text_content = await parse_file_content(file)
    
    # 检查文件内容是否为空
    if not text_content.strip():
        print(f"⚠️ 文件内容为空: {file.filename}")
        raise HTTPException(status_code=400, detail=f"文件内容为空，无法索引: {file.filename}")

    # 确定当前用户的 workspace（部门隔离 or 用户隔离）
    workspace = get_user_workspace(current_user)
    dept_id = current_user.department_id

    # 强制要求用户必须归属部门才能上传
    if not current_user.department_id:
        raise HTTPException(
            status_code=403,
            detail="上传文件需要归属某个部门，请联系管理员将你加入部门。"
        )

    # 检查文档是否已存在（在同一 workspace 内）
    if current_user.department_id:
        existing_doc = db.query(DocumentModel).filter(
            DocumentModel.filename == file.filename,
            DocumentModel.department_id == current_user.department_id
        ).first()
    else:
        existing_doc = db.query(DocumentModel).filter(
            DocumentModel.filename == file.filename,
            DocumentModel.user_id == current_user.id
        ).first()

    if not existing_doc:
        # 创建新文档，状态直接设为 indexing（索引中）
        new_doc = DocumentModel(
            filename=file.filename,
            file_size=size_mb,
            status="indexing",
            user_id=current_user.id,
            department_id=dept_id
        )
        db.add(new_doc)
        db.commit()
    else:
        # 更新现有文档，状态设为 indexing
        existing_doc.status = "indexing"
        existing_doc.file_size = size_mb
        db.commit()

    # 获取 doc_id（用于防重：旧队列任务执行时若文档已删除会跳过）
    current_doc_id = new_doc.id if not existing_doc else existing_doc.id

    # 调度文档处理任务（优先 Celery，降级到 BackgroundTasks）
    dispatch = _dispatch_task(background_tasks, text_content, file.filename, current_doc_id, current_user.id, workspace)

    # 立即返回，不等待处理完成（返回 doc_id 供前端精确轮询）
    response = {"message": "上传已开始，后台处理中...", "status": "indexing", "filename": file.filename, "doc_id": current_doc_id}
    if dispatch["task_id"]:
        response["task_id"] = dispatch["task_id"]   # Celery task_id，可用于查询进度
    return response

@router.delete("/documents/all", summary="删除所有文档")
async def delete_all_documents(
    db: Session = Depends(get_db),
    current_user: UserModel = Depends(get_current_user)
):
    workspace = get_user_workspace(current_user)
    result = await perform_delete_all_documents(
        db,
        workspace=workspace,
        department_id=current_user.department_id,
        user_id=current_user.id
    )
    return result

@router.delete("/documents/{doc_id}", summary="删除文档")
async def delete_document(
    doc_id: int,
    db: Session = Depends(get_db),
    current_user: UserModel = Depends(get_current_user)
):
    # 按 workspace 权限检查：部门成员可删除本部门任意文档
    if current_user.department_id:
        doc = db.query(DocumentModel).filter(
            DocumentModel.id == doc_id,
            DocumentModel.department_id == current_user.department_id
        ).first()
    else:
        doc = db.query(DocumentModel).filter(
            DocumentModel.id == doc_id,
            DocumentModel.user_id == current_user.id
        ).first()

    if not doc:
        raise HTTPException(status_code=404, detail="文档不存在或无权删除")
    
    workspace = get_user_workspace(current_user)
    result = await perform_delete_document(doc_id, db, workspace=workspace, user_id=current_user.id)
    return result
