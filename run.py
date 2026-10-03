#!/usr/bin/env python3
"""
run.py — convenience entry point. From the project's top-level folder,
just run:

    python3 run.py

This boots the Flask server and the background judge workers together,
then prints the local URL to open in your browser.
"""

import os
import sys

BACKEND_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backend")
sys.path.insert(0, BACKEND_DIR)

from app import app, create_app  # noqa: E402  (import must follow the sys.path fix above)

if __name__ == "__main__":
    create_app()
    port = 5050
    print(f"\n  Online Judge is running -> http://127.0.0.1:{port}\n"
          f"  Press Ctrl+C to stop.\n")
    app.run(host="127.0.0.1", port=port, debug=False, threaded=True)
