# Third-party apps and licenses

The benchmark harness in this repository is licensed under [Apache-2.0](LICENSE).

The benchmark tests real open-source Android apps. For each app, the spec in
`src/qualgentbench/data/benchmarks/<app>.yaml` contains small `find:`/`replace:`
excerpts of that app's source code — the seeded-defect patches. **Those excerpts,
and the modified APKs we publish on HuggingFace, remain under each app's own
license**, listed below. For the GPL apps, the patch blocks in the specs are the
complete, corresponding source of our modifications — that is how we meet the GPL's
source-sharing requirement for the published APKs.

All credit for the apps themselves goes to their authors. We chose them because
they are good, real software; the defects are ours, not theirs.

| App id | Upstream project | Pinned ref | License |
|---|---|---|---|
| aegis | [beemdevelopment/Aegis](https://github.com/beemdevelopment/Aegis) | v3.4.2 | GPL-3.0 |
| ankidroid | [ankidroid/Anki-Android](https://github.com/ankidroid/Anki-Android) | v2.24.0 | GPL-3.0 |
| birday | [m-i-n-a-r/birday](https://github.com/m-i-n-a-r/birday) | v4.7.2 | GPL-3.0 |
| broccoli | [flauschtrud/broccoli](https://github.com/flauschtrud/broccoli) | v1.4.6-fdroid | GPL-3.0 |
| catima | [CatimaLoyalty/Android](https://github.com/CatimaLoyalty/Android) | v2.44.0 | GPL-3.0 |
| easynotes | [Kin69/EasyNotes](https://github.com/Kin69/EasyNotes) | see spec | GPL-3.0 |
| fossify-calculator | [FossifyOrg/Calculator](https://github.com/FossifyOrg/Calculator) | 1.4.0 | GPL-3.0 |
| fossify-calendar | [FossifyOrg/Calendar](https://github.com/FossifyOrg/Calendar) | 1.10.3 | GPL-3.0 |
| fossify-clock | [FossifyOrg/Clock](https://github.com/FossifyOrg/Clock) | 1.6.0 | GPL-3.0 |
| fossify-contacts | [FossifyOrg/Contacts](https://github.com/FossifyOrg/Contacts) | 1.6.0 | GPL-3.0 |
| fossify-filemanager | [FossifyOrg/File-Manager](https://github.com/FossifyOrg/File-Manager) | 1.6.1 | GPL-3.0 |
| fossify-gallery | [FossifyOrg/Gallery](https://github.com/FossifyOrg/Gallery) | 1.13.1 | GPL-3.0 |
| fossify-messages | [FossifyOrg/Messages](https://github.com/FossifyOrg/Messages) | 1.9.1 | GPL-3.0 |
| fossify-musicplayer | [FossifyOrg/Music-Player](https://github.com/FossifyOrg/Music-Player) | 1.8.1 | GPL-3.0 |
| fossify-notes | [FossifyOrg/Notes](https://github.com/FossifyOrg/Notes) | 1.7.0 | GPL-3.0 |
| markor | [gsantner/markor](https://github.com/gsantner/markor) | v2.16.1 | Apache-2.0 |
| medtimer | [Futsch1/medTimer](https://github.com/Futsch1/medTimer) | v1.25.2 | MIT |
| moneymanagerex | [moneymanagerex/android-money-manager-ex](https://github.com/moneymanagerex/android-money-manager-ex) | 5.5.11 | GPL-3.0 |
| notally | [OmGodse/Notally](https://github.com/OmGodse/Notally) | v6.2 | GPL-3.0 |
| opencalc | [Darkempire78/OpenCalc](https://github.com/Darkempire78/OpenCalc) | see spec | GPL-3.0 |
| openscale | [oliexdev/openScale](https://github.com/oliexdev/openScale) | v3.1.2 | GPL-3.0 |
| orgzly | [orgzly-revived/orgzly-android-revived](https://github.com/orgzly-revived/orgzly-android-revived) | v1.23.0 | GPL-3.0 |
| pf-qr-scanner | [SecUSo/privacy-friendly-qr-scanner](https://github.com/SecUSo/privacy-friendly-qr-scanner) | v4.6.19 | GPL-3.0 |
| pf-shopping-list | [SecUSo/privacy-friendly-shopping-list](https://github.com/SecUSo/privacy-friendly-shopping-list) | v1.2.1 | Apache-2.0 |
| pftodo | [SecUSo/privacy-friendly-todo-list](https://github.com/SecUSo/privacy-friendly-todo-list) | see spec | GPL-3.0 |
| simpletimetracker | [Razeeman/Android-SimpleTimeTracker](https://github.com/Razeeman/Android-SimpleTimeTracker) | v1.59 | GPL-3.0 |
| tasksorg | [tasks/tasks](https://github.com/tasks/tasks) | 15.10 | GPL-3.0 |
| uhabits | [iSoron/uhabits](https://github.com/iSoron/uhabits) | v2.3.1 | GPL-3.0 |

Licenses were read from each upstream repository on 2026-08-20 (hard-tier apps
2026-09-02). If an entry is
wrong or an author wants an app removed from the benchmark, open an issue and we
will fix it promptly.
