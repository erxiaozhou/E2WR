import collections


class InnerInfo:
    def __init__(self, p_dict):
        self.ori_elem_num = len(p_dict)
        self.considered_elems = set(p_dict.keys())
        # 
        self.all_detected_cannot_remove = 0
        self.all_detected_can_remove = 0
        # 
        self.last_epoch_detected_cannot_remove = 0
        self.last_epoch_detected_can_remove = 0
        # 
        self.new_cannot_remove = 0
        self.new_can_remove = 0
        # 
        self.epoch_start_elem_num = self.ori_elem_num
        self.cur_epoch_visited_elem_num = 0

    def __str__(self):
        strs = []
        strs.append(f'ori_elem_num: {self.ori_elem_num}')
        strs.append(f'all_detected_cannot_remove: {self.all_detected_cannot_remove}')
        strs.append(f'all_detected_can_remove: {self.all_detected_can_remove}')
        strs.append(f'last_epoch_detected_cannot_remove: {self.last_epoch_detected_cannot_remove}')
        strs.append(f'last_epoch_detected_can_remove: {self.last_epoch_detected_can_remove}')
        strs.append(f'new_cannot_remove: {self.new_cannot_remove}')
        strs.append(f'new_can_remove: {self.new_can_remove}')
        strs.append(f'epoch_start_elem_num: {self.epoch_start_elem_num}')
        strs.append(f'cur_epoch_visited_elem_num: {self.cur_epoch_visited_elem_num}')
        return '\n'.join(strs)

    def reset_last_epoch_info(self, p_dict):
        self.last_epoch_detected_cannot_remove = 0
        self.last_epoch_detected_can_remove = 0
        self.cur_epoch_visited_elem_num = 0
        self.considered_elems = set(_get_undetermined_elems(p_dict))
        self.epoch_start_elem_num = len(self.considered_elems)
        # if len(self.considered_elems) > 200:
        #     print('Reset considered_elems length: ', len(self.considered_elems))
        # else:
        #     print('After reset considered_elems: ', self.considered_elems)

    def update_one_step_info(self, p_dict, deleteconfig):
        cur_all_detected_cannot_remove = sum([v>=1 for v in p_dict.values()])
        cur_all_detected_can_remove = sum([v==0 for v in p_dict.values()])
        self.new_cannot_remove = cur_all_detected_cannot_remove - self.all_detected_cannot_remove
        self.new_can_remove = cur_all_detected_can_remove - self.all_detected_can_remove
        self.all_detected_cannot_remove = cur_all_detected_cannot_remove
        self.all_detected_can_remove = cur_all_detected_can_remove
        # 
        self.last_epoch_detected_can_remove += self.new_can_remove
        self.last_epoch_detected_cannot_remove += self.new_cannot_remove
        # 
        self.considered_elems = self.considered_elems.intersection(set(_get_undetermined_elems(p_dict))) - set(deleteconfig)
        # print('In  update_one_step_info self.considered_elems length: ', len(self.considered_elems))
        # 
        self.cur_epoch_visited_elem_num += len(deleteconfig)
        assert self.cur_epoch_visited_elem_num <= self.epoch_start_elem_num, print(f'self.cur_epoch_visited_elem_num: {self.cur_epoch_visited_elem_num}, self.epoch_start_elem_num: {self.epoch_start_elem_num}')

    @property
    def last_epoch_has_removal(self):
        return self.last_epoch_detected_cannot_remove > 0 or self.last_epoch_detected_can_remove > 0

    @property
    def has_sample_all(self):
        return len(self.considered_elems) == 0

    @property
    def visited_elem_num(self):
        return self.cur_epoch_visited_elem_num
    
    @property
    def rest_num(self):
        return self.ori_elem_num - self.all_detected_can_remove - self.all_detected_cannot_remove

def _get_undetermined_elems(p:collections.OrderedDict):
    return [elem for elem, _prob in p.items() if _prob > 1e-6 and _prob < 1]

class Emulator:
    def __init__(self, init_num:int, p0:float, visited_cannot_remove=0):
        self.exp_cannot_remove = init_num * p0 - visited_cannot_remove
        self.total_num = init_num - visited_cannot_remove
        self.p0 = p0

    @classmethod
    def from_direct_num(cls, total_num:int, cannot_remove_num:float):
        cannot_remove_num = max(cannot_remove_num, 1)
        if total_num == 0:
            p0 = 0
        else:
            p0 = cannot_remove_num / total_num
        return cls(total_num, p0)

class BetaEmulator:
    def __init__(self, alpha:float=1, beta:float=1):
        self.alpha = alpha
        self.beta = beta

    def __str__(self):
        return f'BetaEmulator(alpha={self.alpha}, beta={self.beta})'

    def get_p0(self):
        return self.alpha / (self.alpha + self.beta)

    def reset_with_num(self, considered_cannot_remove, considered_total_num):
        # print('Before reset alpha beta: ', self.alpha, self.beta)
        # print('considered_cannot_remove: ', considered_cannot_remove, 'considered_total_num: ', considered_total_num)
        assert considered_total_num > 0
        self.alpha = max(considered_cannot_remove, 1)
        self.beta = max(considered_total_num - considered_cannot_remove, 1)

    def reset_with_p0(self, p0:float):
        self.alpha = self._resize(p0)
        self.beta = self._resize(1 - p0)

    def _resize(self, num:float):
        return max(2 * num, 1)
