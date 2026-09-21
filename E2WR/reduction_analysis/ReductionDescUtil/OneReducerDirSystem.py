from file_util import check_dir
from pathlib import Path
from typing import Union, Optional


class OneReducerDirSystem:
    def __init__(
        self,
        tmp_used_path: Union[str, Path],
        tmp_dir: Union[str, Path, None] = None,
        log_path: Union[str, Path, None] = None
    ):
        if tmp_dir is None:
            self._tmp_dir = None
        else:
            self._tmp_dir = check_dir(tmp_dir)
        if log_path is not None:
            log_path = str(log_path)
        self.log_path = log_path
        self.tmp_used_path = str(tmp_used_path)
        self.default_instrumented_path = self.tmp_dir / 'instrumented.wasm' 

    @property
    def tmp_dir(self) -> Path:
        assert self._tmp_dir is not None
        return self._tmp_dir

    def get_default_log_path(self, pass_name: str) -> str:
        if self.log_path is not None:
            return self.log_path
        if self.tmp_dir is None:
            raise ValueError('Both log_path and tmp_dir are None')
        return str(Path(self.tmp_dir) / f'{pass_name}.log')

    @classmethod
    def from_framework_dir(
        cls,
        work_dir: Path,
        name: str,
        tmp_used_path: Union[str, Path, None] = None,
        log_path: Optional[Union[str, Path]] = None,
    ):
        work_dir = work_dir/name
        if tmp_used_path is None:
            tmp_used_path = work_dir / 'tmp_used.wasm'
        return cls(
            tmp_dir=work_dir,
            tmp_used_path=tmp_used_path,
            log_path=log_path
        )
