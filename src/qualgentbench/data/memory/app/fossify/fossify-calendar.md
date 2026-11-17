# App memory: Fossify Calendar (org.fossify.calendar.debug)

## Navigation
- [nav-001] The main screen shows the calendar in the current view, which is the month view of the current month on a fresh install. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=cases -->
- [nav-002] The + button at the bottom right opens two choices, "Event" and "Task", each opening its own editor. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=spec -->
- [nav-003] The event editor has a title field, start and end date and time rows, an "All-day" switch, reminder rows and a repetition row; the "Save" action is in the toolbar. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=cases -->
- [nav-004] The task editor has a title field, a date and time, reminder rows and a repetition row, with "Save" in the toolbar. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=source -->
- [nav-005] The toolbar's "Change view" button lists the views: daily, weekly, monthly, yearly and "Simple event list", which shows upcoming items by title. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=spec -->
- [nav-006] The search field at the top of the main screen searches events by title as the user types. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=cases -->
- [nav-007] Tapping an item in the calendar or list opens it; an open event has "Delete" in its toolbar. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=cases -->
- [nav-008] Settings is in the main screen's More options menu; "Manage calendars" there lists calendars and has "Add a new calendar". <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=spec -->

## Login and setup
- [setup-001] There is no sign-in; events and tasks are stored on the device in the default "Local calendar". <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=spec -->
- [setup-002] The QA device starts with an empty calendar: no events, tasks or extra calendars. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=cases -->

## Flaky or slow areas
- [flaky-001] The first tap on a reminder row shows a one-time "Disclaimer" dialog about reminders, closed with "OK". <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=spec -->

## Known accepted behavior        <!-- human/closed-bug only (D6) -->

## Past bugs

## Notes
- [note-001] Events and tasks share the same calendar and appear in the same views and lists. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=readme -->
- [note-002] The month view addresses days by date, so the "Simple event list" view is the easiest place to find items by title. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=spec -->
- [note-003] Fossify Calendar can also sync with CalDAV calendars through a separate sync app, but the default local calendar needs no account. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=readme -->
