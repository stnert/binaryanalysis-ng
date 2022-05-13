#!/usr/bin/env python3

# Binary Analysis Next Generation (BANG!)
#
# Copyright 2021-2022 - Armijn Hemel
# Licensed under the terms of the GNU Affero General Public License version 3
# SPDX-License-Identifier: AGPL-3.0-only

'''
This script processes BANG results and generates YARA rules for
dynamically linked ELF files.
'''

import datetime
import multiprocessing
import os
import pathlib
import pickle
import re
import sys
import uuid

import click

# import YAML module for the configuration
from yaml import load
from yaml import YAMLError
try:
    from yaml import CLoader as Loader
except ImportError:
    from yaml import Loader

from yara_config import YaraConfig, YaraConfigException

# YARA escape sequences
ESCAPE = str.maketrans({'"': '\\"',
                        '\\': '\\\\',
                        '\t': '\\t',
                        '\n': '\\n'})

NAME_ESCAPE = str.maketrans({'.': '_',
                             '-': '_'})


def generate_yara(yara_directory, metadata, functions, variables, strings, tags, heuristics, fullword):
    generate_date = datetime.datetime.utcnow().isoformat()
    rule_uuid = uuid.uuid4()
    meta = '''
    meta:
        description = "Rule for %s in %s"
        author = "Generated by BANG"
        date = "%s"
        uuid = "%s"
''' % (metadata['name'], metadata['package'], generate_date, rule_uuid)

    for m in sorted(metadata):
        meta += '        %s = "%s"\n' % (m, metadata[m])

    yara_file = yara_directory / ("%s-%s.yara" % (metadata['package'], metadata['name']))
    if tags == []:
        rule_name = 'rule rule_%s\n' % str(rule_uuid).translate(NAME_ESCAPE)
    else:
        rule_name = 'rule rule_%s: %s\n' % (str(rule_uuid).translate(NAME_ESCAPE), " ".join(tags))

    with yara_file.open(mode='w') as p:
        p.write(rule_name)
        p.write('{')
        p.write(meta)
        p.write('\n    strings:\n')

        if strings != set():
            # write the strings
            p.write("\n        // Extracted strings\n\n")
            counter = 1
            for s in sorted(strings):
                try:
                    if fullword:
                        p.write("        $string%d = \"%s\" fullword\n" % (counter, s))
                    else:
                        p.write("        $string%d = \"%s\"\n" % (counter, s))
                    counter += 1
                except:
                    pass

        if functions != set():
            # write the functions
            p.write("\n        // Extracted functions\n\n")
            counter = 1
            for s in sorted(functions):
                if fullword:
                    p.write("        $function%d = \"%s\" fullword\n" % (counter, s))
                else:
                    p.write("        $function%d = \"%s\"\n" % (counter, s))
                counter += 1

        if variables != set():
            # write the variable names
            p.write("\n        // Extracted variables\n\n")
            counter = 1
            for s in sorted(variables):
                if fullword:
                    p.write("        $variable%d = \"%s\" fullword\n" % (counter, s))
                else:
                    p.write("        $variable%d = \"%s\"\n" % (counter, s))
                counter += 1

        p.write('\n    condition:\n')
        if strings != set():
            if len(strings) >= heuristics['strings_minimum_present']:
                num_strings = max(len(strings)//heuristics['strings_percentage'], heuristics['strings_matched'])
                p.write('        %d of ($string*)' % num_strings)
            else:
                p.write('        any of ($string*)')
            if not (functions == set() and variables == set()):
                p.write(' and\n')
            else:
                p.write('\n')
        if functions != set():
            if len(functions) >= heuristics['functions_minimum_present']:
                num_funcs = max(len(functions)//heuristics['functions_percentage'], heuristics['functions_matched'])
                p.write('        %d of ($function*)' % num_funcs)
            else:
                p.write('        any of ($function*)')
            if variables != set():
                p.write(' and\n')
            else:
                p.write('\n')
        if variables != set():
            if len(variables) >= heuristics['variables_minimum_present']:
                num_vars = max(len(variables)//heuristics['variables_percentage'], heuristics['variables_matched'])
                p.write('        %d of ($variable*)\n' % num_vars)
            else:
                p.write('        any of ($variable*)\n')
        p.write('\n}')
    return yara_file.name


def process_directory(yaraqueue, yara_directory, yara_binary_directory,
                      processlock, processed_files, yara_env):

    heuristics = yara_env['heuristics']
    generate_identifier_files = yara_env['generate_identifier_files']
    while True:
        bang_directory = yaraqueue.get()
        bang_pickle = bang_directory / 'bang.pickle'
        functions_per_package = set()
        variables_per_package = set()
        strings_per_package = set()

        yara_files = []

        elf_to_identifiers = {}
        processed = False

        # open the top level pickle
        bang_data = pickle.load(open(bang_pickle, 'rb'))
        package_name = ''
        for bang_file in bang_data['scantree']:
            if 'root' in bang_data['scantree'][bang_file]['labels']:
                package_name = pathlib.Path(bang_file).name
                root_sha256 = bang_data['scantree'][bang_file]['hash']['sha256']

                processlock.acquire()

                # try to catch duplicates
                if root_sha256 in processed_files:
                    processed = True
                processlock.release()
                break

        if processed:
            yaraqueue.task_done()
            continue

        processlock.acquire()
        processed_files[root_sha256] = ''
        processlock.release()

        for bang_file in bang_data['scantree']:
            metadata = {}
            if 'elf' in bang_data['scantree'][bang_file]['labels']:
                sha256 = bang_data['scantree'][bang_file]['hash']['sha256']
                elf_name = pathlib.Path(bang_file).name
                suffix = pathlib.Path(bang_file).suffix

                if suffix in yara_env['ignored_suffixes']:
                    continue

                # TODO: name is actually not correct, as it assumes
                # there is only one binary with that particular name
                # inside a package. Counter example: apt-utils_2.2.4_amd64.deb
                metadata['name'] = elf_name
                metadata['sha256'] = sha256
                metadata['package'] = package_name

                # open the result pickle
                try:
                    results_data = pickle.load(open(bang_directory / 'results' / ("%s.pickle" % sha256), 'rb'))
                except:
                    continue

                if 'ocaml' in results_data['labels']:
                    if yara_env['ignore_ocaml']:
                        continue

                if 'metadata' not in results_data:
                    # example: statically linked binaries currently
                    # have no associated metadata.
                    continue

                if 'tlsh' in results_data:
                    metadata['tlsh'] = results_data['tlsh']

                if 'telfhash' in results_data['metadata']:
                    metadata['telfhash'] = results_data['metadata']['telfhash']

                strings = set()
                functions = set()
                variables = set()
                if results_data['metadata']['strings'] != []:
                    for s in results_data['metadata']['strings']:
                        if len(s) < yara_env['string_min_cutoff']:
                            continue
                        if len(s) > yara_env['string_max_cutoff']:
                            continue
                        # ignore whitespace-only strings
                        if re.match(r'^\s+$', s) is None:
                            strings.add(s.translate(ESCAPE))
                    strings_per_package.update(strings)
                if results_data['metadata']['symbols'] != []:
                    for s in results_data['metadata']['symbols']:
                        if s['section_index'] == 0:
                            continue
                        if yara_env['ignore_weak_symbols']:
                            if s['binding'] == 'weak':
                                continue
                        if len(s['name']) < yara_env['identifier_cutoff']:
                            continue
                        if '@@' in s['name']:
                            identifier_name = s['name'].rsplit('@@', 1)[0]
                        elif '@' in s['name']:
                            identifier_name = s['name'].rsplit('@', 1)[0]
                        else:
                            identifier_name = s['name']
                        if s['type'] == 'func':
                            if identifier_name in yara_env['lq_identifiers']['elf']['functions']:
                                continue
                            functions.add(identifier_name)
                        elif s['type'] == 'object':
                            if identifier_name in yara_env['lq_identifiers']['elf']['variables']:
                                continue
                            variables.add(identifier_name)
                    functions_per_package.update(functions)
                    variables_per_package.update(variables)
                if elf_name not in elf_to_identifiers:
                    elf_to_identifiers['strings'] = strings
                    elf_to_identifiers['variables'] = variables
                    elf_to_identifiers['functions'] = functions

                if len(strings) < heuristics['strings_extracted']:
                    strings = set()
                if len(functions) < heuristics['functions_extracted']:
                    functions = set()
                if len(variables) < heuristics['variables_extracted']:
                    variables = set()

                # do not generate a YARA file if there is no data
                if strings == set() and variables == set() and functions == set():
                    continue

                total_identifiers = len(functions) + len(variables) + len(strings)

                if total_identifiers > yara_env['max_identifiers']:
                    pass

                yara_tags = yara_env['tags'] + ['elf']
                yara_name = generate_yara(yara_binary_directory, metadata, functions, variables, strings, yara_tags, heuristics, yara_env['fullword'])
                yara_files.append(yara_name)
            elif 'dex' in bang_data['scantree'][bang_file]['labels']:
                sha256 = bang_data['scantree'][bang_file]['hash']['sha256']
                dex_name = pathlib.Path(bang_file).name
                suffix = pathlib.Path(bang_file).suffix

                if suffix in yara_env['ignored_suffixes']:
                    continue

                # TODO: name is actually not correct, as it assumes
                # there is only one binary with that particular name
                # inside a package.
                metadata['name'] = dex_name
                metadata['sha256'] = sha256
                metadata['package'] = package_name

                # open the result pickle
                try:
                    results_data = pickle.load(open(bang_directory / 'results' / ("%s.pickle" % sha256), 'rb'))
                except:
                    continue
                if 'metadata' not in results_data:
                    continue

                if 'tlsh' in results_data:
                    metadata['tlsh'] = results_data['tlsh']

                strings = set()
                functions = set()
                variables = set()

                for c in results_data['metadata']['classes']:
                    for method in c['methods']:
                        # ignore whitespace-only methods
                        if len(method['name']) < yara_env['identifier_cutoff']:
                            continue
                        if re.match(r'^\s+$', method['name']) is not None:
                            continue
                        if method['name'] in ['<init>', '<clinit>']:
                            continue
                        if method['name'].startswith('access$'):
                            continue
                        if method['name'] in yara_env['lq_identifiers']['dex']['functions']:
                            continue
                        functions.add(method['name'])
                    for method in c['methods']:
                        for s in method['strings']:
                            if len(s) < yara_env['string_min_cutoff']:
                                continue
                            if len(s) > yara_env['string_max_cutoff']:
                                continue
                            # ignore whitespace-only strings
                            if re.match(r'^\s+$', s) is None:
                                strings.add(s.translate(ESCAPE))

                    for field in c['fields']:
                        # ignore whitespace-only methods
                        if len(field['name']) < yara_env['identifier_cutoff']:
                            continue
                        if re.match(r'^\s+$', field['name']) is not None:
                            continue

                        if field['name'] in yara_env['lq_identifiers']['dex']['variables']:
                            continue
                        variables.add(field['name'])

                # do not generate a YARA file if there is no data
                if strings == set() and variables == set() and functions == set():
                    continue

                total_identifiers = len(functions) + len(variables) + len(strings)

                if total_identifiers > yara_env['max_identifiers']:
                    pass

                yara_tags = yara_env['tags'] + ['dex']
                yara_name = generate_yara(yara_binary_directory, metadata, functions, variables, strings, yara_tags, heuristics, yara_env['fullword'])
                yara_files.append(yara_name)

        if yara_files != []:
            yara_file = yara_directory / ("%s.yara" % package_name)
            with yara_file.open(mode='w') as p:
                p.write("/*\nRules for %s\n*/\n" % package_name)
                #for y in yara_files:
                for y in sorted(set(yara_files)):
                    p.write("include \"./binary/%s\"\n" % y)
            if generate_identifier_files:
                if len(functions_per_package) != 0:
                    yara_file = yara_directory / ("%s.func" % package_name)
                    with yara_file.open(mode='w') as p:
                        for f in sorted(functions_per_package):
                            p.write(f)
                            p.write('\n')
                if len(variables_per_package) != 0:
                    yara_file = yara_directory / ("%s.var" % package_name)
                    with yara_file.open(mode='w') as p:
                        for f in sorted(variables_per_package):
                            p.write(f)
                            p.write('\n')
                if len(strings) != 0:
                    yara_file = yara_directory / ("%s.strings" % package_name)
                    with yara_file.open(mode='w') as p:
                        for f in sorted(strings_per_package):
                            p.write(f)
                            p.write('\n')
        yaraqueue.task_done()


@click.command(short_help='process BANG result files and output YARA')
@click.option('--config-file', '-c', required=True, help='configuration file', type=click.File('r'))
@click.option('--result-directory', '-r', help='BANG result directories', type=click.Path(exists=True), required=True)
@click.option('--identifiers', '-i', help='pickle with low quality identifiers', type=click.File('rb'))
def main(config_file, result_directory, identifiers):

    result_directory = pathlib.Path(result_directory)

    # ... and should be a real directory
    if not result_directory.is_dir():
        print("Error: %s is not a directory, exiting." % result_directory, file=sys.stderr)
        sys.exit(1)

    lq_identifiers = {'elf': {'functions': [], 'variables': []},
                      'dex': {'functions': [], 'variables': []}}

    # read the pickle with identifiers
    if identifiers is not None:
        try:
            lq_identifiers = pickle.load(identifiers)
        except:
            pass

    # parse the configuration
    yara_config = YaraConfig(config_file)
    yara_env = yara_config.parse()

    yara_binary_directory = yara_env['yara_directory'] / 'binary'

    yara_binary_directory.mkdir(exist_ok=True)

    processmanager = multiprocessing.Manager()

    # ignore object files (regular and GHC specific)
    ignored_suffixes = ['.o', '.p_o']

    # create a lock to control access to any shared data structures
    processlock = multiprocessing.Lock()

    # create a shared dictionary
    processed_files = processmanager.dict()

    # create a queue for scanning files
    yaraqueue = processmanager.JoinableQueue(maxsize=0)
    processes = []

    # walk the results directory
    for bang_directory in result_directory.iterdir():
        try:
            bang_pickle = bang_directory / 'bang.pickle'
            if not bang_pickle.exists():
                continue
        except PermissionError:
            continue

        yaraqueue.put(bang_directory)

    # tags = ['debian', 'debian11']
    tags = []

    generate_identifier_files = False

    # expand yara_env with binary scanning specific values
    yara_env['ignored_suffixes'] = ignored_suffixes
    yara_env['tags'] = tags
    yara_env['lq_identifiers'] = lq_identifiers
    yara_env['generate_identifier_files'] = generate_identifier_files

    # create processes for unpacking archives
    for i in range(0, yara_env['threads']):
        process = multiprocessing.Process(target=process_directory,
                                          args=(yaraqueue, yara_env['yara_directory'],
                                                yara_binary_directory, processlock,
                                                processed_files, yara_env))
        processes.append(process)

    # start all the processes
    for process in processes:
        process.start()

    yaraqueue.join()

    # Done processing, terminate processes
    for process in processes:
        process.terminate()


if __name__ == "__main__":
    main()
