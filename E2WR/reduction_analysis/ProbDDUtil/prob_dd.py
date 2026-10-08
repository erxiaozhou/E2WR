# Copyright (c) 2016-2019 Renata Hodovan, Akos Kiss.
#
# Licensed under the BSD 3-Clause License
# <LICENSE.rst or https://opensource.org/licenses/BSD-3-Clause>.
# This file may not be copied, modified, or distributed except
# according to those terms.

import logging

from .abstract_probdd import AbstractProbDD
from .outcome_cache import ConfigCache
from typing import Optional
logger = logging.getLogger(__name__)


class ProbDD(AbstractProbDD):

    def __init__(self, test, cache=None, id_prefix=(),
                  initialP=0.1, update_p0=False, 
                  given_inip:Optional[dict[int,float]]=None,
                  logger:Optional[logging.Logger]=None,
                  task_id:Optional[str]=None
                  ):

        """
        Initialize a ProbDD object.
        :param test: A callable tester object.
        :param cache: Cache object to use.
        :param id_prefix: Tuple to prepend to config IDs during tests.
        """
        cache = cache or ConfigCache()
        AbstractProbDD.__init__(self, test, cache=cache, id_prefix=id_prefix, initialP=initialP, update_p0=update_p0,  given_inip=given_inip, logger=logger, task_id=task_id)

    def _processElementToPreserve(self,toBePreserve):
        tmp = []
        for history in self.testHistory:
            if self._intersect(toBePreserve, history):
                cha = self._minus(history, toBePreserve)
            else:
                tmp.append(history)
        self.testHistory = tmp
        for elm in toBePreserve:
            self.p[elm] = 1

    def _process(self,config,outcome):
        tmp=[]
        toBePreserve=[]
        if outcome==self.PASS:
            for history in self.testHistory:
                if self._intersect(config,history):
                    cha=self._minus(history,config)
                    if len(cha)==1:
                        if not(cha[0] in toBePreserve):
                            toBePreserve.append(cha[0])
                            # self.logger.info(f'Remove by difference: {cha} history: {history}')
                    else:
                        tmp.append(cha)
                else:
                    tmp.append(history)
            self.testHistory=tmp
            self._processElementToPreserve(toBePreserve)
        elif outcome==self.FAIL:
            self._processElementToPreserve(config)
