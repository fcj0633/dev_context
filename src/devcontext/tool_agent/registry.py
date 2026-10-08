from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    argument: str
    allowed_sources: frozenset[str]
    description: str

    def to_dict(self):
        return {"name": self.name, "description": self.description,
                "allowed_sources": sorted(self.allowed_sources),
                "schema": {"type": "object", "properties": {self.argument: {"type": "string"}},
                           "required": [self.argument], "additionalProperties": False}}


class ToolRegistry:
    def __init__(self, tools):
        self.tools = tools
        code = frozenset({"CODE", "BOTH", "ANY"})
        docs = frozenset({"DOCUMENT", "BOTH", "ANY"})
        self.specs = {s.name: s for s in (
            ToolSpec("search_code", "query", code, "Search repository code for business behavior."),
            ToolSpec("search_docs", "query", docs, "Search repository design and verification documents."),
            ToolSpec("find_symbol", "name", code, "Resolve a name or qualified signature; ambiguity returns candidates only."),
            ToolSpec("find_callers", "symbol_key", code, "Find incoming CALLS for a CONFIRMED method."),
            ToolSpec("find_callees", "symbol_key", code, "Find outgoing CALLS and CONSTRUCTS for a CONFIRMED method or constructor."),
            ToolSpec("find_implementations", "symbol_key", code, "Find implementations/overrides of a CONFIRMED type or method."),
            ToolSpec("find_hierarchy", "symbol_key", code, "Find parents and children of a CONFIRMED type, up to two hops."),
        )}
        self.handlers = {name: getattr(tools, name) for name in self.specs}

    def specifications(self):
        return [spec.to_dict() for spec in self.specs.values()]
