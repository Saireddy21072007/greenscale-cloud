"""Gunicorn configuration.

The one thing here that is not boilerplate is `workers = 1`.

The blockchain lives in the process that created it. Two worker processes would
each hold their own copy of the chain in memory, append different transactions to
it, and then take turns overwriting data/chain.json -- one worker's blocks would
silently vanish every time the other saved. Threads share memory and the chain's
RLock serialises them correctly, so we scale with threads instead.

Moving past one machine means moving the ledger out of process (a small
consensus service, or a real chain like Hyperledger Fabric). That is written up
as future work rather than pretended away.
"""

import multiprocessing

bind = "0.0.0.0:5000"
workers = 1
threads = min(8, multiprocessing.cpu_count() * 2)
worker_class = "gthread"

# Proof-of-work mining can occupy a request for a moment; the default 30 s
# timeout is plenty but we raise it a little for slow free-tier hosts.
timeout = 60
graceful_timeout = 30
keepalive = 5

accesslog = "-"
errorlog = "-"
loglevel = "info"
