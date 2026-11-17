# App memory: Money Manager Ex (com.money.manager.ex)

## Navigation
- [nav-001] The home screen lists the accounts with their balances and shows a summary of the current month below them. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=spec -->
- [nav-002] Tapping an account on the home screen opens its transaction list, with the account balance shown above the list. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=cases -->
- [nav-003] In an account's transaction list, the + button opens a new transaction form; tapping a listed transaction opens it for editing. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=cases -->
- [nav-004] The transaction form has type buttons for Withdrawal, Deposit and Transfer at the top; a new transaction starts as a Withdrawal. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=cases -->
- [nav-005] Tapping the amount opens a separate calculator keypad screen; the amount is entered on its keys and confirmed with that screen's own "Save". <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=spec -->
- [nav-006] The form also has "Payee", "Category", a "Status" selector, a "Notes" field and a "Save" action. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=spec -->
- [nav-007] For a Transfer the form shows the second account, and a swap button switches which account the money comes from and which it goes to. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=cases -->
- [nav-008] The navigation drawer, opened from the top left of the home screen, has "Entities" (which expands to "Accounts", "Payees", "Categories" and more), "Scheduled Transactions", "Budgets", "Search" and "Settings". <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=source -->

## Login and setup
- [setup-001] No sign-in is needed, and the QA device starts on the home screen with a database already open, so the intro and database picker do not appear. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=spec -->
- [setup-002] The QA device starts with two accounts: Checking with a starting balance of 100.00 and Savings with 0.00. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=spec -->
- [setup-003] The QA device has one payee, "Grocer", whose default category is "Food". <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=spec -->

## Flaky or slow areas

## Known accepted behavior        <!-- human/closed-bug only (D6) -->

## Past bugs

## Notes
- [note-001] The transaction form does not save without a payee and a category; choosing "Grocer" as payee fills in its category. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=spec -->
- [note-002] The app keeps all data in one local database file (.mmb), the same format the desktop app uses. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=readme -->
