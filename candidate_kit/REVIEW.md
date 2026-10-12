# REVIEW.md

List every defect you found in the starter `app/main.py` (helpers and endpoints). For each one:

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | | | | |

Also note anything you looked at and decided was **not** a defect, and why.






Defects found in the starter `app/main.py` (line numbers refer to the starter). "Fix" names the function in my `app/main.py`.

| # | Where (function / line) | What is wrong | How you'd notice it | How I fixed it |
|---|---|---|---|---|
| 1 | `compute_late_minutes`, L36-37 | Floors to whole minutes first, then tests `> 10`. R2 says late only if strictly more than 10:00 after shift start. | Shift 09:30, punch-in 09:40:01: floor gives 10, `10 > 10` is false, so late = 0 (should be 10). | `late_minutes()` tests elapsed **seconds** `> 600`, then returns `floor(seconds / 60)`. |
| 2 | `compute_late_minutes`, L35 | Builds the shift start on the punch-in's own (server-local) day with `replace()`, not on the IST attendance date. | Server not on IST: wrong result. Overnight shift 22:00-06:00, punch-in 00:30: compared with 22:00 of the same day, so negative or wrong. | Shift start is built from the attendance date in IST (`datetime.combine(date, time, tzinfo=IST)`). |
| 3 | `compute_work_hours`, L41 | Python `round()` is float / banker's rounding. R4 requires half-up. | In 09:30:00, out 17:37:30 = 8.125 h: `round(8.125, 2)` gives 8.12, expected 8.13. | `round_half_up()` using `Decimal` and `ROUND_HALF_UP`. |
| 4 | `compute_overtime`, L47 | No 30-minute minimum (R3); returns any positive number of minutes. | Shift end 18:30, out 18:40: returns 10, expected 0. | `derived_values()` counts overtime only when whole minutes after shift end are `>= 30`. |
| 5 | `compute_overtime`, L46 | `end` is timezone-aware (IST) but `punch_out` is naive, so the subtraction raises `TypeError`. It also ignores overnight shifts (end should be the next day). | Any call: 500. Overnight shift: wrong end instant. | `shift_end_utc()` returns a UTC naive datetime and adds one day when `shift_end <= shift_start`. |
| 6 | `create_employee` L82, `punch_in` L102 | `datetime.now()` / `fromtimestamp()` use the server's local zone and keep microseconds. Stored "UTC" values are wrong off-UTC, and R1 says truncate to whole seconds. | Run on a non-UTC machine: stored `punch_in` is shifted. `punched_at` with ms=900 keeps the fraction. | `to_datetime()` (UTC, integer maths, whole seconds) and `now_utc_naive()`. |
| 7 | `punch_in`, L103 | Attendance date is `ts.date()` in server time, not the IST date; no overnight rule (R1). | Punch-in at 23:45 IST with a UTC server gets the wrong date. Overnight punch-in at 00:30 gets the wrong day. | `attendance_date_for()` converts to IST and applies the overnight rule. |
| 8 | `PunchInIn`, L65; L102 | `Optional[int]` is lax: accepts `"1783312500000"`, `1.0` and `true`; no range check, so seconds-looking values pass. `if body.punched_at` treats `0` as "not supplied". | `{"punched_at": 1783312500}` returns 201 with a 1970 date instead of 422. | `StrictInt` with `ge/le` epoch-ms limits; `is not None` checks. |
| 9 | `PunchInIn.status`, L66 | Any string is accepted, including `ABSENT`, `LEAVE` and garbage. | `{"status": "LEAVE"}` creates a record that should be impossible. | `Literal["PRESENT", "WFH", "ON_DUTY"]`, so anything else is 422. |
| 10 | `punch_in`, L101-113 | No check that the employee exists: `emp["shift_start"]` on `None` raises `TypeError`. | Unknown `emp_code`: 500 instead of 404. | Explicit 404 check. |
| 11 | `punch_in`, L104-118 | Check-then-insert race, and no unique index. Two simultaneous requests both pass the `find_one` and both insert. | Fire two parallel punch-ins: two 201s and a duplicate record. | Unique index `(emp_code, date)`; `insert_one` + `DuplicateKeyError` gives 409. |
| 12 | `create_employee`, L79-83 | Same race for `emp_code`: no unique index. | Parallel creates of the same code produce duplicates. | Unique index on `emp_code`; `DuplicateKeyError` gives 409. |
| 13 | `create_employee`, L53-60 | No validation: `emp_code` pattern, email, name/department length, `HH:MM` shifts, `shift_start != shift_end`, `joined_on` date. | `{"emp_code": "x", "joined_on": "tomorrow", ...}` returns 201. | Explicit checks, 422 on failure. |
| 14 | Response shapes, L82-85, L119-121, L149 | Raw datetimes are returned (not epoch ms). Punch-in and list responses expose `id` (the Mongo `_id`), which the contract forbids. Legacy records missing `half_day` / `history` are returned as they are. | Response shows ISO strings and an `id` field. | `serialize_attendance()` / `serialize_employee()`: epoch ms, no `_id`, defaults for missing fields. |
| 15 | `list_employees`, L93-95 | `skip = page * page_size` skips a whole page on page 1. `total` counts `{}` instead of the filter. No sort. | `page=1` returns the second page. `?department=X` reports the total of all employees. Order is arbitrary. | `skip = (page - 1) * page_size`, `count_documents(query)`, sort by `emp_code`. |
| 16 | `list_employees` L89, `list_attendance` L130-131 | No bounds on `page` / `page_size`. | `page=0` gives a negative skip and a PyMongo error (500). `page_size=100000` is allowed. | `Query(1, ge=1)`, `Query(20, ge=1, le=100)`. |
| 17 | `health`, L72-74 | Always returns ok, even if MongoDB is down. The client also has no server-selection timeout (30 s default). | Stop MongoDB: `/health` still returns 200. | Pings MongoDB, 503 on error; `serverSelectionTimeoutMS=3000`. |
| 18 | `list_attendance`, L144-147 | Loads the whole collection into memory, then sorts and slices in Python. Not index-backed. | About 100k records: slow and memory-heavy. | `find().sort().skip().limit()` and `count_documents`, backed by indexes. |
| 19 | `list_attendance`, L133-145 | Sorts by `date` only (no `emp_code` tie-break, so page order is unstable). No validation of dates, range or status. | `date_from=garbage`, `date_from > date_to` and `status=NOPE` silently return 200. | Sort `(date desc, emp_code asc)`; 422 for bad dates, reversed range and invalid status. |
| 20 | Whole file | No indexes at all. | `explain` shows `COLLSCAN` everywhere. | `create_indexes()` at startup (see `DECISIONS.md`). |

## Looked at and decided it was fine

- `load_dotenv()` does not override real environment variables, so injected `MONGO_URI` / `MONGO_DB` win.
- `body.model_dump()` is correct for Pydantic v2.
- The `punch_in` document already has the right default derived fields (`work_hours: None`, `overtime_minutes: 0`, `half_day: False`, `history: []`).
- `find(q, {"_id": 0})` in `list_employees` correctly hides `_id`; `doc.pop("_id")` in `create_employee` is also correct.