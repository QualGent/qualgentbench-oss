# App memory: AnkiDroid (com.ichi2.anki.debug)

## Navigation
- [nav-001] The deck list (deck picker) is the start screen; it shows each deck as a row, and tapping a deck name starts a study session for it. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=spec -->
- [nav-002] The + button at the bottom right opens a menu with "Add" (a new note) and "Create deck". <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=spec -->
- [nav-003] "Create deck" opens a dialog with a "Name" field; "Create" adds the deck to the deck list. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=spec -->
- [nav-004] The note editor opened by "Add" has "Front" and "Back" fields for a Basic note, a deck selector, a "Tags" row and a check-mark "Save" action in the top bar. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=spec -->
- [nav-005] The "Tags" row opens a tag dialog: the "Add tag" icon opens a "Name" field, "Add" adds the tag, and "Confirm" closes the dialog. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=spec -->
- [nav-006] In a study session the reviewer shows the question first; "Show answer" reveals the answer and the grade buttons "Again", "Hard", "Good" and "Easy". <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=spec -->
- [nav-007] The navigation drawer ("Open drawer", top left) reaches "Decks", "Card browser", "Statistics" and "Settings". <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=spec -->
- [nav-008] The Card browser lists cards with their question and answer columns, and the deck name at the top of the browser is a selector for which deck's cards are listed. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=cases -->

## Login and setup
- [setup-001] No sign-in is needed; AnkiWeb sync is optional and is not set up on the QA device. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=readme -->
- [setup-002] The QA device starts past the first-run introduction, on the deck list. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=spec -->
- [setup-003] The QA device starts with a "Spanish" deck holding three Basic notes (uno/one, dos/two, tres/three) that have never been studied. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=spec -->

## Flaky or slow areas

## Known accepted behavior        <!-- human/closed-bug only (D6) -->

## Past bugs

## Notes
- [note-001] This is a debug build; it also installs a second launcher entry from LeakCanary, a memory-leak tool that is not part of AnkiDroid. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=spec -->
- [note-002] The reviewer renders the card's question and answer in a web view, so the card text is part of the screen content. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=spec -->
