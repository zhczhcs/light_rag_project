# Phase 3 租户边界修复记录（会话 IDOR + 注册自选部门）

> 任务包 C ｜ 日期：2026-09-23
> 代码基线：git HEAD `fbaecf3`（修复验证期间并行任务包 [A] 提交 `f1650d4` 合入 Mock 工具链路，与本修复无冲突；本修复为 git log 中 `[C]` 开头的提交）
> 证据文件：`evidence_pre_fix.txt`（修复前）、`evidence_post_fix.txt`（修复后）
> 复现脚本：`reproduce_tenant_bypass.py`

---

## 1. 漏洞清单

| # | 漏洞 | 位置（修复前） | 成因（一句话） |
|---|------|----------------|----------------|
| 1 | 会话 IDOR（水平越权） | `app/api/routers/chat.py` POST `/chat`：`chat_with_rag()` 内所有 `request.session_id` 使用点——写入用户消息（L281-288）、读取会话历史注入上下文（L292-299）、保存 AI 回复（L641-655）、自动改会话标题（L657-669） | 四个使用点均直接用客户端传入的 `session_id` 操作数据库，没有任何"会话属于当前用户"的归属校验 |
| 2 | 注册自选部门（垂直越权/租户穿越） | `app/api/routers/auth.py` `UserRegister`（L18-22）+ `register()`（L47-59）；配套公开接口 `GET /auth/departments`（L231-235） | 注册请求体强制要求 `department_id` 并原样写入新用户，任何人注册即可选择任意部门、立即获得该部门知识库（workspace `dept_{id}`）访问权 |

排查过程（任务要求：列出所有涉及 session_id 的端点）：

| 端点 | 文件 | 修复前归属校验 | 处置 |
|------|------|----------------|------|
| POST `/chat` | chat.py | ❌ 无 | 入口统一校验（本次修复） |
| GET `/chat-history/sessions` | chat_history.py | ✅ 查询自带 `user_id` 过滤 | 无需改 |
| POST `/chat-history/sessions/new` | chat_history.py | ✅ 以当前用户创建 | 无需改 |
| GET `/chat-history/sessions/{id}/messages` | chat_history.py | ✅ `id + user_id` 双条件查询，否则 404 | 无需改 |
| POST `/chat-history/messages/{id}/favorite` | chat_history.py | ✅ join 后按 `user_id` 过滤 | 无需改 |
| GET `/chat-history/favorites` | chat_history.py | ✅ 同上 | 无需改 |
| `context_service.py` 各函数 | 服务层 | ⚠️ 仅按 `session_id` 读消息 | 不加校验：其唯一调用方是 `/chat`，已在 API 入口校验归属，服务层再加参数会破坏最小侵入（设计取舍记录于此） |

## 2. 修复前复现

### 2.1 方法

本机无法起完整服务（无 MySQL/Qdrant/LLM Key，PyPI 不可达），采用**真实路由代码 + 依赖替代**的端到端复现：

- 用 FastAPI `TestClient` 挂载**未修改的**真实路由（chat.py / auth.py / chat_history.py / admin.py），路径与线网一致（`/api/*`）；
- `get_db` 重定向到 SQLite 内存库（模型定义沿用 `app.database` 真实代码）；
- `get_current_user` 替换为测试桩（Bearer 头直接映射测试用户，绕开 JWT 专心测授权逻辑）；
- `openai` / `app.rag.engine` / `agentic_rag_service` / `file_service` 打桩：假 LLM 会**录制收到的完整 messages**，从而证明受害者历史被读入攻击者请求的上下文。

场景：部门"技术研发部"(id=1) 有用户 alice 及她的会话 100（内含敏感历史："技术研发部第四季度薪资架构与期权方案"）；部门"运营与合规部"(id=2) 有用户 bob（攻击者）。

### 2.2 步骤与结果（详见 evidence_pre_fix.txt）

**ATTACK-1 会话 IDOR**：bob 拿 alice 的 `session_id=100` 调 `POST /api/chat`：

- HTTP **200**，流式正常返回；
- **写**：alice 的会话里多出 bob 写入的 `("user","测试问题")` 和对应 AI 回复；
- **读**：兜底 LLM 收到的 messages 里出现 alice 的敏感历史（薪资/期权两条）——会话历史被越权读入并会进入模型回答；
- **改**：会话 100 标题从"新对话"被改为"测试 相关的问题"。

**ATTACK-2 注册自选部门**：`POST /api/auth/register` 带 `department_id=1`：

- HTTP **200**，返回 `{"message":"注册成功","user_id":3}`；
- 库中 mallory 的 `department_id == 1`——攻击者注册即进入受害者部门，可检索该部门全部文档。

## 3. 修复方案（最小侵入）

| 文件 | 改动 |
|------|------|
| `app/api/routers/chat.py` | `chat_with_rag()` 在空查询校验之后、任何 DB 操作之前新增：若带 `session_id`，以 `ChatSessionModel.id + user_id == current_user.id` 双条件查询，查不到直接 `404 会话不存在`（语义与 chat_history.py 的"对话不存在"一致，且不泄露会话是否存在性）。一处校验覆盖写/读/改全部四个越权点 |
| `app/api/routers/auth.py` | ① `UserRegister` 删除 `department_id` 字段并设 `model_config = ConfigDict(extra="forbid")`——再传该字段直接 422；② `register()` 删除部门存在性校验，新用户 `department_id=None`；③ 删除仅供注册选部门的公开接口 `GET /auth/departments`（部门列表管理端已有 `GET /admin/departments` 可替代） |
| `frontend/src/pages/Login.tsx` | 注册表单删除部门下拉选择、删除 `department_id` 入参、删除对 `/api/auth/departments` 的拉取及 `Select` 引用 |
| `app/api/routers/admin.py` | **未改动**——已有 `PATCH /admin/users/{user_id}/department`（管理员权限），复用作为部门分配入口 |
| `app/schemas/models.py` | **未改动**——注册请求模型实际定义在 auth.py，本文件只含 ChatRequest 等，无 department_id |

语义统一性说明：`/chat` 用 404 而非 403，与 chat_history.py 已有的"对话不存在"（404）保持一致，同时对非所有者隐藏会话存在性。

## 4. 修复后验证（同一脚本、同一场景重跑）

详见 evidence_post_fix.txt：

| 用例 | 修复前 | 修复后 |
|------|--------|--------|
| ATTACK-1 bob 用 alice 的 session_id | 200，写入+历史泄露+标题被改 | **404 会话不存在**；会话消息数不变、标题不变、LLM 未收到任何历史 |
| ATTACK-2 注册带 department_id=1 | 200，mallory 落入门户部门 | **422 extra_forbidden**，mallory 未创建 |
| REG-1 alice 用自己会话问答 | 200（基线） | 200，流式 done，消息正常落库（2→4 条） |
| REG-2 历史读取 alice/bob | 200 / 404 | 200 / 404（不变） |
| REG-3 注册不带部门 | 422（当时为必填） | 200，新用户 `department_id=None` |
| REG-4 管理员改部门 `PATCH /admin/users/3/department?dept_id=1` | — | 200，carol 被分配到部门 1（新流程闭环） |
| REG-5 不传 session_id 纯问答 | — | 200，流式 done（无会话模式不受影响） |

**拦截率：跨租户攻击用例 2/2 被拦截（100%）；回归用例 5/5 通过。**

## 5. 复现/验证方法（可重跑）

```bash
# 依赖（本机 PyPI 不可达时加 -i https://mirrors.aliyun.com/pypi/simple/）
pip install fastapi sqlalchemy pymysql "pydantic[email]" python-jose bcrypt httpx python-multipart
# 修复前（需先 git checkout 到修复前代码）与修复后各跑一次：
python security_eval/reproduce_tenant_bypass.py security_eval/evidence_<阶段>.txt
```

限制说明：LLM/Qdrant 为桩实现，验证的是**授权逻辑与数据流**（消息读写、历史注入上下文、标题更新、注册入库），未验证真实检索内容；真实服务的问答链路未在本机启动（缺 MySQL/Qdrant/Key），回归 REG-1/REG-5 以桩引擎确认接口与流式协议不受影响。

## 6. 发现但未修的其他安全隐患（按任务要求仅记录）

不在本任务范围（计划 §1 明确不做），留待后续决策：

1. `GET /auth/debug/users`（auth.py）**未鉴权**，可枚举全部用户的用户名/邮箱/部门/状态——调试接口挂在生产路由上。
2. `GET /auth/debug/test-security`（auth.py）未鉴权，响应内含 `SECRET_KEY` 前 10 位及算法、有效期等元信息。
3. CORS `allow_origins=["*"]` 且 `allow_credentials=True`（main.py）。
4. token 存 localStorage（前端 Login.tsx），无刷新轮换。
5. 无用户级速率限制；上传无字节数/MIME 校验体系。
6. 日志含查询文本与模型输出，无脱敏与留存策略。
7. `set_user_department`（admin.py）允许管理员把任意用户调入任意部门（含跨部门文档访问权）——属设计内管理能力，但变更后旧 workspace 文档的可见性语义值得在文档中说明。

## 7. 遗留事项

- 前端 `frontend/` 无 node_modules，本机未能跑 `tsc` 类型检查；Login.tsx 改动经全文 grep 确认无悬空引用（`departments`/`Select`/`department_id` 均已清除）。
- 真实环境（MySQL/Qdrant/LLM）启动后的端到端问答验证需在部署环境补做一次。
