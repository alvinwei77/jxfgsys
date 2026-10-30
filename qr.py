"""极简二维码生成（手机访问地址用）。只支持字节模式 + M 级纠错、版本 1~10（最多 213 字节，网址足够）。
算法按 QR 标准（ISO/IEC 18004）实现，参照 Nayuki 的开源实现思路；不依赖任何第三方库，离线可用。
用法：qr.svg("http://192.168.1.5:5000/m") → SVG 字符串。"""

# M 级纠错：每块纠错码字数、块数（下标=版本号）
_ECC_PER_BLOCK = [None, 10, 16, 26, 18, 24, 16, 18, 22, 22, 26]
_NUM_BLOCKS = [None, 1, 1, 1, 2, 2, 4, 4, 4, 5, 5]
_ECL_FORMAT_BITS = 0  # M


def _gf_mul(x, y):
    z = 0
    for i in reversed(range(8)):
        z = (z << 1) ^ ((z >> 7) * 0x11D)
        z ^= ((y >> i) & 1) * x
    return z


def _rs_divisor(degree):
    result = [0] * (degree - 1) + [1]
    root = 1
    for _ in range(degree):
        for j in range(degree):
            result[j] = _gf_mul(result[j], root)
            if j + 1 < degree:
                result[j] ^= result[j + 1]
        root = _gf_mul(root, 0x02)
    return result


def _rs_remainder(data, divisor):
    result = [0] * len(divisor)
    for b in data:
        factor = b ^ result.pop(0)
        result.append(0)
        for i, coef in enumerate(divisor):
            result[i] ^= _gf_mul(coef, factor)
    return result


def _raw_modules(ver):
    r = (16 * ver + 128) * ver + 64
    if ver >= 2:
        n = ver // 7 + 2
        r -= (25 * n - 10) * n - 55
        if ver >= 7:
            r -= 36
    return r


def _data_codewords(ver):
    return _raw_modules(ver) // 8 - _ECC_PER_BLOCK[ver] * _NUM_BLOCKS[ver]


def _bit(x, i):
    return (x >> i) & 1 != 0


class _QR:
    def __init__(self, ver, codewords, mask):
        self.ver, self.size = ver, ver * 4 + 17
        n = self.size
        self.m = [[False] * n for _ in range(n)]
        self.f = [[False] * n for _ in range(n)]
        self._function_patterns()
        self._draw_codewords(codewords)
        self._apply_mask(mask)
        self._format_bits(mask)

    def _set(self, x, y, dark):
        self.m[y][x] = dark
        self.f[y][x] = True

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
        pos = self._align_positions()
        k = len(pos)
        for i in range(k):
            for j in range(k):
                if (i == 0 and j == 0) or (i == 0 and j == k - 1) or (i == k - 1 and j == 0):
                    continue
                for dy in range(-2, 3):
                    for dx in range(-2, 3):
                        self._set(pos[i] + dx, pos[j] + dy, max(abs(dx), abs(dy)) != 1)
        self._format_bits(0)  # 先占位
        if self.ver >= 7:
            rem = self.ver
            for _ in range(12):
                rem = (rem << 1) ^ ((rem >> 11) * 0x1F25)
            bits = self.ver << 12 | rem
            for i in range(18):
                b = _bit(bits, i)
                a, c = n - 11 + i % 3, i // 3
                self._set(a, c, b)
                self._set(c, a, b)

    def _align_positions(self):
        if self.ver == 1:
            return []
        k = self.ver // 7 + 2
        step = (self.ver * 8 + k * 3 + 5) // (k * 4 - 4) * 2
        return [6] + list(reversed([self.size - 7 - i * step for i in range(k - 1)]))

    def _format_bits(self, mask):
        data = _ECL_FORMAT_BITS << 3 | mask
        rem = data
        for _ in range(10):
            rem = (rem << 1) ^ ((rem >> 9) * 0x537)
        bits = (data << 10 | rem) ^ 0x5412
        n = self.size
        for i in range(6):
            self._set(8, i, _bit(bits, i))
        self._set(8, 7, _bit(bits, 6))
        self._set(8, 8, _bit(bits, 7))
        self._set(7, 8, _bit(bits, 8))
        for i in range(9, 15):
            self._set(14 - i, 8, _bit(bits, i))
        for i in range(8):
            self._set(n - 1 - i, 8, _bit(bits, i))
        for i in range(8, 15):
            self._set(8, n - 15 + i, _bit(bits, i))
        self._set(8, n - 8, True)

    def _draw_codewords(self, data):
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
                    y = (n - 1 - vert) if upward else vert
                    if not self.f[y][x] and i < len(data) * 8:
                        self.m[y][x] = _bit(data[i >> 3], 7 - (i & 7))
                        i += 1
            right -= 2

    _MASKS = [
        lambda x, y: (x + y) % 2 == 0, lambda x, y: y % 2 == 0, lambda x, y: x % 3 == 0,
        lambda x, y: (x + y) % 3 == 0, lambda x, y: (x // 3 + y // 2) % 2 == 0,
        lambda x, y: x * y % 2 + x * y % 3 == 0, lambda x, y: (x * y % 2 + x * y % 3) % 2 == 0,
        lambda x, y: ((x + y) % 2 + x * y % 3) % 2 == 0,
    ]

    def _apply_mask(self, mask):
        fn = self._MASKS[mask]
        for y in range(self.size):
            for x in range(self.size):
                if not self.f[y][x] and fn(x, y):
                    self.m[y][x] = not self.m[y][x]

    def penalty(self):
        """简化版评分(连续同色、2x2同色块、黑白比例)，只用来挑一个更好扫的掩码。"""
        n, m, p = self.size, self.m, 0
        for rows in (m, list(zip(*m))):
            for row in rows:
                run = 1
                for i in range(1, n):
                    if row[i] == row[i - 1]:
                        run += 1
                        if run == 5:
                            p += 3
                        elif run > 5:
                            p += 1
                    else:
                        run = 1
        for y in range(n - 1):
            for x in range(n - 1):
                if m[y][x] == m[y][x + 1] == m[y + 1][x] == m[y + 1][x + 1]:
                    p += 3
        dark = sum(sum(r) for r in m)
        p += abs(dark * 20 - n * n * 10) // (n * n) * 10
        return p


def _encode(text):
    data = text.encode("utf-8")
    for ver in range(1, 11):
        cap = _data_codewords(ver) * 8
        count_bits = 8 if ver <= 9 else 16
        if 4 + count_bits + len(data) * 8 <= cap:
            break
    else:
        raise ValueError("内容太长")
    bits = [0, 1, 0, 0] + [(len(data) >> i) & 1 for i in reversed(range(count_bits))]
    for b in data:
        bits += [(b >> i) & 1 for i in reversed(range(8))]
    bits += [0] * min(4, cap - len(bits))
    bits += [0] * (-len(bits) % 8)
    cws = [int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]
    pad = 0xEC
    while len(cws) < _data_codewords(ver):
        cws.append(pad)
        pad ^= 0xEC ^ 0x11
    # 分块 + 纠错 + 交织
    nblocks, ecclen = _NUM_BLOCKS[ver], _ECC_PER_BLOCK[ver]
    raw = _raw_modules(ver) // 8
    nshort = nblocks - raw % nblocks
    shortlen = raw // nblocks
    div = _rs_divisor(ecclen)
    blocks, k = [], 0
    for i in range(nblocks):
        dat = cws[k:k + shortlen - ecclen + (0 if i < nshort else 1)]
        k += len(dat)
        ecc = _rs_remainder(dat, div)
        if i < nshort:
            dat = dat + [0]
        blocks.append(dat + ecc)
    out = []
    for i in range(len(blocks[0])):
        for j, blk in enumerate(blocks):
            if i != shortlen - ecclen or j >= nshort:
                out.append(blk[i])
    return ver, out


def matrix(text):
    ver, cws = _encode(text)
    best = min((_QR(ver, cws, mk) for mk in range(8)), key=lambda q: q.penalty())
    return best.m


def svg(text, scale=6, border=4):
    m = matrix(text)
    n = len(m)
    size = (n + border * 2) * scale
    paths = "".join(f"M{(x + border) * scale},{(y + border) * scale}h{scale}v{scale}h-{scale}z"
                    for y in range(n) for x in range(n) if m[y][x])
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {size} {size}" width="{size}" height="{size}" '
            f'shape-rendering="crispEdges"><rect width="100%" height="100%" fill="#fff"/>'
            f'<path d="{paths}" fill="#000"/></svg>')
