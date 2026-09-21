from extract_block_mutator.InstUtil.Inst import Inst
from typing import Any, Optional
from enum import Enum


class Vop:
    def __init__(
        self,
        idx: int
    ):
        self.idx = idx

    def __str__(self):
        return f"VOP_{self.idx}"

    def __repr__(self):
        return self.__str__()

    def __eq__(self, other):
        return self.idx == other.idx

    def __hash__(self):
        return hash(self.idx)


class VOPFactory:
    _instance = None
    cur_idx = 0

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super(VOPFactory, cls).__new__(cls)
        return cls._instance

    def gen_one_vop(self):
        result = Vop(VOPFactory.cur_idx)
        VOPFactory.cur_idx += 1
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
    EXISTING_UNKNOWN = 6

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
        return self.inst_type == INST_TYPE.CINST and self.inst_kind != OneElemTypeInfoKind.EXISTING_UNKNOWN

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
        super().__init__(idx, INST_TYPE.CINST, OneElemTypeInfoKind.EXISTING_UNKNOWN)
    

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
    def __init__(self, operand: Vop, inst_idx: InstIdx, edge_type: EdgeType):
        self.operand = operand
        self.inst_idx = inst_idx
        self.edge_type = edge_type

    def __str__(self):
        return f"{self.inst_idx} -{self.edge_type}-> {self.operand}"
    def __repr__(self):
        return self.__str__()


class OperandInstGraph:
    def __init__(self):
        self.edges: list[Edge] = []
        self.operand_to_producer: dict[Vop, Optional[InstIdx]] = {}
        self.operand_to_consumers: dict[Vop, Optional[InstIdx]] = {}
        self.inst_idx_to_produced_ops: dict[InstIdx, list[Vop]] = {}
        self.inst_idx_to_consumed_ops: dict[InstIdx, list[Vop]] = {}

    def add_edge(self, operand: Vop, inst_idx: InstIdx, edge_type: EdgeType):
        edge = Edge(operand, inst_idx, edge_type)
        self.edges.append(edge)

        if edge_type == EdgeType.PRODUCE:
            if self.operand_to_producer.get(operand) is not None:
                raise ValueError(
                    f"op {operand} already produced by inst {self.operand_to_producer[operand]}, cannot be produced by inst {inst_idx}")

            self.operand_to_producer[operand] = inst_idx
            if inst_idx not in self.inst_idx_to_produced_ops:
                self.inst_idx_to_produced_ops[inst_idx] = []
            self.inst_idx_to_produced_ops[inst_idx].append(operand)
        else:
            if operand in self.operand_to_consumers:
                raise ValueError(
                    f"op {operand} already consumed by inst {self.operand_to_consumers[operand]}, cannot be consumed by inst {inst_idx}")

            self.operand_to_consumers[operand] = inst_idx
            if inst_idx not in self.inst_idx_to_consumed_ops:
                self.inst_idx_to_consumed_ops[inst_idx] = []
            self.inst_idx_to_consumed_ops[inst_idx].append(operand)

    def add_initial_operand(self, operand: Vop):
        self.operand_to_producer[operand] = None

    def mark_remaining_operand(self, operand: Vop):
        if operand not in self.operand_to_consumers:
            self.operand_to_consumers[operand] = None

    def connect_init_and_final_ops(self, op1: Vop, op2: Vop):
        assert self.operand_to_producer.get(op1) is None
        assert self.operand_to_consumers.get(op2) is None
        op2_producer = self.operand_to_producer.get(op2)
        if op2_producer is not None:
            self.add_edge(op1, op2_producer, EdgeType.PRODUCE)
            # remove op2
            self.operand_to_producer.pop(op2)
            # self.operand_to_consumers.pop(op2)
            ori_inst_produce_ops = self.inst_idx_to_produced_ops[op2_producer]
            new_inst_produce_ops = [
                op for op in ori_inst_produce_ops if op != op2]
            self.inst_idx_to_produced_ops[op2_producer] = new_inst_produce_ops

    def get_produced_and_consumed_operands(self) -> set[Vop]:
        consumed_ops = set()
        for op in self.operand_to_producer:
            if self.operand_to_producer[op] is not None:
                if op in self.operand_to_consumers and self.operand_to_consumers[op] is not None:
                    consumed_ops.add(op)
        return consumed_ops

    def find_zero_effect_inst_sequences(self) -> list[set[int]]:
        produced_and_consumed = self.get_produced_and_consumed_operands()

        if not produced_and_consumed:
            return []

        processed_ops = set()
        initial_subgraphs = []
        zero_effect_sequences = []

        for start_op in produced_and_consumed:
            if start_op in processed_ops:
                continue

            connected_ops = set()
            connected_insts = set()
            self._build_connected_subgraph(
                start_op, connected_ops, connected_insts)

            processed_ops.update(connected_ops)

            if connected_insts:
                initial_subgraphs.append((connected_ops, connected_insts))

            concrete_idxs = [
                inst_idx.idx for inst_idx in connected_insts if inst_idx.inst_type == INST_TYPE.CINST]
            cur_seq_produced_op_num = 0
            for connected_inst in connected_insts:
                gen_ops = self.inst_idx_to_produced_ops.get(connected_inst, [])
                cur_seq_produced_op_num += len(gen_ops)
            cur_seq_consumed_op_num = 0
            for connected_inst in connected_insts:
                consumed_ops = self.inst_idx_to_consumed_ops.get(
                    connected_inst, [])
                cur_seq_consumed_op_num += len(consumed_ops)
            # print( f'Current group {concrete_idxs} produced {cur_seq_produced_op_num} ops, consumed {cur_seq_consumed_op_num} ops')
            if cur_seq_produced_op_num == cur_seq_consumed_op_num:
                zero_effect_sequences.append(concrete_idxs)

        return zero_effect_sequences

    def _build_connected_subgraph(self,
                                  op: Vop,
                                  connected_ops: set[Vop],
                                  connected_insts: set[InstIdx]):

        if op in connected_ops:
            return
        connected_ops.add(op)
        producer_idx = self.operand_to_producer.get(op)
        consumer_idx = self.operand_to_consumers.get(op)

        if producer_idx is not None:
            if producer_idx not in connected_insts:
                connected_insts.add(producer_idx)

                for consumed_op in self.inst_idx_to_consumed_ops.get(producer_idx, []):
                    self._build_connected_subgraph(
                        consumed_op, connected_ops, connected_insts)
                for produced_op in self.inst_idx_to_produced_ops.get(producer_idx, []):
                    self._build_connected_subgraph(
                        produced_op, connected_ops, connected_insts)

        if consumer_idx is not None:
            if consumer_idx not in connected_insts:
                connected_insts.add(consumer_idx)

                for produced_op in self.inst_idx_to_produced_ops.get(consumer_idx, []):
                    self._build_connected_subgraph(
                        produced_op, connected_ops, connected_insts)
                for consumed_op in self.inst_idx_to_consumed_ops.get(consumer_idx, []):
                    self._build_connected_subgraph(
                        consumed_op, connected_ops, connected_insts)


def build_operand_inst_mapping(
    initial_stack_size: int,
    inst_stack_diff: list[tuple[int, int]],
    common_stack_size: int,
    required_stack_size: int
) -> OperandInstGraph:
    assert common_stack_size <= required_stack_size

    graph = OperandInstGraph()
    vop_factory = VOPFactory()
    v_inst_idx_factory = VInstIdxFactory()
    stack: list[Vop] = []
    for i in range(initial_stack_size):
        op = vop_factory.gen_one_vop()
        stack.append(op)
        graph.add_initial_operand(op)
    common_stack_begin = stack[-common_stack_size:]
    for inst_idx, (taken, stored) in enumerate(inst_stack_diff):
        for _ in range(taken):
            if not stack:
                op = vop_factory.gen_one_vop()
                graph.add_initial_operand(op)
                v_inst_idx = v_inst_idx_factory.gen_one_vinst_idx()
                graph.add_edge(op, v_inst_idx, EdgeType.PRODUCE)
            else:
                op = stack.pop()
            cur_inst_idx = CInstIdx(inst_idx)
            graph.add_edge(op, cur_inst_idx, EdgeType.CONSUME)

        for _ in range(stored):
            op = vop_factory.gen_one_vop()
            graph.add_edge(op, CInstIdx(inst_idx), EdgeType.PRODUCE)
            stack.append(op)
    cur_stack_len = len(stack)
    common_stack_end = stack[cur_stack_len-common_stack_size:]

    for begin_node, end_node in zip(common_stack_begin, common_stack_end):
        graph.connect_init_and_final_ops(begin_node, end_node)

    for op in stack[cur_stack_len-required_stack_size:cur_stack_len-common_stack_size]:
        graph.mark_remaining_operand(op)
    for op in stack[:cur_stack_len-required_stack_size]:
        v_inst_idx = v_inst_idx_factory.gen_one_vinst_idx()
        graph.add_edge(op, v_inst_idx, EdgeType.CONSUME)
    return graph
