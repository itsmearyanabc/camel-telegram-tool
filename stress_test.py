import asyncio
import os
import shutil
from account_manager import AccountManager
from dispatcher import Dispatcher
from logger import logger

async def run_deep_test():
    print("\n" + "="*50)
    print("      ELYNDOR INTERACTIVE - DEEP STRESS TEST")
    print("="*50 + "\n")

    # 1. Setup mock accounts
    am = AccountManager()
    
    # 2. Initialize
    print("[1/3] Initializing account pool...")
    await am.initialize()
    
    # 3. Create Dispatcher with 5 targets
    targets = ["@user1", "@user2", "@user3", "@user4", "@user5"]
    dispatcher = Dispatcher(am, targets)
    
    # 4. Simulate a message
    print("[2/3] Simulating new message arrival...")
    message_data = {
        "from_chat_id": -100123,
        "message_id": 999
    }
    
    # 5. Run dispatcher
    print("[3/3] Starting delivery with Chaos Mode enabled...")
    print("      (Bot will randomly simulate failures and attempt to skip them)\n")
    
    # We run the dispatcher in the background
    asyncio.create_task(dispatcher.run())
    
    # Enqueue the message
    await dispatcher.enqueue(message_data)
    
    # Wait for delivery to finish (or fail)
    # We expect 5 deliveries, each taking 2-5 seconds
    await asyncio.sleep(25)
    
    print("\n" + "="*50)
    print("           STRESS TEST COMPLETED")
    print("="*50 + "\n")
    
    # Cleanup metadata file created by trial lock
    if os.path.exists(".session_metadata.dat"):
        os.remove(".session_metadata.dat")

if __name__ == "__main__":
    asyncio.run(run_deep_test())
