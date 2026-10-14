# DECISIONS.md

1. **Indexes.** Employees: unique `emp_code`, `(department, emp_code)` for the sorted list, `(department, joined_on)` and `(joined_on)` for headcount. Attendance: unique `(emp_code, date)` (one record per day, and employee queries) and `(date desc, emp_code)` for the list sort and month-wide analytics. I rejected an index on `status`; it is rarely queried alone.
2. **Punch-in race.** Both requests reach `insert_one`. The unique `(emp_code, date)` index lets MongoDB accept one and reject the other with a duplicate-key error, which I turn into 409. There is no read before the write.
3. **Ties.** Everyone tied at the cutoff is returned, so there can be more than `limit` rows. I rank with `$rank` (1, 2, 2, 4) and filter on `rank <= limit`, never on row position.
4. **Headcount.** The summary starts from `employees` (joined by month end) and looks up each person's logs. Someone with no logs gets an empty result but is still a row, so they are counted.
5. **At 100x data** I would stop aggregating raw logs per request and keep a small daily rollup per department, updated on punch and correction.
