"""
Celery 应用配置
用于替换 FastAPI BackgroundTasks，实现任务持久化、失败重试与可视化监控
"""
import os
from celery import Celery
from dotenv import load_dotenv
from app.utils.asyncio_compat import configure_windows_event_loop_policy

configure_windows_event_loop_policy()

# 显式加载 .env（Celery 作为独立进程启动时不会自动加载）
load_dotenv()

# Redis 连接地址（从环境变量读取，默认本地）
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

celery_app = Celery(
    "lightrag_tasks",
    broker=REDIS_URL,          # 消息代理
    backend=REDIS_URL,         # 结果存储
    include=["app.tasks.document_tasks"],  # 自动发现任务
)

# ← 队列路由：默认任务发到 local 队列，避免与其他 Worker 竞争
celery_app.conf.task_default_queue = "local"
celery_app.conf.task_queues = {
    "local": {
        "exchange": "local",
        "exchange_type": "direct",
        "binding_key": "local",
    },
    "celery": {  # 保留默认队列兼容
        "exchange": "celery",
        "exchange_type": "direct",
        "binding_key": "celery",
    },
}

celery_app.conf.update(
    # 序列化格式
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],

    # 时区
    timezone="Asia/Shanghai",
    enable_utc=True,

    # ← Redis 断连自动重试（解决 TimeoutError 后 Worker 假死问题）
    broker_connection_retry_on_startup=True,   # 启动时 Redis 不可用 → 持续重试
    broker_connection_retry=True,              # 运行中断连 → 自动重连
    broker_connection_max_retries=None,        # 无限重试（None = 永不放弃）
    broker_transport_options={
        "socket_timeout": 10,                  # Redis socket 超时 10s
        "socket_connect_timeout": 5,           # 连接超时 5s
        "retry_on_timeout": True,              # 超时自动重试
        "socket_keepalive": True,              # 启用 TCP keepalive，防止 NAT/防火墙断开空闲连接
    },

    # ← 心跳保活：定期向 broker 发送心跳，防止长时间无任务时连接被中间件断开
    broker_heartbeat=30,                        # 每 30s 发送一次心跳
    broker_heartbeat_checkrate=3,               # 心跳检查频率（每 10s 检查一次）

    # 可靠性配置
    task_acks_late=True,             # 任务执行完成后才确认，防止 Worker 崩溃丢任务
    worker_prefetch_multiplier=1,    # 每个 Worker 一次只取 1 个任务（文档处理耗时较长）
    task_reject_on_worker_lost=True, # Worker 意外退出时，任务重新入队

    # 重试策略
    task_max_retries=3,
    task_default_retry_delay=60,     # 重试间隔 60 秒

    # 结果保留时长（1天）
    result_expires=86400,

    # Flower 监控所需
    worker_send_task_events=True,
    task_send_sent_event=True,

    # Windows 兼容：Windows 不支持 fork，必须使用 solo 池
    # Linux/Mac 生产环境可改为 prefork 以支持并发
    worker_pool="solo",
)
