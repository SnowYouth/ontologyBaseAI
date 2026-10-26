import pytest
from capabilities.obqc.cache import PhysicalColumnCache

def test_cache_get_set():
    cache = PhysicalColumnCache()
    cols = {"COL1", "COL2"}
    cache.set("lib", "MY_TABLE", cols)
    assert cache.get("lib", "MY_TABLE") == cols

def test_cache_miss_返回None():
    cache = PhysicalColumnCache()
    assert cache.get("lib", "UNKNOWN_TABLE") is None

def test_cache_按source分隔():
    """同表名不同 source → 独立缓存 (spec §3.7: 键 (source, table))"""
    cache = PhysicalColumnCache()
    cache.set("lib", "LIB_READER", {"COL_A"})
    cache.set("lib2", "LIB_READER", {"COL_B"})
    assert cache.get("lib", "LIB_READER") == {"COL_A"}
    assert cache.get("lib2", "LIB_READER") == {"COL_B"}
