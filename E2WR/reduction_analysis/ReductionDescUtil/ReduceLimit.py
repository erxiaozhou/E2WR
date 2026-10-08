from typing import Optional


class ReduceLimit:
    def __init__(
        self,
        timeout:Optional[int]=None,
    ):
        self.timeout = timeout

    def limit_is_reached(self, start_time, cur_time, cur_times:int):
        if self.timeout is not None and cur_time - start_time > self.timeout:
            return True
        return False
