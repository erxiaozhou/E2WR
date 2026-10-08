
from typing import Optional, Sequence
from dataclasses import dataclass
from functools import cached_property
from extract_block_mutator.Context import Context
from reduction_analysis.ReduceUtil.ElemOperandMappingV2 import  EdgeType, GraphQuery, InstIdx, VopWT, build_graph_by_elems
from itertools import chain
from .ReduceInsts_V5_util import OneElem, get_seq_common_prefix_len, infer_mini_stack_change, sort_and_get_continuous_groups
from .find_eq_subg_util import find_subsequence_positions
MAX_EQ_LINK = 10

STACK_SNAPSHOT =  list[VopWT]

@dataclass(frozen=True)
class StackSnapshot:
    graph_helper: 'GraphHelper'
    elem_idx: int

    @cached_property
    def raw_ops(self) -> STACK_SNAPSHOT:
        return self.graph_helper.get_raw_stack_snapshot_before_elem(self.elem_idx)

    @cached_property
    def non_cf_ops(self) -> STACK_SNAPSHOT:
        raw = self.raw_ops
        return list(vop for vop in raw if not self.graph_helper.graph.op_is_taken_by_any(vop))


class SGComponentWOpInfo:
    def __init__(
        self,
        elem_idxs: list[int],
        taken_ops:list[VopWT],
        gen_ops:list[VopWT],
    ):
        # Keep the same public attributes as before.
        self.elem_idxs: list[int] = elem_idxs
        self.drop_types: list[str] = [ori_op.type_info for ori_op in taken_ops]
        self.comp_gen_types: list[str] = [new_op.type_info for new_op in gen_ops]
        self.taken_op_list = taken_ops
        self.gen_op_list = gen_ops
        self.sgc_taken_ops = set(taken_ops)
        self.sgc_gen_ops = set(gen_ops)

    def __str__(self) -> str:
        return f'C({self.elem_idxs}, drop:{self.drop_types}, gen:{self.comp_gen_types})'

    def __repr__(self) -> str:
        return self.__str__()


class SubGraph:
    def __init__(
        self, 
        graph_helper: 'GraphHelper',
        components:list[SGComponentWOpInfo],
        idx:int,
        raw_init_snapshot: StackSnapshot,
        raw_end_snapshot: StackSnapshot,
        enable_internal_cancel: bool = True,
        ):
        self.graph_helper: GraphHelper = graph_helper
        self.components = components
        self.idx = idx
        self.enable_internal_cancel = enable_internal_cancel

        self.raw_taken_ops: set[VopWT] = set()
        self.raw_gen_ops: set[VopWT] = set()
        for comp in components:
            self.raw_taken_ops.update(comp.sgc_taken_ops)
            self.raw_gen_ops.update(comp.sgc_gen_ops)
        # 
        # Derivable from raw_end_snapshot; keep the same attribute name as before.
        stack_snapshot_after_last_elem_non_cf: STACK_SNAPSHOT = raw_end_snapshot.non_cf_ops
        self.ops_from_outside = self.raw_taken_ops - self.raw_gen_ops
        ops_to_outside: set[VopWT] = self.raw_gen_ops - self.raw_taken_ops
        self.ops_to_outside_non_cf = ops_to_outside.intersection(set(stack_snapshot_after_last_elem_non_cf))
        # 
        self.sg_elem_idxs: list[int] = []
        for comp in components:
            self.sg_elem_idxs.extend(comp.elem_idxs)
        self.sg_elem_idxs = sorted(self.sg_elem_idxs)
        self.has_one_elem = len(self.sg_elem_idxs) == 1
        # 
        init_ops = list(raw_init_snapshot.raw_ops)
        end_ops = list(raw_end_snapshot.raw_ops)
        common_prefix = get_seq_common_prefix_len(init_ops, end_ops)

        self.raw_external_input_ops = [
            op
            for op in init_ops[common_prefix:]
            if op in self.ops_from_outside
        ]
        self.final_output_ops = [
            op
            for op in end_ops[common_prefix:]
            if op in self.ops_to_outside_non_cf
        ]
        

    def __str__(self):
        return f'SG(idx:{self.idx}, comps:{self.components})'
    def __repr__(self) -> str:
        return self.__str__()


class GraphHelper:
    def __init__(self, 
                 elems:list[OneElem], 
                 context:Context,
                 init_stack:list[str],
                 end_stack:list[str]
                 ):
        self.elems = elems
        graph = build_graph_by_elems(
            elems=elems,
            context=context,
            start_idx=0,
            init_stack=init_stack,
            end_stack=end_stack,
        )
        self.graph = graph
        qurier = GraphQuery(graph)
        self.qurier = qurier
        self.all_elem_num = len(elems)
        # * build groups and subgraphs
        # 
        dd_subgraphs = qurier.find_subgraph_ncf_inst_sequences()
        # Stack snapshots are memoized on demand to avoid eager O(N) copies.
        # Keys are stack-boundary indices: 0..all_elem_num.
        self._idx2stack_snapshots: dict[int, STACK_SNAPSHOT] = {}
        # Cache boundary view objects so their cached_property results persist.
        self._boundary_idx2snapshot: dict[int, StackSnapshot] = {}

        self.sg_in_contigous_idxs:list[list[list[int]]] = []
        # self
        
        for each_subgraph in dd_subgraphs:
            continuous_elem_groups: list[list[int]] = sort_and_get_continuous_groups(each_subgraph)
            self.sg_in_contigous_idxs.append(continuous_elem_groups)

        # 
        # Collect all elements that are covered by subgraphs
        covered_elem_indices = set()
        for subgraph in dd_subgraphs:
            covered_elem_indices.update(subgraph)
        
        # Add uncovered elements as individual groups
        for idx, elem in enumerate(elems):
            if idx not in covered_elem_indices:
                if elem.is_cf_related_inst():
                    pass
                    # single_group = ImmGroup([elem], [idx])
                else:
                    self.sg_in_contigous_idxs.append([[idx]])

    def get_raw_stack_snapshot_before_elem(self, elem_idx:int)->STACK_SNAPSHOT:
        if elem_idx not in self._idx2stack_snapshots:
            if elem_idx == self.all_elem_num:
                self._idx2stack_snapshots[elem_idx] = list(self.graph.end_stack_ops)
            else:
                inst_ = self.graph.numidx2_instidx[elem_idx]
                self._idx2stack_snapshots[elem_idx] = list(self.graph.inst2stack_before_it[inst_])
        return self._idx2stack_snapshots[elem_idx]

    def get_raw_stack_snapshot_after_elem(self, elem_idx:int)->STACK_SNAPSHOT:
        return self.get_raw_stack_snapshot_before_elem(elem_idx + 1)

    def get_stack_snapshot(self, elem_idx: int) -> StackSnapshot:
        if elem_idx not in self._boundary_idx2snapshot:
            self._boundary_idx2snapshot[elem_idx] = StackSnapshot(
                graph_helper=self,
                elem_idx=elem_idx,
            )
        return self._boundary_idx2snapshot[elem_idx]

    def get_stack_type_after_elem(self, elem_idx:int)->StackSnapshot:
        return self.get_stack_snapshot(elem_idx + 1)


    def count_dependency_on_cf(self, elem_idx:int) ->tuple[int, list[str]]:
        return self.qurier.count_dependency_on_cf(elem_idx)

    def get_ops_taken_by_elem(self, elem_idx:int)->list[VopWT]:
        inst_ = self.graph.numidx2_instidx[elem_idx]
        return self.graph.inst_idx_to_consumed_ops.get(inst_, [])

    def get_ops_generated_by_elem(self, elem_idx:int)->list[VopWT]:
        inst_ = self.graph.numidx2_instidx[elem_idx]
        return self.graph.inst_idx_to_produced_ops.get(inst_, [])
    def get_stack_ops_before_elem(self, elem_idx:int)->list[VopWT]:
        return self.get_raw_stack_snapshot_before_elem(elem_idx)

    def get_stack_ops_after_elem(self, elem_idx:int)->list[VopWT]:
        return self.get_stack_ops_before_elem(elem_idx + 1)



class SubGraphRepo:
    def __init__(self):
        self._sg_idx2sg: dict[int, SubGraph] = {}

    def insert_sg(self, sg:SubGraph):
        self._sg_idx2sg[sg.idx] = sg

    def get_sg_by_idx(self, sg_idx:int)->SubGraph:
        return self._sg_idx2sg[sg_idx]

    def items(self):
        return self._sg_idx2sg.items()


class SGBuilder:
    _count = 0

    def __init__(
        self,
        graph_helper: GraphHelper,
    ) -> None:
        self.graph_helper = graph_helper

    def gen_sg_core(
        self,
        components: list[SGComponentWOpInfo],
        enable_internal_cancel: bool = True,
    ) -> SubGraph:
        comp_idxs = set()
        for c in components:
            comp_idxs.update(c.elem_idxs)
        first_elem_idx = min(comp_idxs)
        last_elem_idx = max(comp_idxs)

        end_snapshot = self.graph_helper.get_stack_snapshot(last_elem_idx + 1)
        init_snapshot = self.graph_helper.get_stack_snapshot(first_elem_idx)

        sg = SubGraph(
            self.graph_helper,
            components,
            SGBuilder._count,
            raw_init_snapshot=init_snapshot,
            raw_end_snapshot=end_snapshot,
            enable_internal_cancel=enable_internal_cancel,
        )
        SGBuilder._count += 1
        return sg

    def gen_sg_component(
        self,
        elem_idxs: list[int],
        operand_remap: Optional[dict[VopWT, VopWT]] = None,
    ) -> SGComponentWOpInfo:
        assert len(elem_idxs) > 0
        ori_stack = self.graph_helper.get_stack_snapshot(min(elem_idxs)).non_cf_ops
        cur_stack = self.graph_helper.get_stack_type_after_elem(max(elem_idxs)).non_cf_ops
        # to_drop_types = 
        offset = 0
        for ori_op, new_op in zip(ori_stack, cur_stack):
            if ori_op != new_op:
                break
            offset += 1
        #
        taken_ops = ori_stack[offset:]
        gen_ops = cur_stack[offset:]
        if operand_remap:
            taken_ops = [operand_remap.get(o, o) for o in taken_ops]
            gen_ops = [operand_remap.get(o, o) for o in gen_ops]
        #
        replacement_info = SGComponentWOpInfo(
            elem_idxs=list(elem_idxs),
            taken_ops=taken_ops,
            gen_ops=gen_ops,
        )
        return replacement_info

def init_subgraph_repo(
    init_graph_helper:GraphHelper,
    sg_builder: SGBuilder,
    )->SubGraphRepo:
    result_repo = SubGraphRepo()
    for continuous_elem_idxs in init_graph_helper.sg_in_contigous_idxs:
        cur_sg_comps = []
        for one_continuous_elem_idxs in continuous_elem_idxs:
            cur_replacement = sg_builder.gen_sg_component(
                one_continuous_elem_idxs,
            )
            cur_sg_comps.append(cur_replacement)
        sg = sg_builder.gen_sg_core(cur_sg_comps)
        result_repo.insert_sg(sg)
    return result_repo

def get_init_subgraph_repo(graph_helper: GraphHelper) -> SubGraphRepo:
    sg_builder = SGBuilder(
            graph_helper=graph_helper,
        )
    return init_subgraph_repo(graph_helper, sg_builder)


def _gen_subgraph_from_elem_idxs(
    sg_builder: SGBuilder,
    elem_idxs: Sequence[int],
    operand_remap: Optional[dict[VopWT, VopWT]] = None,
    enable_internal_cancel: bool = True,
) -> SubGraph:
    assert elem_idxs
    components: list[SGComponentWOpInfo] = []
    for one_seq in sort_and_get_continuous_groups(set(elem_idxs)):
        components.append(sg_builder.gen_sg_component(one_seq, operand_remap=operand_remap))
    return sg_builder.gen_sg_core(
        components,
        enable_internal_cancel=enable_internal_cancel,
    )


def _build_remap_from_unify_pairs(
    unify_pairs: list[tuple[VopWT, VopWT]],
) -> Optional[dict[VopWT, VopWT]]:
    if not unify_pairs:
        return None
    from .ElemOperandMappingV2 import OpWithTypeFactory
    factory = OpWithTypeFactory()
    remap: dict[VopWT, VopWT] = {}
    for taken_op, matched_op in unify_pairs:
        shared = factory.gen_one_vopwt(0, taken_op.type_info)
        remap[taken_op] = shared
        remap[matched_op] = shared
    return remap


class SubGraphSplitter:
    def __init__(self, 
                 graph_helper:GraphHelper,
                 ) -> None:
        self.graph_helper = graph_helper
      
        self.sg_builder = SGBuilder(
            graph_helper=graph_helper,
        )

    def _split_sg_by_last_inst_and_local_dd(
        self,
        idxs_in_sg: Sequence[int],
    ) -> list[SubGraph]:
        assert idxs_in_sg

        sorted_idxs_in_sg = sorted(idxs_in_sg)
        if len(sorted_idxs_in_sg) == 1:
            return [_gen_subgraph_from_elem_idxs(self.sg_builder, sorted_idxs_in_sg)]

        last_elem_idx = sorted_idxs_in_sg[-1]
        prefix_elem_idxs = sorted_idxs_in_sg[:-1]
        prefix_allowed_idxs = set(prefix_elem_idxs)

        graph = self.graph_helper.graph
        adjacency: dict[int, set[int]] = {idx: set() for idx in prefix_allowed_idxs}

        for elem_idx in prefix_elem_idxs:
            inst = graph.numidx2_instidx[elem_idx]
            for op in graph.inst_idx_to_consumed_ops.get(inst, []):
                producer = graph.operand_to_producer.get(op)
                if producer is not None and producer.idx in prefix_allowed_idxs:
                    adjacency[elem_idx].add(producer.idx)
                    adjacency[producer.idx].add(elem_idx)
            for op in graph.inst_idx_to_produced_ops.get(inst, []):
                consumer = graph.operand_to_consumer.get(op)
                if consumer is not None and consumer.idx in prefix_allowed_idxs:
                    adjacency[elem_idx].add(consumer.idx)
                    adjacency[consumer.idx].add(elem_idx)
        
        prefix_subgraphs: list[SubGraph] = []
        visited: set[int] = set()
        covered_prefix_idxs: set[int] = set()
        for start_idx in prefix_elem_idxs:
            if start_idx in visited:
                continue

            stack = [start_idx]
            component: set[int] = set()
            while stack:
                cur_idx = stack.pop()
                if cur_idx in visited:
                    continue
                visited.add(cur_idx)
                component.add(cur_idx)
                stack.extend(sorted(adjacency[cur_idx] - visited, reverse=True))

            if len(component) > 1:
                prefix_subgraphs.append(
                    _gen_subgraph_from_elem_idxs(self.sg_builder, list(component))
                )
                covered_prefix_idxs.update(component)

        uncovered_idxs = prefix_allowed_idxs - covered_prefix_idxs
        for elem_idx in sorted(uncovered_idxs):
            prefix_subgraphs.append(
                _gen_subgraph_from_elem_idxs(self.sg_builder, [elem_idx])
            )

        prefix_subgraphs.append(_gen_subgraph_from_elem_idxs(self.sg_builder, [last_elem_idx]))
        return prefix_subgraphs


    def replace_a_graph(
        self, 
        raw_sg_idx:int,
        subgraph_repo: SubGraphRepo,
    ) -> list[SubGraph]:
        raw_sg = subgraph_repo.get_sg_by_idx(raw_sg_idx)
        new_sgs = self.gen_new_sgs_ori(raw_sg)
        for new_sg in new_sgs:
            subgraph_repo.insert_sg(new_sg)
        return new_sgs

    def gen_new_sgs_ori(self, 
                    raw_sg:SubGraph,
                    ):
        idxs_in_sg = sorted(raw_sg.sg_elem_idxs)

        if len(idxs_in_sg) == 1:
            raw_elem_idx = idxs_in_sg[0]
            component = self.sg_builder.gen_sg_component(
                elem_idxs=[raw_elem_idx],
            )
            raw_sg = self.sg_builder.gen_sg_core([component])
            return [raw_sg]
        return self._split_sg_by_last_inst_and_local_dd(raw_sg.sg_elem_idxs)


class EqSubGraphManager:
    def __init__(self, graph_helper:GraphHelper):
        self.graph = graph_helper.graph
        self.graph_helper = graph_helper
        self.sg_builder = SGBuilder(
            graph_helper=graph_helper,
        )
        self.subgraph_repo_eq = SubGraphRepo()
        all_elem_idxs_taking_something = self._get_all_elem_idx_taking_something()
        for taken_op_elem_idx in all_elem_idxs_taking_something:
            elem = self.graph_helper.graph.numidx2_instidx[taken_op_elem_idx]
            if elem.is_not_sure_type_inst():
                continue
            self._init_all_eq_graph_idxs(taken_op_elem_idx)

    # def bui
    def _get_all_elem_idx_taking_something(self)->set[int]:
        result = set()
        for elem_idx in range(self.graph_helper.all_elem_num):
            taken_ops = self.graph_helper.get_ops_taken_by_elem(elem_idx)
            if len(taken_ops) >0:
                result.add(elem_idx)
        return result

    def _build_forward_eq_remap(
        self,
        idxs,
        borrowed_ops: list[VopWT],
    ) -> Optional[dict[VopWT, VopWT]]:
        tmp_sg = _gen_subgraph_from_elem_idxs(self.sg_builder, idxs)
        outflow = tmp_sg.raw_gen_ops - tmp_sg.raw_taken_ops
        inflow = tmp_sg.raw_taken_ops - tmp_sg.raw_gen_ops
        from .ElemOperandMappingV2 import OpWithTypeFactory
        factory = OpWithTypeFactory()
        remap: dict[VopWT, VopWT] = {}
        used_inflow: set[VopWT] = set()
        for bop in borrowed_ops:
            if bop not in outflow:
                continue
            for iop in inflow:
                if iop in used_inflow:
                    continue
                if iop.type_info == bop.type_info:
                    shared = factory.gen_one_vopwt(0, bop.type_info)
                    remap[bop] = shared
                    remap[iop] = shared
                    used_inflow.add(iop)
                    break
        return remap if remap else None

    def _init_all_eq_graph_idxs(self, taken_op_elem_idx:int):
        forward_eq_graphs = self._get_forward_eq_graphs(taken_op_elem_idx)
        backward_eq_graphs = self._get_backward_eq_taken_ops_on_future_stack_elem_idxs_naive(taken_op_elem_idx)
        result:list[list[int]] = []
        for idxs, borrowed_ops in forward_eq_graphs:
            result.append(sorted(list(idxs)))
            operand_remap = self._build_forward_eq_remap(idxs, borrowed_ops)
            sg = _gen_subgraph_from_elem_idxs(
                self.sg_builder, idxs,
                operand_remap=operand_remap,
                enable_internal_cancel=False,
            )
            self.subgraph_repo_eq.insert_sg(sg)
        for idxs, unify_pairs in backward_eq_graphs:
            result.append(sorted(list(idxs)))
            operand_remap = _build_remap_from_unify_pairs(unify_pairs)
            sg = _gen_subgraph_from_elem_idxs(
                self.sg_builder,
                idxs,
                operand_remap=operand_remap,
                enable_internal_cancel=False,
            )
            self.subgraph_repo_eq.insert_sg(sg)
        # return result

    def _get_forward_eq_graphs(self, taken_op_elem_idx:int)->list[tuple[set[int], list[VopWT]]]:
        # * step 1: get possible eq taken_ops
        taken_ops = self._get_eq_taken_ops(elem_idx=taken_op_elem_idx)
        result = []
        for ops in taken_ops:
            eq_graph_idxs = self._build_graph_with_insts_not_gen_other_ops_taken_by_concrete_ops(
                set(ops), taken_op_elem_idx
            )
            if eq_graph_idxs is not None:
                result.append((eq_graph_idxs, ops))
        _result = []
        for idxs, borrowed_ops in result:
            if min(idxs) + len(idxs) - 1 == taken_op_elem_idx:
                continue
            _result.append((idxs, borrowed_ops))
        return _result

    def _build_graph_with_insts_not_gen_other_ops_taken_by_concrete_ops(
        self,
        borrowed_ops:set[VopWT],
        specified_end_idx:int
    )->Optional[set[int]]:
        direct_related_insts = self._get_concrete_elems_gen_ops(borrowed_ops)
        if direct_related_insts is None:
            return None
        for inst in direct_related_insts:
            if not self._inst_not_gen_other_ops_taken_by_concrete_ops(
                inst,
                borrowed_ops
            ):
                return None
        result = set()
        result.add(specified_end_idx)
        for inst in direct_related_insts:
            result.add(inst.idx)
        return result
        

    def _inst_not_gen_other_ops_taken_by_concrete_ops(
        self,
        inst:InstIdx,
        borrowed_ops:set[VopWT]
    ):
        produced_ops = self.graph_helper.graph.inst_idx_to_produced_ops.get( inst, [] )
        rest_ops = [op for op in produced_ops if op not in borrowed_ops]
        for op in rest_ops:
            consumed_relation = self.graph.operand_to_consumer_relation[op]
            if consumed_relation == EdgeType.CONSUME:
                return False
        return True
    


    # * ==============================================



    def _get_backward_eq_taken_ops_on_future_stack_elem_idxs_naive(self, elem_idx:int)->list[tuple[list[int], list[tuple[VopWT, VopWT]]]]:
        # elem_idx is an elem taking something
        all_elem_num = self.graph_helper.all_elem_num
        if elem_idx + 1 >= all_elem_num:
            return []
        taken_ops = self.graph_helper.get_ops_taken_by_elem(elem_idx)
        taken_num = len(taken_ops)
        future_stacks = []
        stack_before_taken = self.graph_helper.get_stack_ops_before_elem(elem_idx)
        stack_after_taken = self.graph_helper.get_stack_ops_before_elem(elem_idx+1)
        if len(stack_before_taken) < len(stack_after_taken): 
            return []

        to_drop_types, to_gen_types = infer_mini_stack_change(
            [vop.type_info for vop in stack_before_taken],
            [vop.type_info for vop in stack_after_taken]
        )
        # droped_type 
        if len(to_gen_types):  
            return []
        if len(to_drop_types) == 0:  
            return []

        if len(set(to_drop_types)) != 1:
            return []
        drop_type = to_drop_types[0]
        
        # len(stack_after_taken)

        longest_no_consumed_len = self._longest_no_taken_seq(elem_idx+1)
        if longest_no_consumed_len == 0:
            return []
        reference_stack_end = elem_idx + 1 + longest_no_consumed_len
        if reference_stack_end >= all_elem_num:
            return []
        reference_stack = self.graph_helper.get_stack_ops_before_elem(reference_stack_end)
        # if stack_before_taken != reference_stack[:len(stack_before_taken)]:
        #     return []
        if len(reference_stack) < len(stack_before_taken):
            return []
        for idx in range(len(stack_before_taken)):
            vop_before = stack_before_taken[idx]
            vop_reference = reference_stack[idx]
            if vop_before.type_info != vop_reference.type_info:
                return []
        stack_after_taken = stack_before_taken[:len(stack_before_taken)-len(to_drop_types)]
        actual_reference_stack = reference_stack[len(stack_after_taken):]
        len_of_drop_num  = 0
        for idx, vop in enumerate(actual_reference_stack):
            if vop.type_info == drop_type:
                len_of_drop_num += 1
            else:
                break
        considered_stack = actual_reference_stack[:len_of_drop_num]
        considered_reference_stack_types = [vop.type_info for vop in considered_stack]
        # 
        
        can_taken_idxs =  find_subsequence_positions(
            to_drop_types,
            considered_reference_stack_types,
            max_results=MAX_EQ_LINK
        )
        result = []
        for taken_idxs in can_taken_idxs:
            matched_ops = [actual_reference_stack[i] for i in taken_idxs]
            cur_inst_idxs =[]
            for op in matched_ops:
                producer = self.graph.operand_to_producer[op]
                cur_inst_idxs.append(producer.idx)
            cur_inst_idxs.append(elem_idx)
            unify_pairs = list(zip(taken_ops, matched_ops))
            result.append((cur_inst_idxs, unify_pairs))
        # * p1 only one taken
        return result
        
        
    def _longest_no_taken_seq(self, start_elem_idx:int)->int:
        all_elem_num = self.graph_helper.all_elem_num
        if start_elem_idx + 1 >= all_elem_num:
            return 0

        cur_stack = self.graph_helper.get_stack_ops_before_elem(start_elem_idx)
        next_stack = self.graph_helper.get_stack_ops_before_elem(start_elem_idx+1)
        cur_len = 0
        while self._no_op_consumed(cur_stack, next_stack):
            cur_len += 1
            start_elem_idx += 1
            if start_elem_idx + 1 >= all_elem_num:
                break
            cur_stack = next_stack
            next_stack = self.graph_helper.get_stack_ops_before_elem(start_elem_idx+1)
        return cur_len

    def _no_op_consumed(self, ori_stack:list[VopWT], new_stack:list[VopWT])->bool:
        ori_stack_len = len(ori_stack)
        new_stack_len = len(new_stack)
        if new_stack_len < ori_stack_len:
            return False
        for ori_op, new_op in zip(ori_stack, new_stack):
            if ori_op != new_op:
                return False
        return True
    

    def _get_eq_taken_ops(self, elem_idx:int)->list[list[VopWT]]:
        taken_ops = self.graph_helper.get_ops_taken_by_elem(elem_idx)
        taken_ops = [op for op in taken_ops if not self.op_is_v_produce(op)]
        # gen_ops = self.graph_helper.get_ops_generated_by_elem(elem_idx)
        # taken_types = [op.type_info for op in taken_ops]
        # gen_types = [op.type_info for op in gen_ops]
        # to_skip_num = 0
        # for
        # common_type_len = get_seq_common_prefix_len
        # 
        stack_ops = self.graph_helper.get_stack_ops_before_elem(elem_idx)
        # 
        # rest_ops = stack_ops[:len(stack_ops)-len(taken_ops)]
        # rest_types = [vop.type_info for vop in rest_ops]
        rest_types = [vop.type_info for vop in self.graph_helper.get_stack_ops_after_elem(elem_idx)]
        # 
        # taken_op_types = [vop.type_info for vop in taken_ops]
        stack_op_types = [vop.type_info for vop in stack_ops]
        all_idxs = set(range(len(stack_ops)))
        can_taken_idxs = find_subsequence_positions(
            rest_types,
            stack_op_types,
            max_results=MAX_EQ_LINK
        )
        can_taken_idxs = [list(all_idxs - set(s)) for s in can_taken_idxs]

        taken_ops_set = set(taken_ops)
        result = []
        for taken_idxs in can_taken_idxs:
            matched_ops = [stack_ops[i] for i in taken_idxs]
            if taken_ops_set == set(matched_ops):
                continue
            # if set(matched_ops).issubset(taken_ops_set):
            #     continue
            result.append(matched_ops)
        return result
        # 
        # 
    def op_is_v_produce(self, op:VopWT)->bool:
        produce_relation = self.graph.operand_to_producer_relation[op]
        return produce_relation == EdgeType.CF_PRODUCE
    def _get_concrete_elems_gen_ops(self, target_ops:set[VopWT])->Optional[set[InstIdx]]:
        result: set[InstIdx] = set()
        # for consumer,
        for op in target_ops:
            produce_relation = self.graph.operand_to_producer_relation[op]
            if produce_relation == EdgeType.CF_PRODUCE:
                continue
            producer = self.graph.operand_to_producer.get(op, None)
            assert producer is not None
            # Reject ops produced outside the concrete inst stream (e.g., initial stack).
            # These producers are not valid elem idxs, so they cannot participate in an eq graph.
            if not producer.is_inside_concrete_inst():
                return None
            result.add(producer)
        return result
        

def get_eq_sgs(
    graph_helper:GraphHelper
    )->SubGraphRepo:
    eq_manager = EqSubGraphManager(graph_helper)
    return eq_manager.subgraph_repo_eq

    
def sg_is_cf_only(sg: SubGraph, elems: list[OneElem]) -> bool:
    sg_elem_idxs = sg.sg_elem_idxs
    if len(sg_elem_idxs) != 1:
        return False
    elem = elems[sg_elem_idxs[0]]
    return elem.is_cf_related_inst()