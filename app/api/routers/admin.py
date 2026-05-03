from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import func

from app.core.security import get_current_user
from app.database import get_db, UserModel, DocumentModel, ChatSessionModel, ChatMessageModel

router = APIRouter(prefix="/admin", tags=["管理后台"])


def require_admin(current_user: UserModel = Depends(get_current_user)) -> UserModel:
	if current_user.username != "zmq":
		raise HTTPException(status_code=403, detail="需要管理员权限")
	return current_user


@router.get("/users", summary="获取所有用户")
def get_all_users(
	_: UserModel = Depends(require_admin),
	db: Session = Depends(get_db)
):
	users = db.query(UserModel).order_by(UserModel.id.asc()).all()
	result = []

	for user in users:
		document_count = db.query(DocumentModel).filter(DocumentModel.user_id == user.id).count()
		session_count = db.query(ChatSessionModel).filter(ChatSessionModel.user_id == user.id).count()
		message_count = db.query(ChatMessageModel).join(ChatSessionModel).filter(
			ChatSessionModel.user_id == user.id
		).count()

		result.append(
			{
				"id": user.id,
				"username": user.username,
				"email": user.email,
				"role": user.role,
				"is_active": user.is_active,
				"created_at": user.created_at.strftime("%Y-%m-%d") if user.created_at else None,
				"document_count": document_count,
				"session_count": session_count,
				"message_count": message_count
			}
		)

	return result


@router.patch("/users/{user_id}/toggle-active", summary="启用/禁用用户")
def toggle_user_active(
	user_id: int,
	_: UserModel = Depends(require_admin),
	db: Session = Depends(get_db)
):
	user = db.query(UserModel).filter(UserModel.id == user_id).first()
	if not user:
		raise HTTPException(status_code=404, detail="用户不存在")

	if user.username == "zmq":
		raise HTTPException(status_code=400, detail="不能禁用管理员账号")

	user.is_active = not user.is_active
	db.commit()
	return {"is_active": user.is_active}


@router.get("/statistics", summary="系统统计数据")
def get_statistics(
	_: UserModel = Depends(require_admin),
	db: Session = Depends(get_db)
):
	total_users = db.query(UserModel).count()
	total_documents = db.query(DocumentModel).count()
	total_messages = db.query(ChatMessageModel).count()

	popular_queries = db.query(
		ChatMessageModel.content,
		func.count(ChatMessageModel.content).label("count")
	).filter(
		ChatMessageModel.role == "user"
	).group_by(
		ChatMessageModel.content
	).order_by(
		func.count(ChatMessageModel.content).desc()
	).limit(10).all()

	return {
		"total_users": total_users,
		"total_documents": total_documents,
		"total_qa_pairs": total_messages // 2,
		"popular_queries": [
			{"query": item[0], "count": item[1]} for item in popular_queries
		]
	}
