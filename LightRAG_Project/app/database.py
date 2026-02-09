import paramiko
from sqlalchemy import create_engine, Column, Integer, String, DateTime
from sqlalchemy.orm import sessionmaker, declarative_base
from datetime import datetime
from sshtunnel import SSHTunnelForwarder
import pymysql  # 引入原生驱动来建库
import time

# =========================================================
# 🚑 补丁：骗过 sshtunnel 对 paramiko DSSKey 的检查
# =========================================================
if not hasattr(paramiko, "DSSKey"):
    paramiko.DSSKey = paramiko.RSAKey

# =========================================================
# 1. SSH 隧道配置
# =========================================================
ssh_host = 'YOUR_SERVER_IP'
ssh_port = 22222
ssh_user = 'zmq'
ssh_password = 'YOUR_PASSWORD_PLACEHOLDER'

# 数据库配置
db_user = 'YOUR_DB_USER'
db_password = 'YOUR_DB_PASSWORD_PLACEHOLDER'
db_name = 'YOUR_DB_NAME' # 目标数据库名

print(f"🔄 [System] 正在建立 SSH 隧道 ({ssh_host}:{ssh_port})...")

try:
    server = SSHTunnelForwarder(
        (ssh_host, ssh_port),
        ssh_username=ssh_user,
        ssh_password=ssh_password,
        remote_bind_address=('127.0.0.1', 3306),
        set_keepalive=10.0
    )
    server.start()
    print(f"✅ [System] SSH 隧道已打通！本地映射端口: {server.local_bind_port}")

except Exception as e:
    print(f"❌ [Error] SSH 连接失败: {str(e)}")
    raise e

# =========================================================
# 🆕【新增】自动创建数据库 (如果没有)
# =========================================================
print(f"🔄 [System] 正在检查数据库 '{db_name}' 是否存在...")
try:
    # 1. 先不指定数据库，直接连 MySQL 服务
    temp_conn = pymysql.connect(
        host='127.0.0.1',
        port=server.local_bind_port,
        user=db_user,
        password=db_password,
        charset='utf8mb4'
    )
    # 2. 执行创建数据库命令
    with temp_conn.cursor() as cursor:
        cursor.execute(f"CREATE DATABASE IF NOT EXISTS {db_name} CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;")
    temp_conn.commit()
    temp_conn.close()
    print(f"✅ [System] 数据库 '{db_name}' 检查/创建完毕！")
except Exception as e:
    print(f"❌ [Error] 无法创建数据库: {e}")
    # 这一步如果挂了，后面肯定连不上，所以不用 raise，让后面 SQLAlchemy 报更详细的错

# =========================================================
# 2. 正式连接数据库 (SQLAlchemy)
# =========================================================
SQLALCHEMY_DATABASE_URL = f"mysql+pymysql://{db_user}:{db_password}@127.0.0.1:{server.local_bind_port}/{db_name}"

engine = create_engine(
    SQLALCHEMY_DATABASE_URL, 
    pool_recycle=3600,
    pool_pre_ping=True,
    pool_size=10,
    max_overflow=20
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()

# =========================================================
# 3. 定义表模型
# =========================================================
class DocumentModel(Base):
    __tablename__ = "documents"

    id = Column(Integer, primary_key=True, index=True, comment="主键ID")
    filename = Column(String(255), index=True, comment="文件名")
    upload_time = Column(DateTime, default=datetime.now, comment="上传时间")
    file_size = Column(String(50), comment="文件大小")
    status = Column(String(50), default="indexing", comment="处理状态: indexing(索引中), completed(已完成), failed(失败)")

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
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()