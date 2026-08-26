import asyncio
from typing import Optional, Callable
from utils.logger import logger

class LoopManager:
    """
    Manages background scheduling tasks for a session.
    Ensures that starting a new loop automatically cancels the previous one.
    Uses the current running loop to create tasks safely.
    """
    def __init__(self, session_id: str):
        self.session_id = session_id
        self._loop_task: Optional[asyncio.Task] = None

    async def start_loop(self, coro_func: Callable, *args):
        """
        Starts a new background loop. 
        Uses asyncio.get_running_loop() to ensure compatibility with multi-threaded environments.
        """
        await self.stop_loop()
        
        loop = asyncio.get_running_loop()
        self._loop_task = loop.create_task(coro_func(*args))
        
        logger.info(f"[{self.session_id}] New loop manager cycle started via loop.create_task.")

    async def stop_loop(self):
        if self._loop_task and not self._loop_task.done():
            self._loop_task.cancel()
            try: await self._loop_task
            except asyncio.CancelledError: pass
            self._loop_task = None
            logger.info(f"[{self.session_id}] Existing loop task cancelled.")

    @property
    def is_running(self):
        return self._loop_task is not None and not self._loop_task.done()
