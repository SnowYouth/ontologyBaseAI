"""capabilities.ontology — 方向2 本体重构存储层 (SQLite 权威可编辑源).

代码进 git; 数据文件 (*.db) gitignore。
存储层落 SQLite 支持节点级 CRUD, load_raw() 组装成 core.ontology_view.build_indexes
要的 dict, 让 agent OBDA 消费侧零改动。
"""
