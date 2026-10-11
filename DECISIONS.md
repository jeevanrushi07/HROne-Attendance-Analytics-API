# DECISIONS.md

1. **Indexes.** A unique index on `employees.emp_code` prevents duplicate identities. A unique `(emp_code, date)` index makes punch-in race-safe. Department/join-date and join-date indexes support headcount queries; attendance indexes support employee/date filters, date sorting and monthly analytics. I did not add an index for every possible field combination because extra indexes slow writes and consume storage.

2. **Punch-in race.** Both requests may initially find no record, but MongoDB enforces the unique `(emp_code, date)` key. One insert succeeds with 201; the other raises `DuplicateKeyError`, which the API converts to 409. The preliminary read is not treated as the concurrency guarantee.

3. **Ties.** `$rank` assigns equal totals the same rank and skips the next rank. Filtering `rank <= limit` after ranking keeps all employees tied at the cutoff; output ties are ordered by employee code.

4. **Headcount.** The department summary starts from employees and looks up attendance logs. Employees who joined by the period end remain in the aggregation even when the lookup returns no logs; employees who join later are filtered out.

5. **100x data.** I would benchmark query plans and cardinalities, then consider pre-aggregated daily summaries and partitioning/archive policies based on measured workload. I would retain correctness checks and validate every change against the API contract.
