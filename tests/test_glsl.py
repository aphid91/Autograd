"""Static checks on the generated GLSL and its hand-written consumers.

No GL context and no board: these read the shader source as text and compare
it against the Python definitions. They live apart from the scenario tests
because they share nothing with them -- no window, no state to reset -- and
because a failure here means "the generator or the shader is wrong", not
"some gesture misbehaved".

Most of what used to be pinned here is now generated (see
core/glsl_types.py), so these check two different things: that the
*generator* emits what the entity classes say, and that the *hand-written*
GLSL consuming those tables does the authored/placed conversion correctly.
"""
import re as _re

from core import belt_device, belts, cell, entity, font, glsl_types
from core.entity import STRUCTURES, Sink, Underground
from render import camera, shader_loader

from tests.harness import ok

#: The fully resolved shader source, includes and generated tables expanded.
_src = shader_loader.load("world.frag")

#: Direction constant -> its GLSL name, for matching generated table text.
_DIR_NAMES_GLSL = {cell.DIR_PX: "DIR_PX", cell.DIR_PY: "DIR_PY",
                   cell.DIR_NX: "DIR_NX", cell.DIR_NY: "DIR_NY"}

#: The generated pieces on their own, so a check can test the *generator*
#: without the surrounding hand-written GLSL getting in the way.
_ids = glsl_types.type_constants()
_tables = glsl_types.generate()


def _glsl_int(name):
    """An integer constant parsed back out of the shader source.

    Handles both forms the shader uses: a `const int` (which has an `=`) and
    a `#define` (which does not). The tunables block is all `#define`s, so a
    check against one of those would otherwise silently not find it -- and
    the assert below would read as "the shader is missing this" rather than
    "this helper cannot see that form".
    """
    m = _re.search(rf"\b{name}\s*=\s*(-?0x[0-9A-Fa-f]+|-?\d+)", _src)
    if not m:
        m = _re.search(rf"^[ \t]*#define[ \t]+{name}[ \t]+"
                       rf"(-?0x[0-9A-Fa-f]+|-?\d+)[ \t]*(?://.*)?$",
                       _src, _re.M)
    assert m, f"{name} not found in shader source"
    return int(m.group(1), 0)


def test_shader_includes_resolve():
    assert "#include" not in _src, "an include was left unresolved"
    assert "DIR_VECS" in _src and "sd_chevron" in _src, "common.glsl not spliced in"
    # The encoding constants must appear exactly once -- a second copy would be a
    # silent divergence from core/cell.py.
    assert _src.count("const int TYPE_SHIFT") == 1, "common.glsl included twice"
    _deps = [d.replace("\\", "/").rsplit("/", 1)[-1]
             for d in shader_loader.dependencies("world.frag")]
    assert set(_deps) == {"world.frag", "common.glsl"}, _deps
    ok(f"world.frag resolves includes ({len(_src)} chars, deps {_deps})")

def test_glsl_cell_encoding_matches_python():
    # The shader-side encoding must agree with cell.py. Parse the constants back
    # out of the GLSL and compare -- this is the one place the two copies can
    # drift, so it is worth pinning.

    for _name, _want in [
        ("DIR_SHIFT", cell.DIR_SHIFT), ("DIR_MASK", cell.DIR_MASK),
        ("TYPE_SHIFT", cell.TYPE_SHIFT), ("TYPE_MASK", cell.TYPE_MASK),
        ("ID_SHIFT", cell.ID_SHIFT), ("ID_MASK", cell.ID_MASK),
        ("GHOST_MASK", cell.GHOST_MASK), ("LOCAL_SHIFT", cell.LOCAL_SHIFT),
        ("FLIP_SHIFT", cell.FLIP_SHIFT),
        ("TYPE_BELT", cell.TYPE_BELT), ("TYPE_SWAP", cell.TYPE_SWAP),
        ("TYPE_SHEAR", cell.TYPE_SHEAR), ("TYPE_MAC", cell.TYPE_MAC),
        ("TYPE_SOURCE", cell.TYPE_SOURCE), ("TYPE_SINK", cell.TYPE_SINK),
        ("GHOST_PENDING", cell.GHOST_PENDING),
        ("GHOST_FRUSTRATED", cell.GHOST_FRUSTRATED),
        # The matched state is hand-mirrored in common.glsl like the rest of
        # the ghost field, so it needs pinning like the rest. It also has to
        # *fit* -- it is the value that uses up the 2-bit field's last slot,
        # so a widened ghost state would silently truncate it.
        ("GHOST_MATCHED", cell.GHOST_MATCHED),
    ]:
        assert _glsl_int(_name) == _want, (
            f"{_name}: shader says {_glsl_int(_name)}, cell.py says {_want}")
    ok("GLSL cell encoding matches core/cell.py")

def test_generated_type_ids_have_no_dangling_refs():
    """Every registered type is emitted, and every name used is defined."""
    for _tid, _tname in cell.TYPE_NAMES.items():
        _const = "TYPE_" + _tname.upper()
        assert f"const int {_const} = {_tid};" in _ids, _const
    for _const in set(_re.findall(r"\bTYPE_[A-Z_]+\b", _tables)):
        assert f"const int {_const} =" in _ids, (
            f"generated tables reference {_const}, which is never defined")
    ok(f"generated type ids cover {len(cell.TYPE_NAMES)} types, no dangling refs")

def test_generated_port_tables_match_python():
    # The generated port tables must say exactly what face_masks_at() says.
    # This is a generator test, not a drift test: it catches an emitter bug -- a
    # transposed offset, a dropped cell -- which regenerating would not fix.
    _checked = 0
    for _cls in STRUCTURES:
        _tname = "TYPE_" + cell.TYPE_NAMES[_cls.TYPE_ID].upper()
        for _which, _idx in (("input", 0), ("output", 1)):
            _body = _re.search(
                rf"int {_which}_face_mask_local\(int type, ivec2 lcl\)"
                rf"\s*\{{(.*?)\n\}}", _tables, _re.S).group(1)
            _branch = _re.search(rf"type == {_tname}\) \{{(.*?)\n    \}}",
                                 _body, _re.S)
            _emitted = {}
            if _branch:
                for _lx, _ly, _expr in _re.findall(
                        r"lcl == ivec2\((\d+), (\d+)\)\) return ([^;]+);",
                        _branch.group(1)):
                    _m = 0
                    for _f in _re.findall(r"FACE\((DIR_\w+)\)", _expr):
                        _m |= 1 << getattr(cell, _f)
                    _emitted[(int(_lx), int(_ly))] = _m
            _w, _h = _cls.FOOTPRINT
            for _lx in range(_w):
                for _ly in range(_h):
                    _want = _cls.face_masks_at(_lx, _ly)[_idx]
                    _got = _emitted.get((_lx, _ly), 0)
                    assert _got == _want, (
                        f"{_cls.__name__} {_which} cell {(_lx, _ly)}: "
                        f"emitted {_got:04b} != entity {_want:04b}")
                    _checked += 1
            _inside = {(x, y) for x in range(_w) for y in range(_h)}
            assert not (set(_emitted) - _inside), _cls.__name__
    ok(f"generated port tables match Entity.face_masks_at() ({_checked} cells)")

def test_generated_footprints_colours_and_glyphs_match_python():
    # Footprints, control axes, colours and glyphs likewise.
    for _cls in STRUCTURES:
        _tname = "TYPE_" + cell.TYPE_NAMES[_cls.TYPE_ID].upper()
        _fp = _re.search(
            rf"ivec2 type_footprint.*?type == {_tname}\) "
            rf"return ivec2\((\d+), (\d+)\)", _tables, _re.S)
        _got_fp = (int(_fp.group(1)), int(_fp.group(2))) if _fp else (1, 1)
        assert _got_fp == _cls.FOOTPRINT, (_cls.__name__, _got_fp)

        _pt = _re.search(
            rf"int pass_through_mask.*?type == {_tname}\) return ([^;]+);",
            _tables, _re.S)
        _got_pt = 0
        if _pt:
            for _f in _re.findall(r"FACE\((DIR_\w+)\)", _pt.group(1)):
                _got_pt |= 1 << getattr(cell, _f)
        _want_pt = 0
        for _f in _cls.PASS_THROUGH:
            _want_pt |= 1 << _f
        assert _got_pt == _want_pt, (_cls.__name__, _got_pt, _want_pt)

        if _cls.COLOUR is not None:
            _c = _re.search(
                rf"vec3 type_colour.*?type == {_tname}\) return "
                rf"vec3\(([\d.]+), ([\d.]+), ([\d.]+)\)", _tables, _re.S)
            assert _c, f"{_cls.__name__} missing from type_colour()"
            _got_c = tuple(round(float(g), 3) for g in _c.groups())
            assert _got_c == tuple(round(v, 3) for v in _cls.COLOUR), _cls.__name__

        _g = _re.search(rf"int type_glyph.*?type == {_tname}\) return (\w+);",
                        _tables, _re.S)
        _got_g = _g.group(1) if _g else "GLYPH_NONE"
        assert _got_g == glsl_types.GLYPHS[_cls.GLYPH], (_cls.__name__, _got_g)
    ok("generated footprints, control axes, colours and glyphs match Python")

def test_control_axes_have_real_output_faces():
    # A declared pass-through axis must have an output face pointing that way at
    # every orientation, or the overlay draws a channel the machine cannot
    # carry. The shader flips before rotating, like the port masks; rotating
    # alone left a mirrored machine's overlay disagreeing with its own ports.
    for _cls in STRUCTURES:
        _want = 0
        for _f in _cls.PASS_THROUGH:
            _want |= 1 << _f
        for _facing in range(4):
            for _flip in (False, True):
                _out_faces = {f for _c, f in
                              _cls.faces_for(0, 0, _facing, _flip)[1]}
                _pm = _want
                if _flip:
                    _pm = sum(1 << cell.flip_dir_y(_d)
                              for _d in range(4) if _pm >> _d & 1)
                _pm = sum(1 << ((_d + _facing) % 4)
                          for _d in range(4) if _pm >> _d & 1)
                for _d in range(4):
                    if not _pm >> _d & 1:
                        continue
                    assert _d in _out_faces, (
                        f"{_cls.__name__} facing {cell.DIR_NAMES[_facing]} "
                        f"flip={_flip}: control axis {cell.DIR_NAMES[_d]} has no "
                        f"output face")
    ok("control axes line up with real output faces at every orientation")

def test_glsl_masks_invert_the_placed_offset():
    # The hand-written GLSL that *uses* the generated tables still has to do the
    # authored/placed conversion right. That is not generated, and getting it
    # wrong is what put a rotated Sink's ports on interior edges.
    for _fn in ("input_face_mask", "output_face_mask"):
        _body = _re.search(
            rf"int {_fn}\(int type, ivec2 lcl, int facing, bool flipped\)"
            rf"\s*\{{(.*?)\n\}}", _src, _re.S)
        assert _body, f"{_fn} not found with the expected signature"
        _b = _body.group(1)
        assert "unplace_local(type, lcl, facing, flipped)" in _b, (
            f"{_fn} must invert the placed offset before the table lookup -- "
            f"indexing with the raw .y offset puts ports on interior edges")
        assert "flip_face_mask" in _b, f"{_fn} must mirror faces when flipped"
        assert "rotate_face_mask" in _b, f"{_fn} must rotate faces into the facing"
    ok("GLSL mask functions invert the placed offset before lookup")

def test_glsl_mask_callers_pass_the_flip_flag():
    # The two neighbour probes must read the *neighbour's* own flip bit.
    for _probe in ("feeds_into", "accepts_from"):
        _pb = _re.search(rf"bool {_probe}\([^)]*\)\s*\{{(.*?)\n\}}", _src, _re.S)
        assert _pb, f"{_probe} not found"
        assert "is_flipped(n.z)" in _pb.group(1), (
            f"{_probe} must read the neighbour's own flip bit (is_flipped(n.z))")
    assert _re.search(r"input_face_mask\(type, lcl, facing, flipped\)", _src)
    assert _re.search(r"output_face_mask\(type, lcl, facing, flipped\)", _src)
    ok("GLSL port mask callers pass the flip flag")

def test_glsl_control_overlay_flips():
    # The overlay must flip its control axes, not merely rotate them.
    _pb = _re.search(r"int pm = pass_through_mask\(type\);(.*?)int pass_mask",
                     _src, _re.S)
    assert _pb and "flip_face_mask(pm)" in _pb.group(1), (
        "the control-signal overlay must mirror its axes when the machine is "
        "flipped, exactly as the port masks do")
    ok("GLSL control overlay flips with the machine")

def test_glsl_underground_constants_match():
    # The shader scans for a paired run over the same distance the engine pairs
    # over; scanning shorter would stop the dots before the exit.
    _glsl_span = int(_re.search(r"#define UNDER_MAX_SPAN\s+(\d+)", _src).group(1))
    assert _glsl_span == Underground.MAX_SPAN, (
        f"shader UNDER_MAX_SPAN {_glsl_span} != "
        f"Underground.MAX_SPAN {Underground.MAX_SPAN}")
    for _name, _want in [("UNDER_EXIT_SHIFT", cell.UNDER_EXIT_SHIFT),
                         ("UNDER_PAIRED_SHIFT", cell.UNDER_PAIRED_SHIFT),
                         ("TYPE_UNDER", cell.TYPE_UNDER)]:
        assert _glsl_int(_name) == _want, (
            f"{_name}: shader {_glsl_int(_name)}, cell.py {_want}")
    ok(f"GLSL underground constants match (span {_glsl_span})")

def test_glsl_segment_encoding_matches_python():
    # The shader unpacks .w to find which segment a cell is on and how far
    # along; a shifted field would put every item on the wrong belt.
    for _name, _want in [("SEG_OFF_SHIFT", cell.SEG_OFF_SHIFT),
                         ("SEG_OFF_MASK", cell.SEG_OFF_MASK),
                         ("SEG_ID_SHIFT", cell.SEG_ID_SHIFT),
                         ("SEG_ID_MASK", cell.SEG_ID_MASK)]:
        assert _glsl_int(_name) == _want, (
            f"{_name}: shader {_glsl_int(_name)}, cell.py {_want}")
    ok("GLSL segment field layout matches core/cell.py")


def test_glsl_belt_unit_matches_python():
    # BELT_UNIT scales every position the shader computes. If it disagreed
    # with belts.UNIT, items would render at a constant multiple of their
    # real positions -- which looks like a plausible belt at a glance.
    _m = _re.search(r"const float BELT_UNIT\s*=\s*([0-9.]+)", _src)
    assert _m, "BELT_UNIT not found in shader source"
    _glsl_unit = float(_m.group(1))
    assert _glsl_unit == float(belts.UNIT), (
        f"shader BELT_UNIT {_glsl_unit} != belts.UNIT {belts.UNIT}")
    ok(f"GLSL BELT_UNIT matches belts.UNIT ({belts.UNIT})")


def test_glsl_ssbo_bindings_match_the_camera():
    # The camera binds buffers to numbered slots and the shader declares
    # those numbers; a mismatch is silent -- the shader reads whatever else
    # happens to be bound there, or zeroes.
    _bindings = dict(_re.findall(
        r"layout\(std430,\s*binding\s*=\s*(\d+)\)\s*buffer\s+(\w+)", _src))
    _by_name = {name: int(slot) for slot, name in _bindings.items()}
    assert _by_name.get("SegmentsI") == camera.SEG_I_SSBO_BINDING, _by_name
    assert _by_name.get("SegmentsF") == camera.SEG_F_SSBO_BINDING, _by_name
    assert _by_name.get("BeltItems") == camera.ITEM_SSBO_BINDING, _by_name
    assert _by_name.get("Entities") == camera.ENTITY_SSBO_BINDING, _by_name
    # Every declared binding must be distinct, or two buffers would collide.
    _slots = sorted(_by_name.values())
    assert len(set(_slots)) == len(_slots), f"duplicate SSBO binding: {_slots}"
    # ...and the camera must not bind anything to a slot no shader declares,
    # nor leave a declared slot unbound.
    assert set(_slots) == {camera.ENTITY_SSBO_BINDING,
                           camera.SEG_I_SSBO_BINDING,
                           camera.SEG_F_SSBO_BINDING,
                           camera.ITEM_SSBO_BINDING}, _by_name
    ok(f"GLSL SSBO bindings match render/camera.py ({_by_name})")


def test_glsl_segment_record_layout_matches_the_device():
    # The shader reads head/tail/base/cap from an ivec4 and
    # anchor/length/speed/spacing from a vec4, by swizzle. Those positions
    # are decided by core/belt_device.py, so the field order has to agree --
    # a transposed pair here would be read as a plausible-looking number.
    _body = _re.search(r"float item_position\([^)]*\)\s*\{(.*?)\n\}",
                       _src, _re.S)
    assert _body, "item_position() not found"
    _b = _body.group(1)
    # si.x head, si.y tail (unused here), si.z base, si.w cap
    assert "si.z + (k % si.w)" in _b, (
        "the ring slot must be base + k % cap, i.e. si.z + k % si.w")
    assert "float(k - si.x)" in _b, (
        "the packed term must offset from head, i.e. si.x")
    # sf.x anchor, sf.y length, sf.z speed, sf.w spacing
    assert "min(sf.x + alpha * sf.z, sf.y)" in _b, (
        "the render-time anchor must be min(anchor + alpha*speed, length)")
    assert "* sf.w" in _b, "the packed term must use spacing, i.e. sf.w"
    for _name, _idx, _want in [
            ("SEG_I_HEAD", belt_device.SEG_I_HEAD, 0),
            ("SEG_I_TAIL", belt_device.SEG_I_TAIL, 1),
            ("SEG_I_BASE", belt_device.SEG_I_BASE, 2),
            ("SEG_I_CAP", belt_device.SEG_I_CAP, 3),
            ("SEG_F_ANCHOR", belt_device.SEG_F_ANCHOR, 0),
            ("SEG_F_LENGTH", belt_device.SEG_F_LENGTH, 1),
            ("SEG_F_SPEED", belt_device.SEG_F_SPEED, 2),
            ("SEG_F_SPACING", belt_device.SEG_F_SPACING, 3)]:
        assert _idx == _want, (
            f"{_name} is {_idx}; the shader's swizzles assume {_want}")
    ok("GLSL item_position() reads the belt_device record layout correctly")


def test_glsl_item_position_mirrors_the_python_closed_form():
    # The shader recomputes positions rather than reading them, so it is a
    # second implementation of the closed form and can drift from
    # BeltSim.position_of(). Pin the pieces that encode the actual maths.
    _body = _re.search(r"float item_position\([^)]*\)\s*\{(.*?)\n\}",
                       _src, _re.S).group(1)
    # The age term, capped, exactly as Python caps it.
    assert "tick - int(items[slot].x)" in _body and "+ alpha" in _body, (
        "age must be (tick - enter_tick) + alpha")
    assert "min(age," in _body, "age must be capped, as in Python"
    # The result is the min of the two terms -- the heart of the formula.
    assert "min(unblocked, packed_pos)" in _body, (
        "position must be min(unblocked, packed)")
    # And the anchor clamp, which is the bug that let a blocked clump slide
    # off the end of the belt between ticks.
    assert "min(sf.x + alpha * sf.z, sf.y)" in _body, (
        "the fractional anchor must be clamped to the belt length, matching "
        "the whole-tick min(anchor + speed, length)")
    ok("GLSL item_position() mirrors BeltSim.position_of()")


def test_glsl_entity_row_layout_matches_python():
    # The shader reads gate state straight out of the entity SSBO, so the
    # stride and the field offsets must come from core/entity.py. A stale
    # stride here reads a neighbouring row's fields -- plausible floats in
    # the wrong place, which is the worst kind of wrong.
    for _name, _want in [("ENTITY_STRIDE", entity.ENTITY_STRIDE),
                         ("MAX_CHANNELS", entity.MAX_CHANNELS),
                         ("F_IN_VALUE", entity.Entity.F_IN_VALUE),
                         ("F_IN_HELD", entity.Entity.F_IN_HELD),
                         ("F_OUT_VALUE", entity.Entity.F_OUT_VALUE),
                         ("F_OUT_HELD", entity.Entity.F_OUT_HELD)]:
        assert _glsl_int(_name) == _want, (
            f"{_name}: shader {_glsl_int(_name)}, entity.py {_want}")
    # The gate block must fit inside the stride, in the shader's terms too.
    assert (_glsl_int("F_OUT_HELD") + _glsl_int("MAX_CHANNELS")
            <= _glsl_int("ENTITY_STRIDE")), "gate fields overrun the stride"
    ok(f"GLSL entity row layout matches core/entity.py "
       f"(stride {entity.ENTITY_STRIDE})")


def test_generated_channel_tables_match_python():
    # output_channel_at() tells the renderer which gate-state slot a port
    # face drives. It cannot assume "port index == channel": Sink folds its
    # eight faces onto four channels, and indexing by face would read slots
    # that do not exist.
    for c in STRUCTURES:
        for which, ports, chan in (("input", c.INPUTS, c.in_channel),
                                   ("output", c.OUTPUTS, c.out_channel)):
            for i, (lx, ly, face) in enumerate(ports):
                want = chan(i)
                needle = (f"if (lcl == ivec2({lx}, {ly}) && face == "
                          f"{_DIR_NAMES_GLSL[face]}) return {want};")
                assert needle in _tables, (
                    f"{c.__name__} {which} port {i} should map to channel "
                    f"{want}; missing from the generated table:\n  {needle}")
    # Sink specifically: eight faces folded onto four channels, one per
    # footprint cell. The loop above already pins each face's channel; what
    # matters here is that the *shape* of the fold survived into GLSL, since
    # a channel is one item per tick and collapsing these would silently cap
    # a sink's throughput at one belt.
    assert Sink.in_channels() == 4, Sink.in_channels()
    by_cell = {}
    for i, (lx, ly, face) in enumerate(Sink.INPUTS):
        needle = (f"if (lcl == ivec2({lx}, {ly}) && face == "
                  f"{_DIR_NAMES_GLSL[face]}) return {Sink.in_channel(i)};")
        assert needle in _tables, needle
        by_cell.setdefault((lx, ly), set()).add(Sink.in_channel(i))
    assert len(by_cell) == 4, f"expected 4 sink cells, got {sorted(by_cell)}"
    for xy, channels in by_cell.items():
        assert channels == {Sink.in_channel(
            next(i for i, p in enumerate(Sink.INPUTS) if p[:2] == xy))}, (
            f"sink cell {xy} generated channels {sorted(channels)} -- both "
            f"faces of a cell must drive the same one")
    assert len(set().union(*by_cell.values())) == 4, (
        "two sink cells share a channel; they must drain independently")
    ok("generated channel tables match in_channel()/out_channel()")


def test_glsl_unrotate_face_inverts_the_placement():
    # Looking up a port's *channel* needs the authored face, not the placed
    # one -- the masks only answer "is this a port?", which is
    # orientation-independent, but a channel index is not. unrotate_face()
    # must invert exactly what the masks apply, in the opposite order.
    assert "int unrotate_face(int placed, int facing, bool flipped)" in _src
    _body = _re.search(r"int unrotate_face\([^)]*\)\s*\{(.*?)\n\}",
                       _src, _re.S).group(1)
    assert "(placed - facing + 8) % 4" in _body, (
        "un-rotation must subtract the facing")
    # The flip must be undone *after* the rotation, mirroring flip-then-rotate.
    assert _body.index("placed - facing") < _body.index("flipped"), (
        "unrotate_face must un-rotate before un-flipping, since placement "
        "flips then rotates")
    ok("GLSL unrotate_face() inverts flip-then-rotate in the right order")


def test_probe_reach_spans_the_longest_footprint():
    # The shader's footprint probe must reach far enough to span the largest
    # machine, or a multi-cell body would draw as two separate shapes.
    _reach = int(_re.search(r"#define PROBE_REACH\s+(\d+)", _src).group(1))
    _longest = max(max(c.FOOTPRINT) for c in STRUCTURES)
    assert _reach >= _longest - 1, (
        f"PROBE_REACH {_reach} too small for a {_longest}-cell footprint")
    ok(f"PROBE_REACH {_reach} spans the longest footprint ({_longest})")


# --- the bitfield font and the number formatter ----------------------------
# Text is drawn from a 6x8 bitfield generated out of core/font.py, and numbers
# are formatted in GLSL from a policy that core/font.py also implements in
# Python. That Python implementation is an **oracle**, in the same sense as
# belts.naive_tick(): the shader ships, and the reference exists to disagree
# with it.
#
# That is a real change in what these tests can do. The seven-segment display
# they replaced had no Python counterpart, so it could only be pinned by
# regexes over the shader source -- which can assert a branch *exists* but
# never that it computes the right thing. A formatter bug draws a perfectly
# crisp wrong number, and the pixel tests can only tell that something
# value-dependent appeared, not what it said.


def test_the_font_table_reaches_the_shader_intact():
    # The table is generated, so the risk is not divergence but a generator
    # that emits something the shader cannot index: a count that disagrees
    # with the array length, or a value with bit 31 set (which the shader
    # decodes with >>, and a sign-filled shift lights cells that are clear).
    _count = _glsl_int("FONT_COUNT")
    assert _count == len(font.CHARS), (
        f"FONT_COUNT is {_count} but CHARS has {len(font.CHARS)}")
    assert _glsl_int("FONT_W") == font.FONT_W
    assert _glsl_int("FONT_H") == font.FONT_H

    for _name, _half in (("FONT_LO", 0), ("FONT_HI", 1)):
        _m = _re.search(rf"const int {_name}\[(\d+)\]\s*=\s*int\[\d+\]\((.*?)\)\s*;",
                        _src, _re.S)
        assert _m, f"{_name} table not found in the generated source"
        assert int(_m.group(1)) == _count, (
            f"{_name} is declared [{_m.group(1)}] but FONT_COUNT is {_count}")
        _vals = [int(v.strip(), 0)
                 for v in _m.group(2).split(",") if v.strip()]
        assert len(_vals) == _count, (
            f"{_name} holds {len(_vals)} entries, expected {_count}")
        for _i, _v in enumerate(_vals):
            assert _v == font.FONT[_i][_half], (
                f"{_name}[{_i}] ({font.CHARS[_i]!r}) is 0x{_v:X}, Python "
                f"says 0x{font.FONT[_i][_half]:X}")
            # Bit 31 clear, for the same reason grid words keep it clear.
            assert 0 <= _v < (1 << 24), (
                f"{_name}[{_i}] = 0x{_v:X} exceeds 24 bits")
    ok(f"the {_count}-glyph font table reaches the shader intact")


def test_the_named_codes_agree_with_the_character_table():
    # The formatter names a handful of glyphs by constant. If one of these
    # drifts from CHARS the shader draws the wrong character with no other
    # symptom -- a minus becoming a comma turns "-1" into ",1".
    for _name, _want in [("CODE_DIGIT_0", font.CODE_DIGIT_0),
                         ("CODE_MINUS", font.CODE_MINUS),
                         ("CODE_POINT", font.CODE_POINT),
                         ("CODE_SPACE", font.CODE_SPACE),
                         ("CODE_E", font.CODE_E)]:
        _got = _glsl_int(_name)
        assert _got == _want, f"{_name} is {_got} in GLSL, {_want} in Python"
        # And the code really indexes the character it is named for.
        _ch = {"CODE_DIGIT_0": "0", "CODE_MINUS": "-", "CODE_POINT": ".",
               "CODE_SPACE": " ", "CODE_E": "E"}[_name]
        assert font.CHARS[_want] == _ch, (
            f"{_name} = {_want} indexes {font.CHARS[_want]!r}, not {_ch!r}")
    for _name, _codes in (("NAN", font.CODES_NAN), ("INF", font.CODES_INF)):
        for _i, _code in enumerate(_codes):
            assert _glsl_int(f"CODE_{_name}_{_i}") == _code
    # The digits must be contiguous from CODE_DIGIT_0, because the formatter
    # emits a digit as CODE_DIGIT_0 + value. Reordering CHARS would break
    # every number on the board.
    for _d in range(10):
        assert font.CHARS[font.CODE_DIGIT_0 + _d] == str(_d), (
            "the ten digits must be contiguous in CHARS from CODE_DIGIT_0 -- "
            "the formatter emits CODE_DIGIT_0 + value")
    ok("the formatter's named glyph codes index the characters they name")


#: Structural facts about each digit's shape, independent of the bitfield
#: itself: (lit cells, number of enclosed holes, symmetric top-to-bottom).
#:
#: These exist because every other check in this file compares the font
#: against *itself*. The table is generated from core/font.py and the pixel
#: tests identify a rendered glyph by matching it back to that same table, so
#: swapping two glyphs' shapes is self-consistent and passes everything --
#: mutation-tested, and it did. A "2" drawn where a "5" belongs is the worst
#: failure a number display can have, and nothing caught it.
#:
#: Holes are the discriminating property and are cheap to count: 0 has one,
#: 8 has two, 2 and 5 have none. Described here rather than derived so this
#: is a genuinely separate statement of what each digit looks like.
#: Note the 0 has *two* holes, not one: it is a slashed zero, and the
#: diagonal divides its bowl. That is deliberate -- an unslashed 0 and a
#: capital O are the same shape at this resolution, and both are in the font.
_DIGIT_SHAPE = {
    "0": (2, False), "1": (0, False), "2": (0, False), "3": (0, False),
    "4": (1, False), "5": (0, False), "6": (1, False), "7": (0, False),
    "8": (2, True),  "9": (1, False),
}


def _holes(ch):
    """Enclosed background regions in `ch`'s glyph.

    Flood-fills the clear cells from the border; whatever clear cells are
    left unreached are enclosed, and each connected group is one hole.
    """
    lo, hi = font.FONT[font.CHARS.index(ch)]

    def lit(x, y):
        i = y * font.FONT_W + x
        return bool((lo >> i) & 1) if i < 24 else bool((hi >> (i - 24)) & 1)

    w, h = font.FONT_W, font.FONT_H
    seen = set()
    stack = [(x, y) for x in range(w) for y in (0, h - 1) if not lit(x, y)]
    stack += [(x, y) for y in range(h) for x in (0, w - 1) if not lit(x, y)]
    while stack:
        x, y = stack.pop()
        if (x, y) in seen or not (0 <= x < w and 0 <= y < h) or lit(x, y):
            continue
        seen.add((x, y))
        stack += [(x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)]
    # Whatever clear cells the border fill did not reach are enclosed.
    inner = {(x, y) for x in range(w) for y in range(h)
             if not lit(x, y) and (x, y) not in seen}
    holes = 0
    while inner:
        holes += 1
        stack = [inner.pop()]
        while stack:
            x, y = stack.pop()
            for n in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
                if n in inner:
                    inner.discard(n)
                    stack.append(n)
    return holes


def _row_has_enclosed_cell(rows, y):
    """Does row `y` of an authored shape contain an enclosed clear cell?

    Enclosed meaning unreachable from the glyph's border -- the same notion
    `_holes` counts, restricted to one row so a bowl can be located
    vertically.
    """
    h, w = len(rows), len(rows[0])
    lit = lambda x, yy: rows[yy][x] == "#"
    seen = set()
    stack = [(x, yy) for x in range(w) for yy in (0, h - 1) if not lit(x, yy)]
    stack += [(x, yy) for yy in range(h) for x in (0, w - 1) if not lit(x, yy)]
    while stack:
        x, yy = stack.pop()
        if (x, yy) in seen or not (0 <= x < w and 0 <= yy < h) or lit(x, yy):
            continue
        seen.add((x, yy))
        stack += [(x + 1, yy), (x - 1, yy), (x, yy + 1), (x, yy - 1)]
    return any(not lit(x, y) and (x, y) not in seen for x in range(w))


def test_each_digit_has_the_shape_its_name_implies():
    # The independent check: every other font test compares the table to
    # itself, so this describes the digits from outside it.
    for _ch, (_want_holes, _want_sym) in _DIGIT_SHAPE.items():
        _got = _holes(_ch)
        assert _got == _want_holes, (
            f"the glyph for {_ch!r} encloses {_got} hole(s), expected "
            f"{_want_holes} -- is it actually the shape of a {_ch}?")

        # Vertical symmetry separates 8 from everything else with holes, and
        # catches a 6/9 swap, which hole-counting alone cannot.
        _rows = font._SHAPES[_ch]
        _sym = list(_rows[:7]) == list(reversed(_rows[:7]))
        assert _sym == _want_sym, (
            f"{_ch!r} is {'' if _sym else 'not '}symmetric top-to-bottom, "
            f"expected {'' if _want_sym else 'not '}symmetric")

    # 6 and 9 are each other's vertical mirror, which is what makes them the
    # easiest pair to swap and the hardest to notice.
    _six = list(font._SHAPES["6"][:7])
    _nine = list(font._SHAPES["9"][:7])
    assert _six != _nine, "6 and 9 must be different glyphs"
    # And the 6's bowl is at the bottom, the 9's at the top. Located by
    # where the *enclosed hole* sits, which is what the bowl actually is --
    # a "widest row" proxy was tried first and is not reliable, since the
    # crossbar of a 9 is as wide as its bowl.
    for _ch, _want_low in (("6", True), ("9", False)):
        _rows = font._SHAPES[_ch]          # authored top-down
        _ink = [y for y, r in enumerate(_rows) if "#" in r]
        _mid = (min(_ink) + max(_ink)) / 2.0
        _enclosed = [y for y, r in enumerate(_rows)
                     if _row_has_enclosed_cell(_rows, y)]
        assert _enclosed, f"{_ch!r} has no enclosed bowl at all"
        _centre = sum(_enclosed) / len(_enclosed)
        # Rows are top-down here, so a *larger* index is lower on the glyph.
        assert (_centre > _mid) == _want_low, (
            f"{_ch!r}'s bowl sits at row {_centre:.1f} against a midline of "
            f"{_mid:.1f} -- 6 closes at the bottom and 9 at the top")
    # 2 and 5 are the other dangerous pair: both have no holes and neither is
    # symmetric, so the checks above cannot separate them. They are vertical
    # mirrors of each other in this font -- a 5 has its full bar on top and
    # closes bottom-right, a 2 has its full bar on the bottom and opens
    # top-left -- which is exactly why swapping them is easy and invisible.
    # Mutation-tested: the swap passed everything until this.
    for _ch, _want_top in (("2", False), ("5", True)):
        _rows = font._SHAPES[_ch][:7]
        _full = [y for y, r in enumerate(_rows)
                 if r.count("#") >= font.FONT_W - 1]
        assert _full, f"{_ch!r} has no full-width bar"
        # Rows are authored top-down, so a small index is the top.
        _on_top = min(_full) < len(_rows) / 2.0
        assert _on_top == _want_top, (
            f"{_ch!r}'s full-width bar is on the "
            f"{'top' if _on_top else 'bottom'} -- a 5 bars across the top "
            f"and a 2 across the bottom, so this glyph is the other digit")

    # 0 and O are the pair this font has to keep apart, since both are in it
    # and at 6x8 an unslashed zero is identical to the letter. The slash is
    # what separates them, and the hole count is how that shows up.
    assert _holes("0") == 2 and _holes("O") == 1, (
        f"0 encloses {_holes('0')} holes and O encloses {_holes('O')} -- "
        f"the zero's slash is what tells them apart")
    assert font.glyph_bits("0") != font.glyph_bits("O")
    ok("each digit's glyph has the holes, symmetry and bar placement its "
       "name implies; 0/O and 2/5 are distinguishable")


def test_the_formatter_policy_matches_the_python_reference():
    # The fixed/scientific boundary is the one piece of policy that must be
    # identical on both sides: the Python reference is only an oracle if it
    # switches format where the shader does.
    assert _glsl_int("NUM_FIXED_HI_E") == font.FIXED_HI_E, (
        "NUM_FIXED_HI_E and font.FIXED_HI_E must agree")
    assert _glsl_int("NUM_FIXED_LO_E") == font.FIXED_LO_E, (
        "NUM_FIXED_LO_E and font.FIXED_LO_E must agree")
    ok(f"the formatter switches to scientific outside "
       f"[1e{font.FIXED_LO_E}, 1e{font.FIXED_HI_E}) on both sides")


def test_every_string_the_formatter_can_produce_is_drawable():
    # A character with no glyph is silently skipped by the shader, which
    # would turn "-1" into "1" -- a sign error with no visual artefact. Sweep
    # the formatter over the value classes that reach it and check each
    # character resolves to a code.
    _values = [0.0, -0.0, 1.0, -1.0, 2.5, -2.5, 3.14159, 9.999, 0.999,
               -0.5, 100.0, 1234567.0, 5e7, -5e7, 1e-9, -1e-9, 9.96e9,
               float("nan"), float("inf"), float("-inf")]
    # Include the values that *maximise* length, not just representative
    # ones: a negative at the top of the fixed range with every decimal kept
    # is the longest string the formatter can emit, and it is the case that
    # would overflow the shader's fixed buffer. The first version of this
    # test sampled only tidy values and missed it -- NUM_MAX_CODES was 10
    # against a true worst case of 13, so a big untrimmed value would have
    # been silently truncated.
    _values = _values + [-1234567.0, 9999999.0, -9999999.0, -9.96e9]
    _bound = _glsl_int("NUM_MAX_CODES")
    _seen = set()
    _worst = (0, "")
    for _v in _values:
        for _sig in (0, 1, 2, 3, 4):
            for _trim in (True, False):
                _text = font.format_number(_v, _sig, _trim)
                _codes = font.codes_for(_text)       # raises if undrawable
                assert len(_codes) <= _bound, (
                    f"{_text!r} is {len(_codes)} codes but NUM_MAX_CODES is "
                    f"{_bound} -- the shader would silently truncate it")
                if len(_text) > _worst[0]:
                    _worst = (len(_text), _text)
                _seen.update(_text)
    # The bound must also match the arithmetic the shader's comment states,
    # so a change to the fixed range or the digit cap is caught here rather
    # than by whichever value happens to be sampled.
    _longest = 1 + font.FIXED_HI_E + 1 + 4      # sign, ints, point, decimals
    assert _bound >= _longest, (
        f"NUM_MAX_CODES is {_bound} but the policy allows {_longest} "
        f"characters (sign + {font.FIXED_HI_E} integer digits + point + 4 "
        f"decimals)")
    ok(f"every formatter output is drawable and fits NUM_MAX_CODES={_bound} "
       f"(longest {_worst[1]!r}, {_worst[0]} chars; {len(_seen)} distinct "
       f"characters)")


def test_a_trimmed_number_drops_its_trailing_zeros():
    # The headline behaviour: -1.00 must read as -1. Pinned here as well as
    # in font.py's own self-check because this is the property the whole
    # change was for, and a regression would be a silent cosmetic one.
    for _value, _want in [(-1.0, "-1"), (1.0, "1"), (2.5, "2.5"),
                          (-2.5, "-2.5"), (0.0, "0"), (12.0, "12")]:
        _got = font.format_number(_value, sig=2, trim=True)
        assert _got == _want, f"{_value} formats as {_got!r}, want {_want!r}"
    # And the flag is what does it, rather than the rounding happening to.
    assert font.format_number(-1.0, sig=2, trim=False) == "-1.00"
    # A minus is now a column of its own, which is what trimming paid for.
    assert font.format_number(-1.0).startswith("-")
    ok("trimming turns -1.00 into -1 and the sign gets its own column")


def test_the_item_label_is_allowed_to_spill_but_is_still_found():
    # The number may reach past the disc rim, which means two things the old
    # layout did not need and which are easy to get wrong together:
    #
    #  - the per-pixel scan's `reach` must cover the text, not just the disc,
    #    or an item whose digits touch this cell is never considered;
    #  - the coverage written out must be the *union* of disc and text, or
    #    the overhang is clipped back to the circle it was allowed to leave.
    #
    # Both are shader-source properties with no Python counterpart, so they
    # are pinned structurally. A pixel test covers the visible consequence.
    _body = _re.search(r"vec4 draw_items\([^)]*\)\s*\{(.*?)\n\}",
                       _src, _re.S).group(1)
    assert "text_half" in _body, (
        "draw_items() must widen its reach to cover a spilling number")
    assert _re.search(r"reach\s*=.*max\s*\(\s*ITEM_RADIUS\s*,\s*text_half",
                      _body), (
        "reach must be the wider of the disc radius and the text half-width")
    # No early-out on an uncovered pixel: that is exactly where the overhang
    # lives. Mutation-testing this restores the old `if (cov.x <= 0.0)`.
    assert not _re.search(r"if\s*\(\s*cov\.x\s*<=\s*0\.0\s*\)\s*continue",
                          _body), (
        "draw_items() must not skip pixels the disc misses -- the number is "
        "allowed to spill past the rim and would be clipped")
    assert _re.search(r"max\s*\(\s*cov\.x\s*,\s*ink\s*\)", _body), (
        "coverage must be the union of the disc and the number's ink")
    ok("a spilling item label widens the scan reach and the coverage union")


def test_a_source_draws_its_emitted_value_from_the_row():
    # The Source's number comes straight out of the entity SSBO, which is
    # what makes it free: no new upload, no per-instance state anywhere else.
    # The offset must be generated from Source.F_EMIT rather than spelled as
    # F_IN_VALUE in the shader -- the two are equal only because a generator
    # has no input channels, and a hand-written alias would silently read a
    # channel if F_EMIT ever moves.
    from core.entity import Source
    _got = _glsl_int("F_EMIT")
    assert _got == Source.F_EMIT, (
        f"F_EMIT is {_got} in GLSL, {Source.F_EMIT} in Python")
    assert not _re.search(r"ent_field\(\s*id\s*,\s*F_IN_VALUE\s*\)", _src), (
        "the Source's emit value must be read via F_EMIT, not by spelling "
        "out F_IN_VALUE -- they coincide only while Source has no inputs")
    _body = _re.search(r"ent_field\(\s*id\s*,\s*F_EMIT\s*\)", _src)
    assert _body, "the renderer must read F_EMIT out of the entity row"
    # Source has no input channels, which is the precondition for F_EMIT
    # squatting on that slot and so for this read being safe at all.
    assert Source.in_channels() == 0, (
        "Source gained an input channel -- F_EMIT now aliases it and the "
        "renderer would draw a channel value as the emit value")
    ok("a Source's emitted value is read from its row via generated F_EMIT")


def test_the_source_number_does_not_rotate_with_the_facing():
    # Text has a reading orientation a glyph does not, so a south-facing
    # Source must still read left-to-right. `pf` is footprint-centred and
    # axis-aligned to the world, so the fix is to *not* rotate it -- which
    # means the test is that no rotation is applied, and that is only
    # meaningful if the surrounding code is the kind that would.
    _m = _re.search(r"bool show_emit\s*=(.*?)\n            \} else \{",
                    _src, _re.S)
    assert _m, "the Source emit-value branch was not found"
    _branch = _m.group(1)
    assert "sd_number" in _branch, "the branch must draw a formatted number"
    assert "rotate_to_dir" not in _branch and "unrotate" not in _branch, (
        "the Source's number must not be rotated into its facing -- text "
        "has a reading orientation and pf is already world-aligned")
    # A fallback glyph at small sizes, so a zoomed-out Source still reads as
    # a generator rather than as an empty box.
    assert "sd_glyph" in _branch, (
        "below the legibility cutoff the Source must fall back to its glyph")
    ok("the Source's number reads left-to-right at every facing, with a "
       "glyph fallback when small")
