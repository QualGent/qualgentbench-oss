# App memory: Tasks.org (org.tasks)

## Navigation
- [nav-001] The task list is the start screen; each row has a round completion circle at the left, and tapping the row text opens the task's editor. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=spec -->
- [nav-002] The + button ("Create new task") opens a new task in the editor, with the "Task name" field at the top. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=spec -->
- [nav-003] The editor has rows for "No start date", "No due date", repeat and priority, plus an "Add subtask" row that adds a subtask name field. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=spec -->
- [nav-004] The editor's top bar has a check-mark "Save" button and a bin "Delete task" button; deleting asks for confirmation with "OK". <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=spec -->
- [nav-005] Tapping the due date row opens a date picker with quick choices such as a "Today" cell and time chips such as "9 AM"; "OK" confirms the choice. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=spec -->
- [nav-006] The bottom bar has "Search", which filters the list live as you type, and a sort control. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=spec -->
- [nav-007] A parent task shows its subtasks beneath it in the list, and the parent row can be expanded or collapsed to show or hide them. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=cases -->
- [nav-008] The navigation drawer, opened from the top-left button, lists filters and lists; its button has no text label. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=spec -->

## Login and setup
- [setup-001] No sign-in is needed; tasks are kept in a local on-device list, so the first-run account setup screens do not appear. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=spec -->
- [setup-002] The QA device starts with four tasks: "Water plants" (repeats daily, due today with no time of day), "Pack for trip" (subtasks "Passport" and "Chargers"), "Call dentist" (highest priority) and "Library books" (no due date). <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=spec -->
- [setup-003] The QA device has already granted the app permission to post notifications. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=spec -->

## Flaky or slow areas

## Known accepted behavior        <!-- human/closed-bug only (D6) -->

## Past bugs

## Notes
- [note-001] A due date can be date-only or carry a time of day; choosing a time chip in the picker adds a time to the date. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=cases -->
- [note-002] A completed task is hidden from the default list view, so searching is a quick way to find a task again after completing it. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=readme -->
