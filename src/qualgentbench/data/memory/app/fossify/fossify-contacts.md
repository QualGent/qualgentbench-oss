# App memory: Fossify Contacts (org.fossify.contacts.debug)

## Navigation
- [nav-001] The main screen has three tabs along the bottom: the contact list, "Favorites" and "Groups". <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=cases -->
- [nav-002] On the contact list, the + button opens the new-contact form with name fields, phone number, email and more; "Save" is in the toolbar. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=cases -->
- [nav-003] Tapping a contact opens its details screen, whose toolbar has actions such as edit, favorite and "Delete". <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=cases -->
- [nav-004] Long-pressing a contact selects it; the selection bar offers actions such as add to favorites, with more actions under More options. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=spec -->
- [nav-005] On the "Groups" tab, the + button creates a group: the user names it, confirms with "OK", then picks where to store it, for example "Phone storage". <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=cases -->
- [nav-006] Tapping a group opens a screen listing the group's members, where contacts can be added to it. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=spec -->
- [nav-007] Settings is in the main screen's More options menu. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=source -->

## Login and setup
- [setup-001] There is no sign-in. The app needs Android's contacts permission and asks for it if it is not already granted. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=readme -->
- [setup-002] The QA device starts with no contacts and no groups. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=spec -->

## Flaky or slow areas

## Known accepted behavior        <!-- human/closed-bug only (D6) -->

## Past bugs

## Notes
- [note-001] The app keeps no database of its own: contacts and groups live in Android's shared contacts storage, so other apps on the device see the same records. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=spec -->
- [note-002] The contact list is sorted by name, and the "Favorites" tab lists only contacts marked as favorites. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=cases -->
