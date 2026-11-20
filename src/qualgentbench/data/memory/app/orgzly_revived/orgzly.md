# App memory: Orgzly Revived (com.orgzlyrevived)

## Navigation
- [nav-001] The "Notebooks" screen is the start screen and lists each notebook. Tapping a notebook opens its notes as an outline. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=spec -->
- [nav-002] The + button inside a notebook opens the note editor for a new note. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=spec -->
- [nav-003] The note editor has a title field (hint "Title"), buttons for state, "Scheduled time", "Deadline time", priority and tags, and a "Content" field for body text. "Done" saves the note. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=spec -->
- [nav-004] The timestamp dialog for scheduled and deadline times has "Today" and "Tomorrow" buttons, a repeater checkbox and "Set". <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=spec -->
- [nav-005] A new note is added at the end of the notebook. The sample notebook is long, so the new note is several screens down. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=cases -->
- [nav-006] Long-pressing a note selects it and shows actions in the toolbar, such as "Done" (mark as done) and "New note" with "New above", "New under" and "New below". <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=cases -->
- [nav-007] "New under" creates a sub-note one level deeper than the selected note. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=cases -->
- [nav-008] With a note selected, the toolbar's "More options" has "Delete", which asks for confirmation in a dialog. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=cases -->
- [nav-009] "Search" in the toolbar searches notes across all notebooks by title words. "More options" then "Settings" opens the settings. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=spec -->
- [nav-010] The navigation drawer lists the notebooks and saved searches such as "Agenda". <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=readme -->

## Login and setup
- [setup-001] No sign-in and no sync set up. The app stores notebooks locally until a repository is added. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=spec -->
- [setup-002] The QA device starts with the bundled sample notebook "Getting Started with Orgzly", which explains the app through its own notes. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=spec -->

## Flaky or slow areas

## Known accepted behavior        <!-- human/closed-bug only (D6) -->

## Past bugs

## Notes
- [note-001] In the notebook list, each note row shows its state, priority and tags next to the title, and its scheduled or deadline time below it. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=spec -->
