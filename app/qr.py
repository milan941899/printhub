"""Kleiner QR-Code-Erzeuger (Byte-Modus, Fehlerkorrektur M, Version 1–10) – ohne Zusatzpakete.
Nach dem bekannten Verfahren aus ISO/IEC 18004 (angelehnt an Nayukis qrcodegen)."""

ECC_PER_BLOCK = [None, 10, 16, 26, 18, 24, 16, 18, 22, 22, 26]   # Stufe M
NUM_BLOCKS = [None, 1, 1, 1, 2, 2, 4, 4, 4, 5, 5]
FORMAT_M = 0  # Formatbits für M sind 00


def _raw_modules(ver):
    r = (16 * ver + 128) * ver + 64
    if ver >= 2:
        n = ver // 7 + 2
        r -= (25 * n - 10) * n - 55
        if ver >= 7:
            r -= 36
    return r


def _data_codewords(ver):
    return _raw_modules(ver) // 8 - ECC_PER_BLOCK[ver] * NUM_BLOCKS[ver]


def _gf_mul(x, y):
    z = 0
    for i in reversed(range(8)):
        z = (z << 1) ^ ((z >> 7) * 0x11D)
        z ^= ((y >> i) & 1) * x
    return z & 0xFF


def _rs_divisor(degree):
    res = [0] * (degree - 1) + [1]
    root = 1
    for _ in range(degree):
        for j in range(degree):
            res[j] = _gf_mul(res[j], root)
            if j + 1 < degree:
                res[j] ^= res[j + 1]
        root = _gf_mul(root, 0x02)
    return res


def _rs_remainder(data, div):
    res = [0] * len(div)
    for b in data:
        f = b ^ res.pop(0)
        res.append(0)
        for i, c in enumerate(div):
            res[i] ^= _gf_mul(c, f)
    return res


def _align_positions(ver):
    if ver == 1:
        return []
    n = ver // 7 + 2
    step = (ver * 8 + n * 3 + 5) // (n * 4 - 4) * 2
    res = [ver * 4 + 10 - i * step for i in range(n - 1)] + [6]
    return list(reversed(res))


def _bch_format(mask):
    data = (FORMAT_M << 3) | mask
    rem = data
    for _ in range(10):
        rem = (rem << 1) ^ ((rem >> 9) * 0x537)
    return ((data << 10) | rem) ^ 0x5412


def _bch_version(ver):
    rem = ver
    for _ in range(12):
        rem = (rem << 1) ^ ((rem >> 11) * 0x1F25)
    return (ver << 12) | rem


MASKS = [
    lambda x, y: (x + y) % 2 == 0, lambda x, y: y % 2 == 0, lambda x, y: x % 3 == 0,
    lambda x, y: (x + y) % 3 == 0, lambda x, y: (x // 3 + y // 2) % 2 == 0,
    lambda x, y: x * y % 2 + x * y % 3 == 0, lambda x, y: (x * y % 2 + x * y % 3) % 2 == 0,
    lambda x, y: ((x + y) % 2 + x * y % 3) % 2 == 0,
]


class QR:
    def __init__(self, text):
        data = text.encode("utf-8")
        for ver in range(1, 11):
            cc = 8 if ver < 10 else 16
            if 4 + cc + len(data) * 8 <= _data_codewords(ver) * 8:
                break
        else:
            raise ValueError("Text zu lang für einen kleinen QR-Code")
        self.ver = ver
        self.size = ver * 4 + 17
        cap = _data_codewords(ver) * 8
        bits = [0, 1, 0, 0] + [(len(data) >> i) & 1 for i in reversed(range(cc))]
        for b in data:
            bits += [(b >> i) & 1 for i in reversed(range(8))]
        bits += [0] * min(4, cap - len(bits))
        bits += [0] * (-len(bits) % 8)
        pad = 0xEC
        while len(bits) < cap:
            bits += [(pad >> i) & 1 for i in reversed(range(8))]
            pad ^= 0xEC ^ 0x11
        codewords = [int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]
        all_cw = self._add_ecc(codewords)
        n = self.size
        self.mod = [[False] * n for _ in range(n)]
        self.fun = [[False] * n for _ in range(n)]
        self._function_patterns()
        self._draw(all_cw)
        best, best_pen = 0, None
        for m in range(8):
            self._apply_mask(m)
            self._format(m)
            p = self._penalty()
            if best_pen is None or p < best_pen:
                best, best_pen = m, p
            self._apply_mask(m)
        self._apply_mask(best)
        self._format(best)

    def _add_ecc(self, data):
        ver = self.ver
        nb, eb = NUM_BLOCKS[ver], ECC_PER_BLOCK[ver]
        raw = _raw_modules(ver) // 8
        short = nb - raw % nb
        short_len = raw // nb
        div = _rs_divisor(eb)
        blocks, k = [], 0
        for i in range(nb):
            dl = short_len - eb + (0 if i < short else 1)
            d = data[k:k + dl]
            k += dl
            e = _rs_remainder(d, div)
            if i < short:
                d = d + [None]
            blocks.append(d + e)
        out = []
        for i in range(len(blocks[0])):
            for j, b in enumerate(blocks):
                if i != short_len - eb or j >= short:
                    out.append(b[i])
        return out

    def _set(self, x, y, dark):
        self.mod[y][x] = dark
        self.fun[y][x] = True

    def _function_patterns(self):
        n = self.size
        for i in range(n):
            self._set(6, i, i % 2 == 0)
            self._set(i, 6, i % 2 == 0)
        for cx, cy in ((3, 3), (n - 4, 3), (3, n - 4)):
            for dy in range(-4, 5):
                for dx in range(-4, 5):
                    x, y = cx + dx, cy + dy
                    if 0 <= x < n and 0 <= y < n:
                        d = max(abs(dx), abs(dy))
                        self._set(x, y, d not in (2, 4))
        al = _align_positions(self.ver)
        for i, a in enumerate(al):
            for j, b in enumerate(al):
                if (i == 0 and j == 0) or (i == 0 and j == len(al) - 1) or (i == len(al) - 1 and j == 0):
                    continue
                for dy in range(-2, 3):
                    for dx in range(-2, 3):
                        self._set(a + dx, b + dy, max(abs(dx), abs(dy)) != 1)
        self._format(0)
        if self.ver >= 7:
            bits = _bch_version(self.ver)
            for i in range(18):
                bit = (bits >> i) & 1 == 1
                a, b = n - 11 + i % 3, i // 3
                self._set(a, b, bit)
                self._set(b, a, bit)

    def _format(self, mask):
        bits = _bch_format(mask)
        n = self.size
        g = lambda i: (bits >> i) & 1 == 1
        for i in range(6):
            self._set(8, i, g(i))
        self._set(8, 7, g(6))
        self._set(8, 8, g(7))
        self._set(7, 8, g(8))
        for i in range(9, 15):
            self._set(14 - i, 8, g(i))
        for i in range(8):
            self._set(n - 1 - i, 8, g(i))
        for i in range(8, 15):
            self._set(8, n - 15 + i, g(i))
        self._set(8, n - 8, True)

    def _draw(self, data):
        n = self.size
        i = 0
        right = n - 1
        while right >= 1:
            if right == 6:
                right = 5
            for vert in range(n):
                for j in range(2):
                    x = right - j
                    upward = ((right + 1) & 2) == 0
                    y = n - 1 - vert if upward else vert
                    if not self.fun[y][x] and i < len(data) * 8:
                        self.mod[y][x] = (data[i >> 3] >> (7 - (i & 7))) & 1 == 1
                        i += 1
            right -= 2

    def _apply_mask(self, m):
        f = MASKS[m]
        for y in range(self.size):
            for x in range(self.size):
                if not self.fun[y][x] and f(x, y):
                    self.mod[y][x] = not self.mod[y][x]

    def _penalty(self):
        n, m, p = self.size, self.mod, 0
        for lines in (m, list(zip(*m))):
            for row in lines:
                run, last = 0, None
                for v in row:
                    if v == last:
                        run += 1
                        if run == 5:
                            p += 3
                        elif run > 5:
                            p += 1
                    else:
                        run, last = 1, v
        for y in range(n - 1):
            for x in range(n - 1):
                if m[y][x] == m[y][x + 1] == m[y + 1][x] == m[y + 1][x + 1]:
                    p += 3
        dark = sum(sum(r) for r in m)
        k = abs(dark * 20 - n * n * 10) // (n * n)
        return p + k * 10

    def svg(self, border=2, color="#000"):
        n = self.size + border * 2
        path = "".join(f"M{x + border},{y + border}h1v1h-1z" for y in range(self.size)
                       for x in range(self.size) if self.mod[y][x])
        return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {n} {n}" shape-rendering="crispEdges">'
                f'<rect width="100%" height="100%" fill="#fff"/><path d="{path}" fill="{color}"/></svg>')
