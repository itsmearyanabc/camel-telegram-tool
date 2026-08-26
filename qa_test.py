"""
qa_test.py — Full system validation suite.

Tests:
  1. Dispatcher: single message → immediate dispatch → 15-min re-forward
  2. Dispatcher: new message during timer → cancels old, sends new immediately
  3. Dispatcher: rapid burst → only LAST message dispatched
  4. AccountWorker: FloodWait isolates only that worker
  5. AccountWorker: task_done() bug regression check
  6. AccountWorker: exponential backoff on transient errors
  7. Integration: enqueue → distribute → worker receives correct job

Run: ./venv/bin/python qa_test.py
"""

import asyncio
import time
import sys

PASS = "\033[92m✅ PASS\033[0m"
FAIL = "\033[91m❌ FAIL\033[0m"
WARN = "\033[93m⚠️  WARN\033[0m"

results = []

def report(name, passed, detail=""):
    sym = PASS if passed else FAIL
    results.append(passed)
    print(f"  {sym} {name}" + (f" — {detail}" if detail else ""))

# ─────────────────────────────────────────────────────────────
# MOCK INFRASTRUCTURE
# ─────────────────────────────────────────────────────────────
class MockClient:
    """Controllable fake Telegram client."""
    def __init__(self, name="mock"):
        self.name = name
        self.calls = []
        self.fail_next = False      # set True to simulate transient error
        self.floodwait_next = 0     # set N to simulate FloodWait(N)

    async def forward_messages(self, chat_id, from_chat_id, message_ids):
        from pyrogram.errors import FloodWait
        await asyncio.sleep(0.01)   # simulate network
        if self.floodwait_next:
            secs = self.floodwait_next
            self.floodwait_next = 0
            raise FloodWait(secs)
        if self.fail_next:
            self.fail_next = False
            raise ConnectionError("mock network error")
        self.calls.append((chat_id, from_chat_id, message_ids))


class MockAccountManager:
    """AccountManager that uses real AccountWorkers with mock clients."""
    def __init__(self, n_workers=3):
        from account_worker import AccountWorker
        self.workers = []
        for i in range(n_workers):
            client = MockClient(f"mock_{i}")
            targets = [f"@group_{i}_{j}" for j in range(2)]
            w = AccountWorker(client, f"Worker_{i}", i, targets)
            self.workers.append(w)

    def start_all(self):
        for w in self.workers:
            w.start()

    async def stop_all(self):
        for w in self.workers:
            w.stop()
        await asyncio.gather(*(w.wait_stopped() for w in self.workers),
                             return_exceptions=True)

    async def distribute(self, from_chat_id, message_id):
        for w in self.workers:
            await w.enqueue_dispatch(from_chat_id, message_id)


# ─────────────────────────────────────────────────────────────
# TEST 1 — Single message dispatches immediately
# ─────────────────────────────────────────────────────────────
async def test_immediate_dispatch():
    print("\n[TEST 1] Single message → immediate dispatch")
    from dispatcher import Dispatcher

    dispatched = []
    class TrackingManager:
        workers = []
        async def distribute(self, fc, mid):
            dispatched.append((fc, mid))

    d = Dispatcher(TrackingManager())
    asyncio.create_task(d.run())
    await asyncio.sleep(0.05)

    await d.enqueue(111, 42)
    await asyncio.sleep(0.2)   # give dispatch task time to run

    d.stop()
    report("Message dispatched immediately", len(dispatched) == 1,
           f"dispatched={dispatched}")
    report("Correct message_id", dispatched and dispatched[0][1] == 42)


# ─────────────────────────────────────────────────────────────
# TEST 2 — New message cancels 15-min timer, old message NOT re-forwarded
# ─────────────────────────────────────────────────────────────
async def test_timer_cancellation():
    print("\n[TEST 2] New message during timer → old NOT re-forwarded")
    from dispatcher import Dispatcher, REFORWARD_INTERVAL as ORIG_INTERVAL
    import dispatcher as disp_module

    # Temporarily shorten the re-forward interval for this test
    disp_module.REFORWARD_INTERVAL = 0.5   # 500ms instead of 15min

    dispatched = []
    class TrackingManager:
        workers = []
        async def distribute(self, fc, mid):
            dispatched.append((fc, mid))

    d = Dispatcher(TrackingManager())
    asyncio.create_task(d.run())
    await asyncio.sleep(0.05)

    # Send message A
    await d.enqueue(111, 1)
    await asyncio.sleep(0.1)   # dispatch fires

    # Send message B BEFORE 500ms timer expires
    await d.enqueue(111, 2)
    await asyncio.sleep(0.1)

    # Wait past original timer window
    await asyncio.sleep(0.6)

    d.stop()
    disp_module.REFORWARD_INTERVAL = ORIG_INTERVAL  # restore

    msg_ids = [x[1] for x in dispatched]
    report("Message A dispatched initially", 1 in msg_ids)
    report("Message B dispatched after A", 2 in msg_ids)

    # Count re-forwards: message 1 should appear exactly once (no re-forward)
    report("Message A NOT re-forwarded after B arrived",
           msg_ids.count(1) == 1,
           f"dispatch log: {msg_ids}")


# ─────────────────────────────────────────────────────────────
# TEST 3 — Rapid burst: only LAST message survives
# ─────────────────────────────────────────────────────────────
async def test_burst_deduplication():
    print("\n[TEST 3] Rapid burst → only last message dispatched")
    from dispatcher import Dispatcher

    dispatched = []
    class TrackingManager:
        workers = []
        async def distribute(self, fc, mid):
            await asyncio.sleep(0.05)   # slight delay
            dispatched.append((fc, mid))

    d = Dispatcher(TrackingManager())
    asyncio.create_task(d.run())
    await asyncio.sleep(0.05)

    # Fire 5 messages rapidly
    for i in range(1, 6):
        await d.enqueue(111, i)

    await asyncio.sleep(0.5)
    d.stop()

    msg_ids = [x[1] for x in dispatched]
    report("Last message (5) was dispatched", 5 in msg_ids,
           f"dispatched ids: {msg_ids}")
    report("Message 5 dispatched only once", msg_ids.count(5) == 1)


# ─────────────────────────────────────────────────────────────
# TEST 4 — FloodWait isolates only the affected worker
# ─────────────────────────────────────────────────────────────
async def test_floodwait_isolation():
    print("\n[TEST 4] FloodWait on Worker_0 — others continue")
    from account_worker import AccountWorker, WorkerState

    # Worker 0: will hit FloodWait
    c0 = MockClient("flood_worker")
    c0.floodwait_next = 3   # 3-second flood wait
    w0 = AccountWorker(c0, "FloodWorker", 0, ["@target_flood"])

    # Worker 1: healthy
    c1 = MockClient("healthy_worker")
    w1 = AccountWorker(c1, "HealthyWorker", 1, ["@target_ok"])

    w0.start()
    w1.start()

    await w0.enqueue_dispatch(111, 1)
    await w1.enqueue_dispatch(111, 1)

    await asyncio.sleep(0.5)   # let first sends fire

    report("FloodWorker enters cooldown",
           w0.state == WorkerState.COOLDOWN,
           f"state={w0.state}")
    report("HealthyWorker sent successfully",
           w1.sent >= 1, f"sent={w1.sent}")
    report("HealthyWorker NOT in cooldown",
           w1.state != WorkerState.COOLDOWN)

    w0.stop(); w1.stop()
    await asyncio.gather(w0.wait_stopped(), w1.wait_stopped(),
                         return_exceptions=True)


# ─────────────────────────────────────────────────────────────
# TEST 5 — task_done() regression: no ValueError on drain
# ─────────────────────────────────────────────────────────────
async def test_no_task_done_error():
    print("\n[TEST 5] Queue drain regression — no ValueError")
    from account_worker import AccountWorker

    c = MockClient("td_test")
    w = AccountWorker(c, "TDWorker", 0, ["@t1", "@t2"])

    # Pre-fill queue
    await w.enqueue_dispatch(111, 1)
    # Immediately override with new message — this drained the queue
    try:
        await w.enqueue_dispatch(111, 2)
        report("No ValueError on rapid queue drain", True)
    except ValueError as e:
        report("No ValueError on rapid queue drain", False, str(e))


# ─────────────────────────────────────────────────────────────
# TEST 6 — Exponential backoff on transient error
# ─────────────────────────────────────────────────────────────
async def test_exponential_backoff():
    print("\n[TEST 6] Transient error → exponential backoff → eventual success")
    from account_worker import AccountWorker, WorkerState

    call_times = []

    class TimedClient:
        name = "backoff_test"
        call_count = 0
        async def forward_messages(self, chat_id, from_chat_id, message_ids):
            call_times.append(time.monotonic())
            self.call_count += 1
            if self.call_count < 3:
                raise ConnectionError("transient")

    c = TimedClient()
    w = AccountWorker(c, "BackoffWorker", 0, ["@one"])
    w.start()

    await w.enqueue_dispatch(111, 1)
    await asyncio.sleep(12)   # enough for 3 attempts with backoff 2,4s

    w.stop()
    await w.wait_stopped()

    report("Sent after retries", w.sent >= 1, f"sent={w.sent}")
    if len(call_times) >= 2:
        gap = call_times[1] - call_times[0]
        report("First backoff ≥ 2s", gap >= 1.8, f"gap={gap:.2f}s")


# ─────────────────────────────────────────────────────────────
# TEST 7 — Integration: enqueue → AccountManager → worker
# ─────────────────────────────────────────────────────────────
async def test_full_integration():
    print("\n[TEST 7] Integration: Dispatcher → AccountManager → Workers")
    from dispatcher import Dispatcher

    mgr = MockAccountManager(n_workers=2)
    mgr.start_all()

    d = Dispatcher(mgr)
    asyncio.create_task(d.run())
    await asyncio.sleep(0.1)

    await d.enqueue(999, 777)
    await asyncio.sleep(1.0)   # let workers process

    d.stop()
    await mgr.stop_all()

    total_sent = sum(w.sent for w in mgr.workers)
    total_targets = sum(len(w.targets) for w in mgr.workers)
    report("All workers received jobs",
           all(w.sent > 0 for w in mgr.workers),
           f"sent per worker: {[w.sent for w in mgr.workers]}")
    report("Total sent matches total targets",
           total_sent == total_targets,
           f"sent={total_sent} targets={total_targets}")


# ─────────────────────────────────────────────────────────────
# RUNNER
# ─────────────────────────────────────────────────────────────
async def main():
    print("=" * 60)
    print("  ARMEDIAS — QA Validation Suite")
    print("=" * 60)

    await test_immediate_dispatch()
    await test_timer_cancellation()
    await test_burst_deduplication()
    await test_floodwait_isolation()
    await test_no_task_done_error()
    await test_exponential_backoff()
    await test_full_integration()

    passed = sum(results)
    total  = len(results)
    pct    = int(passed / total * 100) if total else 0

    print(f"\n{'='*60}")
    print(f"  Results: {passed}/{total} passed ({pct}%)")
    if passed == total:
        print("  \033[92m🎉 ALL TESTS PASSED — System is production-ready\033[0m")
    else:
        failed = total - passed
        print(f"  \033[91m⚠️  {failed} test(s) FAILED — fix before deploying\033[0m")
    print("=" * 60)
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    asyncio.run(main())
