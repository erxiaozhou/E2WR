import collections


class InferP0History:
    _NEW_UPDATE_P0_HISTORY_MAXLEN = 10

    def __init__(self):
        
        self._all_re_construct_p0_history = []
        # A bounded FIFO pool of the most recent logs.
        # When full, the oldest items are automatically dropped.
        self.new_update_p0_history = collections.deque(maxlen=self._NEW_UPDATE_P0_HISTORY_MAXLEN)

    def add_one_log(self, deleteconfig, test_result:int):
        self.new_update_p0_history.append((deleteconfig, test_result))
        self._all_re_construct_p0_history.append((deleteconfig, test_result))

    def reset_new_update_p0_history(self):
        self.new_update_p0_history.clear()

    def update_p0_using_history(self, p0, ori_p:collections.OrderedDict):
        print('Update p globally using history')
        _update_p_using_all_logs(self._all_re_construct_p0_history, p0, ori_p)


def _update_p_using_all_logs(all_re_construct_p0_history, p0, ori_p:collections.OrderedDict):
    all_re_construct_p0_history = _get_reconstruct_p0_history(all_re_construct_p0_history, ori_p)
    for elem in ori_p.keys():
        cur_p = ori_p[elem]
        if cur_p <=1e-6 or cur_p >= 1-1e-6:
            continue
        ori_p[elem] = p0
    _update_p_with_reconstructed_p0_history_v1(all_re_construct_p0_history, ori_p, p0)

def _update_p_with_reconstructed_p0_history_v1(all_re_construct_p0_history, ori_p:collections.OrderedDict, p0):
    for one_log in all_re_construct_p0_history:
        # if len(one_log[0]) <= 20:
        #     print('Considered log: ', one_log)
        # else:
        #     print('Considered log size: ', len(one_log[0]))
        assert one_log[1] == 1
        _update_p_using_one_log(one_log[0], ori_p)

def _update_p_using_one_log(deleteconfig, p):
    # pn = 1
    for elem in deleteconfig:
        if p[elem] > 0.8:
            continue
        assert elem in p
        # cur_p = p[elem]
        # pn *= (1 - cur_p)
        res = _computRatio(deleteconfig, p)
    # for elem in deleteconfig:
    #     if p[elem] > 0.8:
    #         continue
        # ori_p = min(p[elem], 0.79)
        # p[elem] = min(ori_p / p_base, 0.79)
        p[elem] = min(p[elem] * res, 0.79)
        # p[elem] = p[elem] * res


def _computRatio( deleteconfig, p):
    res = 0
    tmplog = 1
    for delc in deleteconfig:
        if p[delc] > 0 and p[delc] < 1:
            tmplog *= (1 - p[delc])
    res = 1 / (1 - tmplog)
    return res
def _get_reconstruct_p0_history(
    all_re_construct_p0_history: list[tuple[list[int], int]],
    p:collections.OrderedDict
    ):
    # 
    eq1_elems = set()
    eq0_elems = set()
    for elem, _prob in p.items():
        if _prob >= 1:
            eq1_elems.add(elem)
        elif _prob <= 1e-6:
            eq0_elems.add(elem)
    result = []
    visited_logs = []
    for one_log in all_re_construct_p0_history:
        cur_elems = set(one_log[0])
        if one_log[1] == 0:
            continue
        if cur_elems.intersection(eq1_elems):
            continue
        cur_elems = cur_elems - eq0_elems
        visiting_repr = frozenset(cur_elems)
        if visiting_repr in visited_logs:
            # print('meet!')
            continue
        visited_logs.append(visiting_repr)
        one_log = (list(cur_elems), 1)
        # print('one_log: ', one_log)
        result.append(one_log)
    return result