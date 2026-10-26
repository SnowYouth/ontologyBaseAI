"""
rag 模块：阶段 1 的文档检索增强能力。

四个文件分工：
- chunker:  把长文档切成小块 (chunks)
- embedder: 把文本块转成向量 (embeddings)
- store:    存向量 + 按问题向量检索 top-k 相似块
- playground: 把上面三个串起来跑一遍，看每一步的中间结果
"""
