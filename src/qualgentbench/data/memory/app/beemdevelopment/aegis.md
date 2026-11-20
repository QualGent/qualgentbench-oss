# App memory: Aegis Authenticator (com.beemdevelopment.aegis.debug)

## Navigation
- [nav-001] The entry list is the start screen; its toolbar has "Search", "Sort" and a "More options" menu with "Settings" and "About". <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=source -->
- [nav-002] The + button ("Add new entry") offers "Scan QR code", "Scan image" and "Enter manually". <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=source -->
- [nav-003] "Enter manually" opens the entry form with Name, Issuer and Secret fields and a "SAVE" button; the secret must be base32, such as JBSWY3DPEHPK3PXP. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=spec -->
- [nav-004] The form's Group field reads "No group" by default; it opens a group sheet where "New group…" asks for a name, and the sheet's own Save button applies the choice. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=cases -->
- [nav-005] Each list row shows the entry's issuer and account name and a code that refreshes every 30 seconds. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=cases -->
- [nav-006] Long-pressing an entry selects it, and tapping other entries adds them to the selection; the toolbar then shows "Favorite", "Copy", "Edit" and "Transfer", and "More options" holds "Delete", "Select all" and "Assign to group". <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=source -->
- [nav-007] "Edit" opens the "Edit entry" screen, which shows the entry's fields with its usage count and last-used time, and a "SAVE" button. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=cases -->
- [nav-008] "Transfer" opens a screen of QR codes for moving the selected entries to another authenticator app; it is closed with "Done". <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=cases -->
- [nav-009] Settings has the sections "Appearance", "Behavior", "Security", "Backups", "Import & Export" and "Audit log"; "Edit groups" is near the bottom of Appearance. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=source -->
- [nav-010] After the vault changes, a "Changes are not backed up" card appears above the list; tapping it opens a dialog with "Set up backups". <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=cases -->

## Login and setup
- [setup-001] There is no account or sign-in; the vault lives only on the device. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=readme -->
- [setup-002] The QA device starts past the intro with an empty vault that has no password, so the app opens without an unlock screen on an empty list ("No entries found"). <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=spec -->
- [setup-003] The time-sync warning is turned off on the QA device, so no "Automatic time synchronization" dialog appears at launch. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=spec -->

## Flaky or slow areas

## Known accepted behavior        <!-- human/closed-bug only (D6) -->

## Past bugs

## Notes
- [note-001] The OTP type and hash dropdowns on the entry form open popups that do not appear in the screen's accessibility hierarchy. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=spec -->
- [note-002] This is a debug build, so screen-capture protection is off and screenshots work. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=source -->
- [note-003] The list footer counts the entries shown, as "Showing N entries". <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=spec -->
