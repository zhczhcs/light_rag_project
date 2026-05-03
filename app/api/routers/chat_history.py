from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List
from pydantic import BaseModel

from app.database import get_db, ChatSessionModel, ChatMessageModel, UserModel
from app.core.security import get_current_user

router = APIRouter(prefix="/chat-history", tags=["对话历史"])

class SessionResponse(BaseModel):
    id: int
    title: str
    created_at: str
    message_count: int

@router.get("/sessions", response_model=List[SessionResponse], summary="获取对话列表")
def get_sessions(
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    sessions = db.query(ChatSessionModel).filter(
        ChatSessionModel.user_id == current_user.id
    ).order_by(ChatSessionModel.updated_at.desc()).all()
    
    return [
        SessionResponse(
            id=s.id,
            title=s.title,
            created_at=s.created_at.strftime("%Y-%m-%d %H:%M"),
            message_count=len(s.messages)
        ) for s in sessions
    ]

@router.post("/sessions/new", summary="创建新对话")
def create_session(
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    session = ChatSessionModel(user_id=current_user.id, title="新对话")
    db.add(session)
    db.commit()
    return {"session_id": session.id}

@router.get("/sessions/{session_id}/messages", summary="获取对话消息")
def get_messages(
    session_id: int,
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    session = db.query(ChatSessionModel).filter(
        ChatSessionModel.id == session_id,
        ChatSessionModel.user_id == current_user.id
    ).first()
    
    if not session:
        raise HTTPException(status_code=404, detail="对话不存在")
    
    import json
    
    messages = []
    for msg in session.messages:
        sources = []
        if msg.sources:
            try:
                sources = json.loads(msg.sources)
            except:
                pass
                
        messages.append({
            "id": msg.id,
            "role": msg.role,
            "content": msg.content,
            "sources": sources,
            "is_favorited": msg.is_favorited,
            "model": msg.model_name,
            "tokens": msg.tokens,
            "created_at": msg.created_at.strftime("%H:%M:%S"),
            "time": msg.created_at.strftime("%H:%M:%S"),
        })
        
    return messages

@router.post("/messages/{message_id}/favorite", summary="收藏/取消收藏")
def toggle_favorite(
    message_id: int,
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    message = db.query(ChatMessageModel).join(ChatSessionModel).filter(
        ChatMessageModel.id == message_id,
        ChatSessionModel.user_id == current_user.id
    ).first()
    
    if not message:
        raise HTTPException(status_code=404, detail="消息不存在")
    
    message.is_favorited = not message.is_favorited
    db.commit()
    return {"is_favorited": message.is_favorited}

@router.get("/favorites", summary="获取所有收藏")
def get_favorites(
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    messages = db.query(ChatMessageModel).join(ChatSessionModel).filter(
        ChatSessionModel.user_id == current_user.id,
        ChatMessageModel.is_favorited == True
    ).order_by(ChatMessageModel.created_at.desc()).all()
    
    import json
    
    result = []
    for msg in messages:
        sources = []
        if msg.sources:
            try:
                sources = json.loads(msg.sources)
            except:
                pass
                
        result.append({
            "id": msg.id,
            "content": msg.content,
            "sources": sources,
            "created_at": msg.created_at.strftime("%Y-%m-%d %H:%M")
        })
        
    return result
