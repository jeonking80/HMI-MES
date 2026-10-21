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
