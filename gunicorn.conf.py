"""
Gunicorn configuration for Medicine Reminder AI.

The background scheduler (medicine reminders + missed-dose checks) is started
in each worker process via the post_fork hook so that:
  * It runs under gunicorn (Render) as well as `python app.py`.
  * Each worker owns an independent APScheduler instance.
  * Duplicate/overlapping jobs are avoided with max_instances=1 + coalesce=True.
"""

import os


def post_fork(server, worker):
    """Start the APScheduler inside every gunicorn worker."""
    try:
        from app import _start_scheduler
        _start_scheduler()
    except Exception:
        # Don't crash the worker if scheduler startup fails; logs will show why.
        import traceback
        traceback.print_exc()


# Worker timeout - generous for scheduled tasks that may touch the DB.
timeout = int(os.environ.get("GUNICORN_TIMEOUT", 120))

# Use sync workers (simplest; fine for moderate traffic).
worker_class = "sync"

# Bind to the port Render assigns (defaults to 8000).
bind = f"0.0.0.0:{os.environ.get('PORT', 8000)}"

# Log levels.
loglevel = os.environ.get("GUNICORN_LOG_LEVEL", "info")