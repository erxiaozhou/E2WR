import math
from typing import Callable



def cal_actual_ig(
    to_cal_elems:set, 
    cleaned_fail_history:list[set], 
    p_dict:dict, 
    func_cal_failed_prob:Callable,
    pass_:bool
):
    pass_prob = cal_pass_prob(p_dict, to_cal_elems)
    p0s = [p_dict[elem] for elem in to_cal_elems]
    pass_gain = _cal_pass_gain(to_cal_elems, cleaned_fail_history, p_dict, p0s)
    # assert pass_gain < 2
    # cal the information gain if fail
    fail_gain = _cal_fail_gain(to_cal_elems, p_dict, func_cal_failed_prob, p0s)
    if pass_:
        return pass_gain
    else:
        return fail_gain
    
def cal_ieg2m(
    to_cal_elems:set, 
    cleaned_fail_history:list[set], 
    p_dict:dict, 
    func_cal_failed_prob:Callable
):
   
    pass_prob = cal_pass_prob(p_dict, to_cal_elems)
    p0s = [p_dict[elem] for elem in to_cal_elems]
    pass_gain = _cal_pass_gain(to_cal_elems, cleaned_fail_history, p_dict, p0s)
    pass_gain_part = pass_prob * pass_gain
    # cal the information gain if fail
    fail_gain = _cal_fail_gain(to_cal_elems, p_dict, func_cal_failed_prob, p0s)
    fail_gain_part = fail_gain * (1 - pass_prob)
    return pass_gain_part + fail_gain_part

def _cal_fail_gain(to_cal_elems, p_dict, func_cal_failed_prob, p0s):
    new_d = func_cal_failed_prob(to_cal_elems, p_dict)
    new_p0s = [new_d[elem] for elem in to_cal_elems]
    fail_gain = cal_kl_gain(p0s, new_p0s)
    return fail_gain

def _cal_pass_gain(to_cal_elems, cleaned_fail_history, p_dict, p0s):
    cur_entropy = cal_entropy_core(p0s)
    # cal the information gain if pass
    pass_gain_1 = cur_entropy - 0  # 0, because the probs are set to 0
    must_preserve_elems = _sample_can_identify_cannot_remove(to_cal_elems, cleaned_fail_history)
    pass_gain_2 = 0
    for must_preserve_elem in must_preserve_elems:
        pass_gain_2 += cal_one_elem_entropy(p_dict[must_preserve_elem])
    pass_gain = pass_gain_1 + pass_gain_2
    # assert len(must_preserve_elems) != 2, f'pass_gain_1: {pass_gain_1}, pass_gain_2: {pass_gain_2}'
    return pass_gain


# def cal_one_sample

def cal_kl_gain(p0s:list[float], new_p0s:list[float]):
    sum_ = 0
    for p0, p1 in zip(p0s, new_p0s):
        p0 = min(p0, 1-1e-6)
        p1 = min(p1, 1-1e-6)
        part1 = p1 * math.log2(p1 / p0)
        part2 = (1 - p1) * math.log2((1 - p1) / (1 - p0))
        sum_ += part1 + part2
    return sum_


def _sample_can_identify_cannot_remove(to_cal_elems:set, cleaned_fail_history:list[set]):
    to_cal_elem_num = len(to_cal_elems)
    expected_len = to_cal_elem_num + 1
    result = set()
    for one_log in cleaned_fail_history:
        if len(one_log) != expected_len:
            continue
        one_log = set(one_log)
        if to_cal_elems.issubset(one_log):
            result.add(list(one_log - to_cal_elems)[0])
    return result


def cal_pass_prob(p_dict, elems):
    p = 1
    for elem in elems:
        p *= (1 - p_dict[elem])
    return p


def cal_entropy_core(ps:list[float]):
    sum_ = 0
    for p in ps:
        sum_ += cal_one_elem_entropy(p)
    return sum_


def cal_one_elem_entropy(p):
    if p == 0 or p == 1:
        return 0
    return -p * math.log2(p) - (1 - p) * math.log2(1 - p)
