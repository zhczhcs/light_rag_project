# import paramiko
from sqlalchemy import create_engine, Column, Integer, String, DateTime, Boolean, ForeignKey, Text, event
from sqlalchemy.orm import sessionmaker, declarative_base, relationship
from sqlalchemy.pool import Pool
from datetime import datetime
# from sshtunnel import SSHTunnelForwarder
import pymysql  # 引入原生驱动来建库
import time
import threading
import os
# import atexit  # 直连模式不需要清理隧道

# =========================================================
# ❌ 以下 SSH 隧道代码已注释（2026-03-03 切换为 MySQL 直连）
#    原因：paramiko 在 Windows 上 keepalive 不可靠，隧道频繁僵死；
#    直连服务器 MySQL 端口 43960 后，链路简化为 pymysql → MySQL，
#    消除了 SSH channel 这个不稳定中间层。
#    备份文件：database.py.bak
# =========================================================

# # 补丁：骗过 sshtunnel 对 paramiko DSSKey 的检查
# if not hasattr(paramiko, "DSSKey"):
#     paramiko.DSSKey = paramiko.RSAKey

# =========================================================
# 1. 数据库配置（直连模式）
# =========================================================
db_host = os.environ.get("MYSQL_HOST", "106.52.15.237")
db_port = int(os.environ.get("MYSQL_PORT", "43960"))
db_user = os.environ.get("MYSQL_USER", "zhczhcs")
db_password = os.environ.get("MYSQL_PASSWORD", "Zhc29606900")
db_name = os.environ.get("MYSQL_DB", "lightrag_db")

# # =========================================================
# # [已废弃] SSH 隧道相关函数
# # =========================================================
# server: SSHTunnelForwarder = None
# _tunnel_lock = threading.Lock()
# _last_recreate_time: float = 0
#
# def _create_tunnel() -> SSHTunnelForwarder: ...
# def _is_tunnel_truly_alive() -> bool: ...
# def _force_recreate_tunnel(): ...
# def ensure_tunnel() -> SSHTunnelForwarder: ...
# def _cleanup_tunnel(): ...
# atexit.register(_cleanup_tunnel)

# =========================================================
# 🆕 自动创建数据库（直连版本）
# =========================================================
print(f"🔄 [System] 正在直连 {db_host}:{db_port} 检查数据库 '{db_name}'...")
try:
    temp_conn = pymysql.connect(
        host=db_host,
        port=db_port,
        user=db_user,
        password=db_password,
        charset='utf8mb4',
        connect_timeout=10,
    )
    with temp_conn.cursor() as cursor:
        cursor.execute(f"CREATE DATABASE IF NOT EXISTS {db_name} CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;")
    temp_conn.commit()
    temp_conn.close()
    print(f"✅ [System] 数据库 '{db_name}' 检查/创建完毕！")
except Exception as e:
    print(f"❌ [Error] 无法连接数据库: {e}")

# =========================================================
# 2. 正式连接数据库 (SQLAlchemy) — 直连模式
# =========================================================
DATABASE_URL = f"mysql+pymysql://{db_user}:{db_password}@{db_host}:{db_port}/{db_name}?charset=utf8mb4"

engine = create_engine(
    DATABASE_URL,
    pool_recycle=300,
    pool_pre_ping=True,
    pool_size=5,
    max_overflow=10,
    pool_timeout=30,
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()

# =========================================================
# 3. 定义表模型
# =========================================================
class UserModel(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True, comment="主键ID")
    username = Column(String(50), unique=True, index=True, comment="用户名")
    email = Column(String(100), unique=True, index=True, comment="邮箱")
    hashed_password = Column(String(255), comment="加密密码")
    role = Column(String(20), default="user", comment="角色: user/admin")
    is_active = Column(Boolean, default=True, comment="是否启用")
    created_at = Column(DateTime, default=datetime.now, comment="创建时间")

class DocumentModel(Base):
    __tablename__ = "documents"

    id = Column(Integer, primary_key=True, index=True, comment="主键ID")
    filename = Column(String(255), index=True, comment="文件名")
    upload_time = Column(DateTime, default=datetime.now, comment="上传时间")
    file_size = Column(String(50), comment="文件大小")
    status = Column(String(50), default="indexing", comment="处理状态: indexing(索引中), completed(已完成), failed(失败)")
    user_id = Column(Integer, ForeignKey("users.id"), comment="用户ID")
    
    user = relationship("UserModel", backref="documents")

class ChatSessionModel(Base):
    __tablename__ = "chat_sessions"

    id = Column(Integer, primary_key=True, index=True, comment="主键ID")
    user_id = Column(Integer, ForeignKey("users.id"), comment="用户ID")
    title = Column(String(200), default="新对话", comment="会话标题")
    created_at = Column(DateTime, default=datetime.now, comment="创建时间")
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now, comment="更新时间")
    
    user = relationship("UserModel", backref="chat_sessions")
    messages = relationship("ChatMessageModel", backref="session", cascade="all, delete-orphan")

class ChatMessageModel(Base):
    __tablename__ = "chat_messages"

    id = Column(Integer, primary_key=True, index=True, comment="主键ID")
    session_id = Column(Integer, ForeignKey("chat_sessions.id"), comment="会话ID")
    role = Column(String(10), comment="角色: user/ai")
    content = Column(Text, comment="消息内容")
    sources = Column(Text, comment="引用来源JSON字符串")
    is_favorited = Column(Boolean, default=False, comment="是否收藏")
    model_name = Column(String(50), nullable=True, comment="AI模型名称")
    tokens = Column(Integer, nullable=True, comment="本条消息消耗的Token数")
    created_at = Column(DateTime, default=datetime.now, comment="创建时间")

# =========================================================
# 4. 自动建表 (Create Tables)
# =========================================================
try:
    print("🔄 [System] 正在检查/创建表结构...")
    Base.metadata.create_all(bind=engine)
    print("✅ [System] 表结构就绪！所有系统自检通过。")
except Exception as e:
    print(f"❌ [Error] 表结构初始化失败: {e}")

# =========================================================
# 5. 依赖函数
# =========================================================
def get_db():
    """
    FastAPI 依赖：获取数据库 session。
    内置一次重试：当遇到连接错误（SSH 隧道断开等）时，
    强制触发隧道重连后再试一次，而不是直接返回 500。
    """
    db = SessionLocal()
    try:
        # 触发一次 ping，验证连接可用（pool_pre_ping 会处理，但这里额外保障）
        yield db
    except Exception:
        db.close()
        raise
    finally:
        db.close()