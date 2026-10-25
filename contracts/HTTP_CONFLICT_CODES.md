# Browser HTTP 409 conflict contract — CX-31

Browser work-order registration/actions and command submission use a flat JSON response:

```json
{"detail":"registration conflicts with immutable content","reason_code":"WORK_ORDER_VERSION_CHANGED"}
```

`reason_code` is one of the fixed `ConflictReason` values in `contracts/conflict_codes.py`.
Existing public `detail` strings remain fixed by route; they are not private exception diagnostics.

| reason_code | Meaning |
|---|---|
| RESERVED_EXECUTION_NOT_SETTLED | The previous reserved execution is not settled. |
| WORK_ORDER_VERSION_CHANGED | The submitted work-order version is stale or exhausted. |
| WORK_ORDER_ALREADY_BOUND | The work order already owns its original execution. |
| FRESH_SOURCE_REQUIRED | Fresh, complete and verified source evidence is required. |
| PHYSICAL_EXECUTION_MISMATCH | Physical execution, source, epoch or reservation does not match. |
| ID_CONTENT_CONFLICT | An identifier already belongs to different immutable content. |
| CONTROL_VERSION_STALE | The submitted control version differs from the observed version. |
| STATE_NOT_ALLOWED | The current mode, state, reservation or WIP forbids this request. |

These codes describe the rejected admission; they do not report PLC success or resolve an
unknown earlier request. Recover an earlier request only by its original ID. A stale work-order
version is checked before an already-bound work-order link, preserving existing precedence.

HTTP 401/403 authorization and permission checks remain unchanged. No internal exception text,
SQL, credentials or private paths are returned. Internal gateway protocols are unchanged.
