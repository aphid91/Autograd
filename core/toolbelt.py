"""Toolbelt: the Factorio-style slot bar. At most one tool is active.

Ten slots, selected with 1..9,0. Slots beyond the available tools stay visible
but empty, so the bar's shape is stable as tools are added. The UI splits it
visually between slots 5 and 6.

**The default state is an empty hand**, and it is a real state rather than
"an empty slot happens to be selected". `selected` is NO_SELECTION then, no
slot is highlighted, and clicking the board does nothing. That distinction
matters because an empty slot is still a *slot* -- selecting one is a
deliberate act that should look different from holding nothing, and a future
structure-selection tool will live on the empty hand rather than on slot 7.
"""

SLOT_COUNT = 10
SPLIT_AFTER = 5  # the visual gap falls between this slot and the next

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
        """Select a slot by index. Empty slots are selectable (tool = None)."""
        if 0 <= index < SLOT_COUNT:
            self.selected = index
            return True
        return False

    def select_tool_for(self, entity_cls):
        """Select the slot whose builder places `entity_cls`, if there is one.

        The pipette's half of the lookup: Q names a type from the board and
        this finds the tool that builds it. Returns the builder, or None if
        no slot holds one -- a type can exist on the board without being
        placeable, so this must fail softly.
        """
        for i, tool in enumerate(self.slots):
            if tool is None:
                continue
            if getattr(tool, "entity_cls", None) is entity_cls:
                self.selected = i
                return tool
        return None

    def select_key(self, digit):
        """Map a number-row digit to a slot: 1..9 -> 0..8, 0 -> 9."""
        if digit == 0:
            return self.select(SLOT_COUNT - 1)
        if 1 <= digit <= 9:
            return self.select(digit - 1)
        return False

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
