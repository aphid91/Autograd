"""Small persistent UI preferences, kept apart from board state.

One flat JSON file holding things the player set in the panel that should
survive a restart -- currently just the quicksave filename. Deliberately not
part of the `.npz`: a save file is a *board*, and the name of the slot you
last typed is not board state. Putting it in the save would also mean the
name changed every time you loaded a different file, which is the opposite of
remembering it.

**Nothing loads this implicitly.** `app.py` calls `load()` at startup and
`save()` when the value changes; `Orchestrator` and `UI` take the value as
plain data. That is not ceremony -- it is what keeps the test harness out of
the player's file. The harness builds a real `Orchestrator`, so a constructor
that read this would have every test run inherit the player's filename, and
a test that then exercised the save path would write wherever the player had
last pointed it. That is exactly the accident this layout prevents: the
quicksave tests pass explicit scratch paths, and with no implicit load there
is no second route to the default.

Failure is always silent and non-fatal. A missing file is the normal first
run, and a corrupt one is worth neither a crash nor a dialog when the only
cost is retyping a filename.
"""

import json
import os

#: Where the preferences live. Alongside imgui.ini, which is the same kind of
#: thing -- runtime UI state, gitignored, not part of the project.
CONFIG_PATH = "gates_config.json"

#: Keys this module knows about, with their defaults. A key absent from the
#: file falls back here, so adding one needs no migration.
DEFAULTS = {
    # Empty string means "use persistence.DEFAULT_SAVE", which is what keeps
    # an untouched text box behaving exactly as the buttons did before it
    # existed. Not None: imgui's text input deals in strings.
    "save_name": "",
    # The customised toolbar: one entry per slot, flat across both rows, each
    # either None for an empty slot or a `describe_builder()` dict. An empty
    # list means "no saved layout", which falls back to `default_builders()`
    # -- so a first run looks exactly as it did before this existed.
    #
    # Unlike `save_name` this is structured data, and it comes from a file the
    # player can edit. `load()` validates its *shape* here and
    # `toolbar_from_data()` validates the contents, because a bad entry must
    # cost one slot rather than the whole layout.
    "toolbar": [],
}


def load(path=CONFIG_PATH):
    """Read preferences, falling back to DEFAULTS for anything missing.

    Never raises. A missing file is the first run; a malformed one is treated
    the same way, since the stakes are one remembered filename.
    """
    out = dict(DEFAULTS)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return out
    if not isinstance(data, dict):
        return out
    for key, default in DEFAULTS.items():
        value = data.get(key, default)
        # Type-check against the default rather than trusting the file: a
        # hand-edited config should not be able to put an int where the UI
        # will hand a str to imgui.
        if isinstance(value, type(default)):
            out[key] = value
    return out


def save(values, path=CONFIG_PATH):
    """Write preferences. Returns True on success, False if it could not.

    Only known keys are written, so an unrecognised entry in `values` cannot
    bloat the file over time.
    """
    data = {k: values[k] for k in DEFAULTS if k in values}
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
    except OSError:
        return False
    return True


def toolbar_to_data(slots):
    """The toolbelt's slots as JSON-safe plain data.

    `slots` is the flat list across both rows, so the saved layout is
    row-agnostic -- which row is "up" is session state, not a preference, and
    persisting it would mean a restart could silently re-aim the number keys.

    A tool that cannot describe itself saves as an empty slot rather than
    being skipped, because **position is the content here**: dropping an
    entry would shift every later tool one slot left on the next launch.
    """
    from core.builders import describe_builder
    return [describe_builder(t) for t in slots]


def toolbar_from_data(data, slot_count):
    """Rebuild a flat slot list from `toolbar_to_data()`.

    Returns a list of exactly `slot_count` entries (tools or None), or None if
    there is no usable layout at all -- which is the signal to fall back to
    the defaults rather than to start with an empty bar.

    **Per-slot validation.** An entry naming a kind or machine this build does
    not have empties that one slot and leaves the rest alone. That is the
    behaviour a hand-edited file and a version change both want: losing one
    tool is obvious and fixable in two clicks, while refusing the whole layout
    would silently discard an arrangement the player built up over sessions.

    A layout longer than the bar is truncated and a shorter one is padded, so
    changing `SLOT_COUNT` does not invalidate a saved file.
    """
    from core.builders import make_builder, stamp_from_data
    if not isinstance(data, list) or not data:
        return None
    out = []
    for item in data[:slot_count]:
        if not isinstance(item, dict):
            out.append(None)
            continue
        kind = item.get("kind")
        stamp = None
        if kind == "stamp":
            stamp = stamp_from_data(item.get("stamp"))
        name = item.get("name")
        tool = make_builder(kind, type_name=item.get("type"), stamp=stamp)
        if tool is not None and kind == "stamp" and isinstance(name, str):
            # A baked stamp's name is the only label the player can have
            # chosen, so it is worth carrying back rather than regenerating
            # it from the bounds.
            tool.name = name
        out.append(tool)
    out.extend([None] * (slot_count - len(out)))
    return out


def resolve_save_path(name, default):
    """Turn whatever is in the filename box into a path to write.

    `name` empty (or whitespace) means the player has not chosen one, so the
    caller's default applies -- that is what makes the box optional rather
    than something you must fill in before the buttons work.

    A bare name gets `.npz` appended, since that is the only format `save()`
    writes and a file called `mybase` would be a surprise. A name that
    already carries any extension is left alone, and so is anything with a
    directory component -- typing a path is a deliberate act and should not be
    second-guessed.
    """
    name = (name or "").strip()
    if not name:
        return default
    head, ext = os.path.splitext(name)
    if ext:
        return name
    return name + ".npz"
