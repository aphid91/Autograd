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
