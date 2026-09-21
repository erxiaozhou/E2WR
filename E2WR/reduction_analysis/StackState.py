from enum import Enum
from extract_block_mutator.funcTypeFactory import funcTypeFactory
from extract_block_mutator.typeReq import typeReq


class StackStatus(Enum):
    NORMAL = 0
    ANY = 1
    # REQUIRE_LEAST = 2



class StackState:
    def __init__(self, all_rest_types:list[list[str]], status:StackStatus):
        self.all_rest_types = all_rest_types
        self.rest_types = all_rest_types[0]
        self.status = status

    def __str__(self):
        return f'{self.__class__.__name__}(all_rest_types={self.all_rest_types}, rest_types={self.rest_types}, status={self.status})'

    def __repr__(self):
        return self.__str__()

    def as_type_req(self)->typeReq:
        # ret_ty = self.rest_types
        all_tys = [funcTypeFactory.generate_one_func_type_default([], ret_ty) for ret_ty in self.all_rest_types]
        if self.status == StackStatus.ANY:
            req_type = 'eg_param_and_result'
        else:
            req_type = 'eq'
        return typeReq(
            tys=all_tys,
            req_type=req_type
        )
    def __eq__(self, other):
        if not isinstance(other, StackState):
            return False
        return self.rest_types == other.rest_types and self.status == other.status
    
    def __hash__(self):
        return hash((tuple(self.rest_types), self.status))


def sstate1_support_sstate2(sstate1:StackState, sstate2:StackState)->bool:
    if sstate1.status == StackStatus.NORMAL and sstate2.status == StackStatus.ANY:
        return False
    if sstate1.status == StackStatus.ANY :
        all_match = True
        for rest_types2 in sstate2.all_rest_types:
            if not any(
                _any_support(rest_types1, rest_types2)
                for rest_types1 in sstate1.all_rest_types
            ):
                all_match = False
                break
        return all_match
    else:
        # return sstate1.rest_types == sstate2.rest_types
        all_match = True
        for rest_types2 in sstate2.all_rest_types:
            if not any(
                _types_eq_match(rest_types1, rest_types2)
                for rest_types1 in sstate1.all_rest_types
            ):
                all_match = False
                break
        return all_match

def _any_support(
    rest_types1:list[str],
    rest_types2:list[str],
) -> bool:
    rest_type_num1 = len(rest_types1)
    rest_type_num2 = len(rest_types2)
    if rest_type_num1 < rest_type_num2:
        return rest_types1 == rest_types2[rest_type_num2-rest_type_num1:]
    elif rest_type_num1 == rest_type_num2:
        return rest_types1 == rest_types2
    else:
        return False


def _types_eq_match(
    types1:list[str],
    types2:list[str],
) -> bool:
    if len(types1) != len(types2):
        return False
    for type1, type2 in zip(types1, types2):
        if type1 != type2:
            return False
    return True

def get_stack_state_from_type_req(type_req:typeReq)->StackState:
    
    
    if type_req.tys[0].determined_return_ty:
        rest_types:list[list[str]] = [[]]
    else:
        rest_types:list[list[str]] = [ty.result_types for ty in type_req.tys]
    # status = StackStatus.NORMAL if type_req.req_type == 'eq' else StackStatus.ANY
    req_type = type_req.req_type
    if req_type == 'eq' or req_type == 'eg_param_f':
        status = StackStatus.NORMAL
    elif req_type == 'eg_param_and_result' or 'unreachable':
        status = StackStatus.ANY
    else:
        raise ValueError(f'Unknown req_type: {req_type}')
    return StackState(rest_types, status)
