"""capabilities/obqc/cache.py — 两层缓存 (spec §3.7)。

- PhysicalColumnCache: 物理表列清单, 进程级, 不热失效 (业务 DDL 罕见)
- 本体索引缓存: 不单设, 复用 build_obqc_index (每次从 get_ontology() 现建,
  core/ontology.py build_indexes 重建时本体已刷新, obqc_check 下次调用自动拿新快照)
"""

class PhysicalColumnCache:
    """物理表列清单缓存, 键 (source, table), 值 set[str]。进程级单例。"""

    def __init__(self):
        self._cache: dict = {}  # {(source, table_upper): set[str]}

    def get(self, source: str, table: str) -> set | None:
        """查缓存; 未命中返回 None (调用方打库+回填)。"""
        return self._cache.get((source, table.upper()))

    def set(self, source: str, table: str, columns: set):
        """回填缓存。"""
        self._cache[(source, table.upper())] = columns

    def clear(self):
        """清空缓存 (测试用; 生产不调)。"""
        self._cache.clear()


# 模块级单例 (tools/sql_exec 注入后全局共享)
_physical_col_cache: PhysicalColumnCache | None = None


def set_physical_col_cache(cache: PhysicalColumnCache):
    """core/bootstrap 启动期注入单例。"""
    global _physical_col_cache
    _physical_col_cache = cache


def get_physical_col_cache() -> PhysicalColumnCache | None:
    """obqc/validators 取单例。"""
    return _physical_col_cache
