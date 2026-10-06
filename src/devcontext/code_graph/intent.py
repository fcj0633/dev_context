from __future__ import annotations

import re

DIRECTIONS = {
    "CALLERS": ((), ("CALLS",)),
    "CALL_CHAIN": (("CALLS", "CONSTRUCTS", "OVERRIDES"), ("OVERRIDES",)),
    "TYPE_HIERARCHY": (("EXTENDS", "IMPLEMENTS", "OVERRIDES"), ("EXTENDS", "IMPLEMENTS", "OVERRIDES")),
    "LOCATION": ((), ()),
    "NONE": ((), ()),
}


def classify_intent(query: str, requirement) -> str:
    text = " ".join((query, requirement.target, requirement.success_criteria)).lower()
    for intent, words in (
        ("CALLERS", ("谁调用", "哪里调用", "被谁调用", "调用方", "调用者", "上游入口", "callers", "called by")),
        ("TYPE_HIERARCHY", ("实现类", "接口实现", "继承", "父类", "子类", "override", "implements", "implementation", "hierarchy")),
        ("CALL_CHAIN", ("调用", "流程", "链路", "经过", "如何执行", "怎么处理", "callees", "call chain", "calls", "flow")),
        ("LOCATION", ("哪里", "定位", "定义", "location", "definition")),
    ):
        if any(word in text for word in words):
            return intent
    return "NONE"


def symbol_hints(query: str) -> list[str]:
    # Complete keys/signatures first; ordinary identifiers are only candidates
    # until the store confirms them. No annotation/file name is a symbol ID.
    tokens = re.findall(r"(?:[TMC]:)?[A-Za-z_$][\w$]*(?:[.#][A-Za-z_$][\w$]*|#<init>)*(?:\([^()]*\))?", query)
    return list(dict.fromkeys(re.sub(r"\s+", "", token) for token in tokens if not token.endswith(".java")))[:32]
