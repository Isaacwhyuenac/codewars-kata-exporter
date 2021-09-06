import json
import os
from itertools import groupby
from operator import itemgetter

from helper.api import CodeWarsApi
from helper.kata import KataParser

with open('./setup.json') as fin:
    setup = json.load(fin)

with open('./source.html') as fin:
    file = fin.read()

base_dir = setup['download_folder']
extensions = setup['file_extensions']

parser = KataParser(file)
katas = parser.parse_katas()
api = CodeWarsApi(setup['codewars']['api_key'])

print('Exporting katas...')

for i, kata in enumerate(katas):
    print('\r{}/{} katas exported.'.format(i+1, len(katas)), end='')

    kata_description = api.get_kata_description(kata.kata_id)

    language_source_codes = kata.get_languages_and_source_codes
    # print(language_source_codes)
    for language, source_codes in groupby(language_source_codes, itemgetter(0)):
        for j, language_source_code in enumerate(source_codes, 1):
            file_dir = os.path.join(
                base_dir, language, kata.difficulty, kata.title,
            )
            print(file_dir)

            if not os.path.exists(file_dir):
                os.makedirs(file_dir)

            filename = 'solution_' + str(j) + extensions.get(language, '')
            with open(os.path.join(file_dir, filename), 'w') as fout:
                fout.write(itemgetter(1)(language_source_code))

            with open(os.path.join(file_dir, 'README.md'), 'w') as fout:
                fout.write(kata_description)
