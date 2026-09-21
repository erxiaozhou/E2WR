from __future__ import annotations
import heapq
from enum import Enum
from typing import Iterable, Optional
from reduction_analysis.ASTInfo.AST import ASTINode, BlockNode, IfNode, InstsNode, LoopNode, NodeList, RootNode, traverse_ast
from reduction_analysis.ASTInfo.ASTInfo import ASTInfo
from reduction_analysis.ASTState import ASTState


class ScoreStrategy(Enum):
    SIZE = 1
    DISTRIBUTION = 2


class ToReduceTask: 
    def get_first_node(self):
        raise NotImplementedError

    def get_nodes(self):
        raise NotImplementedError

    def get_func_idxs(self) -> set[int]:
        nodes = self.get_nodes()
        func_idxs = set()
        for node in nodes:
            func_idxs.add(node.func_idx)
        return func_idxs

    def get_nodes_info(self) -> str:
        strs = []
        for node in self.get_nodes():
            strs.append(node.get_node_info())
        core_s = '; '.join(strs)
        return f'[{core_s}]'

    def get_total_inst_num(self) -> int:
        total_inst_num = 0
        for node in self.get_nodes():
            total_inst_num += node.get_length()
        return total_inst_num
    

class OneCFNodeReduceTask(ToReduceTask):
    def __init__(self, *, node:ASTINode):
        self.node = node

    def get_first_node(self):
        return self.node

    def get_nodes(self):
        return [self.node]


class NodeListsReduceTask(ToReduceTask):
    def __init__(self, *, nodes: list[NodeList]):
        self.nodes = nodes
    def get_first_node(self):
        return self.nodes[0]
    def get_nodes(self):
        return self.nodes


def get_tree_node_lengths(root: ASTINode, *, strategy: ScoreStrategy) -> dict[ASTINode, int]:
    if strategy != ScoreStrategy.SIZE:
        raise NotImplementedError('get_tree_node_lengths currently only supports ScoreStrategy.SIZE')

    lengths: dict[ASTINode, int] = {}

    def _calc(node: ASTINode) -> int:
        if isinstance(node, InstsNode):
            length = len(node.insts)
        elif isinstance(node, NodeList):
            length = 0
            for child in node.sub_nodes:
                length += _calc(child)
        elif isinstance(node, (BlockNode, LoopNode)):
            length = 2 + _calc(node.sub_node_list)
        elif isinstance(node, IfNode):
            length = 2 + _calc(node.if_sub_node_list)
            if node.has_else:
                length += 1 + _calc(node.else_sub_node_list)
        else:
            raise TypeError(f'Unsupported AST node type: {type(node)}')
        lengths[node] = length
        return length

    _calc(root)
    return lengths

def get_node_size_score(node: ASTINode, max_length: Optional[float] = None) -> float:
    try:
        length_score = node.get_length()
    except Exception:
        length_score = 1
    if isinstance(node, NodeList):
        return 1.0 + float(length_score)
    if isinstance(node, InstsNode):
        return 0.0
    if max_length is not None and max_length > 0:
        return float(length_score) / max_length
    return float(length_score)

def get_node_size_score_proi_not_block(node: ASTINode) -> float:
    try:
        length_score = node.get_length()
    except Exception:
        length_score = 1
    if isinstance(node, NodeList):
        return float(length_score + 0.5)
    if isinstance(node, InstsNode):
        return float(length_score)
    return float(length_score)
    # if isinstance(node, LoopNode):
    #     return float(length_score)
    # if isinstance(node, (BlockNode, IfNode, LoopNode)):
    return float((length_score - 2) / 100) + 2


def _is_cf_node(node: ASTINode) -> bool:
    if isinstance(node, NodeList):
        return False
    if isinstance(node, InstsNode):
        return False
    return True


def get_node_score_consider_distribution(node: ASTINode, all_inst_num: int) -> float:
    if not isinstance(node, NodeList):
        try:
            length_score = node.get_length()
        except Exception:
            length_score = 1
        if isinstance(node, InstsNode):
            return float(length_score * (all_inst_num - length_score))
        return float(((length_score) / 10 + 2) * (all_inst_num - length_score))

    all_subnode_lengths = node.get_sub_node_lengths()
    block_node_sizes: list[int] = []
    inst_num = 0
    for sub_node in node.get_sub_nodes():
        if isinstance(sub_node, InstsNode):
            inst_num += len(sub_node.insts)
        else:
            block_node_sizes.append(sub_node.get_length())

    total_size = sum(all_subnode_lengths) + inst_num
    to_minus_2 = sum([size**2 for size in block_node_sizes]) + inst_num
    expectation = total_size * all_inst_num - to_minus_2
    return float(expectation)


def get_node_naive_score_in_batch_for_heap(
    ast_state: ASTState,
    considered_func_idxs: Optional[set[int]] = None,
) -> list[tuple[float, int, ASTINode]]:
    nodes = ast_state.ast_info.get_all_non_empty_ast_nodes(
        considered_func_idxs=considered_func_idxs
    )
    node_scorer = NodeScoreCalculator(ScoreStrategy.SIZE)
    considered_nodes = [(-node_scorer.calculate_score(node), id(node), node) for node in nodes]
    return considered_nodes


def get_node_score_consider_distribution_in_batch_for_heap(
    ast_state: ASTState,
    all_inst_num: int,
    considered_func_idxs: Optional[set[int]] = None,
) -> list[tuple[float, int, ASTINode]]:
    nodes = ast_state.ast_info.get_all_non_empty_ast_nodes(
        considered_func_idxs=considered_func_idxs
    )
    considered_nodes = [
        (-get_node_score_consider_distribution(node, all_inst_num), id(node), node)
        for node in nodes
    ]
    return considered_nodes


class NodeScoreCalculator:
    def __init__(self, strategy: ScoreStrategy):
        self.strategy = strategy

    def calculate_score(self, node: ASTINode, all_inst_num: Optional[int] = None) -> float:
        if self.strategy == ScoreStrategy.SIZE:
            return get_node_size_score(node)
        if self.strategy == ScoreStrategy.DISTRIBUTION:
            assert all_inst_num is not None, (
                "all_inst_num must be provided for DISTRIBUTION strategy"
            )
            return get_node_score_consider_distribution(node, all_inst_num)
        raise ValueError(f"Unknown strategy: {self.strategy}")

    def calculate_score_for_batch(
        self,
        ast_state: ASTState,
        considered_func_idxs: Optional[set[int]] = None,
        all_inst_num: Optional[int] = None,
    ) -> list[tuple[float, int, ASTINode]]:
        if self.strategy == ScoreStrategy.SIZE:
            return get_node_naive_score_in_batch_for_heap(ast_state, considered_func_idxs)
        if self.strategy == ScoreStrategy.DISTRIBUTION:
            assert all_inst_num is not None, (
                "all_inst_num must be provided for DISTRIBUTION strategy"
            )
            return get_node_score_consider_distribution_in_batch_for_heap(
                ast_state, all_inst_num, considered_func_idxs
            )
        raise ValueError(f"Unknown strategy: {self.strategy}")


class ASTNodePool:
    def __init__(
        self,
        heap_items: Optional[list[tuple[float, int, ASTINode]]] = None,
        *,
        node_scorer: NodeScoreCalculator,
        max_func_num:int,
        all_inst_num: Optional[int] = None,
        ast_state: ASTState,
        prioritize_node_lists: bool = False,
        reprocess_parents: bool = False,
    ) -> None:
        self._heap: list[tuple[float, int, ASTINode]] = list(heap_items or [])
        self._in_heap_node_ids: set[int] = {item[1] for item in self._heap}
        self._node_scorer = node_scorer
        self._all_inst_num = all_inst_num
        self.ast_state = ast_state
        self.max_func_num = max_func_num
        self._prioritize_node_lists = prioritize_node_lists
        self._reprocess_parents = reprocess_parents
        heapq.heapify(self._heap)
        self._p3_heap: list[tuple[int, int, ASTINode]] = []
        self._p3_processed: set[int] = set()
        self._current_source: Optional[str] = 'main_heap'

    @classmethod
    def from_ast_state(
        cls,
        *,
        ast_state: ASTState,
        considered_func_idxs: Optional[set[int]] = None,
        strategy: ScoreStrategy,
        all_inst_num: Optional[int] = None,
        prioritize_node_lists: bool = False,
        reprocess_parents: bool = False,
    ) -> "ASTNodePool":
        nodes = ast_state.ast_info.get_all_non_empty_ast_nodes(
            considered_func_idxs=considered_func_idxs
        )
        node_scorer = NodeScoreCalculator(strategy)
        if strategy != ScoreStrategy.SIZE:
            raise NotImplementedError('ASTNodePool.from_ast_state currently only supports ScoreStrategy.SIZE')

        node_lengths: dict[ASTINode, int] = {}
        for tree in ast_state.ast_info.get_all_root_trees():
            if considered_func_idxs is not None and tree.loc.func_idx not in considered_func_idxs:
                continue
            node_lengths.update(get_tree_node_lengths(tree, strategy=strategy))

        effective_max_length = all_inst_num if prioritize_node_lists else None
        items = [
            (-float(get_node_size_score(node, max_length=effective_max_length)), id(node), node)
            for node in nodes if not isinstance(node, InstsNode)
        ]
        max_func_num = len(considered_func_idxs) if considered_func_idxs is not None else len(ast_state.snapshot.parser.defined_funcs)
        return cls(
            items,
            node_scorer=node_scorer,
            all_inst_num=all_inst_num,
            ast_state=ast_state,
            max_func_num=max_func_num,
            prioritize_node_lists=prioritize_node_lists,
            reprocess_parents=reprocess_parents,
        )

    def __len__(self) -> int:
        return len(self._heap)

    def __bool__(self) -> bool:
        return bool(self._heap)

    def push(self, *, node: ASTINode) -> None:
        node_identity = id(node)
        if node_identity in self._in_heap_node_ids:
            return
        if self._node_scorer.strategy != ScoreStrategy.SIZE:
            raise NotImplementedError('ASTNodePool.push currently only supports ScoreStrategy.SIZE')
        effective_max_length = self._all_inst_num if self._prioritize_node_lists else None
        score = get_node_size_score(node, max_length=effective_max_length)
        heapq.heappush(self._heap, (-float(score), node_identity, node))
        self._in_heap_node_ids.add(node_identity)

    def pop_with_priority(self) -> tuple[float, ASTINode]:
        priority, _, node = heapq.heappop(self._heap)
        self._in_heap_node_ids.discard(id(node))
        return priority, node

    def push_non_empty_subtree_nodes(self, *, new_nodes: Iterable[ASTINode], exclude_node_lists: bool = False) -> None:
        for _n in new_nodes:
            subtree_lengths = get_tree_node_lengths(_n, strategy=self._node_scorer.strategy)
            _sub_nodes = traverse_ast(_n, lambda x: x, collect_results=True)
            assert _sub_nodes is not None
            for sub_node in _sub_nodes:
                if subtree_lengths.get(sub_node, 0) <= 0:
                    continue
                if isinstance(sub_node, InstsNode):
                    continue
                if exclude_node_lists and isinstance(sub_node, NodeList):
                    continue
                if isinstance(sub_node, (BlockNode, IfNode, LoopNode)):
                    assert sub_node.get_parent()
                self.push(node=sub_node)


    def select_top_k(self, k: int) -> list[ASTINode]:
        return [item[2] for item in heapq.nsmallest(k, self._heap)]

    def _passes_threshold_to_save(self, node: ASTINode, *, max_score: Optional[float]) -> bool:
        if max_score is not None and get_node_size_score(node, max_length=self._all_inst_num) >= max_score:
            return False
        return True

    def _pop_with_priority_with_drop(self, max_score: Optional[float] = None) -> Optional[tuple[float, ASTINode]]:
        while self:
            priority, _, node = heapq.heappop(self._heap)
            self._in_heap_node_ids.discard(id(node))
            if isinstance(node, InstsNode):
                continue
            if not self._passes_threshold_to_save(node, max_score=max_score):
                continue
            if not self._is_to_reduce_node(node, ast_info=self.ast_state.ast_info):
                continue
            return priority, node
        return None

    def practical_select(
        self,
        *,
        max_: int = 5,
        max_score: Optional[float] = None,
    ) -> Optional[ToReduceTask]:
        result = self._select_from_main_heap(max_, max_score)
        if result is not None:
            self._current_source = 'main_heap'
            return result

        if self._prioritize_node_lists or self._reprocess_parents:
            result = self._select_from_p3_heap()
            if result is not None:
                self._current_source = 'p3'
                return result

        self._current_source = None
        return None

    def _select_from_main_heap(
        self,
        max_: int,
        max_score: Optional[float],
    ) -> Optional[ToReduceTask]:
        if not self:
            return None
        max_ = min(max_, self.max_func_num)
        result = self._pop_with_priority_with_drop(max_score=max_score)
        if result is None:
            return None
        _, first_node = result
        if _is_cf_node(first_node):
            return OneCFNodeReduceTask(node=first_node)
        nodes: list[NodeList] = [first_node]  # type: ignore
        to_push = []
        covered_func_idxs = {first_node.func_idx}
        # 
        node_inst_num = first_node.get_length()

        while self and len(nodes) < max_ and node_inst_num < 100000:
            result = self._pop_with_priority_with_drop(max_score=max_score)
            if result is None:
                break
            _, node = result

            if not isinstance(node, NodeList):
                to_push.append(node)
                break
            elif node.func_idx in covered_func_idxs:
                to_push.append(node)
            else:
                nodes.append(node)
                covered_func_idxs.add(node.func_idx)
                node_inst_num += node.get_length()
        for node in to_push:
            self.push(node=node)
        return NodeListsReduceTask(nodes=nodes)


    def _is_to_reduce_node(self, node: ASTINode, ast_info: ASTInfo):
        if node.get_length() == 0:
            return False
        if ast_info.node_is_removed(node=node):
            return False
        return True

    # ===== P3: parent re-processing =====

    def _select_from_p3_heap(self) -> Optional[ToReduceTask]:
        while self._p3_heap:
            _, _, node = heapq.heappop(self._p3_heap)

            if id(node) in self._p3_processed:
                continue
            self._p3_processed.add(id(node))

            if self.ast_state.ast_info.node_is_removed(node):
                continue

            if isinstance(node, NodeList):
                return NodeListsReduceTask(nodes=[node])
            elif isinstance(node, (BlockNode, LoopNode, IfNode)):
                return OneCFNodeReduceTask(node=node)
            else:
                continue

        return None

    def _push_to_p3_heap(self, node: ASTINode) -> None:
        score = node.get_length()
        if score > 0:
            heapq.heappush(self._p3_heap, (score, id(node), node))

    def _push_new_nodes_main_heap(self, task: ToReduceTask, new_nodes: list[ASTINode]) -> None:
        if not new_nodes:
            return
        if not self._prioritize_node_lists:
            self.push_non_empty_subtree_nodes(new_nodes=new_nodes)
        elif isinstance(task, OneCFNodeReduceTask):
            self.push_non_empty_subtree_nodes(new_nodes=new_nodes, exclude_node_lists=True)
        else:
            self.push_non_empty_subtree_nodes(new_nodes=new_nodes)

    def handle_task_success(self, task: ToReduceTask, new_nodes: list[ASTINode]) -> None:
        if self._current_source == 'p3':
            self._handle_p3_success(task, new_nodes)
            return
        self._push_new_nodes_main_heap(task, new_nodes)
        if self._reprocess_parents:
            if self._prioritize_node_lists and isinstance(task, OneCFNodeReduceTask):
                if isinstance(task, OneCFNodeReduceTask):
                    self._track_parent_to_p3(new_nodes)
            else:
                for parent in self._find_parents_after_reduce(task, new_nodes):
                    if isinstance(parent, RootNode):
                        continue
                    if not self.ast_state.ast_info.node_is_removed(parent):
                        self._push_to_p3_heap(parent)

    def _handle_p3_success(self, task: ToReduceTask, new_nodes: list[ASTINode]) -> None:
        for parent in self._find_parents_after_reduce(task, new_nodes):
            if not self.ast_state.ast_info.node_is_removed(parent):
                self._push_to_p3_heap(parent)

    def _find_parents_after_reduce(
        self, task: ToReduceTask, new_nodes: list[ASTINode]
    ) -> list[ASTINode]:
        parents: list[ASTINode] = []
        seen_ids: set[int] = set()
        if isinstance(task, NodeListsReduceTask):
            for node in task.nodes:
                if node.has_parent():
                    parent = node.get_parent()
                    if id(parent) not in seen_ids:
                        seen_ids.add(id(parent))
                        parents.append(parent)
        else:
            for n in new_nodes:
                if n.has_parent():
                    parent = n.get_parent()
                    if id(parent) not in seen_ids:
                        seen_ids.add(id(parent))
                        parents.append(parent)
        return parents

    def _track_parent_to_p3(self, new_nodes: list[ASTINode]) -> None:
        for n in new_nodes:
            if n.has_parent():
                parent_node = n.get_parent()
                if isinstance(parent_node, RootNode):
                    continue
                # and isinstance(n.get_parent(), NodeList):
                self._push_to_p3_heap(n.get_parent())
                return