# App memory: Birday (com.minar.birday.debug)

## Navigation
- [nav-001] The bottom bar has the home list, "Favorites" and "Settings". The "New event" button adds an event. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=source -->
- [nav-002] The home list groups events by month by default and starts from the next upcoming date. Each row has a star ("Add to favorites"). <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=readme -->
- [nav-003] A search field ("Search…") above the list filters it as you type. The arrow at the end of the field opens the event-type filters. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=cases -->
- [nav-004] The new-event form has "Event type" (Birthday by default), "Insert First Name", "Insert Last Name", "Select a date" and a "Consider the year" switch. "Insert event" saves. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=source -->
- [nav-005] Tapping an event row opens its details screen, which offers "Edit event", "Delete event" and "Notes". <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=cases -->
- [nav-006] "Edit event" opens the event form filled with the event's values, and "Update event" saves it. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=cases -->
- [nav-007] "Notes" on the details screen opens a dialog for a free-text note, confirmed with "OK". <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=cases -->
- [nav-008] The "Experimental" section at the bottom of Settings has "Delete all events", which asks for confirmation first. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=cases -->

## Login and setup
- [setup-001] No sign-in. The first launch shows three welcome pages ("NEXT", "NEXT", "DONE"), then asks for notification permission and then contacts permission. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=spec -->
- [setup-002] The QA device starts with ten events of all five types, such as Clara Novak, David Okafor and Elena Rossi (birthdays), Iris Bauer (anniversary), Joseph Kerr (death anniversary), Laura (name day) and Book club (other). <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=spec -->
- [setup-003] Only the first few events fit on screen, so the search field is the quick way to reach an event lower in the list. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=spec -->

## Flaky or slow areas
- [flaky-001] The welcome pages and the two permission prompts appear on the first launch only. They come before the event list. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=spec -->

## Known accepted behavior        <!-- human/closed-bug only (D6) -->
- [accepted-001] The statistics area appears only when more than five events are saved. With fewer events it is hidden by design. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=readme -->

## Past bugs

## Notes
- [note-001] Birday can import events from contacts or the calendar and can export CSV or JSON. The contacts permission is used for that import. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=readme -->
