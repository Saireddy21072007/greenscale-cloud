#!/usr/bin/env python
"""Development entry point.  python run.py  -> http://localhost:5000"""

from greenscale import config
from greenscale.app import create_app

app = create_app()

if __name__ == "__main__":
    print(f"GreenScale Cloud 2.0 on http://localhost:{config.PORT}")
    # threaded=True because mining a block briefly blocks the request thread and
    # we do not want the dashboard's own fetch() calls queueing behind it.
    app.run(host=config.HOST, port=config.PORT, debug=config.DEBUG, threaded=True)
