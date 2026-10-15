# DECISIONS.md

1. **Indexes:** I created a unique index on employee codes to prevent duplicate employees and a unique index on employee code plus attendance date to prevent duplicate daily records. Other indexes support department headcount, date-based attendance queries, and employee attendance filtering. Indexes should be reviewed against actual query usage to avoid unnecessary write overhead.

2. **Punch-in race:** If two identical punch-in requests arrive together, MongoDB's unique attendance index allows only one record for that employee and date. The other insert raises `DuplicateKeyError`, which the API converts to HTTP 409 Conflict.

3. **Ties:** The leaderboard uses `$rank` based on total late minutes. Employees tied at the cutoff receive the same rank, so the response may contain more employees than the requested limit.

4. **Headcount:** The department summary starts with employees and uses `$lookup` with `preserveNullAndEmptyArrays`, ensuring employees without attendance logs are included.

5. **Scaling:** For 100 times more data, I would review query plans and index usage, optimize aggregation pipelines, and add load testing and monitoring.