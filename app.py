from flask import Flask
import threading
import os
import time
import logging

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

@app.route('/')
def home():
    return "🤖 Telegram Bot is Running!"

@app.route('/health')
def health():
    return "✅ Bot Health: OK"

@app.route('/ping')
def ping():
    return "pong"

def run_bot():
    try:
        logger.info("🤖 Starting Telegram Bot...")
        from bot import main
        main()
    except Exception as e:
        logger.error(f"❌ Bot crashed: {e}")
        # Restart after 30 seconds if it crashes
        time.sleep(30)
        run_bot()

if __name__ == '__main__':
    # Start bot in background thread
    bot_thread = threading.Thread(target=run_bot)
    bot_thread.daemon = True
    bot_thread.start()
    logger.info("✅ Bot thread started")
    
    # Start Flask web server
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
