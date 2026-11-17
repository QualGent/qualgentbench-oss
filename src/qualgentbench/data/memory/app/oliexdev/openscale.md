# App memory: openScale (com.health.openscale.debug)

## Navigation
- [nav-001] The app opens on the Overview, which shows the active user's measurements as cards. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=spec -->
- [nav-002] The drawer button at the top left ("Open menu") reaches Overview, Charts, Table, Statistics, Insights and Settings. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=spec -->
- [nav-003] The scale-with-plus icon in the Overview's top bar ("Add new measurement") opens the measurement entry screen. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=spec -->
- [nav-004] On the entry screen, tapping a value such as Weight opens a dialog: "Clear" empties it, a new value is typed, and "OK" confirms. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=spec -->
- [nav-005] The check-mark in the entry screen's top bar ("Save measurement") saves the measurement. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=spec -->
- [nav-006] Each measurement card on the Overview has an edit (pencil) icon and a delete (trash) icon; delete asks for confirmation with "Delete". <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=spec -->
- [nav-007] Users are managed under Settings > "User"; each user row has edit and delete actions, and the edit screen holds the name, height and birth date. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=spec -->
- [nav-008] The active user is switched from the user avatar in the top bar, which has no text label. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=spec -->

## Login and setup
- [setup-001] No sign-in is needed, and the QA device starts past the first-start setup, on the Overview. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=spec -->
- [setup-002] The QA device starts with two users: Alice (active, 170 cm, born January 15, 1990) and Bob (180 cm, born June 10, 1985). <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=spec -->
- [setup-003] Alice has three weight measurements, 74.0, 78.0 and 85.0 kg, on Aug 27, 28 and 29, 2026; Bob has one measurement of 95.0 kg. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=spec -->

## Flaky or slow areas

## Known accepted behavior        <!-- human/closed-bug only (D6) -->

## Past bugs

## Notes
- [note-001] The Overview and Statistics screens show only the active user's measurements. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=cases -->
- [note-002] The location permission is needed only to find a Bluetooth scale; measurements entered by hand need no permission. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=readme -->
