from __future__ import annotations

from typing import Iterable, List, Optional, Sequence, TypeVar

T = TypeVar("T")


def _iter_subsequence_positions(
    needle: Sequence[T],
    haystack: Sequence[T],
    *,
    start_idx: int = 0,
    needle_idx: int = 0,
    prefix: tuple[int, ...] = (),
) -> Iterable[List[int]]:
    if needle_idx >= len(needle):
        yield list(prefix)
        return

    target = needle[needle_idx]
    for idx in range(start_idx, len(haystack)):
        if haystack[idx] != target:
            continue
        yield from _iter_subsequence_positions(
            needle,
            haystack,
            start_idx=idx + 1,
            needle_idx=needle_idx + 1,
            prefix=prefix + (idx,),
        )


def find_subsequence_positions(
    needle: Sequence[T],
    haystack: Sequence[T],
    *,
    max_results: Optional[int] = None,
) -> List[List[int]]:
    if max_results is not None and max_results <= 0:
        return []

    out: List[List[int]] = []
    for positions in _iter_subsequence_positions(needle, haystack):
        out.append(positions)
        if max_results is not None and len(out) >= max_results:
            break
    return out

