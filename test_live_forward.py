import asyncio
import json
from pyrogram import Client

async def test_forward():
    print("Loading config...")
    with open("config.json", "r") as f:
        config = json.load(f)

    api_id = config["api_id"]
    api_hash = config["api_hash"]
    source_channel = config["source_channel"]
    
    # FIX: Added country code to match your session file exactly
    # Your session is saved as 'session_919310362649.session'
    phone = "919310362649"
    target = "@Armedia_007"
    
    print(f"Connecting to session for {phone}...")
    app = Client(
        name=f"session_{phone}",
        api_id=api_id,
        api_hash=api_hash,
        workdir="sessions"
    )

    await app.start()
    
    print(f"✅ Successfully logged in as: {(await app.get_me()).first_name}")
    
    try:
        # Get the latest message from the source channel
        print(f"Fetching latest message from source: {source_channel}...")
        
        try:
            source_id = int(source_channel)
        except ValueError:
            source_id = source_channel
            
        async for message in app.get_chat_history(source_id, limit=1):
            msg_id = message.id
            print(f"Found message ID {msg_id}!")
            
            print(f"Forwarding message {msg_id} to {target}...")
            await app.forward_messages(
                chat_id=target,
                from_chat_id=source_id,
                message_ids=msg_id
            )
            print("🎉 SUCCESS! Message forwarded successfully.")
            break
            
    except Exception as e:
        print(f"❌ ERROR: {e}")
        
    finally:
        await app.stop()

if __name__ == "__main__":
    asyncio.run(test_forward())
