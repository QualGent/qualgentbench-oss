# App memory: Simple Time Tracker (com.razeeman.util.simpletimetracker.debug)

## Navigation
- [nav-001] The app has four main tabs: "Timers" (the start screen), "Records", "Statistics" and "Settings". <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=cases -->
- [nav-002] On the Timers screen each activity is a card; tapping a card starts its timer and tapping it again stops the timer and saves a record. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=cases -->
- [nav-003] The "Add" card on the Timers screen opens the screen for a new activity, with a name field ("Enter new name"), "Color", "Icon" and "More" sections and a "SAVE" button. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=source -->
- [nav-004] Long-pressing an activity card on the Timers screen opens that activity's screen for editing. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=cases -->
- [nav-005] Under "More" on the activity screen are the categories, "Goal" (session, day, week and month goals), a note field and the default duration, which shows "Disabled" until one is picked. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=cases -->
- [nav-006] The Records tab lists the day's records; tapping a record opens its screen, which has a "Comment" section, a "SAVE" button and "Delete". <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=cases -->
- [nav-007] "Settings" > "Edit categories and tags" lists categories and record tags, with "Add category" and "Add record tag" buttons. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=cases -->
- [nav-008] A new category's screen has an "Activities" section to pick the activities it covers; a new record tag's screen has an "Activity" section. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=cases -->
- [nav-009] Archived activities, categories and tags are listed under "Settings" > "Archive". <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=spec -->
- [nav-010] The Statistics tab shows tracked time per activity for the chosen range, with each activity's share of the total. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=cases -->

## Login and setup
- [setup-001] There is no sign-in, no onboarding and no runtime permission prompt; the app opens straight on the Timers screen. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=spec -->
- [setup-002] The QA device starts with an empty Timers screen: there are no activities, categories or records until they are created. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=cases -->

## Flaky or slow areas
- [flaky-001] On the edit screens the on-screen keyboard can cover the "SAVE" button; tapping a section header such as "Color" hides the keyboard. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=spec -->

## Known accepted behavior        <!-- human/closed-bug only (D6) -->

## Past bugs

## Notes
- [note-001] This is the F-Droid ("base") build, so there is no Wear OS or Google Play services support. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=spec -->
- [note-002] Record durations and start times depend on the clock, so they differ from run to run. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=spec -->
