"""
OST2 rate-limited email backend.

Background
----------
Open edX sends *every* outgoing message -- instructor "Email all" bulk mail (``lms.djangoapps.
bulk_email``) and transactional mail (``edx_ace``: activation, password reset, enrollment, ...) --
through the single configured Django ``EMAIL_BACKEND``.  OST2 relays all of it through one Google
Workspace user over ``smtp.gmail.com``, which enforces strict *per-user* limits:

  * ~2,000 messages / 24h for a Gmail user (10,000 / 24h via the ``smtp-relay.gmail.com`` service);
  * short-term burst throttling -- send too fast and Gmail returns ``421 4.7.0 Try again later,
    closing connection`` (or ``421 4.7.28``) and drops the socket.

The stock ``bulk_email`` engine was tuned for AWS SES: it only paces *after* it has already been
throttled, has no global (cross-worker) ceiling, and on a 5xx daily-quota reply it counts the
remaining recipients as failures and drops them.  Against Gmail that overruns the limits and loses
mail.

What this backend does
----------------------
``RateLimitedEmailBackend`` is a drop-in replacement for Django's SMTP ``EmailBackend`` that paces
*all* outgoing mail against a single global budget shared by every LMS/CMS web and worker process,
coordinated through Redis:

  * a smooth, evenly-spaced per-minute send rate (so we never trip Gmail's burst throttle), and
  * a hard rolling 24-hour cap (so a large course-wide mailing cannot exhaust the day's budget and
    take down transactional mail such as activation / password-reset emails).

When the per-minute budget cannot be met within ``OST2_EMAIL_MAX_BLOCK_SECONDS``, or the daily cap
is reached, the backend raises ``smtplib.SMTPSenderRefused`` with a transient ``451`` code.  The
``bulk_email`` task treats a 4xx as an "infinite retry" condition, so the affected recipients are
*deferred and retried*, never dropped -- the mailing simply slows down and rides out the limit.

Configuration (all optional; safe defaults shown)
-------------------------------------------------
  OST2_EMAIL_RATE_PER_MIN       target sustained send rate, messages/minute     (default 30)
  OST2_EMAIL_DAILY_CAP          hard rolling-24h message cap, 0 disables         (default 1800)
  OST2_EMAIL_MAX_BLOCK_SECONDS  max seconds a single send will wait for a slot   (default 15)
  OST2_EMAIL_REDIS_URL          redis url for coordination     (default: settings.BROKER_URL)
  OST2_EMAIL_KEY_PREFIX         redis key prefix                       (default "ost2:email")
  OST2_EMAIL_FAIL_OPEN          send anyway if Redis is unreachable            (default True)

This backend subclasses the SMTP backend, so it inherits ``EMAIL_HOST`` / ``EMAIL_PORT`` /
``EMAIL_HOST_USER`` / ``EMAIL_USE_TLS`` / ... unchanged -- only pacing is added.
"""
import logging
import time
from smtplib import SMTPSenderRefused

from django.conf import settings
from django.core.mail.backends.smtp import EmailBackend

log = logging.getLogger('edx.celery.task')

# Atomically reserve the next send "slot" on a timeline shared by every sender process across the
# whole cluster, so sends are spaced by exactly ``interval`` seconds globally.  Returns the epoch
# time at which THIS send is permitted (== now if we are caught up).
#   KEYS[1] = slot key
#   ARGV[1] = now (epoch seconds, float)   ARGV[2] = interval (seconds)   ARGV[3] = key ttl (ms)
_NEXT_SLOT_LUA = """
local slot = redis.call('GET', KEYS[1])
local now = tonumber(ARGV[1])
local interval = tonumber(ARGV[2])
if (not slot) or (tonumber(slot) < now) then
    slot = now
else
    slot = tonumber(slot)
end
redis.call('SET', KEYS[1], slot + interval, 'PX', tonumber(ARGV[3]))
return tostring(slot)
"""


class RateLimitedEmailBackend(EmailBackend):
    """SMTP email backend that paces all outgoing mail against a shared Redis budget."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.rate_per_min = int(getattr(settings, 'OST2_EMAIL_RATE_PER_MIN', 30) or 0)
        self.daily_cap = int(getattr(settings, 'OST2_EMAIL_DAILY_CAP', 1800) or 0)
        self.max_block = float(getattr(settings, 'OST2_EMAIL_MAX_BLOCK_SECONDS', 15))
        self.key_prefix = getattr(settings, 'OST2_EMAIL_KEY_PREFIX', 'ost2:email')
        self.fail_open = bool(getattr(settings, 'OST2_EMAIL_FAIL_OPEN', True))
        self.redis_url = (
            getattr(settings, 'OST2_EMAIL_REDIS_URL', None)
            or getattr(settings, 'BROKER_URL', None)
            or getattr(settings, 'CELERY_BROKER_URL', None)
        )
        self._redis = None

    # -- Redis ---------------------------------------------------------------
    @property
    def redis(self):
        if self._redis is None:
            import redis  # lazy: redis-py ships with edx-platform (it is the Celery/cache client)
            self._redis = redis.Redis.from_url(
                self.redis_url, socket_timeout=2, socket_connect_timeout=2,
            )
        return self._redis

    # -- limiter primitives --------------------------------------------------
    def _defer(self, reason):
        """Raise a transient SMTP error so the caller (bulk_email) defers and retries this message."""
        raise SMTPSenderRefused(
            451, ('4.7.0 OST2 sending budget: ' + reason).encode('utf-8'),
            getattr(self, 'username', '') or '',
        )

    def _reserve_daily(self):
        """Reserve one unit of the rolling-24h budget.  Returns True if reserved, False if disabled."""
        if self.daily_cap <= 0:
            return False
        day = int(time.time() // 86400)
        key = f'{self.key_prefix}:daily:{day}'
        count = self.redis.incr(key)
        if count == 1:
            self.redis.expire(key, 90000)  # a little over 24h, so the window rolls cleanly
        if count > self.daily_cap:
            self.redis.decr(key)  # we did not actually send; keep the counter to accepted sends only
            self._defer(f'daily cap of {self.daily_cap} reached')
        return True

    def _release_daily(self):
        if self.daily_cap <= 0:
            return
        day = int(time.time() // 86400)
        try:
            self.redis.decr(f'{self.key_prefix}:daily:{day}')
        except Exception:  # pylint: disable=broad-except
            pass

    def _await_slot(self):
        """Sleep until this send's evenly-spaced slot, or raise to defer if that is too far off."""
        if self.rate_per_min <= 0:
            return
        interval = 60.0 / self.rate_per_min
        now = time.time()
        raw = self.redis.eval(
            _NEXT_SLOT_LUA, 1, f'{self.key_prefix}:slot',
            repr(now), repr(interval), int((interval + self.max_block + 5) * 1000),
        )
        slot = float(raw.decode() if isinstance(raw, (bytes, bytearray)) else raw)
        wait = slot - now
        if wait > self.max_block:
            self._defer(f'rate limit {self.rate_per_min}/min, next slot in {wait:.1f}s')
        if wait > 0:
            time.sleep(wait)

    def _gate(self, message):  # pylint: disable=unused-argument
        """Block until this message may send, or raise SMTPSenderRefused(451) to defer it."""
        reserved_daily = False
        try:
            reserved_daily = self._reserve_daily()
            self._await_slot()
        except SMTPSenderRefused:
            if reserved_daily:
                self._release_daily()
            raise
        except Exception as exc:  # pylint: disable=broad-except
            # Redis hiccup etc.  Don't let the limiter itself break mail delivery.
            if reserved_daily:
                self._release_daily()
            log.warning(
                "RateLimitedEmailBackend: limiter unavailable (%s); %s",
                exc, "sending anyway (fail-open)" if self.fail_open else "deferring",
            )
            if not self.fail_open:
                self._defer('rate limiter unavailable')

    # -- Django EmailBackend API (mirrors smtp.EmailBackend.send_messages) ----
    def send_messages(self, email_messages):
        if not email_messages:
            return 0
        with self._lock:
            new_conn_created = self.open()
            if not self.connection or new_conn_created is None:
                # open() failed silently; nothing to do.
                return 0
            num_sent = 0
            try:
                for message in email_messages:
                    self._gate(message)            # pace / defer BEFORE touching the wire
                    sent = self._send(message)     # reuses the one open SMTP connection
                    if sent:
                        num_sent += 1
            finally:
                if new_conn_created:
                    self.close()
        return num_sent
