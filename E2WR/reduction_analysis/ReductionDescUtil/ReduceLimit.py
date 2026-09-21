from typing import Optional


class ReduceLimit:
    def __init__(
        self,
        timeout:Optional[int]=None,
        max_passes:Optional[int]=None,
    ):
        self.timeout = timeout
        self.max_passes = max_passes

    def limit_is_reached(self, start_time, cur_time, cur_times:int):
        if self.timeout is not None and cur_time - start_time > self.timeout:
            return True
        if self.max_passes is not None and cur_times >= self.max_passes:
            return True
        return False
