
from enum import Enum
from typing import Any, Optional

from extract_block_mutator.Context import Context
from extract_block_mutator.InstUtil.InstReqUtil import get_inst_ty_req
from reduction_analysis.ReducerCommonConfig import is_debug_env

from .ReduceInsts_V5_util import ElemGroupBase, ImmGroup, MutElemGroup, OneElem, OneNodeType, StackChange, gen_type_for_graph




class VopWT:
    def __init__(self, idx: int, depth_ref: int, type_info: str):
        # depth_ref is not the correct depth, by the ops with the same depth_ref have the same actual depth
        self.idx = idx
        self.depth_ref = depth_ref  
        self.type_info = type_info

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}({self.idx}, {self.depth_ref}, {self.type_info})"
    def __str__(self) -> str:
        return self.__repr__()

    # def __eq__(self, other: Any) -> bool:
    #     if not isinstance(other, VopWT):
    #         return False
    #     return self.idx == other.idx and self.type_info == other.type_info and self.depth_ref == other.depth_ref

    # def __hash__(self) -> int:
    #     return hash((self.idx, self.type_info, self.depth_ref))



class OpWithTypeFactory:
    _instance = None
    cur_idx = 0

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super(OpWithTypeFactory, cls).__new__(cls)
        return cls._instance
    def gen_one_vopwt(self, depth_ref: int, type_info: str) -> VopWT:
        result = VopWT(OpWithTypeFactory.cur_idx, depth_ref, type_info)
        OpWithTypeFactory.cur_idx += 1
        return result


    
class INST_TYPE(Enum):
    VINST = 1
    CINST = 2


class OneElemTypeInfoKind(Enum):
    COMMON = 1
    NCND = 2
    UNREACHABLE = 3
    RETURN_LIKE = 4
    BR_IF = 5
    OUTSIDE = 6

class InstIdx:
    def __init__(self, idx: int, inst_type: INST_TYPE, inst_kind: Optional[OneElemTypeInfoKind] = None):
        self.idx = idx
        self.inst_type = inst_type
        self.inst_kind = inst_kind

    def __str__(self):
        return f"{self.__class__.__name__}({self.idx})"

    def __eq__(self, other):
        return self.idx == other.idx and self.inst_type == other.inst_type and self.inst_kind == other.inst_kind

    def __hash__(self):
        return hash((self.idx, self.inst_type))

    def is_inside_concrete_inst(self) -> bool:
        return self.inst_type == INST_TYPE.CINST and self.inst_kind != OneElemTypeInfoKind.OUTSIDE

    def is_not_sure_type_inst(self) -> bool:
        return self.inst_kind in {OneElemTypeInfoKind.BR_IF, OneElemTypeInfoKind.NCND, OneElemTypeInfoKind.UNREACHABLE, OneElemTypeInfoKind.RETURN_LIKE}

class VInstIdx(InstIdx):
    def __init__(self, idx: int, inst_kind: Optional[OneElemTypeInfoKind] = None):
        super().__init__(idx, INST_TYPE.VINST, inst_kind)

    def __str__(self):
        return f"{self.__class__.__name__}({self.idx})"


class CInstIdx(InstIdx):
    def __init__(self, idx: int, inst_kind: Optional[OneElemTypeInfoKind] = None):
        super().__init__(idx, INST_TYPE.CINST, inst_kind)

    def __str__(self):
        return f"{self.__class__.__name__}({self.idx})"

    def __repr__(self):
        return self.__str__()

class ExistingUnknownInstIdx(InstIdx):
    _idx_counter = 0
    def __init__(self):
        idx = ExistingUnknownInstIdx._idx_counter
        ExistingUnknownInstIdx._idx_counter += 1
        super().__init__(idx, INST_TYPE.CINST, OneElemTypeInfoKind.OUTSIDE)
    

class VInstIdxFactory:
    _instance = None
    cur_idx = 0

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super(VInstIdxFactory, cls).__new__(cls)
        return cls._instance

    def gen_one_vinst_idx(self):
        result = VInstIdx(VInstIdxFactory.cur_idx)
        VInstIdxFactory.cur_idx += 1
        return result


class EdgeType(Enum):
    PRODUCE = 'P'
    CONSUME = 'C'
    CF_PRODUCE = 'VP'
    CF_CONSUME = 'VC'

    def __str__(self) -> str:
        return self.value


class Edge:
    def __init__(self, operand: VopWT, inst_idx: InstIdx, edge_type: EdgeType):
        self.operand = operand
        self.inst_idx = inst_idx
        self.edge_type = edge_type

    def __str__(self):
        return f"{self.inst_idx} -{self.edge_type}-> {self.operand}"
    def __repr__(self):
        return self.__str__()


class OneElemTypeInfo:
    def __init__(self, kind: OneElemTypeInfoKind):
        self.kind = kind

class DeterminedElemTypeInfo(OneElemTypeInfo):
    def __init__(self, 
                 gen_types: list[str],
                 taken_ops_list:list[OneNodeType],
                 ):
        super().__init__(OneElemTypeInfoKind.COMMON)
        self.gen_types = gen_types
        self.taken_ops_list = taken_ops_list


class UnreachableElemTypeInfo(OneElemTypeInfo):
    def __init__(self):
        super().__init__(OneElemTypeInfoKind.UNREACHABLE)

class NonControlflowNonDetermined(OneElemTypeInfo):
    def __init__(self, taken_strs, gen_strs):
        super().__init__(OneElemTypeInfoKind.NCND)
        self.taken_strs = taken_strs
        self.gen_strs = gen_strs

class ReturnLikeElemTypeInfo(OneElemTypeInfo):
    def __init__(self, required_types: list[str]):
        super().__init__(OneElemTypeInfoKind.RETURN_LIKE)
        self.required_types = required_types

class BrIfElemTypeInfo(OneElemTypeInfo):
    def __init__(self, 
                 required_return_types: list[str]  # not include the i32 condition
                 ):
        super().__init__(OneElemTypeInfoKind.BR_IF)
        self.required_return_types = required_return_types


NODE_TYPE = VopWT

class OperandInstGraphV2:
    def __init__(self):
        self.edges: list[Edge] = []
        # self.cf_nodes:list[NODE_TYPE] = []
        self.operand_to_producer: dict[NODE_TYPE, InstIdx] = {}
        self.operand_to_consumer: dict[NODE_TYPE, InstIdx] = {}
        self.operand_to_producer_relation: dict[NODE_TYPE, EdgeType] = {}
        self.operand_to_consumer_relation: dict[NODE_TYPE, EdgeType] = {}
        self.operand_to_produce_edge: dict[NODE_TYPE, Edge] = {}
        self.operand_to_consume_edge: dict[NODE_TYPE, Edge] = {}
        self.inst_idx_to_produced_ops: dict[InstIdx, list[NODE_TYPE]] = {}
        self.inst_idx_to_consumed_ops: dict[InstIdx, list[NODE_TYPE]] = {}
        self.inst_idx_to_produced_edges: dict[InstIdx, list[Edge]] = {}
        self.inst_idx_to_consumed_edges: dict[InstIdx, list[Edge]] = {}
        self.numidx2_instidx: dict[int, InstIdx] = {}
        self.inst2stack_before_it: dict[InstIdx, list[NODE_TYPE]] = {}
        self.end_stack_ops: list[NODE_TYPE] = []
        # selfinst_idx2
        # 
        # self.ops = list(self.operand_to_producer.keys())
    def get_stack_type_before_elem(self, elem_idx:int) ->list[str]:
        inst_ = self.numidx2_instidx[elem_idx]
        ops = self.inst2stack_before_it[inst_]
        return [op.type_info for op in ops]

    def op_is_taken_by_any(self, op: NODE_TYPE) -> bool:
        consumed_relation = self.operand_to_consumer_relation.get(op, None)
        return consumed_relation == EdgeType.CF_CONSUME
        
    @property
    def ops(self) -> set[NODE_TYPE]:
        return set(self.operand_to_producer.keys())
    #     # return self.operand_to_produce.k

    # @property

    def _validate(self):
        all_operands = set([edge.operand for edge in self.edges])
        operand_num = len(all_operands)
        for op in all_operands:
            assert op in self.operand_to_producer , f"op {op} not in operand_to_producer , {self.operand_to_consume_edge.get(op)}"
            assert op in self.operand_to_consume_edge , f"op {op} not in operand_to_consume_edge, {self.operand_to_produce_edge.get(op)}"
        assert operand_num == len(self.operand_to_producer), f"operand_num {operand_num} != len(operand_to_producer) {len(self.operand_to_producer)}  {set(all_operands)}"
        assert operand_num == len(self.operand_to_producer_relation)
        assert operand_num == len(self.operand_to_produce_edge)
        assert operand_num == len(self.operand_to_consumer)
        assert operand_num == len(self.operand_to_consumer_relation)
        assert operand_num == len(self.operand_to_consume_edge)


    # def add

    def add_edge(self, operand: NODE_TYPE, inst_idx: InstIdx, edge_type: EdgeType):
        # if inst_idx.inst_kind != OneElemTypeInfoKind.OUTSIDE:
        #     self.numidx2_instidx[inst_idx.idx] = inst_idx
        edge = Edge(operand, inst_idx, edge_type)
        self.edges.append(edge)

        if edge_type == EdgeType.PRODUCE or edge_type == EdgeType.CF_PRODUCE:
            assert operand not in self.operand_to_producer, f"op {operand} already produced by inst {self.operand_to_producer[operand]}, cannot be produced by inst {inst_idx}"
            assert operand not in self.operand_to_produce_edge, f"op {operand} already has produce edge {self.operand_to_produce_edge[operand]}, cannot add another produce edge for inst {inst_idx}"
            # 
            self.operand_to_producer[operand] = inst_idx
            self.operand_to_producer_relation[operand] = edge_type
            self.operand_to_produce_edge[operand] = edge
            # 
            self.inst_idx_to_produced_ops.setdefault(inst_idx, []).append(operand)
            self.inst_idx_to_produced_edges.setdefault(inst_idx, []).append(edge)
        else:
            assert operand not in self.operand_to_consumer, f"op {operand} already consumed by inst {self.operand_to_consumer[operand]}, cannot be consumed by inst {inst_idx}"
            assert operand not in self.operand_to_consume_edge, f"op {operand} already has consume edge {self.operand_to_consume_edge[operand]}, cannot add another consume edge for inst {inst_idx}"
            # 
            self.operand_to_consumer[operand] = inst_idx
            self.operand_to_consumer_relation[operand] = edge_type
            self.operand_to_consume_edge[operand] = edge
            # 
            self.inst_idx_to_consumed_ops.setdefault(inst_idx, []).append(operand)
            self.inst_idx_to_consumed_edges.setdefault(inst_idx, []).append(edge)

    def add_edge_produced_by_outside_graph(self, operand: NODE_TYPE):
        assert operand not in self.operand_to_producer
        gen_inst = ExistingUnknownInstIdx()
        self.add_edge(operand, gen_inst, EdgeType.PRODUCE)

    def add_edge_concrete_produce(self, operand: NODE_TYPE, inst_idx: InstIdx):
        self.add_edge(operand, inst_idx, EdgeType.PRODUCE)

    def add_edge_concrete_consume(self, operand: NODE_TYPE, inst_idx: CInstIdx):
        self.add_edge(operand, inst_idx, EdgeType.CONSUME)

    def add_edge_comsumed_by_outside_graph(self, operand: NODE_TYPE):
        assert operand not in self.operand_to_consumer
        # if operand not in self.operand_to_consumers:
        gen_inst = ExistingUnknownInstIdx()
        self.add_edge(operand, gen_inst, EdgeType.CONSUME)

    def add_edge_for_control_flow_consume(
        self,
        operand: NODE_TYPE,
        inst_idx: CInstIdx
    ):
        self.add_edge(operand, inst_idx, EdgeType.CF_CONSUME)

    def add_edge_for_control_flow_produce(
        self,
        operand: NODE_TYPE,
        inst_idx: InstIdx
    ):
        self.add_edge(operand, inst_idx, EdgeType.CF_PRODUCE)

class GraphQuery:
    def __init__(self, graph: OperandInstGraphV2):
        self.graph = graph
        pass

    def count_dependency_on_cf(self, inst_idx) ->tuple[int, list[str]]:
        # return  `operand to consume` and `operands to gen`
        if inst_idx not in self.graph.numidx2_instidx:
            return 0, []
        inst_sym = self.graph.numidx2_instidx[inst_idx]
        taken_ops = self.graph.inst_idx_to_consumed_ops.get(inst_sym, [])
        gen_ops = self.graph.inst_idx_to_produced_ops.get(inst_sym, [])
        taken_num = sum(1 for op in taken_ops if self.graph.operand_to_consume_edge[op].edge_type == EdgeType.CF_CONSUME)
        gen_strs = [op.type_info for op in gen_ops if self.graph.operand_to_produce_edge[op].edge_type == EdgeType.CF_PRODUCE]
        return taken_num, gen_strs
        
        # raise NotImplementedError()
    
    def count_vop_from_any(self) -> dict[int, int]:
        result: dict[int, int] = {}
        for inst_idx, consumed_ops in self.graph.inst_idx_to_consumed_ops.items():
            cnt = sum(1 for op in consumed_ops if self.graph.operand_to_produce_edge[op].edge_type == EdgeType.CF_PRODUCE)
            if cnt:
                result[inst_idx.idx] = cnt
        return result

    def count_vop_taken_by_any(self) -> dict[int, int]:
        result: dict[int, int] = {}
        for inst_idx, produced_ops in self.graph.inst_idx_to_produced_ops.items():
            cnt = sum(1 for op in produced_ops if self.graph.operand_to_consume_edge[op].edge_type == EdgeType.CF_CONSUME)
            if cnt:
                result[inst_idx.idx] = cnt
        return result


    def get_both_inside_concrete_produced_and_consumed_operands(self) -> set[NODE_TYPE]:
        consumed_ops = set()
        for op in self.graph.operand_to_producer:
            proudcer_ = self.graph.operand_to_producer[op]
            # if not isinstance(op, )
            if not proudcer_.is_inside_concrete_inst():
                continue
            consumer_ = self.graph.operand_to_consumer[op]
            if not consumer_.is_inside_concrete_inst():
                continue
            consumed_ops.add(op)
            # pro
        return consumed_ops

    def find_subgraph_ncf_inst_sequences(self) -> list[set[int]]:
        considered_ops = self.graph.ops
        if not considered_ops:
            return []

        processed_ops = set()
        all_sequences = []
        for start_op in considered_ops:
            if start_op in processed_ops:
                continue

            connected_ops = set()
            connected_insts = set()
            self._build_connected_subgraph(
                start_op, connected_ops, connected_insts)

            processed_ops.update(connected_ops)

            if connected_insts:
                concrete_idxs = [
                    inst_idx.idx for inst_idx in connected_insts if inst_idx.inst_type == INST_TYPE.CINST]
                if concrete_idxs:
                    all_sequences.append(concrete_idxs)
        return all_sequences

    def _build_connected_subgraph(self,
                                  op: NODE_TYPE,
                                  connected_ops: set[NODE_TYPE],
                                  connected_insts: set[InstIdx]):

        if op in connected_ops:
            return
        connected_ops.add(op)
        producer_idx = self.graph.operand_to_producer[op]
        consumer_idx = self.graph.operand_to_consumer[op]
        if producer_idx.inst_kind != OneElemTypeInfoKind.COMMON and producer_idx.inst_kind != OneElemTypeInfoKind.NCND:
            return
        if consumer_idx.inst_kind != OneElemTypeInfoKind.COMMON and consumer_idx.inst_kind != OneElemTypeInfoKind.NCND:
            return

        if self.graph.operand_to_producer_relation[op] != EdgeType.CF_PRODUCE:
        # if producer_idx not in connected_insts:
            connected_insts.add(producer_idx)

            for consumed_op in self.graph.inst_idx_to_consumed_ops.get(producer_idx, []):
                self._build_connected_subgraph(
                    consumed_op, connected_ops, connected_insts)
            for produced_op in self.graph.inst_idx_to_produced_ops.get(producer_idx, []):
                self._build_connected_subgraph(
                    produced_op, connected_ops, connected_insts)

        if self.graph.operand_to_consumer_relation[op] != EdgeType.CF_CONSUME:
            if consumer_idx not in connected_insts:
                connected_insts.add(consumer_idx)

                for produced_op in self.graph.inst_idx_to_produced_ops.get(consumer_idx, []):
                    self._build_connected_subgraph(
                        produced_op, connected_ops, connected_insts)
                for consumed_op in self.graph.inst_idx_to_consumed_ops.get(consumer_idx, []):
                    self._build_connected_subgraph(
                        consumed_op, connected_ops, connected_insts)




def build_operand_inst_mapping_naive_v2_consider_special_elem(
    # start_idx: int,
    given_idxs:list[int],
    type_infos: list[OneElemTypeInfo],
    init_stack:Optional[list[str]],
    init_is_given_any_type:bool=False,
    end_stack:Optional[list[str]]=None,

) -> OperandInstGraphV2:
    assert len(given_idxs) == len(type_infos)

    graph = OperandInstGraphV2()
    vop_factory = OpWithTypeFactory()
    stack: list[VopWT] = []
    ref_depth = 0
    if init_stack is None:
        init_stack = []
    for ty in init_stack:
        op = vop_factory.gen_one_vopwt(ref_depth, ty)
        stack.append(op)
        graph.add_edge_produced_by_outside_graph(op)
        ref_depth += 1
    # 
    unreachable_stack_souece: Optional[InstIdx] = None
    if init_is_given_any_type:
        unreachable_stack_souece = ExistingUnknownInstIdx()
    
    for elem_idx, elem_type_info in zip(given_idxs, type_infos):
        cinst = CInstIdx(elem_idx, elem_type_info.kind)
        graph.inst2stack_before_it[cinst] = stack.copy()
        graph.numidx2_instidx[elem_idx] = cinst
        if isinstance(elem_type_info, UnreachableElemTypeInfo):
            _consume_stack_by_control_flow(graph, stack, cinst)
            unreachable_stack_souece = cinst
            ref_depth = 0
        elif isinstance(elem_type_info, ReturnLikeElemTypeInfo):
            # type_strs = elem_type_info.required_types
            taken_ops: list[OneNodeType] = [gen_type_for_graph(t) for t in elem_type_info.required_types]
            
            ref_depth = _process_concrete_taken(
                ref_depth=ref_depth,
                unreachable_stack_souece=unreachable_stack_souece, 
                graph=graph,
                vop_factory=vop_factory,
                stack=stack,
                cinst=cinst,
                taken_ops=taken_ops
                )
       
            _consume_stack_by_control_flow(graph, stack, cinst)
            unreachable_stack_souece = cinst
            ref_depth = 0
        elif isinstance(elem_type_info, BrIfElemTypeInfo):
            required_type_strs = elem_type_info.required_return_types
            taken_type_strs = required_type_strs + ['i32']
            taken_ops: list[OneNodeType] = [gen_type_for_graph(t) for t in taken_type_strs]
            ref_depth = _process_concrete_taken(
                ref_depth=ref_depth,
                unreachable_stack_souece=unreachable_stack_souece, 
                graph=graph, 
                vop_factory=vop_factory, 
                stack=stack,
                cinst=cinst, 
                taken_ops=taken_ops
                )
            ref_depth = _build_concrete_produce(
                graph, 
                vop_factory,
                stack,
                cinst,
                required_type_strs,
                ref_depth
            )
        elif isinstance(elem_type_info, NonControlflowNonDetermined):
            # taken_ops = [gen_type_for_graph('any'), gen_type_for_graph('any'), gen_type_for_graph('i32')]
            # gen_type_list = ['any']
            taken_ops = [gen_type_for_graph(t) for t in elem_type_info.taken_strs]
            gen_type_list = list(elem_type_info.gen_strs)
            # First consume to potentially refine `any` in taken_ops based on actual stack ops.
            ref_depth = _process_concrete_taken(
                ref_depth=ref_depth,
                unreachable_stack_souece=unreachable_stack_souece,
                graph=graph,
                vop_factory=vop_factory,
                stack=stack,
                cinst=cinst,
                taken_ops=taken_ops,
            )
            # For select-like nodes, result type equals the operand value type.
            # If we successfully refined either value operand from `any` to a concrete type,
            # we can also refine the produced `any`.
            if (
                gen_type_list == ['any']
                and len(taken_ops) == 3
                and taken_ops[-1].type_str == 'i32'
            ):
                value_ty = None
                for v in taken_ops[:2]:
                    if v.type_str != 'any':
                        value_ty = v.type_str
                        break
                if value_ty is not None:
                    gen_type_list = [value_ty]

            ref_depth = _build_concrete_produce(
                graph,
                vop_factory,
                stack,
                cinst,
                gen_type_list,
                ref_depth,
            )
            
        elif isinstance(elem_type_info, DeterminedElemTypeInfo):
            taken_ops: list[OneNodeType] = elem_type_info.taken_ops_list
            gen_type_list: list[str] = elem_type_info.gen_types
            ref_depth = _process_one_determined_elem(
                ref_depth=ref_depth, 
                unreachable_stack_souece=unreachable_stack_souece, 
                graph=graph, 
                vop_factory=vop_factory, 
                stack=stack, 
                cinst=cinst, 
                taken_ops=taken_ops, 
                gen_type_list=gen_type_list,
                force_by_taken_ops=not is_debug_env()
                )

    for op in stack:
        graph.add_edge_comsumed_by_outside_graph(op)

    graph.end_stack_ops = [op for op in stack]
    last_is_return_likee = len(type_infos) > 0 and (isinstance(type_infos[-1], ( ReturnLikeElemTypeInfo)))

    # Fill missing end_stack slots with produced operands and mark them consumed outside,
    # so every operand has both a produce and consume edge before validation.
    if end_stack is not None and len(stack) < len(end_stack):
        # if len(stack)
        if last_is_return_likee:
            pass
        else:
            missing = end_stack[len(stack):]
            # if unreachable_stack_souece is not None:
                
            producer = unreachable_stack_souece if unreachable_stack_souece is not None else ExistingUnknownInstIdx()
            for ty in missing:
                op = vop_factory.gen_one_vopwt(ref_depth, ty)
                ref_depth += 1
                graph.add_edge_for_control_flow_produce(op, inst_idx=producer)
                graph.add_edge_comsumed_by_outside_graph(op)
                stack.append(op)

    graph._validate()
    return graph

def _process_one_determined_elem(
    ref_depth:int,
    unreachable_stack_souece:Optional[InstIdx],
    graph: OperandInstGraphV2, 
    vop_factory: OpWithTypeFactory, 
    stack:list[VopWT], 
    cinst: CInstIdx, 
    taken_ops: list[OneNodeType], 
    gen_type_list: list[str],
    force_by_taken_ops: bool=False
) -> int:
    copied_taken_ops = [gen_type_for_graph(taken_op.type_str) for taken_op in taken_ops]
    ref_depth = _process_concrete_taken(
        ref_depth=ref_depth, 
        unreachable_stack_souece=unreachable_stack_souece, 
        graph=graph, 
        vop_factory=vop_factory, 
        stack=stack, 
        cinst=cinst, 
        taken_ops=copied_taken_ops,
        force_by_taken_ops=force_by_taken_ops
        )
    ref_depth = _build_concrete_produce(
        graph, 
        vop_factory, 
        stack,
        cinst, 
        gen_type_list, 
        ref_depth
        )
    
    return ref_depth

def _process_concrete_taken(
    ref_depth:int,
    unreachable_stack_souece:Optional[InstIdx],
    graph: OperandInstGraphV2,
    vop_factory: OpWithTypeFactory, 
    stack: list[VopWT], 
    cinst: CInstIdx, 
    taken_ops: list[OneNodeType],
    force_by_taken_ops: bool=False,
    ):
    input_stack = stack.copy()
    for taken_op in taken_ops[::-1]:
        ref_depth -= 1
        if not stack:
            assert ref_depth <= 0
            op = vop_factory.gen_one_vopwt(ref_depth, taken_op.type_str)
            if unreachable_stack_souece is None:
                graph.add_edge_produced_by_outside_graph(op)
            else:
                graph.add_edge_for_control_flow_produce(op, inst_idx=unreachable_stack_souece)
        else:
            op = stack.pop()
            if taken_op.type_str != 'any' and op.type_info != 'any':
                if op.type_info != taken_op.type_str:
                    if force_by_taken_ops:
                        # forcibly refine the type info based on taken_ops, even if it causes mismatch with actual stack ops
                        op.type_info = taken_op.type_str
                    else:
                        raise ValueError(f"type mismatch: stack has {op.type_info}, expected {taken_op.type_str} at inst {cinst}")
                # assert op.type_info == taken_op.type_str, f"type mismatch: stack has {op.type_info}, expected {taken_op.type_str} at inst {cinst}"
            if taken_op.type_str == 'any' and op.type_info != 'any':
                # keep the type info
                taken_op.type_str = op.type_info
            elif op.type_info == 'any' and taken_op.type_str != 'any':
                # keep the type info
                op.type_info = taken_op.type_str
                
        graph.add_edge_concrete_consume(op, cinst)
    return ref_depth

def _build_concrete_produce(
    graph: OperandInstGraphV2, 
    vop_factory: OpWithTypeFactory, 
    stack:list[VopWT],
    cinst: CInstIdx,
    gen_type_list: list[str], 
    ref_depth: int
):
     
    for gen_type in gen_type_list:
        op = vop_factory.gen_one_vopwt(ref_depth, gen_type)
        ref_depth += 1
        graph.add_edge_concrete_produce(op, cinst)
        stack.append(op)
    return ref_depth


def _consume_stack_by_control_flow(
    graph: OperandInstGraphV2,
    stack:list,
    cinst: CInstIdx
):
    
    for op in stack:
        graph.add_edge_for_control_flow_consume(op, inst_idx=cinst)
    stack.clear()



def build_graph_by_elems(
    elems:list[OneElem],
    context:Context,
    init_stack:Optional[list[str]]=None,
    end_stack:Optional[list[str]]=None,
    start_idx:int=0
):
    elem_num = len(elems)
    given_idxs = list(range(start_idx, start_idx + elem_num))
    type_infos = []
    for elem in elems:
        opcode = elem.inst_opcode
        if opcode == 'unreachable':
            type_info = UnreachableElemTypeInfo()
        elif opcode in ['return', 'br',  'br_table']:
            type_req =  get_inst_ty_req(elem.elem, context)  # type: ignore
            assert type_req is not None
            required_types:list[str] = type_req.ty0.param_types
            type_info = ReturnLikeElemTypeInfo(required_types)
        elif opcode == 'br_if':
            type_req =  get_inst_ty_req(elem.elem, context)  # type: ignore
            assert type_req is not None
            required_types:list[str] = type_req.ty0.param_types
            required_return_types = required_types[:-1]
            type_info = BrIfElemTypeInfo(required_return_types)
        elif opcode == 'select' or opcode == 'ref.is_null':
            if opcode == 'select':
                taken_strs = ['any', 'any', 'i32']
                gen_strs = ['any']
            elif opcode == 'ref.is_null':
                taken_strs = ['any']
                gen_strs = ['i32']
            type_info = NonControlflowNonDetermined(
                taken_strs=taken_strs,
                gen_strs=gen_strs
            )
        else:
            assert elem.is_determined_type()
            gen_types = elem.elem_type_info.gen_ops
            taken_ops_list = elem.elem_type_info.taken_ops
            type_info = DeterminedElemTypeInfo(
                gen_types=gen_types,
                taken_ops_list=taken_ops_list
            )
        type_infos.append(type_info)
    graph = build_operand_inst_mapping_naive_v2_consider_special_elem(
        given_idxs=given_idxs,
        type_infos=type_infos,
        init_stack=init_stack,
        end_stack=end_stack,
        init_is_given_any_type=False
    )
    return graph

