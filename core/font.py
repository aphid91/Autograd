"""A 6x8 bitfield font, and the GLSL table generated from it.

The single source for every character the world shader can draw. Each glyph is
48 bits of on/off in a 6-wide, 8-tall grid, carried as two ints and decoded in
the shader by testing one bit -- so a character costs an integer op and one
`sd_box` against the cell the pixel lands in, whichever character it is. There
is no branch chain over glyph shapes, no texture, no sampler binding, and no
asset on disk; the table is spliced into common.glsl at load time exactly as
the per-type tables are (see core/glsl_types.py), so `F5` picks up an edit here
with no build step.

**Why a bitfield rather than an atlas or per-glyph SDFs.** An atlas -- bitmap
or distance-field -- is the cheapest per pixel, but it is a *resource*: a
texture to generate or ship, a binding to manage, and in the SDF case either a
committed binary asset or a startup generation pass. Both cut against the
project's "nothing is written to disk, F5 needs no build step" convention.
Per-glyph stroke SDFs (what `sd_glyph` does for the nine machine marks) are the
prettiest and scale perfectly, but 10 digits plus the alphabet is 36+
hand-authored functions behind a 36-way `if` chain evaluated per pixel per
column -- fine at nine, poor at thirty-six. The bitfield is the generalisation
of the seven-segment display it replaces: same "no asset, no sampler" property,
same antialiasing through `fill()`, but a legible glyph repertoire instead of
ten digits and a handful of fakeable letters.

The visible trade is that glyphs are *blocky* -- a dot-matrix readout rather
than smooth type. That is a deliberate aesthetic for a factory game's machine
displays, and the antialiasing keeps it crisp rather than crunchy, since each
lit cell is a real box in world space and not a magnified texel.

**6x8, not 5x7.** 5x7 is the classic small-bitmap size and fits in one int,
but it is tight: `4`, `M`, `W` and `X` are all compromised, and lowercase is
not really possible. 6x8 is 48 bits, which needs two ints either way, and two
ints cover up to 8x8 -- so the wider cell is free. Only uppercase is defined
for now; lowercase would fit if it is ever wanted.

Rows are listed **top first**, the way they read in the source, and are
reversed on the way out so bit 0 is the bottom-left of the glyph. Keeping the
authored order top-down and doing the flip in one place means a glyph edited
here looks the way it will draw.
"""

#: Glyph cell size, in glyph cells. Mirrored into GLSL as FONT_W / FONT_H.
FONT_W = 6
FONT_H = 8

#: The character set, in code order. A glyph's *code* is its index here, which
#: is what the shader's formatter emits and what `FONT` is indexed by. Codes
#: are an internal numbering rather than ASCII: the table only carries what is
#: drawable, so ASCII would leave 48 dead entries in a `const int[]` to no
#: purpose. `CODE_*` below names the ones the formatter needs by hand.
CHARS = (
    "0123456789"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "-+.,:/%=<>?!*#'\"()[]° "
)

#: Codes the number formatter refers to by name. Derived, so adding a
#: character to CHARS cannot silently renumber one of these out from under the
#: shader.
CODE_MINUS = CHARS.index("-")
CODE_POINT = CHARS.index(".")
CODE_SPACE = CHARS.index(" ")
CODE_E = CHARS.index("E")
CODE_DIGIT_0 = CHARS.index("0")

#: The two non-numeric values a payload can hold, spelled rather than
#: symbolised. NaN means "this computation had no answer" and an infinity
#: means a clamp was exceeded, and both are rare enough that three columns of
#: plain letters are worth more than a glyph nobody would recognise.
CODES_NAN = tuple(CHARS.index(c) for c in "NAN")
CODES_INF = tuple(CHARS.index(c) for c in "INF")

#: Every glyph, as 8 rows of 6 columns, top row first. `#` is a lit cell and
#: anything else is clear, so the shapes are legible as written and a typo is
#: visible rather than arithmetic.
#:
#: The design is a 5-wide face in a 6-wide cell, leaving a one-column gap at
#: the right as built-in letter spacing -- so columns advance by exactly
#: FONT_W with no per-glyph kerning and no gap tunable. Glyphs sit on rows
#: 1..7 with the top row clear, which gives descender room for a comma and
#: keeps a cap-height baseline consistent across the set.
_SHAPES = {
    "0": (".###..",
          "#...#.",
          "#..##.",
          "#.#.#.",
          "##..#.",
          "#...#.",
          ".###..",
          "......"),
    "1": ("..#...",
          ".##...",
          "..#...",
          "..#...",
          "..#...",
          "..#...",
          ".###..",
          "......"),
    "2": (".###..",
          "#...#.",
          "....#.",
          "...#..",
          "..#...",
          ".#....",
          "#####.",
          "......"),
    "3": ("#####.",
          "...#..",
          "..#...",
          "...#..",
          "....#.",
          "#...#.",
          ".###..",
          "......"),
    "4": ("...#..",
          "..##..",
          ".#.#..",
          "#..#..",
          "#####.",
          "...#..",
          "...#..",
          "......"),
    "5": ("#####.",
          "#.....",
          "####..",
          "....#.",
          "....#.",
          "#...#.",
          ".###..",
          "......"),
    "6": ("..##..",
          ".#....",
          "#.....",
          "####..",
          "#...#.",
          "#...#.",
          ".###..",
          "......"),
    "7": ("#####.",
          "....#.",
          "...#..",
          "..#...",
          "..#...",
          "..#...",
          "..#...",
          "......"),
    "8": (".###..",
          "#...#.",
          "#...#.",
          ".###..",
          "#...#.",
          "#...#.",
          ".###..",
          "......"),
    "9": (".###..",
          "#...#.",
          "#...#.",
          ".####.",
          "....#.",
          "...#..",
          ".##...",
          "......"),
    "A": (".###..",
          "#...#.",
          "#...#.",
          "#####.",
          "#...#.",
          "#...#.",
          "#...#.",
          "......"),
    "B": ("####..",
          "#...#.",
          "#...#.",
          "####..",
          "#...#.",
          "#...#.",
          "####..",
          "......"),
    "C": (".###..",
          "#...#.",
          "#.....",
          "#.....",
          "#.....",
          "#...#.",
          ".###..",
          "......"),
    "D": ("####..",
          "#...#.",
          "#...#.",
          "#...#.",
          "#...#.",
          "#...#.",
          "####..",
          "......"),
    "E": ("#####.",
          "#.....",
          "#.....",
          "####..",
          "#.....",
          "#.....",
          "#####.",
          "......"),
    "F": ("#####.",
          "#.....",
          "#.....",
          "####..",
          "#.....",
          "#.....",
          "#.....",
          "......"),
    "G": (".###..",
          "#...#.",
          "#.....",
          "#..##.",
          "#...#.",
          "#...#.",
          ".###..",
          "......"),
    "H": ("#...#.",
          "#...#.",
          "#...#.",
          "#####.",
          "#...#.",
          "#...#.",
          "#...#.",
          "......"),
    "I": (".###..",
          "..#...",
          "..#...",
          "..#...",
          "..#...",
          "..#...",
          ".###..",
          "......"),
    "J": ("..###.",
          "...#..",
          "...#..",
          "...#..",
          "...#..",
          "#..#..",
          ".##...",
          "......"),
    "K": ("#...#.",
          "#..#..",
          "#.#...",
          "##....",
          "#.#...",
          "#..#..",
          "#...#.",
          "......"),
    "L": ("#.....",
          "#.....",
          "#.....",
          "#.....",
          "#.....",
          "#.....",
          "#####.",
          "......"),
    "M": ("#...#.",
          "##.##.",
          "#.#.#.",
          "#.#.#.",
          "#...#.",
          "#...#.",
          "#...#.",
          "......"),
    "N": ("#...#.",
          "##..#.",
          "#.#.#.",
          "#..##.",
          "#...#.",
          "#...#.",
          "#...#.",
          "......"),
    "O": (".###..",
          "#...#.",
          "#...#.",
          "#...#.",
          "#...#.",
          "#...#.",
          ".###..",
          "......"),
    "P": ("####..",
          "#...#.",
          "#...#.",
          "####..",
          "#.....",
          "#.....",
          "#.....",
          "......"),
    "Q": (".###..",
          "#...#.",
          "#...#.",
          "#...#.",
          "#.#.#.",
          "#..#..",
          ".##.#.",
          "......"),
    "R": ("####..",
          "#...#.",
          "#...#.",
          "####..",
          "#.#...",
          "#..#..",
          "#...#.",
          "......"),
    "S": (".####.",
          "#.....",
          "#.....",
          ".###..",
          "....#.",
          "....#.",
          "####..",
          "......"),
    "T": ("#####.",
          "..#...",
          "..#...",
          "..#...",
          "..#...",
          "..#...",
          "..#...",
          "......"),
    "U": ("#...#.",
          "#...#.",
          "#...#.",
          "#...#.",
          "#...#.",
          "#...#.",
          ".###..",
          "......"),
    "V": ("#...#.",
          "#...#.",
          "#...#.",
          "#...#.",
          "#...#.",
          ".#.#..",
          "..#...",
          "......"),
    "W": ("#...#.",
          "#...#.",
          "#...#.",
          "#.#.#.",
          "#.#.#.",
          "##.##.",
          "#...#.",
          "......"),
    "X": ("#...#.",
          "#...#.",
          ".#.#..",
          "..#...",
          ".#.#..",
          "#...#.",
          "#...#.",
          "......"),
    "Y": ("#...#.",
          "#...#.",
          ".#.#..",
          "..#...",
          "..#...",
          "..#...",
          "..#...",
          "......"),
    "Z": ("#####.",
          "....#.",
          "...#..",
          "..#...",
          ".#....",
          "#.....",
          "#####.",
          "......"),
    # The minus sits on the glyph's vertical middle rather than on a
    # seven-segment middle bar, so "-1" reads as type and not as a display.
    "-": ("......",
          "......",
          "......",
          ".####.",
          "......",
          "......",
          "......",
          "......"),
    "+": ("......",
          "..#...",
          "..#...",
          "#####.",
          "..#...",
          "..#...",
          "......",
          "......"),
    # A 2x2 pip rather than a single cell: one cell disappears first as the
    # glyph shrinks, and a decimal point is the last thing that should go.
    ".": ("......",
          "......",
          "......",
          "......",
          "......",
          ".##...",
          ".##...",
          "......"),
    ",": ("......",
          "......",
          "......",
          "......",
          ".##...",
          ".##...",
          "..#...",
          ".#...."),
    ":": ("......",
          "..##..",
          "..##..",
          "......",
          "..##..",
          "..##..",
          "......",
          "......"),
    "/": ("....#.",
          "....#.",
          "...#..",
          "..#...",
          ".#....",
          "#.....",
          "#.....",
          "......"),
    "%": ("##..#.",
          "##..#.",
          "...#..",
          "..#...",
          ".#....",
          "#..##.",
          "#..##.",
          "......"),
    "=": ("......",
          "......",
          "#####.",
          "......",
          "#####.",
          "......",
          "......",
          "......"),
    "<": ("...#..",
          "..#...",
          ".#....",
          "#.....",
          ".#....",
          "..#...",
          "...#..",
          "......"),
    ">": (".#....",
          "..#...",
          "...#..",
          "....#.",
          "...#..",
          "..#...",
          ".#....",
          "......"),
    "?": (".###..",
          "#...#.",
          "....#.",
          "...#..",
          "..#...",
          "......",
          "..#...",
          "......"),
    "!": ("..#...",
          "..#...",
          "..#...",
          "..#...",
          "..#...",
          "......",
          "..#...",
          "......"),
    "*": ("......",
          "#.#.#.",
          ".###..",
          "#####.",
          ".###..",
          "#.#.#.",
          "......",
          "......"),
    "#": (".#.#..",
          ".#.#..",
          "#####.",
          ".#.#..",
          "#####.",
          ".#.#..",
          ".#.#..",
          "......"),
    "'": ("..#...",
          "..#...",
          "......",
          "......",
          "......",
          "......",
          "......",
          "......"),
    '"': (".#.#..",
          ".#.#..",
          "......",
          "......",
          "......",
          "......",
          "......",
          "......"),
    "(": ("...#..",
          "..#...",
          ".#....",
          ".#....",
          ".#....",
          "..#...",
          "...#..",
          "......"),
    ")": (".#....",
          "..#...",
          "...#..",
          "...#..",
          "...#..",
          "..#...",
          ".#....",
          "......"),
    "[": ("..##..",
          "..#...",
          "..#...",
          "..#...",
          "..#...",
          "..#...",
          "..##..",
          "......"),
    "]": (".##...",
          "..#...",
          "..#...",
          "..#...",
          "..#...",
          "..#...",
          ".##...",
          "......"),
    "°": (".##...",
          "#..#..",
          "#..#..",
          ".##...",
          "......",
          "......",
          "......",
          "......"),
    " ": ("......",
          "......",
          "......",
          "......",
          "......",
          "......",
          "......",
          "......"),
}


def glyph_bits(ch):
    """The two 24-bit halves of `ch`'s bitfield, low rows first.

    Bit `y * FONT_W + x` is the cell at column `x` from the left and row `y`
    from the **bottom**, so the glyph's coordinate system matches the world's
    `+Y` up. `_SHAPES` is authored top-down, which is why the rows are
    reversed here.

    Split into two ints of 24 bits rather than 32 + 16 so neither half can
    touch bit 31: a negative int in a `const int[]` table is legal GLSL but
    the shader decodes with `>>`, and a sign-filled shift would light cells
    that are not set. 24 is also exactly four rows, so each half is a clean
    top/bottom split of the glyph.
    """
    rows = _SHAPES[ch]
    if len(rows) != FONT_H:
        raise ValueError(f"{ch!r} has {len(rows)} rows, want {FONT_H}")
    lo = hi = 0
    for y, row in enumerate(reversed(rows)):
        if len(row) != FONT_W:
            raise ValueError(f"{ch!r} row {y} is {len(row)} wide, want {FONT_W}")
        for x, c in enumerate(row):
            if c != "#":
                continue
            bit = y * FONT_W + x
            if bit < 24:
                lo |= 1 << bit
            else:
                hi |= 1 << (bit - 24)
    return lo, hi


#: `(lo, hi)` per character code, in CHARS order. Built once at import so a
#: malformed shape is an import-time error rather than a shader that compiles
#: and draws nothing.
FONT = tuple(glyph_bits(ch) for ch in CHARS)


def glsl_table():
    """The font as GLSL: the cell size, the named codes, and the bit table.

    Spliced in at the `// @generated type-ids` marker, which is where the
    other row-layout constants go -- the formatter in world.frag needs
    `CODE_*` before it can name a glyph, and the ids marker is already
    required to precede everything that does.
    """
    lines = [
        f"const int FONT_W = {FONT_W};",
        f"const int FONT_H = {FONT_H};",
        f"const int FONT_COUNT = {len(CHARS)};",
        f"const int CODE_DIGIT_0 = {CODE_DIGIT_0};",
        f"const int CODE_MINUS = {CODE_MINUS};",
        f"const int CODE_POINT = {CODE_POINT};",
        f"const int CODE_SPACE = {CODE_SPACE};",
        f"const int CODE_E = {CODE_E};",
    ]
    for name, codes in (("NAN", CODES_NAN), ("INF", CODES_INF)):
        for i, code in enumerate(codes):
            lines.append(f"const int CODE_{name}_{i} = {code};")
    # Two parallel arrays rather than one of ivec2: the shader indexes by a
    # computed code, and a flat int array is the form every driver handles
    # identically.
    for half, name in ((0, "FONT_LO"), (1, "FONT_HI")):
        vals = ", ".join(f"0x{FONT[i][half]:06X}" for i in range(len(CHARS)))
        lines.append(
            f"const int {name}[{len(CHARS)}] = int[{len(CHARS)}]({vals});")
    return "\n".join(lines)


#: The exponent range the formatter keeps in fixed-point notation. MUST match
#: NUM_FIXED_HI_E / NUM_FIXED_LO_E in common.glsl, which tests/test_glsl.py
#: pins -- the Python reference below is only an oracle if it switches format
#: at the same place the shader does.
FIXED_HI_E = 7
FIXED_LO_E = -4


def format_number(value, sig=2, trim=True):
    """The reference implementation of the shader's `format_number()`.

    Returns the string the GLSL formatter should produce for `value`. This is
    an **oracle**, in the same sense as `belts.naive_tick()`: the shader is
    the shipping implementation and this exists to disagree with it.

    That matters more here than it looks. A formatter bug draws a perfectly
    crisp *wrong number* -- there is no visual artefact to notice, and the
    pixel tests can only tell that something value-dependent was drawn, not
    what it said. The previous seven-segment formatter was pinned by regexes
    over the shader source instead, which could assert that a branch existed
    but never that it computed the right thing.

    Deliberately written independently of the GLSL -- Python string formatting
    rather than a transliteration of the same digit arithmetic -- so a shared
    misconception does not cancel out. Where the two must agree exactly is the
    format *policy*: the fixed/scientific boundary and the rounding and
    trimming rules.
    """
    import math

    if value != value:                      # NaN
        return "NAN"
    if math.isinf(value):
        return ("-" if value < 0 else "") + "INF"

    neg = value < 0.0
    mag = abs(value)
    sign = "-" if neg else ""

    if mag == 0.0:
        return sign + "0"

    e = math.floor(math.log10(mag))

    if e >= FIXED_HI_E or e < FIXED_LO_E:
        # Scientific. Round the mantissa first, since rounding can carry it
        # to 10 and bump the exponent.
        mant = mag / (10.0 ** e)
        scale = 10 ** sig
        r = math.floor(mant * scale + 0.5)
        if r >= 10 * scale:
            r //= 10
            e += 1
        whole, frac = divmod(int(r), scale)
        digits = f"{frac:0{sig}d}" if sig > 0 else ""
        if trim:
            digits = digits.rstrip("0")
        body = f"{whole}" + (f".{digits}" if digits else "")
        return f"{sign}{body}E{'-' if e < 0 else ''}{abs(int(e))}"

    # Fixed point. Round once at the requested precision, then split -- the
    # other order prints 9.999's integer part as 9 and its fraction as 00.
    scale = 10 ** sig
    r = math.floor(mag * scale + 0.5)
    whole, frac = divmod(int(r), scale)
    digits = f"{frac:0{sig}d}" if sig > 0 else ""
    if trim:
        digits = digits.rstrip("0")
    return f"{sign}{whole}" + (f".{digits}" if digits else "")


def codes_for(text):
    """`text` as a tuple of glyph codes, for comparing against the shader.

    Raises on a character the font cannot draw, which is the point: it means
    the formatter can emit something the renderer would silently skip.
    """
    out = []
    for ch in text:
        if ch not in CHARS:
            raise ValueError(f"no glyph for {ch!r} in {text!r}")
        out.append(CHARS.index(ch))
    return tuple(out)


def _self_check():
    # Every character in CHARS has a shape, and nothing has a shape without a
    # code -- a glyph the formatter cannot name is dead weight in the table.
    assert set(CHARS) == set(_SHAPES), (
        set(CHARS) ^ set(_SHAPES))
    assert len(CHARS) == len(set(CHARS)), "a character is listed twice"

    # No half may touch bit 31 (see glyph_bits).
    for ch, (lo, hi) in zip(CHARS, FONT):
        assert 0 <= lo < (1 << 24), ch
        assert 0 <= hi < (1 << 24), ch

    # Space is blank and nothing else is. A blank glyph draws nothing, so a
    # typo that empties one is otherwise invisible until something reads it.
    for ch, (lo, hi) in zip(CHARS, FONT):
        blank = (lo == 0 and hi == 0)
        assert blank == (ch == " "), f"{ch!r} blankness is {blank}"

    # The rightmost column is the inter-glyph gap, so advancing by FONT_W
    # leaves exactly one clear column between neighbours. If a glyph ever
    # needs the full width, that becomes a per-glyph advance instead.
    for ch in CHARS:
        for y, row in enumerate(_SHAPES[ch]):
            assert row[FONT_W - 1] != "#", (
                f"{ch!r} row {y} uses the spacing column")

    # The digits are what the number formatter leans on, so pin that they are
    # distinct shapes. Two digits sharing a bitfield would misreport a value
    # with no other symptom.
    seen = {}
    for d in "0123456789":
        bits = glyph_bits(d)
        assert bits not in seen, f"{d} and {seen[bits]} are the same glyph"
        seen[bits] = d

    # --- the formatter's own rules ---
    # Trimming is the whole reason the flag exists, so pin the cases that
    # motivated it. A minus now costs a column, which trimming pays for.
    for value, want in [
        (-1.0, "-1"), (1.0, "1"), (0.0, "0"), (-0.0, "0"),
        (2.5, "2.5"), (-2.5, "-2.5"), (3.14159, "3.14"),
        (-0.5, "-0.5"), (100.0, "100"), (12.0, "12"),
    ]:
        got = format_number(value, sig=2, trim=True)
        assert got == want, f"format_number({value}) == {got!r}, want {want!r}"

    # Without trimming the same values keep their zeros, so the flag is
    # genuinely doing the work rather than the rounding happening to drop
    # them.
    assert format_number(-1.0, sig=2, trim=False) == "-1.00"
    assert format_number(2.5, sig=2, trim=False) == "2.50"

    # sig controls the digit count, which is the other half of the ask.
    assert format_number(3.14159, sig=0, trim=True) == "3"
    assert format_number(3.14159, sig=1, trim=True) == "3.1"
    assert format_number(3.14159, sig=4, trim=True) == "3.1416"

    # Rounding must carry through the integer part rather than being applied
    # to the fraction alone -- 9.999 is 10, not 9.00.
    assert format_number(9.999, sig=2, trim=True) == "10"
    assert format_number(0.999, sig=2, trim=True) == "1"

    # The scientific branch, including a mantissa that rounds up to 10 and
    # must renormalise rather than clamp: 9.96e9 is 1E10, not 9.9E9.
    assert format_number(5e7, sig=2, trim=True) == "5E7"
    assert format_number(1e-9, sig=2, trim=True) == "1E-9"
    assert format_number(-5e7, sig=2, trim=True) == "-5E7"
    assert format_number(9.96e9, sig=1, trim=True) == "1E10"

    # The fixed/scientific boundary sits exactly where the constants say.
    assert "E" not in format_number(10.0 ** (FIXED_HI_E - 1))
    assert "E" in format_number(10.0 ** FIXED_HI_E)
    assert "E" not in format_number(10.0 ** FIXED_LO_E)
    assert "E" in format_number(10.0 ** (FIXED_LO_E - 1))

    # Non-finite values. NaN propagates through the gates by design, so it
    # reaches the renderer and must say so rather than formatting as 0.
    assert format_number(float("nan")) == "NAN"
    assert format_number(float("inf")) == "INF"
    assert format_number(float("-inf")) == "-INF"

    # Every string the formatter can produce must be drawable -- a character
    # with no glyph would be silently skipped by the shader, turning "-1"
    # into "1" with no other symptom.
    for v in (0.0, -1.0, 2.5, -3.75, 1e-9, 5e7, -9.96e9, 1234567.0,
              float("nan"), float("inf"), float("-inf")):
        for sig in (0, 1, 2, 3):
            for trim in (True, False):
                codes_for(format_number(v, sig, trim))


_self_check()
