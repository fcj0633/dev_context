"""Request policy is resolved before retrieval; no classifier model call."""
from dataclasses import asdict, dataclass
import re

INTENTS = ("WHAT", "WHY", "HOW", "COMPARE", "DEBUG", "LOCATE", "GENERAL")
INTENT_TASKS = {
    "WHAT": "定义与用途→关键关系→例子与边界",
    "WHY": "原方案能力→当前设计改变的条件→有效原因→代价与前提",
    "HOW": "起始状态→动作→结果→机制依赖→故障分支与边界",
    "COMPARE": "共同问题→相同比较维度→差异→协作关系→选择条件",
    "DEBUG": "预期与现象→可能原因及线索→排查顺序→修复与验证",
    "LOCATE": "具体位置→职责→必要调用关系；没有位置证据就给查找建议",
    "GENERAL": "先回应实际问题，再按理解所需组织说明，不因分类不明拒答",
}

def infer_intent(query):
    q = query.lower()
    patterns = {
        "COMPARE": r"比较|对比|区别|差异|compare|versus|\bvs\b",
        "DEBUG": r"报错|异常排查|排查|故障|失败原因|不生效|debug|troubleshoot",
        "LOCATE": r"在哪里|在哪个|哪个文件|定位|找到.*(?:类|方法)|locate|where",
        "WHY": r"为什么|为何|why",
        "HOW": r"如何|怎么|流程|how",
        "WHAT": r"是什么|什么是|介绍|定义|what",
    }
    matched = [kind for kind, pattern in patterns.items() if re.search(pattern, q)]
    return (matched[0] if matched else "GENERAL", tuple(matched[1:]))

@dataclass(frozen=True, slots=True)
class RequestPolicy:
    profile: str
    reasoning_effort: str
    primary_intent: str
    secondary_intents: tuple[str, ...]
    reader_assumption: str
    latency_target_seconds: float
    hard_timeout_seconds: float | None = None

    def to_dict(self):
        return asdict(self)

def resolve_policy(query, settings, *, profile=None, reasoning_effort=None, hard_timeout=None):
    selected = profile or settings.answer_profile
    effort = reasoning_effort or settings.answer_reasoning_effort or ("low" if selected == "fast" else "high")
    primary, secondary = infer_intent(query)
    reader = "会基础 Java，需要解释本题必要的事务、并发和缓存关系"
    explicit = re.search(r"(?:我是|我的基础是|面向|读者是)([^。\n]{1,100})", query)
    if explicit:
        reader = explicit.group(0)
    timeout = hard_timeout if hard_timeout is not None else settings.answer_hard_timeout_seconds
    if selected not in {"fast", "full"} or effort not in {"low", "medium", "high"}:
        raise ValueError("invalid profile or reasoning effort")
    if timeout is not None and timeout <= 0:
        raise ValueError("hard timeout must be positive")
    return RequestPolicy(selected, effort, primary, secondary, reader,
        settings.answer_fast_latency_target_seconds if selected == "fast" else settings.answer_full_latency_target_seconds,
        timeout)
