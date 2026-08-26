import sys
import os
import asyncio

# Add project root to path
sys.path.append(os.getcwd())

async def diagnostic():
    print("🔍 ARMEDIAS System Diagnostic...")
    
    # 1. Check Directory Structure
    folders = ["core", "api", "utils", "sessions", "logs", "templates", "static"]
    for f in folders:
        if os.path.isdir(f):
            print(f"✅ Folder exists: {f}")
        else:
            print(f"❌ Missing folder: {f}")

    # 2. Check Core Files
    files = [
        "app.py", 
        "core/bot_manager.py", 
        "core/bot_worker.py", 
        "api/routes.py", 
        "utils/logger.py", 
        "utils/config_loader.py"
    ]
    for f in files:
        if os.path.isfile(f):
            print(f"✅ File exists: {f}")
        else:
            print(f"❌ Missing file: {f}")

    # 3. Test Imports
    try:
        from core.bot_manager import BotManager
        from utils.config_loader import load_config
        print("✅ Core imports successful")
    except Exception as e:
        print(f"❌ Import failure: {e}")

    # 4. Test Config Loader
    try:
        cfg = load_config()
        print(f"✅ Config loaded. Sessions detected: {len(cfg.get('account_settings', {}))}")
    except Exception as e:
        print(f"❌ Config failure: {e}")

    print("\n🚀 System seems ready for launch.")

if __name__ == "__main__":
    asyncio.run(diagnostic())
