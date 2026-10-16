"""
Management command to send a site-wide email: one message to every user who wants site-wide email.
"""

import os
import smtplib
import sys
import time
from datetime import datetime, timezone

from django.contrib.auth import get_user_model
from django.core.mail import get_connection
from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q

from lms.djangoapps.bulk_email.site_email import build_site_email, get_site_email_recipients
from openedx.core.djangoapps.user_api.preferences.email_preference import EMAIL_PREFERENCE_ALL, get_email_preference

User = get_user_model()

# SMTP errors worth one retry: Gmail's 421 throttling, the rate limiter's 451, and dropped connections.
TRANSIENT_ERRORS = (
    smtplib.SMTPSenderRefused,
    smtplib.SMTPDataError,
    smtplib.SMTPRecipientsRefused,
    smtplib.SMTPServerDisconnected,
    smtplib.SMTPConnectError,
)


class Command(BaseCommand):
    """
    Sends a site-wide email to the users whose email preference is "Receive all emails".

    Each message gets a footer with a link that switches the recipient to "Receive only emails sent to students
    enrolled in a course" and a link to their account preferences (see lms.djangoapps.bulk_email.site_email).

    A send is named by --campaign, and every message sent is recorded in <log-dir>/<campaign>.log, so running the
    same command again sends only to the users who have not had it yet. That is how to send to more users than
    the daily email budget allows: run the command once a day until it reports that nobody is left.

    Example usage, with the body in body.txt:
    ./manage.py lms send_site_email --campaign 2026-10-news --subject "News" --text-file body.txt --dry-run
    ./manage.py lms send_site_email --campaign 2026-10-news --subject "News" --text-file body.txt --to xeno
    ./manage.py lms send_site_email --campaign 2026-10-news --subject "News" --text-file body.txt --limit 500
    """

    help = __doc__

    def add_arguments(self, parser):
        parser.add_argument('--campaign', required=True, help='Name of this send; names its log file.')
        parser.add_argument('--subject', required=True, help='Subject of the email.')
        parser.add_argument(
            '--text-file', required=True,
            help='Plain text body ("-" reads stdin). {name}, {username} and {email} are filled in per recipient.',
        )
        parser.add_argument('--html-file', help='Optional HTML body, with the same placeholders.')
        parser.add_argument('--from-email', help='From address. Default: BULK_EMAIL_DEFAULT_FROM_EMAIL.')
        parser.add_argument(
            '--to', nargs='+', metavar='USER',
            help='Test send: only these usernames or emails, whatever their preference. Not recorded as sent.',
        )
        parser.add_argument(
            '--last-login-after', metavar='YYYY-MM-DD',
            help='Only users who logged in on or after this date.',
        )
        parser.add_argument('--limit', type=int, help='Send at most this many messages in this run.')
        parser.add_argument(
            '--reserve', type=int, default=300,
            help='Leave this much of the daily email budget for other mail (default 300).',
        )
        parser.add_argument('--pause', type=float, default=2.0, help='Seconds to wait between messages (default 2).')
        parser.add_argument(
            '--log-dir', default='/openedx/data/site_email', help='Where campaign logs go (default %(default)s).',
        )
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Send nothing: print the recipient count and the message the first recipient would get.',
        )

    def handle(self, *args, **options):
        text_body = self._read(options['text_file'])
        html_body = self._read(options['html_file']) if options['html_file'] else None
        log_path = os.path.join(options['log_dir'], f"{self._campaign_name(options['campaign'])}.log")
        recipients = self._recipients(options, log_path)

        budget = self._budget(options['reserve'])
        planned = len(recipients)
        if options['limit'] is not None:
            planned = min(planned, options['limit'])
        if budget is not None:
            self.stdout.write(f"daily email budget left after the reserve: {budget}")
            planned = min(planned, budget)

        def build(user):
            return build_site_email(user, options['subject'], text_body, html_body, options['from_email'])

        if options['dry_run']:
            self.stdout.write(f"DRY RUN: would send to {planned} recipient(s) now")
            if recipients:
                self._print_message(recipients[0], build(recipients[0]))
            return

        if planned < len(recipients):
            self.stdout.write(f"sending to {planned} of them in this run")
        if planned:
            os.makedirs(options['log_dir'], exist_ok=True)
            self._send_all(recipients, planned, build, log_path, test_send=bool(options['to']), pause=options['pause'])

    def _recipients(self, options, log_path):
        """
        Returns the users to send to: the --to users, or the site-wide email recipients not sent to yet.
        """
        if options['to']:
            return list(self._named_users(options['to']))
        already_sent = self._already_sent(log_path)
        queryset = get_site_email_recipients().select_related('profile')
        if options['last_login_after']:
            queryset = queryset.filter(last_login__gte=self._date(options['last_login_after']))
        recipients = [user for user in queryset.iterator() if user.id not in already_sent]
        self.stdout.write(
            f"{len(recipients)} recipient(s) left in campaign {options['campaign']!r} "
            f"({len(already_sent)} already sent, log {log_path})"
        )
        return recipients

    def _print_message(self, user, message):
        """
        Prints the message that `user` would get.
        """
        self.stdout.write(f"--- message for user {user.id} ---")
        self.stdout.write(f"From: {message.from_email}\nTo: {', '.join(message.to)}\nSubject: {message.subject}")
        for header, value in message.extra_headers.items():
            self.stdout.write(f"{header}: {value}")
        self.stdout.write(f"\n{message.body}")
        for content, mimetype in message.alternatives:
            self.stdout.write(f"--- {mimetype} ---\n{content}")

    def _send_all(self, recipients, planned, build, log_path, test_send, pause):
        """
        Sends to `recipients` until `planned` messages went out, recording each one in the campaign log.

        Users who opted out of site-wide email since the run started are skipped (except in a test send).
        """
        sent = skipped = failed = consecutive_failures = 0
        connection = get_connection()
        with open(log_path, 'a', encoding='utf-8') as log_file:
            for user in recipients:
                if sent >= planned:
                    break
                if not test_send and get_email_preference(user) != EMAIL_PREFERENCE_ALL:
                    skipped += 1
                    continue
                message = build(user)
                message.connection = connection
                error = self._send(message)
                if error:
                    status = 'FAIL'
                    failed += 1
                    consecutive_failures += 1
                else:
                    status = 'TEST' if test_send else 'SENT'
                    sent += 1
                    consecutive_failures = 0
                stamp = datetime.now(timezone.utc).isoformat(timespec='seconds')
                log_file.write(f"{status} {user.id} {stamp} {error or ''}".rstrip() + '\n')
                log_file.flush()
                self.stdout.write(f"{status} {user.id} {user.username} {error or ''}".rstrip())
                if consecutive_failures >= 3:
                    raise CommandError(f"stopping after 3 failures in a row; sent={sent} failed={failed}")
                time.sleep(pause)
        self.stdout.write(f"DONE sent={sent} skipped={skipped} failed={failed}")

    def _send(self, message):
        """
        Sends the message, retrying once after a transient SMTP error. Returns None, or the error as a string.
        """
        for attempt in range(2):
            try:
                message.send(fail_silently=False)
                return None
            except TRANSIENT_ERRORS as exc:
                if attempt == 0:
                    time.sleep(30)
                    message.connection.close()
                    continue
                return f"{type(exc).__name__}: {str(exc)[:200]}"
            except Exception as exc:  # pylint: disable=broad-except
                return f"{type(exc).__name__}: {str(exc)[:200]}"
        return None

    def _budget(self, reserve):
        """
        Returns how many more messages may go out today, if the email backend keeps a daily budget.

        That's OST2's RateLimitedEmailBackend, whose daily counter lives in Redis. Returns None otherwise.
        """
        connection = get_connection()
        daily_cap = getattr(connection, 'daily_cap', 0)
        if not daily_cap or not hasattr(connection, 'redis'):
            return None
        try:
            day = int(time.time() // 86400)
            used = int(connection.redis.get(f'{connection.key_prefix}:daily:{day}') or 0)
        except Exception as exc:  # pylint: disable=broad-except
            raise CommandError(f"cannot read the daily email budget: {exc}") from exc
        return max(daily_cap - used - reserve, 0)

    def _named_users(self, names):
        """
        Yields the users with the given usernames or emails.
        """
        for name in names:
            user = User.objects.filter(Q(username=name) | Q(email__iexact=name)).first()
            if user is None:
                raise CommandError(f"no user {name!r}")
            self.stdout.write(f"test recipient {user.id} {user.username}: preference {get_email_preference(user)}")
            yield user

    @staticmethod
    def _already_sent(log_path):
        """
        Returns the ids of the users that the campaign log says were sent the email.
        """
        sent = set()
        if os.path.exists(log_path):
            with open(log_path, encoding='utf-8') as log_file:
                for line in log_file:
                    fields = line.split()
                    if len(fields) >= 2 and fields[0] == 'SENT':
                        sent.add(int(fields[1]))
        return sent

    @staticmethod
    def _read(path):
        """
        Returns the contents of a body file, or of stdin for "-".
        """
        if path == '-':
            return sys.stdin.read()
        try:
            with open(path, encoding='utf-8') as body_file:
                return body_file.read()
        except OSError as exc:
            raise CommandError(f"cannot read {path}: {exc}") from exc

    @staticmethod
    def _date(value):
        """
        Parses a YYYY-MM-DD date, as midnight UTC.
        """
        try:
            return datetime.strptime(value, '%Y-%m-%d').replace(tzinfo=timezone.utc)
        except ValueError as exc:
            raise CommandError(f"--last-login-after must be YYYY-MM-DD, not {value!r}") from exc

    @staticmethod
    def _campaign_name(campaign):
        """
        Returns the campaign name, if it is safe to use as a file name.
        """
        if not campaign or not all(char.isalnum() or char in '-_.' for char in campaign) or campaign[0] == '.':
            raise CommandError("--campaign may only contain letters, digits, '-', '_' and '.'")
        return campaign
