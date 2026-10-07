"""Immutable V5 retrieval contracts and the single legacy normalization seam."""
from dataclasses import asdict, dataclass
import re

EDGES = frozenset({'CALLS', 'CONSTRUCTS', 'IMPLEMENTS', 'EXTENDS', 'OVERRIDES'})
DIRECTIONS = frozenset({'INCOMING', 'OUTGOING', 'BOTH'})
ANCHORS = frozenset({'NEED_SYMBOL', 'NEED_METHOD', 'NEED_CLASS'})


@dataclass(frozen=True, slots=True)
class PathSegment:
    edge_type: str
    direction: str = 'OUTGOING'

    def __post_init__(self):
        if self.edge_type not in EDGES or self.direction not in DIRECTIONS:
            raise ValueError('Invalid physical relation or traversal direction')


@dataclass(frozen=True, slots=True)
class RelationSpec:
    edge_type: str
    direction: str
    anchor_requirement: str = 'NEED_SYMBOL'
    anchor_hint: str = ''
    target_hint: str = ''

    def __post_init__(self):
        PathSegment(self.edge_type, self.direction)
        if self.anchor_requirement not in ANCHORS:
            raise ValueError('Invalid anchor requirement')
        if not all(isinstance(x, str) for x in (self.anchor_hint, self.target_hint)):
            raise ValueError('Hints must be text')


@dataclass(frozen=True, slots=True)
class PathSpec:
    segments: tuple[PathSegment, ...]
    anchor_requirement: str = 'NEED_METHOD'
    anchor_hint: str = ''
    target_hint: str = ''
    mode: str = 'CALL_CHAIN'

    def __post_init__(self):
        if not isinstance(self.segments, tuple) or not 1 <= len(self.segments) <= 2:
            raise ValueError('Paths need one or two immutable physical segments')
        if any(not isinstance(s, PathSegment) for s in self.segments):
            raise ValueError('Invalid path segments')
        if self.anchor_requirement not in ANCHORS or self.mode != 'CALL_CHAIN':
            raise ValueError('Invalid path contract')
        if not all(isinstance(x, str) for x in (self.anchor_hint, self.target_hint)):
            raise ValueError('Hints must be text')


@dataclass(frozen=True, slots=True)
class RetrievalNeed:
    need_type: str
    relation_spec: RelationSpec | None = None
    path_spec: PathSpec | None = None

    def __post_init__(self):
        valid = (self.need_type in {'CODE', 'DOCUMENT'} and self.relation_spec is None and self.path_spec is None
                 or self.need_type == 'RELATION' and isinstance(self.relation_spec, RelationSpec) and self.path_spec is None
                 or self.need_type == 'PATH' and isinstance(self.path_spec, PathSpec) and self.relation_spec is None)
        if not valid:
            raise ValueError('Single relations and paths must use different fields')

    def to_dict(self):
        return asdict(self)

    @property
    def spec(self):
        return self.relation_spec or self.path_spec

    @property
    def segments(self):
        return self.path_spec.segments if self.path_spec else ((PathSegment(self.relation_spec.edge_type, self.relation_spec.direction),) if self.relation_spec else ())


def read_need(raw):
    if not isinstance(raw, dict) or set(raw) - {'need_type', 'relation_spec', 'path_spec'}:
        raise ValueError('Invalid retrieval need fields')
    relation = raw.get('relation_spec')
    path = raw.get('path_spec')
    if relation is not None:
        relation = RelationSpec(**relation)
    if path is not None:
        path = PathSpec(**{**path, 'segments': tuple(PathSegment(**s) for s in path['segments'])})
    return RetrievalNeed(raw['need_type'], relation, path)


def legacy_needs(target, criteria, source):
    """Only legacy/import/fallback entrypoints may infer needs from plain text."""
    text = target + ' ' + criteria
    hints = re.findall(r'\b[A-Z][\w$]*(?:[.#][a-zA-Z_$][\w$]*)?(?:\([^()]*\))?', target)
    hint = next((x for x in hints if any(c.islower() for c in x)), '')
    method = bool(re.search(r'[.#][a-z_$]', hint))
    lower = text.lower()
    edge, direction, anchor = None, 'OUTGOING', 'NEED_METHOD'
    if source != 'DOCUMENT':
        if any(x in lower for x in ('谁调用', '哪里调用', '被谁调用', '调用者', '调用方', 'caller', 'called by')):
            edge, direction = 'CALLS', 'INCOMING'
        elif any(x in lower for x in ('由谁实现', '由哪个方法实现', '实现类', '接口实现', 'implementations', '谁实现')):
            edge, direction, anchor = ('OVERRIDES' if method or '接口方法' in text else 'IMPLEMENTS'), 'INCOMING', ('NEED_METHOD' if method or '接口方法' in text else 'NEED_CLASS')
        elif any(x in lower for x in ('继承', '父类', '子类', 'extends')):
            edge, anchor = 'EXTENDS', 'NEED_CLASS'
            if '子类' in text:
                direction = 'INCOMING'
        elif any(x in lower for x in ('构造关系', '构造器', 'new 哪', '异常类型', 'constructs')):
            edge = 'CONSTRUCTS'
        elif any(x in lower for x in ('调用哪个', '调用哪些', '下游', 'callee')):
            edge = 'CALLS'
        elif any(x in lower for x in ('调用链', '跨方法', 'call chain')):
            return _with_docs((RetrievalNeed('PATH', path_spec=PathSpec((PathSegment('CALLS'),), anchor_hint=hint)),), source)
    needs = (RetrievalNeed('RELATION', relation_spec=RelationSpec(edge, direction, anchor, hint)),) if edge else (RetrievalNeed('DOCUMENT' if source == 'DOCUMENT' else 'CODE'),)
    return _with_docs(needs, source)


def _with_docs(needs, source):
    return needs + (RetrievalNeed('DOCUMENT'),) if source == 'BOTH' else needs


NEED_PROMPT = '''V5 每条 requirement 必须有 retrieval_needs 数组。need_type=CODE/DOCUMENT/RELATION/PATH。
普通代码解释和文档说明使用 CODE/DOCUMENT，不因“流程”一词要求图。BOTH 必须覆盖代码与文档。
RELATION 使用 relation_spec={edge_type,direction,anchor_requirement,anchor_hint,target_hint}，禁止 path_spec。
谁调用=CALLS/INCOMING；下游=CALLS/OUTGOING；类型实现=IMPLEMENTS/INCOMING；方法实现=OVERRIDES/INCOMING；继承=EXTENDS；显式构造=CONSTRUCTS。
PATH 使用 path_spec={mode:"CALL_CHAIN",segments:[{edge_type,direction}],anchor_requirement,anchor_hint,target_hint}，禁止 relation_spec。路径1–2段，较长任务拆需求。
物理 edge_type 仅 CALLS/CONSTRUCTS/IMPLEMENTS/EXTENDS/OVERRIDES。direction=INCOMING/OUTGOING/BOTH；anchor_requirement=NEED_SYMBOL/NEED_METHOD/NEED_CLASS。
hint 只复用用户标识符，不虚构实体，不写 confirmed key。CODE/DOCUMENT 不带关系字段。'''

NEED_PROMPT += '''
以下是必须遵守的分类契约，优先于一般 HOW/Full 教学提示：
1. “某方法调用哪些下游方法”是单关系 RELATION/CALLS/OUTGOING，不是单段 PATH，也不能只输出 CODE。
2. “某方法显式构造哪个异常对象”必须包含 RELATION/CONSTRUCTS/OUTGOING；CODE 正文可以作为额外 need，不能代替关系 need。
3. 用户明确要求两段连续路径，例如 Controller 方法→接口方法→实现方法，必须用一条 PATH need，segments=[CALLS/OUTGOING,OVERRIDES/INCOMING]。不要拆成两条独立 RELATION；只有超过两段才拆需求。
4. anchor_hint/target_hint 只能填用户已提供的 Java 标识符/限定名称/签名。未提供具体目标则 target_hint=""，不能填“下游方法”“实现类”等普通词。
5. 对独立的方法实现问题用 RELATION/OVERRIDES/INCOMING，对独立的类实现接口问题用 RELATION/IMPLEMENTS/INCOMING。
例如下游：{"need_type":"RELATION","relation_spec":{"edge_type":"CALLS","direction":"OUTGOING","anchor_requirement":"NEED_METHOD","anchor_hint":"OrderServiceImpl.closeTimeoutOrder","target_hint":""}}。
例如构造：{"need_type":"RELATION","relation_spec":{"edge_type":"CONSTRUCTS","direction":"OUTGOING","anchor_requirement":"NEED_METHOD","anchor_hint":"OrderServiceImpl.closePayOrder","target_hint":""}}。
例如两段路径：{"need_type":"PATH","path_spec":{"mode":"CALL_CHAIN","segments":[{"edge_type":"CALLS","direction":"OUTGOING"},{"edge_type":"OVERRIDES","direction":"INCOMING"}],"anchor_requirement":"NEED_METHOD","anchor_hint":"TicketOrderController.createTicketOrder","target_hint":""}}。
'''
