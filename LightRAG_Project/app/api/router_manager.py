import importlib
import pkgutil
from fastapi import FastAPI, APIRouter
# 必须确保这个导入路径存在，并且是一个包
import app.api.routers as routers_pkg

def register_dynamic_routes(app: FastAPI, prefix: str = "/api"):
    """
    动态扫描 app/api/routers 目录下的所有模块，
    并自动注册其中定义的 router 对象。
    """
    package = routers_pkg
    package_path = package.__path__
    package_name = package.__name__

    print(f"🔄 [RouterManager] 正在扫描动态路由: {package_path}")

    route_count = 0
    for _, module_name, _ in pkgutil.iter_modules(package_path):
        full_module_name = f"{package_name}.{module_name}"
        try:
            # 动态导入模块
            module = importlib.import_module(full_module_name)
            
            # 检查是否有 'router' 属性且是 APIRouter 实例
            if hasattr(module, "router") and isinstance(module.router, APIRouter):
                # 注册路由
                # 这里默认使用 /api 前缀，标签使用模块名（例如 Documents, Chat）
                tags = [module_name.capitalize()]
                app.include_router(module.router, prefix=prefix, tags=tags)
                print(f"   ✅ 已加载模块: {module_name} -> tags={tags}")
                route_count += 1
            else:
                 print(f"   ⚠️ 跳过模块: {module_name} (未找到 router 对象)")
                 
        except Exception as e:
            print(f"   ❌ 加载模块 {module_name} 失败: {e}")
            
    print(f"✅ [RouterManager] 路由扫描完成，共加载 {route_count} 个模块")
