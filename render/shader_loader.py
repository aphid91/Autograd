"""Load GLSL from shaders/, resolving a minimal #include directive.

GLSL has no include mechanism, so the cell encoding would otherwise have to be
copy-pasted into every shader that reads the grid -- a third copy of the bit
layout to drift out of sync with cell.py. This resolves

    #include "common.glsl"

by textual substitution before compiling, so there is exactly one copy.

Includes are resolved relative to the shaders directory, are processed
recursively, and a file already pulled in is skipped on later encounters (an
implicit include guard), so two shaders can both include common.glsl without
redefining anything.

It also expands the `// @generated types` marker into the per-type tables
built from the Python entity definitions -- see core/glsl_types.py. Doing
that here keeps the whole "one definition of a machine" story inside the
normal load path: there is no generated file on disk to go stale, and F5
picks up a change to core/entity.py with no build step.
"""

import os
import re

from core import glsl_types

SHADER_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "shaders"
)

_INCLUDE_RE = re.compile(r'^[ \t]*#include[ \t]+"([^"]+)"[ \t]*$', re.MULTILINE)
#: The `#version` line, which must stay first in the file -- injected defines
#: go immediately after it.
_VERSION_RE = re.compile(r'^[ \t]*#version[^\n]*$', re.MULTILINE)


class ShaderError(Exception):
    pass


def shader_path(name):
    return os.path.join(SHADER_DIR, name)


def load(name, defines=None, _seen=None):
    """Read a shader and splice in its includes. Returns the source text.

    `defines` is an optional iterable of preprocessor symbols to define at the
    top of the *outermost* file, used for genuinely optional features like the
    segment debug overlay. They go in via `#define` rather than a uniform
    deliberately: an `#ifdef`-guarded block is not compiled at all when the
    symbol is absent, so a debug overlay costs exactly nothing when off --
    whereas a uniform branch would still be evaluated per pixel.
    """
    if _seen is None:
        _seen = set()

    path = shader_path(name)
    try:
        with open(path) as f:
            src = f.read()
    except FileNotFoundError:
        raise ShaderError(f"shader not found: {path}")

    _seen.add(os.path.normcase(os.path.abspath(path)))

    def substitute(match):
        inc = match.group(1)
        inc_abs = os.path.normcase(os.path.abspath(shader_path(inc)))
        if inc_abs in _seen:
            return f"// #include \"{inc}\" (already included)"
        # Mark the boundary so a compile error's line numbers are at least
        # traceable to a file, even though GLSL will renumber them.
        # Defines are injected once, at the outermost file, so an included
        # file does not re-emit them.
        body = load(inc, None, _seen)
        return f"// ---- begin {inc} ----\n{body}\n// ---- end {inc} ----"

    src = _INCLUDE_RE.sub(substitute, src)

    # Inject the requested defines. They must land *after* the #version
    # directive, which GLSL requires to be the first non-comment line, so
    # this splices after that line rather than prepending.
    if defines:
        block = "\n".join(f"#define {d} 1" for d in defines)
        src = _VERSION_RE.sub(
            lambda m: f"{m.group(0)}\n{block}", src, count=1)

    # Splice in the per-type tables generated from core/entity.py. Done here
    # rather than written to a file so there is no generated artefact to get
    # stale, and F5 picks up a Python-side change without a rebuild step.
    # Ids first: they must be defined before anything names a TYPE_* value.
    # The tables come later in the file, after the FACE() macro and the
    # GLYPH_* constants their bodies reference.
    if glsl_types.MARKER_IDS in src:
        src = src.replace(glsl_types.MARKER_IDS, glsl_types.type_constants())
    if glsl_types.MARKER_TABLES in src:
        src = src.replace(glsl_types.MARKER_TABLES, glsl_types.generate())

    return src


def dependencies(name):
    """Every file `name` pulls in, including itself. For hot-reload watching."""
    out = []
    seen = set()

    def walk(n):
        path = os.path.normcase(os.path.abspath(shader_path(n)))
        if path in seen:
            return
        seen.add(path)
        out.append(shader_path(n))
        try:
            with open(shader_path(n)) as f:
                src = f.read()
        except FileNotFoundError:
            return
        for m in _INCLUDE_RE.finditer(src):
            walk(m.group(1))

    walk(name)
    return out
