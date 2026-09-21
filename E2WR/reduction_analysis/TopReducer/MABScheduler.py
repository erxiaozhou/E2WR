from random import choice
import numpy as np
import math
from util.util import AbstractMethodException


class Selector:
    def __init__(self, actions, *args, **kwds):
        self.actions = actions

    def choose_action(self, *args, **kwads):
        raise AbstractMethodException

    def update_inner_info(self, *args, **kwads):
        raise AbstractMethodException


class MABScheduler(Selector):
    def __init__(self, actions, alpha=0.9, *args, **kwds):
        self.action_num = len(actions)
        self.values = np.zeros(self.action_num, dtype=np.float32)
        self.counts = np.zeros(self.action_num, dtype=np.float32)
        self.squares = np.zeros(self.action_num, dtype=np.float32)
        self.total_counts = 0
        self.alpha = alpha  
        self.start_times = 1
        super(MABScheduler, self).__init__(actions, *args, **kwds)

    def _pass_warm_up(self):
        if len([idx for idx in range(len(self.actions)) if self.counts[idx] < self.start_times]) == 0:
            return True
        return False

    def choose_action(self, posible_action_idxs:list[int]):
        candi_action_idxs = [idx for idx in range(len(self.actions)) if self.counts[idx] < self.start_times]
        candi_action_idxs = [idx for idx in posible_action_idxs if idx in candi_action_idxs]
        if len(candi_action_idxs) > 0:
            action_index = choice(candi_action_idxs)
            action = self.actions[action_index]
            return action, action_index

        epsilon = 0.3  
        if np.random.random() < epsilon:
            action_index = choice(posible_action_idxs)
        else:
            possible_values = [(i, self.values[i]) for i in posible_action_idxs]
            action_index = max(possible_values, key=lambda x: x[1])[0]

        action = self.actions[action_index]
        return action, action_index

    @property
    def scores(self):
        return self.values

    def update_inner_info(self, reward, action_index):
        self.counts[action_index] += 1
        self.total_counts += 1
        print('UNDATE VALUES !!!')
        if self.counts[action_index] == 1:
            self.values[action_index] = reward
        else:
            self.values[action_index] = self.alpha * reward + (1 - self.alpha) * self.values[action_index]
