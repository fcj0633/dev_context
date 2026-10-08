"""Deterministic structural proof, followed by one semantic batch."""
from dataclasses import asdict, dataclass, replace
import re
from devcontext.agentic.coverage import _missing_sources
from devcontext.agentic.evidence_models import RequirementCoverage
from devcontext.context.relations import ConfirmedPath


def matches_hint(symbol, hint):
    # Hints bind to actual indexed identities; they never create proof.
    if not hint:
        return True
    key = symbol.symbol_key
    body = key[2:]
    requested = hint.replace('#', '.').removeprefix('T:').removeprefix('M:').removeprefix('C:')
    qualified = body.replace('#', '.')
    return qualified == requested or qualified.endswith('.' + requested) or qualified.split('(')[0] == requested or qualified.split('(')[0].endswith('.' + requested)


def anchor_symbols(need, symbols):
    spec = need.spec
    if spec is None:
        return ()
    kinds = {'NEED_SYMBOL': {'CLASS', 'INTERFACE', 'METHOD', 'CONSTRUCTOR'},
             'NEED_METHOD': {'METHOD'}, 'NEED_CLASS': {'CLASS', 'INTERFACE'}}[spec.anchor_requirement]
    return tuple(s for s in symbols if s.state == 'CONFIRMED' and s.symbol_kind in kinds and matches_hint(s, spec.anchor_hint))


@dataclass(frozen=True, slots=True)
class StructuralCoverage:
    requirement_id: str
    state: str
    missing: tuple[str, ...] = ()
    matched_relations: tuple[tuple, ...] = ()


class StructuralCoverageChecker:
    def check(self, requirement, workspace, round_index=None, allowed_chunk_ids=None):
        """只验证当前需求、当前轮次可见的可靠结构证据。

        allowed_chunk_ids 用于语义结果的端点对齐：语义采信的 Chunk 必须
        自身包含所需关系/路径的全部端点，不能借 Workspace 中未采信的正文
        补齐证明。此过滤不扩图、不生成正文，也不改变 Relation 的物理方向。
        """
        needs = [(i, n) for i, n in enumerate(requirement.retrieval_needs) if n.need_type in {'RELATION', 'PATH'}]
        if not needs:
            return StructuralCoverage(requirement.id, 'NOT_REQUIRED')
        if workspace is None:
            return StructuralCoverage(requirement.id, 'UNVERIFIED', ('结构验证工作区不可用',))
        symbols = {s.symbol_key: s for s in workspace.symbols_for(requirement.id, round_index)
                   if allowed_chunk_ids is None or s.chunk_id in allowed_chunk_ids}
        relations = workspace.relations_for(requirement.id, round_index)
        probes = workspace.probes_for(requirement.id, round_index)
        missing, matched, states = [], [], []
        for index, need in needs:
            anchors = anchor_symbols(need, symbols.values())
            current_keys = set(symbols)
            required_types = {s.edge_type for s in need.segments}
            required_directions = {d for s in need.segments for d in (('INCOMING','OUTGOING') if s.direction == 'BOTH' else (s.direction,))}
            complete = any(p.status == 'COMPLETED' and not p.truncated and index in p.need_indices
                           and required_types <= set(p.edge_types)
                           and required_directions <= {d for direction in p.directions for d in (('INCOMING','OUTGOING') if direction == 'BOTH' else (direction,))}
                           and (p.scope == 'INDUCED' and current_keys <= set(p.symbol_keys)
                                or p.scope == 'NEIGHBORS' and len(need.segments) == 1 and any(a.symbol_key in p.symbol_keys for a in anchors))
                           for p in probes)
            # An unresolved hint is not a capability; ambiguity requires retrieval.
            if need.spec.anchor_hint and len(anchors) != 1:
                states.append('PARTIAL' if complete else 'UNVERIFIED')
                missing.append('需要唯一确认的锚点及其实体正文')
                continue
            found = None

            def walk(node, position, nodes, used, traversals):
                if position == len(need.segments):
                    target = symbols[node]
                    return (nodes, used, traversals) if matches_hint(target, need.spec.target_hint) else None
                segment = need.segments[position]
                for relation in relations:
                    if relation.edge_type != segment.edge_type:
                        continue
                    next_node, direction = None, None
                    if relation.source == node and segment.direction in {'OUTGOING', 'BOTH'}:
                        next_node, direction = relation.target, 'OUTGOING'
                    elif relation.target == node and segment.direction in {'INCOMING', 'BOTH'}:
                        next_node, direction = relation.source, 'INCOMING'
                    if next_node is None or next_node in nodes or next_node not in symbols or relation.identity in used:
                        continue
                    source_ref, target_ref = workspace.get(relation.source_chunk_id), workspace.get(relation.target_chunk_id)
                    if source_ref is None or target_ref is None:
                        continue
                    # Method declarations and implementations need METHOD chunks,
                    # not class summaries with the same method name.
                    if any(symbols[k].symbol_kind == 'METHOD' and ref.chunk_type != 'METHOD'
                           for k, ref in ((relation.source, source_ref), (relation.target, target_ref))):
                        continue
                    if relation.edge_type == 'OVERRIDES':
                        body = re.sub(r'/\*.*?\*/|//[^\n]*', '', source_ref.content or '', flags=re.S)
                        if not re.search(r'\)\s*(?:throws\s+[^;{]+)?\{', body):
                            continue
                    result = walk(next_node, position + 1, nodes + (next_node,), used + (relation.identity,), traversals + (direction,))
                    if result:
                        return result
                return None

            for anchor in anchors:
                found = walk(anchor.symbol_key, 0, (anchor.symbol_key,), (), ())
                if found:
                    break
            if found:
                nodes, ids, directions = found
                matched.extend(ids)
                workspace.add_path(ConfirmedPath(requirement.id, index, nodes, ids, directions, round_index or 0))
                continue
            # A successful induced query proves only relations amongst these
            # endpoints, never absence of external/runtime neighbors.
            states.append('PARTIAL' if complete else 'UNVERIFIED')
            missing.append(f'尚未证明 {need.need_type}：' + ' → '.join(f'{s.edge_type}/{s.direction}' for s in need.segments) + ' 及所需端点正文')
        state = 'UNVERIFIED' if 'UNVERIFIED' in states else ('PARTIAL' if states else 'SATISFIED')
        return StructuralCoverage(requirement.id, state, tuple(missing), tuple(dict.fromkeys(matched)))


class CombinedCoverage:
    """统一按来源、结构、语义顺序验证，前层缺口阻止后层模型放行。

    semantic_checker 可以是生产 CoverageChecker 或评测 OracleChecker，
    两者均经过相同的确定性约束；每批仅对前两层通过的需求调用一次 checker。
    """
    def __init__(self, semantic_checker, hydrator=None):
        self.semantic = semantic_checker
        self.hydrator = hydrator
        self.structural = StructuralCoverageChecker()
        self.last_client = None
        self.last_error = None
        self.last_diagnostics = {}

    def check(self, requirements, view):
        self.last_client = None
        self.last_diagnostics = {}
        workspace = getattr(view, 'workspace', None)
        round_index = getattr(view, 'round_index', None)
        results, eligible = {}, []
        source_eligible = []
        for r in requirements:
            items = list(view.items_for(r.id))
            gaps = _missing_sources(r, items)
            present = {i.citation.source_type for i in items}
            requested_sources = {'DOCUMENT' if n.need_type == 'DOCUMENT' else 'CODE' for n in r.retrieval_needs}
            for source in sorted(requested_sources - present):
                gap = f'缺少 RetrievalNeed 所需的 {source} 直接证据'
                if not any(source in g for g in gaps):
                    gaps.append(gap)
            if gaps:
                results[r.id] = RequirementCoverage(r.id, 'PARTIAL' if items else 'MISSING', tuple(i.chunk_id for i in items), tuple(gaps), '所需来源证据尚未取得', 'rules')
                self.last_diagnostics[r.id] = {'source': 'PARTIAL' if items else 'MISSING', 'structural': 'NOT_QUERIED', 'semantic': 'NOT_QUERIED'}
                if workspace is not None:
                    from devcontext.context.relations import RelationProbe
                    needs = [(i,n) for i,n in enumerate(r.retrieval_needs) if n.segments]
                    if needs:
                        workspace.add_probe(RelationProbe(r.id, tuple(i for i,_ in needs),
                            tuple(s.symbol_key for s in workspace.symbols_for(r.id,round_index)),
                            tuple(sorted({s.edge_type for _,n in needs for s in n.segments})),
                            tuple(sorted({s.direction for _,n in needs for s in n.segments})),
                            round_index or 0, 'NOT_QUERIED', error='source_check_failed'))
            else:
                source_eligible.append(r)
        if self.hydrator is not None and workspace is not None:
            self.hydrator.hydrate(source_eligible, workspace, round_index or 0)
        for r in source_eligible:
            structural = self.structural.check(r, workspace, round_index)
            self.last_diagnostics[r.id] = {'source': 'SATISFIED', 'structural': asdict(structural), 'semantic': 'NOT_QUERIED'}
            if structural.state in {'SATISFIED', 'NOT_REQUIRED'}:
                eligible.append(r)
            else:
                results[r.id] = RequirementCoverage(r.id, structural.state, tuple(i.chunk_id for i in view.items_for(r.id)), structural.missing, '当前结构证据未完成证明；不表示运行时关系不存在', 'rules')
        if eligible:
            checked = self.semantic.check(tuple(eligible), view)
            self.last_client = getattr(self.semantic, 'last_client', None)
            self.last_error = getattr(self.semantic, 'last_error', None)
            for coverage in checked:
                self.last_diagnostics[coverage.requirement_id]['semantic'] = coverage.state
                requirement = next(r for r in eligible if r.id == coverage.requirement_id)
                if coverage.satisfied and any(n.segments for n in requirement.retrieval_needs):
                    # TA07 曾只引用入口正文，却借无关 CALLS 边被判满足。
                    # 在实际采信的 Chunk 中再次核对结构，避免两套证明各自
                    # 成立却没有证明同一个目标；这里只做确定性检查，不加模型调用。
                    aligned = self.structural.check(requirement, workspace, round_index, set(coverage.evidence_ids))
                    self.last_diagnostics[coverage.requirement_id]['semantic_endpoint_alignment'] = asdict(aligned)
                    if aligned.state != 'SATISFIED':
                        coverage = replace(coverage, state='PARTIAL',
                            missing_criteria=('语义采信的证据尚未包含同一可靠关系/连续路径的全部端点正文；需要补齐目标及中间方法正文',),
                            reason='语义判定与实际结构端点未对齐：' + coverage.reason)
                results[coverage.requirement_id] = coverage
        return tuple(results[r.id] for r in requirements)


def combined(checker, hydrator=None):
    if isinstance(checker, CombinedCoverage):
        if hydrator is not None:
            checker.hydrator = hydrator
        return checker
    return CombinedCoverage(checker, hydrator)
