# App memory: Catima (me.hackerchick.catima.debug)

## Navigation
- [nav-001] The card list is the start screen. With no cards it shows "Welcome to Catima" and a hint to use the + button. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=source -->
- [nav-002] The + button ("Add") opens the barcode scanner. Its "More options" menu has "Add a card with no barcode", which asks for the card ID and "OK" moves on to the card editor. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=cases -->
- [nav-003] The card editor has the tabs "Card", "Options" and "Photos". "Name" is on the Card tab, and "Save" saves the card. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=source -->
- [nav-004] The Options tab of the editor holds the note, the balance and its unit, and the groups the card belongs to. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=cases -->
- [nav-005] In the editor, "Edit barcode" opens the scanner again. Its "More options" has "Enter the barcode manually", and after "Continue" a list shows the number drawn in each barcode type. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=cases -->
- [nav-006] Tapping a card opens the card screen. Its toolbar has a star ("Add to favorites") and an edit button, and the three-dot menu has "Archive", "Duplicate" and "Delete". <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=cases -->
- [nav-007] The balance button at the bottom right of the card screen opens a dialog with the current balance, an amount field, and "Spend" and "Receive". <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=cases -->
- [nav-008] Long-pressing a card in the list selects it. The selection's "More options" menu offers actions such as "Add to favorites", "Archive", "Duplicate" and "Delete". <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=cases -->
- [nav-009] The main menu ("More options") has "Search", "Display options", "Sort", "Groups", "Import/export", "Settings" and "About". <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=source -->
- [nav-010] The "Groups" screen adds groups by name. Each group has "Edit" (rename and pick its cards) and "Delete" buttons, and the card list shows a tab per group. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=cases -->

## Login and setup
- [setup-001] No sign-in and no intro. The QA device starts with an empty card list. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=spec -->
- [setup-002] Adding a card through + opens the camera scanner, which asks for camera permission. A card with no barcode needs no camera. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=readme -->

## Flaky or slow areas

## Known accepted behavior        <!-- human/closed-bug only (D6) -->

## Past bugs

## Notes
- [note-001] Catima can import cards from files and from FidMe, Loyalty Card Keychain and Voucher Vault, and can share a card as a link. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=readme -->
