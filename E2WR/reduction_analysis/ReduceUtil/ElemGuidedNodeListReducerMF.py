from reduction_analysis.ReduceUtil.ReduceInsts_V5_util import OneElem



def transform_group_mutation_to_standard_param(ori_groups, groups_mutation):
    raw_elems:list[OneElem] = []
    assert groups_mutation
    # * convert groups_mutation to mutation_elem_idx2new_elems
    mutation_elem_idx2new_elems:dict[int, list[OneElem]] = {}
    cur_elem_num = 0
    for group_idx, group in enumerate(ori_groups):
        if group_idx in groups_mutation:
            new_group = groups_mutation[group_idx]
            new_elems = new_group.elems
            ori_elem_num = len(group.elems)
            new_elem_num = len(new_elems)
            if ori_elem_num == 0:
                assert new_elem_num == 0
            # for i in range(ori_elem_num):
            mutation_elem_idx2new_elems[cur_elem_num] = new_elems
            for i in range(1, ori_elem_num):
                mutation_elem_idx2new_elems[cur_elem_num + i] = []
            # cur_elem_num += ori_elem_num
        # else:
        cur_elem_num += len(group.elems)
        raw_elems.extend(group.elems)
    return raw_elems,mutation_elem_idx2new_elems
