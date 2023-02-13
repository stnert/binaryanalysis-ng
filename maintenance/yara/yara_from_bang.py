#!/usr/bin/env python3

# Binary Analysis Next Generation (BANG!)
#
# Copyright - Armijn Hemel, Tjaldur Software Governance Solutions
# Licensed under the terms of the GNU Affero General Public License version 3
# SPDX-License-Identifier: AGPL-3.0-only

'''
This script processes BANG results and generates YARA rules for
dynamically linked ELF files.
'''

import collections
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


def generate_yara(yara_directory, metadata, functions, variables, strings, tags, heuristics, fullword, yara_operator):
    generate_date = datetime.datetime.utcnow().isoformat()
    rule_uuid = uuid.uuid4()
    meta = '''
    meta:
        description = "Rule for %s"
        author = "Generated by BANG"
        date = "%s"
        uuid = "%s"
''' % (metadata['name'], generate_date, rule_uuid)

    for m in sorted(metadata):
        meta += '        %s = "%s"\n' % (m, metadata[m])

    yara_file = yara_directory / ("%s-%s.yara" % (metadata['name'], metadata['sha256']))
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
                p.write(' %s\n' % yara_operator)
            else:
                p.write('\n')
        if functions != set():
            if len(functions) >= heuristics['functions_minimum_present']:
                num_funcs = max(len(functions)//heuristics['functions_percentage'], heuristics['functions_matched'])
                p.write('        %d of ($function*)' % num_funcs)
            else:
                p.write('        any of ($function*)')
            if variables != set():
                p.write(' %s\n' % yara_operator)
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


def process_bang(yara_queue, yara_directory, yara_binary_directory,
                      process_lock, processed_files, yara_env):
    '''Generate a YARA file for a single ELF or Dex binary'''

    heuristics = yara_env['heuristics']
    generate_identifier_files = yara_env['generate_identifier_files']
    while True:
        bang_pickle = yara_queue.get()

        # open the pickle
        bang_data = pickle.load(open(bang_pickle, 'rb'))

        # store the type of executable
        if 'elf' in bang_data['labels']:
            exec_type = 'elf'
        else:
            exec_type = 'dex'

        # there is a bug where sometimes no hashes are computed
        if 'hashes' not in bang_data['metadata']:
            yara_queue.task_done()
            continue

        path_name = bang_pickle.with_name('pathname')
        with open(path_name, 'r') as path_name_file:
             file_name = pathlib.Path(path_name_file.read()).name

        # TODO: filter empty files
        sha256 = bang_data['metadata']['hashes']['sha256']

        process_lock.acquire()

        # try to catch duplicates
        if sha256 in processed_files:
            process_lock.release()
            yara_queue.task_done()
            continue

        processed_files[sha256] = ''
        process_lock.release()

        # set metadata
        metadata = {'sha256': sha256, 'name': file_name}

        if 'tlsh' in bang_data['metadata']['hashes']:
            metadata['tlsh'] = bang_data['metadata']['hashes']['tlsh']

        strings = set()

        if exec_type == 'elf':
            functions = set()
            variables = set()

            if 'telfhash' in bang_data['metadata']:
                metadata['telfhash'] = bang_data['metadata']['telfhash']

            # process strings
            if bang_data['metadata']['strings'] != []:
                for s in bang_data['metadata']['strings']:
                    if len(s) < yara_env['string_min_cutoff']:
                        continue
                    if len(s) > yara_env['string_max_cutoff']:
                        continue
                    # ignore whitespace-only strings
                    if re.match(r'^\s+$', s) is None:
                        if s in yara_env['lq_identifiers']['elf']['strings']:
                            continue
                        strings.add(s.translate(ESCAPE))

            # process symbols, split in functions and variables
            if bang_data['metadata']['symbols'] != []:
                for s in bang_data['metadata']['symbols']:
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

            if len(strings) < heuristics['strings_extracted']:
                strings = set()
            if len(functions) < heuristics['functions_extracted']:
                functions = set()
            if len(variables) < heuristics['variables_extracted']:
                variables = set()

            # do not generate a YARA file if there is no data
            if strings == set() and variables == set() and functions == set():
                yara_queue.task_done()
                continue

            total_identifiers = len(functions) + len(variables) + len(strings)

            if total_identifiers > yara_env['max_identifiers']:
                pass

            yara_tags = yara_env['tags'] + ['elf']
            yara_name = generate_yara(yara_binary_directory, metadata, functions, variables, strings, yara_tags, heuristics, yara_env['fullword'], yara_env['operator'])
        elif exec_type == 'dex':
            functions = set()
            variables = set()

            for c in bang_data['metadata']['classes']:
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
                yara_queue.task_done()
                continue

            total_identifiers = len(functions) + len(variables) + len(strings)

            if total_identifiers > yara_env['max_identifiers']:
                pass

            yara_tags = yara_env['tags'] + ['dex']
            yara_name = generate_yara(yara_binary_directory, metadata, functions, variables, strings, yara_tags, heuristics, yara_env['fullword'], yara_env['operator'])

        yara_queue.task_done()


@click.command(short_help='process BANG result files and output YARA')
@click.option('--config-file', '-c', required=True, help='configuration file', type=click.File('r'))
@click.option('--result-directory', '-r', help='BANG result directories', type=click.Path(exists=True), required=True)
@click.option('--identifiers', '-i', help='pickle with low quality identifiers', type=click.File('rb'))
def main(config_file, result_directory, identifiers):

    # store the result directory as a pathlib Path instead of str
    result_directory = pathlib.Path(result_directory)

    # result_directory should be a real directory
    if not result_directory.is_dir():
        print("Error: %s is not a directory, exiting." % result_directory, file=sys.stderr)
        sys.exit(1)

    # define a data structure with low quality
    # identifiers for ELF and Dex
    lq_identifiers = {'elf': {'functions': [], 'variables': [], 'strings': []},
                      'dex': {'functions': [], 'variables': [], 'strings': []}}

    # read the pickle with low quality identifiers
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
    ignored_elf_suffixes = ['.o', '.p_o']

    # create a lock to control access to any shared data structures
    process_lock = multiprocessing.Lock()

    # create a shared dictionary
    processed_files = processmanager.dict()

    # create a queue for scanning files
    yara_queue = processmanager.JoinableQueue(maxsize=0)
    processes = []

    # read the root pickle
    try:
        bang_pickle = result_directory / 'info.pkl'
        if not bang_pickle.exists():
            print(f"Error: cannot find {bang_pickle}, exiting.", file=sys.stderr)
            sys.exit(1)
    except PermissionError:
        print(f"Error: cannot read {bang_pickle} (permission error?), exiting.", file=sys.stderr)
        sys.exit(1)

    # create a deque to store results in and retrieve results from
    file_deque = collections.deque()
    file_deque.append(bang_pickle)

    # walk the unpack tree recursively
    while True:
        try:
            bang_pickle = file_deque.popleft()
        except:
            break

        try:
            bang_data = pickle.load(open(bang_pickle, 'rb'))
        except:
            continue

        path_name = bang_pickle.with_name('pathname')
        with open(path_name, 'r') as path_name_file:
             root_name = pathlib.Path(path_name_file.read()).name

        if 'labels' in bang_data:
            if 'ocaml' in bang_data['labels']:
                if yara_env['ignore_ocaml']:
                    continue
            if 'elf' in bang_data['labels']:
                suffix = pathlib.Path(root_name).suffix

                if suffix in ignored_elf_suffixes:
                    continue

                if 'static' in bang_data['labels']:
                    if not 'linuxkernelmodule' in bang_data['labels']:
                        # TODO: clean up for linux kernel modules
                        continue

                yara_queue.put(bang_pickle)
            elif 'dex' in bang_data['labels']:
                yara_queue.put(bang_pickle)

        # add the unpacked/extracted files to the deque
        if 'unpacked_relative_files' in bang_data:
            for unpacked_file in bang_data['unpacked_relative_files']:
                file_meta_directory = bang_data['unpacked_relative_files'][unpacked_file]
                file_pickle = result_directory.parent / file_meta_directory / 'info.pkl'
                file_deque.append(file_pickle)
        if 'unpacked_absolute_files' in bang_data:
            for unpacked_file in bang_data['unpacked_absolute_files']:
                file_meta_directory = bang_data['unpacked_absolute_files'][unpacked_file]
                file_pickle = result_directory.parent / file_meta_directory / 'info.pkl'
                file_deque.append(file_pickle)
        if 'extracted_files' in bang_data:
            for unpacked_file in bang_data['extracted_files']:
                file_meta_directory = bang_data['extracted_files'][unpacked_file]
                file_pickle = result_directory.parent / file_meta_directory / 'info.pkl'
                file_deque.append(file_pickle)

    # tags = ['debian', 'debian11']
    tags = []

    generate_identifier_files = False

    # expand yara_env with binary scanning specific values
    yara_env['tags'] = tags
    yara_env['lq_identifiers'] = lq_identifiers
    yara_env['generate_identifier_files'] = generate_identifier_files

    # create processes for unpacking archives
    for i in range(0, yara_env['threads']):
        process = multiprocessing.Process(target=process_bang,
                                          args=(yara_queue, yara_env['yara_directory'],
                                                yara_binary_directory, process_lock,
                                                processed_files, yara_env))
        processes.append(process)

    # start all the processes
    for process in processes:
        process.start()

    yara_queue.join()

    # Done processing, terminate processes
    for process in processes:
        process.terminate()


if __name__ == "__main__":
    main()
