"""tools 包: import 即触发装饰器副作用, 全部工具自动注册到 base._REGISTRY.

agent loop 通过 get_all_tools() 拿 (schemas, impls);
main.py 通过 set_retriever(store) / set_ontology(path) / set_db_conn(conn) 注入。
"""
from agent.tools import list_dir, ontology_cards, ontology_concept, ontology_enum, ontology_join, rag_query, read_file, skill_tools, sql_exec  # noqa: F401
from agent.tools.base import get_all_tools  # noqa: F401 (re-export)
from core.ontology_view import set_ontology  # noqa: F401 (re-export, main.py 用)
from agent.tools.rag_query import set_retriever  # noqa: F401 (re-export, main.py 用)
from agent.tools.sql_exec import set_connections, set_source_configs, set_source_dialects, set_sensitive_cols  # noqa: F401 (re-export, main.py 用)
