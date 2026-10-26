# RAG 对话记忆与 Query 改写实战指南

> 一份给所有正在做多轮 RAG 的朋友看的、共同进步的策略对比手册。
>
> 本文聚焦三件事:
> 1. **多轮对话里, 怎么把"那它怎么用"这种省略问句, 改成可以单独丢去检索的独立 query** (Query Rewrite)
> 2. **chunks 召回后, 怎么避开 LLM 的"中间塌陷"效应, 把高分块放在它真能看见的位置** (Lost in the Middle 重排)
> 3. **对话越聊越长, 怎么砍历史不让 token 爆掉, 同时不丢关键上下文** (History 截断)
>
> 适合已经跑通单轮 RAG (向量检索 + LLM 生成)、准备做对话化产品 (ChatBot / 助手 / Agent) 的工程师。

---

## 目录

- [前言: 单轮 RAG 跑通后, 多轮为什么炸](#前言-单轮-rag-跑通后多轮为什么炸)
- [第一部分: Lost in the Middle (重排)](#第一部分lost-in-the-middle-重排)
  - [1. 现象与论文背景](#1-现象与论文背景)
  - [2. 主流重排策略](#2-主流重排策略)
  - [3. odd-front / interleave 算法剖析](#3-odd-front--interleave-算法剖析)
  - [4. 选型对照表](#4-选型对照表)
- [第二部分: Query Rewrite (查询改写)](#第二部分query-rewrite-查询改写)
  - [1. 为什么多轮 RAG 必须改写](#1-为什么多轮-rag-必须改写)
  - [2. 五大流派](#2-五大流派)
  - [3. 工程实现要点](#3-工程实现要点)
  - [4. 失败回退的五种情况](#4-失败回退的五种情况)
  - [5. 选型对照表](#5-选型对照表-1)
- [第三部分: History 截断](#第三部分history-截断)
  - [1. 三种截断策略](#1-三种截断策略)
  - [2. tokenizer 精确 vs 字符估算](#2-tokenizer-精确-vs-字符估算)
  - [3. pinned messages 和摘要压缩](#3-pinned-messages-和摘要压缩)
  - [4. 截断顺序与单条超额](#4-截断顺序与单条超额)
- [第四部分: 工程整合 (三者怎么协作)](#第四部分工程整合-三者怎么协作)
  - [1. 调用顺序: rewrite → retrieve → reorder → LLM](#1-调用顺序-rewrite--retrieve--reorder--llm)
  - [2. config 开关与可观测性](#2-config-开关与可观测性)
  - [3. token 预算分配](#3-token-预算分配)
- [第五部分: 生产真实做法](#第五部分生产真实做法)
  - [1. LangChain ConversationalRetrievalChain](#1-langchain-conversationalretrievalchain)
  - [2. LlamaIndex CondensePlusContextChatEngine](#2-llamaindex-condenseplusconchatengine)
  - [3. 大厂自研: 摘要 + 滑窗 + pinned](#3-大厂自研-摘要--滑窗--pinned)
  - [4. 评测怎么做](#4-评测怎么做)
- [第六部分: 常见误区](#第六部分常见误区)
- [第七部分: 小型实现的常见坑 → 生产对策](#第七部分小型实现的常见坑--生产对策)
  - [7.1 常见坑](#71-常见坑)
  - [7.2 生产对策](#72-生产对策)
  - [7.3 综合对照表](#73-综合对照表)
- [第八部分: 补齐路线 (按 ROI 排序)](#第八部分补齐路线-按-roi-排序)
- [附录 A: 术语小词典](#附录-a术语小词典)
- [附录 B: 进一步阅读](#附录-b进一步阅读)
- [写在最后](#写在最后)

---

## 前言: 单轮 RAG 跑通后, 多轮为什么炸

如果你已经把单轮 RAG (向量检索 → 拼 prompt → LLM 生成) 跑通了, 第二天产品经理大概率会说: **"加个对话功能, 用户可以追问"**。这一句话听起来一行 if/else 就能搞定, 实际上会带出三个隐性问题, 任何一个不处理都会让你的 RAG 在多轮场景下从"还行"掉到"难用"。

**第一个问题: 用户第二轮起就开始省略。**

```
轮 1: 用户 "uv 是什么"     → 检索到 uv 文档 → LLM 答得不错
轮 2: 用户 "怎么用"         → 检索 "怎么用" 这三个字 → 命中一堆无关文档 → 幻觉
```

向量检索是"无状态"的, 它不知道"它"指什么。从用户视角看, "怎么用"是承接上文的自然提问, 但从检索器视角看, 这是个孤立的三字短句, 跟 "uv" 没任何关系。**这就是 query rewrite 要解决的事**: 在送进检索器之前, 把当前 query 改写成一个独立的、能脱离上下文存在的版本 ("uv 怎么用")。

**第二个问题: 召回 chunk 数变多后, LLM 开始"中间塌陷"。**

单轮 RAG 一般给 3-5 块。多轮 + 复杂任务 (代码生成 / 多文档对比) 经常一次塞 10-30 块。Liu et al. 2023 的 *Lost in the Middle* 用一个对照实验把这事讲清楚了: **同一份文档, 放在长 prompt 的开头或末尾时召回率高得多, 放在中间时召回率明显下降**, 形成一个 U 形曲线。模型不是"看不到中间"——它能复述, 但生成时的注意力分布严重偏向头尾。

```
召回率 (示意, 非真实数字)
 1.0 ●                                ●
     │\                              /│
 0.8 │ \                            / │
     │  \                          /  │
 0.6 │   ●        中间塌陷        ●   │
     │    \      ┌─────────┐     /    │
 0.4 │     ●─────●         ●─────●    │
     │                                │
     └────────────────────────────────┘
       1    3    5    7    9   11   13   ← 文档在 prompt 中的位置
```

所以 chunk 数一多, **简单按相关性降序拼 (`[资料 1]` 最相关, `[资料 N]` 最差) 就是把次要资料硬塞到 LLM 注意力最弱的位置**。重排 (reorder) 是把这条曲线反过来用——高分块放头尾, 低分块挪到中间, 让 LLM 的"看得到"和我们的"想让它看到"对齐。

**第三个问题: 对话越聊越长, token 越烧越多。**

`messages = [user_1, assistant_1, user_2, assistant_2, ...]`, 每轮都把全部历史发过去。10 轮过后, 输入 token 翻倍; 30 轮过后, 一些模型直接抛 "context length exceeded"。这是 **history 截断** 要解决的事——保留近期对话, 砍掉远期, 既保住"我们刚才聊过 X"的连贯感, 又让 token 不爆。

这三件事在工程上彼此独立, 但在产品体验上是一个整体。它们也是从单轮 RAG 走到对话产品最早遇到的三道坎, 在 LangChain、LlamaIndex 这些主流框架里都有对应的封装。本文逐一讲清。

---

## 第一部分: Lost in the Middle (重排)

### 1. 现象与论文背景

*Lost in the Middle: How Language Models Use Long Contexts* (Liu et al., 2023) 给出了一个非常干净的对照实验:

- 任务: 给 LLM 一段 prompt, 里面有 10/20/30 份文档, 其中只有一份包含答案。让 LLM 输出答案。
- 变量: 那份"含答案"的文档放在第几位 (位置 1, 5, 10, 20, ...)
- 指标: 答对率

结果: **答对率随位置呈 U 形**。开头几位和末尾几位答对率最高, 中间位置答对率最低, 最低点比头尾低 20-30 个百分点。这个现象在 GPT-3.5 / GPT-4 / Claude / 开源大模型上全部复现, 说明它不是单个模型的问题, 是当前 transformer 长上下文架构的共性。

为什么会这样? 学术界几种解释:

- **训练数据偏差**: 互联网文本的关键信息天然集中在文章开头 (lead) 和结尾 (conclusion), 模型在大量数据上学到了"开头和结尾值得多看一眼"。
- **位置编码退化**: RoPE / ALiBi 这类相对位置编码在长距离衰减明显, 中间位置的 token 跟 query 的"距离感"被拉远。
- **attention 稀疏化**: 长上下文里, 大多数 attention head 倾向把权重压在少数 token 上, 中间均匀分布的内容被"平均化", 信号弱。

具体哪个是主因还有争议, 但**现象稳定**——只要 prompt 里塞 ≥ 5 份文档, 重排就有正收益。短 prompt (≤ 3 份) 重排没什么必要, 因为没"中间"可言。

### 2. 主流重排策略

不限于 RAG, 长上下文场景的"位置优化"大致是这几招:

| 策略 | 做法 | 适用场景 |
|---|---|---|
| **odd-front interleave** | 偶数下标顺序 + 奇数下标倒序: `[s0, s2, s4, ..., s5, s3, s1]` | RAG 召回后重排, 最通用 |
| **head-tail 对称** | 高分放头, 次高放尾, 低分塞中间 | 适合 N ≤ 10 的精排场景 |
| **chunked re-ranking** | chunk 内按句子也做位置优化 | 长 chunk (> 1000 token) 的精细优化 |
| **adaptive interleave** | 根据 LLM context window 长度动态调整重排强度 | 多模型混部, 切模型不用改逻辑 |
| **不重排** | 按相关性降序拼 | N ≤ 4 时, 重排没意义 |

odd-front 是最常见的, LangChain `LongContextReorder` 默认就这么做, LlamaIndex `LongContextReorder` 后处理器也一样。它的好处是 **不依赖任何模型先验, 不引入新的延迟, 一个纯 list 操作就完成**, 几乎是零成本的优化。

### 3. odd-front / interleave 算法剖析

完整算法就一行 Python:

```python
def odd_front_reorder(chunks):
    """chunks 已按 score 降序排列。"""
    evens = chunks[0::2]            # [s0, s2, s4, ...]   高分占头
    odds_reversed = chunks[1::2][::-1]  # [..., s5, s3, s1]    次高分占尾
    return evens + odds_reversed
```

举例, 输入 5 个 chunk (score 从高到低):

```
原始 (按 score 降序):
  [s0=0.92, s1=0.85, s2=0.71, s3=0.62, s4=0.55]

odd-front 重排后:
  evens         = [s0=0.92, s2=0.71, s4=0.55]
  odds_reversed = [s3=0.62, s1=0.85]
  结果          = [s0=0.92, s2=0.71, s4=0.55, s3=0.62, s1=0.85]
                   ↑头部高分          ↑中间最低              ↑尾部次高
```

为什么这么做? 因为 LLM 的注意力分布是 "头部 > 尾部 > 中间"。把 score 最低的 0.55 放在中间, 它本来就最不可能贡献答案, 即使被"塌陷"也无所谓。把 score 第二高的 0.85 放在尾部, 利用尾部 attention 强的特性, 让它跟头部的 0.92 一起承担答题主力。

**6 个 chunk 的版本**:

```
原始: [s0, s1, s2, s3, s4, s5]
偶数下标: [s0, s2, s4]
奇数下标倒序: [s5, s3, s1]
结果: [s0, s2, s4, s5, s3, s1]
```

注意 s5 (最低分) 落在中段, s1 (次高分) 落在最尾——同样满足 "高分占头尾, 低分挪中间"。

**算法对延迟和成本的影响**: 几乎为零。一千个 chunk 也就 几微秒的 list slice 操作, 跟 LLM 调用 (秒级)、检索 (毫秒级) 比可以忽略。这就是为什么这步**几乎没有不做的理由**, 唯一的代价是: 如果 chunk 数太少 (< 5), 没必要重排, 加个 `min_chunks` 阈值保护一下就好。

### 4. 选型对照表

| 重排策略 | 实现复杂度 | 额外延迟 | 对 LLM 召回率提升 | 适用 chunk 数 |
|---|---|---|---|---|
| odd-front interleave | 1 行代码 | 0 ms | +5-15% (论文复现值) | 5-30 |
| head-tail 对称 | 5 行代码 | 0 ms | +3-10% | 5-10 |
| chunked re-ranking (句级) | 中等 | < 50 ms | +5-10% (额外) | 长 chunk |
| 不重排 (基线) | - | - | - | < 5 |

入门项目直接上 odd-front。生产项目可以叠 chunked re-ranking 做精细化。

---

## 第二部分: Query Rewrite (查询改写)

### 1. 为什么多轮 RAG 必须改写

向量检索是无状态的: 它只看当前 query 的向量, 跟历史向量空间互不相关。多轮对话里, 用户的当前 query 90% 以上会出现以下情况之一:

| 现象 | 例子 | 检索后果 |
|---|---|---|
| 代词指代 | "那 **它** 怎么用" | 不知道 "它" 是谁 |
| 主语省略 | "**也** 支持 Windows 吗" | "也" 隐含上文话题 |
| 话题延续 | (上文聊 A) "**有更好的方案吗**" | 检索 "更好的方案" 命中各种无关文档 |
| 反向追问 | "**你刚才说的第二点**" | 检索器看不到刚才说的 |

如果不改写, 检索器只能拿到当前这句话, **召回质量从单轮的 0.7+ 直接掉到 0.2-0.4**, RAG 形同虚设。

Query Rewrite 的本质是 **把对话状态从 LLM 的"记忆"里"显式化"出来, 喂给检索器**。从单轮到多轮, 这是一个机制上的必经之路。

### 2. 五大流派

社区里常见的 query rewrite 方案大致五类, 各有取舍:

#### 流派 1: Single Query Rewrite (单 query 改写)

最常见、最简单。用一次 LLM 把 (history + current query) 改写成一个独立 query, 然后用这个独立 query 走原本的检索器。

- **代表实现**: LangChain `ConversationalRetrievalChain` 的 `_get_chat_history` + `condense_question_prompt` 流程
- **优点**: 实现简单 (一次 LLM 调用), 不动检索器逻辑
- **缺点**: 只生成一个 query, 对"多面性"问题 (一个问题有多个检索角度) 召回不够全
- **典型代价**: 单次延迟 + 200-500 ms (一次小模型 LLM 调用), 单次 token 100-500

#### 流派 2: Multi-Query (多 query 改写)

让 LLM 一次生成 N 个不同角度的改写 query, 每个都跑一遍检索, 结果取并集 + 去重。

- **代表实现**: LangChain `MultiQueryRetriever`, LlamaIndex `QueryFusionRetriever`
- **优点**: 召回更全, 对"多面性"问题特别有效
- **缺点**: N 倍检索成本, 去重逻辑要写好 (chunk id 去重 / 内容相似度去重)
- **适用**: 高价值场景 (法律 / 医疗) 接受额外延迟换召回率

#### 流派 3: HyDE (Hypothetical Document Embeddings)

不改写 query, 而是让 LLM **假设性地生成一段"如果这个 query 有答案, 答案会长什么样"的文本**, 然后用这段假设答案的向量去检索, 而不是 query 本身的向量。

- **代表论文**: *Precise Zero-Shot Dense Retrieval without Relevance Labels* (Gao et al., 2022)
- **核心思路**: query 向量跟 doc 向量在语义空间常有"不对齐"现象 (query 短而抽象, doc 长而具体), HyDE 用假设答案做"语义桥梁"
- **优点**: 对短 query / 抽象 query 召回提升明显 (论文 +10-30% NDCG)
- **缺点**: 多一次 LLM 生成, 延迟和成本都翻倍; 假设答案错了反而误导检索
- **适用**: 短 query 多的搜索场景 (问答 / 智能搜索)

#### 流派 4: Step-back Prompting

让 LLM 先做"抽象化": 把具体 query 抽象成更宽的概念, 检索相关背景, 再回到原 query。

- **代表论文**: *Take a Step Back: Evoking Reasoning via Abstraction in Large Language Models* (Zheng et al., 2023, Google)
- **例子**: 用户问 "如果温度是 100K 时理想气体的压力是多少, 体积加倍后压力是多少", step-back query 是 "压力跟体积的关系是什么"
- **优点**: 复杂 query 召回质量高, 适合需要先建立背景的领域 (科研 / 教育)
- **缺点**: 实现复杂, 抽象 prompt 调教难

#### 流派 5: Decomposition (分解)

把复杂 query 拆成多个子 query, 分别检索 + 分别回答, 最后合成。

- **代表实现**: LangChain `MultiRetrievalQAChain`, LlamaIndex `SubQuestionQueryEngine`
- **例子**: "比较 X 和 Y 在内存占用、延迟、可扩展性三个维度的表现" → 拆成 3 个子 query, 每个分别检索 + 回答
- **优点**: 对"对比类"、"多维度"问题召回极准
- **缺点**: 调用成本 N 倍, 子问题独立性强时才有效

#### 总结

入门最稳的路: **single rewrite 起步**。它是中小项目 80% 场景的够用方案, 实现简单, 维护成本低。等召回率瓶颈被 single rewrite 顶住, 再按场景往 multi-query / HyDE / decomposition 升级。

### 3. 工程实现要点

single rewrite 的工程实现不复杂, 但有几个细节决定它能不能上生产:

#### 3.1 history 给几轮?

不能给全部历史 (token 浪费 + 噪音)。经验值: **最近 3-5 轮**。再多了 LLM 改写时容易把无关上下文塞进改写 query, 反而误导检索。

#### 3.2 第一轮要不要调 LLM?

不要。第一轮 history 为空, 当前 query 本身就是独立的, 调 LLM 改写只会引入"改写器幻觉"——LLM 可能凭空给 query 加修饰词, 反而让检索效果变差。**直接 short-circuit 返回原 query**, 顺便省一次调用。

#### 3.3 改写 prompt 怎么写?

经验上, 一份能用的改写 prompt 要明确以下几点:

```
你是 RAG 系统的 query 改写器。给定对话历史和用户当前问题, 
输出一个独立的、可以单独丢去检索的 query。

约束:
1. 只输出改写后的 query 一行, 不要解释、不要引号
2. 把代词、省略、指代全部展开 (例: "那它怎么用" + 上文聊 A → "A 怎么用")
3. 不要添加上下文里没有的信息
4. 如果当前问题本身就独立, 原样返回

历史:
{history}

用户当前问题: {query}

改写后:
```

四条约束都重要:
- 第 1 条防 LLM 输出解释性文字 ("好的, 改写如下:..."), 一旦多行就要回退
- 第 2 条是改写器的核心任务
- 第 3 条防"幻觉补全" (LLM 凭空添加上下文里没有的信息)
- 第 4 条防"过度改写" (独立 query 也被加上无关历史)

#### 3.4 用哪个模型改写?

**用便宜的、快的、小的**。改写不是创作, 是"把指代和省略展开", 小模型 (1B-8B) 完全够用。生产里常见做法:

- Claude Haiku / GPT-4o-mini / Gemini Flash (闭源, 便宜)
- Llama 3 8B / Qwen 2.5 7B (开源, 自部署)
- 甚至专门蒸馏一个 1B 的改写器 (高流量场景)

**不要用旗舰模型改写**。延迟翻倍、成本翻倍, 改写质量提升微乎其微 (任务太简单, 大小模型差不多)。

#### 3.5 改写器的 token 要不要计入总成本?

要。一个常见错误是把改写器的 token 藏起来不报告, 让用户以为 "一次问答只花了 X token", 实际是 X + 改写器消耗。在自家可观测体系里, **rewriter 的 input/output token 必须显式累计到本轮总 token**, 哪怕只是埋点不计费, 也要让自己看清真实成本结构。

### 4. 失败回退的五种情况

改写器是 RAG 主链路上多出来的一环, 它挂了不能拖累主链路。生产代码必须把以下 5 类失败全部回退到原 query (而不是抛异常向上传):

| 失败类型 | 触发条件 | 处理 |
|---|---|---|
| 1. 网络/超时 | LLM 调用超时或 5xx | 落 fallback 埋点, 返回原 query |
| 2. 返回结构异常 | SDK 返回 `content` 是空 list, 或第一块不是 text block | 落 fallback 埋点, 返回原 query |
| 3. 输出空字符串 | LLM 返回 `""` 或纯空白 | 落 fallback 埋点, 返回原 query |
| 4. 输出多行 | LLM 输出 "好的, 改写如下:\n..." 这种 | 落 fallback 埋点, 返回原 query |
| 5. 输出过长 | LLM 改写后比原 query 长很多 (× 10+) | 落 fallback 埋点, 返回原 query (可选) |

**核心原则**: 改写器是"加分项", 不是主链路。任何异常都不应该让用户的查询失败, 退回原 query 总比报错好。

埋点的 `error_type` 字段要分得清——网络异常和格式异常的处理方式不同, 长期看哪一类占大头能指导后续优化 (网络多 → 加超时重试 / 升级 SDK; 格式多 → 改 prompt / 加 fewshot)。

### 5. 选型对照表

| 流派 | 实现成本 | 单次延迟 | 单次成本 | 召回提升 | 适用场景 |
|---|---|---|---|---|---|
| Single Rewrite | 低 (一次 LLM call) | + 200-500 ms | + 100-500 token | +20-40% | 多轮 RAG 默认 |
| Multi-Query | 中 (循环 + 去重) | + 500-1500 ms | + N × 单次 | +10-25% (再叠加) | 高价值多面性问题 |
| HyDE | 低-中 | + 500-1500 ms | + 200-1000 token | +10-30% (论文值) | 短 query / 抽象 query |
| Step-back | 中-高 | + 1-3 s | + 中等 | +复杂场景显著 | 科研 / 教育 |
| Decomposition | 高 | + 数 s (子问题数 × ) | + N × 单次 + 合成 | 复合问题显著 | 对比类 / 多维度 |

入门项目从 Single Rewrite 起步, 完全够 80% 场景。

---

## 第三部分: History 截断

### 1. 三种截断策略

对话历史无限增长会撞 LLM 上下文窗口, 且 token 单价不便宜。截断主流是三种思路:

#### 策略 1: 轮数滑窗 (Rounds Sliding Window)

保留最后 N 轮对话 (一轮 = user + assistant 两条), 老的全丢。

- **优点**:
  - 实现极简 (一行 `messages[-2*N:]`)
  - 不依赖任何 tokenizer, 跨模型零适配
  - 行为可预测, 用户和开发者都好理解
- **缺点**:
  - 不感知单条长度: 如果某轮用户贴了一万字代码, 轮数小但 token 已经爆了
  - 信息丢失突然: 第 N+1 轮和第 N 轮的差别就是"完全保留" vs "完全消失"

经验值: **N=10**。再小用户会觉得"AI 记性差", 再大对话连贯感提升有限但 token 成本明显上升。

#### 策略 2: Token / 字符上限 (Token Budget)

按总 token 数封顶, 超过就从最早的消息开始砍, 一条一条扔, 直到 ≤ 上限。

- **优点**: 精确感知"成本", 单条特别长时能及时截断
- **缺点**:
  - 精确 token 计数依赖 tokenizer, 切模型要换 (OpenAI tiktoken / Anthropic count_tokens / 各家不同)
  - 字符估算的话 (`token ≈ chars / 2.5` 对中文偏保守, 英文用 4) 误差 20-40%, 只能粗算

经验值: **8000 token 上限** 对当前主流 32k/128k context 模型够用, 留出 prompt / RAG chunks / LLM 输出的预算。

#### 策略 3: 摘要压缩 (Summarization)

定期把老历史压缩成一段摘要, 摘要本身留在 history 里, 原始对话丢弃。

- **优点**: 信息损失最小, 100 轮对话也能撑住
- **缺点**:
  - 实现复杂度高 (要写摘要 prompt, 调一次 LLM)
  - 摘要本身有信息损失, 长期累积可能跑偏
  - 摘要质量直接决定后续对话质量, 调教成本高
  - 跟 query rewrite 一样, 这又是个"加分项炸主链路"的风险点

适用: 真的需要保留 **跨越数十轮的长期上下文**的场景 (角色扮演 / 长期助手 / 任务分解)。一般产品**用不上**。

#### 实战推荐: 轮数 + token 二级保险

生产里最常见的 95% 实现, 是**轮数滑窗 + token 上限二级保险**, 不做摘要压缩:

```python
# 伪代码
msgs = full_history
msgs = msgs[-2*MAX_ROUNDS:]               # 第一刀: 砍轮数
while sum(len(m["content"]) for m in msgs) > MAX_CHARS and len(msgs) > 1:
    msgs.pop(0)                            # 第二刀: 砍字符 (从头一条一条)
```

两道保险:
- 第一道防 "对话很长但每轮都短" (轮数砍干净)
- 第二道防 "对话不长但某一轮贴了万字代码" (字符砍兜底)

**摘要压缩留给 100k+ token 长对话的特殊场景**, 一般产品收益和复杂度不成正比, 不建议默认开启。

### 2. tokenizer 精确 vs 字符估算

工程上一个常见的两难: 截断要不要算精确 token?

**精确 token 计数的优势**: 跟模型的 context window 严格对齐, 不会浪费、不会爆。

**精确 token 计数的代价**:
- 每条消息都跑一遍 tokenizer (有的快有的慢, OpenAI tiktoken 快, 一些 SDK 的 count_tokens 是网络调用)
- 切模型要换 tokenizer 实现, 多模型混部时 adapter 层得自己写
- 一些代理服务 / 中转服务没暴露 count_tokens 接口, 强用就要自己装 tokenizer 包

**字符估算的代价**: 准度差 20-40%, 中文比英文还更不准。

**字符估算的优势**: 0 依赖, 0 延迟, 跨模型不用改。

经验上的取舍:

- 输入消息少、对成本不敏感的场景: **字符估算就够了**, 多留 20-30% 余量 (比如想留 10000 token, 按 25000 字符截)
- 高流量、对成本敏感的场景: **精确 token 计数**, 但要做 tokenizer 缓存 (同一字符串别算两次)
- 多模型混部场景: 用各家 SDK 自己的 `count_tokens` 接口, 别自己写

中文场景常用的字符估算系数:
- `chars / 2.5` (偏保守, 留缓冲)
- `chars / 3` (中文 GPT 类编码下大致准确)
- `chars / 1.5` (针对生僻字 / 表情 / 代码混杂的稳妥估算)

对照来看, OpenAI 的 cl100k tokenizer 在中文上 **1 个汉字大概 1.5-2 个 token**, 英文 1 个 token 大概 4 个字符。如果你的对话是纯中文, 用 `chars / 2.0` 偏激进, 用 `chars / 2.5` 比较安全。

### 3. pinned messages 和摘要压缩

实战中, 截断的难点不是"砍多少", 是"砍哪条"。直接砍最早的对话有个隐藏问题: **第一轮往往包含全局上下文** ("我们要做的是一个 X 系统, 你扮演 Y 角色, 注意 Z 约束")。砍掉它, 后面的对话就失锚。

生产做法是 **pinned messages**: 给某些消息打 "不可截断" 标记, 截断时跳过它们。

| pinned 候选 | 理由 |
|---|---|
| system prompt | 角色设定, 几乎所有 RAG 系统都把它放最前面且永远不动 |
| 第一条 user message | 经常包含任务的全局上下文 |
| 用户显式 pin 的消息 | 用户说 "记住这个" 的消息 |
| Agent 工具调用的关键结果 | Tool use 的关键输出 (比如"已确认订单号 ABC") |
| 当前任务的 "目标定义" | 多步 Agent 场景, 任务定义不能丢 |

实现上, `Conversation` 类多一个 `pinned: list[bool]` 数组, 截断时跳过 `pinned[i] == True` 的位置。或者把 pinned 消息放进一个独立的 `pinned_messages` list, 拼 prompt 时跟截断后的 history 合并。

**摘要压缩**: 触发时机的经验值是 **轮数 ≥ 50 且 token ≥ 30k**, 这时再不压就要爆了。压缩的内容选 "最早被滑窗砍出去的部分", 而不是当前还在 active 的部分。

### 4. 截断顺序与单条超额

工程上几个容易踩的细节:

**截断顺序: 先轮数后字符**。轮数粗砍快, 字符精砍准, 顺序反了 (先字符后轮数) 容易在长对话里把字符数压到上限, 但保留的轮数不够多, 实际 context 利用率反而低。

**单条超过 max_chars 怎么办?** 比如 max_chars = 20000, 但某一条用户贴了 50000 字符的代码。两种处理:

1. **保留整条**: 单条无论多长都保留, 字符上限只控制累计而非单条。代价: 单轮可能直接撑爆 LLM 输入上限, 但行为可预测, 不会"切碎"用户输入。
2. **切碎单条**: 单条超过就截尾。代价: 用户看到 "我贴的代码被截了" 体验不好, 而且代码截尾后语法都不完整, 喂 LLM 没意义。

**推荐保留整条 + 落 oversize 埋点**。让用户和运维知道 "有一条超额了", 后续可以告警 / 提示用户拆短, 而不是默默切碎。这是一个 **"显式故障比静默退化好"** 的标准案例。

---

## 第四部分: 工程整合 (三者怎么协作)

三件事 (rewrite / reorder / history 截断) 在工程上独立, 但在一次问答的 pipeline 里有明确的先后关系。讲清楚这个 pipeline, 才能把它们真正用起来。

### 1. 调用顺序: rewrite → retrieve → reorder → LLM

```
用户输入 query
     │
     ▼
┌──────────────────────┐
│ 1. history 取最近 N 轮 │  ← history 截断 (给 rewrite 用)
└──────────┬───────────┘
           ▼
┌──────────────────────┐
│ 2. query rewrite      │  ← LLM 把 (history + query) 改写成独立 query
│    (history 空时跳过) │     失败回退原 query
└──────────┬───────────┘
           ▼
┌──────────────────────┐
│ 3. retrieve           │  ← 用 rewritten query 走向量 / Hybrid 检索
│    返回 top-K chunk    │
└──────────┬───────────┘
           ▼
┌──────────────────────┐
│ 4. reorder            │  ← chunks ≥ min_chunks 时 odd-front 重排
└──────────┬───────────┘
           ▼
┌──────────────────────┐
│ 5. 拼 prompt          │  ← 把 chunks 拼成 [资料 1]..[资料 N], 编号在重排之后
└──────────┬───────────┘
           ▼
┌──────────────────────┐
│ 6. history 截断 + 拼  │  ← 截断后的 history + 当前 rag_user_msg
└──────────┬───────────┘
           ▼
┌──────────────────────┐
│ 7. LLM 生成           │
└──────────────────────┘
```

每一步的设计要点:

- **第 1 步的 history 跟第 6 步的 history 截断参数可以不同**。给 rewriter 用的 history 一般取最近 3-5 轮 (改写不需要太久远), 给 LLM 拼 prompt 用的取最近 8-15 轮 (保上下文连贯)。
- **第 3 步的检索器不感知是单轮还是多轮**, 它只看 rewritten query。这是单轮 RAG 检索器逻辑能复用到多轮的关键, 不要让检索器再"自己读 history"。
- **第 4 步的编号在重排之后做**, 这样 LLM 看到的 `[资料 1]` 跟它 prompt 里第一块是同一块, 引用编号不会错乱。如果先编号再重排, LLM 输出 `根据[资料 3]...` 时, 你根本不知道它指的是原来的第 3 块还是重排后的第 3 块。
- **第 6 步的 history 截断需要小心**: 截断时拼接的 rag_user_msg 单条可能特别长 (RAG chunks 一塞动辄几十 k 字符), 字符上限要么不传给这个调用、要么调大很多, 否则会被自己当 oversize 砍掉前面 history。

### 2. config 开关与可观测性

每一个新行为都应该有 **独立开关 + 埋点**, 这是后续做 A/B、做容量规划、做问题排查的地基。

```
# config 示例 (env 命名按你团队习惯就好)
REORDER_ENABLED=true
REORDER_MIN_CHUNKS=5            # chunks 数低于此值跳过重排

QUERY_REWRITE_ENABLED=true
QUERY_REWRITE_MODEL=<小模型>     # 改写器用便宜的, 不用旗舰
QUERY_REWRITE_HISTORY_ROUNDS=4  # rewriter 看几轮历史
QUERY_REWRITE_MAX_TOKENS=200    # 改写结果上限
QUERY_REWRITE_TIMEOUT_S=5.0     # 超时回退

HISTORY_TRUNCATE_ENABLED=true
HISTORY_MAX_ROUNDS=10           # 滑窗轮数
HISTORY_MAX_CHARS=20000         # ≈ 8k token 二级保险
```

埋点至少要覆盖 **每个行为的 "做了 / 没做 / 失败" 三种状态**:

| 事件 | 触发 | 关键字段 |
|---|---|---|
| `reorder.applied` | 真重排了 | `n_chunks`, `min_chunks`, `order_before`, `order_after` |
| `reorder.disabled` | 配置关 / 数量不足 | `reason`, `n_chunks` |
| `rewriter.skipped` | history 空 | `reason: "history_empty"` |
| `rewriter.disabled` | 配置关 | (无) |
| `rewriter.ok` | 改写成功 | `original`, `rewritten`, `in_tok`, `out_tok` |
| `rewriter.fallback` | 改写失败 | `error_type`, `error_msg`, `original` |
| `history.truncated` | 真截了 | `kept_msgs`, `dropped_msgs`, `reason` |
| `history.oversize` | 单条超额 | `msg_idx`, `chars`, `max_chars` |

**为什么 `skipped` 和 `disabled` 要拆开?** 两个原因:

1. 语义不同: skipped 是"逻辑判断该跳过", disabled 是"配置不让做"。混在一起后期排查会迷糊。
2. 统计需求不同: skipped 的发生率反映"用户第一轮提问的比例", disabled 的发生率反映"配置变更的影响"。

这种 **细颗粒度事件** 早期看起来啰嗦, 但生产 3 个月后做"为什么改写率突然掉了"这种排查时, 颗粒度细的埋点会让你少走 80% 的弯路。

### 3. token 预算分配

一次问答里, token 预算大致分给四块:

```
LLM 上下文窗口 (假设 32k)
├─ system prompt              (1k 左右)
├─ history 截断后              (8k 左右)  ← max_chars 这里管
├─ rag_user_msg (chunks)      (10k 左右) ← 控制 top_k + 单 chunk 长度
└─ 留给 LLM 输出的预算          (4k 左右)
   ──────────────
   小计                        23k
   剩余余量                      9k       ← 给意外情况
```

经验法则:
- **system prompt 占 5% 以内**
- **history 占 25% 以内**
- **RAG chunks 占 40-50%** (这部分是核心)
- **留 15-20% 给 LLM 输出**
- **留 10% 余量** 防意外

如果某次问答的 RAG chunks 特别多 (比如塞了 30 块), 应该 **优先砍 history** (因为 chunks 跟当次问题相关性更高), 而不是砍 chunks。这是 token 预算的"优先级", 不在代码里写死也行, 但心里要有。

---

## 第五部分: 生产真实做法

这一节讲工业代表方案。不绑定任何具体团队或产品身份, 都是公开文档里能查到的实现。

### 1. LangChain ConversationalRetrievalChain

LangChain 早期主推的多轮 RAG 默认链。

- **核心**: `condense_question_prompt` 把 (history + question) 缩成一个独立 question, 然后走标准 RetrievalQA
- **改写 prompt 模板**: 自带一个英文版, 用户可以替换成自己的 (中文场景必须换)
- **history 截断**: 默认 `ConversationBufferWindowMemory(k=N)`, 保留最后 N 轮; 也支持 `ConversationSummaryMemory` 摘要
- **重排**: 默认没有, 要叠一个 `LongContextReorder` 后处理器
- **优点**: 开箱即用, 代码 20 行能跑
- **缺点**: 中文 prompt 默认不好, 改写 LLM 选不准会影响整体, 摘要压缩用上去后调试链路长

适合**入门多轮 RAG 项目**, 跑通了再考虑替换某些组件。

### 2. LlamaIndex CondensePlusContextChatEngine

LlamaIndex 的多轮 RAG 标准方案, 比 LangChain 那套更精细。

- **核心**: 分两步: (1) condense question (改写), (2) 用改写后的 question 检索, 然后把检索结果作为 "context" 加到 system prompt 里, 跟 chat history 一起送给 LLM
- **改写 prompt**: 自带专门的 `CondenseQuestionPrompt`
- **history 管理**: `ChatMemoryBuffer(token_limit=...)` 精确按 token 截断
- **重排**: `LongContextReorder()` 后处理器, 跟 `MMRPostprocessor` (多样性) 是平行选项
- **优点**: 接口干净, token 控制精确, 后处理器组合灵活
- **缺点**: 学习曲线比 LangChain 稍陡, 文档密度大

适合**愿意投入 LlamaIndex 学习成本的中型项目**。

### 3. 大厂自研: 摘要 + 滑窗 + pinned

大厂自研的多轮对话系统 (各家智能助手 / Copilot / ChatGPT 类产品) 普遍走得更精细:

- **三层 history**: 
  - **pinned**: system + 用户显式 pin + 任务定义 (永不丢)
  - **active**: 最近 K 轮 (滑窗)
  - **archived**: 老对话被摘要压缩成 1-2 条摘要消息, 跟 active 拼起来送给 LLM
- **rewrite**: 用专门蒸馏过的 1B-3B 改写器, 不用旗舰模型
- **重排**: 不止 odd-front, 还会按 query 类型 (问答类 / 摘要类 / 对比类) 分桶, 用不同的位置策略
- **token 精算**: tokenizer 离线服务, RPC 调用 1ms 内, 不用估算
- **可观测性**: 全链路埋点 + Prometheus + Grafana, 实时盯改写成功率、截断触发率、平均轮数、跨模型一致性

入门项目能跑到 **LangChain 配置版** 已经及格, 跑到 **LlamaIndex 精细配置版** 算优秀。自研那套 **是 50+ 人团队的工程量**, 量级到了再考虑。

### 4. 评测怎么做

多轮 RAG 比单轮 RAG 难评得多, 因为 "对" 跟 "错" 跟历史强相关。社区主流方法:

| 评测维度 | 工具 / 方法 |
|---|---|
| 改写质量 | 人工标注 50-100 条 (history + raw query → ground truth rewritten), 算 BLEU / ROUGE; 或 LLM-judge 打分 |
| 召回率 (改写后) | Recall@k / NDCG@k, 在 (改写 query, 期望 chunk) 对上算 |
| 整体回答质量 | LLM-judge 对比 "无改写 vs 有改写" 的回答, 多个维度打分 (相关度 / 完整度 / 是否幻觉) |
| 截断对体验影响 | 长对话 (20+ 轮) 测试, 看 "记得我们之前说过 X 吗" 这种问题的答对率 |
| token 成本 | 改写器 token + 检索 / LLM token, 算 "改写 ROI" (召回提升 / 成本提升 比值) |

入门项目至少跑 LLM-judge 对比 (50 条数据 1 小时能做完), 看改写是否真的提升整体回答质量。**不要凭直觉判断"加了 rewrite 应该更好"**——任务不对、prompt 不好、模型选错, 改写都可能负向。

---

## 第六部分: 常见误区

跟具体项目无关的五个常见坑, 社区里反复出现:

- **误区 1: 多轮 RAG 直接把全部 history 丢检索器**。看起来"信息更全", 实际是噪音 dominating——检索器拿到 200 字 query (其中 180 字是历史), 检索结果完全跑偏。**单 query 改写是必经之路**, 不是"可选优化"。

- **误区 2: rewrite 用旗舰模型, 想着"改得更准"**。实际上 query 改写是机械任务 (展开代词 + 加全主语), 小模型完全够。用旗舰模型只会让单轮延迟 + 1-2 秒, 用户体验下降。

- **误区 3: 重排只看 chunk 数, 不看 LLM context 长度**。8k chunk 的小 prompt 重排收益微乎其微; 64k+ 的长 prompt 重排收益巨大。重排策略应该按 **prompt 总长度** 而不是 **chunk 数** 决策。

- **误区 4: history 截断只砍轮数不砍字符**。一旦用户贴长代码, 单轮就能爆 context, 轮数限制完全兜不住。**轮数 + 字符** 是标配。

- **误区 5: 摘要压缩开了就不管了**。摘要质量会逐渐"漂移"——LLM 总结自己之前的总结, 信息熵越压越低, 5 轮压缩后早期内容已经面目全非。生产里**必须定期算"摘要后能不能答原始问题"**, 不能开了就当万能。

这五条覆盖了 70% 的踩坑场景。剩下的细节在第七节展开。

---

## 第七部分: 小型实现的常见坑 → 生产对策

> 这一节是全文最重的部分。主语统一用 "demo 实现 / 小型项目 / 学习场景 / 入门做法", 对面是 "生产 / 工业级 / 规模化部署"。所有坑都是社区里反复出现的, 不绑定任何特定项目身份。

### 7.1 常见坑

#### 坑 1: 改写器抛异常炸主链路

**demo 实现常见做法**: rewriter 函数直接 `raise` 异常 (网络错 / 超时 / 输出格式不对), 让调用方处理。

**症状**:
- 用户问"那它怎么用", LLM 改写器超时, 整轮问答挂掉, 用户看到 500
- 一旦改写器有任何不稳, 主链路 RAG 跟着不稳
- 改写器的成功率耦合到 RAG 的成功率

**本质**: rewriter 是 **"加分项"** 不是 **"主链路"**。它失败时, 最坏情况就是 "回到不改写的水平"——这跟 "整轮挂掉" 是两件事。但 demo 代码经常把它写成主链路, 一旦失败就抛出去, 调用方又没专门 catch, 用户直接吃 500。

**退化级**: ⭐ (1 颗星, 严重退化, 因为它让"可选优化"变成了"故障源")

**生产对策**: rewriter 函数内部 catch 所有异常, 返回 `(原 query, None)` 并落 fallback 埋点。调用方只看返回值, **永远不会从 rewriter 收到异常**。代码示意:

```python
def rewrite_query(client, model, history, query, ...):
    if not history:
        log_event("rewriter.skipped", {"reason": "history_empty"})
        return (query, None)
    try:
        resp = client.messages.create(...)
        # ... 5 类失败检查 ...
        return (rewritten, (in_tok, out_tok))
    except Exception as e:
        log_event("rewriter.fallback", {
            "error_type": type(e).__name__,
            "error_msg": str(e)[:200],
            "original": query,
        })
        return (query, None)
```

**生产参考**:
- LangChain `ConversationalRetrievalChain` 改写失败时回退 raw question (但默认不打埋点, 自己加)
- LlamaIndex `CondenseQuestionChatEngine` 同上

---

#### 坑 2: 单 query 改写 + 短 history → "话题转移失忆"

**demo 实现常见做法**: rewriter 只看 history 最近 2-3 轮, 改写时把 history 拼成 "U: ...\nA: ...\n" 直接喂给 LLM。

**症状**: 用户在第 10 轮聊了 X, 第 11-13 轮聊了 Y, 第 14 轮问"那 X 怎么用", rewriter 只看到最近 3 轮的 Y, 改写成了 "Y 怎么用", 检索全错。

**本质**: 短 history 看不到话题切换。但**给 rewriter 长 history 又有反作用**——LLM 会被无关上下文带偏。这是个**结构性矛盾**: 短了失忆, 长了被带偏。

**生产对策有几种**:

| 招 | 做法 | 适用 |
|---|---|---|
| A | history 不变, 但加 "用户原始 query 优先" 的 prompt 约束 | 中小项目首选 |
| B | rewriter 用 entity-aware: 先抽实体, 改写时强制保留实体 | 实体密集场景 |
| C | 用户显式触发 `/topic` 标记话题边界, rewriter 只看当前话题 | 工具型对话 |
| D | multi-query 改写: 一次生成"基于近期" + "基于全局"两个改写, 都跑检索 | 高价值场景 |

**退化级**: ⭐⭐ (2 颗星, 中度退化, 影响多轮深度对话场景)

---

#### 坑 3: 重排破坏 `[资料 N]` 引用语义

**demo 实现常见做法**: 先按 score 降序编号 `[资料 1]..[资料 N]`, 再做 odd-front 重排。

**症状**:
- LLM 输出 "根据[资料 3], 这个参数是 X", 但你的日志显示重排后第 3 位是原本第 5 位的 chunk
- 用户点开"资料 3"原文跟 LLM 引用的内容对不上
- 评测时无法验证 LLM 引用是否正确

**本质**: 编号 (字面 `[资料 1]`) 和位置 (chunk 在 prompt 里第几个) 是两个独立维度。先编号后重排, 编号跟着 chunk 走但 LLM 看到的位置变了, LLM 的引用是按 prompt 里看到的位置说的, 编号已经失去原本"按 score 降序"的语义但又没获得新语义, **同名不同义, 引用错位**。

**生产对策**: **重排后再编号**。让 LLM 看到的 `[资料 1]` 就是它 prompt 里第一块。代码顺序:

```python
chunks = retrieve(query, top_k=K)
chunks = reorder_for_long_context(chunks, min_chunks=5)
# 编号在重排之后做
prompt_parts = [f"[资料 {i+1}] {c['text']}" for i, c in enumerate(chunks)]
```

这样:
- LLM 看到 `[资料 1]` 就是 prompt 里第一段
- 评测时用 prompt 还原, "资料 3" 就是 prompt 里第三段
- 日志记 `order_after` 字段, 调试时能反查回原始 score 顺序

**退化级**: ⭐⭐ (2 颗星, 看着小但定位 bug 时很头疼)

---

#### 坑 4: `to_messages()` 返回内部引用 + mock 测试假阳/假阴

**demo 实现常见做法**: `Conversation.to_messages()` 直接 `return self._messages` (返回内部 list 的引用, 不拷贝)。

**症状**: 写测试时:

```python
def test_truncation():
    conv = Conversation()
    conv.add_user("A")
    conv.add_assistant("X")
    # 用 mock 抓 LLM 调用时拿到的 messages
    with mock.patch.object(client, "messages_create") as m:
        ask(client, conv, "B")
        sent_messages = m.call_args.kwargs["messages"]
    # 后续断言挂掉, sent_messages 里少了一条
    assert len(sent_messages) == 3   # ← 失败! 是 2
```

**为什么挂?** 因为 `mock.call_args` 记的是 `messages` 参数的 **引用**, 不是当时的快照。`ask()` 内部如果调了 `conv.to_messages()` 拿到的也是内部 list 的引用, 然后函数结束后某处对原 list 做了 pop/append 改动, `call_args` 跟着变了, 测试断言挂在 "看起来少了一条"。

**本质**: 这是 Python 引用语义的经典陷阱, **跟 mock 的"记录调用参数"机制叠加 = 测试结果跟你的直觉相反**。生产代码 + mock 测试 + 引用语义三件套同时存在时, 这个坑必踩。

**生产对策**:

| 方案 | 做法 | 利 | 弊 |
|---|---|---|---|
| A | `to_messages()` 总是返回拷贝 | 干净, 没有引用陷阱 | 老调用方依赖引用语义的话要改 |
| B | `to_messages(*, copy=False)` 默认拷贝, 显式才返回引用 | 兼容 + 默认安全 | 接口稍复杂 |
| C | 带任何参数 (max_rounds/max_chars) 时强制拷贝, 无参时保持引用 (零回归) | 老接口不动 | 心智负担在测试: 测试要显式开截断走拷贝路径 |
| D | 不改库, 测试里手工 `messages=list(m.call_args.kwargs["messages"])` 拷贝出来 | 不改库 | 每个测试都要记得拷, 容易漏 |

C 方案是 **零回归约束** 下唯一可行的, 但要在团队文档里**显式记下**: 测试 mock 引用语义时, 必须显式打开截断 (传 max_rounds/max_chars), 否则 mock 抓的是 self._messages 引用, 后续操作会反向污染。

**退化级**: ⭐⭐ (2 颗星, 隐蔽, 容易写出假阴性测试还自以为绿)

---

#### 坑 5: 子进程不继承环境变量

**demo 实现常见做法**: 在 PowerShell / git-bash 等环境跑 smoke 时, 用 `VAR=x cmd | next` 这种 bash 风格的"行内环境变量"语法。

**症状**: smoke 脚本写 `HISTORY_MAX_ROUNDS=2 my_runner my_app` 期望生效, 实际进程里读到的还是默认值 10。

**本质**: bash 行内环境变量语法 (`VAR=x cmd`) 只在 POSIX shell **直接执行 cmd 时** 起作用。一旦中间多一层 (uv 包装、PowerShell 直接调 git-bash、跨 shell 链), 中间那层会把 VAR=x 当作普通参数解析或丢掉, 最终子进程的 env 里根本没这个变量。

**症状不显眼**: 测试 / smoke 不会报错, 只是行为跟预期不同, 默默走默认值。

**生产对策**:

- **显式用 `env` 命令前缀**: `env VAR=x cmd` 在 POSIX shell 里强制 env 注入, 比行内语法更稳
- **跨 shell 场景用 `os.environ` 在脚本内设置**: 不依赖 shell 语法
- **可观测性兜底**: 启动时 log 关键 config 值, smoke 时盯日志判断 env 是否真生效
- **CI 场景用专门的 env 文件**: `.env.smoke` / `.env.test`, 不依赖命令行 inline

**退化级**: 这个不是"代码退化", 是"工程流程坑", 但同样让 smoke 验证假绿——配置改了但跑的还是默认值, 自以为测了实际没测。

---

### 7.2 生产对策

把 5 个坑的对策汇总到一张表, 让"从 demo 走到生产"的路线一眼能看清:

| 坑 | demo 实现做法 | 生产做法 | 退化级对照 |
|---|---|---|---|
| 改写器抛异常 | 直接 raise | 函数内 try/except + fallback 埋点 + 返回原 query | ⭐ vs ⭐⭐⭐⭐⭐ |
| 短 history 失忆 | 取最近 2-3 轮 | 取 3-5 轮 + 实体保留 / multi-query / 话题边界标记 | ⭐⭐ vs ⭐⭐⭐⭐ |
| 重排破坏引用 | 先编号后重排 | 重排后再编号, 日志记 order_after 反查 | ⭐⭐ vs ⭐⭐⭐⭐⭐ |
| 引用语义 mock 陷阱 | to_messages 总返引用 | 默认拷贝, 或带参时拷贝 + 测试纪律 | ⭐⭐ vs ⭐⭐⭐⭐ |
| 子进程不继承 env | `VAR=x cmd` 行内语法 | `env VAR=x cmd` / 脚本内 os.environ / .env 文件 | ⭐ vs ⭐⭐⭐⭐ |
| 改写模型选型 | 用旗舰模型 | 用便宜小模型 (蒸馏 1B-3B 最佳) | ⭐⭐ vs ⭐⭐⭐⭐ |
| 截断策略 | 只砍轮数 | 轮数 + 字符二级保险 + pinned messages | ⭐⭐ vs ⭐⭐⭐⭐⭐ |
| token 精算 | 字符÷2.5 估算 | tokenizer 服务化 RPC 调用 | ⭐⭐ vs ⭐⭐⭐⭐ |
| 摘要压缩 | 不做 / 一开启就不管 | 长对话场景启用 + 定期评测摘要质量 | ⭐⭐⭐ vs ⭐⭐⭐⭐⭐ |
| 评测 | 没评测 | LLM-judge 对比 + 人工抽检 + 召回指标 | ⭐ vs ⭐⭐⭐⭐⭐ |

### 7.3 综合对照表

把整个第一/二/三部分的退化点统一成一张评级表:

| 维度 | demo 实现常见 | 生产标准 |
|---|---|---|
| 重排算法 | odd-front (基础) | odd-front + chunked re-ranking + 按 query 类型分桶 |
| 重排触发 | min_chunks 单阈值 | 按 prompt 总长动态决策 |
| 改写流派 | single rewrite | single / multi / HyDE / step-back 按场景选 |
| 改写模型 | 旗舰模型 | 蒸馏 1B-3B 专用 |
| 改写 prompt | 通用模板 | 多场景模板池, 按 query 类型路由 |
| 改写失败 | raise 异常 | 5 类失败全 fallback + 埋点 |
| history 截断 | 只砍轮数 | 轮数 + token + pinned + 摘要压缩四级 |
| token 精算 | 字符估算 | tokenizer 服务化 RPC |
| 可观测性 | 没有 / 只 print | 全链路 JSONL + Prometheus + Grafana |
| 评测 | 没评测 | LLM-judge + 人工 + 定期回归 |

⭐ 数越多, 离生产最佳实践越近。

---

## 第八部分: 补齐路线 (按 ROI 排序)

给"想从 demo 走到生产"的读者一份路线图。顺序不是技术先进度, 是 **"改最少代码 / 拿最大收益"** 的排序:

1. **加可观测性**。落 `reorder.applied` / `rewriter.ok` / `rewriter.fallback` / `history.truncated` 这种 JSONL 事件, 字段至少包括必要的输入输出和原因。后续所有改动都靠数据说话, 没数据全是猜。

2. **改写器写入 fallback 路径**。把 5 类失败全 catch + 退原 query + 埋点。这是稳定性基础, 没这个所有后续改进都没用 (改写一挂全挂)。

3. **加重排**。10 行代码, odd-front 算法直接套, 立刻有 5-15% 召回收益。这是 ROI 最高的一项, 几乎没有不做的理由。

4. **改 query rewrite 模型为便宜小模型**。延迟和成本都立竿见影下降, 改写质量损失微乎其微 (任务本身不需要旗舰)。

5. **加 history 截断 + pinned 消息**。轮数 + 字符二级保险, system + 第一条 user 设为 pinned。这是产品体验底线。

6. **加评测集**。50-100 条 (history, query, 期望答案 / 期望召回) 标注, 算改写 ROI、回答质量提升。后续选型有数。

7. **改重排策略**。从 odd-front 升到按 prompt 总长度自适应, 或叠 chunked re-ranking。这是后期精细化。

8. **改写流派升级**。从 single rewrite 升 multi-query 或 HyDE。需要量化 ROI 后再上 (不要无脑加, 成本翻倍可能不值得)。

9. **摘要压缩**。长对话场景才上, 必须配定期摘要质量评测。这是高复杂度环节。

10. **token 精算 + tokenizer 服务化**。流量大、成本敏感时再做。

前 5 条都是 "几小时到几天" 能做完的, 性价比最高。后 5 条是 "几周到几个月" 的工程, 量级到了再上。

---

## 附录 A: 术语小词典

- **odd-front interleave**: 一种重排策略, 偶数下标顺序 + 奇数下标倒序, 让高分占头尾、低分挪中间
- **Lost in the Middle**: Liu et al. 2023 论文标题, 描述 LLM 在长上下文中"中间塌陷"现象
- **Query Rewrite**: 把多轮对话的当前 query 改写成独立 query, 让检索器能脱离上下文工作
- **Single Rewrite**: 一次 LLM 调用生成一个改写 query, 最常用流派
- **Multi-Query**: 一次生成多个改写 query, 分别检索取并集
- **HyDE (Hypothetical Document Embeddings)**: 用 LLM 生成假设答案, 用假设答案的向量去检索
- **Step-back Prompting**: 把具体 query 抽象成宽问题再检索, 再回到具体 query
- **Query Decomposition**: 把复杂 query 拆成多个子 query, 分别回答再合成
- **ConversationBufferWindowMemory**: LangChain 的轮数滑窗 memory 实现
- **ConversationSummaryMemory**: LangChain 的摘要压缩 memory 实现
- **pinned message**: 不可截断的消息, 通常是 system / 第一条 user / 用户显式 pin
- **history truncation**: 对话历史截断, 主流策略是轮数 + token 二级保险
- **token budget**: 一次 LLM 调用的 token 预算分配, 大致分给 system / history / context / output
- **fallback**: 兜底回退, 改写器 / reranker 失败时回到不做该优化的状态
- **fail-safe**: 失败时退到次优可用状态而不是抛错, 跟 fail-fast 是对立设计哲学
- **tokenizer**: 把文本切成 token 的工具, OpenAI tiktoken / Anthropic count_tokens / 各家不同
- **U 型注意力曲线**: LLM 长上下文的注意力分布形态, 头尾高、中间低
- **召回率 (Recall@k)**: top-k 里至少有一个相关 doc 的比例, 衡量改写后的检索质量
- **LLM-judge**: 用通用 LLM 当评测器, 给两个版本打分, 自动化评测的常用做法
- **CondenseQuestion**: LlamaIndex 术语, 跟 LangChain 的 condense_question 同义, 都是单 query 改写
- **chat memory**: 对话状态管理模块, 截断 / 摘要 / pinned 都属于这块
- **drift (摘要漂移)**: 摘要压缩多次后, LLM 总结自己之前的总结, 信息逐渐失真
- **context window**: LLM 一次能接受的 token 上限, 8k / 32k / 128k 是常见档位

---

## 附录 B: 进一步阅读

**论文 / 原始资料:**

- *Lost in the Middle: How Language Models Use Long Contexts* (Liu et al., 2023) — 重排理论奠基
- *Precise Zero-Shot Dense Retrieval without Relevance Labels* (Gao et al., 2022) — HyDE 原始论文
- *Take a Step Back: Evoking Reasoning via Abstraction in Large Language Models* (Zheng et al., 2023) — Step-back prompting
- *Query2doc: Query Expansion with Large Language Models* (Wang et al., 2023) — query expansion 经典思路
- *Self-RAG: Learning to Retrieve, Generate, and Critique through Self-Reflection* (Asai et al., 2023) — 自适应改写
- *Recurrent Memory Transformer* (Bulatov et al., 2022) — 长上下文记忆机制学术研究
- *MemGPT: Towards LLMs as Operating Systems* (Packer et al., 2023) — 把对话历史当虚拟内存管理

**工程实践 / 文档:**

- LangChain `ConversationalRetrievalChain` 与 `MultiQueryRetriever` 源码
- LlamaIndex `CondensePlusContextChatEngine` 与 `LongContextReorder` 文档
- LangChain `LongContextReorder` 后处理器 (odd-front 实现, 几十行代码)
- Anthropic prompt engineering 指南里的 multi-turn / context management 节
- OpenAI Cookbook 的 "How to handle chat history" 章节
- Pinecone Learning Center: query expansion 与 reranking 教程

**评测 / 基准:**

- MT-Bench (多轮对话评测, LLM-judge 标准做法)
- ConvAI / ConvAI2 (多轮对话数据集)
- TopiOCQA (Topic-switching Open-domain Conversational QA, 话题切换专门数据集)
- MS MARCO Conversational Search 子集

---

## 写在最后

多轮 RAG 的难点不在算法, 在 **"把无状态的检索器套上有状态的对话"** 这个工程问题。三件事 (rewrite / reorder / history 截断) 是这个问题最早暴露的三个表面症状, 它们的天花板由几件事共同决定:

1. **改写器是加分项, 必须 fail-safe** —— 任何让改写器异常炸主链路的设计都是错的
2. **重排几乎零成本, 默认应该开** —— odd-front 一行代码, chunk 数 ≥ 5 时就有收益
3. **history 截断必须双保险** —— 单看轮数兜不住贴长代码, 单看字符兜不住对话连贯, 两个都要
4. **pipeline 顺序决定能不能用** —— rewrite → retrieve → reorder → LLM, 编号在重排之后做
5. **可观测性是 A/B 的地基** —— 没埋点的优化都靠猜, 三个月后没人记得当初为什么这么调的
6. **评测是所有调优的真相** —— LLM-judge 不贵, 50 条数据 1 小时, 比拍脑袋强一万倍

最后一句送给所有要做多轮 RAG 的朋友: **三件事都不是 "加上就好", 是 "加上才看见自己原来差在哪"**。把改写的回退路径、重排的编号顺序、截断的双保险、引用语义的 mock 陷阱一件件搞清楚, 比换一个更大的模型管用得多。

希望这份指南对你有帮助。

— *本文档遵循 [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) 协议, 可自由引用、修改、再发布, 保留出处即可。欢迎根据你自己的实战经验提 issue 或 PR。*
