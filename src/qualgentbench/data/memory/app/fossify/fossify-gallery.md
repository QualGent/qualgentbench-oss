# App memory: Fossify Gallery (org.fossify.gallery.debug)

## Navigation
- [nav-001] The app opens on the folders screen, which shows one tile per album; tapping a tile opens the album's photo grid. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=cases -->
- [nav-002] Photo thumbnails show no file names until "Toggle filename visibility" in the album toolbar is switched on; the choice is kept for the whole app. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=cases -->
- [nav-003] Tapping a photo opens the full-screen viewer; its star is "Toggle favorite", and its More options menu has "Rename" and "Properties". <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=cases -->
- [nav-004] The album's More options menu has "Sort by", "Group by", "Change view type" and "Open the Recycle Bin". <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=source -->
- [nav-005] Long-pressing a photo selects it; the selection bar has delete, and its More options menu has "Rename", "Add to favorites", "Select all" and more. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=cases -->
- [nav-006] Long-pressing a folder tile on the folders screen offers "Hide folder" and "Exclude" under More options. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=cases -->
- [nav-007] The folders screen's More options menu has "Temporarily show hidden" and "Settings"; Settings has "Manage excluded folders". <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=source -->

## Login and setup
- [setup-001] There is no sign-in. On Android 11 and later the app asks for "All files" access on first launch; on the QA device it is already granted. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=spec -->
- [setup-002] The QA device starts with two albums: "QGB" with QGB-Photo-One.png and QGB-Photo-Two.png, and "Holiday" with one photo. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=spec -->

## Flaky or slow areas

## Known accepted behavior        <!-- human/closed-bug only (D6) -->

## Past bugs

## Notes
- [note-001] Deleting a photo first asks to move it to the recycle bin; the bin can also show as a "Recycle bin" folder on the folders screen. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=cases -->
- [note-002] "Exclude" hides a folder only inside Fossify Gallery, while "Hide folder" also hides it from other apps. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=source -->
- [note-003] The device may hold other albums of its own besides "QGB" and "Holiday". <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=spec -->
