# Org memory: Beem Development

## Company
- [company-001] Aegis Authenticator is a free, open-source two-factor authentication (2FA) app for Android made by Beem Development. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=readme -->
- [company-002] The app is licensed GPL-3.0 and is distributed on Google Play, F-Droid and GitHub releases. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=readme -->

## Users
- [users-001] Users keep the one-time codes for their online accounts in Aegis and type or copy a code when a service asks for it. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=readme -->
- [users-002] Many users move to Aegis from other authenticator apps through its importers, and move entries out again with export or transfer. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=readme -->

## What matters to them
- [matters-001] Security comes first: the vault can be encrypted with AES-256-GCM and unlocked with a password or biometrics. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=readme -->
- [matters-002] Losing access to 2FA secrets can lock a user out of accounts, so backups and exports of the vault matter a great deal. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=readme -->
- [matters-003] The app supports the standard HOTP and TOTP algorithms and is compatible with Google Authenticator. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=readme -->

## Terminology
- [term-001] An "entry" is one account's 2FA secret; it has a name (the account), an issuer (the service), a secret and an optional group. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=cases -->
- [term-002] The "vault" is the store that holds all entries, groups and settings. <!-- src=human; confirmed=2026-11-17; seen=0; contra=0; basis=readme -->
