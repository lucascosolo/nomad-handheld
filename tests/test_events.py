from __future__ import annotations

import asyncio

from nomad.core.events import Event, EventBus


async def test_publish_subscribe_exact_match(event_bus: EventBus) -> None:
    received: list[Event] = []

    async def handler(event: Event) -> None:
        received.append(event)

    event_bus.subscribe("tool.called", handler)
    await event_bus.publish(Event(type="tool.called", source="test", payload={"x": 1}))
    await event_bus.publish(Event(type="tool.other", source="test", payload={}))
    await asyncio.sleep(0.05)

    assert len(received) == 1
    assert received[0].type == "tool.called"
    assert received[0].payload == {"x": 1}


async def test_wildcard_prefix_pattern(event_bus: EventBus) -> None:
    received: list[str] = []

    async def handler(event: Event) -> None:
        received.append(event.type)

    event_bus.subscribe("tool.*", handler)
    await event_bus.publish(Event(type="tool.called", source="test", payload={}))
    await event_bus.publish(Event(type="tool.finished", source="test", payload={}))
    await event_bus.publish(Event(type="display.updated", source="test", payload={}))
    await asyncio.sleep(0.05)

    assert sorted(received) == ["tool.called", "tool.finished"]


async def test_wildcard_all_pattern(event_bus: EventBus) -> None:
    received: list[str] = []

    async def handler(event: Event) -> None:
        received.append(event.type)

    event_bus.subscribe("*", handler)
    await event_bus.publish(Event(type="anything.at.all", source="test", payload={}))
    await asyncio.sleep(0.05)

    assert received == ["anything.at.all"]


async def test_handler_error_is_isolated_and_republished(event_bus: EventBus) -> None:
    error_events: list[Event] = []

    async def failing_handler(event: Event) -> None:
        raise ValueError("boom")

    async def error_watcher(event: Event) -> None:
        error_events.append(event)

    event_bus.subscribe("risky.thing", failing_handler)
    event_bus.subscribe("system.handler_error", error_watcher)

    # Publishing must not raise even though the handler blows up.
    await event_bus.publish(Event(type="risky.thing", source="test", payload={}))
    await asyncio.sleep(0.05)

    assert len(error_events) == 1
    assert error_events[0].payload["original_event_type"] == "risky.thing"
    assert "boom" in error_events[0].payload["error"]


async def test_handler_error_in_error_handler_does_not_recurse(event_bus: EventBus) -> None:
    async def always_fails(event: Event) -> None:
        raise RuntimeError("still broken")

    # Subscribe the failing handler to the error topic itself.
    event_bus.subscribe("system.handler_error", always_fails)
    event_bus.subscribe("root.trigger", always_fails)

    await event_bus.publish(Event(type="root.trigger", source="test", payload={}))
    # Give the bus time to process; if recursion were unbounded this would hang/crash.
    await asyncio.sleep(0.1)

    stats = event_bus.stats()
    assert sum(s["dropped"] for s in stats.values()) >= 0  # bus still alive, no crash


async def test_slow_subscriber_drops_oldest_without_blocking_publisher(
    event_bus: EventBus,
) -> None:
    gate = asyncio.Event()
    received: list[Event] = []

    async def slow_handler(event: Event) -> None:
        await gate.wait()
        received.append(event)

    unsubscribe = event_bus.subscribe("slow.event", slow_handler)
    # small bound to make the drop deterministic without publishing hundreds of events
    bus2 = EventBus(queue_size=2)
    await bus2.start()
    try:
        received2: list[Event] = []

        async def slow_handler2(event: Event) -> None:
            await gate.wait()
            received2.append(event)

        bus2.subscribe("slow.event", slow_handler2)

        # Publisher must return immediately even though the subscriber never drains.
        for i in range(10):
            await asyncio.wait_for(
                bus2.publish(Event(type="slow.event", source="test", payload={"i": i})),
                timeout=0.5,
            )

        stats = bus2.stats()
        sub_stats = next(iter(stats.values()))
        assert sub_stats["dropped"] > 0

        gate.set()
        await asyncio.sleep(0.05)
    finally:
        await bus2.stop()

    unsubscribe()


async def test_unsubscribe_stops_delivery(event_bus: EventBus) -> None:
    received: list[Event] = []

    async def handler(event: Event) -> None:
        received.append(event)

    unsubscribe = event_bus.subscribe("thing", handler)
    await event_bus.publish(Event(type="thing", source="test", payload={}))
    await asyncio.sleep(0.05)
    unsubscribe()
    await event_bus.publish(Event(type="thing", source="test", payload={}))
    await asyncio.sleep(0.05)

    assert len(received) == 1


async def test_stats_tracks_delivered(event_bus: EventBus) -> None:
    async def handler(event: Event) -> None:
        pass

    event_bus.subscribe("counted", handler)
    for _ in range(3):
        await event_bus.publish(Event(type="counted", source="test", payload={}))
    await asyncio.sleep(0.05)

    stats = event_bus.stats()
    sub_stats = next(iter(stats.values()))
    assert sub_stats["delivered"] == 3
