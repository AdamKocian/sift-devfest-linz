"""The noisy-shop telemetry mix (docs/DEMO_WORLD.md), as records per day at full scale.

Every row is one Sift signal. `scale` shrinks the whole world evenly, so the shares stay as
in the demo while a trial tenant ingests only a few MB. The eight planted cases are
marked with `case`."""

from __future__ import annotations

from dataclasses import dataclass, field

GIB = 1024**3
SERVICES = ("frontend", "cart", "checkout", "payment", "inventory", "email", "recommendation")


@dataclass(frozen=True)
class LogSignal:
    service: str
    source: str  # sent as the log.source attribute
    level: str
    gib_week: float
    avg_bytes: int
    messages: tuple[str, ...]  # templates, see emit.render
    case: int | None = None
    attributes: dict[str, str] = field(default_factory=dict)

    @property
    def records_day(self) -> float:
        return self.gib_week / 7 * GIB / self.avg_bytes


@dataclass(frozen=True)
class SpanSignal:
    service: str
    name: str
    gib_week: float
    avg_bytes: int
    attributes: dict[str, str]
    case: int | None = None

    @property
    def records_day(self) -> float:
        return self.gib_week / 7 * GIB / self.avg_bytes


TRACE = "{trace}"  # placeholder for a Java stack trace

# The demo world's log volumes. Patterns (cases 3, 4, 6, 7) are their own rows here; their
# source row carries only the rest of the source's volume.
LOGS: tuple[LogSignal, ...] = (
    LogSignal("payment", "app", "DEBUG", 80.5, 1100, (
        "Processing gateway payload txn={hex:12} bytes={int:3000-5000} payload={hex:900}",
        "Token exchange raw response status=200 body={hex:960}",
    ), case=1),
    LogSignal("payment", "app", "INFO", 6.0, 520, (
        "Charge captured order={int:100000-999999} amount={int:5-900}.{int:10-99} currency=EUR",
    )),
    LogSignal("payment", "app", "ERROR", 0.8, 1400, (
        "Gateway declined order={int:100000-999999} code={choice:card_declined|expired_card}",
    )),
    LogSignal("checkout", "app", "INFO", 41.4, 610, (
        "Order placed order_id={int:100000-999999} items={int:1-9} total={int:5-900}.{int:10-99}",
        "Payment confirmed order_id={int:100000-999999} provider={choice:stripe|adyen}",
    ), case=8),
    LogSignal("checkout", "app", "ERROR", 0.6, 1500, (
        "Order failed order_id={int:100000-999999} reason={choice:inventory_unavailable|timeout}",
    )),
    LogSignal("checkout", "audit", "INFO", 2.5, 700, (
        "Refund approved actor={email} order_id={int:100000-999999}",
    )),
    LogSignal("cart", "app", "WARN", 2.4, 1900, (
        "Retrying inventory call after {int:500-4000}ms attempt={int:1-3}",
    )),
    # Case 3: one timeout, re-logged three times with the same 45-line stack trace.
    LogSignal("cart", "app", "WARN", 27.6, 3900, (
        "Timeout calling inventory: SocketTimeoutException" + TRACE,
    ), case=3),
    LogSignal("cart", "app", "INFO", 5.0, 450, (
        "Item added cart={uuid} sku=SKU-{int:1000-9999} qty={int:1-5}",
    )),
    LogSignal("inventory", "app", "INFO", 1.6, 480, (
        "Reserved sku=SKU-{int:1000-9999} qty={int:1-5} warehouse=linz-{int:1-3}",
    )),
    LogSignal("inventory", "app", "INFO", 18.4, 220, (
        "heartbeat ok - worker {worker} alive",
    ), case=4),
    LogSignal("inventory", "app", "WARN", 1.4, 600, (
        "Low stock sku=SKU-{int:1000-9999} remaining={int:1-9}",
    )),
    LogSignal("recommendation", "app", "ERROR", 0.3, 1600, (
        "Failed to score request session={uuid} after {int:100-900}ms",
    )),
    LogSignal("recommendation", "app", "ERROR", 13.5, 2950, (
        "ModelWeightsExpiredException: weights older than {int:72-96}h, reload failed" + TRACE,
    ), case=7, attributes={"exception.type": "ModelWeightsExpiredException"}),
    LogSignal("recommendation", "app", "INFO", 4.5, 380, (
        "Served {int:5-20} recommendations session={uuid}",
    )),
    LogSignal("email", "app", "INFO", 0.8, 1270, (
        "Queued order confirmation order_id={int:100000-999999}",
    )),
    LogSignal("email", "app", "INFO", 9.2, 310, (
        "Sent confirmation to user: {email} order_id={int:100000-999999}",
    ), case=6),
    LogSignal("email", "app", "ERROR", 0.3, 1200, (
        "Bounce from provider code={choice:550|552|554} order_id={int:100000-999999}",
    )),
    LogSignal("frontend", "nginx access", "INFO", 12.0, 410, (
        "GET /{choice:api/products|api/cart|product} {choice:200|304} {int:2-90}ms ua={hex:8}",
        "POST /{choice:api/cart|api/checkout} {choice:200|201} {int:20-400}ms ua={hex:8}",
    )),
    LogSignal("frontend", "app", "WARN", 1.2, 520, (
        "Slow render route=/product/{int:1-999} took {int:1000-3000}ms",
    )),
)  # fmt: skip

CART_FUNCTIONS = ("InventoryClient.reserve", "CartService.reserve", "CartController.addItem")

SPANS: tuple[SpanSignal, ...] = (
    # Case 2: Kubernetes probes every 250 ms. Failed probes (non-200) must stay traced.
    SpanSignal("frontend", "GET /healthz", 33.75, 1250,
               {"http.request.method": "GET", "http.route": "/healthz"}, case=2),
    SpanSignal("frontend", "GET /api/products", 28.0, 1600,
               {"http.request.method": "GET", "http.route": "/api/products"}),
    SpanSignal("frontend", "GET /static/*", 3.25, 900,
               {"http.request.method": "GET", "http.route": "/static/*"}),
    SpanSignal("checkout", "POST /checkout", 22.0, 2100,
               {"http.request.method": "POST", "http.route": "/checkout"}),
    SpanSignal("cart", "CartService/AddItem", 14.0, 1300, {"rpc.system": "grpc"}),
    SpanSignal("payment", "PaymentService/Charge", 12.0, 1700, {"rpc.system": "grpc"}),
    SpanSignal("inventory", "SELECT inventory.stock", 10.5, 1900, {
        "db.system": "postgresql",
        "db.statement": "SELECT qty, reserved FROM inventory.stock"
        " WHERE sku = $1 AND warehouse = $2 /* " + "x" * 700 + " */",
    }),
    SpanSignal("recommendation", "ListRecommendations", 8.0, 1400, {"rpc.system": "grpc"}),
    SpanSignal("email", "SendOrderConfirmation", 3.5, 1100, {"messaging.system": "kafka"}),
)  # fmt: skip

# Case 5: http.server.request.duration carries user.id, one value per visitor.
HTTP_DURATION_RECORDS_DAY = 26.0 / 7 * GIB / 180
GAUGES = (
    # (service, metric, unit, attribute sets)
    ("checkout", "jvm.memory.used", "By",
     [{"jvm.memory.pool.name": p, "jvm.memory.type": "heap"}
      for p in ("G1 Eden Space", "G1 Old Gen")]),
    ("inventory", "db.client.connection.count", "{connection}",
     [{"db.client.connection.state": s} for s in ("used", "idle")]),
    ("inventory", "inventory.stock.level", "{item}",
     [{"sku": f"SKU-{1000 + i}", "warehouse": f"linz-{i % 3 + 1}"} for i in range(12)]),
    ("recommendation", "process.cpu.utilization", "1", [{"host.name": "reco-1"}]),
)  # fmt: skip


def planted_cases() -> dict[int, str]:
    out: dict[int, str] = {}
    for x in LOGS:
        if x.case:
            out[x.case] = f"{x.service} {x.level} logs"
    for s in SPANS:
        if s.case:
            out[s.case] = f"{s.service} {s.name} spans"
    out[5] = "http.server.request.duration user.id dimension"
    return out
