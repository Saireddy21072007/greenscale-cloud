"""Production entry point for gunicorn / waitress.

    gunicorn -c gunicorn.conf.py wsgi:app

Read DEPLOYMENT.md before changing the worker count -- the ledger lives in one
process and multiple workers would each keep their own copy of the chain.
"""

from greenscale.app import create_app

app = create_app()
