# App memory: Fossify File Manager (org.fossify.filemanager.debug)

## Navigation
- [nav-001] The app has three tabs along the bottom: "Files", "Recents" and "Storage analysis". <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=source -->
- [nav-002] The "Files" tab opens on the home folder, by default the top of internal storage; breadcrumbs at the top show the path, and "Internal" goes back to the top. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=cases -->
- [nav-003] The search field at the top of the "Files" tab finds files and folders by name in the current folder and below it. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=cases -->
- [nav-004] The folder's More options menu has "Change view type", "Add to favorites", "Temporarily show hidden", "Set as home folder" and "Settings"; "Go to favorite" is in the toolbar. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=source -->
- [nav-005] Long-pressing a file or folder selects it; the selection bar has "Delete" and others, and its More options menu has "Properties", "Rename", "Copy to", "Move to", "Compress", "Hide" and "Select all". <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=source -->
- [nav-006] "Copy to" and "Move to" open a folder picker whose + button makes a new folder. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=cases -->
- [nav-007] The + button on the "Files" tab creates a new folder or file in the current folder. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=source -->
- [nav-008] Settings, in the More options menu, has "Manage favorites". <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=cases -->

## Login and setup
- [setup-001] There is no sign-in. On Android 11 and later the app needs "All files" access, which is already granted on the QA device. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=spec -->
- [setup-002] The QA device starts with a "QGB" folder inside "Download" that holds two small text files, alpha.txt and beta.txt. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=spec -->

## Flaky or slow areas

## Known accepted behavior        <!-- human/closed-bug only (D6) -->

## Past bugs

## Notes
- [note-001] Tapping a file opens Android's app chooser, whose entries depend on the other apps installed on the device. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=spec -->
- [note-002] The device may hold other folders of its own besides the "QGB" folder. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=spec -->
