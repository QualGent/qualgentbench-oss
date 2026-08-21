-- Deterministic notes for easynotes' checks.
--
-- The app seeds three notes with RANDOMISED names on first run ("Workout plan #99",
-- body "Sample note 99"). The checks anchored on the substring "Sample note", which
-- stopped resolving once the replayer refused substring matches on non-clickable
-- nodes (2026-08-11, after a non-clickable label was tapped and produced 8 confident
-- wrong verdicts). Compose list rows report clickable="false", so all five checks
-- derived `undecidable`.
--
-- Fixed names let the checks anchor EXACTLY, which the matcher allows regardless of
-- clickability — and removes the randomised-data problem that also made agents'
-- reproductions unreplayable.
delete from `notes-table`;
insert into `notes-table` (`note-name`,`note-description`,pinned,encrypted,created_at)
values ('QA Note One','QA body one',0,0,1),
       ('QA Note Two','QA body two',0,0,2),
       ('QA Note Three','QA body three',0,0,3);
