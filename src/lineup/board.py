"""Board geometry: sizing, the free centre, and bingo-line detection."""

from __future__ import annotations

from functools import lru_cache

SUPPORTED_SIZES: tuple[int, ...] = (3, 4, 5)


def supports_free_centre(size: int) -> bool:
    """Only odd boards have a middle square to give away."""
    return size % 2 == 1


def free_index(size: int, free_centre: bool) -> int | None:
    """Flat index of the FREE square, or None when the board doesn't have one."""
    if not (free_centre and supports_free_centre(size)):
        return None
    mid = size // 2
    return mid * size + mid


def prompts_required(size: int, free_centre: bool) -> int:
    """How many distinct prompts one board of this shape consumes."""
    return size * size - (1 if free_index(size, free_centre) is not None else 0)


def largest_size_for(prompt_count: int, free_centre: bool) -> int | None:
    """Biggest supported board that `prompt_count` distinct prompts can fill."""
    for size in sorted(SUPPORTED_SIZES, reverse=True):
        if prompt_count >= prompts_required(size, free_centre):
            return size
    return None


@lru_cache(maxsize=len(SUPPORTED_SIZES))
def lines(size: int) -> tuple[tuple[int, ...], ...]:
    """Every winning line — rows, columns, both diagonals — as flat indices."""
    rows = [tuple(r * size + c for c in range(size)) for r in range(size)]
    cols = [tuple(r * size + c for r in range(size)) for c in range(size)]
    diagonals = [
        tuple(i * size + i for i in range(size)),
        tuple(i * size + (size - 1 - i) for i in range(size)),
    ]
    return tuple(rows + cols + diagonals)


def completed_lines(filled: list[bool], size: int) -> list[list[int]]:
    """Winning lines that are fully filled, as lists of flat indices."""
    return [list(line) for line in lines(size) if all(filled[i] for i in line)]
