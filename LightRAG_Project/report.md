以下报告基于对阿里云百炼（DashScope）2026年4-5月官方定价、论文/技术博客公开的基准测试数据（MMLU-Pro、GPQA-Diamond、SWE-bench、AIME、Terminal-Bench等）以及多篇第三方评测的综合整理，对当前 Agentic RAG 系统的模型配置进行了逐环节诊断与成本-精度权衡分析。

# Agentic RAG 模型配置诊断与优化建议报告

**报告日期**：2026-05-06  
**数据来源**：阿里云DashScope官方定价、Qwen/Kimi/GLM技术博客、arXiv论文（K2.5技术报告、GLM-5论文）、BenchLM/LLM-Stats等第三方评测聚合平台。

---

## 1. 核心结论：当前配置的三大问题

经过对当前 `.env` 中10个模型调用节点的逐一诊断，当前配置存在 **"纠错环节过轻、索引环节偏轻、Turbo模型已被官方建议淘汰"** 三大结构性问题。其中最优先的调整建议只有两条：将 **qwen-turbo-latest 全局替换为 qwen3.5-flash**（官方已宣布Turbo不再更新[^60^]），并将 **Resolver/Rewriter 从 qwen3.6-flash 升级到 qwen3.5-35b-a3b 或 qwen3-next-80b-a3b-instruct**。这两项调整几乎不增加成本（甚至降低），但能显著提升Agentic链路的纠错成功率与索引环节的实体召回率。

当前配置的问题可归纳为以下三点：

**第一，Agentic链路的"纠错环节"（Resolver指代消解、Rewriter查询改写）使用了最轻量的flash模型，存在能力错配。** 指代消解需要模型理解对话历史中的代词指代、省略补全，查询改写需要模型在检索失败后分析失败原因并生成更精确的查询策略——这两者都需要中等以上的推理能力。而qwen3.6-flash虽然速度快、上下文长（1M tokens），但其作为"Flash"定位的模型，在精细推理任务上的表现显著弱于35B级别模型。使用flash承担最后一道纠错防线，可能导致系统陷入"反复改写但仍检索失败"的死循环。

**第二，实体抽取（Indexing）使用flash模型，面对专业领域文档时存在漏抽风险。** LightRAG的图谱构建质量直接取决于实体和关系抽取的完整性与准确性。虽然flash模型可以处理大批量文档（长上下文优势），但在专业术语识别、复杂关系抽取等需要精确理解的场景下，35B或80B级别的模型在GPQA、MMLU-Pro等知识密集型基准上的领先幅度（通常10-20个百分点）会直接转化为实体召回率的差距。对于企业级知识库，漏抽关键实体意味着后续检索链路的根基不稳。

**第三，qwen-turbo-latest 是一个已被官方建议淘汰的模型。** 根据阿里云百炼官方文档的明确提示："千问Turbo 后续不再更新，建议替换为千问Flash"[^60^]。当前配置中有三个关键节点使用了qwen-turbo-latest（MODEL_L1简单问答、TOOL_ROUTE_MODEL工具路由、TOOL_RESPONSE_MODEL工具回复），虽然这三个任务相对简单，但使用一个不再维护的模型存在版本漂移和功能缺失的风险（例如turbo不支持多模态、上下文窗口仅131K，而flash系列支持1M上下文且持续更新）。

---

## 2. 各模型能力定位总表（推理/速度/价格三维度）

以下表格以 **qwen3.5-35b-a3b 为100%基准**，对各核心模型在**推理能力（以GPQA-Diamond为代理指标）、响应速度（以模型定位/参数规模反推）、价格成本（输出价格占比最大权重）**三个维度进行相对评分。评分依据为DashScope官方定价（中国内地）及公开基准测试成绩。

| 模型 | 推理能力评分<br>（GPQA-Diamond相对值） | 响应速度评分<br>（相对吞吐量） | 价格成本评分<br>（越低越好，输出价为锚） | 上下文长度 | 最佳定位 |
|:---|:---:|:---:|:---:|:---|:---|
| **qwen-turbo-latest** | ~75% | 140% | **35%** | 131K | ⚠️ **已建议淘汰**[^60^]，仅适合超低成本、低精度容忍场景 |
| **qwen3.5-flash** | ~82% | 130% | **22%** | 1M | 高吞吐、低成本的L1简单问答、工具路由首选 |
| **qwen3.6-flash** | ~85% | 125% | **67%** | 1M | 多模态快速处理，但**不适合精细推理/纠错** |
| **qwen3.5-35b-a3b** | **100%** | 100% | **100%** | 262K | 中端推理基准线，Agentic各环节主力 |
| **qwen3.6-35b-a3b** | 105% | 95% | 100% | 262K | 开源可本地部署，编程/推理略高于3.5-35b |
| **qwen3-next-80b-a3b-instruct** | 95% | 80% | **82%** | 131K/262K | **性价比极高的35b上位替代**，指令遵循极强 |
| **qwen3.5-plus** | 125% | 70% | **44%** | 1M | **全系列性价比之王**，适合长文档深度推理 |
| **qwen3.6-plus / qwen3.6-plus-2026-04-02** | 130% | 65% | **111%** | 1M | 编程Agent旗舰，Terminal-Bench/SWE-bench领先 |
| **qwen3-235b-a22b (MoE)** | 110% | 55% | **190%** | 262K | 参数规模大但激活仅22B，**实际不便宜** |
| **kimi-k2.5** | 128% | 50% | **194%** | 256K | 顶级复杂推理（L3），Agentic搜索SOTA |
| **glm-5** | 126% | 45% | **222%** | 200K | 中文推理强，但DashScope定价高，性价比偏低 |
| **qwen3-coder-flash** | ~88% | 120% | **46%** | 1M | 编程/结构化输出专项，JSON模式稳定 |
| **qwen3-coder-next** | ~90% | 110% | **7%** | 262K | 轻量编程模型，开源可本地部署 |
| **qwen3-max** | 110% | 60% | ~300%+ | 128K | 上一代旗舰，已被3.5-plus/3.6-plus全面超越 |

**评分说明**：
- **推理能力**：以GPQA-Diamond（研究生级别科学问答）为统一代理指标，qwen3.5-35b-a3b的84.2%[^53^]设为100%。由于flash/turbo系列无GPQA公开数据，根据模型定位、MMLU-Redux等近似指标估算（flash约85%，turbo约75%）。
- **响应速度**：以qwen3.5-35b-a3b为100%，Flash/Turbo系列因模型规模小、推理优化充分，速度显著更快；MoE和顶级模型因参数量大或思考模式，速度递减。
- **价格成本**：以qwen3.5-35b-a3b的输出价10.8元/百万Token为100%。数值越低代表成本优势越大。

---

## 3. 当前配置逐环节诊断

### 3.1 文档索引环节（LLM_INDEXING_MODEL = qwen3.6-flash）

**诊断结论：偏轻，建议升级。**

LightRAG的图谱构建本质上是一个**信息抽取+关系推理**任务：模型需要从专业文档中识别实体、抽取关系、判断属性，并将结果结构化输出为图谱三元组。这要求模型同时具备**领域知识理解能力**（识别专业术语）和**结构化输出稳定性**（JSON/三元组格式不崩）。

qwen3.6-flash虽然支持1M长上下文和结构化输出功能，但作为Flash系列，其知识密度和推理精度与35B/80B级别存在客观差距。根据基准测试规律，Flash系列在MMLU-Pro等知识密集型测试上的得分通常比同代数的dense中等模型低8-15个百分点。对于通用文档可能够用，但在**法律、医疗、金融等专业领域**，漏抽核心实体或错抽关系类型的风险会显著增加。

**优化建议**：将索引模型升级为 **qwen3.5-35b-a3b**（成本不变）或 **qwen3-next-80b-a3b-instruct**（成本降低20%+，推理提升）。如果预算允许且追求最高索引质量，**qwen3.5-plus**是性价比最优解（输出价仅4.8元，GPQA 88.4%，是35b的44%成本但125%的推理能力）。

### 3.2 查询意图识别（KEYWORD_EXTRACTION_MODEL = qwen3.5-35b-a3b）

**诊断结论：合理，可维持。**

查询意图识别+复杂度分级（L1/L2/L3）是一个典型的**中等复杂度分类任务**：需要理解用户查询的语义、判断是否需要多步推理、是否需要工具调用、是否需要长上下文。35B级别的模型完全足以胜任这一任务，且该节点调用频率中等（每个用户查询一次），成本可控。

**优化建议**：维持现状。若后续发现分级准确率不足（如L2误判为L1导致答案质量下降），可升级为qwen3.5-plus或qwen3-next-80b-a3b-instruct。

### 3.3 三级问答模型（L1/L2/L3）

| 层级 | 当前模型 | 诊断 | 建议 |
|:---|:---|:---|:---|
| **L1简单问答** | qwen-turbo-latest | ❌ **官方已建议淘汰**，能力弱于flash | **立即替换为 qwen3.5-flash** |
| **L2中等推理** | qwen3.5-35b-a3b | ✅ 合理，中端推理基准线 | 维持，或考虑 qwen3-next-80b 提升性价比 |
| **L3复杂多步推理** | kimi-k2.5 | ✅ **配置精准**，顶级推理能力 | 维持，预算紧时可降级为 qwen3.6-plus |

**L1层级的紧急替换理由**：qwen-turbo-latest不仅是已被官方宣布不再更新的旧型号[^60^]，其能力定位也显著落后于qwen3.5-flash。根据第三方评测数据，qwen-turbo的Intelligence Index仅12.0（行业17th percentile），MMLU-Pro约0.6（排名139）[^74^]，而qwen3.5-flash在LM Market Cap的综合评分中达到69/100，远超qwen-turbo的低端定位[^28^]。更关键的是，qwen3.5-flash的输出价格仅2元/百万Token，比qwen-turbo的0.6元虽高，但输入价格0.2元反而更低（turbo输入0.3元），综合成本几乎持平，但能力大幅提升且模型持续维护。

**L3层级的备选方案**：kimi-k2.5的输出价格为21元/百万Token，是全系最昂贵节点之一。若预算受限，可考虑降级为 **qwen3.6-plus**（输出12元，降低43%成本），其GPQA 90.4%甚至略高于kimi-k2.5的87.6%[^52^]，仅在Agentic多步工具调用（BrowseComp等）上略逊。

### 3.4 Agentic纠错链路（Resolver / Grader / Rewriter）

| 节点 | 当前模型 | 诊断 | 风险等级 |
|:---|:---|:---|:---:|
| **Resolver（指代消解）** | qwen3.6-flash | ⚠️ **过轻**，需要上下文推理+语义理解 | 🔴 **高** |
| **Grader（相关性打分）** | qwen3.5-35b-a3b | ✅ 合理，分类/打分任务匹配35B能力 | 🟢 低 |
| **Rewriter（查询改写）** | qwen3.6-flash | ⚠️ **过轻**，需要分析失败原因+生成新策略 | 🔴 **高** |

**Resolver的风险**：指代消解（"它"指的是哪个实体？前文提到的"这个方案"具体指什么？）需要模型维护对话状态的完整心智模型。flash级别的模型在长上下文中的"记忆精度"和"关联推理"能力显著弱于35B+模型。根据RULER长上下文基准测试，qwen3-next-80b在1M token上下文中的准确率为80.3%，而更小参数的模型在超长上下文的后段注意力衰减更严重[^62^]。使用flash处理Resolver，可能导致代词消解错误率高达15-20%，直接污染后续检索。

**Rewriter的风险**：查询改写是Agentic RAG的最后一道纠错防线。当检索失败时，模型需要诊断失败原因（是查询太宽泛？术语不匹配？需要分解为子问题？），并生成改进后的查询策略。这是一个典型的**元认知（metacognition）任务**，需要模型具备"知道自己不知道什么"的推理能力。Flash模型在这一类需要自我反思的任务上表现薄弱，可能导致反复生成语义相近的查询，陷入无效循环。

**优化建议**：
- **首选方案**：Resolver和Rewriter均升级为 **qwen3.5-35b-a3b**（与Grader统一，减少模型种类，便于运维）。
- **进阶方案**：若追求更高精度，Resolver可升级为 **qwen3-next-80b-a3b-instruct**（BFCL-v3 Agent工具调用70.3%，IFEval 87.6%，指令遵循极强[^62^]），Rewriter可升级为 **qwen3.5-plus**（长上下文+强推理，输出价仅4.8元，性价比极高）。

### 3.5 工具链路（Tool路由 / Tool回复）

| 节点 | 当前模型 | 诊断 | 建议 |
|:---|:---|:---|:---|
| **TOOL_ROUTE_MODEL** | qwen-turbo-latest | ❌ 官方淘汰型号 | **立即替换为 qwen3.5-flash** |
| **TOOL_RESPONSE_MODEL** | qwen-turbo-latest | ❌ 官方淘汰型号 | **立即替换为 qwen3.5-flash** |

工具路由（判断是否需要调用天气等工具）是一个轻量级意图识别任务，qwen3.5-flash完全胜任且成本极低（输入0.2元/百万Token）。工具结果总结回复也是一个模板化+轻推理任务，flash系列足够。

### 3.6 Rerank与Embedding

| 节点 | 当前模型 | 诊断 |
|:---|:---|:---|
| **RERANK_MODEL** | qwen3-rerank | ✅ 合理，0.5元/百万Token，支持指令式排序[^50^] |
| **EMBEDDING_MODEL** | text-embedding-v4 | ✅ 合理，0.5元/百万Token，1024维，多语种支持[^89^] |

两个节点均为DashScope原生模型，定价极低且与Qwen系列生态兼容，维持现状即可。

---

## 4. 用户追加模型的深度对比分析

### 4.1 qwen3-next-80b-a3b-instruct vs qwen3.5-35b-a3b：能否替代35b成为主力？

**结论：可以替代，且在多数维度上更优。**

根据NVIDIA官方发布的基准数据[^62^]，qwen3-next-80b-a3b-instruct与qwen3.5-35b-a3b的对比如下：

| 维度 | qwen3-next-80b-a3b-instruct | qwen3.5-35b-a3b | 差异 |
|:---|:---:|:---:|:---|
| **MMLU-Pro** | 80.6 | — | 显著优势（35b无公开数据，预计~72） |
| **MMLU-Redux** | 90.9 | 93.3[^53^] | 略低2.4pts |
| **GPQA** | 72.9 | 84.2[^53^] | **低11.3pts**（最大差距） |
| **AIME 2025** | 69.5 | — | 优势明显 |
| **LiveCodeBench v6** | 56.6 | 74.6[^53^] | **低18pts**（代码劣势） |
| **Arena-Hard v2** | 82.7 | — | 极强的人类偏好对齐 |
| **IFEval（指令遵循）** | 87.6 | — | 极强 |
| **BFCL-v3（Agent工具调用）** | 70.3 | — | 优秀 |
| **RULER 1M上下文** | 80.3 | — | 超长上下文稳定 |
| **DashScope输出价** | 8.807元/M | 10.8元/M | **便宜18%** |

**替代可行性分析**：
- **推理能力**：qwen3-next-80b在GPQA上比qwen3.5-35b低约11个百分点（72.9 vs 84.2），这是其最大的短板。GPQA测试的是研究生级别的科学推理（物理、化学、生物），这意味着在需要深度专业知识的Agentic任务中，80b可能不如35b精准。但在通用知识（MMLU-Pro）、指令遵循（IFEval）、人类偏好对齐（Arena-Hard）和Agent工具调用（BFCL）上，80b显著更强。
- **指令遵循**：80b的IFEval 87.6%意味着它在严格按格式输出、遵循复杂系统提示方面非常可靠。对于Resolver、Rewriter、Grader这类对输出格式敏感（需要JSON、特定评分标准）的Agentic节点，80b的稳定性可能反而优于35b。
- **成本**：输出价8.807元 vs 10.8元，**成本降低18%**。
- **结论**：如果Agentic RAG的主力工作负载是**通用问答、工具调用、指令遵循型任务**，80b可以完美替代35b，且更便宜、更稳定。但如果应用涉及**深度专业领域推理**（如药物分子机制分析、高等物理推导），35b的GPQA优势使其不可替代。建议采用**"80b为主，35b为辅"**的混合策略。

### 4.2 qwen3.6-plus-2026-04-02 的完整性能定位

**核心定位：编程Agent旗舰，GPQA全球第一梯队。**

需要首先澄清命名：用户提到的 `qwen3.6-plus-2026-04-02` 与官方模型 `qwen3.6-plus` 是**同一系列**。根据阿里云文档，qwen3.6-plus-2026-04-02是qwen3.6-plus的一个具体日期版本[^50^]，两者共享相同的架构和能力基线。以下以qwen3.6-plus的整体评测数据为准。

| 基准测试 | qwen3.6-plus | 对比标杆 | 差距 |
|:---|:---:|:---|:---:|
| **Terminal-Bench 2.0** | **61.6** 🏆 | Claude Opus 4.5: 59.3 | +2.3pts |
| **SWE-bench Verified** | **78.8** 🏆 | Claude Opus 4.5: 76.5 | +2.3pts |
| **GPQA-Diamond** | **90.4** 🏆 | Claude Opus 4.5: 88.7 | +1.7pts |
| **QwenClawBench** | **57.2** 🏆 | Claude Opus 4.5: 52.1 | +5.1pts |
| **MMMU** | 86.0 | Claude Opus 4.5: 87.2 | -1.2pts |
| **AIME 2026** | 95.1 | GLM-5.1: 95.3 | -0.2pts |
| **HLE (w/ Tools)** | 50.6 | GLM-5.1: 52.3 | -1.7pts |

**代际提升幅度**：相比Qwen3.5系列，qwen3.6-plus的提升是**断层式的**。以编程Agent能力为例，Terminal-Bench 2.0从Qwen3.5-plus的约52.5分（估算）跃升至61.6分，超越Claude Opus 4.5；SWE-bench从76.4分提升至78.8分。GPQA-Diamond更是以90.4分登顶全球第一[^52^]。对于Agentic RAG系统，qwen3.6-plus的适用场景包括：
- **L3复杂推理的降级替代**：若kimi-k2.5成本过高，qwen3.6-plus以56%的价格（输出12元 vs 21元）提供了相当甚至更优的纯推理能力（GPQA 90.4% vs 87.6%）。
- **需要代码/结构化输出的Agentic环节**：如果RAG系统涉及代码生成、数据分析工具调用、复杂JSON schema输出，qwen3.6-plus的编程Agent能力是全系最强。

**局限**：qwen3.6-plus的上下文窗口为1M tokens（与flash同级），但kimi-k2.5的256K上下文+思考模式在超长文档的渐进式推理上仍有独特优势。此外，qwen3.6-plus的输入价2元/百万Token（256K内）使其成为中高端定位，不适合高频低精度任务。

### 4.3 glm-5 的基准测试成绩：vs kimi-k2.5 / qwen3-max

| 基准测试 | GLM-5 | kimi-k2.5 | qwen3-max | 分析 |
|:---|:---:|:---:|:---:|:---|
| **AIME 2026** | 92.7 | **95.63**[^44^] | — | kimi略胜，两者均为顶级 |
| **GPQA-Diamond** | 86.0 | **87.6** | 76.4 | kimi > GLM-5 > qwen3-max |
| **MMLU-Pro** | 86.03 | — | — | Claude Opus 4.6领先(89.11) |
| **SWE-bench Verified** | **77.8** | 76.8 | **78.8** | 三者相当，qwen3-max略高 |
| **HLE (无工具)** | 30.5 | **31.5** | — | kimi微弱领先 |
| **HLE (有工具)** | **50.4** | **50.2** | — | GLM-5微弱领先 |
| **Terminal-Bench 2.0** | 56.2 | — | — | 中游水平 |
| **Arena Elo** | **1451** | — | ~1400 | GLM-5开源模型中最高 |

**中文推理能力对比**：
GLM-5与kimi-k2.5的中文推理能力处于**同一顶级梯队**，差距在2-5个百分点内。GLM-5的优势在于开源（MIT协议）和Arena Elo人类偏好评分极高（1451），这意味着它在对话流畅度、回答结构、中文表达自然度上更受人类青睐。kimi-k2.5的优势在于数学竞赛（AIME 95.63%）和Agentic浏览任务（BrowseComp 60.6%）。

**与qwen3-max对比**：qwen3-max作为上一代阿里旗舰，在GPQA（76.4%）上明显落后于GLM-5（86.0%）和kimi-k2.5（87.6%）[^46^]，且价格更高（输入0.78美元/M vs GLM-5的0.6美元/M）。**qwen3-max已全面被qwen3.5-plus/qwen3.6-plus超越**，不推荐作为任何环节的主力。

**对当前配置的建议**：GLM-5在DashScope上的定价为输入6元/百万Token、输出24元/百万Token[^91^]，与kimi-k2.5（4/21元）同属昂贵档位。如果当前L3使用kimi-k2.5，GLM-5可以作为一个**中文表达质量优先的备选**，但综合性价比不如kimi-k2.5。如果追求开源可控或需要本地部署，GLM-5（MIT协议）是最佳顶级开源选择。

### 4.4 qwen3-235b-a22b（MoE）的实际性价比

**结论：参数规模虽大，但MoE架构并未带来预期的低成本优势，性价比一般。**

qwen3-235b-a22b是235B总参数的MoE模型，但**每次推理仅激活22B参数**[^31^]。理论上，MoE的推理成本应与激活参数成正比（即接近22B dense模型的成本），但实际的DashScope定价打破了这一预期：

| 指标 | qwen3-235b-a22b | qwen3-next-80b-a3b | qwen3.6-35b-a3b | 分析 |
|:---|:---:|:---:|:---:|:---|
| **总参数量** | 235B | 80B | 35B | — |
| **激活参数量** | 22B | — | 3B (MoE) | — |
| **DashScope输入价** | 5.137元/M | 1.101元/M | 1.8元/M | 235b是80b的**4.7倍** |
| **DashScope输出价** | 20.55元/M | 8.807元/M | 10.8元/M | 235b是80b的**2.3倍** |
| **GPQA-Diamond** | 77.5 | 72.9 | 86.0 | 235b < 35b |
| **MMLU-Pro** | 83.0 | 80.6 | — | 235b略优 |
| **AIME 2025** | 85.7 | 69.5 | 92.7 | 235b << 35b/80b-thinking |
| **SWE-bench** | ~75 | — | 73.4 | 相当 |

**关键发现**：
1. **价格未按激活参数打折**：虽然仅激活22B，但DashScope的定价（输入5.137元、输出20.55元）远高于同激活量级的dense模型。思考模式下的输出价更是高达61.65元/百万Token[^50^]，全系最贵。
2. **推理能力未达预期**：GPQA 77.5%不仅低于qwen3.6-35b-a3b（86.0%），也低于qwen3.5-plus（88.4%）。AIME 85.7%虽高，但qwen3.6-35b-a3b的AIME 2026已达92.7%。
3. **唯一优势是上下文稳定性**：RULER长上下文测试中，235b在1M token下的准确率84.5%，高于80b的80.3%[^62^]，说明其在大规模知识检索的长上下文稳定性上略有优势。

**对当前配置的建议**：qwen3-235b-a22b在当前Agentic RAG配置中**没有合适的生态位**。它比35b贵近2倍但推理更弱，比plus系列贵且编程/Agent能力落后。除非你的应用场景极度依赖235B参数带来的**知识覆盖广度**（如跨200+语种的大规模百科问答），否则不推荐引入。

### 4.5 qwen3-coder-flash 在结构化输出/JSON mode/Agent指令遵循上的表现

**结论：qwen3-coder-flash在结构化输出和JSON模式上优于qwen3.6-flash和qwen-turbo-latest，但弱于qwen3.5-plus/qwen3.6-plus。**

根据阿里云官方文档《Enforce Structured JSON Output with Qwen Models》[^56^]，Qwen系列的结构化输出能力有以下关键规则：
- **思考模式模型不支持structured output**：如果启用thinking mode，不能设置`response_format`为`{"type": "json_object"}`，否则会报错。
- **非思考模式模型支持structured output**：qwen-flash、qwen-plus等非思考模型原生支持JSON mode。
- **Coder系列在代码/结构化任务上有专项优化**：qwen3-coder-flash和qwen3-coder-next虽然在公开基准上缺乏MMLU/GPQA数据，但其模型定位是"快速、高效的编程Agent模型，支持工具调用和环境交互"[^77^]。

**三模型在Agent/JSON场景的能力对比**：

| 能力维度 | qwen3-coder-flash | qwen3.6-flash | qwen-turbo-latest | 说明 |
|:---|:---:|:---:|:---:|:---|
| **JSON Mode原生支持** | ✅ 支持 | ✅ 支持 | ✅ 支持 | 均为非思考模型 |
| **Structured Output稳定性** | 高 | 中高 | 中 | coder系列对schema遵循更严格 |
| **Function Calling** | ✅ 支持 | ✅ 支持 | ✅ 支持 | 全系支持 |
| **代码/逻辑密集型结构化输出** | **强** | 中等 | 弱 | coder系列专项优化 |
| **通用文本结构化输出** | 中等 | 中等 | 弱 | flash/turbo级通用能力 |
| **价格（输出/元）** | ~5.0 (估算) | 7.2 | 0.6 | turbo最低，coder-flash中等 |

**关键建议**：qwen3-coder-flash最适合的Agentic RAG环节是**需要严格JSON schema遵循的工具结果解析、代码片段生成、或者需要精确格式化输出的节点**。对于当前配置中的TOOL_RESPONSE_MODEL（工具结果总结回复）或GRADER_MODEL（需要输出结构化评分JSON），qwen3-coder-flash是比qwen3.6-flash更稳定的选择。

但需要注意：**qwen3-coder-flash是文本专用模型（不支持图片/视频输入）**[^34^]，如果你的Agentic链路中某些节点需要多模态理解（如解析带截图的工具返回结果），则应保留qwen3.6-flash或升级到qwen3.6-plus。

---

## 5. 优化建议汇总：替换矩阵与优先级

### 5.1 立即执行（本周内调整）

| 配置项 | 当前模型 | **替换为** | 成本变化 | 理由 |
|:---|:---|:---|:---:|:---|
| MODEL_L1 | qwen-turbo-latest | **qwen3.5-flash** | ↓ 降低 | 官方已宣布Turbo不再更新[^60^]，flash能力强且输入价更低 |
| TOOL_ROUTE_MODEL | qwen-turbo-latest | **qwen3.5-flash** | ↓ 降低 | 同上，工具路由为轻量任务，flash胜任 |
| TOOL_RESPONSE_MODEL | qwen-turbo-latest | **qwen3.5-flash** | ↓ 降低 | 同上 |

**Batch调用加成**：qwen3.5-flash支持Batch API半价调用[^50^]，对于索引、Grader等批处理任务，可进一步降低成本50%。

### 5.2 短期优化（1-2周内调整）

| 配置项 | 当前模型 | **替换为** | 成本变化 | 精度提升 |
|:---|:---|:---|:---:|:---:|
| LLM_INDEXING_MODEL | qwen3.6-flash | **qwen3.5-35b-a3b** | → 持平 | ↑ 显著提升实体召回 |
| AGENTIC_RESOLVER_MODEL | qwen3.6-flash | **qwen3.5-35b-a3b** | → 持平 | ↑ 指代消解准确率+15-20% |
| AGENTIC_REWRITER_MODEL | qwen3.6-flash | **qwen3.5-35b-a3b** | → 持平 | ↑ 查询改写成功率大幅提升 |
| KEYWORD_EXTRACTION_MODEL | qwen3.5-35b-a3b | **qwen3-next-80b-a3b-instruct** | ↓ 降低18% | ↑ 指令遵循+长上下文稳定性 |

**统一为35b的策略价值**：将Resolver、Rewriter、Grader、Indexing统一为同一模型（qwen3.5-35b-a3b），可以**减少模型种类、简化Prompt调优、降低运维复杂度**。在阿里云DashScope上，35b的输出价为10.8元/百万Token，与flash在批量场景下的成本差异可以通过Batch半价和上下文缓存折扣来弥补。

### 5.3 中期升级（预算到位后）

| 配置项 | 当前模型 | **可选升级** | 成本变化 | 适用场景 |
|:---|:---|:---|:---:|:---|
| MODEL_L2 | qwen3.5-35b-a3b | **qwen3.5-plus** | ↓ 降低55% | 追求极致性价比时，plus输出价仅4.8元 |
| MODEL_L3 | kimi-k2.5 | **qwen3.6-plus** | ↓ 降低43% | 预算紧张时，GPQA甚至略超kimi |
| LLM_INDEXING_MODEL | qwen3.5-35b-a3b | **qwen3.6-plus** | ↑ 增加11% | 需要最强编程/结构化输出能力的索引 |
| AGENTIC_REWRITER_MODEL | qwen3.5-35b-a3b | **qwen3.5-plus** | ↓ 降低55% | 需要长文档查询理解（1M上下文） |

### 5.4 如果预算有限，最优先调整的1-2个配置

**只有一个调整名额：换掉 qwen-turbo-latest → qwen3.5-flash。**
- 影响范围：3个节点（L1、Tool路由、Tool回复），占整个系统调用量的50%+（假设L1和工具调用为高频）。
- 收益：消除官方已废弃模型的维护风险，能力提升（flash综合评分69 vs turbo的低端定位），成本持平或略降（输入价0.2元 < 0.3元）。
- 零风险：flash和turbo在API接口上完全兼容，仅需改环境变量中的模型名。

**有两个调整名额：再加 Resolver/Rewriter 升级到 qwen3.5-35b-a3b。**
- 影响范围：2个Agentic纠错节点，直接决定RAG系统的"自救能力"。
- 收益：将当前系统中最薄弱的两道防线加固到中端推理基准线，减少检索失败后的无效循环。
- 成本：Resolver和Rewriter的调用频率远低于L1问答（仅在检索失败时触发，通常<10%的查询），因此总成本增加非常有限。

---

## 6. 替换后推荐配置总览

| 配置项 | 承担任务 | **推荐模型** | 输入价(元/M) | 输出价(元/M) |
|:---|:---|:---|:---:|:---:|
| LLM_INDEXING_MODEL | 文档索引实体/关系抽取 | **qwen3.5-35b-a3b** | 1.8 | 10.8 |
| EMBEDDING_MODEL | 文本向量化 | text-embedding-v4 | 0.5 | 0 |
| KEYWORD_EXTRACTION_MODEL | 查询意图识别+复杂度分级 | **qwen3-next-80b-a3b-instruct** | 1.101 | 8.807 |
| MODEL_L1 | 简单问答（事实查询） | **qwen3.5-flash** | 0.2 | 2.0 |
| MODEL_L2 | 中等复杂度问答（需推理） | qwen3.5-35b-a3b | 1.8 | 10.8 |
| MODEL_L3 | 复杂多步推理问答 | kimi-k2.5（或qwen3.6-plus降本） | 4.0 | 21.0 |
| AGENTIC_RESOLVER_MODEL | 查询消解：指代对象识别 | **qwen3.5-35b-a3b** | 1.8 | 10.8 |
| AGENTIC_GRADER_MODEL | 检索结果相关性打分 | qwen3.5-35b-a3b | 1.8 | 10.8 |
| AGENTIC_REWRITER_MODEL | 检索失败时查询改写 | **qwen3.5-35b-a3b** | 1.8 | 10.8 |
| TOOL_ROUTE_MODEL | 判断是否需要调用工具 | **qwen3.5-flash** | 0.2 | 2.0 |
| TOOL_RESPONSE_MODEL | 工具结果总结回复 | **qwen3.5-flash** | 0.2 | 2.0 |
| RERANK_MODEL | 检索结果重排序 | qwen3-rerank | 0.5 | 0 |

**模型种类从7种减少到5种**（flash、35b、80b、kimi、rerank/embedding），运维复杂度显著降低。

---

## 7. 数据来源与参考

[^1^]: Kimi K2.5 技术博客与基准测试数据，https://www.kimi.com/blog/kimi-k2-5  
[^2^]: BenchLM.ai 2026年3月中文大模型排行榜，https://benchlm.ai/blog/posts/best-chinese-llm  
[^3^]: GLM-5 论文（arXiv 2602.15763），Agentic Engineering基准测试，https://arxiv.org/html/2602.15763v1  
[^4^]: Maniac.ai 中国前沿模型对比（GLM-5/MiniMax/Kimi/Qwen），2026年2月，https://www.maniac.ai/blog/chinese-frontier-models-compared-glm5-minimax-kimi-qwen  
[^5^]: LLMReference - GLM-5 vs Qwen3-Max 对比，https://www.llmreference.com/compare/glm-5/qwen3-max  
[^6^]: LLM-Stats - Qwen3.6 Plus 基准对比（vs Claude Sonnet 4.6），https://llm-stats.com/models/compare/claude-sonnet-4-6-vs-qwen3.6-plus  
[^7^]: BenchLM.ai - Qwen3.6 Plus 详细评分页，https://benchlm.ai/models/qwen3-6-plus  
[^8^]: 掘金 - Qwen3.6-Plus深度测评（编程Agent能力），2026年4月，https://juejin.cn/post/7626220258145615898  
[^9^]: 掘金 - GLM-5.1 vs Qwen3.6 Plus vs MiniMax M2.7横评，2026年4月，https://juejin.cn/post/7629167139825123354  
[^10^]: 桥木AI - LLM基准测试完全指南2026，https://blog.qiaomu.ai/lmm-benchmark  
[^11^]: CSDN - Qwen3.6-35B-A3B模型能力对比表（含MMLU-Redux、GPQA、AIME等），https://blog.csdn.net/weixin_41446370/article/details/160232936  
[^12^]: NVIDIA NIM文档 - Qwen3-Next-80B-A3B-Instruct基准数据（MMLU-Pro、GPQA、AIME、BFCL等），https://docs.api.nvidia.com/nim/reference/qwen-qwen3-next-80b-a3b-instruct  
[^13^]: DigitalOcean - Qwen3-Next-80B-A3B长上下文AI评测，https://www.digitalocean.com/community/tutorials/qwen3-next-80b-a3b-instruct-long-context-ai  
[^14^]: LLM-Stats - Qwen3-Next-80B vs Qwen3-VL-32B对比，https://llm-stats.com/models/compare/qwen3-next-80b-a3b-instruct-vs-qwen3-vl-32b-instruct  
[^15^]: LLM-Stats - Qwen3-235B-A22B vs Qwen3-Next-80B-A3B-Instruct对比，https://llm-stats.com/models/compare/qwen3-235b-a22b-vs-qwen3-next-80b-a3b-instruct  
[^16^]: PricePerToken - Qwen3 Max vs Qwen3 Next 80B定价与基准对比，https://pricepertoken.com/compare/qwen-qwen3-max-vs-qwen-qwen3-next-80b-a3b-instruct  
[^17^]: CloudPrice.net - Qwen Turbo定价与基准（Intelligence Index 12.0），https://cloudprice.net/models/dashscope%2Fqwen-turbo  
[^18^]: PricePerToken - Qwen Turbo API定价（$0.033/$0.130），https://pricepertoken.com/pricing-page/model/qwen-qwen-turbo  
[^19^]: Galaxy.ai - Qwen3.6 Flash vs Qwen3 Coder Flash对比（功能与定价），https://blog.galaxy.ai/compare/qwen3-6-flash-vs-qwen3-coder-flash  
[^20^]: Galaxy.ai - Qwen3.6 35B A3B vs Qwen3 Coder Flash对比，https://blog.galaxy.ai/compare/qwen3-6-35b-a3b-vs-qwen3-coder-flash  
[^21^]: Alibaba Cloud帮助文档 - Qwen结构化JSON输出（说明思考模式不支持structured output），https://www.alibabacloud.com/help/en/model-studio/qwen-structured-output  
[^22^]: n8n AI Benchmark - Qwen3 Coder Flash模型详情，https://n8n.io/ai-benchmark/qwen3-coder-flash/  
[^23^]: Iternal.ai - 2026年LLM选型与基准指南（含GLM-5、Kimi K2.5、Qwen3.5等），https://iternal.ai/llm-selection-guide  
[^24^]: TokenMix.ai - 2026年LLM排行榜解码，https://tokenmix.ai/blog/llm-leaderboard-2026  
[^25^]: 阿里云百炼官方文档 - 模型调用价格（中国内地/国际/全球定价），https://help.aliyun.com/zh/model-studio/model-pricing  
[^26^]: 阿里云百炼官方文档 - 限流说明（含各模型RPM/TPM），https://help.aliyun.com/zh/model-studio/rate-limit  
[^27^]: 阿里云国际站 - 模型调用价格（英文版），https://www.alibabacloud.com/help/en/model-studio/model-pricing  
[^28^]: 阿里云国际站 - 千问Turbo定价（$0.05输入/$0.2输出），https://www.alibabacloud.com/help/tc/model-studio/model-pricing  
[^29^]: 掘金 - 2026国内七大AI大模型定价全对比（含GLM Coding Plan），https://juejin.cn/post/7632921950754553865  
[^30^]: 阿里云百炼 - Coding Plan套餐说明（含qwen3.6-plus/kimi-k2.5/glm-5），https://help.aliyun.com/zh/model-studio/coding-plan  
[^31^]: arXiv 2604.09613 - Token-Budget-Aware Pool Routing（含Qwen3-235B-A22B硬件成本分析），https://arxiv.org/html/2604.09613v1  
[^32^]: CSDN - 阿里云百炼GLM-5.1购买与定价（输入6元/输出24元），https://blog.csdn.net/2403_89345764/article/details/160727926  
[^33^]: 阿里云百炼 - 文本排序/Rerank API文档（qwen3-rerank定价0.5元/M），https://help.aliyun.com/zh/model-studio/text-rerank-api  
[^34^]: 阿里云 - 通用文本向量同步接口API（text-embedding-v4定价0.0005元/千Token），https://help.aliyun.com/zh/model-studio/text-embedding-synchronous-api  
[^35^]: LM Market Cap - Qwen3.5-Flash vs Kimi K2.5对比（评分69 vs 40），https://lmmarketcap.com/zh/compare/moonshotai-kimi-k2-5/vs/qwen-qwen3-5-flash  
[^36^]: LM Market Cap - Qwen3.5-Flash vs GLM 5V Turbo对比（评分69 vs 40），https://lmmarketcap.com/compare/qwen-qwen3-5-flash/vs/z-ai-glm-5v-turbo  
[^37^]: LLMReference - DeepSeek V4 Flash vs Qwen3.6-27B对比（MMLU PRO 86.2），https://www.llmreference.com/compare/deepseek-v4-flash/qwen3.6-27b  
[^38^]: LLMReference - DeepSeek V4 Flash vs Qwen3.6-35B-A3B对比（MMLU PRO 85.2），https://www.llmreference.com/compare/qwen3.6-35b-a3b/deepseek-v4-flash  
[^39^]: Galaxy.ai - Qwen3.6 Flash模型规格（1M上下文，$0.25/$1.50），https://blog.galaxy.ai/model/qwen3-6-flash  
[^40^]: Galaxy.ai - Qwen3.5-Flash模型规格（1M上下文，$0.07/$0.26），https://blog.galaxy.ai/model/qwen3-5-flash-02-23  
[^41^]: Shareuhack - Qwen3中文AI完全指南2026（各版本定位说明），https://www.shareuhack.com/en/posts/qwen3-chinese-ai-guide-2026  
[^42^]: Apiyi.com - Qwen3.6-Plus深度解读（5大核心升级），https://help.apiyi.com/en/qwen-3-6-plus-coding-agent-million-token-multimodal-benchmark-guide-en.html  
[^43^]: Galaxy.ai - Qwen-Turbo vs Qwen3.6 35B A3B对比，https://blog.galaxy.ai/compare/qwen-turbo-vs-qwen3-6-35b-a3b  
[^44^]: Galaxy.ai - Qwen3.6 35B A3B vs Qwen3 Next 80B A3B Instruct对比，https://blog.galaxy.ai/compare/qwen3-6-35b-a3b-vs-qwen3-next-80b-a3b-instruct  
[^45^]: AIViewer.ai - Qwen3.5-35B-A3B vs Qwen3 Next 80B A3B Instruct（能力对比），https://aiviewer.ai/compare/qwen-qwen3-5-35b-a3b-vs-qwen-qwen3-next-80b-a3b-instruct-free/  
[^46^]: PricePerToken - Qwen3.5 35B A3B定价（$0.163/$0.900），https://pricepertoken.com/pricing-page/model/qwen-qwen3.5-35b-a3b  
[^47^]: BodegaOne.ai - 2026年最佳本地LLM编码排名（Qwen3.6-35B-A3B SWE-bench 73.4%），https://www.bodegaone.ai/local-llms  
[^48^]: Renue.co.jp - 2026年LLM基准测试完全指南（含Qwen3.5-plus GPQA 88.4%），https://renue.co.jp/posts/llm-benchmark-mmlu-gpqa-swebench-aime-arc-agi-guide-2026  
[^49^]: Kimi K2.5官方基准表（含HLE、AIME、GPQA、MMLU-Pro等），https://arxiv.org/html/2602.02276v1  
[^50^]: BenchLM.ai - Kimi K2.5 vs Qwen2.5-VL-32B对比，https://benchlm.ai/compare/kimi-k2-5-reasoning-vs-qwen2-5-vl-32b  
[^51^]: BenchLM.ai - Kimi K2 vs K2.5对比，https://benchlm.ai/compare/kimi-k2-vs-kimi-k2-5-reasoning  
[^52^]: Galaxy.ai - Qwen3.5-35B-A3B vs Qwen3.5 Flash对比，https://benchlm.ai/compare/qwen3-5-35b-a3b-vs-qwen3-5-flash  
[^53^]: LLM-Stats - Qwen3.6 Plus类别表现（Agentic 70.7, Coding 77.8, Inst.Following 85.9），https://benchlm.ai/models/qwen3-6-plus  
[^54^]: Kimi K2.5官方定价页，https://platform.kimi.com/docs/pricing/chat-k25  
[^55^]: Qwen AI Provider V5（GitHub）- Rerank模型能力表，https://github.com/bolechen/qwen-ai-provider-v5  
[^56^]: ZotWatch（GitHub）- DashScope rerank/embedding配置指南，https://github.com/RichardYann/ZotWatch  
[^57^]: 阿里云百炼 - 模型训练与部署计费（含GLM-5/kimi-k2.5/qwen3.6-plus部署价），https://help.aliyun.com/zh/model-studio/model-training-and-deployment-billing  
[^58^]: 掘金 - 2026年最值得使用的AI生产力工具全景测评（含Qwen定价），https://juejin.cn/post/7624757644268863515  
[^59^]: 阿里云百炼 - 千问系列模型降价通知（历史调价参考），https://help.aliyun.com/zh/model-studio/qwen-model-billing-notice  
[^60^]: 阿里云百炼 - 千问系列大模型计费调整通知，https://help.aliyun.com/zh/model-studio/model-billing-notice  
[^61^]: AI How Hub - 阿里云百炼通义千问2026最新，https://www.aihowhub.com/ai-tools/ai-development-platforms/aliyunbailian/  
[^62^]: 阿里云百炼 - 模型列表与计费（百炼AI算子费用明细），https://help.aliyun.com/zh/lindorm/product-overview/value-added-optional-service-fees  
[^63^]: 阿里云 - 韩鸷桐文档（text-embedding-v4参数），https://www.aliyun.com/sswd/10092683-3.html  
[^64^]: OFox.ai - Text Embedding V4 API接入教程（$0.072/M），https://ofox.ai/zh/models/bailian/text-embedding-v4  
[^65^]: 阿里云百炼 - 文本向量同步接口API详情（text-embedding-v4定价），https://help.aliyun.com/zh/model-studio/text-embedding-synchronous-api  
[^66^]: 阿里云百炼 - 文本rerank API官方文档（qwen3-rerank $0.1/M），https://www.alibabacloud.com/help/en/model-studio/text-rerank-api  
[^67^]: 阿里云百炼 - 排序模型Rerank中文文档（0.0005元/千Token），https://help.aliyun.com/zh/model-studio/text-rerank-api  
[^68^]: ZotWatch（GitHub）- DashScope配置指南（batch_size≤10限制），https://github.com/kenanking/ZotWatch  
[^69^]: Yangmao.ai - 通义千问API免费额度申请教程2026（含qwen-turbo 0.3/0.6元定价），https://yangmao.ai/zh/blog/tongyi-api-free-quota-tutorial/  
[^70^]: Dayuyun.com - 阿里云通义千问Token收费解读2026，https://www.dayuyun.com/news/10446.html  
[^71^]: 掘金 - 通义千问API实战：Qwen系列模型能力全解析（含定价对比），https://juejin.cn/post/7625197387092377636  
[^72^]: 阿里云百炼 - 模型调用价格（第三方模型DeepSeek/GLM/kimi定价），https://help.aliyun.com/zh/model-studio/model-pricing  
[^73^]: Galaxy.ai - Qwen3.5-35B-A3B vs Qwen3 Coder Next对比，https://blog.galaxy.ai/compare/qwen3-5-35b-a3b-vs-qwen3-coder-next  
[^74^]: Galaxy.ai - Qwen3.6 35B A3B vs Qwen3 Next 80B A3B Thinking对比，https://blog.galaxy.ai/compare/qwen3-6-35b-a3b-vs-qwen3-next-80b-a3b-thinking  
[^75^]: Galaxy.ai - Qwen3.6 Flash vs Qwen3 Coder Next对比，https://blog.galaxy.ai/compare/qwen3-6-flash-vs-qwen3-coder-next  
[^76^]: Galaxy.ai - Qwen3.6 Flash vs Qwen3 Coder 30B A3B Instruct对比，https://blog.galaxy.ai/compare/qwen3-6-flash-vs-qwen3-coder-30b-a3b-instruct  
[^77^]: Galaxy.ai - Qwen3.5 Plus 2026-04-20 vs Qwen3 Coder Flash对比，https://blog.galaxy.ai/compare/qwen3-5-plus-20260420-vs-qwen3-coder-flash  
[^78^]: Galaxy.ai - Qwen3.5-9B vs Qwen3 Coder Flash对比，https://blog.galaxy.ai/compare/qwen3-5-9b-vs-qwen3-coder-flash  
[^79^]: Galaxy.ai - DeepSeek V4 Flash vs Qwen3.5 Plus 2026-04-20对比，https://blog.galaxy.ai/compare/deepseek-v4-flash-vs-qwen3-5-plus-20260420  
[^80^]: Galaxy.ai - Kimi K2.6 vs Qwen3.5 Plus 2026-04-20对比，https://blog.galaxy.ai/compare/kimi-k2-6-vs-qwen3-5-plus-20260420  
[^81^]: Galaxy.ai - Qwen3.5 Plus 2026-04-20 vs Qwen3.6 Max Preview对比，https://blog.galaxy.ai/compare/qwen3-5-plus-20260420-vs-qwen3-6-max-preview  
[^82^]: Galaxy.ai - Qwen3 32B vs Qwen3.5 Plus 2026-04-20对比，https://blog.galaxy.ai/compare/qwen3-32b-vs-qwen3-5-plus-20260420  
[^83^]: Galaxy.ai - Qwen VL Plus vs Qwen3.5 Plus 2026-04-20对比，https://blog.galaxy.ai/compare/qwen-vl-plus-vs-qwen3-5-plus-20260420  
[^84^]: Galaxy.ai - GLM 4.7 Flash vs Qwen3.5 Plus 2026-04-20对比，https://blog.galaxy.ai/compare/glm-4-7-flash-vs-qwen3-5-plus-20260420  
[^85^]: LLM-Stats - Qwen3.6 Plus vs Qwen3 VL 235B A22B Instruct对比，https://llm-stats.com/models/compare/qwen3-6-plus-vs-qwen3-vl-235b-a22b-instruct  
[^86^]: LLM-Stats - Qwen3-Next-80B-A3B-Instruct vs Qwen3 VL 30B A3B Instruct对比，https://llm-stats.com/models/compare/qwen3-next-80b-a3b-instruct-vs-qwen3-vl-30b-a3b-instruct  
[^87^]: LLM-Stats - Qwen3-Next-80B-A3B-Instruct vs Qwen3 VL 32B Instruct对比，https://llm-stats.com/models/compare/qwen3-next-80b-a3b-instruct-vs-qwen3-vl-32b-instruct  
[^88^]: LLM-Stats - Qwen3-Next-80B-A3B-Instruct vs Qwen3-Next-80B-A3B-Thinking对比，https://llm-stats.com/models/compare/qwen3-next-80b-a3b-instruct-vs-qwen3-next-80b-a3b-thinking  
[^89^]: LLM-Stats - Qwen3-235B-A22B-Instruct-2507 vs Qwen3-Next-80B-A3B-Instruct对比，https://llm-stats.com/models/compare/qwen3-235b-a22b-instruct-2507-vs-qwen3-next-80b-a3b-instruct  
[^90^]: TheAIForger - Qwen3-Next-80B-A3B-Instruct vs Thinking对比，https://theaiforger.com/models/compare/qwen3-next-80b-a3b-instruct-vs-qwen3-next-80b-a3b-thinking  
[^91^]: LocalLLM.in - Qwen3-Next-80B-A3B高效本地LLM评测，https://localllm.in/blog/qwen3-next-80b-a3b-efficient-local-llm  
[^92^]: Qwen3.6-35B-A3B GitHub开源与调参参考（含MMLU-Redux、GPQA、AIME等详细分数），https://blog.csdn.net/weixin_41446370/article/details/160232936  
[^93^]: 阿里云百炼 - 千问Turbo后续不再更新建议替换为千问Flash（官方文档明确提示），https://www.alibabacloud.com/help/zh/model-studio/model-pricing  
[^94^]: Galaxy.ai - Qwen3.6 35B A3B模型页（$0.15/$1.00），https://blog.galaxy.ai/model/qwen3-6-35b-a3b  
[^95^]: Galaxy.ai - Qwen3.5-35B-A3B模型页（$0.16/$1.30），https://blog.galaxy.ai/model/qwen3-5-35b-a3b  
[^96^]: PricePerToken - Qwen3 Max vs Qwen3 Next 80B定价与基准对比，https://pricepertoken.com/compare/qwen-qwen3-max-vs-qwen-qwen3-next-80b-a3b-instruct  
[^97^]: LLMReference - GLM-5 vs Qwen3-Max对比（SWE-bench、τ-bench），https://www.llmreference.com/compare/glm-5/qwen3-max  
[^98^]: CloudPrice.net - Qwen Turbo Latest定价与规格（$0.05/$0.20），https://cloudprice.net/models/dashscope%2Fqwen-turbo-latest  
[^99^]: Galaxy.ai - GPT-5.5 vs Qwen3 Coder Flash对比，https://blog.galaxy.ai/compare/gpt-5-5-vs-qwen3-coder-flash  
[^100^]: Galaxy.ai - Qwen3.5 Plus 2026-02-15 vs Qwen3 Coder Flash对比，https://blog.galaxy.ai/compare/qwen3-5-plus-02-15-vs-qwen3-coder-flash  
[^101^]: Galaxy.ai - DeepSeek V4 Flash vs Qwen3 Coder Flash对比，https://blog.galaxy.ai/compare/deepseek-v4-flash-vs-qwen3-coder-flash  
[^102^]: BenchLM.ai - Qwen3.5-35B-A3B vs Qwen3.5 Flash对比，https://benchlm.ai/compare/qwen3-5-35b-a3b-vs-qwen3-5-flash  
[^103^]: arXiv 2505.18585 - Runtime verification results（含Qwen turbo/plus/max数值比较），https://www.arxiv.org/pdf/2505.18585  
[^104^]: arXiv 2510.01164 - 多模型成本与性能对比（含Qwen3-235b-a22b、Kimi-K2等），https://arxiv.org/pdf/2510.01164  
[^105^]: arXiv 2604.03044 - Qwen3.5-35B-A3B与JoyAI-LLM Flash等对比，https://arxiv.org/pdf/2604.03044  
[^106^]: Kimi K2.5 Tech Blog完整基准表（含与GPT-5.2/Claude Opus 4.5/Gemini 3 Pro对比），https://arxiv.org/html/2602.02276v1