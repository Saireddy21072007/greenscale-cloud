# Deployment

## Local

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
source .venv/bin/activate       # Linux / macOS
pip install -r requirements.txt
python scripts/train_model.py
python run.py
```

`run.bat` does all of that in one step on Windows.

Without a trained model the app still boots — the predictor falls back to the
closed-form heuristic and the Schedule page shows a `heuristic` badge so nobody
mistakes it for the real thing.

## Docker

```bash
docker compose up --build
```

The model is trained during `docker build`, so container start is fast and the image is
self-contained. `data/` is a named volume because two things in it must outlive the
container:

- `chain.json` — the ledger
- `scheduler_key.pem` — the private signing key

Lose the key and every signature already in the chain becomes unverifiable. The chain
stores the matching public key inside itself, so an *existing* ledger stays auditable,
but the node can no longer sign new records as the same identity.

## Environment variables

| Variable | Default | Notes |
| --- | --- | --- |
| `GS_SECRET_KEY` | `dev-only-change-me` | **Change before exposing the app.** |
| `GS_POW_DIFFICULTY` | `3` | Leading zeros required. 5 makes mining visible. |
| `GS_HOST` | `0.0.0.0` | |
| `GS_PORT` | `5000` | |
| `GS_DEBUG` | `0` | Never set to `1` in production. |

## Production WSGI

```bash
gunicorn -c gunicorn.conf.py wsgi:app        # Linux / macOS
waitress-serve --port=5000 wsgi:app          # Windows
```

### The one rule: a single worker

`gunicorn.conf.py` sets `workers = 1` and scales with threads instead. This is not
laziness.

The blockchain lives in the memory of the process that created it. Two worker processes
would each hold their own copy, append different transactions, and then take turns
overwriting `data/chain.json` — one worker's blocks would silently disappear every time
the other saved. Threads share memory and the chain's `RLock` serialises them correctly.

Throughput is not the constraint here. A block seals in about 2.7 ms including signing
and mining, so one threaded worker handles far more scheduling decisions per second than
a scheduler of this kind ever needs to make.

**Scaling past one machine** means taking the ledger out of the application process:
either a small dedicated ledger service that all schedulers write to, or a real
permissioned chain such as Hyperledger Fabric. We list that as future work rather than
pretending the current design scales horizontally.

## Free-tier hosting

The app is a single stateless-ish Flask process with a small persistent volume, so it
fits any of the usual student-budget hosts. Two things to check wherever you put it:

1. **Persistent disk mounted at `/app/data`.** On a platform with ephemeral filesystems
   the ledger resets on every deploy, which rather defeats the point.
2. **Timeout above 60 s** if you raise `GS_POW_DIFFICULTY`. At difficulty 6 or more,
   mining can exceed a default 30 s request timeout.

`/api/health` returns chain height, pending transaction count and the active predictor,
so it works directly as a platform health check. The Dockerfile already wires it into
`HEALTHCHECK`.

## Backing up the ledger

```bash
docker compose cp greenscale:/app/data/chain.json ./chain-backup.json
```

A copy of `chain.json` is independently verifiable — the public keys are stored inside
the file, so an auditor needs nothing else from us:

```python
from greenscale.blockchain import Blockchain
result = Blockchain.load("chain-backup.json").validate()
print(result.valid, result.problems)
```

That is the whole point of the design: the person checking our sustainability claim does
not have to trust our server, only the file.

## Operational notes

- **Corrupt ledger.** `Blockchain.open` renames an unparseable `chain.json` to
  `chain.json.corrupt` and starts fresh rather than silently overwriting evidence.
- **Atomic writes.** Saves go to a temp file and are then renamed, so a crash mid-write
  cannot leave a half-written ledger.
- **Key permissions.** The private key is written `0600` where the OS supports it.
  Windows ignores POSIX modes; on a real deployment the key belongs in a KMS or an HSM,
  not on the application disk.
