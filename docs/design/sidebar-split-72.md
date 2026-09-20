# Sidebar section sizing (revision 72)

The divider above task shortcuts is now a draggable horizontal splitter. Moving
it upward gives shortcuts more space; profiles scroll in their own viewport.
Shortcuts also have an independent scrollbar when their content overflows.

- Drag the divider, or focus it and use Up/Down (16 DIP per step).
- Double-click to restore the default 65% profile / 35% shortcut allocation.
- Profile and shortcut sections retain minimum heights of 96 and 112 DIP.
- The final ratio is saved in `work/control-center/sidebar-layout.json` after a
  completed adjustment, then restored at launch. Window resizing does not write
  preferences. Missing, malformed or out-of-range preferences use the default.
- Saving uses a temporary file and atomic replacement. A failed save is logged;
  adjusting the current window still works.

`MainWindow` gives both lists finite height instead of placing the whole sidebar
in a vertical ScrollViewer. Pixel scrolling allows access to cards even when a
card is taller than the viewport. Expanded settings have their own bounded
footer viewport, so they remain reachable at the minimum window size.
`SidebarSectionSplit` only manages layout and a local preference; it does not
change profile selection or invoke the manager service.

Periodic card refreshes replace only changed items when IDs/order are stable.
Clearing and rebuilding that collection invalidated WPF's measured virtualized
card heights, causing a visible 16-DIP jump in the mid-list regression fixture.
Membership/order changes still rebuild the list normally.

Validation uses the WPF layout fixture with a temporary root and synthetic
profiles/shortcuts. It exercises splitter gestures, independent scrolling,
selection preservation, preference restoration, and minimum-window controls.
It also checks the visible item and its position after a usage/shortcut refresh.
It never opens, closes or switches a live Codex profile.
