#!/usr/bin/env python3

# Binary Analysis Next Generation (BANG!)
#
# Copyright 2021-2022 - Armijn Hemel
# Licensed under the terms of the GNU Affero General Public License version 3
# SPDX-License-Identifier: AGPL-3.0-only

'''
Process JSON results generated with bang_extract_identifier.py and
output YARA rules.
'''

import copy
import datetime
import json
import multiprocessing
import pathlib
import pickle
import sys
import uuid

import packageurl
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
    '''Generate YARA rules from identifiers and heuristics'''
    generate_date = datetime.datetime.utcnow().isoformat()
    rule_uuid = uuid.uuid4()
    meta = '''
    meta:
        description = "Rule for %s"
        author = "Generated by BANG"
        date = "%s"
        uuid = "%s"
''' % (metadata['archive'], generate_date, rule_uuid)

    for m in sorted(metadata):
        meta += '        %s = "%s"\n' % (m, metadata[m])

    # TODO: origin and package?
    yara_file = yara_directory / ("%s-%s.yara" % (metadata['archive'], metadata['language']))
    if tags == []:
        rule_name = 'rule rule_%s\n' % str(rule_uuid).translate(NAME_ESCAPE)
    else:
        rule_name = 'rule rule_%s: %s\n' % (str(rule_uuid).translate(NAME_ESCAPE), " ".join(tags))

    with yara_file.open(mode='w') as p:
        p.write(rule_name)
        p.write('{')
        p.write(meta)
        p.write('\n    strings:\n')

        if strings != []:
            # write the strings
            p.write("\n        // Extracted strings\n\n")
            counter = 1
            for s in strings:
                try:
                    if fullword:
                        p.write("        $string%d = \"%s\" fullword\n" % (counter, s.translate(ESCAPE)))
                    else:
                        p.write("        $string%d = \"%s\"\n" % (counter, s.translate(ESCAPE)))
                    counter += 1
                except:
                    pass

        if functions != []:
            # write the functions
            p.write("\n        // Extracted functions\n\n")
            counter = 1
            for s in functions:
                if fullword:
                    p.write("        $function%d = \"%s\" fullword\n" % (counter, s))
                else:
                    p.write("        $function%d = \"%s\"\n" % (counter, s))
                counter += 1

        if variables != []:
            # write the variable names
            p.write("\n        // Extracted variables\n\n")
            counter = 1
            for s in variables:
                if fullword:
                    p.write("        $variable%d = \"%s\" fullword\n" % (counter, s))
                else:
                    p.write("        $variable%d = \"%s\"\n" % (counter, s))
                counter += 1

        # TODO: find good heuristics of how many identifiers should be matched
        p.write('\n    condition:\n')
        if strings != []:
            if len(strings) >= heuristics['strings_minimum_present']:
                num_strings = int(max(len(strings) / 100 * heuristics['strings_percentage'], heuristics['strings_matched']))
                p.write('        %d of ($string*)' % num_strings)
            else:
                p.write('        any of ($string*)')
            if not (functions == set() and variables == set()):
                p.write(' and\n')
            else:
                p.write('\n')
        if functions != []:
            if len(functions) >= heuristics['functions_minimum_present']:
                num_funcs = int(max(len(functions) / 100 * heuristics['functions_percentage'], heuristics['functions_matched']))
                p.write('        %d of ($function*)' % num_funcs)
            else:
                p.write('        any of ($function*)')
            if variables != set():
                p.write(' and\n')
            else:
                p.write('\n')
        if variables != []:
            if len(variables) >= heuristics['variables_minimum_present']:
                num_vars = int(max(len(variables) / 100 * heuristics['variables_percentage'], heuristics['variables_matched']))
                p.write('        %d of ($variable*)\n' % num_vars)
            else:
                p.write('        any of ($variable*)\n')
        p.write('\n}')

    return yara_file.name


def extract_identifiers(process_queue, result_queue, json_directory, yara_output_directory, yara_env):
    '''Read a JSON result file and generate YARA rules'''

    heuristics = yara_env['heuristics']
    while True:
        json_file = process_queue.get()

        with open(json_file, 'r') as json_archive:
            identifiers = json.load(json_archive)

        identifiers_per_language = {}
        language = identifiers['metadata']['language']

        identifiers_per_language[language] = {}
        identifiers_per_language[language]['strings'] = set()
        identifiers_per_language[language]['functions'] = set()
        identifiers_per_language[language]['variables'] = set()

        for string in identifiers['strings']:
            if len(string) >= yara_env['string_min_cutoff'] and len(string) <= yara_env['string_max_cutoff']:
                identifiers_per_language[language]['strings'].add(string)

        for function in identifiers['functions']:
            if len(function) < yara_env['identifier_cutoff']:
                continue
            if language == 'c':
                if function in yara_env['lq_identifiers']['elf']['functions']:
                    continue
            identifiers_per_language[language]['functions'].add(function)

        for variable in identifiers['variables']:
            if len(variable) < yara_env['identifier_cutoff']:
                continue
            if language == 'c':
                if variable in yara_env['lq_identifiers']['elf']['variables']:
                    continue
            identifiers_per_language[language]['variables'].add(variable)

        for language in identifiers_per_language:
            metadata = identifiers['metadata']

            strings = sorted(identifiers_per_language[language]['strings'])
            variables = sorted(identifiers_per_language[language]['variables'])
            functions = sorted(identifiers_per_language[language]['functions'])

            if not (strings == [] and variables == [] and functions == []):
                yara_tags = yara_env['tags'] + [language]
                yara_name = generate_yara(yara_output_directory, metadata, functions, variables, strings, yara_tags, heuristics, yara_env['fullword'])

        result_meta = {}
        for language in identifiers_per_language:
            result_meta[language] = {}
            result_meta[language]['strings'] = len(identifiers_per_language[language]['strings'])
            result_meta[language]['variables'] = len(identifiers_per_language[language]['variables'])
            result_meta[language]['functions'] = len(identifiers_per_language[language]['functions'])

        result_queue.put(result_meta)
        process_queue.task_done()


@click.command(short_help='process BANG result files and output YARA')
@click.option('--config-file', '-c', required=True, help='configuration file', type=click.File('r'))
@click.option('--json-directory', '-j', required=True, help='JSON file directory', type=click.Path(exists=True))
@click.option('--identifiers', '-i', help='pickle with low quality identifiers', type=click.File('rb'))
@click.option('--meta', '-m', required=True, help='file with meta information about a package', type=click.File('r'))
def main(config_file, json_directory, identifiers, meta):
    json_directory = pathlib.Path(json_directory)

    # should be a real directory
    if not json_directory.is_dir():
        print("%s is not a directory, exiting." % json_directory, file=sys.stderr)
        sys.exit(1)

    # parse the package meta information
    try:
        package_meta_information = load(meta, Loader=Loader)
    except (YAMLError, PermissionError) as e:
        print("invalid YAML:", e.args, file=sys.stderr)
        sys.exit(1)

    packages = []

    package = package_meta_information['package']

    # first verify that the top level package url is valid
    try:
        top_purl = packageurl.PackageURL.from_string(package_meta_information['packageurl'])
    except ValueError:
        print("%s not a valid packageurl" % package_meta_information['packageurl'], file=sys.stderr)
        sys.exit(1)

    versions = set()

    for release in package_meta_information['releases']:
        for version in release:
            # verify that the version is a valid package url
            try:
                purl = packageurl.PackageURL.from_string(version)
            except ValueError:
                print("%s not a valid packageurl" % version, file=sys.stderr)
                if extraction_env['error_fatal']:
                    sys.exit(1)
                continue
            # sanity checks to verify that the top level purl matches
            if purl.type != top_purl.type:
                print("type '%s' does not match top level type '%s'" % (purl.type, top_purl.type),
                      file=sys.stderr)
                if extraction_env['error_fatal']:
                    sys.exit(1)
                continue
            if purl.name != top_purl.name:
                print("name '%s' does not match top level name '%s'" % (purl.name, top_purl.name),
                      file=sys.stderr)
                if extraction_env['error_fatal']:
                    sys.exit(1)
                continue
            versions.add(version)

    # store the languages
    languages = set()

    # process all the JSON files in the directory
    for result_file in json_directory.glob('**/*'):
        # sanity check for the package
        try:
            with open(result_file, 'r') as json_archive:
                json_results = json.load(json_archive)

            languages.add(json_results['metadata']['language'])

            if json_results['metadata']['package'] == package:
                if json_results['metadata'].get('packageurl') in versions:
                    packages.append(result_file)
        except Exception as e:
            continue

    # parse the configuration
    yara_config = YaraConfig(config_file)
    try:
        yara_env = yara_config.parse()
    except YaraConfigException as e:
        print(e, file=sys.stderr)
        sys.exit(1)

    # mapping for low quality identifiers. C is mapped to ELF,
    # Java is mapped to Dex. TODO: use something a bit more sensible.
    lq_identifiers = {'elf': {'functions': [], 'variables': [], 'strings': []},
                      'dex': {'functions': [], 'variables': [], 'strings': []}}

    # read the pickle with identifiers
    if identifiers is not None:
        try:
            lq_identifiers = pickle.load(identifiers)
        except:
            pass

    yara_output_directory = yara_env['yara_directory'] / 'src' / top_purl.type / top_purl.name

    yara_output_directory.mkdir(parents=True, exist_ok=True)

    tags = ['source']

    # expand yara_env with source scanning specific values
    yara_env['tags'] = tags
    yara_env['lq_identifiers'] = lq_identifiers

    processmanager = multiprocessing.Manager()

    # create a queue for scanning files
    process_queue = processmanager.JoinableQueue(maxsize=0)
    result_queue = processmanager.JoinableQueue(maxsize=0)
    processes = []

    # walk the archives directory
    for json_file in packages:
        json_results = json_directory / json_file
        process_queue.put(json_results)

    # create processes for unpacking archives
    for i in range(0, yara_env['threads']):
        process = multiprocessing.Process(target=extract_identifiers,
                                          args=(process_queue, result_queue, json_directory,
                                                yara_output_directory, yara_env))
        processes.append(process)

    # start all the processes
    for process in processes:
        process.start()

    process_queue.join()

    # Done processing, terminate processes
    for process in processes:
        process.terminate()

    # store the minimum per language, relevant for heuristics
    min_per_language = {}
    for language in languages:
        min_per_language[language] = {}
        min_per_language[language]['strings'] = sys.maxsize
        min_per_language[language]['variables'] = sys.maxsize
        min_per_language[language]['functions'] = sys.maxsize

    while True:
        try:
            result = result_queue.get_nowait()
            for language in result:
                for identifier in ['strings', 'functions', 'variables']:
                    min_per_language[language][identifier] = min(min_per_language[language][identifier], result[language][identifier])
                result_queue.task_done()
        except:
            break

    # block until the result queue is empty
    result_queue.join()

    # Now generate the top level YARA file
    yara_output_directory = yara_env['yara_directory'] / 'src' / top_purl.type

    # TODO: sort the packages based on version number
    for language in languages:
        # read the JSON again, this time aggregate the data
        all_strings_union = set()
        all_strings_intersection = set()

        all_functions_union = set()
        all_functions_intersection = set()

        all_variables_union = set()
        all_variables_intersection = set()

        website = ''

        # keep track of if the first element is being processed
        is_start = True

        for package in packages:
            with open(package, 'r') as json_archive:
                json_results = json.load(json_archive)

                if website == '':
                    website = json_results['metadata']['website']

                strings = set()

                for string in json_results['strings']:
                    if len(string) >= yara_env['string_min_cutoff'] and len(string) <= yara_env['string_max_cutoff']:
                        strings.add(string)

                functions = set()

                for function in json_results['functions']:
                    if len(function) < yara_env['identifier_cutoff']:
                        continue
                    if language == 'c':
                        if function in yara_env['lq_identifiers']['elf']['functions']:
                            continue
                    functions.add(function)

                variables = set()
                for variable in json_results['variables']:
                    if len(variable) < yara_env['identifier_cutoff']:
                        continue
                    if language == 'c':
                        if variable in yara_env['lq_identifiers']['elf']['variables']:
                            continue
                    variables.add(variable)

                all_strings_union.update(strings)
                all_functions_union.update(functions)
                all_variables_union.update(variables)

                if is_start:
                    all_strings_intersection.update(strings)
                    all_functions_intersection.update(functions)
                    all_variables_intersection.update(variables)
                    is_start = False
                else:
                    all_strings_intersection &= strings
                    all_functions_intersection &= functions
                    all_variables_intersection &= variables

        # sort the identifiers so they are printed in
        # sorted order in the YARA rule as well
        strings = sorted(all_strings_union)
        variables = sorted(all_variables_union)
        functions = sorted(all_functions_union)

        # adapt the heuristics based on the minimum amount of strings
        # found in a package.

        # first instantiate the heuristics
        heuristics = copy.deepcopy(yara_env['heuristics'])

        # then change the percentage based on the minimum
        # amount of identifiers, and the union
        heuristics['strings_percentage'] = min(heuristics['strings_percentage'],
                                               heuristics['strings_percentage'] * min_per_language[language]['strings'] / len(strings))
        heuristics['functions_percentage'] = min(heuristics['functions_percentage'],
                                                heuristics['functions_percentage'] * min_per_language[language]['functions'] / len(functions))
        heuristics['variables_percentage'] = min(heuristics['variables_percentage'],
                                                 heuristics['variables_percentage'] * min_per_language[language]['variables'] / len(variables))

        metadata = {'archive': top_purl.name + "-union", 'language': language,
                    'package': top_purl.name, 'packageurl': top_purl,
                    'website': website}

        if not (strings == set() and variables == set() and functions == set()):
            yara_tags = yara_env['tags'] + [language]
            yara_name = generate_yara(yara_output_directory, metadata, functions, variables, strings, yara_tags, heuristics, yara_env['fullword'])

        strings = sorted(all_strings_intersection)
        variables = sorted(all_variables_intersection)
        functions = sorted(all_functions_intersection)

        # reset heuristics
        heuristics = copy.deepcopy(yara_env['heuristics'])

        metadata = {'archive': top_purl.name + "-intersection", 'language': language,
                    'package': top_purl.name, 'packageurl': top_purl,
                    'website': website}

        if not (strings == [] and variables == [] and functions == []):
            yara_tags = yara_env['tags'] + [language]
            yara_name = generate_yara(yara_output_directory, metadata, functions, variables, strings, yara_tags, heuristics, yara_env['fullword'])


if __name__ == "__main__":
    main()
