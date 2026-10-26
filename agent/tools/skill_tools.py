"""agent/tools/skill_tools.py — load_skill 工具 (渐进披露 L2 激活)。

agent 从 L1 清单 (每轮拼进 system 的 name+desc) 判断某 skill 相关后, 调 load_skill
调出该 skill 的 SKILL.md 正文。只回正文, references 由 agent 用 read_file 按需读。

category=serving: skill 是 agent 核心能力, 上线也该能用; 且 L1 清单每轮无门控拼
system, load_skill 必须 serving 才不会被 serving profile 的 _filter_local 剔除 (断链)。
"""
from agent.toolconfig import skills
from agent.tools.base import register_tool


@register_tool(category="serving")
def load_skill(name: str) -> dict:
    """调出一个 skill 的完整能力包正文 (SKILL.md)。当你从技能清单里判断某个 skill 与当前任务相关时调用它, 拿到详细步骤/约定后再据此行动。"""
    return {"name": name, "content": skills.read_body(name)}
