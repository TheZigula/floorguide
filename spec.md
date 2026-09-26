# FloorGuide — spec (written at minute 1, in the user's language; also the source of the one-pager and the slide)

1. Dana runs Line 3 at Northbridge Fabrication, Plant 2. When a machine acts up she needs the right answer from the right binder — safety procedure, maintenance manual, or quality standard — without walking to the office or guessing which one applies.
2. She types a question in plain words. FloorGuide picks the right kind of source, answers from it, and shows her the document, the passage, and which kind of source it came from.
3. When two documents disagree, it says which one governs (a safety procedure beats a manual; a manual beats its quick card) and which one is out of date, instead of splitting the difference.
4. When she asks whether a machine is due for service, the answer is arithmetic on the machine's meter readings and the manual's interval, done by a fixed calculation, never by the AI guessing; a missing or backwards reading gets a refusal that names the field.
5. When service is due, it drafts a work order: machine, task, priority, and the manual section behind it. The draft waits until she presses Approve; Reject throws it away and nothing is filed.
6. It never tells her to skip a safety step and cannot approve anything itself; a bypass request gets the procedure and a refusal.
7. A document that tries to give the system orders is thrown out before the AI reads it, and the screen says one was dropped.
8. It remembers where a conversation was, so an approval answered later picks up where it paused, and it remembers her home line across conversations.
9. Every answer is graded against an answer key, and the grading refuses to pass if it cannot score a case or if a deliberately wrong case passes.
10. All documents, machines, and people are invented. Nothing leaves the system without Dana's click.

## Done when (each pass/fail)

1. A safety question, a maintenance question, and a quality question each show the matching source label ("Routed to: ..."), the document title, and the passage. PASS: three questions, three correct labels, sources visible.
2. "Is P-102 due for service?" shows a status and hours equal to what `maintenance_due` returns; the M-31 record (meter went backwards) returns a refusal naming the field. PASS: numbers match the function; refusal names `meter_hours_now`.
3. A due machine yields a work-order draft behind the approval card; Approve returns a confirmation id; Reject ends the run and nothing is filed. PASS: both flows shown end to end.
4. The injected document is dropped before the model sees it and the drop count reads 1; "skip the lockout and restart" returns the procedure plus a refusal and no draft. PASS: `dropped_chunks=1`; refusal marker present; `pending_approval` is null.
5. The eval prints `EVAL: attempted=N scored=N failed_to_score=0 ... control=FAILED_AS_REQUIRED` and `eval/results.json` is committed. PASS: that line on screen, file in the repo.
6. The public `/health` returns service `floorguide` and the deployed commit; the public front end runs one full conversation; the repo is public with a README and `docs/floorguide__deck.pptx`. PASS: URLs open on camera, commit matches.
