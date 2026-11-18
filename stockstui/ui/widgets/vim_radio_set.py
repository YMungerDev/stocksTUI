from textual.binding import Binding
from textual.widgets import RadioSet


class VimRadioSet(RadioSet):
    """A RadioSet widget with added Vim-like (h/l, j/k) keybindings for navigation."""

    # Define custom key bindings for Vim-like navigation
    BINDINGS = [
        Binding(
            "down,right,l", "next_button", "Next option", show=False
        ),  # Move to the next radio button
        Binding(
            "enter,space", "toggle_button", "Toggle", show=False
        ),  # Toggle the selected radio button
        Binding(
            "up,left,h", "previous_button", "Previous option", show=False
        ),  # Move to the previous radio button
    ]

    def on_mount(self) -> None:
        """
        Puts the cursor on the pressed button.

        Textual's RadioSet always puts its cursor on the first button when it
        mounts, so a set opening on '1M' showed the '1D' highlight as well
        (#22). Runs after the base class has finished mounting.
        """
        self.call_after_refresh(self.sync_cursor_to_pressed)

    def sync_cursor_to_pressed(self) -> None:
        if self.pressed_index >= 0:
            self._selected = self.pressed_index

