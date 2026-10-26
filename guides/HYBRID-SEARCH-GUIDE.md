# 混合检索 (Hybrid Search) 实战指南

> 一份给所有正在做 RAG 检索的朋友看的、共同进步的策略对比手册。
>
> 本文聚焦一件事:**当稠密向量检索遇到天花板时,怎么用 BM25 救场,以及怎么把两路结果融合得漂亮。**
> 适合已经做过纯向量检索、想再上一个台阶的工程师阅读。

---

## 目录

- [前言:为什么单纯向量检索不够](#前言为什么单纯向量检索不够)
- [第一部分:稀疏检索基础 — BM25 是啥](#第一部分稀疏检索基础--bm25-是啥)
  - [1. 从 TF-IDF 到 BM25](#1-从-tf-idf-到-bm25)
  - [2. BM25 公式拆解](#2-bm25-公式拆解)
  - [3. BM25 跟向量检索的本质差异](#3-bm25-跟向量检索的本质差异)
- [第二部分:稀疏索引引擎的选型](#第二部分稀疏索引引擎的选型)
  - [1. 内存 BM25 库 (rank-bm25 / bm25s)](#1-内存-bm25-库-rank-bm25--bm25s)
  - [2. Lucene 系 (Elasticsearch / OpenSearch / Solr)](#2-lucene-系-elasticsearch--opensearch--solr)
  - [3. Tantivy 系 (Rust)](#3-tantivy-系-rust)
  - [4. 一体化向量+稀疏引擎 (Vespa / Qdrant / Weaviate / Milvus)](#4-一体化向量稀疏引擎-vespa--qdrant--weaviate--milvus)
  - [5. PostgreSQL 全文检索 (tsvector / pg_trgm / RUM)](#5-postgresql-全文检索-tsvector--pg_trgm--rum)
  - [6. 选型决策树](#6-选型决策树)
- [第三部分:中文分词决定 BM25 上限](#第三部分中文分词决定-bm25-上限)
  - [1. 为啥英文不用考虑这事](#1-为啥英文不用考虑这事)
  - [2. 主流中文分词方案对比](#2-主流中文分词方案对比)
  - [3. 词典 / 模型 / 混合三类技术路线](#3-词典--模型--混合三类技术路线)
  - [4. 分词没切对会出现什么病症](#4-分词没切对会出现什么病症)
- [第四部分:多路结果融合算法](#第四部分多路结果融合算法)
  - [1. 为啥不能直接把分数加起来](#1-为啥不能直接把分数加起来)
  - [2. RRF (Reciprocal Rank Fusion) — 最稳的入门选](#2-rrf-reciprocal-rank-fusion--最稳的入门选)
  - [3. CombSUM / CombMNZ — 分数归一后加权](#3-combsum--combmnz--分数归一后加权)
  - [4. Learned-to-Rank — 用模型学权重](#4-learned-to-rank--用模型学权重)
  - [5. 一些工程上的小动作](#5-一些工程上的小动作)
- [第五部分:工程一体化 — 双索引怎么活下去](#第五部分工程一体化--双索引怎么活下去)
  - [1. 谁来保证两路索引一致](#1-谁来保证两路索引一致)
  - [2. 增量更新的三种策略](#2-增量更新的三种策略)
  - [3. 故障注入:某一路挂了怎么办](#3-故障注入某一路挂了怎么办)
  - [4. 可观测性:每路召回该埋哪些日志](#4-可观测性每路召回该埋哪些日志)
- [第六部分:本项目当前实现 vs 生产](#第六部分本项目当前实现-vs-生产)
- [附录 A:术语小词典](#附录-a术语小词典)
- [附录 B:进一步阅读](#附录-b进一步阅读)

---

## 前言:为什么单纯向量检索不够

如果你只跑过 `vector_store.search(query, top_k=3)`,大概率会碰到这两类灵异场景:

**场景一:专有名词查不到。** 用户问"Chroma 怎么持久化",知识库里明明有 "ChromaDB PersistentClient" 的相关段落,但 top-3 全是无关内容。原因是 embedding 模型在预训练语料里没见过 "Chroma" 这个产品名,语义空间里它孤零零地落在某个角落,跟"持久化"这个语义距离很远。BM25 这时候直接靠字面命中,一秒搞定。

**场景二:语义对了字面不对。** 用户问"模型怎么训",资料里有"如何调参微调底座",纯字面检索 BM25 一个词都不沾,但向量检索能稳稳召回。

工业界的共识:**这俩谁都替代不了谁,Hybrid 是必选项。** 微软的 Bing、Google 的 Search、Elasticsearch 8.x 的 kNN+BM25、Vespa 的 nativeRank、Qdrant/Weaviate 的 Hybrid Search,本质都是这条路。

Hybrid 的核心三件事,就是本文的主线:

1. **BM25 那一路怎么搭** (引擎 + 分词)
2. **多路结果怎么融合** (RRF / 加权 / LTR)
3. **工程上双索引怎么活下去** (一致性 / 增量 / 故障)

---

## 第一部分:稀疏检索基础 — BM25 是啥

### 1. 从 TF-IDF 到 BM25

经典文本检索三十年都在围绕一个问题打转:**给定 query 和文档,怎么算"匹配度"?**

最朴素的答案是 **TF (Term Frequency)**:某个词在文档里出现 5 次,比出现 1 次更匹配。但这立刻有问题 —— "的""is""the"这种停用词哪里都是高频,贡献的信息量却几乎为零。

于是补上 **IDF (Inverse Document Frequency)**:一个词在整个语料里越罕见,它命中时含金量越高。"Elasticsearch" 全语料只出现 3 次,一旦命中就强信号;"the" 全语料 10 万次,命中等于没命中。

TF × IDF 就是 **TF-IDF**,1972 年发表,统治了一二十年的搜索引擎。但 TF-IDF 有两个明显短板:

1. **TF 线性增长不合理。** 词出现 100 次的文档不应该比出现 10 次的文档"重要 10 倍",边际效益递减是直觉。
2. **没考虑文档长度。** 长文档随便就累积大量 TF,短文档天生吃亏。

BM25 (Best Matching 25,出自 1994 年 TREC-3 论文) 就是来修这两个洞的。

### 2. BM25 公式拆解

```
score(D, Q) = Σ IDF(qi) · ( f(qi, D) · (k1 + 1) ) / ( f(qi, D) + k1 · (1 - b + b · |D|/avgdl) )
```

吓人是吓人,拆开就 4 个东西:

- **IDF(qi)** — query 词 qi 的逆文档频率。罕见词权重高,完全继承自 TF-IDF。
- **f(qi, D)** — 词 qi 在文档 D 里出现的次数 (Term Frequency)。
- **k1** — 控制 TF 饱和速度。**k1 越小,越早饱和**(2 次跟 20 次差不多)。默认 1.2。
- **b** — 控制文档长度归一化的强度。**b=0** 完全忽略长度,**b=1** 完全归一化。默认 0.75。
- **|D| / avgdl** — 文档 D 的长度除以平均文档长度。

两个关键的"修复"动作:

- 分子里的 `f · (k1+1)` 配合分母里的 `f + k1·...`,让 TF 增长曲线变成**饱和递增**:从 0 涨到 10 次进步明显,从 50 涨到 100 几乎没差别。这就是"边际效益递减"的数学体现。
- `(1 - b + b · |D|/avgdl)` 是长度惩罚因子。长文档分母变大,得分被压;短文档不被惩罚。

`k1` 和 `b` 的默认值 (1.2 / 0.75) 是 TREC 数据集上调出来的,大多数场景不用动。真要调,**短文档 (Tweet/标题) 调小 b**,**长文档 (论文/书) 维持默认**。

### 3. BM25 跟向量检索的本质差异

| 维度 | BM25 (稀疏) | 向量检索 (稠密) |
|---|---|---|
| **索引内容** | 倒排表:词 → 出现该词的文档列表 | 高维浮点向量 + ANN 索引 (HNSW/IVF) |
| **匹配机制** | 字面 token 重叠 | 语义空间距离 |
| **专有名词** | 强 (直接命中) | 弱 (训练语料没见过的词) |
| **同义词** | 弱 (字面不同就废) | 强 (语义近就能召回) |
| **多语言** | 需要分词器适配 | 跨语言 embedding 天生能跨语言 |
| **可解释性** | 强 (能指出命中了哪些词) | 弱 (一个分数说明不了啥) |
| **冷启动** | 0 训练 (语料即模型) | 需要训练/下载 embedding 模型 |
| **资源消耗** | CPU 友好,内存 ~ 词表大小 | embedding 推理要 GPU,索引大 |

记住一个直觉:**BM25 是"精确的笨蛋",向量是"模糊的聪明人"**。前者不会扩展但说一不二,后者会联想但常常跑偏。Hybrid 就是让它俩一起干活。

---

## 第二部分:稀疏索引引擎的选型

### 1. 内存 BM25 库 (rank-bm25 / bm25s)

**代表**: `rank-bm25` (Python, ~500 行)、`bm25s` (Python, 优化版,2024 出)

**特点**:
- 纯 Python / Cython,装上就用,无外部依赖
- 索引整个塞内存里
- 不支持持久化、不支持增量(`rank-bm25` 加一条要全部重建)
- 不支持复杂查询 (布尔、短语、模糊)

**什么时候用**:
- 学习项目、原型、POC
- 数据规模小 (< 几万块) 且重启可以接受秒级冷启动
- 想完全跟其他系统解耦,自己维护索引状态

**生产线上慎用**:数据上 10 万行,重建一次 BM25Okapi 几秒到几十秒,这段时间检索全挂。

### 2. Lucene 系 (Elasticsearch / OpenSearch / Solr)

**代表**: Elasticsearch、OpenSearch (亚马逊 fork)、Solr (Apache)

**特点**:
- Lucene 是底层 Java 引擎,1999 年诞生,Apache 旗舰项目
- ES/OpenSearch 把它包成分布式 REST 服务,Solr 是另一套外壳
- 倒排索引 + 段合并 + 持久化全套
- 支持布尔、短语、模糊、范围、聚合,功能富到吓人
- ES 8.x 起原生支持 kNN,直接一份数据双索引

**生产标杆配置**:
```http
POST /docs/_search
{
  "query": {
    "match": { "content": "查询字符串" }      // BM25
  },
  "knn": {
    "field": "embedding",
    "query_vector": [...],                     // 稠密向量
    "k": 30
  },
  "rank": { "rrf": { "rank_window_size": 100, "rank_constant": 60 } }  // 内置 RRF
}
```

一条请求,Hybrid 全做完。这就是为啥 ES 是行业标杆。

**代价**:
- JVM 内存吃不消(生产至少 8GB 起,典型 32GB+)
- 集群运维不轻 (master 节点、数据节点、协调节点、ILM 策略)
- 学习曲线陡 (DSL / mapping / analyzer / index template)

**适合**:数据规模 10 万 ~ 10 亿、有运维团队、需要复杂查询。

### 3. Tantivy 系 (Rust)

**代表**: Tantivy (Rust 版 Lucene)、Meilisearch (基于 Tantivy 的搜索引擎)、Qdrant (向量库,文本部分用 Tantivy)

**特点**:
- Rust 写的全文检索引擎,API 神似 Lucene
- 单机性能比 ES 还猛 (无 JVM 开销 + 编译期优化)
- 嵌入式部署,作为库链接进应用
- 持久化、增量更新都有

**适合**:
- 单机大数据量 (千万级文档)
- 不想搞集群,但又要持久化和增量
- 写 Rust / 跟 Rust 生态共存

**不适合**:需要分布式扩缩容时还是要回 ES。

### 4. 一体化向量+稀疏引擎 (Vespa / Qdrant / Weaviate / Milvus)

**Vespa**: Yahoo 出品,**唯一一个原生支持 BM25 + 向量 + LTR 三位一体**的开源引擎。配置 yaml 一写,排序公式自由表达 `score = 0.6 * bm25 + 0.4 * vector + 0.05 * recency`。规模能撑到百亿级。**学习曲线最陡**(自有 query language 和 ranking profile 概念)。

**Qdrant**: Rust 写的向量库,从 1.10 起原生支持 BM25 (用 Tantivy)。Hybrid 一行 API 解决。**最容易上手的生产级 Hybrid**。

**Weaviate**: Go 写的向量库,Hybrid Search 是核心卖点之一 (`hybrid: { query, alpha: 0.5 }` alpha 控制稀疏/稠密权重)。

**Milvus**: 主打稠密向量,2.4 起加了 sparse vector 字段支持 BM25 风格稀疏检索。

**适合**: 不想自己搭 ES + 向量库两套系统,要一份数据双索引自动同步的场景。

### 5. PostgreSQL 全文检索 (tsvector / pg_trgm / RUM)

**特点**:
- 数据库自带的 `tsvector` + `to_tsquery` 就能做基础全文检索
- `pg_trgm` 扩展加模糊匹配
- `RUM` 扩展加倒排索引相位信息 (短语检索)
- 跟 pgvector 配合,**一个数据库做完 Hybrid**

**适合**:
- 数据量小 (< 千万)
- 已经用 PG 不想引新组件
- 团队 SQL 比 DSL 熟

**不适合**: 想要高级 BM25 调参或十亿级规模。

### 6. 选型决策树

```
你的数据量是多少?
├── < 10 万块  → 内存 BM25 (rank-bm25 / bm25s)
├── < 1000 万  → Tantivy (单机) / PostgreSQL (嵌入业务库)
├── < 10 亿   → Elasticsearch / OpenSearch / Qdrant / Weaviate
└── > 10 亿   → Vespa / Milvus 大集群 / 自建 Lucene 分片

你的团队会啥?
├── Java/JVM 强  → ES 全家桶
├── Rust/Go 强   → Tantivy / Qdrant
├── 只想用 SQL   → PostgreSQL + pgvector
└── 全栈轻量化   → Qdrant (Rust 内核,一键 Hybrid)

你想要什么级别的 Hybrid?
├── 简单加分融合 → 任意引擎 + 自己 RRF
├── 内置 Hybrid  → Qdrant / Weaviate / ES 8.x
└── LTR 可调排序 → Vespa
```

---

## 第三部分:中文分词决定 BM25 上限

### 1. 为啥英文不用考虑这事

英文用空格分词,一秒搞定。`"natural language processing"` → `["natural", "language", "processing"]`,字面命中精确无歧义。

中文是连续字符流。`"自然语言处理"` 可以切成 `["自然", "语言", "处理"]`,也可以是 `["自然", "语言处理"]`,甚至 `["自然语言", "处理"]`。**切得不一样,BM25 召回的文档就完全不同**。

更要命的是:**索引时怎么切,查询时也得用同样的切法**,否则字面对不齐永远召回不到。所以分词器在 BM25 链路里跟"压缩算法"一样,索引侧和查询侧必须严格对称。

### 2. 主流中文分词方案对比

| 方案 | 准确率 | 速度 | 词典/模型 | 维护方 |
|---|---|---|---|---|
| **jieba** | ~92% | 中 | 词典 + HMM 兜底 | 个人开源,2012 ~ 至今 |
| **THULAC** | ~94% | 快 | 模型 (感知机) | 清华大学 |
| **pkuseg** | ~95% | 中 | 模型 (神经网络) | 北京大学 |
| **HanLP** | ~96% | 中 | 模型 + 大词典 | 何晗,商业开源 |
| **LAC (百度)** | ~96% | 快 | 模型 (LSTM/Transformer) | 百度 PaddleNLP |
| **ICTCLAS** | ~95% | 快 | 词典 + 隐马 | 中科院 |
| **IK Analyzer** | ~90% | 极快 | 词典 | ES 插件,Java 生态 |
| **Jieba-rs** | ~92% | 极快 | 同 jieba | jieba 的 Rust 重写 |

**几个常被问的判断:**

- "jieba 够用吗?" → 学习项目 / 中等准确率场景够,严肃业务上 HanLP 或 LAC。
- "HanLP 和 LAC 怎么选?" → HanLP 功能多 (NER / 句法 / 摘要 / 拼音);LAC 速度快、对短文本和口语好。
- "ES 用啥分词器?" → IK 是 ES 中文事实标准,装插件就能用,但准确率不如 LAC/HanLP。讲究的会自己接 LAC 当外部 service。
- "想要极致速度?" → Jieba-rs 或 ICTCLAS C++ 版,百万 QPS 能扛。

### 3. 词典 / 模型 / 混合三类技术路线

**词典派** (jieba 默认模式 / IK):
- 维护一个大词典 (几万到几十万词条),正向最大匹配 / 双向最大匹配
- 优点:快、可控、加词即生效
- 缺点:新词死活切不出来,歧义切分靠运气
- 代表场景:**搜索引擎索引侧** (要快、可解释)

**模型派** (LAC / pkuseg / HanLP 默认):
- 把分词当序列标注 (BIES: 词首/词中/词尾/单字),用 CRF / LSTM / Transformer 学
- 优点:新词识别强、歧义切分准
- 缺点:慢、不可解释、加新词要重训或加 finetune
- 代表场景:**离线 NLP 流水线**

**混合派** (jieba 全模式 / HanLP):
- 词典做主干,模型 (HMM/感知机) 兜底未登录词
- 工程上最常见,平衡了速度和准确

**生产选型口诀**: **检索侧短文本快,优先词典 + 自定义词表;离线分析重精度,上 LAC/HanLP**。

### 4. 分词没切对会出现什么病症

| 病症 | 原因 | 修法 |
|---|---|---|
| **专有名词召不回** | "ChromaDB" 被切成 `["Chroma", "DB"]`,而索引侧是 `["ChromaDB"]` | 加自定义词典 |
| **缩写/版本号丢失** | "Python3.12" 被切成 `["Python", "3.12"]`,查询时被 `to_tsquery` 标准化掉数字 | 配置不切分数字、英文 |
| **同义词不通** | "笔记本" 召不回 "电脑" | BM25 解决不了,这是同义词扩展的活 |
| **大小写不一致** | "Elasticsearch" 索引侧小写化,查询侧没,字面对不齐 | 索引和查询都强制小写 |
| **停用词漏过** | "的""了""and" 进了倒排表,IDF 低但占空间,query 里也被当词命中 | 配置 stopword 过滤 |
| **召回过多噪音** | 切成单字 `["自","然","语","言"]`,任何含这些字的文档都匹配 | 检查分词器是否退化到单字模式 (jieba.cut(s, cut_all=True) 误用) |

---

## 第四部分:多路结果融合算法

### 1. 为啥不能直接把分数加起来

BM25 分是绝对值,典型范围 [0, 30]。Vector cosine 是 [0, 1]。直接相加就是 BM25 一家独大。

可以归一化?可以,但 BM25 没有理论上界,每个 query 的分数分布都不一样,归一化窗口口径选取本身就是个调参问题。**归一化越仔细,系统越脆**:今天换个语料,昨天的归一化参数全废。

所以业界主流方案分两类:

- **看排名不看分数** (RRF) — 最稳,鲁棒到无脑
- **归一化后加权** (CombSUM/CombMNZ) — 灵活但易翻车

### 2. RRF (Reciprocal Rank Fusion) — 最稳的入门选

公式简单到一行:

```
score(d) = Σ over all retrievers r:  1 / (k + rank_r(d))
```

- `rank_r(d)`:文档 d 在检索器 r 的结果列表里排第几 (从 1 起算)
- `k`:常数,Cormack 原论文推荐 60,Elasticsearch / Vespa 默认 60

为啥这玩意儿好用:

- **跟具体分数无关**,只看排名 → 跨检索器尺度自动对齐
- **k 控制低排名的影响**:k 大,排第 1 和排第 100 的差别变小;k 小,排首位的强势放大
- **没有可调参数(实质上)**,k=60 在公开数据集上几乎都接近最优

代码就这几行:

```python
def rrf_fuse(ranked_lists: list[list[str]], k: int = 60) -> list[tuple[str, float]]:
    scores: dict[str, float] = {}
    for ranked in ranked_lists:
        for rank, item_id in enumerate(ranked, start=1):
            scores[item_id] = scores.get(item_id, 0.0) + 1.0 / (k + rank)
    return sorted(scores.items(), key=lambda x: x[1], reverse=True)
```

**RRF 的工程优势**:
- 任意路检索器都能融合 (BM25 + vector + recency + popularity + ...)
- 加新路只需新加一个 ranked_list,代码零改
- 实验对照容易做 (开/关某一路重跑就行)

**RRF 的局限**:
- 信息有损 (只看排名扔了分数,极端高分和及格分等价)
- 对"一致排在前 10 名但本身就是噪音"的文档无能为力

ES、OpenSearch、Vespa、Qdrant、Weaviate 内置融合默认都是 RRF。**不知道选啥就选它,不用调参。**

### 3. CombSUM / CombMNZ — 分数归一后加权

**CombSUM**: `score = Σ normalize(score_r(d))`
**CombMNZ**: `score = (出现路数) × Σ normalize(score_r(d))`

CombMNZ 多乘了个"被多少路命中",鼓励双边命中。归一化方法常见:

- **Min-Max**: `(s - min) / (max - min)`,简单但对异常值敏感
- **Z-Score**: `(s - mean) / std`,假设分数高斯分布
- **Soft-max**: 把分数变成概率,跟温度参数挂钩

**用 CombSUM/CombMNZ 的场景**:
- 各路分数有可靠的范围保证 (eg. 都是 [0,1] 概率)
- 业务明确"双边命中应该加分" (CombMNZ)
- 有评测集能持续调权重

**坑**: 真线上分数分布会因为语料变化、模型升级、query 长度变化不断漂移,你昨天调好的权重明天就废了。**没评测集和监控的话别上 CombSUM。**

### 4. Learned-to-Rank — 用模型学权重

**思路**: 把多路 score 当特征,加上其他信号 (点击、停留、CTR),用监督学习训一个 ranker。常用算法:

- **Pointwise**: 每条文档独立打分 (LinearRegression / GBDT)
- **Pairwise**: 学"A 比 B 更相关" (RankNet / LambdaRank)
- **Listwise**: 直接优化整个排序的 NDCG (LambdaMART, XGBoost-rank)

**Vespa** 把 LTR 当一等公民,ranking profile 里直接写 ML 公式。**ES** 通过 Learning-to-Rank 插件支持。

**前置条件**:
- **有点击日志或人工标注** → 没标注,LTR 无源之水
- **流量够大** → 流量小训不出稳定模型
- **有线上 A/B 实验框架** → 没的话,LTR 不可知是好是坏

**学习项目和中小业务不必上**。RRF 起步,等到 RRF 的天花板真碰到了,再想 LTR。

### 5. 一些工程上的小动作

- **候选数量**: 每路召回 `top_k * 4` 左右给融合层一点排序空间,太少没用、太多浪费
- **去重**: 多路结果可能 id 重复,合并后基于唯一 id 去重再排序
- **rank 起点**: RRF 公式 rank 从 1 起,不要从 0
- **空结果**: 某一路返回空,RRF 自动退化为单边检索 (零贡献),不需要特判
- **分阶段融合**: 大系统先 RRF 粗融合,再 cross-encoder rerank,再业务排序 (recency / popularity)

---

## 第五部分:工程一体化 — 双索引怎么活下去

### 1. 谁来保证两路索引一致

**症状**: BM25 索引里有 100 万块,向量库里只有 99 万 (新增数据时一边写成功一边写失败),查询时召回错位。

**生产解法 (按推荐顺序)**:

**方案 A: 一份数据双索引** (Vespa / Qdrant / Weaviate / ES 8.x 同 doc)
- 写入只有一个事务,引擎内部保证两路索引同步
- 最省心,但绑死引擎

**方案 B: 共享存储,异步同步** (Postgres / Kafka 当源,ES + 向量库订阅)
- 业务只写一处源数据库,下游 BM25 和向量库各自消费日志构建索引
- 容许短暂不一致 (秒级到分钟级),用日志回放补
- 代表:Debezium → Kafka → ES + Milvus

**方案 C: 应用层双写** (我们项目走的就是这条)
- 应用代码里同时调 `vector_store.add()` 和 `bm25_index.add()`
- 实现简单,但容错麻烦:一边成功一边失败要回滚或重试
- 适合学习项目和单机部署,生产慎用

**方案 D: 检索时旁路加 BM25** (LangChain 那种 Ensemble Retriever)
- 向量库正常,BM25 临时从内存拿全量数据现算
- 适合小数据,大数据会拖死延迟

### 2. 增量更新的三种策略

**策略 1: 全量重建** (最简单)
- 数据有变就重建整个 BM25 索引
- 我们项目目前这么干,~100 块重建毫秒级
- 数据上千就疼,上万就废

**策略 2: 段合并** (Lucene/Tantivy 默认)
- 新数据写新段 (segment),后台异步合并
- 查询时多段并发再合并
- 持久化 + 高可用都有,但实现复杂,不该自己写

**策略 3: 双索引切换** (大改时用)
- 新索引在后台从 0 灌好,前台仍用旧索引
- 灌完原子切换 (一个引用变更)
- 适合切分词器、改 mapping 这种需要重建全表的场景

### 3. 故障注入:某一路挂了怎么办

| 故障 | 影响 | 对策 |
|---|---|---|
| **BM25 节点挂了** | 字面命中能力丢失 | 走 vector-only,降级 (我们项目自然就这样) |
| **向量模型 down 了** | 语义命中能力丢失 | 走 BM25-only,降级 |
| **两路都挂** | 检索全废 | 兜底返回"系统繁忙",**绝不能去问 LLM**(没资料一定幻觉) |
| **某路超时**| 拖累整体延迟 | 各路独立超时,超时即跳过该路结果 |
| **某路分数异常**| 排序紊乱 | 加范围 sanity check,异常分数走告警 |

**关键设计**: **两路并发,谁先回来谁的先用,慢的拖了到截止时间就放弃**。不要串行,任一路慢就拖垮整体。

### 4. 可观测性:每路召回该埋哪些日志

生产标配 (按重要程度):

1. **每路的 top-k id 列表** — 必埋,回溯时一目了然
2. **每路的耗时** — 哪一路是瓶颈
3. **融合后的最终 id + 分数** — 跟用户最终看到的对得上
4. **某一路为空的频率** — BM25 老是空说明分词器或词表有问题
5. **某一路命中数 / 候选数** — 召回质量监控
6. **重排前后变化率** — 加 rerank 之后,top-1 变化率高 = rerank 起作用了;变化率低 = rerank 是浪费

**我们项目埋的事件**: `retriever.fuse`,字段:
```json
{
  "query": "...",
  "top_k": 3,
  "bm25_topk": ["...::0", "...::1", "..."],
  "vector_topk": ["...::0", "..."],
  "rrf_merged": [{"id": "...", "score": 0.0328}, ...],
  "elapsed_ms": 249
}
```

后期接 rerank 再加 `reranker.rerank` 事件;接 LLM 再加 `llm.call` 事件。这套日志 + 时间戳能直接做时序分析,问题不慌。

---

## 第六部分:本项目当前实现 vs 生产

到这里把全文知识点对照一下,看看我们项目的退化点和补齐方向。

### 当前实现 (阶段 1.4 完工状态)

```
HybridRetriever
├── VectorStore (ChromaDB, persistent)
└── BM25 内存索引
    ├── tokenize_zh = jieba.lcut + 过滤标点空白 + 英文小写
    ├── BM25Okapi (rank-bm25 库)
    └── 启动时从 ChromaDB 拉全量 documents → 重建

search(query, top_k=3):
  1. 向量召回 top_k * 4 候选
  2. BM25 召回 top_k * 4 候选 (jieba 切 query)
  3. RRF (k=60) 融合
  4. 返回 top_k, score 字段填 cosine (BM25-only 填 0.0)
```

### 退化点清单 (诚实版)

| 维度 | 当前 | 生产级 | 差距程度 |
|---|---|---|---|
| **BM25 引擎** | rank-bm25 (Python, 内存, 全量重建) | Elasticsearch / Tantivy / Qdrant 内置 | ⭐⭐⭐⭐ |
| **持久化** | 无,每次启动从 store 重建 | Lucene 段合并持久化 | ⭐⭐⭐ |
| **增量** | 全量重建 | 段合并增量 | ⭐⭐⭐ |
| **分词** | jieba 默认模式,~92% | HanLP / LAC,~96% | ⭐⭐ |
| **自定义词典** | 没配 | 业务专名词全部进词典 | ⭐⭐ |
| **融合算法** | RRF k=60 写死 | RRF + 业务侧 k 调参,或 LTR | ⭐ |
| **双索引一致性** | 应用层双写,无回滚 | 一份数据双索引 / 日志回放 | ⭐⭐⭐ |
| **故障降级** | 没设计 | 各路独立超时 + 降级路径 | ⭐⭐⭐ |
| **可观测性** | retriever.fuse 一个事件,字段够用 | 同款,加各路命中率 / 重排变化率 | ⭐ |

⭐ 数越多差得越远。

### 补齐路线 (按 ROI 排序)

如果有一天这个项目要上线,补齐顺序应该是:

1. **先上 rerank** (我们的下一步 1.5):一个 cross-encoder 抵得过 BM25/向量很多调参,投入最小回报最大
2. **加自定义词典**:把项目专名词、产品名、缩写灌进 jieba 用户词典,一行配置准确率明显涨
3. **换 Qdrant 一体化**:数据上 10 万就考虑,免去自己同步双索引的运维负担
4. **加日志埋点的覆盖率**:命中率 / 重排变化率 / 各路耗时分布
5. **加降级路径**:某一路挂了走单边,而不是整个 search 抛异常
6. **再考虑分词升级**:换 LAC / HanLP 准确率涨,但工程改动比上面几条都大

LTR 在很后面,十亿级流量、有点击日志、有 A/B 实验框架的体量再说。

---

## 附录 A:术语小词典

- **BM25 (Best Matching 25)**:1994 年的概率信息检索模型,大多数搜索引擎的默认评分函数
- **TF (Term Frequency)**:词频
- **IDF (Inverse Document Frequency)**:逆文档频率,罕见词权重高
- **倒排索引 (inverted index)**:词 → 文档列表的映射,稀疏检索的核心数据结构
- **段 (segment)**:Lucene 把索引分成的小块,可独立写入合并
- **稠密向量 (dense vector)**:每一维都有值的低维浮点向量,语义检索的代表
- **稀疏向量 (sparse vector)**:维度极高但大多数为零的向量,BM25/TF-IDF 的代表
- **HNSW**:Hierarchical Navigable Small World,主流 ANN 索引算法
- **ANN (Approximate Nearest Neighbor)**:近似最近邻,牺牲精度换速度
- **RRF (Reciprocal Rank Fusion)**:基于排名的多路结果融合
- **CombSUM / CombMNZ**:基于归一化分数的多路融合方法
- **LTR (Learning to Rank)**:用监督学习训练排序模型
- **NDCG (Normalized Discounted Cumulative Gain)**:排序质量评价指标,LTR 优化目标
- **Cross-encoder**:把 query 和 doc 拼起来一起过模型的精排方案,精度高速度慢
- **Bi-encoder**:query 和 doc 分别 embedding 再算相似度,速度快精度次之
- **召回 (recall)**:相关结果中有多少被检索回来了
- **精度 (precision)**:检索回来的结果里有多少是真相关的
- **分词 (tokenization / segmentation)**:把文本切成 token 的过程,中文检索的咽喉
- **停用词 (stop words)**:不携带语义的高频词 ("的""了""and""the")
- **未登录词 (OOV, Out-Of-Vocabulary)**:词典里没有的新词、专名

---

## 附录 B:进一步阅读

**论文 / 原始资料:**
- *Okapi at TREC-3* (Robertson et al., 1994) —— BM25 的原始论文
- *Reciprocal Rank Fusion outperforms Condorcet and individual Rank Learning Methods* (Cormack et al., 2009) —— RRF 提出的关键论文
- *BM25 and Beyond* (Robertson & Zaragoza, 2009) —— BM25 经典综述
- *Learning to Rank for Information Retrieval* (Liu, 2009) —— LTR 综述
- *Dense Passage Retrieval for Open-Domain Question Answering* (Karpukhin et al., 2020) —— DPR 稠密检索奠基

**工程实践 / 文档:**
- Elasticsearch 官方 "Hybrid search with kNN and BM25" 文档 (8.x)
- Qdrant 的 Hybrid Search 教程
- Weaviate 的 Hybrid Search 文档
- Vespa 的 Ranking Expression 文档 (开源中最完整的 LTR 实现案例)
- LangChain EnsembleRetriever 源码 (Python 工程参考)
- Cohere 的 Rerank API 使用指南

**中文分词资料:**
- jieba 项目 README
- pkuseg-python 项目
- HanLP 文档站
- 百度 LAC 项目

**评测:**
- BEIR Benchmark (零样本检索评测,Hybrid 跟单路检索的对比有现成数字)
- MTEB Leaderboard (embedding 模型对比)
- 中文检索:CMTEB / T2Reranking 数据集

---

## 写在最后

Hybrid 不是银弹,是一个"BM25 和 Vector 互相补盲"的工程妥协。它的天花板由几件事共同决定:

1. **BM25 引擎选对** —— 学习项目内存库无所谓,生产上别用 Python 内存库
2. **中文分词配对** —— 用 jieba 默认就够入门,上严肃业务请配自定义词典 + 模型派分词器
3. **融合算法保守一点** —— RRF k=60 起步,**没评测集不要调成 CombSUM 加权**
4. **双索引一致性方案选对** —— 应用层双写适合学习,生产请用一体化引擎或日志回放
5. **加可观测性** —— 每路 top-k、每路耗时、融合前后变化,出问题不慌

最后一句送给所有要做 RAG 的朋友: **越是看起来简单的索引技术 (BM25 是 1994 年的方法),工程化到生产可用越花心思。** 别因为 BM25 概念上"老",就低估它在 Hybrid 里的作用。

希望这份指南对你有帮助。

— *本文档遵循 [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) 协议,可自由引用、修改、再发布,保留出处即可。欢迎根据你自己的实战经验提 issue 或 PR。*
