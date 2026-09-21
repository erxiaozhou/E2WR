from pathlib import Path
from util.debug_util import wasm2wat
from extract_block_mutator.InstUtil.Inst import Inst
from reduction_analysis.ASTInfo.AST import NodeList
from reduction_analysis.ASTState import ASTState
from reduction_analysis.ParserModification import MultiPhaseMutationApplier, WMSnapshot
from reduction_analysis.ParserModificationUtil import FuncInstMutation
from reduction_analysis.ReduceUtil.BaseReducer import BaseReducer
from reduction_analysis.ReduceUtil.MutationInstsUtil import (
    FIMutationsInOneNodeList,
    InstsUpdateInfo,
    OneNodeListMutation,
    get_mutated_insts_sequence,
)
from reduction_analysis.ReduceUtil.MutationInstsUtil import OneReduceUnitInfo
from reduction_analysis.ReduceUtil.RewritingUtil.NodeRewriter import NodeRewriter

class OnlyInstSnapshotRewriter:
    def __init__(
        self,
        ast_state: ASTState,
        node_rewriter: NodeRewriter,
        DEBUG: bool = False,
    ):
        self.ast_state = ast_state
        self.node_rewriter = node_rewriter
        self.DEBUG = DEBUG
        self.final_snapshot: WMSnapshot = ast_state.snapshot
        self._base = BaseReducer(
            tmp_used_path=self.node_rewriter.tmp_used_path,
            oracle_func=self.node_rewriter.oracle_func,
            DEBUG=self.DEBUG,
        )

    def update_ast_state_by_final_snapshot(self) -> None:
        self.ast_state.snapshot = self.final_snapshot

    def _build_tmp_snapshot_by_inst_mutations(
        self,

        one_nl_mutations: FIMutationsInOneNodeList,
    ) -> WMSnapshot:
        inst_mutations = sorted(one_nl_mutations.inst_mutations, key=lambda x: x.start_offset, reverse=True)

        tmp_snapshot = self.ast_state.snapshot.copy_for_code_mutations({one_nl_mutations.func_idx})
        mutation_applier = MultiPhaseMutationApplier()
        mutation_applier.merge_to_snapshot(
            snapshot=tmp_snapshot,
            inst_mutations=inst_mutations,
            type_mutations=[],
        )
        mutation_applier.flush_and_encode(
            snapshot=tmp_snapshot,
            output_file=self.node_rewriter.tmp_used_path,
        )
        return tmp_snapshot

    def _build_tmp_snapshot_by_inst_mutations_multi(
        self,
        one_nl_mutations_list: dict[NodeList, FIMutationsInOneNodeList],
    ) -> WMSnapshot:
        func_idxs: set[int] = set()
        inst_mutations: list[FuncInstMutation] = []

        # Sort each function's mutations by start_offset descending to keep slicing stable.
        for one_nl_mutations in one_nl_mutations_list.values():
            func_idxs.add(one_nl_mutations.func_idx)
            inst_mutations.extend(
                sorted(one_nl_mutations.inst_mutations, key=lambda x: x.start_offset, reverse=True)
            )

        tmp_snapshot = self.ast_state.snapshot.copy_for_code_mutations(func_idxs)
        mutation_applier = MultiPhaseMutationApplier()
        mutation_applier.merge_to_snapshot(
            snapshot=tmp_snapshot,
            inst_mutations=inst_mutations,
            type_mutations=[],
        )
        mutation_applier.flush_and_encode(
            snapshot=tmp_snapshot,
            output_file=self.node_rewriter.tmp_used_path,
        )
        return tmp_snapshot

    def try_apply_tmp_snapshot_and_update(
        self,
        one_nl_mutations: FIMutationsInOneNodeList,
        check_invalid_and_return_false=None,
    ) -> bool:
        tmp_snapshot = self._build_tmp_snapshot_by_inst_mutations(
            one_nl_mutations=one_nl_mutations,
        )
        whether_valid = self._base.tmp_file_is_valid()
        if not whether_valid:
            if self.DEBUG:
                wat_path = Path(self.node_rewriter.tmp_used_path).with_suffix('.wat')
                wasm2wat(self.node_rewriter.tmp_used_path, wat_path)

            if check_invalid_and_return_false is None:
                check_invalid_and_return_false = not self.DEBUG
            if not check_invalid_and_return_false:
                raise Exception(
                    f'check_invalid_and_return_false is False, but the wasm is invalid, {wat_path} is saved'
                )
            return False

        can_apply = self._base.oracle_passes()
        if not can_apply:
            return False

        assert tmp_snapshot.parser == self.ast_state.snapshot.parser

        self.final_snapshot = tmp_snapshot
        if self.node_rewriter.best_path:
            self._base.commit_tmp_to(self.node_rewriter.best_path)

        return True
    def try_apply_tmp_snapshot_and_update_multi(
        self,
        one_nl_mutations_list: dict[NodeList, FIMutationsInOneNodeList],
        node_list2node_list_info: dict[NodeList, OneReduceUnitInfo],
        node_list2mutation: dict[NodeList, OneNodeListMutation],
        check_invalid_and_return_false=None,
    ) -> bool:
        tmp_snapshot = self._build_tmp_snapshot_by_inst_mutations_multi(
            one_nl_mutations_list=one_nl_mutations_list,
        )
        whether_valid = self._base.tmp_file_is_valid()
        if not whether_valid:
            if self.DEBUG:
                wat_path = Path(self.node_rewriter.tmp_used_path).with_suffix('.wat')
                wasm2wat(self.node_rewriter.tmp_used_path, wat_path)

            if check_invalid_and_return_false is None:
                check_invalid_and_return_false = not self.DEBUG
            if not check_invalid_and_return_false:
                raise Exception(
                    f'check_invalid_and_return_false is False, but the wasm is invalid, {wat_path} is saved'
                )
            return False

        can_apply = self._base.oracle_passes()
        if not can_apply:
            return False

        assert tmp_snapshot.parser == self.ast_state.snapshot.parser

        self.final_snapshot = tmp_snapshot
        if self.node_rewriter.best_path:
            self._base.commit_tmp_to(self.node_rewriter.best_path)

        for ori_node_list, mutation in node_list2mutation.items():
            unit_info = node_list2node_list_info[ori_node_list]
            insts = get_mutated_insts_sequence(
                mutation.raw_elems,
                mutation.mutation_elem_idx2new_elems,
            )
            unit_info.update_ast_nodes(self.ast_state, insts)

        return True

    def finalize_replace_insts_range(
        self,
        unit_info: OneReduceUnitInfo,
        raw_length: int,
        new_insts: list[Inst],
    ) -> None:
        # ! There is an assumption: there is no new types added
        insts = self.ast_state.snapshot.parser.defined_funcs[unit_info.func_idx].insts
        insts[unit_info.inst_idx:unit_info.inst_idx + raw_length] = new_insts
        self.update_ast_state_by_final_snapshot()

    def finalize_replace_insts_range_v2(
        self,
        insts_update: InstsUpdateInfo
    ) -> None:
        # ! There is an assumption: there is no new types added
        # if self.DEBUG:
        #     parser_before = self.ast_state.snapshot.parser
        #     func_idx = insts_update.seq_loc.func_idx
        #     parser_insts_before = parser_before.defined_funcs[func_idx].insts
        #     final_parser = self.final_snapshot.parser
        #     final_insts = final_parser.defined_funcs[func_idx].insts
        #     print(
        #         "[Finalize-Debug][SnapshotRewriter.before] "
        #         f"func_idx={func_idx}, inst_idx={insts_update.seq_loc.inst_idx}, raw_length={insts_update.raw_length}, "
        #         f"new_insts_len={len(insts_update.new_insts)}, "
        #         f"id(ast_state.snapshot)={id(self.ast_state.snapshot)}, id(final_snapshot)={id(self.final_snapshot)}, "
        #         f"id(parser_before)={id(parser_before)}, id(final_parser)={id(final_parser)}, "
        #         f"parser_len_before={len(parser_insts_before)}, final_parser_len={len(final_insts)}"
        #     )
        insts = self.ast_state.snapshot.parser.defined_funcs[insts_update.seq_loc.func_idx].insts
        insts[insts_update.seq_loc.inst_idx:insts_update.seq_loc.inst_idx + insts_update.raw_length] = insts_update.new_insts
        self.update_ast_state_by_final_snapshot()