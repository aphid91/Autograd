"""Toolbelt: the Factorio-style slot bar. At most one tool is active.

**Two rows of ten.** The number keys address the *active* row and `Tab` swaps
which row that is, so twenty tools are reachable with one modifier-free key
each. The rows are a view onto one flat `slots` list -- row `r` owns indices
`r*ROW_SIZE .. (r+1)*ROW_SIZE`, and `selected` is an index into that flat list
rather than a (row, column) pair. That matters at every call site: a stashed
slot, a persisted layout and `select_tool_for()`'s answer are all one integer,
and none of them has to agree with the orchestrator about which row is up.

`Tab` therefore changes which digits reach which tools **without changing what
is held.** Swapping rows mid-gesture would otherwise be a silent tool change.

**The default state is an empty hand**, and it is a real state rather than
"an empty slot happens to be selected". `selected` is NO_SELECTION then, no
slot is highlighted, and clicking the board inspects rather than builds. That
distinction matters because an empty slot is still a *slot* -- selecting one
is a deliberate act that should look different from holding nothing, and the
inspect gesture lives on the empty hand rather than on a slot.
"""

#: Slots per row, and the number of rows. Ten per row because that is what the
#: number row addresses. Two places assume it: `ui/input_manager.py` maps a
#: digit to a column, and `Orchestrator._select_column()` adds the active
#: row's offset. The split is deliberate -- the input layer reports which
#: digit was pressed and the orchestrator decides which slot that reaches,
#: the same division that has input report `R` rather than what `R` means.
ROW_SIZE = 10
ROW_COUNT = 2

#: Total slots. Kept as the old name because it is what the panel's layout and
#: the input manager's digit clamp both mean by "how many slots are there".
SLOT_COUNT = ROW_SIZE * ROW_COUNT

#: The visual gap, within a row, falls between this column and the next.
SPLIT_AFTER = 5

#: `selected` when the hand is empty. Not a valid slot index.
NO_SELECTION = -1


class Toolbelt:
    def __init__(self, builders=()):
        self.slots = [None] * SLOT_COUNT
        for i, b in enumerate(builders[:SLOT_COUNT]):
            self.slots[i] = b
        # The hand starts empty, Factorio-style: nothing is held until the
        # player picks something up.
        self.selected = NO_SELECTION
        #: Which row the number keys address. Independent of `selected`, so
        #: swapping rows never changes what is held.
        self.row = 0

    # --- rows ------------------------------------------------------------
    @property
    def row_start(self):
        """Flat index of the active row's first slot."""
        return self.row * ROW_SIZE

    def swap_rows(self):
        """Tab: make the other row the one the number keys address.

        **Holds nothing and drops nothing.** The hand keeps whatever it has,
        even when that tool now sits on the inactive row -- a row swap is a
        change of *addressing*, not of state, and silently clearing the hand
        (or jumping to the same column in the new row) would make Tab a
        destructive key mid-gesture.
        """
        self.row = (self.row + 1) % ROW_COUNT
        return True

    def row_of(self, index):
        """Which row a flat slot index belongs to."""
        return index // ROW_SIZE

    # --- queries ---------------------------------------------------------
    @property
    def active(self):
        """The active builder, or None for an empty hand or an empty slot."""
        if self.selected == NO_SELECTION:
            return None
        return self.slots[self.selected]

    @property
    def empty_handed(self):
        """True when nothing at all is held.

        Distinct from `active is None`, which is also true for a selected but
        empty slot. Callers that care about the *hand* rather than about
        having a builder want this one.
        """
        return self.selected == NO_SELECTION

    def clear(self):
        """Empty the hand. Always succeeds, so it reports no change flag."""
        self.selected = NO_SELECTION

    def select(self, index):
        """Select a slot by flat index. Empty slots are selectable (tool=None).

        Takes a flat index rather than a column, so a caller restoring a
        stashed slot or applying a persisted layout needs to know nothing
        about which row is currently up.
        """
        if 0 <= index < SLOT_COUNT:
            self.selected = index
            return True
        return False

    def set_slot(self, index, builder):
        """Put `builder` in a slot, or None to empty it. For the type menu.

        Does not change the selection: re-assigning the slot you are holding
        should leave you holding that slot, now with the new tool in it.
        """
        if not 0 <= index < SLOT_COUNT:
            return False
        self.slots[index] = builder
        return True

    def select_tool_for(self, entity_cls):
        """Select the slot whose builder places `entity_cls`, if there is one.

        The pipette's half of the lookup: Q names a type from the board and
        this finds the tool that builds it. Returns the builder, or None if
        no slot holds one -- a type can exist on the board without being
        placeable, so this must fail softly.

        **Scans both rows, and switches rows to reach a match.** Q's job is
        "give me the tool for this thing"; failing because the tool happens to
        sit on the inactive row would be a surprise, and leaving the row alone
        after selecting across it would leave the number keys addressing a row
        the highlighted slot is not in.

        The active row is searched **first**, so a type with tools on both
        rows resolves to the one whose digits are already live. Within a row
        it is first match wins, which is a real rule rather than an accident:
        two tools can legitimately place the same type (the belt planner and
        the raw belt tool both place `Belt`), so Q's answer depends on slot
        order -- the earliest slot offering the type.
        """
        order = (list(range(self.row_start, self.row_start + ROW_SIZE))
                 + [i for i in range(SLOT_COUNT)
                    if self.row_of(i) != self.row])
        for i in order:
            tool = self.slots[i]
            if tool is None:
                continue
            if getattr(tool, "entity_cls", None) is entity_cls:
                self.selected = i
                self.row = self.row_of(i)
                return tool
        return None

    def scroll(self, delta):
        """Scroll through slots, wrapping. Positive scrolls toward slot 1.

        Wraps rather than clamping so a long scroll cannot strand you on an
        end slot, matching how a hotbar normally behaves.

        Currently unbound: the scroll wheel zooms the camera, Factorio-style,
        and slots are selected with the number keys. Kept for whenever slot
        cycling wants a binding (shift+scroll, say).
        """
        if not delta:
            return False
        step = -1 if delta > 0 else 1
        self.selected = (self.selected + step) % SLOT_COUNT
        return True

    def occupied(self):
        return [(i, s) for i, s in enumerate(self.slots) if s is not None]
