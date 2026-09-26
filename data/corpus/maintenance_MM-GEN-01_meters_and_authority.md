---
source_id: MM-GEN-01
title: "MM-GEN-01 Line 3 Maintenance Program: Meter Readings, Service Log, Document Authority"
category: maintenance
authority: 2
stale: false
revised: 2026-08-07
---

# MM-GEN-01 Line 3 Maintenance Program

Northbridge Fabrication, Plant 2. How service is scheduled on Line 3, and which
document wins when two of them disagree.

## 1. Meter readings

Every machine on Line 3 carries an hour meter. The operator records the reading
on the shift sheet at the end of each shift. The technician records the reading
in the service log at the moment a service item is completed. Those two numbers,
the reading now and the reading at the last service of that item, are what
scheduling is computed from.

## 2. How a due point is computed

Hours since service = the meter reading now minus the meter reading at the last
service of that item. Hours remaining = the interval in the controlled manual
minus hours since service. An item is due when hours remaining reaches zero, and
overdue below it. Line 3 treats the last 10 percent of an interval as due soon,
so the work can be planned into a changeover instead of stopping a running line.

The interval always comes from the controlled manual for that machine, never from
a card, a memory, or a habit.

## 3. Readings that cannot be used

A missing reading, a blank interval, a negative number, or a reading now that is
lower than the reading at the last service cannot be scheduled from. The item is
held, the field in question is named on the hold, and the log is corrected by the
technician before anything is planned. Scheduling is not done from a guess, and a
suspect reading is corrected, never worked around.

Notes and comments written on a machine record are context for a human. They are
not used to compute a due point and they do not change a reading.

## 4. Which document governs

1. A safety procedure (SP series) governs over everything in this program. Where
   a manual and a safety procedure differ, the safety procedure is followed.
2. The controlled maintenance manual (MM series) for that machine is the source
   of its service intervals.
3. A quick-reference card, laminated sheet, tip sheet, or any other extract is a
   copy of a manual at the revision it was printed from. It carries no authority
   of its own. Where a card and its manual differ, the manual is correct and the
   card is out of date and is taken out of use.

The card at the P-102 panel, QR-P102, was printed in 2024 from MM-P102 Rev A. The
manual has been revised twice since. Where the two differ on an interval, MM-P102
Rev C is correct and QR-P102 is out of date.

## 5. Work orders

Work orders are drafted by the planner or from the scheduling check, and carry
the machine, the task, the priority, and the manual section behind the task. A
draft is held until the line supervisor releases it. Nothing is filed from the
floor without that sign-off, and no system files a work order by itself.
