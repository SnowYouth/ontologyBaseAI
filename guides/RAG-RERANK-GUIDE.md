# RAG 重排序 (Reranking) 实战指南

> 一份给所有正在做 RAG 的朋友看的、共同进步的策略对比手册。
>
> 本文聚焦一件事:**粗排把候选拿回来之后,怎么再精排一刀,把真正相关的顶到前面。**
> 适合已经做过纯向量或 Hybrid 检索、想把召回的 top-k 真正用起来的工程师。

---

## 目录

- [前言:为什么"粗排够好了"不成立](#前言为什么粗排够好了不成立)
- [第一部分:cross-encoder vs bi-encoder](#第一部分cross-encoder-vs-bi-encoder)
  - [1. bi-encoder 的工作方式](#1-bi-encoder-的工作方式)
  - [2. cross-encoder 的工作方式](#2-cross-encoder-的工作方式)
  - [3. 为啥 cross-encoder 精度高、延迟也高](#3-为啥-cross-encoder-精度高延迟也高)
  - [4. 双塔 vs 单塔的图示意](#4-双塔-vs-单塔的图示意)
- [第二部分:主流 reranker 模型分类](#第二部分主流-reranker-模型分类)
  - [1. 通用 cross-encoder](#1-通用-cross-encoder)
  - [2. LLM-as-reranker (LLM-judge)](#2-llm-as-reranker-llm-judge)
  - [3. ColBERT / Late Interaction](#3-colbert--late-interaction)
  - [4. LambdaMART / GBDT 传统学习排序](#4-lambdamart--gbdt-传统学习排序)
  - [5. 选型对照表](#5-选型对照表)
- [第三部分:工程上"粗排 → 精排"流水线](#第三部分工程上粗排--精排流水线)
  - [1. 为什么要分阶段](#1-为什么要分阶段)
  - [2. 候选量 N 怎么定](#2-候选量-n-怎么定)
  - [3. 阈值 vs top-k 截断的取舍](#3-阈值-vs-top-k-截断的取舍)
  - [4. 多阶段 cascade](#4-多阶段-cascade)
- [第四部分:推理性能与部署](#第四部分推理性能与部署)
  - [1. CPU 单条延迟分布](#1-cpu-单条延迟分布)
  - [2. Batch 化加速](#2-batch-化加速)
  - [3. GPU / 量化](#3-gpu--量化)
  - [4. 蒸馏到 bi-encoder](#4-蒸馏到-bi-encoder)
- [第五部分:生产真实做法 (规模 / SLA / 成本)](#第五部分生产真实做法-规模--sla--成本)
  - [1. Cohere Rerank API](#1-cohere-rerank-api)
  - [2. Vespa native reranking](#2-vespa-native-reranking)
  - [3. Pinecone reranker](#3-pinecone-reranker)
  - [4. Search Quality Eval 工具](#4-search-quality-eval-工具)
  - [5. 多样性 (MMR) 跟 rerank 的关系](#5-多样性-mmr-跟-rerank-的关系)
- [第六部分:Reranker 集成的常见误区](#第六部分reranker-集成的常见误区)
- [第七部分:小型实现的常见坑 → 生产对策](#第七部分小型实现的常见坑--生产对策)
  - [7.1 常见坑](#71-常见坑)
  - [7.2 生产对策 — 4 招应对绝对阈值失效](#72-生产对策--4-招应对绝对阈值失效)
  - [7.3 综合对照表](#73-综合对照表)
- [第八部分:补齐路线 (按 ROI 排序)](#第八部分补齐路线-按-roi-排序)
- [附录 A:术语小词典](#附录-a术语小词典)
- [附录 B:进一步阅读](#附录-b进一步阅读)
- [写在最后](#写在最后)

---

## 前言:为什么"粗排够好了"不成立

如果你已经做完了 Hybrid 检索 (向量 + BM25 + RRF),top-10 看起来花花绿绿,正确答案大多数情况下也确实在里面,那你大概率会问一句:**"够了吧,再加 reranker 是不是过度设计?"**

通常,不够。原因藏在三件事里。

**第一件事:向量召回 top-10 是"语义相近",不是"真相关"。** embedding 模型擅长把"主题接近"的文档拉到一起,但同一主题下的文档往往答的不是同一个问题。一个问"怎么配置参数"的 query,top-10 里可能塞着 "参数的历史背景"、"参数文档目录"、"参数模块的设计哲学",真正讲"怎么配置"的那条排在第 7。从用户视角看,前 3 条全是废话。

**第二件事:BM25 是"字面命中",不是"答得对题"。** 同一个 query 里的关键词出现得越多,BM25 分越高,但 "在哪个段落出现" 它不管。一篇综述里把所有关键词堆在一段,分数能压住真正在回答问题的那一篇专项文章。

**第三件事:Hybrid (RRF) 是"两路都不偏",但 top-1 经常被"半相关但流行的"占走。** RRF 是把两路排名加权平均的"民主投票",在两路都觉得这条 ok 但都不觉得它最好时,这条往往爬到 top-1。结果就是 "看起来都还行" 的中间地带占着前排,真正最优的那条被挤到第 4 第 5。

cross-encoder 干的是另一件事——**query × doc 联合判别**。它不只看 query 里有什么、doc 里有什么,它看 "query 的每个 token 跟 doc 的每个 token 怎么对上"。同样的 doc,换个 query 就是不同的分,因为 attention 的注意力分布完全不同。这是 bi-encoder 这类"各自独立编码再算 cosine"的方案根本做不到的。

举一个**典型现象**: 把 query 换个同义说法 ("怎么配置 X 参数" → "X 参数有哪些可选值"),纯向量召回里原本排 top-3 的那篇可能掉到 top-12,但 reranker 在 top-30 里能把它捞回 top-3。这就是行业公认的"reranker 救场"场景,跟具体语料无关。

结论是:**粗排负责把候选撒进网子,精排负责把鱼挑出来。** 两步缺一不可,top-k 越大,这条规律越明显。

---

## 第一部分:cross-encoder vs bi-encoder

reranker 的精度差异来自模型架构。这一部分讲清楚 bi-encoder 和 cross-encoder 在做什么,以及为什么 cross-encoder 又准又慢。

### 1. bi-encoder 的工作方式

bi-encoder 就是常说的"双塔模型"。query 和 doc 各自独立过一次模型,输出各自一个定长向量,再算 cosine 或 inner product 当相关度。

```
query → [Encoder A] → vec_q
doc   → [Encoder B] → vec_d        # 通常 Encoder A 和 B 共享权重
score = cosine(vec_q, vec_d)
```

最大优势是 **doc 向量可以离线算好**。一千万篇文档,做 embedding 一次性写进 ANN 索引 (HNSW / IVF),查询时只算一次 query 向量,毫秒级返回 top-k。整套流水线的 QPS 上限基本由 ANN 决定,而不是模型。

最大短板是 **query 和 doc 之间没有交互**。模型在分别"独白" —— query 编码的时候不知道有哪些候选 doc,doc 编码的时候也不知道未来会被哪种 query 命中。两边的语义空间靠预训练数据共同分布对齐,这对齐质量直接决定 top-k 的精度上限。

### 2. cross-encoder 的工作方式

cross-encoder 把 query 和 doc 拼成一段输入,一起过模型:

```
[CLS] query tokens [SEP] doc tokens [SEP] → [Transformer] → logit → sigmoid → score
```

注意力机制全开。query 的每个 token 都能"看到" doc 的每个 token,反过来也是。模型最终从 [CLS] 位置 (或别的池化方式) 拿一个标量 logit,通常 sigmoid 化压到 [0, 1] 当相关度分。

这就是为什么 cross-encoder 能捕捉到 bi-encoder 永远抓不到的细节,比如:

- **否定与反义**: "X is opposite of Y" 和 "X is the same as Y" 在 bi-encoder 看可能差不多 (主题词一样),cross-encoder 能区分。
- **数量关系**: "至少 3 个" 和 "不超过 3 个" 字面像,语义反。cross-encoder 的 attention 能注意到 "至少" 和 "不超过" 跟 "3 个" 的搭配方式不同。
- **指代消解**: "他" 指谁、"这个" 指什么,只有 query 和 doc 一起编码时才能解析对。

### 3. 为啥 cross-encoder 精度高、延迟也高

精度高的原因上一节讲过了,延迟高的原因更简单:**不能预计算**。

每来一个 query,要把所有候选 doc 一条一条跟 query 拼起来,跑 N 遍 transformer 前向。N=30 是入门,N=1000 是大规模搜索。bi-encoder 那种 "doc 向量提前算好,查询只过一次模型" 的便宜没法占。

量级感:

- **bi-encoder 单次查询**: ANN 内部搜索 ~1 ms (HNSW), 加上 query embedding ~5-10 ms。整体毫秒级。
- **cross-encoder 单次重排 30 候选**: CPU 上 50-500 ms (取决于模型大小、序列长度、是否 batch),GPU 上 10-50 ms。

如果你正在做 50-100 ms SLA 的在线搜索,cross-encoder 上 30 候选基本是天花板,再多就要 GPU + batch + 量化一起上。

### 4. 双塔 vs 单塔的图示意

```
bi-encoder (双塔):                cross-encoder (单塔):

  query        doc                  query ⊕ doc       ← ⊕ 是 [CLS] q [SEP] d [SEP]
    │           │                       │
    ▼           ▼                       ▼
  ┌───┐       ┌───┐                 ┌─────────────┐
  │enc│       │enc│  ← doc 可离线   │ Transformer │   ← 一次前向, 不能拆
  └─┬─┘       └─┬─┘                 │ full attn   │
    │vec_q     │vec_d               └──────┬──────┘
    └─► cosine ◄┘                          │
         │                                 ▼
         ▼                          logit → sigmoid → score
       score
```

直观上,双塔像两个人各自写答卷再交换比对,单塔像两个人坐一张桌子边讨论边写。后者效果好,但慢得多。

---

## 第二部分:主流 reranker 模型分类

reranker 不是只有 cross-encoder 一种,只是它最常见。按技术路线分,大致是四类。

### 1. 通用 cross-encoder

最主流的一类,生态最全,文档最多,99% 的 RAG 项目选的就是这个。

- **代表模型**: BAAI 的 bge-reranker 系列 (base / large / v2-m3) / Jina 的 jina-reranker 系列 / Cohere Rerank (闭源 API) / mxbai-rerank
- **体积区间**: 小的 200M 参数 (bge-reranker-base / ms-marco-MiniLM), 大的 2B+ (bge-reranker-v2-m3, Cohere Rerank-3 据传更大)
- **训练数据**: 通常是 XLM-RoBERTa 或 mBERT 基底,在 MS MARCO + 多语种检索对数据上 fine-tune
- **选型经验**:
  - 中文 / 多语种场景,默认 bge-reranker-v2-m3 (开源,中文表现是公开数据集第一档) 或 Cohere multilingual (闭源,要钱)
  - 英文场景,轻量选 ms-marco-MiniLM-L-6-v2,顶配选 Cohere Rerank-3 或 bge-reranker-large
  - 日韩等少数语种,先看 jina-reranker 的语种支持列表,实在没有就 bge-reranker-v2-m3 兜底

### 2. LLM-as-reranker (LLM-judge)

直接用通用大模型当 reranker。

- **接口形态**: 给 LLM 一个 query 和候选列表,通过 prompt 让它输出每条的相关度分 (0-1 浮点 / {高,中,低} 分类 / 0-100 整数都有人用)
- **代表实现**: GPT-4 / Claude / 开源 Llama 3 70B 配合 few-shot prompt; LlamaIndex 的 `LLMRerank`;Cohere v3 据传内部混合了 LLM-judge 思路
- **优点**:
  - 精度可以非常高 (相当于一个 reasoning 模型在做相关度评估)
  - 能自然解释 "为啥这条相关" (让 LLM 顺手输出理由)
  - 不需要训练,prompt 就能用
- **缺点**:
  - 慢, 秒级延迟, 上线难
  - 贵, 按 token 计费, 单次 rerank 几分钱起步
  - 输出不稳定,需要严格的 prompt 工程加 schema 校验
- **工业用法**:
  - **离线生成训练数据**: 用 LLM 标 100 万 (query, doc) 对,蒸馏给 cross-encoder
  - **二级 rerank**: cross-encoder 把 30 → 5,LLM-judge 再 5 → 3, 延迟摊得开
  - **A/B 评估**: LLM-judge 当 ground truth, 算 NDCG/MRR

### 3. ColBERT / Late Interaction

介于 bi-encoder 和 cross-encoder 之间的中间形态。

- **代表**: ColBERT v1 (Khattab & Zaharia, 2020), ColBERT v2 (压缩版), ColPali (多模态版,query 是文本,doc 是图)
- **核心思路**: 像 bi-encoder 一样各自编码,但**输出的不是单个向量,而是每个 token 一个向量**。检索时算 query 每个 token 跟 doc 所有 token 的 max-sim,再把所有 query token 的 max-sim 加起来。
- **优点**:
  - 比 cross-encoder 快 (不用每对都重跑 transformer)
  - 比 bi-encoder 准 (有 token 级交互)
  - 可以做"候选预筛 + 精排"一体化 (PLAID 等专用索引)
- **缺点**:
  - **存储量爆炸**: 每个文档存 N 个向量 (N = doc token 数), 索引大小是 bi-encoder 的 5-10 倍, 1 千万文档级别后维护成本非常高
  - 生态比 cross-encoder 窄,工具支持不如 sentence-transformers
- **适用**: 中等规模 (百万到千万 doc)、对延迟敏感、能接受更高存储成本的搜索系统

### 4. LambdaMART / GBDT 传统学习排序

工业搜索/推荐系统的主力,**RAG 圈讲得少但工业用得最广**。

- **代表**: LightGBM / XGBoost 的 ranker 模式 (objective=rank:pairwise / lambdarank), Vespa 的 native ranking, 各家自研 GBDT
- **核心思路**: 把多路检索分数 (BM25 / vector cosine / freshness / click-through rate / domain authority / 用户特征 / 上下文特征) 当特征,用 pairwise 或 listwise 损失训一个 GBDT 模型当 reranker。
- **优点**:
  - **可解释**: 每个特征的贡献透明 (SHAP 一拉就清),问题好排查
  - **快**: 一次预测毫秒级,QPS 上限极高
  - **工业沉淀深**: Yahoo / 微软 / 阿里 / 字节都在用,内部最佳实践成熟
- **缺点**:
  - 需要监督信号 (点击日志或人工标注),冷启动慢
  - 表达力弱于深度学习,语义不如 cross-encoder 抓得细
- **工业用法**: 推荐系统 / 广告排序 / 商品搜索的 L1 二排主力,离线 batch rerank 仍是首选

### 5. 选型对照表

| 类型 | 精度 | 单次延迟 | 训练成本 | 部署难度 | 适合场景 |
|---|---|---|---|---|---|
| 通用 cross-encoder | 高 | 中 (50-500ms/30 候选) | 低 (拿现成的) | 低 (一行调用) | 通用 RAG 精排首选 |
| LLM-as-reranker | 极高 | 极慢 (秒级) | 0 (用 API) / 高 (自训) | 中 (API) / 高 (自部署) | 离线评估 / 高价值场景 |
| ColBERT | 高 | 中 (比 cross-encoder 快) | 中 | 高 (存储成本高) | 大语料 + 中等 QPS |
| LambdaMART | 中-高 | 极快 (μs 级) | 中 (要训,要标注) | 低 | 多信号融合,业务排序 |

入门最稳的路: **通用 cross-encoder 起步, 数据攒够后蒸馏个轻量版 bi-encoder 或上 LambdaMART 做 L1, LLM-as-reranker 留给离线评估**。

---

## 第三部分:工程上"粗排 → 精排"流水线

reranker 不能单独存在,它必须跟粗排配合成一条流水线。这一节讲分阶段、候选量、阈值三个关键设计点。

### 1. 为什么要分阶段

直觉上 "为啥不直接 cross-encoder 全打分"? 因为算不过来。如果你有 100 万 doc,每个 query 都让 cross-encoder 跑 100 万次前向,单 query 几分钟,直接劝退。

所以工业上是 cascade:

| 阶段 | 名称 | 输入候选 | 输出候选 | 关注指标 | 典型模型 |
|---|---|---|---|---|---|
| L0 | 粗排 (Retrieval) | 全量 doc | 1000 | **召回率 (Recall)** | 向量 + BM25 + RRF |
| L1 | 二排 (First-stage rerank) | 1000 | 100 | 精度 + 召回兼顾 | LambdaMART / GBDT / 轻量 cross-encoder |
| L2 | 精排 (Final rerank) | 100 | 10 | **精度 (Precision)** | 大 cross-encoder |
| L3 | 终排 (Business rules) | 10 | 3-5 | 用户体验 | 多样性 / 新鲜度 / 个性化规则 / LLM |

每一层都比上一层准、比上一层慢、比上一层候选少。这就是"漏斗"模型。

入门项目通常合并到两层 (L0 + L2),把 L1 和 L3 省了。够用,但天花板低。

### 2. 候选量 N 怎么定

cross-encoder 重排的候选量 N 是关键参数,直接决定延迟。

- **N = 30**: 入门首选, CPU 单次 < 1s, 大多数 demo / 学习项目用这个
- **N = 100**: 中等规模生产, 适合有 GPU 的服务, 延迟在 100-300 ms
- **N = 1000+**: 大规模搜索 (Bing / Google 级别), 必须 batch 化 + GPU 并行 + 多机分片

定 N 的两条经验法则:

1. **召回率拐点法**: 在评测集上画 Recall@N 曲线,找曲线变平的点。如果 Recall@30 = 0.85, Recall@100 = 0.86, Recall@1000 = 0.86, 那 N=30 就够,加大 N 只是增加噪音让 reranker 更累。
2. **延迟预算法**: 把端到端 SLA 减去其它环节 (粗排 / LLM 生成 / 网络),剩下的留给 rerank,反推 N。

### 3. 阈值 vs top-k 截断的取舍

reranker 打完分,怎么决定哪些返回给下游? 三种主流策略,各有取舍。

| 策略 | 做法 | 优点 | 缺点 |
|---|---|---|---|
| top-k 截断 | 永远返回前 k 条 | 简单, 实现一行 | 没召回信号, query 不命中也返回 k 条 |
| 绝对阈值 | 分数 < T 的扔掉 | 有 "不命中" 信号 | 依赖分数标定 (后面专讲, 这是大坑) |
| 相对阈值 | top1/mean(top5) ≥ R | 不依赖绝对分数 | 调参从 T 换成 R, 仍要小规模实测 |
| 混合 | top-k 截断 + 兜底阈值 | 兼顾两者 | 调参多一个 |

**生产里最常见的是混合**: 最多返回 k 条,但分数明显偏低的过滤掉。这样既不会无脑塞满 k 条,也不会因为阈值太严返回空。

### 4. 多阶段 cascade

把上面的几件事拼起来,是一个典型工业搜索引擎的样子 (这里用搜索引擎口径,不是任何特定项目的):

```
        ┌─────────────────┐
query → │  L0: 倒排 + ANN │ → 1000 候选
        │  粗排, 召回为主 │
        └─────────────────┘
                 │
                 ▼
        ┌─────────────────┐
        │  L1: LambdaMART │ → 100 候选
        │  多信号融合二排 │
        └─────────────────┘
                 │
                 ▼
        ┌─────────────────┐
        │ L2: cross-encoder│ → 10 候选
        │ 精排, 精度为主  │
        └─────────────────┘
                 │
                 ▼
        ┌─────────────────┐
        │  L3: 业务规则   │ → 3-5 给用户
        │  多样性 / LLM   │
        └─────────────────┘
```

学习项目能跑到 L0 + L2 就已经及格,跑到 L0 + L1 + L2 算优秀。L3 是产品力,跟 reranker 关系不大。

---

## 第四部分:推理性能与部署

cross-encoder 的延迟是个绕不开的话题。这一节讲 CPU/GPU 上的实测分布,以及加速的三板斧:batch、量化、蒸馏。

### 1. CPU 单条延迟分布

经验数据 (单线程 CPU, AVX2, FP32, 序列长度 256):

| 模型 | 参数量 | 单条 (q, d) 延迟 | 30 候选总时长 (单条循环) |
|---|---|---|---|
| ms-marco-MiniLM-L-6-v2 | ~22M | 5-10 ms | 150-300 ms |
| bge-reranker-base | ~280M | 15-30 ms | 450-900 ms |
| jina-reranker-v2-base | ~280M | 15-30 ms | 450-900 ms |
| bge-reranker-large | ~560M | 30-60 ms | 900-1800 ms |
| bge-reranker-v2-m3 | ~570M | 30-80 ms | 900-2400 ms |

经验法则: **30 候选 CPU 跑超过 1.5 s,就要考虑批量优化 / GPU / 量化了**。

入门项目不要被这数字吓住——多数 demo 场景一次问答 1-2 秒延迟用户能接受,reranker 占大头不是问题。但生产上线必须算清楚这笔账。

### 2. Batch 化加速

把 (query, doc) pair 打成 batch 一起前传,比逐条调快 3-10x。

```python
# 反例 (慢)
scores = []
for doc in candidates:
    scores.append(model.predict([(query, doc)]))

# 正例 (快)
pairs = [(query, doc) for doc in candidates]
scores = model.predict(pairs, batch_size=32)
```

注意点:

- **batch_size 太大爆显存,太小没收益**。16-32 是 CPU/小 GPU 的经验区间,大 GPU 上 64-128
- **batch 内序列长度不一致时, padding 会浪费算力**。按 doc 长度排序后再 batch,padding 损耗能降一半
- **库内部通常已经做了 batch**, 比如 sentence-transformers 的 `model.predict(pairs)` 默认会按 batch_size 切。自己写 pipeline 时别忘了

### 3. GPU / 量化

GPU 上 cross-encoder 飞起来:

- **FP16 (半精度)**: 显存减半, 速度快 1.5-2x, 精度几乎无损。主流 reranker 库默认支持。
- **BF16 (脑浮点)**: Ampere 及以上 NVIDIA GPU 才支持, 数值范围比 FP16 大, 训练更稳定, 推理速度跟 FP16 差不多
- **INT8 (8 位整数量化)**: 显存 1/4, 速度快 2-4x, 精度有小损失 (NDCG 通常掉 0.5-2%)。适合大 batch 场景
- **INT4**: 极致压缩, 适合边缘设备, 精度损失明显, RAG 用得少

工具链:

- **ONNX Runtime**: 跨平台, CPU/GPU 都支持, 转换简单
- **TensorRT**: NVIDIA 专用, 优化最激进, 配置复杂
- **HuggingFace Optimum**: 包装了 ONNX Runtime 和 TensorRT, sentence-transformers 转 ONNX 一行命令
- **OpenVINO**: Intel CPU 专用, AVX-512 收益明显

### 4. 蒸馏到 bi-encoder

如果实在受不了 cross-encoder 的延迟,可以走"知识蒸馏":

```
[cross-encoder]    用 (query, doc) → score 标 100 万对训练数据
       │
       │  老师教学生
       ▼
[bi-encoder]       学生学着复现老师的相关度判断
```

学生 (bi-encoder) 推理快, 但精度趋近 cross-encoder 上限的 80-90%。代表案例: ms-marco-MiniLM-L-6-v2 这种小 bi-encoder 就是这么训出来的,精度比直接训的 bi-encoder 高一截。

适合的场景: 流量大、SLA 紧、有标注预算或愿意用 LLM 当 labeler 的团队。

---

## 第五部分:生产真实做法 (规模 / SLA / 成本)

这一节讲工业代表方案,不绑定任何具体项目身份。看完后心里大致有数 "如果有一天要扛真流量, 应该长什么样"。

### 1. Cohere Rerank API

服务化的 reranker, 不自部署。

- **定价**: 按 query 收费, $2/1000 search units (一次最多 100 候选), 算下来一次 rerank 几毫钱
- **延迟**: 单次 ≤ 100 ms (云端 GPU + 网络往返)
- **模型**: 多语种 v3, 中英日韩西法德都支持
- **优点**: 免运维, 模型升级跟着 Cohere 走, SDK 一行调用
- **缺点**: 数据出境 (中国用户合规问题), 长期成本不低, 模型选择有限

适合**不想自部署的中小生产**。

### 2. Vespa native reranking

Vespa 是 Yahoo 开源的搜索引擎, 把粗排、二排、精排、业务排序全做在一个引擎里。

- **核心**: 在 `rank-profile` 里写表达式 `bm25(content) * 0.3 + closeness("embedding") * 0.4 + cross_encoder_score * 0.3`
- **优点**: 单引擎多阶段 cascade, 状态自洽, 不用维护多个服务之间的数据一致性
- **缺点**: 学习曲线陡, 文档密度大, 中文社区资料少, 团队上手成本高

适合**愿意投入引擎专业度、长期吃工业级流量**的团队。

### 3. Pinecone reranker

Pinecone 是托管向量库, 最近几年加了 rerank 功能, 跟向量库一体。

- **接口**: `index.query(vector=..., rerank=True, rerank_model="bge-reranker-v2-m3")`
- **优点**: API 一行启用, 不用关心模型部署
- **缺点**: 一般只支持自家提供的 reranker 模型, 自定义有限

适合**已经在用 Pinecone 做向量库的团队**, 一键升级到带 rerank 的检索。

### 4. Search Quality Eval 工具

reranker 选型必须靠评测,不能拍脑袋。主流工具:

- **Ranx (Python)**: NDCG / MRR / MAP / Recall@k 一站算, 支持多模型对比, 开源
- **TREC eval (C)**: 学术标准, 工业团队几乎都用, 输入是 TREC 格式
- **BEIR benchmark**: 跨数据集对比, reranker 选型直接看 BEIR leaderboard
- **MTEB Reranking sub-leaderboard**: HuggingFace 维护, 主流 reranker 在多个数据集上的横评

入门项目至少装一个 Ranx, 50 条评测就能算出 NDCG@5 和 MRR, 选型有数。

### 5. 多样性 (MMR) 跟 rerank 的关系

reranker 是 "选最相关的", 不管会不会重复。如果 top-10 里有 5 条都讲同一件事, 用户和 LLM 都吃亏。**MMR (Maximal Marginal Relevance)** 是 rerank 之后的"去同质化"。

公式:

```
MMR(d) = arg max_{d ∈ candidates}  λ · rel(d, q) - (1 - λ) · max_{d' ∈ selected} sim(d, d')
```

人话: 既要跟 query 相关 (`rel(d, q)` 高), 也要跟已选的不像 (`sim(d, d')` 低)。`λ ∈ [0, 1]` 平衡两边, 0.5-0.7 是经验区间。

实战做法: cross-encoder 给 `rel(d, q)` 当 MMR 的相关度项, MMR 在 top-30 里挑 top-10, 既相关又不重复。

工具: LangChain `ContextualCompressionRetriever`, LlamaIndex `MMRPostprocessor`, Vespa 内置 diversity 维度都支持。

---

## 第六部分:Reranker 集成的常见误区

这一节列五个常见坑,跟具体项目无关,都是社区里反复出现的:

- **误区 1: 以为 "rerank 一定涨指标"**。如果粗排已经把对的排在 top-3, rerank 再洗一遍可能反而把噪音顶上去 (cross-encoder 也会犯错)。**必须有评测集量化**,不涨就关掉,别盲目信任。

- **误区 2: 用没在中文语料微调的英文 reranker 处理中文 query**。ms-marco-MiniLM 在英文上漂亮,中文上经常乱排。中文场景 bge-reranker-v2-m3 / Cohere multilingual 才是默认选项。

- **误区 3: 不放阈值光看 top-k**。query 没命中知识库, reranker 还是会从粗排候选里挑 top-k 返回, 喂给 LLM 直接幻觉。一定要有"拒答"机制 (阈值或 LLM 自觉拒答, 后面专讲)。

- **误区 4: 把 reranker 的分跟 cosine 分混在一起做加权融合**。两者尺度完全不同 (cosine ∈ [-1, 1] 实际 [0, 1], rerank sigmoid 后 [0, 1] 但分布完全不同), 加权后基本是噪音。融合用 RRF 这种 rank-based 方案, 别 score-based。

- **误区 5: 模型加载失败用 SystemExit 这种"不继承 Exception"的异常**。调用方写的"宽 catch 降级路径"全是死代码, 进程直接挂掉, 降级和埋点形同虚设 (后面 7.1 坑 3 专讲)。

这五条覆盖了 80% 的踩坑场景。剩下的细节, 第七节展开。

---

## 第七部分:小型实现的常见坑 → 生产对策

> 这一节是全文最重的部分。主语统一用 "demo 实现 / 小型项目 / 学习场景 / 入门做法", 对面是 "生产 / 工业级 / 规模化部署"。所有坑都是 RAG 社区里反复出现的, 不绑定任何特定项目。

### 7.1 常见坑

#### 坑 1: 单 score 字段双语义 (rerank 分覆盖粗排分)

**demo 实现常见做法**: 检索函数返回 `{"text": ..., "score": ...}` 列表, score 字段既装粗排的 cosine, 又装 rerank 的分。开启 reranker 后,新分覆盖旧分,字段名不变。

**症状**:
- 上层代码看不出来 "这个 score 是怎么来的"
- 日志埋点把 cosine 误当 rerank 分比对, 评测时一头雾水
- 想做 A/B 时分不开两路分数的贡献

**本质**: 单字段在两种语义间复用。vector cosine ∈ [-1, 1] (实际多在 [0, 1]), rerank logit 经 sigmoid 后 [0, 1] 但分布完全不同——同一个数 0.6, 在 cosine 里算 "比较像", 在 rerank 里算 "中等相关"。**名字一样, 意义不同**, 是工程上典型的 "隐式 schema 坑"。

**退化级**: ⭐ (1 颗星, 严重退化)

**生产做法**: 多字段并存, 每路检索器自己一个分字段, 最终保留一个 `score` 当 "由策略决定的当前最优分":

```json
{
  "text": "...",
  "vector_score": 0.78,
  "bm25_score": 4.32,
  "rrf_score": 0.0328,
  "rerank_score": 0.6904,
  "score": 0.6904
}
```

**生产参考**:
- Elasticsearch 8.x 的 `_score` + `fields` 同时返回, 各算子可拿到原始分
- Vespa 每个 `rank-feature` 都能取
- Qdrant payload 自定义字段, 任意保留

**退化级**: ⭐⭐⭐⭐ (4 颗星, 接近最佳实践)

---

#### 坑 2: sigmoid(0) = 0.5 + 小语料 + 保守模型 → 绝对阈值过滤形同虚设

**demo 实现常见做法**: 设一个绝对阈值, 比如 `rerank_score >= 0.5` 才返回, 不到的丢掉。

**症状**: 给一组**完全不相关的 query** (比如问"今天天气怎么样", 但语料只讲 RAG), reranker 输出**都在 0.50 上下漂**。绝对阈值过滤掉的不多, 反而把弱相关的也放过去喂给 LLM。

**三个原因叠加, 缺一不可**:

1. **sigmoid(0) = 0.5 是数学决定的**。cross-encoder 输出 logit, sigmoid 函数把 logit 压到 [0, 1], 当模型 "不确定" 时 logit 接近 0, 分数自然接近 0.5。这不是模型差, 是 sigmoid 的形状决定的——只要 logit 没有显著偏离 0, sigmoid(logit) 就在 0.5 附近。

2. **小语料 (几十块以内) 时, 候选池里没有"明显不相关"的 doc**。如果只有十几块 chunk, 全是同一主题, 那"最不相关"的 doc 跟 query 也有词重合 (都是中文 / 都讲技术), 模型难给低分。
   ```
   query: "怎么配置 X 参数"
   候选: [讲 X 历史的, 讲 X 模块的, 讲 X 设计哲学的, 讲 X 配置的, ...]
   ```
   全部都跟 X 有关, 模型很难说哪个 "完全不相关"。

3. **保守模型不敢打负分**。多语种 reranker 在公开数据 (MS MARCO 等) 上微调时, 为了泛化, 倾向输出 logit ∈ [-1, +1.5] 这种窄区间, sigmoid 后大多落在 [0.27, 0.82], 中间地带挤得满满当当, 区分度不大。

**直接现象**: 相关 query 的 top1 (比如 0.50-0.73) 跟不相关 query 的 top1 (比如 0.50-0.51) **分布严重重叠**, 在中间随便取一个值当阈值, 跟 "不设阈值" 几乎一样。

**退化级**: ⭐⭐ (2 颗星, 退化中度)

**生产对策**: 下面 "7.2 生产对策" 4 招专讲, 招招都比 "拍一个 0.5 上线" 强。

---

#### 坑 3: SystemExit 不继承 Exception → 调用方宽 catch 兜不住

**demo 实现常见做法**: reranker 模型文件缺失时直接 `raise SystemExit("model not found")`, 觉得 "干净退出"。调用方写:

```python
try:
    reranker = Reranker(model_path)
except Exception as e:
    log_event("reranker.load_failed", {"error_type": type(e).__name__})
    # 降级到不用 reranker
```

**症状**: `SystemExit` 不继承 `Exception` (它继承 `BaseException`), 上面的 `except Exception` 抓不住, **进程直接退出**, 降级路径 (continue without reranker) 和埋点全是死代码。

**本质**: Python 异常继承层级里有四个特殊 `BaseException` 子类:

```
BaseException
├── SystemExit          ← sys.exit() / raise SystemExit
├── KeyboardInterrupt   ← Ctrl+C
├── GeneratorExit       ← 生成器关闭
└── Exception           ← 业务异常都在这下面
    ├── OSError
    │   ├── FileNotFoundError
    │   └── ...
    ├── ValueError
    └── ...
```

任何 "降级用宽 catch" 的代码 (`except Exception`) 都兜不住前三类。这是 Python 文档明写的设计 (PEP 3134 时定下来), 目的是让 `sys.exit()` 和 `Ctrl+C` 能穿透业务异常处理, 让进程真的能退。

库作者写 `raise SystemExit(1)` 时往往只想到 "退出干净", 没想到 "调用方需要降级"。结果就是**库逼着调用方接受 "失败 = 进程死"**, 一致性差, 没法降级。

**退化级**: ⭐ (1 颗星, 严重退化, 因为它让所有降级路径失效)

**三种修法对比**:

| 修法 | 做法 | 利 | 弊 | 推荐度 |
|---|---|---|---|---|
| A: 改 except | 调用方写 `except (Exception, SystemExit)` | 不动库 | 把 Ctrl+C 也兜了, 用户按了退不出 | ✗ 不推荐 |
| B: 改 raise | 库改成 `raise FileNotFoundError("model dir missing: ...")` | 语义对题, 标准 catch 兜得住, 各调用方写法不用改 | 改库代码 | ⭐ **推荐** |
| C: 接受现状 | 不改, 文档里写"加载失败会直接退出, 自行兜底" | 不改代码 | 把降级责任甩给所有调用方, 一致性差 | ✗ 不推荐 |

**生产参考**:
- HuggingFace `transformers` 的 `AutoModel.from_pretrained` 抛 `OSError`
- sentence-transformers 抛 `OSError` 或库定义的 `LookupError` 子类
- PyTorch `torch.load` 文件不存在抛 `FileNotFoundError`

**没人用 SystemExit 当业务错误**, 这是约定俗成。

### 7.2 生产对策 — 4 招应对绝对阈值失效

坑 2 是 reranker 集成最棘手的问题。这里展开 4 招, 从轻到重排, 都能用。

#### 招 1: Platt scaling / Isotonic regression 做分数校准

**思路**: 收集 100-1000 条 `(query, doc, 真相关吗?)` 标注, 训一个 1D 逻辑回归 (Platt scaling) 或保序回归 (Isotonic regression), 把 raw rerank score 映射成 "真正的相关概率"。

```
raw_score (sigmoid 后) ─→ [calibrator] ─→ calibrated_score (真概率)

经典效果: 原本 0.5 的中间分, 校准后被压到 0.1 (因为大多数 0.5 其实是 "不相关")
        原本 0.7 的分, 校准后保留在 0.7 (那段确实是 "相关")
```

**效果**: 0.5 这种"中间值"在校准后会被压到接近 0 (大多数中间分数其实是 "不相关"), 阈值变得有意义, 可以放心写 `score >= 0.5` 当过滤条件。

**代价**:
- 需要标注 (人工 100 条, 或用 LLM-judge 当 labeler 标 500 条)
- 标注分布要跟在线 query 分布对得上 (校准是有 bias 的, 用错数据校反了)
- 校准模型本身要定期重训 (语料更新 / 模型升级后重新校)

**代表实现**: scikit-learn `CalibratedClassifierCV` (Platt / Isotonic 都封装好了), Cohere Rerank-3 内部据说做了类似处理。

#### 招 2: 相对阈值 (不卡绝对分)

**思路**: 不看分数本身, 看分数的**结构关系**:

| 方式 | 公式 | 含义 |
|---|---|---|
| 比例阈值 | `top1 / mean(top5) >= 1.5` | top1 显著高于平均, 有命中 |
| 差值阈值 | `top1 - top5 >= 0.05` | top1 跟 top5 拉开了差 |
| Gap-based | `top1 - top2 >= 0.1` | top1 鹤立鸡群 |

**效果**: 对模型分数分布不敏感, 只关心 "有没有显著强的那一条"。同一个 query 在不同 reranker / 不同模型版本上都能用。

**代价**: 调参从 "阈值 T" 换成 "倍率 R 或差值 Δ", 仍要小规模实测找合适的值。但实测样本量不需要校准那么多, 30-50 条就能定。

**代表实现**:
- LangChain `ContextualCompressionRetriever`
- LlamaIndex `SimilarityCutoffPostprocessor`
- Vespa 的 dynamic threshold

**这一招是入门项目最值得做的升级**, 不需要标注、不需要重训, 几行代码切过去就有效果。

#### 招 3: 把"拒答"交给 LLM (兜底防线)

**思路**: reranker 即使过了阈值也未必真相关, 让最终的 LLM 看完资料后**自觉拒答**。在 system prompt 里写明:

```
仅基于以下资料回答用户问题。如果资料里没有相关信息, 直接回复
"根据提供的资料, 我无法回答这个问题"。不要编造。
```

**效果**: reranker 是粗筛, LLM 是终审。两道防线总有一道兜住。

**代价**: 对 LLM 的 "诚实性" 有依赖。新模型 (Claude 4.x / GPT-4o / DeepSeek V3) 已经做得不错, 但弱模型 (7B 开源、未充分对齐) 不靠谱, 拒答指令照样幻觉。

**代表实现**: 所有靠谱 RAG 系统的 system prompt 都包含这种约束。OpenAI 官方 Cookbook 的 RAG 例子也是这么写的, LangChain / LlamaIndex 的默认 prompt 模板里全都有 "if you don't know, say I don't know"。

**退化级**: 这个不是退化, 是**标配的双保险**, 生产也照样要。

#### 招 4: LLM-as-reranker (替代 cross-encoder)

**思路**: 直接用 LLM (Claude Haiku / GPT-4o-mini / GLM-4-Flash 这类轻量模型) 当 reranker, 用 prompt 把候选喂进去让它输出每条的相关度。

**效果**: 精度通常**比 cross-encoder 高一截** (推理能力强), 不存在 "sigmoid 中间地带" 问题。可以直接给 0/1 二分类或 {高,中,低} 三档, 阈值天然有意义。

**代价**:
- 延迟秒级 (cross-encoder 是百毫秒级)
- 成本按 token 算 (cross-encoder 几乎零成本)
- 输出要做 schema 校验 (LLM 偶尔会输出非法格式)

**代表实现**:
- Cohere Rerank v3 / Voyage rerank-2 内部据传走的就是 LLM-judge 路线
- LlamaIndex 的 `LLMRerank`
- LangChain `LLMChainExtractor` 提示工程版本

**适用**: 高价值场景 (法律 / 医疗 / 金融) 接受秒级延迟和高单价, 或者**离线评估**当 ground truth (这个用法最常见)。

### 7.3 综合对照表

把上面三个坑 + 通用退化点统一列一遍, 让"从 demo 走到生产"的路线一眼能看清:

| 退化点 | demo 实现常见做法 | 生产做法 | 退化级对照 |
|---|---|---|---|
| 单字段双语义 | `score` 一字段两用 | 多字段并存 (`vector_score` / `rerank_score` / final `score`) | ⭐ vs ⭐⭐⭐⭐ |
| 绝对阈值 | `rerank_score >= 0.5` 一刀切 | Platt 校准 / 相对阈值 / LLM 兜底拒答 / LLM-as-reranker | ⭐⭐ vs ⭐⭐⭐⭐ |
| 模型加载失败异常 | `raise SystemExit(1)` | `raise FileNotFoundError / OSError` | ⭐ vs ⭐⭐⭐⭐⭐ |
| 模型选型 | 用一个固定 cross-encoder | 多模型 ensemble + LLM-judge 离线评估选最优 | ⭐ vs ⭐⭐⭐⭐ |
| 候选量 | N=30 写死 | 按 query 类型 / 流量分桶动态调 (30-1000) | ⭐⭐ vs ⭐⭐⭐⭐⭐ |
| 阈值策略 | 静态阈值 | 按 query 类型动态调, 配 NDCG / MRR 监控 | ⭐⭐ vs ⭐⭐⭐⭐ |
| 多样性 (MMR) | 没做 | MMR / DPP post-processor 标配 | ⭐ vs ⭐⭐⭐⭐ |
| 多阶段 cascade | 单一 cross-encoder | L0 + L1 GBDT + L2 cross-encoder + L3 LLM | ⭐⭐ vs ⭐⭐⭐⭐⭐ |

⭐ 数越多, 离生产最佳实践越近。退化级 ⭐ 跟 ⭐⭐⭐⭐⭐ 差三档以上的, 都是补齐路线里 ROI 最高的几条。

---

## 第八部分:补齐路线 (按 ROI 排序)

给"想从 demo 走到生产"的读者一份路线图。顺序不是技术先进度, 是 **"改最少代码 / 拿最大收益"** 的排序:

1. **加可观测性**。落 `reranker.rerank` / `reranker.load_failed` 这种 JSONL 事件, 字段至少包括 `query_hash` / `n_candidates` / `top1_score` / `latency_ms` / `model_id`。后续所有改动都靠数据说话, 没数据全是猜。

2. **加评测集**。50-100 条 `(query, 期望相关 doc id)` 标注, 算 NDCG@5 / MRR / Recall@10。规模不大就够开始量化。

3. **修单字段双语义** (坑 1)。多字段并存, 这是后续 A/B 和评测的前提, 改完后续工作才有抓手。

4. **修 SystemExit → FileNotFoundError 之类** (坑 3)。让宽 catch 真正生效, 模型加载失败能优雅降级到 "无 rerank 版本"。

5. **修阈值** (招 2, 相对阈值)。从绝对阈值切到相对阈值, 不需要标注, 几行代码改完, 立刻有效果。

6. **若需要更准, 上 Platt 校准** (招 1)。要小规模标注 (100-500 条), 校准后绝对阈值才真有意义。

7. **若需要更稳, 上 cascade**。粗排 1000 → GBDT 100 → cross-encoder 30 → LLM 5。工程改动大, 但效果天花板高。

8. **若 LLM-judge 跑得起, 加招 4 当二级 rerank**。高价值场景接受秒级延迟和高单价时, 用 LLM 兜底, 精度再上一档。

前 5 条都是 "几小时到几天" 能做完的, 性价比最高。后 3 条是 "几周到几个月" 的工程, 量级到了再上。

---

## 附录 A:术语小词典

- **cross-encoder**: 把 query 和 doc 拼起来一起过 transformer 的模型, 精度高速度慢, 适合精排
- **bi-encoder**: query 和 doc 分别编码再算 cosine 的模型, 速度快精度次之, 适合粗排
- **late interaction (ColBERT)**: 各自编码出 token 级向量, 检索时算 token-token 的 max-sim, 介于 bi/cross 之间
- **logit**: 模型最后一层的原始输出 (未经 sigmoid 或 softmax), 实数, 范围一般 ±10 以内
- **sigmoid**: `σ(x) = 1 / (1 + e^-x)`, 把任意实数压到 (0, 1), `σ(0) = 0.5`
- **softmax**: 把一组实数归一成概率分布, 多分类任务的常见输出层
- **L0 / L1 / L2 cascade**: 工业搜索的多阶段流水线, L0 粗排 (倒排+ANN), L1 二排 (GBDT), L2 精排 (cross-encoder)
- **MRR (Mean Reciprocal Rank)**: 第一个相关结果排名的倒数的均值, 衡量 top-1 准不准
- **NDCG (Normalized Discounted Cumulative Gain)**: 考虑相关度等级和排名位置的综合指标, LTR 默认优化目标
- **MAP (Mean Average Precision)**: 多个 query 的 AP 平均, 衡量整体排序质量
- **Recall@k**: top-k 里至少有一个相关 doc 的比例, 衡量召回
- **Precision@k**: top-k 里相关 doc 占比, 衡量精度
- **MMR (Maximal Marginal Relevance)**: rerank 后的多样性算法, 平衡相关性和去重复
- **DPP (Determinantal Point Process)**: 比 MMR 更严格的多样性算法, 数学上保证子集行列式最大
- **diversity**: 排序结果的多样性, 反指标是 "前 N 条全讲同一件事"
- **Platt scaling**: 用 1D 逻辑回归把模型 raw score 校准成真概率, 1999 年提出
- **Isotonic regression**: 保序回归校准, 不假设线性, 比 Platt 灵活
- **calibration**: 分数校准, 让模型输出的 0.7 真的对应 70% 的事件概率
- **BEIR benchmark**: 零样本检索评测集合 (18 个数据集), reranker 选型主要看 BEIR leaderboard
- **MS MARCO**: 微软发布的大规模检索数据集 (800 万 doc + 100 万 query), 多数 reranker 在这上面预训练
- **MTEB**: Massive Text Embedding Benchmark, HuggingFace 维护, 含 Reranking sub-leaderboard
- **LambdaMART**: 基于 GBDT 的 listwise 排序模型, 微软 LightGBM 实现, 工业 LTR 主力
- **RankNet / pairwise / listwise / pointwise**: LTR 训练损失的四种范式, listwise 表达力最强
- **LLM-as-reranker / LLM-judge**: 用通用 LLM 当 reranker 或评测器, 精度高但慢且贵
- **shadow eval**: 影子评估, 把新模型的输出跟旧模型并行记录, 不影响线上, 用离线评测对比
- **query × doc joint encoding**: query 和 doc 联合编码, cross-encoder 的核心机制
- **token-level interaction**: token 级交互, ColBERT 和 cross-encoder 都有, bi-encoder 没有

---

## 附录 B:进一步阅读

**论文 / 原始资料:**

- *Sentence-BERT: Sentence Embeddings using Siamese BERT-Networks* (Reimers & Gurevych, 2019) — bi-encoder 现代奠基
- *ColBERT: Efficient and Effective Passage Search via Contextualized Late Interaction over BERT* (Khattab & Zaharia, 2020) — Late Interaction 原始论文
- *ColBERTv2: Effective and Efficient Retrieval via Lightweight Late Interaction* (Santhanam et al., 2022) — ColBERT 压缩版
- *Beyond Goldfish Memory: Long-Term Open-Domain Conversation* (Xu et al., 2022) — 对话场景的 rerank 实践
- *Lost in the Middle: How Language Models Use Long Contexts* (Liu et al., 2023) — 解释了 "为什么 rerank 后还要做 reorder"
- *From RankNet to LambdaRank to LambdaMART: An Overview* (Burges, 2010) — LambdaMART 经典综述
- *Distilling Dense Representations for Ranking using Tightly-Coupled Teachers* (Lin et al., 2020) — cross-encoder 蒸馏到 bi-encoder 的经典工作

**工程实践 / 文档:**

- HuggingFace sentence-transformers 的 `CrossEncoder` 文档与示例
- Cohere Rerank API 官方文档
- Vespa Ranking Profile 文档 (开源里最完整的 cascade 实现案例)
- LangChain `ContextualCompressionRetriever` 与 `CrossEncoderReranker` 源码
- LlamaIndex `LLMRerank` / `SimilarityCutoffPostprocessor` 文档
- Pinecone Reranking 教程
- Qdrant + FastEmbed + Rerank 集成教程
- ONNX Runtime 的 BERT 量化推理示例

**评测 / 基准:**

- BEIR Benchmark — 跨 18 个数据集的零样本检索评测, reranker 选型直接看 leaderboard
- MTEB Reranking sub-leaderboard — HuggingFace 维护, 主流 reranker 横评
- MS MARCO Passage Ranking — 检索社区基准, 多数 reranker 在这上面预训练
- TREC Deep Learning Track — 学术标准评测, NDCG@10 是金标
- T2Reranking — 中文检索评测, 国内 reranker 选型必看
- CMTEB — 中文 MTEB, 含中文 reranking 子任务

---

## 写在最后

reranker 不是银弹, 是一把 "把粗排的网捞回来的鱼再挑一遍" 的精排刀。它的天花板由几件事共同决定:

1. **rerank 是粗排的精排刀, 不是粗排的替代品** —— 粗排召回不够, 再准的 reranker 也救不了
2. **阈值的标定比模型选型还难** —— sigmoid 中间地带、保守模型、小语料三件事一叠就废
3. **宽 catch 兜不住 SystemExit** —— 库里别拿 SystemExit 当业务错, 调用方降级路径会全废
4. **可观测性是后续所有改进的地基** —— 没有 JSONL 日志, 所有优化都靠猜
5. **评测集是所有调优的真相** —— Recall@10 / NDCG@5 / MRR 没数, 上 reranker 跟没上效果一样
6. **多样性跟相关性是两件事** —— rerank 后的 MMR 不是锦上添花, 是 top-k 真用起来的必选项

最后一句送给所有要做 RAG 的朋友: **rerank 给你的不是 "更准的 top-1", 是 "你终于知道 top-1 凭什么排第一"**。把分数的语义、阈值的逻辑、降级的路径、评测的指标一件件搞清楚, 比换一个更大的 reranker 模型管用得多。

希望这份指南对你有帮助。

— *本文档遵循 [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) 协议, 可自由引用、修改、再发布, 保留出处即可。欢迎根据你自己的实战经验提 issue 或 PR。*
