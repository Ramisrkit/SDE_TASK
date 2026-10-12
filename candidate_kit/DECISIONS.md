# DECISIONS.md  (about 150-200 words in total - plain language, your own words)

1. **Indexes.** Which indexes did you create, and which query or guarantee does each one serve? Which one did you consider and reject?
2. **Punch-in race.** Two identical punch-in requests arrive at the same instant. Walk through exactly what happens in your code.
3. **Ties.** Two employees are tied on late minutes at the cutoff of `limit`. What does your leaderboard return, and why?
4. **Headcount.** How does your department summary make sure employees with zero logs are counted?
5. **One thing you would change** if this had to serve 100x the data.




1. **Indexes.** `employees`: unique `emp_code`; `(department, joined_on)` for department and headcount filters; `joined_on` for the all-department summary. `attendance_logs`: unique `(emp_code, date)` (one record per day, race protection, monthly and join lookups); `(date desc, emp_code)` for the sorted list, leaderboard and trend; `(emp_code, punch_in desc)` for punch-out lookup. I rejected `(emp_code, date, status)`: the unique index already covers its prefix.

2. **Punch-in race.** Both requests compute the same date and call `insert_one`. The unique `(emp_code, date)` index lets one succeed; the other gets `DuplicateKeyError`, returned as 409. No check-then-insert.

3. **Ties.** `$rank` gives tied employees the same rank and skips the next. I filter `rank <= limit` afterwards, so everyone tied at the cutoff is returned and the list can exceed `limit`.

4. **Headcount.** The summary starts from `employees` (joined by month end), `$lookup`s logs, and `$unwind`s with `preserveNullAndEmptyArrays`. Headcount is the size of an `$addToSet` of employee ids, so zero-log employees count.

5. **100x data.** The trend `$lookup` reads every log for a date and filters in memory. I would store `department` on logs, or keep daily rollups.


