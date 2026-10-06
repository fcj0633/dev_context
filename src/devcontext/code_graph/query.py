from __future__ import annotations


class CodeGraphQuery:
    def __init__(self, store):
        self.store = store

    def query(self, command: str, value: str) -> dict:
        with self.store.session() as session:
            if command == "symbol":
                return {"symbols": session.find_symbols([value]), "repository": self.store.repository}
            symbols = session.symbols_by_keys([value])
            if len(symbols) != 1:
                raise ValueError("Relationship queries require an existing complete symbol_key")
            anchor = symbols[0]
            if command == "callers":
                directions = ((), ("CALLS",))
            elif command == "callees":
                directions = (("CALLS",), ())
            elif command == "implementations":
                directions = ((), ("OVERRIDES",) if anchor["symbol_kind"] == "METHOD" else ("IMPLEMENTS",))
            elif command == "hierarchy":
                directions = (("EXTENDS", "IMPLEMENTS"), ("EXTENDS", "IMPLEMENTS"))
            else:
                raise ValueError("Unknown graph query")
            if command == "hierarchy" and anchor["symbol_kind"] not in {"CLASS", "INTERFACE"}:
                raise ValueError("hierarchy requires a type symbol")
            frontier = [anchor]; seen = {anchor["id"]}; relations = []; truncated = False
            for hop in range(1, 3 if command == "hierarchy" else 2):
                grouped = session.neighbors([row["id"] for row in frontier], *directions, limit=6)
                new_frontier = []
                for node in frontier:
                    neighbors = grouped[node["id"]]
                    truncated |= len(neighbors) > 5
                    for neighbor in neighbors[:5]:
                        relations.append({"from": node["symbol_key"], "to": neighbor["symbol_key"], "hop": hop,
                                          "edge_type": neighbor["edge_type"], "direction": neighbor["direction"],
                                          "resolution_kind": neighbor["resolution_kind"], "source_line": neighbor["source_line"],
                                          "source_column": neighbor["source_column"], "neighbor": neighbor})
                        if neighbor["id"] not in seen:
                            seen.add(neighbor["id"]); new_frontier.append(neighbor)
                frontier = new_frontier
                if not frontier:
                    break
            return {"repository": self.store.repository, "anchor": anchor, "relations": relations,
                    "neighbor_limit": 5, "truncated": truncated, "max_hops": 2 if command == "hierarchy" else 1}
