"""Copy and paste modes: an override on whatever the hand already holds.

Copy and paste are **not toolbelt slots**. They temporarily take over from the
held tool and hand it back when they finish, so ctrl+C in the middle of
drawing a belt run does not lose the belt tool. `Clipboard` owns that
stashing, the mode flag, and the stamp history; the orchestrator drives it and
`core/stamp.py` does the geometry.

    MODE_OFF     normal play, the toolbelt is in charge
    MODE_COPY    left-drag draws a selection rectangle
    MODE_PASTE   the active stamp previews under the cursor

Entering either mode stashes the current slot. Leaving restores it -- except
on `Q`, which is the universal "empty the hand" and therefore drops the stash
rather than returning to it. That asymmetry is deliberate: every other exit is
"I am done with this mode", while Q is "I want to be holding nothing".

Copy mode has no camera pan, because a left-drag is the selection gesture. In
paste mode left-drag still pans, since the stamp follows the cursor and needs
no drag of its own.
"""

from core.stamp import Stamp, StampEntry, StampHistory

MODE_OFF = 0
MODE_COPY = 1
MODE_PASTE = 2

MODE_NAMES = {MODE_OFF: "Off", MODE_COPY: "Copy", MODE_PASTE: "Paste"}


class Clipboard:
    """Mode state, the stashed tool, and the stamp history."""

    def __init__(self, history=None):
        self.mode = MODE_OFF
        self.history = history or StampHistory()
        #: Toolbelt slot to return to when a mode ends normally. None means
        #: the hand was empty, which is a real state and must be restored as
        #: such rather than collapsing to slot 0.
        self._stashed_slot = None
        self._has_stash = False

    # --- queries ---------------------------------------------------------
    @property
    def active(self):
        return self.mode != MODE_OFF

    @property
    def copying(self):
        return self.mode == MODE_COPY

    @property
    def pasting(self):
        return self.mode == MODE_PASTE

    @property
    def stamp(self):
        """The stamp paste mode would place, or None."""
        return self.history.active

    def status(self):
        if self.mode == MODE_COPY:
            return "Copy: drag to select"
        if self.mode == MODE_PASTE:
            s = self.stamp
            if s is None:
                return "Paste: nothing copied"
            n = len(self.history)
            idx = self.history.active_index
            where = f"  [{idx + 1}/{n}]" if n > 1 else ""
            _x, _y, w, h = s.bounds()
            return f"Paste: {len(s)} entities {w}x{h}{where}"
        return ""

    # --- mode transitions ------------------------------------------------
    def enter_copy(self, current_slot):
        """ctrl+C. Stash the held tool and start selecting."""
        self._stash(current_slot)
        self.mode = MODE_COPY
        return True

    def enter_paste(self, current_slot):
        """ctrl+V. Only possible once something has been copied."""
        if not self.history:
            return False
        self._stash(current_slot)
        self.mode = MODE_PASTE
        return True

    def _stash(self, current_slot):
        """Remember the held slot, unless a mode is already holding a stash.

        Switching copy -> paste must not overwrite the stash with copy mode's
        own (empty) selection, or the tool the player started with is lost.
        """
        if not self._has_stash:
            self._stashed_slot = current_slot
            self._has_stash = True

    def exit_to_stash(self):
        """Leave the mode and report the slot to restore.

        Returns (slot_or_None, had_stash). A `None` slot with `had_stash`
        true means "the hand was empty", which is different from no stash
        at all.
        """
        slot, had = self._stashed_slot, self._has_stash
        self.mode = MODE_OFF
        self._stashed_slot = None
        self._has_stash = False
        return slot, had

    def exit_to_empty(self):
        """Q: leave the mode and drop the stash -- the hand becomes empty."""
        self.mode = MODE_OFF
        self._stashed_slot = None
        self._has_stash = False

    # --- the stamp history -----------------------------------------------
    def adopt(self, stamp):
        """A fresh capture becomes the active stamp, and we switch to paste.

        Called with a non-empty stamp only; an empty selection returns the
        player to their tool instead (the orchestrator decides, since only it
        can restore a slot).
        """
        self.history.push(stamp)
        self.mode = MODE_PASTE
        return stamp

    def cycle_stamp(self, delta):
        """shift+scroll in paste mode: walk the history."""
        if self.mode != MODE_PASTE:
            return False
        return self.history.cycle(delta)

    def transform_active(self, rotate=0, mirror=False):
        """Rotate and/or mirror the active stamp in place.

        Mutates the history entry rather than pushing a new one: rotating a
        stamp is adjusting the thing in your hand, not producing a second
        copy. Returns the new stamp, or None if there is nothing to transform.
        """
        s = self.stamp
        if s is None:
            return None
        out = s
        if mirror:
            out = out.mirrored()
        for _ in range(abs(rotate)):
            out = out.rotated(backward=rotate < 0)
        if out is s:
            return s
        return self.history.replace_active(out)


def capture(board, x0, y0, x1, y1, include_ghosts=False):
    """Build a Stamp from every entity touching the rectangle.

    Touching, not contained: a structure with any cell inside the selection
    comes along whole, which is what makes a sloppy drag over a machine do
    what the player meant. The stamp's offsets are relative to the covered
    box, so the capture rectangle itself does not have to be tidy.

    Undergrounds record their partner as an **index into this stamp**, so a
    copied pair re-pairs with each other on paste instead of latching onto
    whatever happens to sit near the paste site. An end whose partner was
    outside the selection comes across unpaired, which is a legal thing to
    have on the board.
    """
    from core import cell as _cell

    lo_x, hi_x = (x0, x1) if x0 <= x1 else (x1, x0)
    lo_y, hi_y = (y0, y1) if y0 <= y1 else (y1, y0)

    # Collect distinct entity rows, so a multi-cell entity is captured once
    # however many of its cells the rectangle covers.
    rows = []
    seen = set()
    for y in range(lo_y, hi_y + 1):
        for x in range(lo_x, hi_x + 1):
            if not board.in_bounds(x, y):
                continue
            view = board.entity_at(x, y)
            if view is None or view.row in seen:
                continue
            if view.is_ghost and not include_ghosts:
                continue
            seen.add(view.row)
            rows.append(view)

    if not rows:
        return Stamp()

    entries = []
    row_to_index = {}
    for view in rows:
        ox, oy = view.origin
        entry = StampEntry(
            type(view), ox, oy, view.direction, bool(view.flipped),
            is_exit=bool(getattr(view, "is_exit", False)),
        )
        row_to_index[view.row] = len(entries)
        entries.append(entry)

    # Resolve underground pairings to stamp-local indices. A partner outside
    # the selection becomes None -- an unpaired end, which is legal.
    for view in rows:
        if view.type_id != _cell.TYPE_UNDER:
            continue
        idx = row_to_index[view.row]
        partner_row = view.partner
        if partner_row and partner_row in row_to_index:
            entries[idx].partner = row_to_index[partner_row]

    stamp = Stamp(entries)
    return stamp.normalise()
