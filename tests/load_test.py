import asyncio
import time
import sys
import os
from unittest.mock import AsyncMock, MagicMock

# Add project root to path
sys.path.append(os.getcwd())

from core.bot_worker import BotWorker, WorkerState

async def simulate_dispatch_burst(worker):
    """Scenario B: Spam clicking dispatch."""
    print(f"🔥 Starting Scenario B: Burst Dispatch...")
    tasks = []
    for i in range(20):
        tasks.append(worker.trigger_dispatch(100, i))
    await asyncio.gather(*tasks)
    # The queue should only contain targets for the LAST message (id 19)
    print(f"✅ Queue size after burst: {worker.queue.qsize()} (Expected: {len(worker.targets)})")

async def simulate_heavy_load(num_sessions=10, targets_per_session=50):
    """Scenario A: 10 sessions running simultaneously."""
    print(f"🚀 Starting Scenario A: {num_sessions} Sessions Load Test...")
    semaphore = asyncio.Semaphore(3)
    workers = []
    
    for i in range(num_sessions):
        mock_client = MagicMock()
        # Simulate Telegram latency
        mock_client.forward_messages = AsyncMock(side_effect=lambda **kw: asyncio.sleep(0.5))
        
        targets = [f"@target_{j}" for j in range(targets_per_session)]
        w = BotWorker(mock_client, f"+{i}", str(i), targets, "@source", 15, semaphore)
        w.current_msg_id = 999
        w.current_from_chat = 888
        workers.append(w)
        w.start()

    start_time = time.time()
    # Trigger dispatch on all
    for w in workers:
        for t in w.targets:
            await w.queue.put(t)

    # Wait for all queues to empty
    while any(w.queue.qsize() > 0 or w.state == WorkerState.SENDING for w in workers):
        await asyncio.sleep(1)
        active = sum(1 for w in workers if w.state == WorkerState.SENDING)
        total_sent = sum(w.sent for w in workers)
        print(f"📊 Progress: {total_sent}/{num_sessions * targets_per_session} sent. Active Workers: {active}")

    duration = time.time() - start_time
    print(f"\n✅ Load Test Complete in {duration:.2f}s")
    print(f"📈 Performance: {total_sent / duration:.2f} messages/sec")
    
    for w in workers: w.stop()

if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    # Run Scenario B on a single worker
    mock_s = asyncio.Semaphore(1)
    w_test = BotWorker(MagicMock(), "+99", "99", ["@t1", "@t2"], "@src", 1, mock_s)
    loop.run_until_complete(simulate_dispatch_burst(w_test))
    
    # Run Scenario A
    loop.run_until_complete(simulate_heavy_load(num_sessions=5, targets_per_session=20))
