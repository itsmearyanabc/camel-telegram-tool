import asyncio
import os
from pyrogram import Client
from pyrogram.errors import FloodWait, RPCError

async def brutal_diagnostic():
    from config import ACCOUNTS
    print("\n" + "!"*60)
    print("      BRUTAL TELEGRAM DIAGNOSTIC - ELYNDOR INTERACTIVE")
    print("!"*60 + "\n")

    for acc in ACCOUNTS:
        phone = acc['phone'].replace(" ", "").replace("-", "")
        print(f"DEBUG: Testing Account: {acc['name']} ({phone})...")
        
        client = Client(
            name=f"diag_{acc['name']}",
            api_id=acc['api_id'],
            api_hash=acc['api_hash'],
            phone_number=phone,
            workdir="sessions"
        )
        
        try:
            print(f"  [1] Connecting to Telegram...")
            await client.connect()
            
            print(f"  [2] Requesting login code for {phone}...")
            # We must pass the phone_number to send_code
            sent_code = await client.send_code(phone)
            
            print(f"  SUCCESS: Code sent to {phone}!")
            await client.disconnect()
            
        except FloodWait as e:
            print(f"  ERROR: RATE LIMITED: Telegram says wait {e.value} seconds.")
        except RPCError as e:
            print(f"  ERROR: TELEGRAM REJECTED: {e.MESSAGE}")
        except Exception as e:
            print(f"  ERROR: SYSTEM ERROR: {str(e)}")
        finally:
            try: await client.disconnect()
            except: pass
        print("-" * 30)

    print("\n" + "!"*60)
    print("            DIAGNOSTIC COMPLETED")
    print("!"*60 + "\n")

if __name__ == "__main__":
    asyncio.run(brutal_diagnostic())
