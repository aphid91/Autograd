"""Save and load a board to disk.

One file holds the whole persistent state: the grid, the entity table, and
the row allocator. The format is a single `.npz` -- the two big arrays are
already numpy, so this is a near-verbatim dump rather than a serialisation
layer, and it stays fast enough to sit behind a quicksave button.

**Transient state is deliberately not saved.** Ghosts are real rows in real
cells, and frustration overlays live in `.z`, but both belong to an in-flight
gesture rather than to the board; writing them out would reload a half-drawn
belt run as though it had been built. `save()` therefore refuses to run while
a preview is up -- the caller clears it first (see `Engine.save_board`).

Versioning: `FORMAT_VERSION` is bumped whenever the layout changes in a way
old files cannot satisfy. Loading refuses a version it does not recognise
rather than guessing, since a misread entity table produces entities that
look valid and behave wrongly.
"""

import json

import numpy as np

from core import cell
from core.entity import ENTITY_STRIDE

#: Bumped on any incompatible change to the cell encoding, the entity row
#: layout, or the set of arrays written below.
FORMAT_VERSION = 1

#: What a quicksave is called when the caller does not say.
DEFAULT_SAVE = "quicksave.npz"


class SaveError(Exception):
    """A save file could not be read, or does not fit this board."""


def save(board, path, annotations=None):
    """Write `board` to `path`. Returns the path written.

    The caller is responsible for clearing ghosts first; this asserts rather
    than silently persisting them, because a file containing ghost rows would
    reload as a board with phantom entities nobody placed.

    `annotations` is an `AnnotationStore`, or None. They ship *inside* the
    save rather than in a sidecar: a note is about a particular factory, so
    loading a different board must bring that board's notes and leave none of
    the previous one's floating over it. One file also means a save cannot be
    separated from its annotations by being moved or renamed.

    Stored as a JSON string in a 0-d array, not as more numpy. The arrays
    above are bulk numeric data that `.npz` is exactly right for; this is a
    handful of short records with strings in them, and keeping it readable
    (and trivially versionable) beats packing it into parallel arrays.
    """
    ghosts = int((cell.ghost_state_arr(board.grid[:, :, cell.CH_Z])
                  != cell.GHOST_NONE).sum())
    if ghosts:
        raise SaveError(
            f"refusing to save with {ghosts} ghost/frustrated cells still "
            f"set -- clear the preview first")

    # .w carries belt-segment membership, which is derived from the rest of
    # the grid and whose ids mean nothing without the live SegmentIndex.
    # Saved as zero so a reloaded board is byte-identical to a freshly
    # derived one: the engine rebuilds segments on the next frame either way,
    # and persisting ids that will be reallocated differently is a trap.
    saved_grid = board.grid.copy()
    saved_grid[:, :, cell.CH_W] = 0

    notes = "[]" if annotations is None else json.dumps(annotations.to_data())

    np.savez_compressed(
        path,
        version=np.array([FORMAT_VERSION], dtype=np.int32),
        size=np.array([board.width, board.height], dtype=np.int32),
        grid=saved_grid,
        entities=board.entities,
        # Not in the `missing` check on load: a file written before
        # annotations existed has no such key, and that must read as "no
        # annotations" rather than as a corrupt save.
        annotations=np.array(notes),
        # The allocator is part of the state: entity ids are embedded in grid
        # cells, so the free list and the high-water mark have to come back
        # exactly or a later spawn could hand out a row that is still in use.
        next_row=np.array([board._next_row], dtype=np.int32),
        free_rows=np.array(board._free_rows, dtype=np.int32),
        live_count=np.array([board._live_count], dtype=np.int32),
    )
    return path


def load(board, path, annotations=None):
    """Replace `board`'s contents from `path`. Returns the number of entities.

    The board is mutated in place rather than replaced, because the device
    resources (grid texture, entity SSBO) are bound to it and everything
    upstream holds a reference.

    `annotations`, if given, is replaced from the file's own set -- cleared
    first, so loading a board whose file has no annotations leaves none of the
    previous board's behind. **A file written before annotations existed loads
    as having none**, which is why the key is absent from the `missing` check
    rather than required: every save in existence predates this field, and
    refusing them would be a format break for no reason.
    """
    # np.load on an .npz is lazy and keeps the file open, which on Windows
    # leaves it locked against the next save. Read everything out and close.
    try:
        with np.load(path) as handle:
            data = {k: handle[k] for k in handle.files}
    except (OSError, ValueError) as exc:
        raise SaveError(f"could not read {path}: {exc}") from exc

    missing = {"version", "size", "grid", "entities", "next_row",
               "free_rows", "live_count"} - set(data)
    if missing:
        raise SaveError(f"{path} is missing {sorted(missing)}")

    version = int(data["version"][0])
    if version != FORMAT_VERSION:
        raise SaveError(
            f"{path} is format version {version}, this build reads "
            f"{FORMAT_VERSION}")

    w, h = (int(v) for v in data["size"])
    if (w, h) != (board.width, board.height):
        raise SaveError(
            f"{path} is {w}x{h}, this board is "
            f"{board.width}x{board.height}")

    grid = data["grid"]
    entities = data["entities"]
    if grid.shape != board.grid.shape:
        raise SaveError(f"grid shape {grid.shape} != {board.grid.shape}")
    if entities.ndim != 2 or entities.shape[1] != ENTITY_STRIDE:
        raise SaveError(
            f"entity rows are {entities.shape[1]} wide, expected "
            f"{ENTITY_STRIDE}")

    board.grid[:] = grid.astype(np.int32, copy=False)
    # .w holds belt-segment membership, which is *derived* from the rest of
    # the grid and whose ids are only meaningful alongside the live
    # SegmentIndex. Clear it rather than trusting the file: the engine
    # re-derives segments on the next frame, and a stale id here would have
    # cells pointing at segments that do not exist. Saving it is harmless;
    # believing it is not.
    board.grid[:, :, cell.CH_W] = 0
    # The table may have grown since the save; keep the larger of the two so
    # capacity never shrinks under live rows.
    rows = entities.shape[0]
    # Through resize_entities(), not by assigning a fresh array: the SSBO is
    # reserved at the old capacity and would then be too small for the next
    # to_device(), which moderngl reports as "out of range offset".
    board.resize_entities(rows, keep=False)
    board.entities[:] = 0.0
    board.entities[:rows] = entities.astype(np.float32, copy=False)

    board._next_row = int(data["next_row"][0])
    board._free_rows = [int(r) for r in data["free_rows"]]
    board._live_count = int(data["live_count"][0])
    board._frustrated = []
    # Every row was just overwritten, so the cached live-row list describes
    # the outgoing board. resize_entities() above invalidates it only when the
    # table actually grew, which a same-capacity load does not.
    board._rows_changed()

    if annotations is not None:
        # Cleared unconditionally, then filled from whatever the file has.
        # An older save has no `annotations` key at all, and the clear is what
        # makes that case correct: the new board gets no notes rather than
        # inheriting the outgoing board's.
        annotations.clear()
        raw = data.get("annotations")
        if raw is not None:
            try:
                annotations.load_data(json.loads(str(raw)))
            except (ValueError, TypeError):
                # A corrupt annotation blob costs the notes, not the board.
                # The factory is the irreplaceable half.
                pass

    board.mark_all_dirty()
    return board._live_count
