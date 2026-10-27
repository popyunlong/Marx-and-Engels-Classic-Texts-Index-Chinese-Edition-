"""Bounded original-text matching for highlights, without a dynamic regex.

Equivalent to literal characters separated by greedy Unicode non-word gaps.
Suffix offsets are computed backwards in O(text * term) time and O(text) space;
there is no recursive/backtracking exploration of punctuation allocations.
"""


def literal_gap_spans(text: str, characters: list[str]) -> list[tuple[int, int]]:
    if not text or not characters or len(characters) > len(text):
        return []
    size = len(text)
    word = [c.isalnum() or c == "_" for c in text]
    ends = [i + 1 if c == characters[-1] else 0 for i, c in enumerate(text)]
    for character in reversed(characters[:-1]):
        following = [0] * (size + 1)
        rightmost = 0
        for i in range(size - 1, -1, -1):
            if word[i]:
                rightmost = ends[i]
            elif not rightmost:
                rightmost = ends[i]
            following[i] = rightmost
        ends = [following[i + 1] if c == character else 0 for i, c in enumerate(text)]
        if not any(ends):
            return []
    spans = []
    cursor = 0
    while cursor < size:
        end = ends[cursor]
        if end:
            spans.append((cursor, end))
            cursor = end
        else:
            cursor += 1
    return spans
