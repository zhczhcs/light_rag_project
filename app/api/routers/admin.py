from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import func

from app.core.security import get_current_user
from app.database import get_db, UserModel, DocumentModel, ChatSessionModel, ChatMessageModel, DepartmentModel

router = APIRouter(prefix="/admin", tags=["管理后台"])


def require_admin(current_user: UserModel = Depends(get_current_user)) -> UserModel:
	if current_user.role != "admin":
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
				"department_id": user.department_id,
				"department_name": user.department.name if user.department else None,
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


# =========================================================
# 部门管理接口
# =========================================================
class DepartmentCreate(BaseModel):
	name: str
	description: str = ""

class DepartmentUpdate(BaseModel):
	name: Optional[str] = None
	description: Optional[str] = None

@router.get("/departments", summary="获取所有部门")
def get_departments(
	_: UserModel = Depends(require_admin),
	db: Session = Depends(get_db)
):
	depts = db.query(DepartmentModel).order_by(DepartmentModel.id.asc()).all()
	return [
		{
			"id": d.id,
			"name": d.name,
			"description": d.description,
			"member_count": len(d.users),
			"created_at": d.created_at.strftime("%Y-%m-%d") if d.created_at else None
		}
		for d in depts
	]

@router.post("/departments", summary="创建部门")
def create_department(
	data: DepartmentCreate,
	_: UserModel = Depends(require_admin),
	db: Session = Depends(get_db)
):
	existing = db.query(DepartmentModel).filter(DepartmentModel.name == data.name).first()
	if existing:
		raise HTTPException(status_code=400, detail="部门名称已存在")
	dept = DepartmentModel(name=data.name, description=data.description)
	db.add(dept)
	db.commit()
	db.refresh(dept)
	return {"id": dept.id, "name": dept.name, "description": dept.description}

@router.patch("/departments/{dept_id}", summary="修改部门信息")
def update_department(
	dept_id: int,
	data: DepartmentUpdate,
	_: UserModel = Depends(require_admin),
	db: Session = Depends(get_db)
):
	dept = db.query(DepartmentModel).filter(DepartmentModel.id == dept_id).first()
	if not dept:
		raise HTTPException(status_code=404, detail="部门不存在")
	if data.name is not None:
		dept.name = data.name
	if data.description is not None:
		dept.description = data.description
	db.commit()
	return {"id": dept.id, "name": dept.name, "description": dept.description}

@router.delete("/departments/{dept_id}", summary="删除部门")
def delete_department(
	dept_id: int,
	_: UserModel = Depends(require_admin),
	db: Session = Depends(get_db)
):
	dept = db.query(DepartmentModel).filter(DepartmentModel.id == dept_id).first()
	if not dept:
		raise HTTPException(status_code=404, detail="部门不存在")
	# 将该部门的用户 department_id 清空（解除关联）
	db.query(UserModel).filter(UserModel.department_id == dept_id).update({"department_id": None})
	db.delete(dept)
	db.commit()
	return {"message": "部门已删除"}

@router.patch("/users/{user_id}/department", summary="设置用户所属部门")
def set_user_department(
	user_id: int,
	dept_id: Optional[int] = None,
	_: UserModel = Depends(require_admin),
	db: Session = Depends(get_db)
):
	"""
	将用户分配到指定部门，或传 dept_id=0/null 移出部门。
	注意：更改部门后，用户需重新上传文档以使索引生效于新部门的 workspace。
	"""
	user = db.query(UserModel).filter(UserModel.id == user_id).first()
	if not user:
		raise HTTPException(status_code=404, detail="用户不存在")
	if dept_id:
		dept = db.query(DepartmentModel).filter(DepartmentModel.id == dept_id).first()
		if not dept:
			raise HTTPException(status_code=404, detail="部门不存在")
		user.department_id = dept_id
	else:
		user.department_id = None
	db.commit()
	dept_name = user.department.name if user.department else None
	return {"user_id": user_id, "department_id": user.department_id, "department_name": dept_name}
