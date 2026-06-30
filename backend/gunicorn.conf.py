# Gunicorn configuration for the availability-calendar backend.
# Picked up automatically when gunicorn is invoked from this directory.
#
# NOTE: workers are separate OS processes with NO shared memory, so every data
# integrity rule lives in a SQLite transaction (see app.py's writing() helper
# and the conditional UPDATE in approve), never in Python-side bookkeeping.

import multiprocessing

bind = "0.0.0.0:8000"
workers = max(2, multiprocessing.cpu_count())
worker_class = "sync"
timeout = 30
keepalive = 5
accesslog = "-"
errorlog = "-"
loglevel = "info"
