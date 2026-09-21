
from reduction_analysis.ASTInfo.AST import ASTINode, ASTNodeLoc


class ProcessedNodeId:
    def __init__(self,
                 func_idx:int,
                 start_idx:int,
                 length:int,
                 ):
        self.func_idx = func_idx
        self.start_idx = start_idx
        self.length = length

    def __eq__(self, other):
        return self.func_idx == other.func_idx and self.start_idx == other.start_idx and self.length == other.length

    def __hash__(self):
        return hash((self.func_idx, self.start_idx, self.length))
    
    def __str__(self):
        return f'{self.__class__.__name__}(func_idx={self.func_idx}, start_idx={self.start_idx}, length={self.length})'
    
    def __repr__(self):
        return self.__str__()

def get_feature_of_node(node:ASTINode)->ProcessedNodeId:
    return ProcessedNodeId(node.func_idx, node.inst_idx, node.get_length())


class ProcessedNodeManager:
    def __init__(self):
        self.ignore_nodes = set()

    def clear_ignore_nodes(self):
        self.ignore_nodes = set()
        
    def add_node(self, node:ASTINode):
        feature = get_feature_of_node(node)
        self.ignore_nodes.add(feature)

    def get_not_ignored_nodes(self, nodes:list[ASTINode])->list[ASTINode]:
        result = []
        for node in nodes:
            if not self.is_node_ignored(node):
                result.append(node)
        return result

    def update_existing_ignored_by_reduce(self, loc:ASTNodeLoc, reduced_size:int, ori_node_length:int) -> None:
        assert reduced_size > 0
        for n in self.ignore_nodes:
            if n.func_idx == loc.func_idx:
                if n.start_idx >= loc.inst_idx + ori_node_length:
                    n.start_idx -= reduced_size
        

    def is_node_ignored(self, node:ASTINode)->bool:
        feature = get_feature_of_node(node)
        return feature in self.ignore_nodes
