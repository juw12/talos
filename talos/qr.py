"""A QR code from a short text, with the standard library only.

Byte mode, error-correction level L, versions 1 to 10: up to 271 bytes, which is more than a link
needs. `matrix(text)` gives rows of True (dark) and False; `svg` and `blocks` draw them.
"""
from __future__ import annotations

# version -> (error-correction codewords per block, blocks, data codewords each, more blocks, data codewords each)
BLOCKS = {1: (7, 1, 19, 0, 0), 2: (10, 1, 34, 0, 0), 3: (15, 1, 55, 0, 0), 4: (20, 1, 80, 0, 0),
          5: (26, 1, 108, 0, 0), 6: (18, 2, 68, 0, 0), 7: (20, 2, 78, 0, 0), 8: (24, 2, 97, 0, 0),
          9: (30, 2, 116, 0, 0), 10: (18, 2, 68, 2, 69)}
ALIGN = {1: [], 2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30], 6: [6, 34], 7: [6, 22, 38],
         8: [6, 24, 42], 9: [6, 26, 46], 10: [6, 28, 50]}
MASKS = (lambda x, y: (x + y) % 2 == 0, lambda x, y: y % 2 == 0, lambda x, y: x % 3 == 0,
         lambda x, y: (x + y) % 3 == 0, lambda x, y: (y // 2 + x // 3) % 2 == 0,
         lambda x, y: x * y % 2 + x * y % 3 == 0, lambda x, y: (x * y % 2 + x * y % 3) % 2 == 0,
         lambda x, y: ((x + y) % 2 + x * y % 3) % 2 == 0)

EXP, LOG = [0] * 510, [0] * 256
_value = 1
for _power in range(255):
    EXP[_power] = EXP[_power + 255] = _value
    LOG[_value] = _power
    _value = (_value << 1) ^ (0x11D if _value & 0x80 else 0)


def _check_words(data: list, count: int) -> list:
    """Reed-Solomon: the `count` codewords that let a reader repair `data`."""
    generator = [1]
    for power in range(count):
        longer = generator + [0]
        for index, factor in enumerate(generator):
            if factor:
                longer[index + 1] ^= EXP[LOG[factor] + power]
        generator = longer
    rest = [0] * count
    for byte in data:
        lead = byte ^ rest[0]
        rest = rest[1:] + [0]
        if lead:
            for index in range(count):
                if generator[index + 1]:
                    rest[index] ^= EXP[LOG[generator[index + 1]] + LOG[lead]]
    return rest


def _codewords(data: bytes, version: int) -> list:
    check, first, size_first, second, size_second = BLOCKS[version]
    room = first * size_first + second * size_second
    bits = "0100" + format(len(data), "016b" if version > 9 else "08b") + "".join(format(byte, "08b") for byte in data)
    bits += "0" * min(4, room * 8 - len(bits))
    bits += "0" * (-len(bits) % 8)
    words = [int(bits[index:index + 8], 2) for index in range(0, len(bits), 8)]
    words += [(0xEC, 0x11)[index % 2] for index in range(room - len(words))]
    blocks, start = [], 0
    for size in [size_first] * first + [size_second] * second:
        blocks.append(words[start:start + size])
        start += size
    checks = [_check_words(block, check) for block in blocks]
    mixed = [block[index] for index in range(max(size_first, size_second)) for block in blocks if index < len(block)]
    return mixed + [words_[index] for index in range(check) for words_ in checks]


def _bch(value: int, bits: int, generator: int) -> int:
    rest = value << bits
    for shift in range(rest.bit_length() - 1, bits - 1, -1):
        if rest >> shift & 1:
            rest ^= generator << (shift - bits)
    return value << bits | rest


def _penalty(grid: list) -> int:
    size, score = len(grid), 0
    lines = grid + [list(column) for column in zip(*grid)]
    for line in lines:
        run = 1
        for index in range(1, size + 1):
            if index < size and line[index] == line[index - 1]:
                run += 1
            else:
                score += run - 2 if run >= 5 else 0
                run = 1
        text = "".join("1" if cell else "0" for cell in line)
        for pattern in ("10111010000", "00001011101"):
            start = text.find(pattern)
            while start >= 0:
                score += 40
                start = text.find(pattern, start + 1)
    for y in range(size - 1):
        for x in range(size - 1):
            if grid[y][x] == grid[y][x + 1] == grid[y + 1][x] == grid[y + 1][x + 1]:
                score += 3
    dark = sum(map(sum, grid))
    return score + 10 * (abs(dark * 20 - size * size * 10) // (size * size))


def matrix(text, mask=None) -> list:
    """The code as rows of booleans, True for a dark square. Raises ValueError when the text is too long."""
    data = text.encode("utf-8") if isinstance(text, str) else bytes(text)
    version = next((number for number, (_, first, size_first, second, size_second) in BLOCKS.items()
                    if len(data) <= first * size_first + second * size_second - (3 if number > 9 else 2)), 0)
    if not version:
        raise ValueError("This text is too long for the QR code Talos can draw (271 bytes).")
    size = 17 + 4 * version
    grid = [[False] * size for _ in range(size)]
    fixed = [[False] * size for _ in range(size)]

    def put(x, y, dark):
        grid[y][x], fixed[y][x] = bool(dark), True

    for left, top in ((0, 0), (size - 7, 0), (0, size - 7)):               # the three corner squares
        for y in range(-1, 8):
            for x in range(-1, 8):
                if 0 <= left + x < size and 0 <= top + y < size:
                    ring = max(abs(x - 3), abs(y - 3))
                    put(left + x, top + y, ring <= 1 or ring == 3)
    for index in range(8, size - 8):                                         # the dotted lines between them
        put(index, 6, index % 2 == 0)
        put(6, index, index % 2 == 0)
    places = ALIGN[version]
    for centre_y in places:                                                  # the small squares
        for centre_x in places:
            if (centre_x, centre_y) in ((6, 6), (6, places[-1]), (places[-1], 6)):
                continue
            for y in range(-2, 3):
                for x in range(-2, 3):
                    put(centre_x + x, centre_y + y, max(abs(x), abs(y)) != 1)
    for index in range(9):                                                   # room for the format bits
        fixed[8][index] = fixed[index][8] = True
    for index in range(1, 9):
        fixed[8][size - index] = fixed[size - index][8] = True
    put(8, size - 8, True)
    if version >= 7:
        number = _bch(version, 12, 0x1F25)
        for index in range(18):
            dark = number >> index & 1
            put(index // 3, size - 11 + index % 3, dark)
            put(size - 11 + index % 3, index // 3, dark)

    bits = [byte >> shift & 1 for byte in _codewords(data, version) for shift in range(7, -1, -1)]
    position, x, upward = 0, size - 1, True
    while x > 0:                                                             # the data, in a zigzag from the bottom right
        if x == 6:
            x -= 1
        for step in range(size):
            y = size - 1 - step if upward else step
            for column in (x, x - 1):
                if not fixed[y][column]:
                    grid[y][column] = bool(position < len(bits) and bits[position])
                    position += 1
        upward, x = not upward, x - 2

    def masked(number):
        rule = MASKS[number]
        made = [[cell != (not fixed[y][x] and rule(x, y)) for x, cell in enumerate(row)] for y, row in enumerate(grid)]
        code = _bch(0b01000 | number, 10, 0x537) ^ 0x5412
        for index in range(15):
            dark = bool(code >> index & 1)
            if index < 6:
                made[index][8] = dark
            elif index < 8:
                made[index + 1][8] = dark
            elif index == 8:
                made[8][7] = dark
            else:
                made[8][14 - index] = dark
            if index < 8:
                made[8][size - 1 - index] = dark
            else:
                made[size - 15 + index][8] = dark
        return made

    if mask is not None:
        return masked(mask)
    return min((masked(number) for number in range(8)), key=_penalty)


def svg(rows: list, scale: int = 8, border: int = 4) -> str:
    size = (len(rows) + 2 * border) * scale
    squares = "".join(f"M{(x + border) * scale},{(y + border) * scale}h{scale}v{scale}h-{scale}z"
                      for y, row in enumerate(rows) for x, dark in enumerate(row) if dark)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}" viewBox="0 0 {size} {size}" '
            f'role="img" aria-label="QR code"><rect width="{size}" height="{size}" fill="#fff"/>'
            f'<path d="{squares}" fill="#000"/></svg>')


def blocks(rows: list, border: int = 2) -> str:
    """The code as text for a terminal: two rows of squares per line, dark on a light ground."""
    width = len(rows) + 2 * border
    empty = [False] * width
    padded = [empty] * border + [[False] * border + list(row) + [False] * border for row in rows] + [empty] * border
    if len(padded) % 2:
        padded.append(empty)
    shapes = {(False, False): "\u2588", (True, True): " ", (True, False): "\u2584", (False, True): "\u2580"}
    return "\n".join("".join(shapes[(upper[x], lower[x])] for x in range(width))
                     for upper, lower in zip(padded[::2], padded[1::2]))
