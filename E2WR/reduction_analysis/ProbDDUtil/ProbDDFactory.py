import logging
from typing import Optional
from .outcome_cache import ConfigCache
from .prob_dd import ProbDD


class ProbDDFactory:
    default_initialP = 0.1
    _use_p0_pred = False
    _summary_logger = None
    tmp_last_p = None

    @staticmethod
    def set_probdd_factory(
        use_p0_pred: bool,
        initialP: float,
        logger: Optional[logging.Logger] = None
    ):
        ProbDDFactory._use_p0_pred = use_p0_pred
        ProbDDFactory.default_initialP = initialP
        ProbDDFactory._summary_logger = logger

    @staticmethod
    def enable_p0_pred():
        ProbDDFactory._use_p0_pred = True

    @staticmethod
    def set_default_initialP(initialP:float):
        ProbDDFactory.default_initialP = initialP

    @staticmethod
    def use_a_initp_temp(tmp_initialP:float):
        ProbDDFactory.tmp_last_p = ProbDDFactory.default_initialP
        ProbDDFactory.default_initialP = tmp_initialP

    @staticmethod
    def reset_initp():
        if ProbDDFactory.tmp_last_p is not None:
            ProbDDFactory.default_initialP = ProbDDFactory.tmp_last_p
            ProbDDFactory.tmp_last_p = None

    @staticmethod
    def set_summary_logger(logger:logging.Logger):
        ProbDDFactory._summary_logger = logger

    @staticmethod
    def get_default_probdd(test, 
                           cache=None,
                           initialP=None,
                           task_id:Optional[str]=None,
                           given_inip:Optional[dict[int,float]]=None
                           ):
        if cache is None:
            cache = ConfigCache()
        if initialP is None:
            initialP = ProbDDFactory.default_initialP
        return ProbDD(
            test=test,
            cache=cache,
            update_p0= ProbDDFactory._use_p0_pred,
            initialP=initialP,
            given_inip=given_inip,
            task_id=task_id,
            logger=ProbDDFactory._summary_logger
        )
    