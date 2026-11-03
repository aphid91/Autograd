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
"""

import os
import re

SHADER_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "shaders"
)

_INCLUDE_RE = re.compile(r'^[ \t]*#include[ \t]+"([^"]+)"[ \t]*$', re.MULTILINE)


class ShaderError(Exception):
    pass


def shader_path(name):
    return os.path.join(SHADER_DIR, name)


def load(name, _seen=None):
    """Read a shader and splice in its includes. Returns the source text."""
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
        body = load(inc, _seen)
        return f"// ---- begin {inc} ----\n{body}\n// ---- end {inc} ----"

    return _INCLUDE_RE.sub(substitute, src)


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
