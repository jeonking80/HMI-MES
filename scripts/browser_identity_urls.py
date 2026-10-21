"""Fixed identity-query URLs shared by actual App A browser checks."""

from urllib.parse import urlencode


def work_action(origin, kind, order):
    return origin + "/api/v1/work-order-actions/" + kind + "?" + urlencode({"work_order_id": order})


def work_receipt(origin, kind, order, identity):
    key = {"release": "release_id", "binding": "command_id", "cancellation": "cancellation_id"}[
        kind
    ]
    return (
        origin
        + "/api/v1/work-order-receipts/"
        + kind
        + "?"
        + urlencode({"work_order_id": order, key: identity})
    )


def work_detail(origin, order):
    return origin + "/api/v1/work-order-detail?" + urlencode({"work_order_id": order})
