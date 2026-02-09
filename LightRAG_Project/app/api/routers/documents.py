from typing import List
from fastapi import APIRouter, UploadFile, File, HTTPException, Depends, BackgroundTasks
from sqlalchemy.orm import Session

from app.database import get_db, DocumentModel
from app.schemas.models import DocResponse
from app.services.file_service import parse_file_content
from app.services.rag_service import process_doc_background
from app.services.cleanup_service import perform_delete_all_documents, perform_delete_document
from app.core import globals

router = APIRouter()

@router.get("/documents", response_model=List[DocResponse], summary="获取文件列表")
def get_documents(db: Session = Depends(get_db)):
    docs = db.query(DocumentModel).order_by(DocumentModel.upload_time.desc()).all()
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

@router.post("/upload", summary="上传文件")
async def upload_document(
    background_tasks: BackgroundTasks, 
    file: UploadFile = File(...), 
    db: Session = Depends(get_db)
):
    if not globals.rag_engine:
        raise HTTPException(status_code=500, detail="RAG 引擎未初始化")

    file.file.seek(0, 2)
    size_mb = f"{file.file.tell() / 1024 / 1024:.2f} MB"
    file.file.seek(0)

    # 解析文件内容
    text_content = await parse_file_content(file)
    
    # 检查文件内容是否为空
    if not text_content.strip():
        print(f"⚠️ 文件内容为空或格式不支持: {file.filename}")
        return {"message": "空文件或格式不支持"}

    # 检查文档是否已存在
    existing_doc = db.query(DocumentModel).filter(DocumentModel.filename == file.filename).first()
    if not existing_doc:
        # 创建新文档，状态直接设为 indexing（索引中）
        new_doc = DocumentModel(
            filename=file.filename,
            file_size=size_mb,
            status="indexing"
        )
        db.add(new_doc)
        db.commit()
    else:
        # 更新现有文档，状态设为 indexing
        existing_doc.status = "indexing"
        existing_doc.file_size = size_mb
        db.commit()

    # 添加后台任务处理文档
    background_tasks.add_task(process_doc_background, text_content, file.filename)

    # 立即返回，不等待处理完成
    return {"message": "上传已开始，后台处理中...", "status": "indexing", "filename": file.filename}

@router.delete("/documents/all", summary="删除所有文档")
async def delete_all_documents(db: Session = Depends(get_db)):
    return await perform_delete_all_documents(db)

@router.delete("/documents/{doc_id}", summary="删除文档")
async def delete_document(doc_id: int, db: Session = Depends(get_db)):
    result = await perform_delete_document(doc_id, db)
    if result is None:
        raise HTTPException(status_code=404, detail="文档不存在")
    return result
