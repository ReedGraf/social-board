"""Password / recovery-phrase hashing, session tokens, rate limiting."""
import base64
import hashlib
import hmac
import re
import secrets
import threading
import time
from collections import defaultdict, deque

from .words import WORDS

# scrypt, OWASP-recommended cost: N=2^17, r=8, p=1 (128 MiB, ~0.3s).
# Params are stored with each hash so they can be raised later.
LOGN, R, P = 17, 8, 1
# Each hash needs 128 MiB; cap concurrent hashes so a login flood can't OOM the box.
_hash_slots = threading.BoundedSemaphore(2)

PHRASE_WORDS = 12


def _scrypt(secret, salt, logn, r, p):
    with _hash_slots:
        return hashlib.scrypt(secret.encode(), salt=salt, n=1 << logn, r=r, p=p,
                              maxmem=512 * 1024 * 1024, dklen=32)


def hash_secret(secret):
    salt = secrets.token_bytes(16)
    dk = _scrypt(secret, salt, LOGN, R, P)
    return "scrypt$%d$%d$%d$%s$%s" % (LOGN, R, P, base64.b64encode(salt).decode(),
                                      base64.b64encode(dk).decode())


def verify_secret(secret, stored):
    try:
        algo, logn, r, p, salt, dk = stored.split("$")
        if algo != "scrypt":
            return False
        got = _scrypt(secret, base64.b64decode(salt), int(logn), int(r), int(p))
        return hmac.compare_digest(got, base64.b64decode(dk))
    except (ValueError, TypeError):
        return False


def needs_rehash(stored):
    return not stored.startswith("scrypt$%d$%d$%d$" % (LOGN, R, P))


_DUMMY = None


def burn_time(secret):
    """Spend the same time as a real check, so unknown usernames aren't detectable by timing."""
    global _DUMMY
    if _DUMMY is None:
        _DUMMY = hash_secret("dummy")
    verify_secret(secret, _DUMMY)


def new_phrase():
    return " ".join(secrets.choice(WORDS) for _ in range(PHRASE_WORDS))


def normalize_phrase(s):
    # Tolerate numbering, commas, newlines and case from however the user wrote it down.
    return " ".join(re.findall(r"[a-z]+", s.lower()))


def new_token():
    return secrets.token_urlsafe(32)


def token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


class RateLimiter:
    def __init__(self, limit, window):
        self.limit, self.window = limit, window
        self.hits = defaultdict(deque)
        self.lock = threading.Lock()

    def allow(self, key):
        now = time.monotonic()
        with self.lock:
            q = self.hits[key]
            while q and q[0] < now - self.window:
                q.popleft()
            if len(q) >= self.limit:
                return False
            q.append(now)
            if len(self.hits) > 10000:  # drop idle keys
                for k in [k for k, v in self.hits.items() if v[-1] < now - self.window]:
                    del self.hits[k]
            return True
