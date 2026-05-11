# LightRAG Agentic Knowledge Base

基于 LightRAG 构建的企业级 Agentic RAG 知识库系统，支持多格式文档解析、图谱+向量混合检索、引用溯源问答、多部门数据隔离与长程对话记忆。

![demo](assets/demo.gif)

## Overview

这个项目面向企业内部知识库与团队级智能助手场景，重点解决以下问题：

- 多格式文档的统一解析、索引与检索
- 复杂问题下的图谱+向量混合召回
- Agentic 检索链路中的查询消解、查询分解、自纠错重检索
- 带引用编号的可追溯问答结果
- 多部门共享与跨部门隔离
- 长会话场景下的上下文记忆与历史复用

## Core Capabilities

### 1. Hybrid Retrieval

- 同时走知识图谱路径与向量检索路径
- 图谱侧基于实体、关系链召回相关切片
- 向量侧结合原始查询、查询改写与子查询结果补充语义覆盖
- 最终结果统一去重、重排并送入 LLM 生成答案

### 2. Agentic Retrieval Loop

- Query Resolver 负责指代消解、查询补全与复杂问题预处理
- Query Decomposer 将多意图、多维度问题拆成子查询独立检索
- Retrieval Grader 对召回结果做相关性判断
- 当召回质量不足时触发 Query Rewriter 改写查询并重检索
- qwen3-rerank 对候选片段交叉编码打分，过滤低质量上下文

### 3. Multi-Model Routing

- 在单次 LLM 调用中完成意图识别、复杂度分级与查询增强判断
- 简单问题走轻量模型以降低成本
- 复杂问题走更强模型与完整 Agentic 链路以保证质量
- 当前工程最优方案采用主回答 Flash + Resolver Flash + Grader/Rewriter 高质量模型的混合配置

### 4. Citation Grounding

- 检索阶段注入引用锚点 `[n]`
- 生成阶段保留可追踪的引用编号
- 返回前清理孤儿引用、无效编号与无用尾部引用块
- 前端可展示真实文档名与对应原文片段

### 5. Long-Context Memory

- 滑动窗口保留最近多轮对话连续性
- Embedding/BM25 混合召回历史上下文
- 滚动摘要压缩超长会话，降低上下文膨胀

### 6. Workspace Isolation

- 按 `dept_{id}` / `user_{id}` 进行知识空间隔离
- 同部门用户共享知识库
- 跨部门与个人空间默认隔离
- 向量、图谱、本地文件和缓存均按 workspace 区分

## Architecture

```text
React Frontend
    |
    v
FastAPI API Layer
    |
    +-- JWT Auth
    +-- Chat / Upload / Document APIs
    +-- Agentic RAG Orchestrator
    |
    +-- LightRAG Engine
    |     +-- Graph Retrieval
    |     +-- Vector Retrieval (Qdrant)
    |     +-- Rerank
    |
    +-- Celery Worker
    |     +-- Async Indexing
    |
    +-- Redis
    +-- MySQL
```

## Benchmark Results

项目内置自动化 Benchmark，用于同时评估检索质量与生成质量。

- 检索指标：chunk 命中率、召回率、精确率
- 生成指标：忠实度、回答相关性、完整性
- 判分方式：LLM-as-a-Judge

当前较优配置下：

- 37 个预设 chunk 中正确命中 33 个
- chunk 级命中率 89.2%
- 综合评测总分 87.6
- 常规问答响应时间约 10-15s
- 复杂多跳问答响应时间约 25-35s

## Tech Stack

| Layer | Tech |
|---|---|
| Frontend | React, Vite, Ant Design |
| Backend | FastAPI, SQLAlchemy, MySQL |
| Retrieval | LightRAG, Qdrant |
| Queue | Celery, Redis |
| LLM / Rerank | DashScope (Qwen series), qwen3-rerank |
| Embedding | text-embedding-v4 |

## Project Structure

```text
.
├── app/                    # FastAPI backend
├── frontend/               # React frontend
├── benchmark/              # Benchmark and experiment scripts
├── docs/                   # Project docs
├── data/                   # Workspace-scoped runtime data
├── assets/                 # Images and demos
├── main.py                 # FastAPI entry
└── start-all.ps1           # Start backend + celery + frontend
```

## Quick Start

### 1. Start all services

```powershell
.\start-all.ps1
```

可选：

```powershell
.\start-all.ps1 -Flower
.\start-all.ps1 -Stop
```

### 2. Frontend and backend ports

- Backend API: `http://localhost:8000`
- Frontend: `http://localhost:5173`
- Flower: `http://localhost:5555`（可选）

## Why This Project Matters

这个项目不是简单把 RAG 跑通，而是围绕真实工程问题做了完整落地：

- 用混合检索提升复杂问题召回
- 用 Agentic 自纠错链路改善检索质量
- 用多模型路由控制时延与成本
- 用 Benchmark 和重复实验做配置决策，而不是凭单次结果拍脑袋调参

## License

本项目遵循仓库中的 [LICENSE](LICENSE)。
                ┌──────────────────────────┐
                │ ① 关键词提取 + 复杂度分级 │
                │ L1/L2/L3 · HYDE?       │
                │ NEED_REFS?             │
                └───────────┬──────────────┘
                            │
                            ▼
                ┌──────────────────────────┐
                │ ② QueryResolver          │
                │ 指代消解 / 查询改写       │
                │ QueryDecomposer 子查询   │
                └───────────┬──────────────┘
                            │
                            ▼
                    ┌───────┴───────┐
                    │               │
                    ▼               ▼
                ┌─────────┐   ┌─────────┐
                │③-a HyDE │   │③-b 直接 │
                │假设文档  │   │检索     │
                │+向量检索 │   │         │
                └────┬────┘   └────┬────┘
                     │             │
                     └──────┬──────┘
                            │
                            ▼
┌────────────────────────────────────────────────────────────────────────────┐
│ ④ LightRAG 混合检索 (engine.aquery_llm)                                    │
│    ┌─────────────────────────────────────────────────────────────────────┐ │
│    │  内部流程：                                                          │ │
│    │                                                                    │ │
│    │   向量路径 ──►  chunks_vdb.query()                                  │ │
│    │   + hyde_extra    ──►                                              │ │
│    │     chunks          三路合并 ──► 去重 ──► Rerank ──► LLM生成回答    │ │
│    │   图谱路径 ──►      (round-robin)    (qwen3-rerank)   (带引用[n])  │ │
│    │   实体+关系                                                         │ │
│    │   关键词路径 ──►  keyword_chunks                                    │ │
│    │                                                                    │ │
│    │   注：Rerank 发生在 aquery_llm 内部，对合并后的候选 chunks 重打分   │ │
│    └─────────────────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────┬──────────────────────────────────────┘
                                      │
                                      ▼
                           ┌─────────────────────┐
                           │ ⑤ RetrievalGrader   │
                           │ 评分 chunks 相关性   │
                           └──────────┬──────────┘
                                      │
                  ┌───────────────────┼───────────────────┐
                  │                   │                   │
                  ▼                   ▼                   ▼
            ┌───────────┐      ┌───────────┐      ┌───────────┐
            │ 0 chunks  │      │  不通过   │      │   通过    │
            └─────┬─────┘      └─────┬─────┘      └─────┬─────┘
                  │                  │                  │
                  │                  ▼                  │
                  │       ┌────────────────────┐        │
                  │       │ Neighbor Chunk     │        │
                  │       │ 扩展 → 重新评分    │        │
                  │       └──────────┬────┬────┘        │
                  │                  │    │             │
                  │               Yes│    │No           │
                  │                  │    │             │
                  │                  │    ▼             │
                  │                  │ ┌──────────────┐ │
                  │                  │ │还有重试次数? │ │
                  │                  │ └──────┬───────┘ │
                  │                  │        │         │
                  │                  │   Yes  │   No    │
                  │                  │    │   │    │    │
                  │                  │    ▼   │    ▼    │
                  │                  │ ┌──────┐ ┌──────┐│
                  │                  │ │Query │ │grade_││
                  │                  │ │Rewrit│ │passed││
                  │                  │ │er改写│ │=False││
                  │                  │ │查询  │ │break ││
                  │                  │ └──┬───┘ └──────┘│
                  │                  │    │             │
                  │                  └────┼─────────────┘
                  │                       │
                  ▼                       │
            ┌──────────────┐              │
            │还有重试次数? │              │
            └──────┬───────┘              │
                   │                      │
              Yes  │   No                 │
               │   │    │                 │
               ▼   │    ▼                 │
            ┌──────┐│ ┌──────┐            │
            │Query ││ │grade_│            │
            │Rewrit││ │passed│            │
            │er改写││ │=False│            │
            │查询  ││ │break │            │
            └──┬───┘│ └──────┘            │
               │    │                      │
               └────┼──────────────────────┘
                    │
                    ▼
         ┌─────────────────────┐
         │ 最终结果 → 前端      │
         │ 孤儿引用清理         │
         │ 流式输出给用户       │
         └─────────────────────┘
```

### Agentic RAG 循环详解

```
┌────────────────────────────────────────────────────────────────────────────┐
│                    AgenticOrchestrator 内部循环 (max_retries=1)             │
└────────────────────────────────────────────────────────────────────────────┘

                             ┌─────────────┐
                             │ aquery_llm()│◄────────────────────────────┐
                             │  (循环入口)  │                             │
                             └──────┬──────┘                             │
                                    │                                    │
                             ┌──────▼──────┐                             │
                             │  提取 chunks  │                             │
                             └──────┬──────┘                             │
                                    │                                    │
                          ┌─────────┴─────────┐                         │
                          │                   │                         │
                          ▼                   ▼                         │
                    ┌───────────┐      ┌───────────┐                   │
                    │ chunks=0  │      │ chunks>0  │                   │
                    └─────┬─────┘      └─────┬─────┘                   │
                          │                  │                         │
                          ▼                  ▼                         │
                    ┌─────────────┐    ┌─────────────┐                 │
                    │还有重试次数?│    │ Grader评分  │                 │
                    └──────┬──────┘    └──────┬──────┘                 │
                           │                  │                         │
                     ┌─────┴─────┐      ┌─────┴─────┐                   │
                     │           │      │           │                   │
                    Yes         No     通过      不通过                 │
                     │           │      │           │                   │
                     ▼           ▼      ▼           ▼                   │
                ┌────────┐  ┌──────┐ break    ┌─────────────────┐     │
                │Query   │  │grade_│ (返回)   │ Neighbor Chunk  │     │
                │Rewriter│  │passed│          │ 扩展 + 重新评分 │     │
                │改写查询 │  |=False│          └────────┬────────┘     │
                │continue├─┤break │                   │              │
                └────────┘  └──────┘              ┌────┴────┐         │
                                                   │         │         │
                                                  Yes       No         │
                                                   │         │         │
                                                   ▼         ▼         │
                                                break    ┌──────────┐  │
                                                (返回)   │还有重试? │  │
                                                         └────┬─────┘  │
                                                              │        │
                                                         ┌────┴────┐   │
                                                        Yes       No   │
                                                         │         │   │
                                                         ▼         ▼   │
                                                      ┌──────┐ ┌──────┐│
                                                      │Query │ │grade_││
                                                      │Rewrit│ │passed││
                                                      │er改写│ │=False││
                                                      │contin│ │break ││
                                                      │ue    │ └──────┘│
                                                      └──┬───┘         │
                                                         │              │
                                                         └──────────────┘
                                                                │
                                                                │
                                                    (回到 aquery_llm)
```

### 流程说明

| 节点 | 说明 |
|------|------|
| **① 关键词提取** | LLM 一次调用完成：复杂度分级(L1/L2/L3)、关键词提取、HYDE 判断、参考文献判断 |
| **② QueryResolver** | 结合最近 3 轮对话历史，消解指代/省略；对序列/多维度问题分解子查询 |
| **③ HyDE** | 对泛化查询生成假设文档，增强向量检索召回 |
| **④ aquery_llm** | LightRAG 内部完成：三路检索 → 合并去重 → **Rerank** → LLM 生成 |
| **⑤ Grader** | LLM 评估 chunks 相关性，0 chunks 或评分不通过触发自纠错 |
| **Neighbor 扩展** | Grader 不通过时，按 `chunk_order_index` 扩展前后相邻 chunk 重新评分 |
| **QueryRewriter** | 分析失败原因（0 chunks / 不相关）并生成改进查询，通过 `continue` 回到 ④ 重试 |
| **重试次数** | `max_retries=1`，即最多 2 次检索（初始 1 次 + 重试 1 次）|

### 旧版字符画的已知问题

旧版流程图（见上方"程序流程"章节）存在以下问题：

1. **Rerank 位置错误**：Rerank 实际发生在 `aquery_llm()` **内部**，旧图画在了循环外部
2. **Neighbor Chunk 扩展后缺少重试判断**：扩展失败后应先检查"还有重试次数?"，旧图直接指向 QueryRewriter
3. **0 chunks 分支缺少重试判断**：旧图直接指向 QueryRewriter，未体现"无重试则直接返回"
4. **箭头交叉混乱**：两条回到 ④ 的路径画得不清晰
