# Evidence storage

Ringer writes local evidence as one JSON object per line. The default file is
`~/.ringer/runs.jsonl`; the configured JSONL path is used when set. Each row
keeps the existing fields and adds `logged_at`, `log_sink`, and
`fallback_reason`. JSONL is written locally without a dashboard. The explicitly
configured PostgreSQL backend remains separate.

Ringer serializes each row before opening the evidence file. Cooperating
Ringer processes and threads take an exclusive file lock while checking the
last record and appending. A short write is completed; an interrupted write is
retried. A write error attempts to truncate only bytes from that failed append
while the lock is held. If that rollback also fails, the error reports both
failures. Earlier complete rows are not intentionally changed.

Before append, Ringer checks only the final existing row. If the file ends in
an incomplete line, or its final line is not a JSON object, Ringer stops and
leaves the file unchanged. Preserve the file and repair or restore its tail
manually before retrying. It does not scan the full history or silently discard
data. This is not a power-loss durability guarantee: Ringer does not promise
that recently written data survives a machine or storage failure.

`run_id` identifies a run, and `task_key` identifies its task context. Neither
is a unique attempt identifier. The existing `retry` field is Boolean; it does
not distinguish multiple retry numbers. Do not use these fields alone as a
unique key for individual attempts.
