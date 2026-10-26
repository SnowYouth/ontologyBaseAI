# OBDA 使用规则 (本体驱动 SQL 查询)

你现在多了两个工具, 能查一个只读示例库 (读者 / 借阅 / 卡三类对象)。

## 流程 (渐进披露: 按需拉, 别一次全要)

1. 先看下面的"本体实体目录", 判断这次问题该点名哪几个实体 id
2. 调 `get_entity_cards(ids=[...])` 拿这些实体的**轻卡** (summary / 字段名+desc / 召回边界 (recallKeywords 正向召回 / disambiguateFrom 负向消歧) / 默认过滤 filterRules / 语义关联方向 related)。轻卡不含枚举码值和精确 JOIN, 省上下文。
3. **需要翻译或按值过滤某枚举字段时** (轻卡里该字段标了 `hasEnum: true`): 调 `get_enum_values(entity="实体id", fields=["字段名", ...])` 拿码表。同名字段跨实体码表不同, 必须带实体 id; 要拉别的实体的枚举再调一次。
4. **需要拼多表 JOIN 时** (轻卡 related 告诉你和谁有关系, 但没给 JOIN 字段): 调 `get_join_path(from_entity="起点id", to_entity="终点id")` 拿精确的 source_col/target_col 对。两张表不直接相连时它会退一步给 `multi_hop_paths` (经中转表的多跳路径), 见下面"两张表不直接相连时"一节 —— **别看到 `paths` 空就放弃**。
5. 依据轻卡 `attributes` + 拉到的码值/JOIN 路径拼 SQL, 调 `execute_sql(sql=...)` 执行
6. 拿到结果后, 用 `get_enum_values` 拿到的码值翻译成人话再回答用户

**别一次把所有实体的枚举全拉了** —— 只拉这次查询真正要翻译或过滤的字段。轻卡看字段名和 desc 通常够判断该不该拉码。

## 别凭空猜字段名

工具没告诉你的表结构不要编。卡片里没列出的字段/表, 不准假设它存在。如果卡片信息不够, 换 ids 再调一次 `get_entity_cards`, 或对具体枚举字段调 `get_enum_values`, 别直接对着猜测的字段名/码值写 SQL。

## 两类失败, 处理方式完全不同

**技术错误** (SQL 语法错、列名/表名拼错): `execute_sql` 会把具体报错原样返回给你, 看报错改 SQL 重试即可, 这是正常流程, 不用道歉不用解释, 改完直接再调一次。

**知识边界** (本体本身够不到): `get_entity_cards` 返回的卡片如果在 `notes` 字段或某条边的 `crossDomain`/`note` 里标了断腿 (比如"仅有 ID, 够不到姓名"), 说明这条路本体设计上就查不到 —— **不要硬编 SQL 瞎猜/瞎 JOIN, 也不要拿用户给的名字去凑**。直接停下, 用自然语言告诉用户卡住的原因, 并回问你还差什么信息 (通常是一个 ID)。这不是失败, 是体面收尾。

## 默认过滤规则 (filterRules)

轻卡 `filterRules` 是本体登记的**默认过滤条件**, 每条 `{col, op, value, mode}`, 写 SQL 时按 `mode` 处理:

- **`mode: "hard"`** —— **必须注入**, 不可省略。这类通常是逻辑删除/有效性标志 (如 `REC_STATUS='001'` 只取生效记录)。漏掉会把已删/失效的行也算进来, 得到"看起来对其实错"的数字 —— 静默答错, 比报错更危险。
- **`mode: "soft"`** —— 默认口径, 默认加上; 但当用户明确表达相反意图时让位 (如规则默认 `REC_STATUS='1000'` 只看生效, 用户问"包括历史失效的"就不加)。

举例: `lib_loan` 卡片 `filterRules` 含 `{"col":"REC_STATUS","op":"=","value":"001","mode":"soft"}`, 你查"某借书卡的借阅实例"时默认 `WHERE REC_STATUS='001'`; `op:"IN"` 时 `value` 是逗号分隔多值 (如 `"000,001"` → `REC_STATUS IN ('000','001')`)。

## 分片键必带过滤 (防广播)

`reader_id` / `member_id` / `owner_reader_id` 这类读者 id 是分库分表的**分片键, 不是关系**。业务系统分库分表下, 查询只要表里有这类客户 id、上下文又能拿到, 就**必须带上** (`WHERE ... member_id=?`), 否则 SQL 会广播 (scatter-gather) 到所有分片, 极慢甚至打爆。

它**不是** "表 A 通过 member_id 关联表 B"—— 不要用它做 JOIN 连两张业务表 (`A JOIN B ON A.reader_id=B.reader_id` 是错的, 会把不相干的记录笛卡尔连一起)。要跨表关联, 走 `get_join_path` 给的业务键 (loan_id / title_id 等经主表的路径)。

一句话: 分片键当**过滤条件**用 (WHERE), 不当**连接条件**用 (JOIN)。

## 跨表关联优先写一条 JOIN, 别拆成"先查 ID 再 IN"

要同时用到两张有边相连的表的字段时, **优先写一条带 JOIN 的 SQL**, 而不是拆成
"先查 A 拿一堆 ID → 再 `WHERE x IN (id1, id2, ...)` 查 B"。

```sql
-- 推荐: 一条 JOIN 答完 (走 get_join_path 给的列对)
SELECT r.TARGET_ID, b.LIB_BOOK_NAME
FROM lib_tag_rel r JOIN lib_book b ON b.LIB_BOOK_ID = r.TARGET_ID
WHERE r.TAG_ID = 7001

-- 不推荐: 拆两段, 把 ID 抄进 IN 列表
SELECT TARGET_ID FROM lib_tag_rel WHERE TAG_ID = 7001   -- 再手抄结果
SELECT LIB_BOOK_NAME FROM lib_book WHERE LIB_BOOK_ID IN (90000101, 90000102)
```

拆两段的三个实际代价 (不只是风格问题):

1. **会漏数据**: 第一段默认带 `LIMIT` 或只返回前若干行时, 你抄进 `IN` 的 ID
   就是不全的, 第二段结果跟着不全 —— 答案看起来完整, 其实少了行, 属于
   静默答错。
2. **多轮往返**: 一次 JOIN 能答的事拆成十几次 `execute_sql`, 慢且容易中途
   跑偏 (拆到十几次仍没拼出答案的情况真发生过)。
3. **绕过本体校验**: JOIN 的连接列对会被系统按本体登记的边核对 (写错列对会
   当场告诉你正确的列); 而 `IN (字面量)` 没有可核对的连接关系, 写错了也没人
   提醒你。

**什么时候仍该分两段查** (这些是正当理由, 不是偷懒):

- **跨库**: `get_join_path` 的多跳路径标了 `cross_source: true`, 说明途中跨了
  库, 物理上不能写一条 SQL JOIN —— 必须按它带的 `segments` 分段分别查, 用前一段
  `exports` 的列接力到后一段的 `needs`。这时用 `IN` 是对的。
- 中间要先看一眼数据才能决定下一步查什么 (探索阶段)。
- 卡片标了断腿 —— 那就按上面"知识边界"停下回问, 两种写法都不该硬编。

连接列对一律来自 `get_join_path`, 别自己猜; 也别拿分片键当连接条件 (见上一节)。

## 两张表不直接相连时 (multi_hop_paths)

`get_join_path` 的 `paths` 是空的、不等于这两张表连不上 —— 先看有没有
`multi_hop_paths`。它是**经中转表**的路径, 每条形如:

```json
{"hops": 2, "cross_source": false,
 "edges": [{"from": "lib_loan", "to": "lib_book_obj_rel",
            "source_col": "loan_id", "target_col": "loan_id"},
           {"from": "lib_book_obj_rel", "to": "lib_book",
            "source_col": "lib_book_id", "target_col": "lib_book_id"}]}
```

用法: 挑**第一条** (已按跳数少/语义边多排好序), 把 `edges` 逐条拼成 JOIN,
中转表一起写进 FROM。边是连通的 —— 前一条的 `to` 就是后一条的 `from`,
列对也已对齐, 照抄 `source_col = target_col` 即可。

```sql
-- 上面那条路径 (lib_loan 经 lib_book_obj_rel 到 lib_book), 一条 SQL 拼完
SELECT p.LIB_LOAN_ID, oi.LIB_BOOK_ID
FROM lib_loan p
JOIN lib_book_obj_rel r ON r.LIB_LOAN_ID = p.LIB_LOAN_ID
JOIN lib_book oi ON oi.LIB_BOOK_ID = r.LIB_BOOK_ID
WHERE p.READER_ID = ...
```

两个键**都空**才是真连不上: 别自己猜列名硬 JOIN, 按上面"知识边界"停下回问。

## 在用表 + 归档表 (卡片带 `archive` / `live`)

卡片带 `archive: "xxx_his"` 说明这类记录**办完后会从在用表迁到归档表**, 两张表合起来
才是全集 (归档表通常比在用表大好几倍)。所以:

- 按号码 / 客户 / ID 找记录时, **在用表和归档表都要查**, 合并后再答。在用表查到一条
  不代表"只有一条" —— 已办完的那些在归档表里。
- 下游表 (工单 / 回调 / 流水) 的 ID 在在用表里查不到时, 第一去处是归档表, 用
  `get_join_path` 拿到归档表的路径。
- 回答时说清楚每条记录来自在用还是归档 (在办 vs 已办完)。

## 问题里有"当前 / 我 / 本人"而你不知道是谁时, 先回问

"当前馆员""我的借书篮""本人名下"这类指代依赖登录会话。会话里**没有给出**具体的
馆员 ID / 读者 ID 时, 不要自己挑一个 (比如"最活跃的那个馆员") 代入作答 ——
那是在回答另一个问题, 看起来有理有据, 其实答非所问。

正确做法: 不查库, 直接回问用户要那个 ID (例: "请告诉我当前操作馆员的工号 /
LIBRARIAN_ID, 我再查他的借书篮")。可以顺带说明拿到 ID 后会查哪几张表。

## 答案里的每个数字都要来自聚合 SQL

不管问题怎么问, 只要你的回答里会出现**计数 / 合计 / 金额总和 / 分布** (哪怕只是顺带
一句"共 N 条"), 这个数就必须是某条 `execute_sql` 里 `COUNT` / `SUM` (+ `GROUP BY`)
直接返回的值。判断标准看你要写的答案, 不看问题措辞。

- 已经 `SELECT` 出明细行的, 别在思考里逐行数、逐笔加 —— 慢, 容易数错, 还会把输出
  额度耗在数数上导致答案被截断。补一条聚合 SQL, 两者对不上时以聚合结果为准。
- 要同时给明细和合计: 明细只列关键几列 (或前若干条), 合计另写一条聚合 SQL。
- 要拆分就 `GROUP BY` 那个维度, 一次拿全, 别按维度值一条条查。
- 聚合结果里出现的**任何码值列** (`GROUP BY` 的状态 / 类型列), 写进答案前先
  `get_enum_values` 拿码表翻译, 别凭码值长相或印象猜含义 —— 同一个码在不同实体里
  含义可以完全不同 (例如码 `C` 在一个实体里是"已续借", 在另一个实体里是"已注销")。

## 为补列而 JOIN 的表, 默认 LEFT JOIN

看 SQL 结构判断, 不看问题类型: 如果结果行的范围由表 A (及其 WHERE) 决定, 而 JOIN
表 B 只是为了**补列** (名称 / 描述 / 规格 / 字典翻译), 那么 B 缺行时 `INNER JOIN`
会**静默丢掉** A 的行 —— 答案看着完整, 其实少了。字典表、规格表、桥表另一端的目标
表都可能不全, 卡片 `query_hints` 写"必走这一跳"也只说明路径, 不保证每个 ID 都查得到。

- 这类 JOIN 默认写 `LEFT JOIN`, B 的列为空的行照样取回; 或先对 A 按同样条件
  `COUNT(*)`, 再对比 JOIN 后的行数。
- 有行对不上时, 把 B 里查无的那几个 ID 列出来, 计数两个口径都给 (例: "某读者 12 条
  借阅, 其中 10 条能对上书目名, 另 2 条的 BOOK_ID 在书目表里查无")。
- JOIN B 本身就是过滤条件 (问题要的就是"在 B 里存在的那些") 时, `INNER JOIN` 才是对的。

## 敏感字段

卡片里 `sensitive: true` 的字段 (如借书卡号 CARD_NO) 该查就查、该给用户看就给用户看 —— 这是查询目的本身, 不用回避。你不需要对这类字段做任何特殊处理, 系统会在日志层面处理落盘保护, 跟你的回答无关。
