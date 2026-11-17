# App memory: MedTimer (com.futsch1.medtimer)

## Navigation
- [nav-001] The bottom tabs are "Overview", "Medicine" and "Analysis". The app opens on "Overview", which shows today's dose events as cards. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=spec -->
- [nav-002] On an Overview card, the status icon opens quick actions "Taken", "Skipped" and "Reschedule". While the reminder waits for an answer, the icon's label is "Reminded". <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=spec -->
- [nav-003] Tapping the body of an Overview card opens that event's editor, with "Taken" and "Skipped" buttons, name, amount, times and notes. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=spec -->
- [nav-004] The "Medicine" tab lists the medicines. Each row's subtitle names its reminder count, such as "1 reminder" or "0 reminders", and tapping the row opens the medicine. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=spec -->
- [nav-005] The "Add medicine" button on the "Medicine" tab asks for a "Medicine name" and "OK" adds it. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=spec -->
- [nav-006] A medicine's screen has its name field and an icon row: "Medicine settings", "Notes", "Tags", "Medicine stock" and "Calendar". <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=spec -->
- [nav-007] Below the icon row, a medicine's screen lists its reminders with inline time and dosage fields. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=spec -->
- [nav-008] "Add reminder" on a medicine's screen offers reminder kinds. "Time based reminder" asks for a "Dosage" and a time, suggests a default time, and "Create reminder" adds it. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=cases -->
- [nav-009] The "Analysis" tab shows charts of recorded doses, and its "Tabular view" lists the events as a table. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=spec -->

## Login and setup
- [setup-001] No sign-in. The app opens straight on the Overview with no intro, and notification permission is already granted. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=spec -->
- [setup-002] The QA device starts with two medicines with stock tracked: Aspirin, with one active daily reminder, and Ibuprofen, which shows no reminders. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=spec -->
- [setup-003] Today's Overview on the QA device already has a logged dose "Ibuprofen (2.5)" and a raised reminder "Ibuprofen (4)" that waits for an answer. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=spec -->

## Flaky or slow areas
- [flaky-001] The Overview content depends on the time of day. A time-based reminder raises its own event card once its time has passed, so the number of cards on today's Overview changes with the hour the app is opened. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=spec -->

## Known accepted behavior        <!-- human/closed-bug only (D6) -->

## Past bugs

## Notes
- [note-001] Most MedTimer edit screens have no Save button. Changes are saved when the screen is left with back. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=spec -->
- [note-002] Besides time-based reminders, MedTimer has interval reminders, weekend mode, snooze, and schedules with breaks or specific days. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=readme -->
