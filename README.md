# ARMEDIAS AI — Production Messaging Automation Hub

A professional, scalable, and session-based Telegram automation platform.

## 🏗 Architecture
- **Entry**: `app.py` (Flask + SocketIO)
- **Engine**: `core/bot_manager.py` (Orchestrator) & `core/bot_worker.py` (Independent Account Workers)
- **Routing**: `api/routes.py` (Modular Controller)
- **Utilities**: `utils/logger.py` & `utils/config_loader.py`
- **Frontend**: Vanilla JS with Optimized DOM Diffing

## 🚀 Deployment
1. Install dependencies: `pip install flask flask-socketio flask-cors pyrogram tgcrypto`
2. Launch: `python app.py`
3. Access: `http://localhost:5001`

## ⚙️ Features
- **Independent Sessions**: Each account runs its own process loop.
- **Dynamic Intervals**: Change loop timing in real-time without restarts.
- **Anti-Flood Protection**: Integrated FloodWait handling and exponential backoff.
- **Real-time Monitoring**: Live status updates via WebSockets.
- **Production Logging**: Rotating logs in `logs/bot.log`.

## 🔐 Security
- **Admin Authentication**: Secure login required for dashboard access.
- **Session Isolation**: Each account has its own isolated `.session` file.
- **Encrypted Communication**: Powered by Pyrogram's MTProto.
