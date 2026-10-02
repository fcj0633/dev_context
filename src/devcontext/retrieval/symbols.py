from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from devcontext.context import EvidenceWorkspace


# Deliberately restated from the symbol-shaped subset of
# ``routing/router.py::_CODE_PATTERNS`` rather than imported from it: that module
# is out of scope for this experiment and must not be edited, so importing one of
# its private names would couple the two.
#
# The generic Chinese hints in that tuple (代码/源码, 如何实现, 在哪里, 业务方法,
# 责任链 handler ...) are excluded on purpose. They say "this question wants code
# evidence", which is a routing decision. This module answers a narrower
# question: "does this text name something that actually exists in the project?"
# Only the latter justifies an exact-match search.
_SYMBOL_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Qualified member access: PurchaseTicketTxService.doPurchaseInTransaction
    re.compile(r"\b[A-Za-z_$][A-Za-z0-9_$]*\.[A-Za-z_$][A-Za-z0-9_$]*\b"),
    # Any identifier carrying an internal capital: loadBucket, closeTimeoutOrder,
    # PurchaseTicketTxService. The router's own version requires a leading capital
    # and so misses lowerCamelCase, which is what most Java methods look like.
    re.compile(r"\b[A-Za-z_$][A-Za-z0-9_$]*[A-Z][A-Za-z0-9_$]*\b"),
    # Annotations: @Transactional
    re.compile(r"@[A-Za-z_$][A-Za-z0-9_$]*"),
    # Source files: UserReuseUtil.java
    re.compile(r"\b[A-Za-z_$][A-Za-z0-9_$]*\.java\b", re.IGNORECASE),
)

MAX_SYMBOLS = 8


def _is_symbol_shaped(value: str) -> bool:
    # An all-caps token is an acronym (HTTP, SQL, USB), not a project symbol.
    # Anything with a lowercase letter and an internal capital is camelCase, and
    # that is the shape a class or method name has.
    return not (len(value) > 1 and value.isupper())


def symbols_in_text(text: str) -> tuple[str, ...]:
    """Symbol-shaped tokens that appear literally in ``text``.

    This is the only way a *query* can count as naming a symbol, and it matches
    shapes rather than meanings: a token must look like a Java identifier.
    Ordinary domain vocabulary (订单 / 事务 / Redis / 锁 / 补偿) does not match,
    which is the point - the experiment plan is explicit that such words must not
    be treated as code symbols.
    """
    found: list[str] = []
    for pattern in _SYMBOL_PATTERNS:
        for match in pattern.finditer(text):
            value = match.group(0)
            if value not in found and _is_symbol_shaped(value):
                found.append(value)
    return tuple(found[:MAX_SYMBOLS])


def workspace_symbols(
    workspace: "EvidenceWorkspace | None", requirement_id: str
) -> tuple[str, ...]:
    """Symbols already confirmed by evidence retrieved for one requirement.

    Only the citation fields that *name something in the project* count. A file
    path or a document heading says where a thing lives, not what it is called,
    so neither is included here even though both appear in ``_discovered_terms``.
    """
    if workspace is None:
        return ()
    found: list[str] = []
    for ref in workspace.for_requirement(requirement_id):
        citation = ref.citation
        for value in (citation.class_name, citation.symbol_name, citation.signature):
            if value and value not in found:
                found.append(value)
    return tuple(found[:MAX_SYMBOLS])


def detected_symbols(
    query: str,
    workspace: "EvidenceWorkspace | None",
    requirement_id: str,
) -> tuple[str, ...]:
    """Real code symbols behind one search action.

    Two sources qualify, and both are facts rather than model output:

    1. the query text, when the user wrote an actual identifier
       (``PurchaseTicketTxService 的事务边界``);
    2. the workspace, when retrieval has already returned evidence whose citation
       names a class, method or signature.

    Round 0 normally has neither, because nothing has been retrieved yet, so it
    falls through to semantic search. Round 1 normally has the second.
    """
    found = list(symbols_in_text(query))
    for value in workspace_symbols(workspace, requirement_id):
        if value not in found:
            found.append(value)
    return tuple(found[:MAX_SYMBOLS])


def code_strategy_for(
    query: str,
    workspace: "EvidenceWorkspace | None",
    requirement_id: str,
) -> tuple[str, tuple[str, ...]]:
    """The CODE strategy for one action, and the symbols that decided it.

    A symbol the project actually contains is what a keyword search is good at,
    so its presence selects keyword. Its absence - the normal case for a first,
    purely semantic round - selects vector: with no exact term to match, hybrid's
    keyword half contributes ranking noise rather than evidence.
    """
    symbols = detected_symbols(query, workspace, requirement_id)
    return ("keyword" if symbols else "vector"), symbols
