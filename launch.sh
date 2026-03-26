#!/bin/bash
# Kill any existing instance on port 5050
lsof -ti:5050 | xargs kill -9 2>/dev/null

# Start the Flask server in background
cd /Users/mervyn/ClaudeCode
python3 app.py &

# Wait for server to start
sleep 2

# Open browser
open http://127.0.0.1:5050
/Users/mervyn/Desktop/launch.sh alias