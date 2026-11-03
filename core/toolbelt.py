"""Toolbelt: the Factorio-style slot bar. Exactly one tool is active.

Ten slots, selected with 1..9,0 or the scroll wheel. Slots beyond the
available tools stay visible but empty, so the bar's shape is stable as tools
are added. The UI splits it visually between slots 5 and 6.
"""

SLOT_COUNT = 10
SPLIT_AFTER = 5  # the visual gap falls between this slot and the next


class Toolbelt:
    def __init__(self, builders=()):
        self.slots = [None] * SLOT_COUNT
        for i, b in enumerate(builders[:SLOT_COUNT]):
            self.slots[i] = b
        # Start on the first occupied slot, if any.
        self.selected = next(
            (i for i, s in enumerate(self.slots) if s is not None), 0
        )

    @property
    def active(self):
        """The active builder, or None if the selected slot is empty."""
        return self.slots[self.selected]

    def select(self, index):
        """Select a slot by index. Empty slots are selectable (tool = None)."""
        if 0 <= index < SLOT_COUNT:
            self.selected = index
            return True
        return False

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
        """
        if not delta:
            return False
        step = -1 if delta > 0 else 1
        self.selected = (self.selected + step) % SLOT_COUNT
        return True

    def occupied(self):
        return [(i, s) for i, s in enumerate(self.slots) if s is not None]
