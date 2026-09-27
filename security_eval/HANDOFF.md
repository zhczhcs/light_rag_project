# HANDOFF — AI 安全改造项目交接说明（任务包 G）

> 日期：2026-09-27
> 项目：面向企业知识库的 Secure Agentic RAG（LightRAG 多租户知识库 + 注入防护 + 工具权限网关）
> 计划文档：`docs/AI_SECURITY_IMPROVEMENT_PLAN.md`（v2，工作区文档，未入库）
> 总报告：`security_eval/final_report.md`（**所有对外数字以此为准**）
> 前置报告：`baseline_report.md`（无防护基线）/ `phase4_report.md`（扫描防护）/ `phase5_report.md`（工具网关）

---

## 1. 做了什么 —— 七个任务包

| 任务包 | 阶段 | 交付物 | 关键 commit |
|---|---|---|---|
| A | P0+P1 环境整理 + Mock 工具 | `requirements.txt`、3 个 Mock 高风险工具（send_email/delete_document/export_department_data）、最小工具调用链路 | `f1650d4` |
| B | P2 攻击样本库 + 无防护基线 | `attacks.jsonl`（58 条，四类中英双语）、`run_attacks.py`、基线 ASR：direct 2.08% / indirect 0%（模型抵抗）/ cross_tenant 100% / tool_abuse 72.92% | `0afc164`、`4a73c35`、`e42dcee`、`2527fdb` |
| C | P3 租户边界修复 | 会话 IDOR 修复（`chat.py`/`context_service.py`）+ 注册不再接受自选部门（`auth.py`）；cross_tenant 12/12 → 0/12 | `b87f9ac` |
| D | P4 输入/检索/输出三侧扫描 | `app/core/guard_service.py`（中文 BERT + 11 条英文规则层）；direct_injection 2.08% → 0%（48/48）；误报 0%；输入扫描 p50 114.6ms | `29b3c83` |
| E | P5 自研工具权限网关 | `app/services/tool_gateway.py`（注册表/校验链/预览-确认-执行/限流/审计）；tool_abuse 72.92% → 0%（48/48）；正常流程 6/6 | `8f8a84e`、`e184595`、`23612f1` |
| F | P6 红队评测 + 中文对比实验 + 汇总 | fp_probe_full 0%（50/50）、promptfoo 96/96 pass、中文 BERT vs deberta-v2 对比（JailBench 26.0%/28.5%，英文盲区 0/8）、`final_report.md` | `a97d68a` |
| G | P7 文档与简历收尾 | README 安全架构节、简历四条 bullet（`rag项目文档/简历.md`，主仓外）、面试问答稿（`rag项目文档/AI安全面试问答.md`，主仓外）、本文件 | 本提交 `[G]` |

infra 支撑 commit：`02189c2`（双渠道 LLM 配置：Kimi chat + DashScope embedding/rerank）、`0b643fa`（kimi-compat shim，强制 temperature=1/top_p=0.95）；合并提交 `5c28d19`（pkg-e）、`8b5a753`（pkg-d）。

---

## 2. 没做什么

### 2.1 计划内明确不做（§1 清单，确认未做）

文档级 ACL 细粒度权限模型；上传文件字节数/MIME/解压比/页数校验体系；CORS 收紧/token 移出 localStorage/refresh 轮换；用户级速率限制与预算系统；日志脱敏与数据留存；NeMo/OpenAI/Invariant Guardrails、LLM Guard 集成；完整 AgentDojo harness；Alembic 迁移/CI/测试体系补齐；任何连真实服务的工具（工具一律 Mock）。

### 2.2 计划内要做但本轮没做成（如实留白，均非系统缺陷）

| 项 | 原因 | 恢复方式 |
|---|---|---|
| **indirect_injection 防护后端到端跑测**（最大遗留） | embedding 渠道 09-25 13:46 起持续 403 `AccessDenied.Unpurchased`（免费 1M 额度耗尽），09-24/09-25 两次尝试均 403，主动中止以避免产生"假的 0%"。组件级证据已齐：检索扫描对投毒 chunk 3/3 flagged（conf 1.0）、标记/输入 guard/网关各层代码路径生效 | embedding 充值恢复后：`python security_eval/run_attacks.py --category indirect_injection --passes 3 --delay 3`（completed 文档自动跳过重复上传） |
| **garak 生成端补扫** | 按计划 §7 砍单顺序砍掉：依赖重、与 torch 并存有冲突风险、LLM 额度优先保主评测 | `pip install garak` 后补扫 promptinject/prodan 等 probe |
| **promptfoo 48 条 jailbreak 交互式探针** | 该策略需配置 redteam 生成 LLM（多轮攻击者），本环境无可用配置（kimi 拒非默认采样参数），报 `redteamProvider.id is not a function`；96 条可评测探针结果不受影响 | 有合规的多轮 redteam LLM 后重跑 `redteam generate`/`eval` |
| **PromptGuard 2 本体集成** | HF gated 许可墙，hf-mirror 拉权重 401（09-23、09-25 两次）；按计划 §7 回退中文 BERT + 规则层，评测对照用 deberta-v2 | 接受 Meta 许可拿到权重后可替换线上分类器并复测 |

---

## 3. 已知边界与残留

1. **攻击覆盖边界**：多轮注入/慢速拆散注入、多模态注入、记忆投毒未覆盖（计划 §6 边界）。
2. **越狱型检出率仅 26.0%（中文 BERT）/ 28.5%（deberta-v2）**：检测器学的是"ignore previous instructions"型模式，非越狱语义；当前依赖模型自身拒答兜底（kimi 特异，不可外推）。
3. **输出扫描为事后修正**：流式内容已先发往客户端，靠 `content_correction` 兜底；流式缓冲窗口扫描是 future work。
4. **检索注入扫描是异步审计非门禁**：刻意设计（CPU batch 推理秒级，同步会拖慢每个回答）；丢弃策略 future work。
5. **embedding 免费额度耗尽待充值**：403 持续中，检索降级为 LLM 知识兜底；所有受影响的评测点已在 final_report 逐条注明。
6. **kimi-for-coding 5 小时额度窗口**：批量跑测需错峰（tool_abuse 首跑曾因此中断 23/48，配额恢复后重跑补齐）；强制 temperature=1 → 所有数字为 3 轮合并口径，**换模型必须全量重测**。
7. **wt-pkg-d 残留目录**：worktree 已 prune（`git worktree list` 只剩 main），但工作区根目录 `wt-pkg-d/` 空目录仍被进程锁定，重启后手动删除。
8. **评测残留数据**：评测账号（sec_eval_victim/sec_eval_attacker/d_eval_fp/e_eval_*）、14 份投毒文档（远程库，隔离无副作用）；便携 Node/promptfoo 在 %TEMP%（pfoo），重启即失效，不入库。
9. **模型权重不入库**：`models/bert_cn_prompt_attack_detection` 已 gitignore，复现需从 ModelScope 重新下载。
10. **设计计划文档未入库**：`docs/AI_SECURITY_IMPROVEMENT_PLAN.md` 为工作区文档（v2 版明确替代旧 885 行版）。

---

## 4. 复现命令清单

```bash
# 0) 环境
pip install -r requirements.txt          # Phase 0 产物，直接依赖手工裁剪版
# 检测模型（PromptGuard 2 回退件，权重不入库）
#   ModelScope: bixuechao/bert_cn_prompt_attack_detection → models/，阈值 0.5

# 1) 服务（8000）—— 间接注入跑测另需 celery worker 消费 local 队列
python -m uvicorn main:app --host 127.0.0.1 --port 8000
celery -A app.tasks.celery_app worker -Q local --pool=solo

# 2) 四类攻击（需 LLM/embedding 渠道可用；间接注入需 embedding 恢复）
python security_eval/run_attacks.py --category direct_injection   --passes 3 --delay 3
python security_eval/run_attacks.py --category indirect_injection --passes 3 --delay 3
python security_eval/run_attacks.py --category tool_abuse         --passes 3 --delay 3
# 跨租户 PRE/POST 对照见 reproduce_tenant_baseline.py / run_cross_tenant_baseline.py

# 3) 误报率（403 重试版，跑满 50 有效样本）
python security_eval/fp_probe_full.py --base-url http://127.0.0.1:8000

# 4) 网关正常流程验收（6 用例）
python security_eval/phase5_normal_calls.py --base-url http://127.0.0.1:8000 --rate-limit-max 3

# 5) 中文对比实验（离线；--deberta-dir 指向本地 snapshot）
python security_eval/cn_detector_compare.py --jailbench-csv <JailBench.csv> --deberta-dir <snapshot>

# 6) promptfoo redteam（便携 Node；见 final_report §9）
node <promptfoo>/dist/src/entrypoint.js redteam generate -c promptfooconfig.yaml -o redteam_tests.yaml
CI=true node <promptfoo>/dist/src/entrypoint.js redteam eval -c redteam_tests.yaml -o redteam_results.json
```

报告链：`baseline_report.md`（基线）→ `phase4_report.md`（扫描）→ `phase5_report.md`（网关）→ `final_report.md`（总表，含 promptfoo/中文实验/延迟/时间线）。简历与面试材料在工作区根目录 `rag项目文档/`（`简历.md`、`AI安全面试问答.md`），主仓外不入库。
