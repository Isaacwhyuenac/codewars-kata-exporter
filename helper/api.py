import json
import re
import threading
import time
from urllib.parse import unquote

import requests


class RateLimiter:
    """Thread-safe minimum spacing between outbound HTTP requests."""

    def __init__(self, min_interval=0.5):
        self.min_interval = max(0.0, float(min_interval))
        self._lock = threading.Lock()
        self._next_allowed = 0.0

    def wait(self):
        """Block until this caller may send the next request."""
        with self._lock:
            now = time.monotonic()
            delay = self._next_allowed - now
            if delay < 0:
                delay = 0.0
            # Reserve the next slot before sleeping so concurrent callers queue.
            start = max(now, self._next_allowed)
            self._next_allowed = start + self.min_interval
        if delay > 0:
            time.sleep(delay)

    def backoff(self, seconds):
        """Push the next allowed request out (e.g. after a 429)."""
        seconds = max(0.0, float(seconds))
        with self._lock:
            self._next_allowed = max(
                self._next_allowed,
                time.monotonic() + seconds,
            )


class CodeWarsApi:
    """Client for Codewars public API and authenticated train-session endpoints."""

    BASE_URL = 'https://www.codewars.com'

    # One login for the process; workers reuse cookies instead of hammering sign_in.
    _login_lock = threading.Lock()
    _auth_cookies = None

    def __init__(
        self,
        token='',
        email='',
        password='',
        rate_limiter=None,
        rate_limit_seconds=0.5,
        max_retries=6,
    ):
        self.token = token
        self.email = email
        self.password = password
        self.max_retries = max(1, int(max_retries))
        self.rate_limiter = rate_limiter or RateLimiter(rate_limit_seconds)
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': (
                'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                'AppleWebKit/537.36 (KHTML, like Gecko) '
                'Chrome/120.0.0.0 Safari/537.36'
            ),
        })
        if email and password:
            self.login(email, password)

    def login(self, email, password):
        """Sign in once per process and reuse cookies across worker clients."""
        with self._login_lock:
            if CodeWarsApi._auth_cookies is not None:
                self.session.cookies.update(CodeWarsApi._auth_cookies)
                return

            login_url = '{}/users/sign_in'.format(self.BASE_URL)
            res = self._request('GET', login_url)
            csrf = self._extract_csrf(res.text)
            res = self._request(
                'POST',
                login_url,
                data={
                    'authenticity_token': csrf,
                    'user[email]': email,
                    'user[password]': password,
                },
                headers={'Referer': login_url},
                allow_redirects=True,
            )
            if 'sign_in' in res.url:
                raise RuntimeError(
                    'Codewars login failed. Check email/password in setup.json.'
                )

            CodeWarsApi._auth_cookies = requests.utils.dict_from_cookiejar(
                self.session.cookies
            )

    def get_kata_description(self, kata_id):
        """Return the kata problem statement (markdown) from the public API."""
        endpoint = '{}/api/v1/code-challenges/{}'.format(self.BASE_URL, kata_id)
        res = self._request(
            'GET',
            endpoint,
            # params={'Authorization': self.token}
        )
        data = json.loads(res.text)
        return data.get('description', '') or ''

    def get_sample_tests(self, kata_id, language):
        """
        Fetch sample test cases for a kata language via the train session API.

        Returns the exampleFixture source (plaintext sample tests). The full
        submission suite remains encrypted server-side and is not available.
        """
        language = (language or '').strip().lower()
        if not language:
            return ''

        train_url = '{}/kata/{}/train/{}'.format(
            self.BASE_URL, kata_id, language
        )
        res = self._request('GET', train_url)

        csrf = self._extract_csrf(res.text)
        session_path = self._extract_session_path(res.text, language)
        if not session_path:
            return ''

        session_url = self.BASE_URL + session_path
        res = self._request(
            'POST',
            session_url,
            headers={
                'X-CSRF-Token': csrf,
                'X-Requested-With': 'XMLHttpRequest',
                'Accept': 'application/json, text/javascript, */*; q=0.01',
                'Referer': train_url,
                'Origin': self.BASE_URL,
            },
        )
        data = res.json()
        if not data.get('success', True) and 'exampleFixture' not in data:
            return ''
        return data.get('exampleFixture') or ''

    def _request(self, method, url, **kwargs):
        """Send an HTTP request with global rate limiting and 429/5xx retries."""
        last_response = None
        last_error = None

        for attempt in range(self.max_retries):
            self.rate_limiter.wait()
            try:
                res = self.session.request(method, url, **kwargs)
            except requests.RequestException as exc:
                last_error = exc
                delay = min(2 ** attempt, 30)
                self.rate_limiter.backoff(delay)
                time.sleep(delay)
                continue

            last_response = res

            if res.status_code == 429:
                delay = self._retry_after_seconds(res, attempt)
                self.rate_limiter.backoff(delay)
                time.sleep(delay)
                continue

            if res.status_code >= 500:
                delay = min(2 ** attempt, 30)
                self.rate_limiter.backoff(delay)
                time.sleep(delay)
                continue

            res.raise_for_status()
            return res

        if last_error is not None and last_response is None:
            raise last_error
        if last_response is not None:
            last_response.raise_for_status()
        raise RuntimeError(
            'Request failed after {} attempts: {} {}'.format(
                self.max_retries, method, url
            )
        )

    @staticmethod
    def _retry_after_seconds(response, attempt):
        """Parse Retry-After, falling back to exponential backoff."""
        header = response.headers.get('Retry-After')
        if header:
            try:
                return max(1.0, float(header))
            except ValueError:
                pass
        return min(float(2 ** attempt), 60.0)

    @staticmethod
    def _extract_csrf(html):
        match = re.search(r'name="csrf-token" content="([^"]+)"', html)
        if not match:
            raise RuntimeError('Could not find CSRF token on Codewars page.')
        return match.group(1)

    @staticmethod
    def _extract_session_path(html, language):
        """Parse App.setup routes.session and substitute the language."""
        match = re.search(r'routes:\s*(\{.*?\})\s*,\s*\n', html, re.S)
        if not match:
            return None
        try:
            routes = json.loads(match.group(1))
        except json.JSONDecodeError:
            return None

        path = routes.get('session')
        if not path:
            return None

        path = unquote(path)
        path = path.replace('{language}', language)
        return path
