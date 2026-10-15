# REVIEW.md

## Defects found and fixes

| # | Where | What is wrong | How to notice | How it was fixed |
|---|---|---|---|---|
| 1 | `health` | Always returns success without checking MongoDB connectivity. | Stop MongoDB and call `/health`. | Check database connectivity and return an error when unavailable. |
| 2 | `create_employee` | A pre-insert duplicate check alone is not safe under concurrent requests. | Submit simultaneous requests with the same employee code. | Add a unique employee-code index and handle duplicate-key errors as HTTP 409. |
| 3 | `list_employees` | Pagination skips the first page, and the total ignores the department filter. | Request page 1 and compare totals with a filtered query. | Use `(page - 1) * page_size` and count filtered results. |
| 4 | `compute_late_minutes` | The calculation can produce negative minutes for early arrivals and does not precisely implement the required grace-period boundary. | Test arrivals before shift start and around 10 minutes late. | Calculate late minutes using the specified IST shift-time rules and whole-minute semantics. |
| 5 | `compute_work_hours` | Built-in `round()` does not guarantee the required half-up rounding. | Test durations whose second decimal digit is exactly halfway. | Use explicit decimal half-up rounding. |
| 6 | `compute_overtime` | It counts overtime without enforcing the 30-minute minimum and does not correctly handle overnight shifts. | Test punch-outs before shift end, at 29 minutes, at 30 minutes, and after an overnight shift. | Calculate overtime relative to the correct shift end and apply the minimum threshold. |
| 7 | `punch_in` | It does not safely handle unknown employees, validate all input rules, or consistently derive the attendance date using IST and overnight-shift rules. | Submit an unknown employee, invalid timestamp/status, or overnight punch-in. | Add request validation, employee checks, UTC/IST conversion, and attendance-date calculation. |
| 8 | `punch_in` | Checking for an existing attendance record before insertion does not prevent concurrent duplicate records. | Send simultaneous punch-in requests for the same employee and date. | Add a unique compound index on employee code and attendance date and handle duplicate-key errors. |
| 9 | `list_attendance` | It loads all matching records into Python before paginating and does not provide the required deterministic secondary sort. | Query a large dataset or compare records sharing a date. | Apply filtering, sorting, counting, skip, and limit in MongoDB. |
| 10 | Missing endpoints | Punch-out, attendance regularization, analytics, and query-plan explain routes were not implemented in the starter. | Compare the starter routes with `openapi.yaml`. | Implement the required endpoints and their validation, aggregation, and concurrency handling. |

## Reviewed behavior

- The original duplicate checks are useful for early feedback, but they are not a substitute for database unique indexes.
- Rounding, attendance dates, and shift boundaries must follow the API contract rather than assumptions based on the machine's local timezone.
- Indexes improve query performance but should be evaluated against actual query patterns and write overhead.
