import sys
import os

# Project root যোগ করো যাতে app.py, protobuf ফাইল পাওয়া যায়
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import app

# Vercel Python handler
handler = app

# Render / local server
if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)