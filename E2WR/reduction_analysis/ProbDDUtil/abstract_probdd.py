# Copyright (c) 2016-2020 Renata Hodovan, Akos Kiss.
#
# Licensed under the BSD 3-Clause License
# <LICENSE.rst or https://opensource.org/licenses/BSD-3-Clause>.
# This file may not be copied, modified, or distributed except
# according to those terms.

import logging
from random import shuffle
import time
from .InnerInfo import InnerInfo, Emulator, BetaEmulator
from .InferP0History import InferP0History
import collections
import math
from typing import Optional, Any
from .update_p0_util import should_update_p0_bak3, update_global_probability_v2
from .sample_util import entropy_based_sampling
from file_util import get_logger
from itertools import combinations
from .outcome_cache import OutcomeCache
from .cal_ieg2m_util import cal_actual_ig, cal_ieg2m
DEBUG_DD = False

logger = logging.getLogger(__name__)

class OneCallLogInfo:
    def __init__(self, task_id:Optional[str]):
        self.task_id = task_id
        self.run = 0
        self.pass_times = 0
        self.fail_times = 0
        self.input_size = 0
        self.output_size = 0

    def __str__(self):
        num_per_run = self.input_size / self.run if self.run > 0 else 0
        if self.task_id is None:
            id_repr = ''
        else:
            id_repr = f'task_id={self.task_id}, '
        # 
        ratio = self.output_size / self.input_size if self.input_size > 0 else 0
        # 
        return f"{id_repr}run={self.run}, P={self.pass_times}, F={self.fail_times}, IN={self.input_size}, OUT={self.output_size}, PR={num_per_run:.2f}, R={ratio:.2f}"
    def set_input_size(self, input_size:int):
        self.input_size = input_size
    def set_output_size(self, output_size:int):
        self.output_size = output_size

    def add_one_pass(self):
        self.run += 1
        self.pass_times += 1
    def add_one_fail(self):
        self.run += 1
        self.fail_times += 1
        
    # def set_pass_times(self, pass_times:int):
    #     self.pass_times = pass_times
    # def set_fail_times(self, fail_times:int):
    # def __repr__(self):
    #     return self.__str__()

class AbstractProbDD(object):
    """
    Abstract super-class of the parallel and non-parallel DD classes.
    """

    # Test outcomes.
    PASS = 'PASS'
    FAIL = 'FAIL'

    def __init__(self, test, split, cache=None, id_prefix=(), initialP=0.1, ig_sample=False, update_p0=False,given_inip:Optional[dict[int,float]]=None, logger:Optional[logging.Logger]=None, task_id:Optional[str]=None):
        """
        Initialise an abstract DD class. Not to be called directly, only by
        super calls in subclass initializers.
        :param test: A callable tester object.
        :param split: Splitter method to break a configuration up to n parts.
        :param cache: Cache object to use.
        :param id_prefix: Tuple to prepend to config IDs during tests.
        """
        self._test = test
        self._split = split
        self._cache = cache or OutcomeCache()
        self._id_prefix = id_prefix
        self.p = collections.OrderedDict()
        self.memory = {}
        self.testHistory = []
        self.threshold = 0.8
        self.initialP = max(min(self.threshold-0.001, initialP), 0.001)
        self.passconfig = []
        self.ig_sample = ig_sample
        self.update_p0 = update_p0
        self.given_inip = given_inip

        self.beta_emulator = BetaEmulator()
        
        self.fail_history: list[tuple[int, frozenset[Any]]] = [] 
        self.p0 = self.initialP
        self.max_update_p0_times = 20000000000
        # 
        if logger is None:
            logger = get_logger('remove_num_logger', log_file_name='tt/remove_num.log')
        self.logger = logger
        self.task_id = task_id

    def __call__(self, config, weights:Optional[dict[int,float]]=None, expected_end_time:Optional[float]=None):
        """
        Return a 1-minimal failing subset of the initial configuration.
        :param config: The initial configuration that will be reduced.
        :return: 1-minimal failing configuration.
        """
        start_time = time.time()
        one_call_info = OneCallLogInfo(task_id=self.task_id)
        one_call_info.set_input_size(len(config))
        for idx, c in enumerate(config):
            if self.given_inip is not None and c in self.given_inip:
                self.p[c] = self.given_inip[c]
            else:
                self.p[c] = self.initialP
        update_p0_times = 0
        # 
        large_enough = len(config) >= 10
        large_enough = True
        # 
        
        inner_info = InnerInfo(self.p)
        run = 0
        test_in_main = 0
        can_update = True
        self.passconfig = config
        # print("probdd_sample process: ")
        # print(config)
        infer_p0_history = InferP0History()
        emulator = Emulator(inner_info.epoch_start_elem_num, self.p0)
        self.p_before_update = self.p.copy()
        total_info = 0
        new_p0 = self.p0
        while not self._test_done():
            if expected_end_time is not None:
                cur_time = time.time()
                if cur_time >= expected_end_time:
                    print('Time is up, stop testing.')
                    break
            run += 1
            print("run time: "+str(run))
            # print('determined_elem_num: ', _get_determined_elem_num(self.p))
            # print(f"prob: {self.p}")
            if DEBUG_DD:
                if len(self.p) <= 20:
                    print(f"prob: {self.p}")
                else:
                    print(f"prob size: {len(self.p)}")
            # print(self.p)
            assert not self.ig_sample
            if self.ig_sample:
                deleteconfig = self.sample2()
            else:
                deleteconfig = self.sample(weights=weights)
            
            if self.update_p0 and large_enough and len(deleteconfig) + inner_info.cur_epoch_visited_elem_num >= inner_info.epoch_start_elem_num:
                print('HAS SAMPLE ALL')
                # print('len(deleteconfig): ',len(deleteconfig))
                # print('inner_info.cur_epoch_visited_elem_num: ', inner_info.cur_epoch_visited_elem_num)
                # print('inner_info.epoch_start_elem_num: ', inner_info.epoch_start_elem_num)
                # print('inner_info.rest_num: ', inner_info.rest_num)
                print('new_p0: ', new_p0)
                # print('inner_info', inner_info)
                emulator = Emulator.from_direct_num(inner_info.rest_num, inner_info.epoch_start_elem_num * new_p0 - inner_info.last_epoch_detected_cannot_remove)
                infer_p0_history.reset_new_update_p0_history()

                self.beta_emulator.reset_with_num(emulator.exp_cannot_remove, inner_info.rest_num)
                inner_info.reset_last_epoch_info(self.p)
                self.p_before_update = self.p.copy()
            # 
            # if len(deleteconfig) >= 10:
            #     print('Large deletion size: ', len(deleteconfig))
            # else:
            #     print('ZZ deleteconfig', deleteconfig)
            #     if weights is not None:
            #         print('Delet weights: ', {k:weights[k] for k in deleteconfig})
            config2test = self._minus(self.passconfig,deleteconfig)
            config_id = ('r%d' % run, 's%d' % test_in_main)
            outcome = self._lookup_history(config2test)

            if outcome is None:
                outcome = self._test_config(config2test,config_id)
            test_in_main += 1
            # cur_step_gain = cal_ieg2m(set(deleteconfig),self.testHistory, self.p, self._get_d_update_deleteconfig)
            cur_step_gain_actual = cal_actual_ig(set(deleteconfig),self.testHistory, self.p, self._get_d_update_deleteconfig, outcome == self.PASS)
            total_info += cur_step_gain_actual
            # self.logger.info(f'[{run}] [{outcome}] : {len(deleteconfig)}; SUM: {sum(self.p.values()):.4f}; PASS_PROB: {cal_pass_prob(self.p, deleteconfig)-0.5:.4f}, ENTROPY: {cur_step_gain:.4f} TOTAL: {total_info:.4f} ACTUAL: {cur_step_gain_actual:.4f}')
            if outcome == self.FAIL:
                one_call_info.add_one_fail()
                print("test failed\n")
                self._update_probabilities_on_fail(deleteconfig, config2test=config2test, d=self.p)
                self.testHistory.append(deleteconfig)
                if len(deleteconfig) == 1:
                    # print(str(deleteconfig[0]) + " must preserve\n")
                    self.p[deleteconfig[0]] = 1
                
            else:
                one_call_info.add_one_pass()
                print("test passed\n")
                for key in self.p.keys():
                    if key not in config2test:
                        self.p[key] = 0
                deleteconfig = self._minus(self.passconfig, config2test)
                self._process(deleteconfig,self.PASS)
                self.passconfig = config2test
            assert self.p0< self.threshold
            if self.update_p0 and large_enough and ( self._get_all_non_determined_elem_num()>10) and not self._test_done():
                inner_info.update_one_step_info(self.p, deleteconfig)
                test_result = 1 if outcome == self.FAIL else 0
                infer_p0_history.add_one_log(deleteconfig, test_result)

                new_p0 = update_global_probability_v2(infer_p0_history.new_update_p0_history, self.beta_emulator.alpha, self.beta_emulator.beta)
                new_p0 = max(min(new_p0, self.threshold-0.0001), 0.001)
                # print('===================')
                # print('Before update p0, beta_emulator: ', self.beta_emulator, 'infered new_p0: ', new_p0, 'existing p0: ', self.p0)
                # print('===================')
                
                if new_p0 != self.p0:
                    # print('Determine whether update p0')
                    # print('inner_info: ', inner_info)
                    
                    need_update_p0_v3 = should_update_p0_bak3(
                        infer_p0_history.new_update_p0_history, 
                        self.p0, 
                        new_p0
                        )
                    need_update_p0 = need_update_p0_v3
                    # print('Should update p0: ', need_update_p0)
                    # print('Should update p0 v3: ', need_update_p0_v3)
                    # if need_update_p0_v3:
                    #     print('current self.p_before_update', self.p_before_update)
                    if need_update_p0 and update_p0_times < self.max_update_p0_times :
                    # if need_update_p0 and False:
                        print('Before update p0, beta_emulator: ', self.beta_emulator, 'infered new_p0: ', new_p0, 'existing p0: ', self.p0)
                        print('Will update P0')
                        # print(f"Raw prob: {self.p}")
                        # self.logger.info(f'[{run}] Will update P0')
                        self.beta_emulator.reset_with_p0(new_p0)
                        emulator = Emulator(
                            inner_info.epoch_start_elem_num, 
                            new_p0, 
                            inner_info.last_epoch_detected_cannot_remove
                        )
                        # print('ori new_p0: ', new_p0)
                        self.p0 = new_p0
                        infer_p0_history.update_p0_using_history(self.p0, self.p)
                        self.p_before_update = self.p.copy()
                        # print('determined_elem_num: ', _get_determined_elem_num(self.p))
                        # print("prob after update:")
                        # print(self.p)
                        update_p0_times += 1
                        print(f'ori new_p0: {new_p0} P0 updated to: {self.p0:.4f}')
            self.memory[str(config2test)] = outcome
        print(f'{run} times test')
        # print(self.passconfig)
        print('Update p0 times: ', update_p0_times)
        # print('Actual update_p0_times: ', update_p0_times)
        test_cost = time.time() - start_time
        # print('Time cost: ', time.time() - start_time)
        one_call_info.set_output_size(len(self.passconfig))
        # one_call_info.set_pass_times(update_p0_times)
        self.logger.info(f'{one_call_info} Time cost: {test_cost:.4f}')
        return self.passconfig

    def _update_probabilities_on_fail(self, deleteconfig, config2test, d):
        for key in d.keys():
            if key not in config2test and d[key] != 0 and d[key] != 1:
                delta = (self.computRatio(deleteconfig, d) - 1) * d[key]
                # print("prob = " + str(self.p[key]) + ". By theory, increase delta: " + str(delta))
                d[key] = d[key] + delta

    def _get_d_update_deleteconfig(self, deleteconfig, d):
        d_update = d.copy()
        for k in d_update.keys():
            if k in deleteconfig and d[k] != 0 and d[k] != 1:
                d_update[k] = d[k] * self.computRatio(deleteconfig, d)
        return d_update

    def computRatio(self, deleteconfig, p):
        res = 0
        tmplog = 1
        for delc in deleteconfig:
            if p[delc] > 0 and p[delc] < 1:
                tmplog *= (1 - p[delc])
        res = 1 / (1 - tmplog)
        return res

    def _processElementToPreserve(toBePreserve):
        raise NotImplementedError()

    def _process(self, config, outcome):
        raise NotImplementedError()

    def f(self,x):
        return min(x,1)

    def sample(self, weights:Optional[dict[int,float]]=None):
        # weights = None
        config2test = []
        p_items = list(self.p.items())
        # shuffle(p_items)
        # Sort by probability (ascending), then by weight (descending) when probabilities are equal
        if weights is not None:
            self.p = collections.OrderedDict(sorted(p_items, key=lambda item: (item[1], -weights.get(item[0], 0))))
        else:
            self.p = collections.OrderedDict(sorted(p_items, key=lambda item:item[1]))
        # print('self.p after sorting: ',[v for v in self.p.items()][:200])
        # print('weights: ', weights)
        # print('len(self.p): ', len(self.p))
        # print('len(self.weights): ', len(weights) if weights is not None else 0)
        # if weights is not None:
        #     print('weights: ',  [-weights.get(item[0], 0) for item in p_items][:500])
        k = 0
        tmp = 1
        last = 0
        keylist = list(self.p.keys())
        i = 0
        while i < len(self.p):
            if self.p[keylist[i]] == 0 :
                k = k + 1
                i = i + 1
                continue
            if not self.p[keylist[i]] < 1 :
                # raise Exception("Error: prob should be less than 1")
                break
            for j in range(k,i+1):
                tmp *= (1 - self.p[keylist[j]])
            tmp *= (i - k + 1)
            # print("prob = " + str(self.p[keylist[i]]) + "; tmp = " + str(tmp) + "; last = " + str(last))
            if tmp < last:
                break
            last = tmp
            tmp = 1
            i = i + 1
        while i > k:
            i = i - 1
            config2test.append(keylist[i])
        # print("selected deletion size: " + str(len(config2test)))
        # for elm in config2test:
        #     print(self.p[elm],)
        # print("\n")
        # print(f'config2test: {config2test}')
        # print('selected weights: ', {k:weights[k] for k in config2test} if weights is not None else 'No weights')
        # print('selected ps: ', {k:self.p[k] for k in config2test})
        # input('XXXXXXXXXX')
        return config2test


    def sample2(self, considered_elems:Optional[set[int]]=None):
      
        if considered_elems is not None:
            assert len(considered_elems) > 0
        selected_elements = entropy_based_sampling(dict(self.p), considered_elems=considered_elems)
        
        return selected_elements


    def _test_done(self):
        tmp = list(set(self.p.values()))
        alldecided = (tmp == [0,1] or tmp == [0] or tmp == [1])
        if alldecided:
            print("Iteration needs to stop because all elements are decided.")
            return True
        for key in self.p.keys():
            if self.f(self.p[key])<self.threshold:
                return False
        print("Iteration needs to stop because of convergence.")
        return True

    def _get_all_non_determined_elem_num(self):
        r_ = 0
        for elem, _prob in self.p.items():
            if _prob >= self.threshold:
                r_ += 1
            elif _prob < 1e-6:
                r_ += 1
        return len(self.p) - r_

    def _lookup_history(self, config):
        if str(config) in self.memory:
            return self.memory[str(config)]
        return None

    def _lookup_cache(self, config, config_id):
        """
        Perform a cache lookup if caching is enabled.
        :param config: The configuration we are looking for.
        :param config_id: The ID describing the configuration (only for debug
            message).
        :return: None if outcome is not found for config in cache or if caching
            is disabled, PASS or FAIL otherwise.
        """
        cached_result = self._cache.lookup(config)
        # if cached_result is not None:
        #     logger.debug('\t[ %s ]: cache = %r', self._pretty_config_id(self._id_prefix + config_id), cached_result)

        return cached_result

    def _test_config(self, config, config_id):
        """
        Test a single configuration and save the result in cache.
        :param config: The current configuration to test.
        :param config_id: Unique ID that will be used to save tests to easily
            identifiable directories.
        :return: PASS or FAIL
        """
        config_id = self._id_prefix + config_id

        # logger.debug('\t[ %s ]: test...', self._pretty_config_id(config_id))
        outcome = self._test(config, config_id)
        # logger.debug('\t[ %s ]: test = %r', self._pretty_config_id(config_id), outcome)

        if 'assert' not in config_id:
            self._cache.add(config, outcome)

        return outcome

    @staticmethod
    def _pretty_config_id(config_id):
        """
        Create beautified identifier for the current task from the argument.
        The argument is typically a tuple in the form of ('rN', 'DM'), where N
        is the index of the current iteration, D is direction of reduce (either
        s(ubset) or c(omplement)), and M is the index of the current test in the
        iteration. Alternatively, argument can also be in the form of
        (rN, 'assert') for double checking the input at the start of an
        iteration.
        :param config_id: Config ID tuple.
        :return: Concatenating the arguments with slashes, e.g., "rN / DM".
        """
        return ' / '.join(str(i) for i in config_id)

    @staticmethod
    def _minus(c1, c2):
        """
        Return a list of all elements of C1 that are not in C2.
        """
        c2 = set(c2)
        return [c for c in c1 if c not in c2]
    
    @staticmethod
    def _aInb(c1,c2):
        for i in c1:
            if i not in c2:
                return False
        return True

    @staticmethod
    def _intersect(c1,c2):
        for i in c1:
            if i in c2:
                return True
        return False


def _get_determined_elem_num(p:collections.OrderedDict):
    r_ = 0
    for elem, _prob in p.items():
        if _prob >= 1:
            r_ += 1
        elif _prob < 1e-6:
            r_ += 1
    return r_


def cal_pass_prob(p_dict, elems):
    p = 1
    for elem in elems:
        p *= (1 - p_dict[elem])
    return p


def get_subsets_minus_one(input_set: set):
    if len(input_set) <= 1:
        return []
    
    target_length = len(input_set) - 1
    return [set(combo) for combo in combinations(input_set, target_length)]


def cal_entropy(p_dict, elems):
    p = cal_pass_prob(p_dict, elems)
    return -p * math.log2(p) - (1 - p) * math.log2(1 - p)