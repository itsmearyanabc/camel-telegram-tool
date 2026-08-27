/**
 * PM2 configuration for the Telegram Tool.
 *
 *   pm2 start /opt/telegram-tool/deploy/ecosystem.config.js
 *   pm2 save
 *
 * Use deploy/use-pm2.sh to migrate a systemd install across cleanly — it stops
 * and disables the unit first so the two supervisors cannot both run the app.
 *
 * Secrets are NOT listed here. app.py calls load_dotenv() at startup, so
 * /opt/telegram-tool/.env is read automatically because cwd is set below.
 * That keeps the token and keys out of this committed file.
 */

const APP_DIR = "/opt/telegram-tool";
const PORT = process.env.TELEGRAM_TOOL_PORT || "5001";

module.exports = {
  apps: [
    {
      name: "telegram-tool",
      cwd: APP_DIR,

      // gunicorn is a real executable, not a JS file — run it directly.
      script: `${APP_DIR}/venv/bin/gunicorn`,
      interpreter: "none",

      // --workers 1 is MANDATORY. Every Telegram session, worker queue and
      // campaign lives in process memory; a second worker would hold its own
      // copy of all of it and forward every message twice.
      args: [
        "--worker-class geventwebsocket.gunicorn.workers.GeventWebSocketWorker",
        "--workers 1",
        `--bind 127.0.0.1:${PORT}`,
        "--timeout 120",
        "--graceful-timeout 30",
        "--log-level info",
        "app:app",
      ].join(" "),

      exec_mode: "fork",
      instances: 1,

      autorestart: true,
      max_restarts: 10,
      min_uptime: "30s",
      restart_delay: 5000,

      // Telegram sessions plus roster data; well clear of normal usage, and
      // low enough to catch a genuine leak.
      max_memory_restart: "600M",

      // SIGTERM lets the atexit handler flush state to Supabase and close
      // Telegram sessions before the process goes away.
      kill_timeout: 30000,
      listen_timeout: 20000,

      merge_logs: true,
      time: true,
      out_file: `${APP_DIR}/logs/pm2-out.log`,
      error_file: `${APP_DIR}/logs/pm2-error.log`,
    },
  ],
};
