"""Emit the per-type GLSL tables from the Python entity definitions.

The shaders need to know, per entity type: its footprint, which faces of
which cell are ports, which axes pass a signal through unchanged, and what
colour and glyph to draw. All of that is already declared on the `Entity`
subclasses. Writing it a second time in GLSL by hand is what this module
exists to avoid.

**Why generate rather than pin.** The two copies used to be kept in step by
assertions in test_drive.py, and the mechanism failed twice: once when a
rotated structure's ports landed on interior edges, and once when a flipped
machine's control overlay pointed the wrong way. Both were "GLSL disagrees
with Python about a type" -- a class of bug that a generator makes
structurally impossible rather than merely detectable. Simulation will add
more per-type data (throughput, transforms, buffer sizes), and every field
would otherwise need the same hand-mirroring and the same tests.

The output is spliced into `shaders/common.glsl` at load time by
render/shader_loader.py, which replaces the

    // @generated types

marker. Nothing is written to disk: the shader source on disk keeps the
marker, so there is no generated file to get stale or to commit.
"""

from core import cell
from core.entity import Entity

#: Glyph names usable in `Entity.GLYPH`, mapped to the GLSL constants in
#: common.glsl. Keep in step with the GLYPH_* block there.
GLYPHS = {
    "none": "GLYPH_NONE",
    "plus": "GLYPH_PLUS",
    "cross": "GLYPH_CROSS",
    "zed": "GLYPH_ZED",
    "dot": "GLYPH_DOT",
    # Abstract marks rather than text. "1/x" and "MX" are multi-character
    # strings, and glyph rendering here is a hand-drawn SDF per mark -- a
    # bitmap font is its own project (see the Deferred notes). A divide sign
    # and a pair of chevrons read at a 2x1's size, where lettering would not.
    "divide": "GLYPH_DIVIDE",
    "chevrons": "GLYPH_CHEVRONS",
    "arrow_down": "GLYPH_ARROW_DOWN",
    "wye": "GLYPH_WYE",
    "wye_down": "GLYPH_WYE_DOWN",
}

#: Expanded to the type id constants. Must appear before anything that
#: names a TYPE_* value.
MARKER_IDS = "// @generated type-ids"
#: Expanded to the per-type tables. Must appear *after* FACE() and the
#: GLYPH_* constants, which the generated bodies reference.
MARKER_TABLES = "// @generated type-tables"


def _emitted_types():
    """Registered types with a type id, in id order.

    Sorted so the generated source is stable: an unstable ordering would
    change the shader text run to run and defeat any caching, and makes
    diffing generated output pointless.
    """
    return [cls for _tid, cls in sorted(Entity._registry.items())]


#: Direction constant -> its GLSL name. Indexed by the DIR_* values, so a
#: reordering in cell.py shows up here rather than silently mismatching.
_DIR_GLSL = {
    cell.DIR_PX: "DIR_PX",
    cell.DIR_PY: "DIR_PY",
    cell.DIR_NX: "DIR_NX",
    cell.DIR_NY: "DIR_NY",
}


def _face_expr(mask):
    """A face bitmask as a GLSL expression, or '0'."""
    names = [f"FACE({_DIR_GLSL[d]})" for d in range(4) if mask >> d & 1]
    return " | ".join(names) if names else "0"


def _type_const(entity_cls):
    """The GLSL name of a type's id constant, e.g. TYPE_SHEAR."""
    return "TYPE_" + cell.TYPE_NAMES[entity_cls.TYPE_ID].upper()


def _fn(signature, body_lines, fallback):
    """An if-chain function. Empty bodies collapse to just the fallback."""
    out = [signature + " {"]
    out.extend("    " + line for line in body_lines)
    out.append(f"    return {fallback};")
    out.append("}")
    return out


def generate():
    """The GLSL source for every per-type table. Returns a string."""
    types = _emitted_types()
    lines = [
        "// ======================================================================",
        "// GENERATED from core/entity.py by core/glsl_types.py -- do not edit.",
        "//",
        "// Every per-type fact the shaders need comes from the Entity",
        "// subclasses, so a machine is defined in exactly one place. Add a",
        "// type in Python and it appears here automatically.",
        "// ======================================================================",
        "",
    ]

    # --- footprints ---
    body = []
    for c in types:
        if c.FOOTPRINT == (1, 1):
            continue          # the fallback already covers it
        w, h = c.FOOTPRINT
        body.append(f"if (type == {_type_const(c)}) return ivec2({w}, {h});")
    lines += _fn("ivec2 type_footprint(int type)", body, "ivec2(1, 1)")
    lines.append("")

    # --- per-cell port masks ---
    # A port belongs to one cell of a footprint, so these are keyed on the
    # authored local offset as well as the type. See Entity.face_masks_at().
    for which, idx in (("input", 0), ("output", 1)):
        body = []
        for c in types:
            w, h = c.FOOTPRINT
            cells = []
            for lx in range(w):
                for ly in range(h):
                    mask = c.face_masks_at(lx, ly)[idx]
                    if mask:
                        cells.append((lx, ly, mask))
            if not cells:
                continue
            body.append(f"if (type == {_type_const(c)}) {{")
            for (lx, ly, mask) in cells:
                body.append(f"    if (lcl == ivec2({lx}, {ly})) "
                            f"return {_face_expr(mask)};")
            body.append("}")
        lines += _fn(f"int {which}_face_mask_local(int type, ivec2 lcl)",
                     body, "0")
        lines.append("")

    # --- port -> channel index ---
    # Which gate-state slot a port face uses. For every machine but Sink this
    # is just the port's position in OUTPUTS/INPUTS, but the renderer cannot
    # assume that -- it has to look up the same mapping the simulation uses,
    # or a Sink's eight faces would index eight slots that do not exist.
    for which, attr, chan in (("output", "OUTPUTS", "out_channel"),
                              ("input", "INPUTS", "in_channel")):
        body = []
        for c in types:
            ports = getattr(c, attr)
            if not ports:
                continue
            body.append(f"if (type == {_type_const(c)}) {{")
            for i, (lx, ly, face) in enumerate(ports):
                body.append(
                    f"    if (lcl == ivec2({lx}, {ly}) && face == "
                    f"{_DIR_GLSL[face]}) return {getattr(c, chan)(i)};")
            body.append("}")
        lines += _fn(f"int {which}_channel_at(int type, ivec2 lcl, int face)",
                     body, "-1")
        lines.append("")

    # --- control channels ---
    body = []
    for c in types:
        mask = 0
        for d in c.PASS_THROUGH:
            mask |= 1 << d
        if mask:
            body.append(f"if (type == {_type_const(c)}) "
                        f"return {_face_expr(mask)};")
    lines += _fn("int pass_through_mask(int type)", body, "0")
    lines.append("")

    # --- appearance ---
    body = []
    for c in types:
        if c.COLOUR is None:
            continue
        r, g, b = c.COLOUR
        body.append(f"if (type == {_type_const(c)}) "
                    f"return vec3({r:.3f}, {g:.3f}, {b:.3f});")
    # The fallback is a literal rather than COL_BELT: these tables live in
    # common.glsl, which is included by shaders that have no palette of
    # their own. Only machines reach this function, and every machine
    # declares a COLOUR, so the fallback is never actually drawn.
    lines += _fn("vec3 type_colour(int type)", body, "vec3(0.5)")
    lines.append("")

    body = []
    for c in types:
        if c.GLYPH == "none":
            continue
        if c.GLYPH not in GLYPHS:
            raise ValueError(
                f"{c.__name__}.GLYPH is {c.GLYPH!r}; known: {sorted(GLYPHS)}")
        body.append(f"if (type == {_type_const(c)}) "
                    f"return {GLYPHS[c.GLYPH]};")
    lines += _fn("int type_glyph(int type)", body, "GLYPH_NONE")

    return "\n".join(lines)


def type_constants():
    """`const int TYPE_* = n;` for every registered type, plus TYPE_EMPTY.

    Emitted alongside the tables so the id numbering also has a single
    source: cell.TYPE_NAMES. Also carries the entity-row layout, which the
    shader needs to read gate state out of the entity SSBO -- the stride and
    the field offsets are decided in core/entity.py and nowhere else.
    """
    from core.entity import ENTITY_STRIDE, Entity, MAX_CHANNELS

    lines = [
        f"const int ENTITY_STRIDE = {ENTITY_STRIDE};",
        f"const int MAX_CHANNELS = {MAX_CHANNELS};",
        f"const int F_IN_VALUE  = {Entity.F_IN_VALUE};",
        f"const int F_IN_HELD   = {Entity.F_IN_HELD};",
        f"const int F_OUT_VALUE = {Entity.F_OUT_VALUE};",
        f"const int F_OUT_HELD  = {Entity.F_OUT_HELD};",
        f"const int TYPE_EMPTY = {cell.TYPE_EMPTY};",
    ]
    for tid, name in sorted(cell.TYPE_NAMES.items()):
        if tid == cell.TYPE_EMPTY:
            continue
        const = "TYPE_" + name.upper()
        lines.append(f"const int {const} = {tid};")
    return "\n".join(lines)
