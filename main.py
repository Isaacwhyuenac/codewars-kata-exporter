import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

from helper.api import CodeWarsApi, RateLimiter
from helper.kata import KataParser

with open('./setup.json') as fin:
    setup = json.load(fin)

with open('./source.html') as fin:
    file = fin.read()

base_dir = setup['download_folder']
extensions = setup['file_extensions']
codewars = setup.get('codewars', {})

# Global spacing between HTTP calls across all workers (seconds).
request_delay = float(setup.get('request_delay_seconds', 0.6))
# Concurrent workers; keep modest — rate limiter is global.
max_workers = int(setup.get('max_workers', 4))
# Retries for 429 / 5xx responses.
max_retries = int(setup.get('max_retries', 6))

parser = KataParser(file)
katas = parser.parse_katas()

# Shared limiter so N workers cannot stampede Codewars.
_rate_limiter = RateLimiter(request_delay)

# requests.Session is not thread-safe — one client per worker, shared cookies/limiter.
_thread_local = threading.local()
_print_lock = threading.Lock()
_progress = {'done': 0}


def get_api():
    api = getattr(_thread_local, 'api', None)
    if api is None:
        api = CodeWarsApi(
            token=codewars.get('api_key', ''),
            email=codewars.get('email', ''),
            password=codewars.get('password', ''),
            rate_limiter=_rate_limiter,
            max_retries=max_retries,
        )
        _thread_local.api = api
    return api


def write_readme(path, title, description):
    with open(path, 'w') as fout:
        fout.write('# {}\n\n'.format(title))
        if description:
            fout.write(description)
            if not description.endswith('\n'):
                fout.write('\n')
        else:
            fout.write('_No description available._\n')


def export_kata(kata):
    """Fetch remote data and write solution files for one kata."""
    api = get_api()
    warnings = []

    try:
        kata_description = api.get_kata_description(kata.kata_id)
    except Exception as exc:
        warnings.append('description failed for {}: {}'.format(kata.title, exc))
        kata_description = ''

    for language, source_code in zip(kata.languages, kata.source_codes):
        language_key = (language or '').strip().lower()
        file_dir = os.path.join(
            base_dir, language_key, kata.difficulty, kata.title,
        )
        os.makedirs(file_dir, exist_ok=True)

        ext = extensions.get(language_key, '')

        # Solution source code
        solution_name = 'solution' + ext
        with open(os.path.join(file_dir, solution_name), 'w') as fout:
            fout.write(source_code)

        # Problem statement (markdown)
        title = kata.title.replace('-', ' ').title()
        write_readme(os.path.join(file_dir, 'README.md'), title, kata_description)

        # Sample test cases (same language extension as the solution)
        try:
            sample_tests = api.get_sample_tests(kata.kata_id, language_key)
        except Exception as exc:
            warnings.append('tests failed for {}/{}: {}'.format(
                kata.title, language_key, exc
            ))
            sample_tests = ''

        if sample_tests:
            tests_name = 'tests' + ext
            with open(os.path.join(file_dir, tests_name), 'w') as fout:
                fout.write(sample_tests)
                if not sample_tests.endswith('\n'):
                    fout.write('\n')

    return warnings


def main():
    total = len(katas)
    workers = max(1, min(max_workers, total or 1))
    print(
        'Exporting {} katas with {} workers '
        '(rate limit: {:.2f}s between requests, {} retries)...'.format(
            total, workers, request_delay, max_retries
        )
    )

    all_warnings = []
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(export_kata, kata): kata for kata in katas}
        for future in as_completed(futures):
            kata = futures[future]
            try:
                warnings = future.result()
            except Exception as exc:
                warnings = ['export crashed for {}: {}'.format(kata.title, exc)]
            if warnings:
                all_warnings.extend(warnings)

            with _print_lock:
                _progress['done'] += 1
                print(
                    '\r{}/{} katas exported.'.format(_progress['done'], total),
                    end='',
                )
                if warnings:
                    for message in warnings:
                        print('\nWarning: {}'.format(message), end='')
                    print(
                        '\r{}/{} katas exported.'.format(_progress['done'], total),
                        end='',
                    )

    print('\nDone!')
    if all_warnings:
        print('{} warning(s) during export.'.format(len(all_warnings)))


if __name__ == '__main__':
    main()
