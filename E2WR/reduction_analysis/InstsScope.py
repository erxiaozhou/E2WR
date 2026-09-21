class InstsScope:
    def __init__(
        self,
        func_idx:int,
        start_idx:int,
        end_idx:int
    ):
        self.func_idx = func_idx
        self.start_idx = start_idx
        self.end_idx = end_idx

    def __eq__(self, other):
        return self.func_idx == other.func_idx \
            and self.start_idx == other.start_idx \
            and self.end_idx == other.end_idx

    def __hash__(self):
        return hash((self.func_idx, self.start_idx, self.end_idx))

    def __str__(self):
        return f'{self.__class__.__name__}(func_idx={self.func_idx}, start_idx={self.start_idx}, end_idx={self.end_idx})'

    def __repr__(self):
        return self.__str__()

    def get_length(self)->int:
        return self.end_idx - self.start_idx

    def contains(self, other:'InstsScope')->bool:
        return self.func_idx == other.func_idx \
            and self.start_idx <= other.start_idx \
            and self.end_idx >= other.end_idx
