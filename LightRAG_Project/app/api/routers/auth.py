from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.orm import Session
from pydantic import BaseModel, EmailStr

from app.database import get_db, UserModel, DepartmentModel
from app.core.security import (
    verify_password,
    get_password_hash,
    create_access_token,
    create_refresh_token,
    decode_token,
    get_current_user
)

router = APIRouter(prefix="/auth", tags=["认证"])

class UserRegister(BaseModel):
    username: str
    email: EmailStr
    password: str
    department_id: int  # 必填，注册时选择所属部门

class UserLogin(BaseModel):
    username: str
    password: str

class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str
    user: dict

class RefreshTokenRequest(BaseModel):
    refresh_token: str

@router.post("/register", summary="用户注册")
async def register(user_data: UserRegister, db: Session = Depends(get_db)):
    existing_user = db.query(UserModel).filter(UserModel.username == user_data.username).first()
    if existing_user:
        raise HTTPException(status_code=400, detail="用户名已存在")
    
    existing_email = db.query(UserModel).filter(UserModel.email == user_data.email).first()
    if existing_email:
        raise HTTPException(status_code=400, detail="邮箱已被注册")
    
    # 校验部门存在
    dept = db.query(DepartmentModel).filter(DepartmentModel.id == user_data.department_id).first()
    if not dept:
        raise HTTPException(status_code=400, detail="所选部门不存在")

    hashed_password = get_password_hash(user_data.password)
    new_user = UserModel(
        username=user_data.username,
        email=user_data.email,
        hashed_password=hashed_password,
        role="user",
        is_active=True,
        department_id=user_data.department_id
    )
    db.add(new_user)
    db.commit()
    db.refresh(new_user)
    
    return {"message": "注册成功", "user_id": new_user.id}

@router.post("/login", response_model=TokenResponse, summary="用户登录")
async def login(form_data: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    user = db.query(UserModel).filter(UserModel.username == form_data.username).first()
    
    if not user or not verify_password(form_data.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="用户名或密码错误",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    if not user.is_active:
        raise HTTPException(status_code=400, detail="用户已被禁用")
    
    access_token = create_access_token(data={"sub": user.username})
    refresh_token = create_refresh_token(data={"sub": user.username})
    
    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": "bearer",
        "user": {
            "id": user.id,
            "username": user.username,
            "email": user.email,
            "role": user.role,
            "department_id": user.department_id
        }
    }

@router.post("/refresh", response_model=TokenResponse, summary="刷新Token")
async def refresh_token(request: RefreshTokenRequest, db: Session = Depends(get_db)):
    payload = decode_token(request.refresh_token)
    
    if payload is None:
        raise HTTPException(status_code=401, detail="无效的刷新令牌")
    
    if payload.get("type") != "refresh":
        raise HTTPException(status_code=401, detail="令牌类型错误")
    
    username = payload.get("sub")
    if not username:
        raise HTTPException(status_code=401, detail="令牌无效")
    
    user = db.query(UserModel).filter(UserModel.username == username).first()
    if not user:
        raise HTTPException(status_code=401, detail="用户不存在")
    
    if not user.is_active:
        raise HTTPException(status_code=400, detail="用户已被禁用")
    
    access_token = create_access_token(data={"sub": user.username})
    new_refresh_token = create_refresh_token(data={"sub": user.username})
    
    return {
        "access_token": access_token,
        "refresh_token": new_refresh_token,
        "token_type": "bearer",
        "user": {
            "id": user.id,
            "username": user.username,
            "email": user.email,
            "role": user.role
        }
    }

@router.get("/me", summary="获取当前用户信息")
async def get_current_user_info(current_user: UserModel = Depends(get_current_user)):
    return {
        "id": current_user.id,
        "username": current_user.username,
        "email": current_user.email,
        "role": current_user.role,
        "is_active": current_user.is_active,
        "department_id": current_user.department_id,
        "department_name": current_user.department.name if current_user.department else None,
        "created_at": current_user.created_at
    }

@router.get("/debug/users", summary="调试：查看所有用户")
async def debug_get_all_users(db: Session = Depends(get_db)):
    users = db.query(UserModel).all()
    return {
        "total": len(users),
        "users": [
            {
                "id": u.id,
                "username": u.username,
                "email": u.email,
                "role": u.role,
                "is_active": u.is_active,
                "department_id": u.department_id,
                "created_at": u.created_at
            }
            for u in users
        ]
    }

@router.get("/debug/test-security", summary="测试：验证安全模块功能")
async def test_security_functions():
    from app.core.security import (
        get_password_hash,
        verify_password,
        create_access_token,
        create_refresh_token,
        decode_token,
        SECRET_KEY,
        ALGORITHM,
        ACCESS_TOKEN_EXPIRE_MINUTES,
        REFRESH_TOKEN_EXPIRE_DAYS
    )
    
    # 使用随机生成的测试密码，避免硬编码
    import secrets
    test_password = secrets.token_urlsafe(16)
    
    hashed = get_password_hash(test_password)
    is_valid = verify_password(test_password, hashed)
    
    test_username = "testuser"
    access_token = create_access_token(data={"sub": test_username})
    refresh_token = create_refresh_token(data={"sub": test_username})
    
    access_payload = decode_token(access_token)
    refresh_payload = decode_token(refresh_token)
    
    from datetime import datetime, timedelta
    
    return {
        "password_encryption": {
            "original_password": test_password,
            "hashed_password": hashed,
            "verification_result": is_valid,
            "status": "✅ 密码加密和验证正常" if is_valid else "❌ 密码验证失败"
        },
        "jwt_access_token": {
            "token": access_token[:50] + "...",
            "payload": access_payload,
            "expires_in_minutes": ACCESS_TOKEN_EXPIRE_MINUTES,
            "algorithm": ALGORITHM,
            "secret_key": SECRET_KEY[:10] + "...",
            "status": "✅ Access Token 生成正常" if access_payload else "❌ Access Token 生成失败"
        },
        "jwt_refresh_token": {
            "token": refresh_token[:50] + "...",
            "payload": refresh_payload,
            "expires_in_days": REFRESH_TOKEN_EXPIRE_DAYS,
            "algorithm": ALGORITHM,
            "status": "✅ Refresh Token 生成正常" if refresh_payload else "❌ Refresh Token 生成失败"
        },
        "token_validation": {
            "access_token_type": access_payload.get("type") if access_payload else None,
            "refresh_token_type": refresh_payload.get("type") if refresh_payload else None,
            "access_token_subject": access_payload.get("sub") if access_payload else None,
            "refresh_token_subject": refresh_payload.get("sub") if refresh_payload else None,
            "status": "✅ Token 结构正确" if (
                access_payload and 
                refresh_payload and 
                access_payload.get("type") == "access" and 
                refresh_payload.get("type") == "refresh"
            ) else "❌ Token 结构错误"
        }
    }

@router.get("/departments", summary="获取部门列表（注册用，无需登录）")
async def get_departments(db: Session = Depends(get_db)):
    """返回所有可用部门列表，供注册页面下拉选择"""
    departments = db.query(DepartmentModel).all()
    return [{"id": d.id, "name": d.name} for d in departments]
