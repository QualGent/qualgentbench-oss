# App memory: Loop Habit Tracker (org.isoron.uhabits)

## Navigation
- [nav-001] The habit list is the start screen; its toolbar has "Add habit" (+), "Filter" and a "More options" menu with "Settings", "Help & FAQ" and "About". <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=source -->
- [nav-002] "Add habit" first asks for the habit type, "Yes or No" or "Measurable", then opens the create-habit screen with a "SAVE" button in the toolbar. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=cases -->
- [nav-003] A Yes or No habit has a name ("e.g. Exercise"), a question, "Frequency", "Reminder" and notes; tapping "Frequency" opens a dialog with options such as every day or a number of times per week or per month. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=cases -->
- [nav-004] A Measurable habit has a unit, a numeric target, a "Target Type" ("At least" or "At most") and a frequency of every day, week or month. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=cases -->
- [nav-005] Long-pressing a habit selects it; the toolbar shows "Edit" and "Change color", and its "More options" menu holds "Archive", "Delete" and the other selection actions. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=source -->
- [nav-006] "Delete" asks for confirmation; "Yes" removes the habit with its history. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=cases -->
- [nav-007] Archived habits are hidden from the list until "Hide archived" is turned off in the "Filter" menu; the same menu has "Hide completed" and "Sort". <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=source -->
- [nav-008] Tapping a habit's name opens the habit screen with "Overview", "Score", "History" and other cards; its toolbar has "Edit" and a "More options" menu with "Export" and "Delete". <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=source -->
- [nav-009] "Change color" opens a palette of twenty colors whose swatches are labelled "Color 1" to "Color 20". <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=cases -->
- [nav-010] The reminder field on the edit screen opens a time picker that is closed with "Done". <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=cases -->

## Login and setup
- [setup-001] There is no account or sign-in, and the app asks for no storage or network permission. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=spec -->
- [setup-002] The QA device starts past the three-page intro, on an empty habit list with no habits. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=spec -->

## Flaky or slow areas
- [flaky-001] The selection toolbar and its overflow menu animate in; a tap made before one finishes opening can miss. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=spec -->

## Known accepted behavior        <!-- human/closed-bug only (D6) -->

## Past bugs

## Notes
- [note-001] The day checkmark buttons on each habit row and the history editor are drawn as custom views with no accessibility labels, so they can only be found by position on screen. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=spec -->
- [note-002] The habit list shows only active habits by default; archiving takes a habit off that list without deleting it. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=cases -->
