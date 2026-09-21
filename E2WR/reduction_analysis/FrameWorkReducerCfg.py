from dataclasses import dataclass
from typing import Callable, Union
from pathlib import Path


@dataclass(slots=True)
class FrameWorkReducerCfg:
    tmp_dir: Path
    log_dir: Path
    pass_timeout: int
    debug: bool
    use_remove_unused_elem: bool
    common_passes: bool
    use_value_replacement: bool
    use_perses_replace_nodeshrink: bool
    use_final_polish: bool

    def __post_init__(self) -> None:
        # Keep behavior identical: normalize directories to Path.
        self.tmp_dir = Path(self.tmp_dir)
        self.log_dir = Path(self.log_dir)

    @classmethod
    def default_cfg(cls, 
                    tmp_dir: Union[str, Path], 
        log_dir: Union[str, Path],
        pass_timeout:int,
    ) -> 'FrameWorkReducerCfg':
        return cls(
            tmp_dir=Path(tmp_dir),
            log_dir=Path(log_dir),
            pass_timeout=pass_timeout,
            use_remove_unused_elem=True,
            use_final_polish=True,
            # 
            debug=False,
            common_passes=False,
            use_value_replacement=False,
            use_perses_replace_nodeshrink=False
        )
