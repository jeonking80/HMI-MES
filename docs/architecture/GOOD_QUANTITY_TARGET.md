# Target quantity is good output

The system specification §10 defines target_quantity as 목표 양품 수량 (target
good quantity). A raw input cap stops too early after rejection or explicit WIP
scrap. The controller therefore permits a new item only while
`good_quantity + wip_quantity < target_quantity`, with the existing moving and
not-ENDING permissions still required.

Each in-flight item reserves one potential good unit. Good inspection converts
that reservation to completed good output without creating capacity. Confirmed
rejection or explicit scrap frees capacity for a replacement; a future inspection
is never guessed to be a reject. This prevents over-admission while allowing total
input to exceed the good target by the actual reject/scrap quantities. Conservation
remains input = good + reject + scrap + WIP on every durable process boundary.

Reaching the target stops new admission after existing WIP drains. It does not
create END_RUN, switch execution, fabricate an ended summary or automatically
restart. Explicit END_RUN still stops admission immediately and drains the
existing items without replacing losses, even if the final good quantity is below
the target. Existing final-boundary and MES settlement rules remain unchanged.

Verification uses deterministic GOOD, GOOD, REJECT inspection order and an
explicitly verified paused-item scrap. A good target of four reaches exactly four
good, one reject, one scrap, zero WIP and six total input; it remains RUNNING until
END_RUN. A separate early END_RUN drains three items to two good and one reject
without admitting replacements toward its larger target.

```sh
uv run pytest -q tests/integration/test_simulator_controller.py tests/integration/test_controller_retention.py
```

## CX-33 approved automatic completion

The earlier explicit-END-only rule above is superseded for new opted-in LOADs.
Reason: an automatic packaging run should complete when the PLC observes its good
output target, while preserving the original execution and durable accounting.
LOAD auto_end_at_target defaults true; binding exposes the same option. False
keeps the preceding explicit-END behavior, including the existing good+WIP cap.

The PLC checks actual process good_quantity, inhibits new admission, drains WIP,
and completes only with zero WIP and stopped output/feedback/speed. Existing
pending commands, unresolved conflicts, recovery, faults and safety inputs retain
priority. It does not start, unpause, clear a fault or change modes. No fabricated
CommandRequest, END_RUN ID or command reservation is used. The final state event,
process quantities, summary and TARGET_AUTO_END audit proof share the storage
owner's transaction. Crash before commit rolls back all of them.

New automatic summaries carry end_reason=TARGET_REACHED; newly completed operator
END_RUN carries OPERATOR. Historical finals retain an unknown/absent reason.
Retention accepts an automatic final only with a unique durable target proof
bound to the successful original explicit-policy LOAD, execution, target, observed
good count, original final event sequence and canonical final summary. Operator
and legacy finals still require their original successful END_RUN completion.
Final source evidence never resolves an unknown API command result by itself.

Existing policy-free journal bytes and final-summary hashes are unchanged.
Old accepted LOADs and restored policy-free PLC state retain false. New omitted
policy resolves true before storing its immutable request. Process dataclass
snapshots are unchanged. Legacy V1 state field sets are frozen during migration
so added fields do not invalidate the known format; old V2 state fills false/None.

UI and rollout remain Claude-owned: exact binding parsers must accept the optional
intent/LOAD flag and summary reason before this backend contract is deployed for
public operation. The running runtime and WO-T1 are not upgraded by this commit.
