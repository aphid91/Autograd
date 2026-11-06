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

from core import cell, glsl_types
from core.entity import STRUCTURES, Underground
from render import shader_loader

from tests.harness import ok

#: The fully resolved shader source, includes and generated tables expanded.
_src = shader_loader.load("world.frag")

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

def test_probe_reach_spans_the_longest_footprint():
    # The shader's footprint probe must reach far enough to span the largest
    # machine, or a multi-cell body would draw as two separate shapes.
    _reach = int(_re.search(r"#define PROBE_REACH\s+(\d+)", _src).group(1))
    _longest = max(max(c.FOOTPRINT) for c in STRUCTURES)
    assert _reach >= _longest - 1, (
        f"PROBE_REACH {_reach} too small for a {_longest}-cell footprint")
    ok(f"PROBE_REACH {_reach} spans the longest footprint ({_longest})")
