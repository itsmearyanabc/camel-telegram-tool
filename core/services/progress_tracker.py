import asyncio

class ProgressTracker:
    """
    Handles atomic progress tracking for a single dispatch session.
    Ensures sent + failed never exceeds total.
    """
    def __init__(self):
        self.total = 0
        self.sent = 0
        self.failed = 0
        self.last_action = "Idle"
        self._lock = asyncio.Lock()

    async def reset(self, total: int):
        async with self._lock:
            self.total = total
            self.sent = 0
            self.failed = 0
            self.last_action = "Starting Dispatch..."

    async def mark_success(self, target: str):
        async with self._lock:
            if self.sent + self.failed < self.total:
                self.sent += 1
            self.last_action = f"✅ Success: {target}"

    async def mark_failure(self, target: str, error: str):
        async with self._lock:
            if self.sent + self.failed < self.total:
                self.failed += 1
            self.last_action = f"❌ Failed {target}: {error}"

    async def set_action(self, action: str):
        async with self._lock:
            self.last_action = action

    def get_stats(self):
        return {
            "total": self.total,
            "sent": self.sent,
            "failed": self.failed,
            "last_action": self.last_action,
            "progress": int((self.sent + self.failed) / self.total * 100) if self.total > 0 else 0
        }
