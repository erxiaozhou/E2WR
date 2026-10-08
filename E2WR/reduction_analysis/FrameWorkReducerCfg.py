from dataclasses import dataclass
from typing import Callable, Union
from pathlib import Path


@dataclass(slots=True)
class FrameWorkReducerCfg:
    tmp_dir: Path
    log_dir: Path
    debug: bool
    use_remove_unused_elem: bool
    use_final_polish: bool

    def __post_init__(self) -> None:
        # Keep behavior identical: normalize directories to Path.
        self.tmp_dir = Path(self.tmp_dir)
        self.log_dir = Path(self.log_dir)

    @classmethod
    def default_cfg(cls,
                    tmp_dir: Union[str, Path],
        log_dir: Union[str, Path],
    ) -> 'FrameWorkReducerCfg':
        return cls(
            tmp_dir=Path(tmp_dir),
            log_dir=Path(log_dir),
            use_remove_unused_elem=True,
            use_final_polish=True,
            #
            debug=False,
        )
