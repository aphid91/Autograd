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

from core import belt_device, belts, cell, entity, glsl_types
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
    """An integer constant parsed back out of the shader source."""
    m = _re.search(rf"\b{name}\s*=\s*(0x[0-9A-Fa-f]+|\d+)", _src)
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


# --- the seven-segment number on an item -----------------------------------
# Three columns of calculator digits, formatted entirely in GLSL. There is no
# Python counterpart to compare against -- the shader is the only
# implementation -- so these pin the *rules* instead: the bar layout, the two
# format branches, and the three rounding edge cases that a plausible-looking
# render hides.
#
# Why here and not in the pixel tests: a formatter bug draws a perfectly
# crisp wrong number. Mutation testing showed exactly that -- disabling the
# scientific branch, dropping the decimal point, and removing both rounding
# carries all left every framebuffer assertion passing, because each still
# produced three well-formed glyphs on a disc.

#: Masks the shader must use, derived from the bar numbering in its own
#: comment: bit 0 top, 1 upper-right, 2 lower-right, 3 bottom, 4 lower-left,
#: 5 upper-left, 6 middle.
_SEG_EXPECTED = {
    0: 0b0111111, 1: 0b0000110, 2: 0b1011011, 3: 0b1001111, 4: 0b1100110,
    5: 0b1101101, 6: 0b1111101, 7: 0b0000111, 8: 0b1111111, 9: 0b1101111,
}


def test_seven_segment_digit_masks_are_right():
    # A wrong mask draws a wrong digit crisply, which no pixel test here
    # would call a failure -- it checks that digits differ, not what they
    # say. The masks are the ground truth for the whole display.
    _m = _re.search(r"SEG_DIGITS\[10\]\s*=\s*int\[10\]\((.*?)\)\s*;",
                    _src, _re.S)
    assert _m, "SEG_DIGITS table not found in the shader"
    _vals = [int(v.strip(), 0) for v in
             _re.findall(r"(0x[0-9A-Fa-f]+|\d+)\s*(?=,|$)",
                         _re.sub(r"//[^\n]*", "", _m.group(1)))]
    assert len(_vals) == 10, f"expected 10 digit masks, parsed {len(_vals)}"
    for _d, _want in _SEG_EXPECTED.items():
        assert _vals[_d] == _want, (
            f"digit {_d} is mask 0x{_vals[_d]:02X}, expected "
            f"0x{_want:02X} -- it would draw as a different digit")
    # A "1" must be exactly two bars and an "8" all seven: the display's
    # legibility rests on those being distinguishable.
    assert bin(_vals[1]).count("1") == 2, "a 1 must light exactly two bars"
    assert bin(_vals[8]).count("1") == 7, "an 8 must light all seven bars"
    ok("all ten seven-segment digit masks match the bar numbering")


def test_the_exponent_sign_uses_a_distinct_glyph():
    # With only three columns there is no room for a separate sign, so the
    # exponent's sign rides on the E. The two glyphs must be *different
    # shapes*, or 5e7 and 5e-7 render identically -- a silent factor of
    # 10^14.
    # These are #defines, not consts, so _glsl_int (which looks for
    # "= value") does not see them. One may be defined as another's name.
    def _define(name, depth=0):
        m = _re.search(rf"#define\s+{name}\s+(\S+)", _src)
        assert m, f"#define {name} not found in shader source"
        val = m.group(1)
        if _re.fullmatch(r"0x[0-9A-Fa-f]+|\d+", val):
            return int(val, 0)
        assert depth < 4, f"#define {name} chains too deep"
        return _define(val, depth + 1)

    _e = _define("SEG_E")
    _c = _define("SEG_E_NEG")
    assert _e != _c, "SEG_E and SEG_E_NEG must differ, or the sign is lost"
    assert _c != 0, "SEG_E_NEG must draw something"
    # They must differ by *several* bars, not one. An "E" and a "C" differ by
    # a single bar out of four and are nearly the same shape at the few
    # pixels these are drawn at -- the whole-frame difference between 1e9 and
    # 1e-9 was 16 pixels, which is not a legible distinction for a value
    # eighteen orders of magnitude apart. A bare minus (one bar) against an E
    # (four) is unmistakable.
    _differing = bin(_e ^ _c).count("1")
    assert _differing >= 3, (
        f"SEG_E (0x{_e:02X}) and SEG_E_NEG (0x{_c:02X}) differ in only "
        f"{_differing} bar(s) -- too alike to tell apart at item size")
    ok(f"a negative exponent uses a clearly distinct glyph "
       f"(0x{_e:02X} vs 0x{_c:02X}, {_differing} bars differ)")


def test_item_number_has_both_format_branches():
    _body = _re.search(r"float item_number\([^)]*\)\s*\{(.*?)\n\}",
                       _src, _re.S).group(1)
    # Scientific notation must actually be reachable. Disabling this branch
    # leaves every pixel test passing, since a fixed-point fallback still
    # draws three plausible glyphs for a huge value.
    assert "NUM_FIXED_HI" in _body and "NUM_FIXED_LO" in _body, (
        "the format must be chosen by comparing the exponent against both "
        "NUM_FIXED_HI and NUM_FIXED_LO")
    assert "SEG_E" in _body, "the scientific branch must draw an E"
    # The decimal point belongs to the fixed branch only; a scientific
    # render has no point, so "point" must be conditional.
    assert "sd_seg_point" in _body, "fixed point must draw a decimal point"
    assert _re.search(r"if\s*\(\s*point\s*\)", _body), (
        "the decimal point must be conditional -- a scientific render has "
        "none")
    ok("item_number() has both format branches and a conditional point")


def test_item_number_handles_both_rounding_carries():
    _body = _re.search(r"float item_number\([^)]*\)\s*\{(.*?)\n\}",
                       _src, _re.S).group(1)
    # Carry 1: rounding a mantissa to one digit can reach 10, which is two
    # digits. It must renormalise, not clamp -- clamping shows 9.6e3 as
    # "0E3" or "9E3" rather than "1E4".
    assert _re.search(r"md\s*>=\s*10", _body), (
        "the mantissa must be checked for rounding up to 10")
    assert _re.search(r"md\s*=\s*1\s*;.*?e\s*\+=\s*1", _body, _re.S), (
        "a mantissa that rounds to 10 must become 1 with the exponent "
        "incremented, not be clamped to 9")
    # Carry 2: rounding to two decimals can cross the fixed-point ceiling.
    # 9.996 -> 10.00 needs three integer columns, so it must promote to
    # scientific rather than clamp to 9.99 and report a smaller value.
    assert _re.search(r"1000\.0", _body), (
        "a fixed-point value that rounds past 9.99 must be detected "
        "(scaled >= 1000) and promoted to scientific")
    assert "NUM_FIXED_HI" in _body
    ok("item_number() renormalises both rounding carries")


def test_the_glyph_columns_fit_inside_the_disc():
    # The number is drawn on the face of the item, so the three columns must
    # fit inside the ring -- otherwise digits spill over the rim and read as
    # belt decoration. Checked here rather than by eye because the layout is
    # arithmetic over four tunables that are each independently adjustable.
    import math
    _half_w = float(_re.search(r"#define SEG_HALF_W\s+([\d.]+)", _src).group(1))
    _gap = float(_re.search(r"#define NUM_GAP\s+([\d.]+)", _src).group(1))
    _size = float(_re.search(r"#define ITEM_NUM_SIZE\s+([\d.]+)", _src).group(1))
    _radius = float(_re.search(r"#define ITEM_RADIUS\s+([\d.]+)", _src).group(1))
    _ring = float(_re.search(r"#define ITEM_RING\s+([\d.]+)", _src).group(1))
    _adv = _half_w * 2.0 + _gap
    # Furthest lit corner: the outer edge of an outer column, full height.
    _corner = math.hypot((_adv + _half_w) * _size, 1.0 * _size)
    _inner = _radius - _ring
    assert _corner <= _inner, (
        f"the glyph columns reach {_corner:.4f} cells from the centre but "
        f"the disc's inner radius is {_inner:.4f} -- digits would cross the "
        f"ring. Reduce ITEM_NUM_SIZE or NUM_GAP.")
    ok(f"three glyph columns fit inside the ring "
       f"({_corner:.3f} <= {_inner:.3f} cells)")
