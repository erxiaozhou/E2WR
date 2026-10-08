from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

from .ReduceInsts_V5_util import (
    OneElem,
    StackChange,
    gen_type_for_graph,
    get_seq_common_prefix_len,
    ElemTypeInfo,
)
from .ReduceInsts_cfg_util import V7Cfg
from reduction_analysis.ReduceUtil.NewInstUtil import get_inst_by_require_ty_const_n
from .ReduceInsts_V5_util import DropOrType
from extract_block_mutator.funcTypeFactory import funcTypeFactory
from extract_block_mutator.InstGeneration.InstFactory import InstFactory

from .V6V1GraphHelper import SGComponentWOpInfo, SubGraph
from .ElemOperandMappingV2 import VopWT


@dataclass(frozen=True, slots=True)
class ConsumedOperandInfo:
    operand: Optional[VopWT]
    replacement_elem: OneElem


@dataclass(frozen=True, slots=True)
class ProducedOperandInfo:
    operand: Optional[VopWT]
    replacement_elem: OneElem


@dataclass(frozen=True, slots=True)
class _ReplacementComponentPlan:
    anchor_elem_idx: int
    removed_elem_idxs: list[int]
    consumed_operands: list[ConsumedOperandInfo]
    produced_operands: list[ProducedOperandInfo]


class Replacement:
    def __init__(
        self,
        *,
        sg_idx: int,
        component_plans: Sequence[_ReplacementComponentPlan],
        concrete_mutation: Optional[dict[int, list[OneElem]]] = None,
    ) -> None:
        self.sg_idx = int(sg_idx)
        concrete_mutation = None if concrete_mutation is None else {int(k): list(v) for k, v in concrete_mutation.items()}
        self.is_operand_aware = concrete_mutation is None
        self.covered_elem_idxs: set[int] = set()
        self.materialized_mutation: dict[int, list[OneElem]] = {}
        self.elem_idx2operands: dict[int, tuple[Optional[VopWT], ...]] = {}
        self.consumed_operands: set[VopWT] = set()
        self.produced_operands: set[VopWT] = set()

        for plan in component_plans:
            self.covered_elem_idxs.update(plan.removed_elem_idxs)
            self.consumed_operands.update(
                info.operand
                for info in plan.consumed_operands
                if info.operand is not None
            )
            self.produced_operands.update(
                info.operand
                for info in plan.produced_operands
                if info.operand is not None
            )

            new_elems = [info.replacement_elem for info in plan.consumed_operands]
            new_elems.extend(info.replacement_elem for info in plan.produced_operands)
            self.materialized_mutation[plan.anchor_elem_idx] = new_elems
            self.elem_idx2operands[plan.anchor_elem_idx] = tuple(
                info.operand for info in plan.consumed_operands
            ) + tuple(
                info.operand for info in plan.produced_operands
            )
            for idx in plan.removed_elem_idxs[1:]:
                self.materialized_mutation[idx] = []
                self.elem_idx2operands[idx] = tuple()

        if concrete_mutation is not None:
            self.covered_elem_idxs.update(concrete_mutation.keys())
            self.materialized_mutation = {int(k): list(v) for k, v in concrete_mutation.items()}
            self.elem_idx2operands = {int(k): tuple() for k in concrete_mutation.keys()}

    def gen_concrete_elems(
        self,
        *,
        skip_consumed_operands: Optional[set[VopWT]] = None,
        skip_produced_operands: Optional[set[VopWT]] = None,
    ) -> dict[int, list[OneElem]]:
        blocked_operands: set[VopWT] = set()
        if skip_consumed_operands is not None:
            blocked_operands.update(skip_consumed_operands)
        if skip_produced_operands is not None:
            blocked_operands.update(skip_produced_operands)
        if not self.is_operand_aware or not blocked_operands:
            return self.materialized_mutation

        mutation: dict[int, list[OneElem]] = {}
        for elem_idx, elems in self.materialized_mutation.items():
            operands = self.elem_idx2operands[elem_idx]
            if not operands:
                assert len(elems) == 0
                mutation[elem_idx] = []
                continue
            assert len(elems) == len(operands)
            mutation[elem_idx] = [
                elem
                for elem, operand in zip(elems, operands)
                if operand not in blocked_operands
            ]
        return mutation

    def materialize_mutation(self) -> dict[int, list[OneElem]]:
        return self.materialized_mutation


def materialize_replacements(
    replacements: Sequence[Replacement],
) -> dict[int, list[OneElem]]:
    operand_aware = [
        r
        for r in replacements
        if r.is_operand_aware
    ]
    internal_operands: set[VopWT] = set()
    if operand_aware:
        produced = set().union(*(repl.produced_operands for repl in operand_aware))
        consumed = set().union(*(repl.consumed_operands for repl in operand_aware))
        internal_operands = produced & consumed


    result: dict[int, list[OneElem]] = {}
    for repl in replacements:
        cur = repl.gen_concrete_elems(
            skip_consumed_operands=internal_operands,
            skip_produced_operands=internal_operands,
        )
       
        for k, v in cur.items():
            if k in result and result[k] != v:
                raise ValueError(f'Conflicting concrete mutations at elem_idx={k}')
            result[k] = v
  
    return result


def gen_produced_operand_elem(
    *,
    operand_type: str,
) -> OneElem:
    inst = get_inst_by_require_ty_const_n(operand_type)
    return OneElem(
        elem_idx=-1,
        elem=inst,
        elem_type_info=ElemTypeInfo(
            DropOrType(
                is_drop=False,
                inst_type=funcTypeFactory.generate_one_func_type_default([], [operand_type]),
            )
        ),
    )


def get_elem_mutation_for_sg_ng(
    sg: SubGraph,
    cfg: V7Cfg,
) -> Optional[Replacement]:
    assert sg.components
    assert all(c.elem_idxs for c in sg.components)

    components: list[SGComponentWOpInfo] = sorted(sg.components, key=lambda c: c.elem_idxs[0])
    bypass_cancel = not getattr(sg, 'enable_internal_cancel', True)

    skip_num = 0
    if not bypass_cancel:
        for input_op, output_op in zip(sg.raw_external_input_ops, sg.final_output_ops):
            if input_op.type_info != output_op.type_info:
                break
            skip_num += 1
    skipped_input_ops: set[VopWT] = set(sg.raw_external_input_ops[:skip_num])
    final_output_ops: list[VopWT] = sg.final_output_ops[skip_num:]

    component_plans: list[_ReplacementComponentPlan] = []
    for comp_i, comp in enumerate(components):
        if bypass_cancel:
            external_consumed_ops = list(comp.taken_op_list)
            ops_to_rebuild = comp.gen_op_list
        else:
            external_consumed_ops = [
                op for op in comp.taken_op_list
                if op in sg.ops_from_outside and op not in skipped_input_ops
            ]
            ops_to_rebuild = final_output_ops if comp_i == len(components) - 1 else []

        produced_operands: list[ProducedOperandInfo] = []
        for op in ops_to_rebuild:
            ty = op.type_info
            if ty == 'any':
                return None
            produced_operands.append(
                ProducedOperandInfo(
                    operand=op,
                    replacement_elem=gen_produced_operand_elem(
                        operand_type=ty,
                    ),
                )
            )

        consumed_operands = [
            ConsumedOperandInfo(
                operand=op,
                replacement_elem=OneElem(
                    elem_idx=-1,
                    elem=InstFactory.opcode_inst('drop'),
                    elem_type_info=ElemTypeInfo(DropOrType(is_drop=True)),
                ),
            )
            for op in external_consumed_ops
        ]
        removed_elem_idxs = sorted(comp.elem_idxs)
        component_plans.append(
            _ReplacementComponentPlan(
                anchor_elem_idx=removed_elem_idxs[0],
                removed_elem_idxs=removed_elem_idxs,
                consumed_operands=consumed_operands,
                produced_operands=produced_operands,
            )
        )

    if not component_plans:
        print('[get_elem_mutation_for_sg_ng:no-plan]', {'sg_idx': sg.idx})
        return None

    return Replacement(
        sg_idx=sg.idx,
        component_plans=component_plans,
    )

