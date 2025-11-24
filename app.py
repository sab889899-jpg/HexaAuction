from flask import Flask
import threading
import os
import time
import subprocess
import sys

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
    """Run the bot in a separate process"""
    while True:
        try:
            print("🚀 Starting Telegram Bot...")
            # Run the bot as a subprocess
            process = subprocess.Popen([sys.executable, "bot.py"], 
                                     stdout=subprocess.PIPE, 
                                     stderr=subprocess.PIPE)
            
            # Wait for the process to finish
            stdout, stderr = process.communicate()
            
            if stdout:
                print(f"Bot stdout: {stdout.decode()}")
            if stderr:
                print(f"Bot stderr: {stderr.decode()}")
                
            print(f"❌ Bot stopped with return code: {process.returncode}")
            print("🔄 Restarting bot in 10 seconds...")
            time.sleep(10)
            
        except Exception as e:
            print(f"❌ Error running bot: {e}")
            time.sleep(10)

if __name__ == '__main__':
    # Start bot in background thread
    bot_thread = threading.Thread(target=run_bot)
    bot_thread.daemon = True
    bot_thread.start()
    print("✅ Bot thread started successfully!")
    
    # Start Flask web server
    port = int(os.environ.get('PORT', 5000))
    print(f"🌐 Starting web server on port {port}")
    app.run(host='0.0.0.0', port=port, debug=False)
