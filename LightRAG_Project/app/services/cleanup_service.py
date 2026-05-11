import os
import json
import httpx
import networkx as nx
from sqlalchemy.orm import Session
from app.database import DocumentModel
from app.rag.engine import invalidate_user_engine, invalidate_workspace_engine

# Qdrant 配置（优先环境变量）
QDRANT_HOST = os.environ.get("QDRANT_HOST")
QDRANT_PORT = os.environ.get("QDRANT_PORT")
QDRANT_URL = os.environ.get("QDRANT_URL")
QDRANT_API_KEY = os.environ.get("QDRANT_API_KEY")

# LightRAG 固定的 Qdrant 集合名（全部用户共用同一组集合，通过 workspace_id 字段隔离数据）
QDRANT_COLLECTIONS = [
    "lightrag_vdb_entities",
    "lightrag_vdb_relationships",
    "lightrag_vdb_chunks",
]

def _workspace_id(user_id: int = None, workspace: str = None) -> str:
    """LightRAG 在 Qdrant payload 中用 workspace_id 字段隔离数据，与 engine.py 的 workspace 参数保持一致"""
    if workspace:
        return workspace
    return f"user_{user_id}" if user_id else "_"


def _value_contains_deleted_filename(value, filenames):
    if isinstance(value, str):
        return any(filename in value for filename in filenames)
    if isinstance(value, dict):
        return any(_value_contains_deleted_filename(item, filenames) for item in value.values())
    if isinstance(value, list):
        return any(_value_contains_deleted_filename(item, filenames) for item in value)
    return False


def _remove_deleted_filenames_from_value(value, filenames):
    if not isinstance(value, str):
        return value, False, False

    if "<SEP>" in value:
        parts = [part for part in value.split("<SEP>") if part]
        new_parts = [part for part in parts if part not in filenames]
        changed = new_parts != parts
        return "<SEP>".join(new_parts), changed, changed and not new_parts

    if value in filenames:
        return "", True, True

    return value, False, False


def _clean_graphml_by_filenames(file_path: str, filenames, deleted_entities=None):
    filenames = set(filenames or [])
    deleted_entities = set(deleted_entities or [])
    if not os.path.exists(file_path) or (not filenames and not deleted_entities):
        return

    source_attr_keys = {"source_id", "file_path", "source", "sources", "d4"}
    graph = nx.read_graphml(file_path)

    nodes_to_remove = set()
    for node_name, attrs in list(graph.nodes(data=True)):
        if node_name in deleted_entities or f'"{node_name}"' in deleted_entities:
            nodes_to_remove.add(node_name)
            continue

        for attr_name, attr_value in list(attrs.items()):
            new_value, changed, empty = _remove_deleted_filenames_from_value(attr_value, filenames)
            if not changed:
                continue
            if empty and attr_name in source_attr_keys:
                nodes_to_remove.add(node_name)
                break
            attrs[attr_name] = new_value

    if nodes_to_remove:
        graph.remove_nodes_from(nodes_to_remove)

    edges_to_remove = []
    for edge in list(graph.edges(data=True)):
        source, target, attrs = edge
        remove_edge = False
        for attr_name, attr_value in list(attrs.items()):
            new_value, changed, empty = _remove_deleted_filenames_from_value(attr_value, filenames)
            if not changed:
                continue
            if empty and attr_name in source_attr_keys:
                remove_edge = True
                break
            attrs[attr_name] = new_value
        if remove_edge:
            edges_to_remove.append((source, target))

    if edges_to_remove:
        graph.remove_edges_from(edges_to_remove)

    nx.write_graphml(graph, file_path)

async def perform_delete_all_documents(db: Session, workspace: str = None, department_id: int = None, user_id: int = None):
    print(f"🗑️ [DeleteAll] 准备删除 workspace='{workspace}' 的所有文档...")
    
    # 第一步：获取所有文档的文件名
    query = db.query(DocumentModel)
    if department_id:
        query = query.filter(DocumentModel.department_id == department_id)
    elif user_id:
        query = query.filter(DocumentModel.user_id == user_id)
    docs = query.all()
    filenames = [doc.filename for doc in docs]
    
    if not filenames:
        print(f"⚠️ [DeleteAll] 没有文档需要删除")
        return {"message": "没有文档需要删除", "deleted_count": 0}
    
    print(f"📋 [DeleteAll] 找到 {len(filenames)} 个文档需要删除: {filenames}")
    
    # LightRAG 的 Qdrant 实现固定使用 lightrag_vdb_* 集合，通过 workspace_id payload 字段隔离用户数据
    ws_id = _workspace_id(user_id, workspace)
    collection_names = QDRANT_COLLECTIONS
    
    print(f"🎯 [DeleteAll] 将从以下集合中删除 workspace_id='{ws_id}' 的数据: {', '.join(collection_names)}")
    
    # 第三步：尝试从 Qdrant 的三个集合中删除所有向量数据
    total_deleted_collections = 0
    
    for collection_name in collection_names:
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                # 构造 Qdrant 删除请求的 URL
                delete_url = f"{QDRANT_URL}/collections/{collection_name}/points/delete"
                
                # 删除所有点（不设置筛选条件）
                print(f"📤 [DeleteAll] 正在清空 Qdrant 集合 '{collection_name}'...")
                print(f"📤 [DeleteAll] URL: {delete_url}")
                
                # 发送异步 POST 请求，删除所有点
                response = await client.post(
                    delete_url,
                    headers={
                        "api-key": QDRANT_API_KEY,
                        "Content-Type": "application/json"
                    },
                    json={"filter": {"must": [{"key": "workspace_id", "match": {"value": ws_id}}]}}  # 按 workspace_id 过滤当前用户的数据
                )
                
                # 打印响应状态
                print(f"📥 [DeleteAll] 集合 '{collection_name}' 响应状态码: {response.status_code}")
                
                if response.status_code == 200:
                    print(f"✅ [DeleteAll] 集合 '{collection_name}' 所有向量数据删除成功！")
                    print(f"📊 [DeleteAll] 响应内容: {response.text}")
                    total_deleted_collections += 1
                else:
                    print(f"⚠️ [DeleteAll] 集合 '{collection_name}' 删除响应异常，状态码: {response.status_code}")
                    print(f"⚠️ [DeleteAll] 响应内容: {response.text}")
                    
        except httpx.TimeoutException:
            print(f"❌ [DeleteAll] 集合 '{collection_name}' 删除请求超时！")
        except httpx.ConnectError:
            print(f"❌ [DeleteAll] 集合 '{collection_name}' 连接失败！请检查网络或服务器地址。URL: {QDRANT_URL}")
        except Exception as e:
            print(f"❌ [DeleteAll] 集合 '{collection_name}' 删除过程中发生异常: {str(e)}")
    
    print(f"📊 [DeleteAll] Qdrant 删除完成，成功清空 {total_deleted_collections}/{len(collection_names)} 个集合")
    
    # 第四步：清理本地文件 (Surgical Cleanup - Total Wipeout)
    print(f"🧹 [DeleteAll] 正在清理本地存储文件...")
    # LightRAG workspace: working_dir(./data)/{workspace}/
    data_dir = f"./data/{workspace}" if workspace else (f"./data/user_{user_id}" if user_id else "./data")
    if user_id and not os.path.exists(data_dir):
        print(f"⚠️ [DeleteAll] 数据目录不存在: {data_dir}")
    deleted_files_count = 0
    
    try:
        # 列出并删除 data 目录下的所有 json 和 graphml 文件
        if os.path.exists(data_dir):
            for filename in os.listdir(data_dir):
                file_path = os.path.join(data_dir, filename)
                # 确保是文件且是 LightRAG 的存储文件
                if os.path.isfile(file_path) and (filename.endswith(".json") or filename.endswith(".graphml")):
                    try:
                        os.remove(file_path)
                        deleted_files_count += 1
                        print(f"   🗑️ 已删除本地文件: {filename}")
                    except Exception as e:
                        print(f"   ❌ 删除文件失败 {filename}: {str(e)}")
        else:
             print(f"⚠️ [DeleteAll] 数据目录不存在: {data_dir}")

    except Exception as e:
        print(f"❌ [DeleteAll] 本地文件清理过程中发生异常: {str(e)}")
    
    print(f"✅ [DeleteAll] 本地文件清理完成！共删除 {deleted_files_count} 个文件")

    # 第五步：删除 MySQL 中的所有文档记录
    print(f"🗃️ [DeleteAll] 正在从 MySQL 删除文档记录...")
    del_query = db.query(DocumentModel)
    if department_id:
        del_query = del_query.filter(DocumentModel.department_id == department_id)
    elif user_id:
        del_query = del_query.filter(DocumentModel.user_id == user_id)
    deleted_count = del_query.delete(synchronize_session=False)
    db.commit()
    print(f"✅ [DeleteAll] MySQL 记录删除成功！共删除 {deleted_count} 条记录")
    
    # 提示重启
    print(f"\n💡 [DeleteAll] 删除操作已完成，正在使缓存引擎失效...")
    try:
        if workspace:
            invalidate_workspace_engine(workspace)
        else:
            invalidate_user_engine(user_id)
        print(f"✅ [DeleteAll] 引擎缓存已清除！")
    except Exception as e:
        print(f"⚠️ [DeleteAll] 引擎缓存清除失败: {str(e)}")
    
    return {"message": "删除成功", "deleted_count": deleted_count}


async def perform_delete_document(doc_id: int, db: Session, workspace: str = None, user_id: int = None):
    # 第一步：根据 doc_id 查询 MySQL，拿到该文档的 filename
    doc = db.query(DocumentModel).filter(DocumentModel.id == doc_id).first()
    if not doc:
        return None  # 表示未找到
    
    filename = doc.filename
    print(f"🗑️ [Delete] 准备删除文档: {filename} (ID: {doc_id}, user_id: {user_id})")
    
    # LightRAG Qdrant 固定集合名，通过 workspace_id 隔离用户数据
    ws_id = _workspace_id(user_id, workspace)
    chunks_collection = "lightrag_vdb_chunks"
    entities_collection = "lightrag_vdb_entities"
    relationships_collection = "lightrag_vdb_relationships"
    
    print(f"🎯 [Delete] 集合配置 (workspace_id='{ws_id}'):")
    print(f"   - Chunks: {chunks_collection}")
    print(f"   - Entities: {entities_collection}")
    print(f"   - Relationships: {relationships_collection}")
    
    # ==========================================
    # Step 1: 找线头 (Get Full Doc ID)
    # ==========================================
    print(f"\n🔍 [Step 1] 正在找线头 - 获取 Full Doc ID...")
    print(f"📝 [Step 1] 筛选条件: file_path == '{filename}'")
    
    full_doc_id = None
    all_chunk_payload_ids = []  # 初始化，避免 fallback 未赋值时 UnboundLocalError
    
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            scroll_url = f"{QDRANT_URL}/collections/{chunks_collection}/points/scroll"
            
            scroll_payload = {
                "filter": {
                    "must": [
                        {
                            "key": "file_path",
                            "match": {
                                "value": filename
                            }
                        },
                        {
                            "key": "workspace_id",
                            "match": {
                                "value": ws_id
                            }
                        }
                    ]
                },
                "limit": 1,
                "with_payload": True
            }
            
            response = await client.post(
                scroll_url,
                headers={
                    "api-key": QDRANT_API_KEY,
                    "Content-Type": "application/json"
                },
                json=scroll_payload
            )
            
            if response.status_code == 200:
                data = response.json()
                points = data.get("result", {}).get("points", [])
                
                if points:
                    full_doc_id = points[0].get("payload", {}).get("full_doc_id")
                    print(f"✅ [Step 1] 找到线头！Full Doc ID: {full_doc_id}")
                else:
                    print(f"⚠️ [Step 1] 未找到包含文件名的切片，文档可能未索引或已删除")
            else:
                print(f"⚠️ [Step 1] 查询失败，状态码: {response.status_code}")
                print(f"⚠️ [Step 1] 响应内容: {response.text}")
                
    except Exception as e:
        print(f"❌ [Step 1] 查询过程中发生异常: {str(e)}")
    
    if not full_doc_id:
        print(f"⚠️ [Step 1] 无法从 Qdrant 获取 Full Doc ID，尝试从本地 kv_store 查找...")
        
        # 回退方案：从本地 kv_store_doc_status.json 按 file_path 匹配
        fallback_data_dir = f"./data/{workspace}" if workspace else (f"./data/user_{user_id}" if user_id else "./data")
        
        doc_status_path = os.path.join(fallback_data_dir, "kv_store_doc_status.json")
        if os.path.exists(doc_status_path):
            try:
                with open(doc_status_path, "r", encoding="utf-8") as f:
                    doc_status_data = json.load(f)
                for doc_key, doc_val in doc_status_data.items():
                    if doc_val.get("file_path") == filename:
                        full_doc_id = doc_key
                        all_chunk_payload_ids = doc_val.get("chunks_list", [])
                        print(f"✅ [Step 1 Fallback] 从本地 kv_store 找到！Full Doc ID: {full_doc_id}, chunks: {len(all_chunk_payload_ids)}")
                        break
            except Exception as e:
                print(f"⚠️ [Step 1 Fallback] 读取本地 kv_store 失败: {e}")
        
        if not full_doc_id:
            print(f"⚠️ [Step 1] Qdrant 和本地均未找到 Full Doc ID")
    
    # 无论 full_doc_id 来自 Qdrant 还是本地回退，都尝试 Qdrant 清理
    all_point_ids = []
    
    # 当 full_doc_id 为空时（如之前索引失败导致 Qdrant 有向量但本地 JSON 无记录），
    # 按 file_path + workspace_id 直接查找并删除 Qdrant 中的残留向量
    if not full_doc_id:
        print(f"\n🔍 [Step 2b] full_doc_id 为空，按文件名直接清理 Qdrant 残留...")
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                scroll_url = f"{QDRANT_URL}/collections/{chunks_collection}/points/scroll"
                scroll_payload = {
                    "filter": {
                        "must": [
                            {"key": "file_path", "match": {"value": filename}},
                            {"key": "workspace_id", "match": {"value": ws_id}}
                        ]
                    },
                    "limit": 1000,
                    "with_payload": False
                }
                response = await client.post(scroll_url, headers={"api-key": QDRANT_API_KEY, "Content-Type": "application/json"}, json=scroll_payload)
                if response.status_code == 200:
                    data = response.json()
                    points = data.get("result", {}).get("points", [])
                    if points:
                        point_ids = [str(p["id"]) for p in points]
                        delete_url = f"{QDRANT_URL}/collections/{chunks_collection}/points/delete"
                        del_response = await client.post(delete_url, headers={"api-key": QDRANT_API_KEY, "Content-Type": "application/json"}, json={"points": point_ids})
                        if del_response.status_code == 200:
                            print(f"✅ [Step 2b] 按文件名清理 Qdrant chunks 成功: {len(point_ids)} 个")
                        else:
                            print(f"⚠️ [Step 2b] 清理 chunks 失败: {del_response.status_code}")
                    else:
                        print(f"ℹ️ [Step 2b] Qdrant 中无该文件名的残留 chunks")
                else:
                    print(f"⚠️ [Step 2b] scroll 查询失败: {response.status_code}")
        except Exception as e:
            print(f"❌ [Step 2b] 按文件名清理 Qdrant 异常: {e}")
    
    if full_doc_id:
        # ==========================================
        # Step 2: 一锅端 (Find All Chunks by Doc ID)
        # ==========================================
        print(f"\n🔗 [Step 2] 正在一锅端 - 查找所有关联切片...")
        print(f"📝 [Step 2] 筛选条件: full_doc_id == {full_doc_id}")
        
        # 保留可能来自 fallback 的 chunk IDs，Qdrant Step 2 只追加 point IDs
        
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                scroll_url = f"{QDRANT_URL}/collections/{chunks_collection}/points/scroll"
                
                scroll_payload = {
                    "filter": {
                        "must": [
                            {
                                "key": "full_doc_id",
                                "match": {
                                    "value": full_doc_id
                                }
                            },
                            {
                                "key": "workspace_id",
                                "match": {
                                    "value": ws_id
                                }
                            }
                        ]
                    },
                    "limit": 1000,
                    "with_payload": True
                }
                
                response = await client.post(
                    scroll_url,
                    headers={
                        "api-key": QDRANT_API_KEY,
                        "Content-Type": "application/json"
                    },
                    json=scroll_payload
                )
                
                if response.status_code == 200:
                    data = response.json()
                    points = data.get("result", {}).get("points", [])
                    
                    all_point_ids = [str(point["id"]) for point in points]
                    # 合并 Qdrant chunk IDs（可能 fallback 已有部分）
                    qdrant_chunk_ids = [point.get("payload", {}).get("id") for point in points if point.get("payload", {}).get("id")]
                    if qdrant_chunk_ids:
                        all_chunk_payload_ids = list(set(all_chunk_payload_ids + qdrant_chunk_ids))
                    
                    print(f"✅ [Step 2] 查找完成！找到 {len(all_point_ids)} 个切片")
                    print(f"🔗 [Step 2] 通过线头找到 Doc ID: {full_doc_id}，关联切片总数: {len(all_point_ids)}")
                else:
                    print(f"⚠️ [Step 2] 查询失败，状态码: {response.status_code}")
                    print(f"⚠️ [Step 2] 响应内容: {response.text}")
                    
        except Exception as e:
            print(f"❌ [Step 2] 查询过程中发生异常: {str(e)}")
        
        if not all_point_ids:
            print(f"⚠️ [Step 2] 未找到任何切片，跳过 Qdrant 删除步骤")
        else:
            # ==========================================
            # Step 3: 外科手术 - 删除实体
            # ==========================================
            print(f"\n🔬 [Step 3] 正在外科手术 - 处理实体...")
            
            try:
                async with httpx.AsyncClient(timeout=30.0) as client:
                    scroll_url = f"{QDRANT_URL}/collections/{entities_collection}/points/scroll"
                    
                    scroll_payload = {
                        "filter": {
                            "must": [
                                {
                                    "key": "workspace_id",
                                    "match": {
                                        "value": ws_id
                                    }
                                }
                            ]
                        },
                        "limit": 1000,
                        "with_payload": True
                    }
                    
                    response = await client.post(
                        scroll_url,
                        headers={
                            "api-key": QDRANT_API_KEY,
                            "Content-Type": "application/json"
                        },
                        json=scroll_payload
                    )
                    
                    if response.status_code == 200:
                        data = response.json()
                        entities = data.get("result", {}).get("points", [])
                        
                        entities_to_delete = []
                        entities_to_update = []
                        
                        for entity in entities:
                            entity_id = entity["id"]
                            entity_name = entity.get("payload", {}).get("entity_name", "")
                            source_id = entity.get("payload", {}).get("source_id", "")
                            content = entity.get("payload", {}).get("content", "")
                            
                            if not source_id:
                                continue
                            
                            src_list = source_id.split("<SEP>")
                            content_list = content.split("<SEP>") if content else []
                            
                            new_src_list = []
                            new_content_list = []
                            deleted_indices = []
                            
                            for idx, src_id in enumerate(src_list):
                                if src_id not in all_chunk_payload_ids:
                                    new_src_list.append(src_id)
                                    if idx < len(content_list):
                                        new_content_list.append(content_list[idx])
                                else:
                                    deleted_indices.append(idx)
                            
                            if not new_src_list:
                                entities_to_delete.append({"id": entity_id, "name": entity_name})
                            elif len(new_src_list) != len(src_list):
                                new_source_id = "<SEP>".join(new_src_list)
                                new_content = "<SEP>".join(new_content_list)
                                entities_to_update.append({
                                    "id": entity_id,
                                    "name": entity_name,
                                    "new_source_id": new_source_id,
                                    "new_content": new_content,
                                    "deleted_indices": deleted_indices
                                })
                        
                        print(f"📊 [Step 3] 扫描完成！需要删除 {len(entities_to_delete)} 个实体，更新 {len(entities_to_update)} 个实体")
                        
                        if entities_to_delete:
                            delete_url = f"{QDRANT_URL}/collections/{entities_collection}/points/delete"
                            response = await client.post(
                                delete_url,
                                headers={
                                    "api-key": QDRANT_API_KEY,
                                    "Content-Type": "application/json"
                                },
                                json={"points": [e["id"] for e in entities_to_delete]}
                            )
                            if response.status_code == 200:
                                print(f"✅ [Step 3] 删除 {len(entities_to_delete)} 个实体成功")
                        
                        if entities_to_update:
                            print(f"\n📝 [Step 3] 正在更新实体...")
                            for update_item in entities_to_update:
                                update_url = f"{QDRANT_URL}/collections/{entities_collection}/points/payload"
                                await client.post(
                                    update_url,
                                    headers={
                                        "api-key": QDRANT_API_KEY,
                                        "Content-Type": "application/json"
                                    },
                                    json={
                                        "payload": {
                                            "source_id": update_item["new_source_id"],
                                            "content": update_item["new_content"]
                                        },
                                        "points": [update_item["id"]]
                                    }
                                )
                            print(f"✅ [Step 3] 更新 {len(entities_to_update)} 个实体成功")
                            
                    else:
                        print(f"⚠️ [Step 3] 查询实体失败，状态码: {response.status_code}")
                        
            except Exception as e:
                print(f"❌ [Step 3] 处理实体过程中发生异常: {str(e)}")
            
            # ==========================================
            # Step 4: 外科手术 - 删除关系
            # ==========================================
            print(f"\n🔬 [Step 4] 正在外科手术 - 处理关系...")
            
            try:
                async with httpx.AsyncClient(timeout=30.0) as client:
                    scroll_url = f"{QDRANT_URL}/collections/{relationships_collection}/points/scroll"
                    
                    scroll_payload = {
                        "filter": {
                            "must": [
                                {
                                    "key": "workspace_id",
                                    "match": {
                                        "value": ws_id
                                    }
                                }
                            ]
                        },
                        "limit": 1000,
                        "with_payload": True
                    }
                    
                    response = await client.post(
                        scroll_url,
                        headers={
                            "api-key": QDRANT_API_KEY,
                            "Content-Type": "application/json"
                        },
                        json=scroll_payload
                    )
                    
                    if response.status_code == 200:
                        data = response.json()
                        relationships = data.get("result", {}).get("points", [])
                        
                        relationships_to_delete = []
                        relationships_to_update = []
                        
                        for rel in relationships:
                            rel_id = rel["id"]
                            source_id = rel.get("payload", {}).get("source_id", "")
                            content = rel.get("payload", {}).get("content", "")
                            
                            if not source_id:
                                continue
                            
                            src_list = source_id.split("<SEP>")
                            content_list = content.split("<SEP>") if content else []
                            
                            new_src_list = []
                            new_content_list = []
                            deleted_indices = []
                            
                            for idx, src_id in enumerate(src_list):
                                if src_id not in all_chunk_payload_ids:
                                    new_src_list.append(src_id)
                                    if idx < len(content_list):
                                        new_content_list.append(content_list[idx])
                                else:
                                    deleted_indices.append(idx)
                            
                            if not new_src_list:
                                relationships_to_delete.append(rel_id)
                            elif len(new_src_list) != len(src_list):
                                new_source_id = "<SEP>".join(new_src_list)
                                new_content = "<SEP>".join(new_content_list)
                                relationships_to_update.append({
                                    "id": rel_id,
                                    "new_source_id": new_source_id,
                                    "new_content": new_content,
                                    "deleted_indices": deleted_indices
                                })
                        
                        print(f"📊 [Step 4] 扫描完成！需要删除 {len(relationships_to_delete)} 个关系，更新 {len(relationships_to_update)} 个关系")
                        
                        if relationships_to_delete:
                            delete_url = f"{QDRANT_URL}/collections/{relationships_collection}/points/delete"
                            response = await client.post(
                                delete_url,
                                headers={
                                    "api-key": QDRANT_API_KEY,
                                    "Content-Type": "application/json"
                                },
                                json={"points": relationships_to_delete}
                            )
                            if response.status_code == 200:
                                print(f"✅ [Step 4] 删除 {len(relationships_to_delete)} 个关系成功")
                        
                        if relationships_to_update:
                            print(f"\n📝 [Step 4] 正在更新关系...")
                            for update_item in relationships_to_update:
                                update_url = f"{QDRANT_URL}/collections/{relationships_collection}/points/payload"
                                await client.post(
                                    update_url,
                                    headers={
                                        "api-key": QDRANT_API_KEY,
                                        "Content-Type": "application/json"
                                    },
                                    json={
                                        "payload": {
                                            "source_id": update_item["new_source_id"],
                                            "content": update_item["new_content"]
                                        },
                                        "points": [update_item["id"]]
                                    }
                                )
                            print(f"✅ [Step 4] 更新 {len(relationships_to_update)} 个关系成功")
                            
                    else:
                        print(f"⚠️ [Step 4] 查询关系失败，状态码: {response.status_code}")
                        
            except Exception as e:
                print(f"❌ [Step 4] 处理关系过程中发生异常: {str(e)}")
            
            # ==========================================
            # Step 5: 灭口 - 删除切片
            # ==========================================
            print(f"\n🗑️ [Step 5] 正在灭口 - 删除所有切片...")
            try:
                async with httpx.AsyncClient(timeout=30.0) as client:
                    delete_url = f"{QDRANT_URL}/collections/{chunks_collection}/points/delete"
                    response = await client.post(
                        delete_url,
                        headers={
                            "api-key": QDRANT_API_KEY,
                            "Content-Type": "application/json"
                        },
                        json={"points": all_point_ids}
                    )
                    if response.status_code == 200:
                        print(f"✅ [Step 5] 删除 {len(all_point_ids)} 个切片成功")
            except Exception as e:
                print(f"❌ [Step 5] 删除切片过程中发生异常: {str(e)}")

    # ==========================================
    # Step 6: 销户 - 删除 MySQL 记录
    # ==========================================
    print(f"\n🗃️ [Step 6] 正在从 MySQL 删除文档记录...")
    db.delete(doc)
    db.commit()
    print(f"\n✅ [Step 6] MySQL 记录删除成功！文件名: {filename}")
    
    # ==========================================
    # Step 7: 本地文件外科手术 (Safe Surgical Cleanup)
    # ==========================================
    print(f"\n🧹 [Step 7] 正在进行本地文件外科手术...")
    # LightRAG workspace: working_dir(./data)/user_X/
    data_dir = f"./data/{workspace}" if workspace else (f"./data/user_{user_id}" if user_id else "./data")
    
    if not full_doc_id:
        # 即使 Qdrant 中找不到 Full Doc ID（如之前索引失败），也要按文件名强制清理本地残留
        print(f"⚠️ [Step 7] 无 Full Doc ID，按文件名 '{filename}' 强制清理本地残留...")
        perform_local_cleanup_by_filename(data_dir, filename)
    else:
        perform_local_cleanup(data_dir, full_doc_id, all_chunk_payload_ids, [filename])

    # ==========================================
    # Step 8: 清除用户引擎缓存 (Hot Invalidate)
    # ==========================================
    print(f"\n🔄 [Step 8] 正在清除用户引擎缓存，下次查询时自动重载...")
    if workspace:
        invalidate_workspace_engine(workspace)
        print(f"✅ [Step 8] workspace '{workspace}' 的引擎缓存已清除")
    elif user_id:
        invalidate_user_engine(user_id)
        print(f"✅ [Step 8] 用户 {user_id} 的引擎缓存已清除")
    else:
        print(f"⚠️ [Step 8] 无 workspace/user_id，跳过引擎缓存清除")

    print(f"\n🎉 [Delete] 全链路级联删除完成！")
    
    return {"message": "删除成功", "filename": filename, "deleted_chunks": len(all_point_ids) if full_doc_id else 0}

def perform_local_cleanup(data_dir, full_doc_id, all_chunk_payload_ids, filenames=None):
    # This helper function encapsulates steps 7.1 to 7.9 to keep main function readable
    filenames = set(filenames or [])

    # Step 7.1: kv_store_doc_status.json
    try:
        doc_status_file = os.path.join(data_dir, "kv_store_doc_status.json")
        if os.path.exists(doc_status_file):
            with open(doc_status_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            if full_doc_id and full_doc_id in data:
                del data[full_doc_id]
                with open(doc_status_file, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                print(f"✅ [Cleanup] doc_status cleaned")
    except Exception as e:
        print(f"❌ [Cleanup] doc_status failed: {e}")

    # Step 7.2: kv_store_full_docs.json
    try:
        full_docs_file = os.path.join(data_dir, "kv_store_full_docs.json")
        if os.path.exists(full_docs_file):
            with open(full_docs_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            if full_doc_id and full_doc_id in data:
                del data[full_doc_id]
                with open(full_docs_file, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                print(f"✅ [Cleanup] full_docs cleaned")
    except Exception as e:
        print(f"❌ [Cleanup] full_docs failed: {e}")

    # Step 7.3: kv_store_text_chunks.json
    try:
        chunks_file = os.path.join(data_dir, "kv_store_text_chunks.json")
        if os.path.exists(chunks_file):
            with open(chunks_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            for chunk_id in all_chunk_payload_ids:
                if chunk_id in data:
                    del data[chunk_id]
            with open(chunks_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            print(f"✅ [Cleanup] text_chunks cleaned")
    except Exception as e:
        print(f"❌ [Cleanup] text_chunks failed: {e}")

    # Step 7.4: kv_store_full_entities.json
    final_deleted_entities = []
    try:
        entities_file = os.path.join(data_dir, "kv_store_full_entities.json")
        if os.path.exists(entities_file):
            with open(entities_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            
            to_delete = []
            for name, val in data.items():
                # Skip if val is not dict (could be doc_id index if structure differs, but code assumes standard lightrag)
                if not isinstance(val, dict): continue
                
                source_id = val.get("source_id", "")
                if not source_id: continue
                
                src_list = source_id.split("<SEP>")
                content_list = val.get("content", "").split("<SEP>")
                
                new_src = []
                new_cont = []
                
                for idx, src in enumerate(src_list):
                    if src not in all_chunk_payload_ids:
                        new_src.append(src)
                        if idx < len(content_list):
                            new_cont.append(content_list[idx])
                
                if not new_src:
                    to_delete.append(name)
                elif len(new_src) != len(src_list):
                    data[name]["source_id"] = "<SEP>".join(new_src)
                    data[name]["content"] = "<SEP>".join(new_cont)
            
            final_deleted_entities = to_delete
            for name in to_delete:
                del data[name]
            
            if full_doc_id in data:
                del data[full_doc_id]
                
            with open(entities_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            print(f"✅ [Cleanup] entities cleaned")
    except Exception as e:
        print(f"❌ [Cleanup] entities failed: {e}")

    # Step 7.5: kv_store_full_relations.json
    try:
        file_path = os.path.join(data_dir, "kv_store_full_relations.json")
        if os.path.exists(file_path):
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            to_delete = []
            for relation_key, relation_val in data.items():
                if not isinstance(relation_val, dict):
                    continue

                source_id = relation_val.get("source_id", "")
                if not source_id:
                    continue

                src_list = source_id.split("<SEP>")
                content_list = relation_val.get("content", "").split("<SEP>")

                new_src = []
                new_cont = []
                for idx, src in enumerate(src_list):
                    if src not in all_chunk_payload_ids:
                        new_src.append(src)
                        if idx < len(content_list):
                            new_cont.append(content_list[idx])

                if not new_src:
                    to_delete.append(relation_key)
                elif len(new_src) != len(src_list):
                    relation_val["source_id"] = "<SEP>".join(new_src)
                    relation_val["content"] = "<SEP>".join(new_cont)

            for relation_key in to_delete:
                del data[relation_key]

            if full_doc_id and full_doc_id in data:
                del data[full_doc_id]

            with open(file_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            print(f"✅ [Cleanup] relations cleaned")
    except Exception as e:
        print(f"❌ [Cleanup] relations failed: {e}")

    # Step 7.6: kv_store_llm_response_cache.json
    try:
        file_path = os.path.join(data_dir, "kv_store_llm_response_cache.json")
        if os.path.exists(file_path):
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            keys_to_del = [
                key
                for key, value in data.items()
                if value.get("chunk_id") in all_chunk_payload_ids or _value_contains_deleted_filename(value, filenames)
            ]
            for k in keys_to_del:
                del data[k]
            with open(file_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            print(f"✅ [Cleanup] llm_cache cleaned")
    except Exception as e:
        print(f"❌ [Cleanup] llm_cache failed: {e}")

    # Step 7.8: graph_chunk_entity_relation.graphml
    try:
        file_path = os.path.join(data_dir, "graph_chunk_entity_relation.graphml")
        if os.path.exists(file_path):
            graph_deleted_entities = set(final_deleted_entities)
            graph_deleted_entities.update({f'"{entity}"' for entity in final_deleted_entities})
            _clean_graphml_by_filenames(file_path, filenames, graph_deleted_entities)
            print(f"✅ [Cleanup] graphml cleaned")
    except Exception as e:
        print(f"❌ [Cleanup] graphml failed: {e}")

def perform_local_cleanup_by_filename(data_dir: str, filename: str):
    """
    按文件名强制清理本地残留数据（用于 Qdrant 中找不到 Full Doc ID 的情况）。
    遍历所有本地 JSON 文件，删除 file_path 匹配的 doc_id 和关联的 chunks/entities/relations。
    """
    import re
    
    print(f"🧹 [CleanupByName] 按文件名 '{filename}' 清理本地残留...")
    
    # Step A: 从 doc_status 找到关联的 doc_id 和 chunks_list
    full_doc_id = None
    all_chunk_payload_ids = []
    doc_status_file = os.path.join(data_dir, "kv_store_doc_status.json")
    
    try:
        if os.path.exists(doc_status_file):
            with open(doc_status_file, "r", encoding="utf-8") as f:
                doc_status_data = json.load(f)
            for doc_key, doc_val in doc_status_data.items():
                if doc_val.get("file_path") == filename:
                    full_doc_id = doc_key
                    all_chunk_payload_ids = doc_val.get("chunks_list", [])
                    break
    except Exception as e:
        print(f"⚠️ [CleanupByName] 读取 doc_status 失败: {e}")
    
    if not full_doc_id:
        print(f"⚠️ [CleanupByName] 本地未找到 doc_id，继续按文件名清理残留")
    else:
        print(f"✅ [CleanupByName] 找到残留: doc_id={full_doc_id}, chunks={len(all_chunk_payload_ids)}")

    # Step B: 复用 perform_local_cleanup 清理所有关联数据
    perform_local_cleanup(data_dir, full_doc_id, all_chunk_payload_ids, [filename])

