"""Load-test harness (board c226): scripted auth'd HTTP mix + WS connect storm.

Client-side only — this package never imports `app`. It drives a running Chirp
backend over HTTP/WS exactly the way the mobile client does, so it can be pointed
at any environment. The target defaults to localhost and refuses a non-local URL
without an explicit, recorded approval (Jose's park on running the test is
enforced in code, not just in prose — see loadtest/config.py).
"""
