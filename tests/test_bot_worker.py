import pytest
import asyncio
import time
from unittest.mock import AsyncMock, MagicMock
from core.bot_worker import BotWorker, WorkerState
from pyrogram.errors import FloodWait

@pytest.mark.asyncio
async def test_worker_initialization():
    mock_client = MagicMock()
    worker = BotWorker(mock_client, "+123456", "123456", ["user1"], "@source", 15)
    assert worker.phone == "+123456"
    assert worker.loop_interval == 15
    assert worker.state == WorkerState.IDLE

@pytest.mark.asyncio
async def test_trigger_dispatch_queues_targets():
    mock_client = MagicMock()
    worker = BotWorker(mock_client, "+123456", "123456", ["user1", "user2"], "@source", 15)
    
    await worker.trigger_dispatch(123, 456)
    
    assert worker.queue.qsize() == 2
    assert worker.current_msg_id == 456
    assert worker.current_from_chat == 123

@pytest.mark.asyncio
async def test_worker_handles_flood_wait(mocker):
    mock_client = MagicMock()
    # Mock forward_messages to raise FloodWait on first call, then succeed
    mock_client.forward_messages = AsyncMock(side_effect=[FloodWait(2), True])
    
    worker = BotWorker(mock_client, "+123456", "123456", ["user1"], "@source", 15)
    worker.current_from_chat = 123
    worker.current_msg_id = 456
    
    # Run the send logic
    success = await worker._send_msg("user1")
    
    assert success is False
    assert worker.state == WorkerState.IDLE # Becomes idle after error handling
    assert worker.cooldown_until > time.monotonic()
    # Check if it re-queued the target
    assert worker.queue.qsize() == 1

@pytest.mark.asyncio
async def test_worker_retry_logic_on_failure():
    mock_client = MagicMock()
    # Simulate 3 consecutive failures
    mock_client.forward_messages = AsyncMock(side_effect=Exception("Generic Error"))
    
    worker = BotWorker(mock_client, "+123456", "123456", ["user1"], "@source", 15)
    worker.current_from_chat = 123
    worker.current_msg_id = 456
    
    # Mock sleep to speed up test
    mocker.patch("asyncio.sleep", return_value=None)
    
    success = await worker._send_msg("user1")
    
    assert success is False
    assert mock_client.forward_messages.call_count == 3

@pytest.mark.asyncio
async def test_dynamic_interval_update():
    mock_client = MagicMock()
    worker = BotWorker(mock_client, "+123456", "123456", ["user1"], "@source", 15)
    worker.is_loop_active = True
    
    # Mock scheduler start
    worker._start_scheduler = MagicMock()
    
    worker.update_settings("@new_source", 30, ["user2"])
    
    assert worker.source_channel == "@new_source"
    assert worker.loop_interval == 30
    assert worker.targets == ["user2"]
    worker._start_scheduler.assert_called_once()
