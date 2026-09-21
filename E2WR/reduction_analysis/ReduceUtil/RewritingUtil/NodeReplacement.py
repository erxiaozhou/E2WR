from ...ASTInfo.AST import ASTINode, ElseNode, NodeList, BlockNode, LoopNode, IfNode, RootNode


class NodeReplacement:
    def __init__(self, 
                 original_nodes:list[ASTINode], 
                 new_nodes:list[ASTINode]
                 ):
        self.original_nodes = original_nodes
        if len(new_nodes) > 0 and isinstance(new_nodes[0], RootNode):
            assert len(new_nodes) == 1
            new_nodes = new_nodes[0].sub_nodes
        self.new_nodes = new_nodes
        
        self.original_position = None
        self.parent_node = self.identify_parent_node(original_nodes)
        self.replace_root_info = None
        
        if self.parent_node is not None:
            if isinstance(self.parent_node, NodeList):
                self.original_position = self.parent_node.sub_nodes.index(original_nodes[0])
                assert not isinstance(original_nodes, NodeList)
                
            else:
                assert len(original_nodes) == 1 
                assert isinstance(original_nodes[0], NodeList)
                if not original_nodes[0].__class__ == new_nodes[0].__class__:
                    self.new_nodes:list[ASTINode] = [original_nodes[0].__class__(new_nodes, original_nodes[0].loc.func_idx, original_nodes[0].loc.inst_idx)]
        else:
            assert isinstance(original_nodes[0], RootNode) and len(original_nodes) == 1

            self.replace_root_info = {
                'root_node': original_nodes[0],
                'ori_root_sub_nodes': original_nodes[0].sub_nodes,
                'new_root_sub_nodes': new_nodes
            }
            
                
    @classmethod
    def from_one_node(cls, original_node:ASTINode, new_node:ASTINode):
        return cls([original_node], [new_node])
    
    def identify_parent_node(self, nodes:list[ASTINode]):
        if len(nodes) == 1 and isinstance(nodes[0], RootNode):
            return None
        assert len(nodes) > 0
        # print('nodes : ', [n.get_node_info() for n in nodes])
        # assert 0
        base_parent = nodes[0].get_parent()
        for n in nodes[1:]:
            assert n.get_parent() == base_parent
        return base_parent

    def apply_root_replace(self, revert:bool=False):
        assert self.replace_root_info is not None
        root_node = self.replace_root_info['root_node']
        ori_root_sub_nodes = self.replace_root_info['ori_root_sub_nodes']
        new_root_sub_nodes = self.replace_root_info['new_root_sub_nodes']
        if revert:
            ori_root_sub_nodes, new_root_sub_nodes = new_root_sub_nodes, ori_root_sub_nodes
        root_node.replace_split_with_new_sub_nodes(0, len(ori_root_sub_nodes), new_root_sub_nodes)
    
    def apply(self):
        if self.replace_root_info is None:
            self.update_a_node(self.original_nodes, self.new_nodes)
        else:
            self.apply_root_replace(False)
        
    def revert(self):
        if self.replace_root_info is None:
            self.update_a_node(self.new_nodes, self.original_nodes)
        else:
            self.apply_root_replace(revert=True)
        
    def update_a_node(self, nodes:list[ASTINode], new_nodes:list[ASTINode]):
        assert self.parent_node is not None
        if isinstance(nodes[0], NodeList):
            assert len(new_nodes) == 1 and len(nodes) == 1
            ori_node = nodes[0]
            new_node = new_nodes[0]
            if isinstance(self.parent_node, (BlockNode, LoopNode)):
                assert isinstance(new_node, NodeList)
                self.parent_node.set_sub_node_list(new_node)
            elif isinstance(self.parent_node, IfNode):
                is_else = isinstance(ori_node, ElseNode)
                if is_else:
                    assert isinstance(new_node, ElseNode)
                    self.parent_node.set_else_sub_node_list(new_node)
                else:
                    assert isinstance(new_node, NodeList)
                    self.parent_node.set_if_sub_node_list(new_node)
            else:
                raise NotImplementedError(f'{self.parent_node} is not supported')
        else:
            assert isinstance(self.parent_node, NodeList), f'Parent node is {self.parent_node.get_node_info()}'
            assert self.original_position is not None
            original_node_unm = len(nodes)
            # assert 0
            self.parent_node.replace_split_with_new_sub_nodes(self.original_position, self.original_position + original_node_unm, new_nodes)
