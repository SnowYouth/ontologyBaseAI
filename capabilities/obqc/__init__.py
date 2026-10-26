"""capabilities/obqc/ — Ontology-Based Query Check: LLM 写完 SQL、执行前的确定性守门员。

纯函数裁判 (spec §2.7): 同一 SQL + 同一本体快照 → 同一 verdict。
本包不依赖 agent loop, 可被本体自演化复用。
"""
from capabilities.obqc.check import obqc_check  # noqa: F401
