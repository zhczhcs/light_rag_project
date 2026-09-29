from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware  # 【新增】
from contextlib import asynccontextmanager
import uvicorn
from app.utils.asyncio_compat import configure_windows_event_loop_policy
# 替换为新的服务和路由管理器
from app.services.rag_service import init_rag_engine
from app.api.router_manager import register_dynamic_routes
# from app.database import _cleanup_tunnel  # 已切换为直连，无需清理隧道

configure_windows_event_loop_policy()

# ===========================
# 1. 生命周期管理
# ===========================
@asynccontextmanager
async def lifespan(app: FastAPI):
    print("🚀 系统正在启动...")
    await init_rag_engine()
    yield
    # _cleanup_tunnel()  # 已切换为直连，无需清理隧道
    print("🛑 系统已关闭")

app = FastAPI(
    title="LightRAG 知识库系统",
    version="1.0.0",
    lifespan=lifespan
)

# ===========================
# 【新增】关键配置：允许跨域
# ===========================
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 允许任何来源的前端访问 (开发环境图省事)
    allow_credentials=True,
    allow_methods=["*"],  # 允许 GET, POST 等所有方法
    allow_headers=["*"],
)

# 动态注册路由
register_dynamic_routes(app, prefix="/api")

@app.get("/")
async def root():
    return {"message": "LightRAG 服务已在线"}

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)