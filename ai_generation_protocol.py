"""Control tokens are not prose and must not be interpreted inside quotations/code."""
import re

MARKER = '<section_complete/>'


def section_end(text, marker=MARKER):
    fence, inline, quotes, offset = None, 0, [], 0
    pairs = {'“': '”', '「': '」', '『': '』', '"': '"', '[': ']', '(': ')'}
    for line in text.splitlines(keepends=True):
        stripped = line.lstrip()
        fence_match = re.match(r'(`{3,}|~{3,})', stripped)
        if fence_match and not quotes and not inline:
            token = fence_match[1]
            if fence is None:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence):
                fence = None
            offset += len(line)
            continue
        if fence or stripped.startswith('>'):
            offset += len(line)
            continue
        i = 0
        while i < len(line):
            char = line[i]
            if char == '\\':
                i += 2
                continue
            if char == '`':
                n = len(line[i:]) - len(line[i:].lstrip('`'))
                if not inline:
                    inline = n
                elif inline == n:
                    inline = 0
                i += n
                continue
            if not inline:
                if not quotes and line.startswith(marker, i):
                    return offset + i
                if quotes and char == quotes[-1]:
                    quotes.pop()
                elif char in pairs:
                    quotes.append(pairs[char])
            i += 1
        offset += len(line)
    return -1
