# App memory: Markor (net.gsantner.markor)

## Navigation
- [nav-001] The app opens on the "Files" tab, a file browser of the notebook folder; the bottom bar has the tabs "Files", "To-Do", "QuickNote" and "More". <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=source -->
- [nav-002] The + button in the file list ("Create a new file or folder") opens the new-file dialog with a name field, a format choice (Markdown by default), a "Template" choice, and the buttons "OK" (new file), "FOLDER" (new folder) and "Cancel". <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=source -->
- [nav-003] Tapping a folder opens it; the ".." row at the top of the list goes up one level. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=cases -->
- [nav-004] Tapping a file opens it in the editor; the editor toolbar has a "Save" button and a "More options" menu that includes "File Browser". <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=cases -->
- [nav-005] Long-pressing a file selects it, and tapping more files adds them to the selection; the toolbar then shows actions such as "Details" and "Delete", and "More options" holds "Move", "Copy" and "Share". <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=source -->
- [nav-006] "Move" and "Copy" open a folder picker; ".." goes up a level and "Select this folder" confirms the destination. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=cases -->
- [nav-007] "Search" in the file list toolbar asks for text, and the results list matching files with their path relative to the current folder, such as "Recipes/Pancakes.md". <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=cases -->
- [nav-008] The "To-Do" tab is a text editor for one todo.txt list; its action bar has "Priority" (a list of letters) and "Toggle done", which act on the line under the cursor. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=cases -->
- [nav-009] "Details" on a selected file opens a dialog with its name, location, last change, size in Bytes, type, and line, word and character counts. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=cases -->

## Login and setup
- [setup-001] There is no account or sign-in. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=readme -->
- [setup-002] The QA device starts past the intro, on the Files tab of the notebook folder Documents/markor, which holds "Shopping.md", "Travel.md" and a "Recipes" folder with "Pancakes.md" and "Soup.md". <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=spec -->
- [setup-003] Access to all files in storage is already granted on the QA device, so no storage permission prompt appears. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=spec -->

## Flaky or slow areas
- [flaky-001] A changelog dialog can appear over the file list at launch; it closes with "OK". <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=spec -->
- [flaky-002] After "OK" in the new-file dialog the new note's editor takes a moment to open. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=spec -->

## Known accepted behavior        <!-- human/closed-bug only (D6) -->

## Past bugs

## Notes
- [note-001] A new file gets the format's extension added to the name entered, so "Packing" becomes "Packing.md". <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=cases -->
- [note-002] The rename dialog opens pre-filled with the file's current name. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=spec -->
