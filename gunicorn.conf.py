"""How gunicorn runs the app. Picked up automatically from the working directory."""

import os

bind = f"0.0.0.0:{os.environ.get('PORT', '8080')}"
# Threads, not many workers: requests are short and the data is files on one
# volume. The job and merge locks hold across workers either way.
workers = int(os.environ.get("WEB_CONCURRENCY", "2"))
threads = int(os.environ.get("WEB_THREADS", "8"))
accesslog = "-"
# Nothing here may assume a disk it can write to.
control_socket_disable = True
worker_tmp_dir = "/dev/shm"
forwarded_allow_ips = "*"      # wsgi.py decides how many proxy hops to trust

# Load the app once, in the master. Without this a refusal to start happens in
# a worker, and gunicorn respawns that worker forever instead of exiting - the
# container looks up while serving nothing.
preload_app = True


def post_fork(server, worker):
    """Start the scheduler in the workers, never in the master.

    A thread that exists when the master forks leaves its locks held forever
    in every child: the first request to need one hangs. Each worker starts
    its own instead, and the job's file lock lets exactly one of them run.
    """
    import config
    import jobs
    if config.flag("ELEC_SCHEDULER"):
        jobs.start_scheduler()
