from typing import Optional
from extract_block_mutator.WasmParser import WasmParser
from reduction_analysis.ASTInfo.AST import BlockNode, IfNode, LoopNode
from reduction_analysis.ASTInfo.AST import ASTINode, ASTNodeLoc, InstsNode, NodeList, find_nodes_by_predicate, func2AST, is_ancestor_of, traverse_ast


class ASTInfo:
    def __init__(self,
                 raw_ast: dict[int, ASTINode]
                 ):
        self.raw_ast = raw_ast

    def remove_a_func(self, func_idx: int):
        new_raw_ast = {}
        for idx in self.raw_ast:
            if idx > func_idx:
                new_raw_ast[idx-1] = self.raw_ast[idx]
                all_sub_nodes = traverse_ast(
                    new_raw_ast[idx-1], lambda x: x, collect_results=True)
                assert all_sub_nodes is not None
                for sub_node in all_sub_nodes:
                    sub_node.loc.func_idx -= 1
            elif idx == func_idx:
                pass
            else:
                new_raw_ast[idx] = self.raw_ast[idx]
        self.raw_ast = new_raw_ast

    def update_call_insts_in_ast(self, parser: WasmParser):
        raise DeprecationWarning('This function is deprecated')
        # assert 0
        all_insts_nodes: list[InstsNode] = find_nodes_by_predicate(
            self.raw_ast[0], lambda x: isinstance(x, InstsNode))
        for insts_node in all_insts_nodes:
            defined_func_idx = insts_node.loc.func_idx
            start_idx = insts_node.loc.inst_idx
            node_insts = insts_node.get_insts()
            end_idx = start_idx + len(node_insts)
            parser_insts = parser.defined_funcs[defined_func_idx].insts[start_idx:end_idx]
            for idx, (node_inst, parser_inst) in enumerate(zip(node_insts, parser_insts)):
                node_inst_op = node_inst.opcode_text
                parser_inst_op = parser_inst.opcode_text
                if node_inst_op == 'call' or node_inst_op == 'ref.func':

                    if node_inst != parser_inst:
                        # assert 0 , 'To remove the fucntion'
                        insts_node.insts[idx] = parser_inst

    @classmethod
    def from_parser(cls, parser: WasmParser, func_idxs: Optional[list[int]] = None):
        raw_ast: dict[int, ASTINode] = {}
        for func_idx, func in enumerate(parser.defined_funcs):
            if func_idxs is None or func_idx in func_idxs:
                raw_ast[func_idx] = func2AST(func, func_idx, parser.types)
        return cls(raw_ast)

    def get_not_empty_nodes_by_pos(self, pos: ASTNodeLoc) -> list[ASTINode]:
        func_root = self.raw_ast[pos.func_idx]
        all_nodes = traverse_ast(
            func_root, lambda x: x if x.loc == pos else None, collect_results=True)
        assert all_nodes is not None
        all_nodes = [n for n in all_nodes if n.get_length() > 0]
        # assert len(all_nodes) <= 2, print('all_nodes', all_nodes, [x.get_node_info() for x in all_nodes])
        return all_nodes

    def get_parent_node_list_by_pos(self, pos: ASTNodeLoc) -> Optional[NodeList]:
        def has_child_at_pos(node: ASTINode) -> Optional[ASTINode]:
            for child in node.get_sub_nodes():

                if child.loc == pos:
                    if isinstance(child, NodeList):
                        if isinstance(node, NodeList):
                            raise ValueError(
                                'NodeList\'s parent should not be NodeList')
                        else:
                            continue
                    return node
            return None
        results = traverse_ast(
            self.raw_ast[pos.func_idx], has_child_at_pos, collect_results=True)
        assert results is not None

        if len(results) == 0:
            return None
        return results[0]

    def get_parent_node(self, node: ASTINode) -> list[ASTINode]:
        def collect_parent(possible_parent: ASTINode) -> Optional[ASTINode]:
            for child in possible_parent.get_sub_nodes():
                if child == node:
                    return possible_parent
            return None
        results = traverse_ast(
            self.raw_ast[node.func_idx], collect_parent, collect_results=True)
        assert results is not None
        assert len(results) <= 1, print('results', results)
        return results

    def get_all_root_trees(self):
        return list(self.raw_ast.values())

    def get_non_empty_nodes_from_longest_to_shortest(self):
        nodes = self.get_all_non_empty_ast_nodes()
        nodes.sort(key=lambda x: x.get_length(), reverse=True)
        return nodes

    def get_func_root_ast(self, func_idx: int):
        return self.raw_ast[func_idx]

    def get_non_empty_ast_head_pos(self) -> list[ASTNodeLoc]:
        raise DeprecationWarning('This function is deprecated')
        to_insert_pos: list[ASTNodeLoc] = []
        all_sub_trees: list[ASTINode] = self.get_all_non_empty_ast_nodes()
        for sub_tree in all_sub_trees:
            to_insert_pos.append(sub_tree.loc)
        return to_insert_pos

    def get_all_non_empty_ast_nodes(self, considered_func_idxs:Optional[set[int]]=None) -> list[ASTINode]:
        nodes = []
        for tree in self.get_all_root_trees():
            if considered_func_idxs is not None and tree.loc.func_idx not in considered_func_idxs:
                continue
            sub_nodes = traverse_ast(
                tree, lambda x: x if x.get_length() > 0 else None, collect_results=True)
            assert sub_nodes is not None
            nodes.extend(sub_nodes)
        return nodes

    def get_root_inode_of_a_node(self, node: ASTINode) -> ASTINode:
        return self.raw_ast[node.func_idx]

    def get_node_by_tail_loc(self, tail_loc: ASTNodeLoc) -> list[ASTINode]:
        cur_root_ast = self.raw_ast[tail_loc.func_idx]
        sub_nodes = traverse_ast(cur_root_ast, lambda x: x if x.loc.inst_idx +
                                 x.get_length()-1 == tail_loc.inst_idx else None, collect_results=True)
        assert sub_nodes is not None
        return sub_nodes

    def node_is_removed(self, node: ASTINode) -> bool:
        root_of_node = self.get_root_inode_of_a_node(node)
        if not is_ancestor_of(root_of_node, node):
            return True
        return False

    def update_node_loc_after_replace_one_node(
        self,
        loc: ASTNodeLoc,
        ori_length: int,
        new_length: int
    ):
        raise DeprecationWarning('This function is deprecated')
        func_idx = loc.func_idx
        cur_root_ast = self.raw_ast[func_idx]
        sub_nodes = traverse_ast(cur_root_ast, lambda x: x if x.loc.inst_idx >=
                                 loc.inst_idx else None, collect_results=True)
        ori_start = loc.inst_idx
        ori_end = loc.inst_idx + ori_length
        assert sub_nodes is not None
        to_add_length = new_length - ori_length
        for n in sub_nodes:
            if n.loc.inst_idx > ori_start:
                assert n.loc.inst_idx >= ori_end, print(
                    f'n.loc : {n.loc}, ori_end: {ori_end}, loc: {loc}, to_add_length: {to_add_length}, new_length: {new_length}, ori_length: {ori_length}, n: {n.get_node_info()}')
                n.loc.inst_idx += to_add_length

    def update_loc_info(self, func_idx: int):
        cur_root_ast = self.raw_ast[func_idx]

        def update_node_positions(node, current_position):

            node.loc.inst_idx = current_position

            if isinstance(node, (BlockNode, LoopNode)):
                sub_list = node.sub_node_list
                update_node_positions(sub_list, current_position + 1)

            elif isinstance(node, IfNode):
                update_node_positions(
                    node.if_sub_node_list, current_position + 1)

                if node.has_else:
                    else_position = current_position + 1 + node.if_sub_node_list.get_length()
                    update_node_positions(
                        node.else_sub_node_list, else_position + 1)

            elif isinstance(node, NodeList):
                position = current_position
                for sub_node in node.get_sub_nodes():
                    update_node_positions(sub_node, position)
                    position += sub_node.get_length()

        update_node_positions(cur_root_ast, 0)
        # print('After update root is ', cur_root_ast.get_node_info())

    def get_all_nodes_in_a_func(self, func_idx: int) -> list[ASTINode]:
        sub_nodes = traverse_ast(
            self.raw_ast[func_idx], lambda x: x, collect_results=True)
        assert sub_nodes is not None
        return sub_nodes
