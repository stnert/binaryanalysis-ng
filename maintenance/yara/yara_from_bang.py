#!/usr/bin/env python3

# Binary Analysis Next Generation (BANG!)
#
# Copyright - Armijn Hemel, Tjaldur Software Governance Solutions
# Licensed under the terms of the GNU Affero General Public License version 3
# SPDX-License-Identifier: AGPL-3.0-only

'''
This script generates a YARA rule from a JSON file containing symbols
and strings that were extracted from a binary using BANG.

Use bang_to_json.py to generate the JSON file.
'''

import datetime
import json
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
                p.write(f'       {num_strings} of ($string*)')
            else:
                p.write('        any of ($string*)')
            if not (functions == set() and variables == set()):
                p.write(' %s\n' % yara_operator)
            else:
                p.write('\n')
        if functions != set():
            if len(functions) >= heuristics['functions_minimum_present']:
                num_funcs = max(len(functions)//heuristics['functions_percentage'], heuristics['functions_matched'])
                p.write(f'       {num_funcs} of ($string*)')
            else:
                p.write('        any of ($function*)')
            if variables != set():
                p.write(' %s\n' % yara_operator)
            else:
                p.write('\n')
        if variables != set():
            if len(variables) >= heuristics['variables_minimum_present']:
                num_vars = max(len(variables)//heuristics['variables_percentage'], heuristics['variables_matched'])
                p.write(f'       {num_vars} of ($string*)')
            else:
                p.write('        any of ($variable*)\n')
        p.write('\n}')
    return yara_file.name

@click.command(short_help='process BANG JSON result file and output YARA')
@click.option('--config-file', '-c', required=True, help='configuration file', type=click.File('r'))
@click.option('--json', '-j', 'result_json', help='BANG result directories', type=click.File('r'), required=True)
@click.option('--identifiers', '-i', help='pickle with low quality identifiers', type=click.File('rb'))
def main(config_file, result_json, identifiers):
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

    # ignore object files (regular and GHC specific)
    ignored_elf_suffixes = ['.o', '.p_o']

    # load the JSON
    try:
        bang_data = json.load(result_json)
    except:
        print("Could not open JSON, exiting", file=sys.stderr)
        sys.exit(1)

    if 'labels' in bang_data:
        if 'ocaml' in bang_data['labels']:
            if yara_env['ignore_ocaml']:
                print("OCAML file found that should be ignored, exiting", file=sys.stderr)
                sys.exit()
        if 'elf' in bang_data['labels']:
            suffix = pathlib.Path(bang_data['metadata']['name']).suffix

            if suffix in ignored_elf_suffixes:
                print("Ignored suffix, exiting", file=sys.stderr)
                sys.exit()

            if 'static' in bang_data['labels']:
                if not 'linuxkernelmodule' in bang_data['labels']:
                    # TODO: clean up for linux kernel modules
                    print("Static ELF binary not supported yet, exiting", file=sys.stderr)
                    sys.exit()

    if bang_data['metadata']['sha256'] == 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855':
        print("Cannot generate YARA file for empty file, exiting", file=sys.stderr)
        sys.exit(1)

    # tags = ['debian', 'debian11']
    tags = []

    # expand yara_env with binary scanning specific values
    yara_env['tags'] = tags
    yara_env['lq_identifiers'] = lq_identifiers

    # store the type of executable
    if 'elf' in bang_data['labels']:
        exec_type = 'elf'
    elif 'dex' in bang_data['labels']:
        exec_type = 'dex'
    else:
        exec_type = None

    if not exec_type:
        print("Unsupported executable type, exiting", file=sys.stderr)
        sys.exit(2)

    # set metadata
    metadata = bang_data['metadata']

    strings = set()

    heuristics = yara_env['heuristics']

    if exec_type == 'elf':
        functions = set()
        variables = set()

        if 'telfhash' in bang_data['metadata']:
            metadata['telfhash'] = bang_data['metadata']['telfhash']

        # process strings
        if bang_data['strings'] != []:
            for s in bang_data['strings']:
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
        if bang_data['symbols'] != []:
            for s in bang_data['symbols']:
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
            return

        total_identifiers = len(functions) + len(variables) + len(strings)

        if total_identifiers > yara_env['max_identifiers']:
            pass

        yara_tags = yara_env['tags'] + ['elf']
        generate_yara(yara_binary_directory, metadata, functions, variables, strings, yara_tags, heuristics, yara_env['fullword'], yara_env['operator'])
    elif exec_type == 'dex':
        functions = set()
        variables = set()

        for c in bang_data['classes']:
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
            return

        total_identifiers = len(functions) + len(variables) + len(strings)

        if total_identifiers > yara_env['max_identifiers']:
            pass

        yara_tags = yara_env['tags'] + ['dex']
        generate_yara(yara_binary_directory, metadata, functions, variables, strings, yara_tags, heuristics, yara_env['fullword'], yara_env['operator'])

if __name__ == "__main__":
    main()
