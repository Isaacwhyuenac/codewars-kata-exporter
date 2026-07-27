import json
import re
import time

from selenium import webdriver
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.wait import WebDriverWait
from webdriver_manager.chrome import ChromeDriverManager

BASE_URL = 'https://www.codewars.com'


def load_setup(path='./setup.json'):
    with open(path) as fin:
        return json.load(fin)


def create_driver(headless=True):
    chrome_options = Options()
    if headless:
        # Selenium 4 / modern Chrome headless mode
        chrome_options.add_argument('--headless=new')
    chrome_options.add_argument('--window-size=1920,1080')
    chrome_options.add_argument('--disable-gpu')
    chrome_options.add_argument('--no-sandbox')
    chrome_options.add_argument('--disable-dev-shm-usage')
    # Reduce automation banners that sometimes break form UX
    chrome_options.add_experimental_option('excludeSwitches', ['enable-automation'])
    chrome_options.add_experimental_option('useAutomationExtension', False)

    service = Service(ChromeDriverManager().install())
    return webdriver.Chrome(service=service, options=chrome_options)


def login(driver, email, password, timeout=20):
    """Sign in on the current Codewars auth form."""
    driver.get('{}/users/sign_in'.format(BASE_URL))
    wait = WebDriverWait(driver, timeout)

    email_elem = wait.until(EC.presence_of_element_located((By.ID, 'user_email')))
    password_elem = wait.until(EC.presence_of_element_located((By.ID, 'user_password')))

    email_elem.clear()
    email_elem.send_keys(email)
    password_elem.clear()
    password_elem.send_keys(password)

    # Prefer the form submit button over fragile nth-button XPath.
    submit = wait.until(
        EC.element_to_be_clickable((By.CSS_SELECTOR, 'form#new_user button[type="submit"]'))
    )
    submit.click()

    # Login should leave the sign-in page.
    wait.until(lambda d: 'sign_in' not in d.current_url)
    if 'sign_in' in driver.current_url:
        raise RuntimeError(
            'Codewars login failed. Check email/password in setup.json.'
        )


_RESERVED_USER_PATHS = frozenset({
    'sign_in', 'sign_out', 'password', 'edit', 'settings',
    'notifications', 'following', 'leaderboard', 'search',
    'new', 'join',
})


def _looks_like_username(value):
    if not value:
        return False
    value = value.strip()
    if value in _RESERVED_USER_PATHS:
        return False
    # Codewars user ids are 24-char hex; usernames are handle-like.
    if re.fullmatch(r'[0-9a-f]{24}', value):
        return False
    if not re.fullmatch(r'[A-Za-z0-9_-]{2,50}', value):
        return False
    return True


def detect_username(driver, configured_username='', timeout=15):
    """Resolve the Codewars username after login."""
    if configured_username and _looks_like_username(configured_username):
        return configured_username.strip()

    # Current site stamps the signed-in user on profile/avatar nodes.
    for elem in driver.find_elements(By.CSS_SELECTOR, '[data-username]'):
        username = (elem.get_attribute('data-username') or '').strip()
        if _looks_like_username(username):
            return username

    # Direct profile URL after some redirects.
    match = re.search(r'/users/([^/?#]+)', driver.current_url)
    if match and _looks_like_username(match.group(1)):
        return match.group(1)

    # Profile-pic / shell anchors that point at the signed-in user.
    for elem in driver.find_elements(By.CSS_SELECTOR, 'a[href*="/users/"]'):
        href = elem.get_attribute('href') or ''
        match = re.search(r'/users/([^/?#]+)', href)
        if not match:
            continue
        username = match.group(1)
        if not _looks_like_username(username):
            continue
        # Prefer the avatar menu item whose visible text is the username.
        text = (elem.text or '').strip()
        if text == username or 'profile-pic' in (elem.get_attribute('class') or ''):
            return username

    # Fallback: first plausible /users/<name> link that isn't a reserved path.
    for elem in driver.find_elements(By.CSS_SELECTOR, 'a[href*="/users/"]'):
        href = elem.get_attribute('href') or ''
        match = re.search(r'/users/([^/?#]+)', href)
        if match and _looks_like_username(match.group(1)):
            return match.group(1)

    # Last resort: account settings / page JSON.
    driver.get('{}/users/edit'.format(BASE_URL))
    WebDriverWait(driver, timeout).until(
        lambda d: 'sign_in' not in d.current_url
    )
    html = driver.page_source
    for pattern in (
        r'data-username="([^"]+)"',
        r'"username"\s*:\s*"([^"]+)"',
        r'/users/([A-Za-z0-9_-]{2,50})',
    ):
        for username in re.findall(pattern, html):
            if _looks_like_username(username):
                return username

    raise RuntimeError(
        'Could not detect Codewars username. Set codewars.username in setup.json.'
    )


def open_solutions_page(driver, username, timeout=20):
    """Navigate to the private completed-solutions page and wait for items."""
    solutions_url = '{}/users/{}/completed_solutions'.format(BASE_URL, username)
    driver.get(solutions_url)

    wait = WebDriverWait(driver, timeout)
    wait.until(EC.presence_of_element_located((By.TAG_NAME, 'body')))

    if 'sign_in' in driver.current_url:
        raise RuntimeError(
            'Not authenticated when opening solutions page. Login likely failed.'
        )

    # Solutions are lazy-loaded; wait for at least one item when possible.
    try:
        wait.until(
            EC.presence_of_element_located((By.CSS_SELECTOR, 'div.list-item-solutions'))
        )
    except TimeoutException:
        # Page may still be empty for brand-new accounts; keep going so
        # the HTML is saved for inspection.
        print(
            'Warning: no list-item-solutions found yet on {}. '
            'Continuing scroll/load loop.'.format(solutions_url)
        )


def scroll_to_load_all(driver, max_reloads, settle_rounds=3, pause_seconds=2):
    """
    Infinite-scroll the solutions list until height stabilizes or max_reloads.

    Codewars appends more solution cards as you approach the bottom of the page.
    """
    last_height = 0
    stable = 0
    last_count = 0

    for i in range(max_reloads):
        driver.execute_script('window.scrollTo(0, document.body.scrollHeight);')
        time.sleep(pause_seconds)

        new_height = driver.execute_script('return document.body.scrollHeight')
        count = len(driver.find_elements(By.CSS_SELECTOR, 'div.list-item-solutions'))

        print(
            '\rLoading solutions... scroll {}/{} ({} katas visible)'.format(
                i + 1, max_reloads, count
            ),
            end='',
        )

        if new_height == last_height and count == last_count:
            stable += 1
            if stable >= settle_rounds:
                break
        else:
            stable = 0

        last_height = new_height
        last_count = count

    print()
    return last_count


def main():
    setup = load_setup()
    codewars = setup.get('codewars', {})
    email = codewars.get('email', '')
    password = codewars.get('password', '')
    username = codewars.get('username', '')
    headless = setup.get('headless', True)
    n_reloads = int(setup.get('reloads_in_browser', 100))
    output_path = setup.get('source_html', './source.html')

    if not email or not password:
        raise SystemExit(
            'setup.json must include codewars.email and codewars.password '
            'to fetch solutions via the browser.'
        )

    driver = create_driver(headless=headless)
    try:
        print('Logging in...')
        login(driver, email, password)

        print('Detecting username...')
        username = detect_username(driver, configured_username=username)
        print('Using username: {}'.format(username))

        print('Opening completed solutions...')
        open_solutions_page(driver, username)

        print('Scrolling to load solutions (up to {} passes)...'.format(n_reloads))
        total = scroll_to_load_all(driver, max_reloads=n_reloads)
        print('Loaded {} solution groups.'.format(total))

        with open(output_path, 'w', encoding='utf-8') as fout:
            fout.write(driver.page_source)
        print('Saved HTML to {}'.format(output_path))
    finally:
        driver.quit()


if __name__ == '__main__':
    main()
