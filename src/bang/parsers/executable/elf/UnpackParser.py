# Binary Analysis Next Generation (BANG!)
#
# This file is part of BANG.
#
# BANG is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License, version 3,
# as published by the Free Software Foundation.
#
# BANG is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public
# License, version 3, along with BANG.  If not, see
# <http://www.gnu.org/licenses/>
#
# Copyright Armijn Hemel
# Licensed under the terms of the GNU Affero General Public License
# version 3
# SPDX-License-Identifier: AGPL-3.0-only

import binascii
import hashlib
import json
import pathlib

import tlsh
import telfhash

from bang.UnpackParser import UnpackParser, check_condition
from bang.UnpackParserException import UnpackParserException
from kaitaistruct import ValidationFailedError, UndecidedEndiannessError
from . import elf

# a list of (partial) names of functions that have been
# compiled with FORTIFY_SOURCE. This list is not necessarily
# complete, but at least catches some verified functions.
FORTIFY_NAMES = ['cpy_chk', 'printf_chk', 'cat_chk', 'poll_chk',
                 'read_chk', '__memset_chk', '__memmove_chk',
                 'syslog_chk', '__longjmp_chk', '__fdelt_chk',
                 '__realpath_chk', '__explicit_bzero_chk', '__recv_chk',
                 '__getdomainname_chk', '__gethostname_chk']

# some names used in OCaml
OCAML_NAMES = ['caml_c_cal', 'caml_init_atom_table',
               'caml_init_backtrace', 'caml_init_custom_operations',
               'caml_init_domain', 'caml_init_frame_descriptors',
               'caml_init_gc', 'caml_init_ieee_floats',
               'caml_init_locale', 'caml_init_major_heap',
               'caml_init_signals', 'caml_sys_error',
               'caml_sys_executable_name', 'caml_sys_exit',
               'caml_sys_file_exists', 'caml_sys_get_argv',
               'caml_sys_get_config', 'caml_sys_getcwd',
               'caml_sys_getenv', 'caml_sys_init']

# road only data sections. This should be expanded.
RODATA_SECTIONS = ['.rodata', '.rodata.str1.1', '.rodata.str1.4',
                   '.rodata.str1.8', '.rodata.cst4', '.rodata.cst8',
                   '.rodata.cst16', 'rodata']

# sections with interesting data found in guile programs
GUILE_STRTAB_SECTIONS = ['.guile.arities.strtab', '.guile.docstrs.strtab']

REMOVE_CHARACTERS = ['\a', '\b', '\v', '\f', '\x01', '\x02', '\x03', '\x04',
                     '\x05', '\x06', '\x0e', '\x0f', '\x10', '\x11', '\x12',
                     '\x13', '\x14', '\x15', '\x16', '\x17', '\x18', '\x19',
                     '\x1a', '\x1b', '\x1c', '\x1d', '\x1e', '\x1f', '\x7f']

REMOVE_CHARACTERS_TABLE = str.maketrans({'\a': '', '\b': '', '\v': '',
                                         '\f': '', '\x01': '', '\x02': '',
                                         '\x03': '', '\x04': '', '\x05': '',
                                         '\x06': '', '\x0e': '', '\x0f': '',
                                         '\x10': '', '\x11': '', '\x12': '',
                                         '\x13': '', '\x14': '', '\x15': '',
                                         '\x16': '', '\x17': '', '\x18': '',
                                         '\x19': '', '\x1a': '', '\x1b': '',
                                         '\x1c': '', '\x1d': '', '\x1e': '',
                                         '\x1f': '', '\x7f': ''
                                        })

# translation table for ASCII strings for the string
# to pass the isascii() test
STRING_TRANSLATION_TABLE = str.maketrans({'\t': ' '})

# hashes to compute for sections
HASH_ALGORITHMS = ['sha256', 'md5', 'sha1']


class ElfUnpackParser(UnpackParser):
    extensions = []
    signatures = [
        (0, b'\x7f\x45\x4c\x46')
    ]
    pretty_name = 'elf'

    def parse(self):
        try:
            self.data = elf.Elf.from_io(self.infile)

            # calculate size, also read all the data to catch EOF
            # This isn't always accurate, for example when debugging
            # data is stored in ELF files as a compressed ELF file.
            phoff = self.data.header.program_header_offset
            self.unpacked_size = phoff
            for header in self.data.header.program_headers:
                self.unpacked_size = max(self.unpacked_size, phoff + header.offset + header.filesz)

            # TODO: Qualcomm DSP6 (Hexagon) files, as found on many
            # Android devices.

            # typically the section header is at the end of the ELF file
            shoff = self.data.header.section_header_offset
            self.unpacked_size = max(self.unpacked_size, shoff + self.data.header.qty_section_header
                                     * self.data.header.section_header_entry_size)
            for header in self.data.header.section_headers:
                if header.type == elf.Elf.ShType.nobits:
                    continue
                self.unpacked_size = max(self.unpacked_size, header.ofs_body + header.len_body)

                # ugly ugly hack to work around situations on Android where
                # ELF files have been split into individual sections and all
                # offsets are wrong.
                if header.type == elf.Elf.ShType.note:
                    for entry in header.body.entries:
                        pass
                elif header.type == elf.Elf.ShType.strtab:
                    for entry in header.body.entries:
                        pass

                # force read the header name
                name = header.name
                if header.type == elf.Elf.ShType.symtab:
                    if header.name == '.symtab':
                        for entry in header.body.entries:
                            name = entry.name
                if header.type == elf.Elf.ShType.dynamic:
                    if header.name == '.dynamic':
                        for entry in header.body.entries:
                            if entry.tag_enum == elf.Elf.DynamicArrayTags.needed:
                                name = entry.value_str
                            elif entry.tag_enum == elf.Elf.DynamicArrayTags.rpath:
                                name = entry.value_str
                            elif entry.tag_enum == elf.Elf.DynamicArrayTags.runpath:
                                name = entry.value_str
                            elif entry.tag_enum == elf.Elf.DynamicArrayTags.soname:
                                name = entry.value_str

                elif header.type == elf.Elf.ShType.symtab:
                    if header.name == '.symtab':
                        for entry in header.body.entries:
                            name = entry.name
                            name = entry.type.name
                            name = entry.bind.name
                            name = entry.visibility.name
                            name = entry.sh_idx
                            name = entry.size
                elif header.type == elf.Elf.ShType.dynsym:
                    if header.name == '.dynsym':
                        for entry in header.body.entries:
                            name = entry.name
                            name = entry.type.name
                            name = entry.bind.name
                            name = entry.visibility.name
                            name = entry.sh_idx
                            name = entry.size
                elif header.type == elf.Elf.ShType.progbits:
                    if header.name in RODATA_SECTIONS:
                        body = header.body

            # read the names, but don't proces them. This is just to force
            # evaluation, which normally happens lazily for instances in
            # kaitai struct.
            names = self.data.header.section_names

            # TODO linux kernel module signatures
            # see scripts/sign-file.c in Linux kernel
        except (Exception, ValidationFailedError, UndecidedEndiannessError) as e:
            raise UnpackParserException(e.args)

    def calculate_unpacked_size(self):
        pass

    def unpack(self, meta_directory):
        # interesting data might reside in some of the ELF sections.
        # TODO: write *all* ELF sections?
        # There are some Android variants where the interesting data might
        # span multiple sections.
        for header in self.data.header.section_headers:
            if header.type == elf.Elf.ShType.progbits:
                interesting = False
                if header.name in ['.gnu_debugdata', '.qtmimedatabase', '.BTF', '.BTF.ext']:
                    interesting = True
                if header.name.startswith('.gresource'):
                    interesting = True
                if interesting:
                    file_path = pathlib.Path(header.name)
                    with meta_directory.unpack_regular_file(file_path) as (unpacked_md, outfile):
                        outfile.write(header.body)
                        yield unpacked_md

    def write_info(self, to_meta_directory):
        self.labels, self.metadata = self.extract_metadata_and_labels(to_meta_directory)
        super().write_info(to_meta_directory)

    def extract_metadata_and_labels(self, to_meta_directory):
        '''Extract metadata from the ELF file and set labels'''
        labels = ['elf']
        metadata = {}
        string_cutoff_length = 4

        if self.data.bits == elf.Elf.Bits.b32:
            metadata['bits'] = 32
        elif self.data.bits == elf.Elf.Bits.b64:
            metadata['bits'] = 64

        # store the endianness
        if self.data.endian == elf.Elf.Endian.le:
            metadata['endian'] = 'little'
        elif self.data.endian == elf.Elf.Endian.be:
            metadata['endian'] = 'big'

        # store the ELF version
        metadata['version'] = self.data.ei_version

        # store the type of ELF file
        if self.data.header.e_type == elf.Elf.ObjType.no_file_type:
            metadata['type'] = None
        elif self.data.header.e_type == elf.Elf.ObjType.relocatable:
            metadata['type'] = 'relocatable'
        elif self.data.header.e_type == elf.Elf.ObjType.executable:
            metadata['type'] = 'executable'
        elif self.data.header.e_type == elf.Elf.ObjType.shared:
            metadata['type'] = 'shared'
        elif self.data.header.e_type == elf.Elf.ObjType.core:
            metadata['type'] = 'core'
        else:
            metadata['type'] = 'processor specific'

        # store the machine type, both numerical and pretty printed
        if type(self.data.header.machine) == int:
            metadata['machine_name'] = "unknown architecture"
            metadata['machine'] = self.data.header.machine
        else:
            metadata['machine_name'] = self.data.header.machine.name
            metadata['machine'] = self.data.header.machine.value

        # store the ABI, both numerical and pretty printed
        metadata['abi_name'] = self.data.abi.name
        metadata['abi'] = self.data.abi.value

        metadata['security'] = []

        # record the section names so they are easily accessible
        if self.data.header.section_names is not None:
            metadata['section_names'] = sorted(self.data.header.section_names.entries)

        # RELRO is a technique to mitigate some security vulnerabilities
        # http://refspecs.linuxfoundation.org/LSB_4.1.0/LSB-Core-generic/LSB-Core-generic/progheader.html
        seen_relro = False

        for header in self.data.header.program_headers:
            if header.type == elf.Elf.PhType.gnu_relro:
                metadata['security'].append('relro')
                seen_relro = True
            elif header.type == elf.Elf.PhType.gnu_stack:
                # check to see if NX is set
                if not header.flags_obj.execute:
                    metadata['security'].append('nx')
            elif header.type == elf.Elf.PhType.pax_flags:
                metadata['security'].append('pax')

        # store the data normally extracted using for example 'strings'
        data_strings = []

        # store dependencies (empty for statically linked binaries)
        needed = []

        # store dynamic symbols (empty for statically linked binaries)
        dynamic_symbols = []

        # guile symbols (empty for non-Guile programs)
        guile_symbols = []

        # store information about notes
        notes = []

        # store symbols (empty for most binaries, except for
        # non-stripped binaries)
        symbols = []

        # store RPATH and RUNPATH. Both could be present in a binary
        rpath = ''
        runpath = ''

        # shared object name (for libraries)
        self.soname = ''

        # module name (for Linux kernel modules)
        self.module_name = ''
        linux_kernel_module_info = {}

        # process the various section headers
        is_dynamic_elf = False
        section_to_hash = {}
        section_information = {}
        section_ctr = 0
        for header in self.data.header.section_headers:
            section_information[header.name] = {}
            section_information[header.name]['nr'] = section_ctr
            section_information[header.name]['address'] = header.addr
            if header.type != elf.Elf.ShType.nobits:
                section_information[header.name]['size'] = header.len_body
                section_information[header.name]['offset'] = header.ofs_body
                if header.body != b'':
                    section_information[header.name]['hashes'] = {}
                    for h in HASH_ALGORITHMS:
                        section_hash = hashlib.new(h)
                        section_hash.update(header.raw_body)
                        section_information[header.name]['hashes'][h] = section_hash.hexdigest()

                    try:
                        tlsh_hash = tlsh.hash(header.raw_body)
                        if tlsh_hash != 'TNULL':
                            section_information[header.name]['hashes']['tlsh'] = tlsh_hash
                    except:
                        pass

            section_ctr += 1

            if header.name in ['.modinfo', '__ksymtab_strings']:
                labels.append('linuxkernelmodule')
                try:
                    module_meta = header.body.split(b'\x00')
                    for m in module_meta:
                        meta = m.decode()
                        if meta.startswith('name='):
                            self.module_name = meta.split('=', maxsplit=1)[1]
                            linux_kernel_module_info['name'] = self.module_name
                            break
                except Exception as e:
                    pass
            elif header.name in ['.oat_patches', '.text.oat_patches', '.dex']:
                # OAT information has been stored in various sections
                # test files:
                # .oat_patches : fugu-lrx21m-factory-e012394c.zip
                labels.append('oat')
                labels.append('android')
            elif header.name in ['.guile.procprops', '.guile.frame-maps',
                                 '.guile.arities.strtab', '.guile.arities',
                                 '.guile.docstrs.strtab', '.guile.docstrs']:
                labels.append('guile')

            if header.type == elf.Elf.ShType.dynamic:
                if header.name == '.dynamic':
                    for entry in header.body.entries:
                        if entry.tag_enum == elf.Elf.DynamicArrayTags.needed:
                            needed.append(entry.value_str)
                        elif entry.tag_enum == elf.Elf.DynamicArrayTags.rpath:
                            rpath = entry.value_str
                        elif entry.tag_enum == elf.Elf.DynamicArrayTags.runpath:
                            runpath = entry.value_str
                        elif entry.tag_enum == elf.Elf.DynamicArrayTags.soname:
                            self.soname = entry.value_str
                        elif entry.tag_enum == elf.Elf.DynamicArrayTags.flags_1:
                            # check for position independent code
                            if entry.flag_1_values.pie:
                                metadata['security'].append('pie')
                            # check for bind_now
                            if entry.flag_1_values.now:
                                if seen_relro:
                                    metadata['security'].append('full relro')
                                else:
                                    metadata['security'].append('partial relro')
                        elif entry.tag_enum == elf.Elf.DynamicArrayTags.flags:
                            # check for bind_now here as well
                            if entry.flag_values.bind_now:
                                if seen_relro:
                                    metadata['security'].append('full relro')
                                else:
                                    metadata['security'].append('partial relro')
            elif header.type == elf.Elf.ShType.symtab:
                if header.name == '.symtab':
                    for entry in header.body.entries:
                        symbol = {}
                        if entry.name is None:
                            symbol['name'] = ''
                        else:
                            symbol['name'] = entry.name
                        symbol['type'] = entry.type.name
                        symbol['binding'] = entry.bind.name
                        symbol['visibility'] = entry.visibility.name
                        symbol['section_index'] = entry.sh_idx
                        symbol['size'] = entry.size
                        symbols.append(symbol)
            elif header.type == elf.Elf.ShType.dynsym:
                if header.name == '.dynsym':
                    for entry in header.body.entries:
                        symbol = {}
                        if entry.name is None:
                            symbol['name'] = ''
                        else:
                            symbol['name'] = entry.name
                        symbol['type'] = entry.type.name
                        symbol['binding'] = entry.bind.name
                        symbol['visibility'] = entry.visibility.name
                        symbol['section_index'] = entry.sh_idx
                        symbol['size'] = entry.size
                        symbols.append(symbol)
                        dynamic_symbols.append(symbol)

                        if symbol['name'] == 'oatdata':
                            labels.append('oat')
                            labels.append('android')

                        if symbol['name'] in OCAML_NAMES:
                            labels.append('ocaml')

                        # security related information
                        if symbol['name'] == '__stack_chk_fail':
                            metadata['security'].append('stack smashing protector')
                        if '_chk' in symbol['name']:
                            if 'fortify' not in metadata['security']:
                                for fortify_name in FORTIFY_NAMES:
                                    if symbol['name'].endswith(fortify_name):
                                        metadata['security'].append('fortify')
                                        break

            elif header.type == elf.Elf.ShType.progbits:
                # process the various progbits sections here
                if header.name == '.comment':
                    # comment, typically in binaries that have
                    # not been stripped.
                    #
                    # The "strings" flag *should* be set for this section
                    try:
                        comment = list(filter(lambda x: x != b'', header.body.split(b'\x00')))[0].decode()
                        metadata['comment'] = comment
                    except:
                        pass
                elif header.name == '.gcc_except_table':
                    # debug information from GCC
                    pass
                elif header.name == '.gnu_debuglink':
                    # https://sourceware.org/gdb/onlinedocs/gdb/Separate-Debug-Files.html
                    link_name = header.body.split(b'\x00', 1)[0].decode()
                    link_crc = int.from_bytes(header.body[-4:], byteorder=metadata['endian'])
                    metadata['gnu debuglink'] = link_name
                    metadata['gnu debuglink crc'] = link_crc
                elif header.name in RODATA_SECTIONS:
                    for s in header.body.split(b'\x00'):
                        try:
                            decoded_strings = s.decode().splitlines()
                            for decoded_string in decoded_strings:
                                for rc in REMOVE_CHARACTERS:
                                    if rc in decoded_string:
                                        decoded_string = decoded_string.translate(REMOVE_CHARACTERS_TABLE)

                                if len(decoded_string) < string_cutoff_length:
                                    continue
                                if decoded_string.isspace():
                                    continue

                                translated_string = decoded_string.translate(STRING_TRANSLATION_TABLE)
                                if decoded_string.isascii():
                                    # test the translated string
                                    if translated_string.isprintable():
                                        data_strings.append(decoded_string)
                                else:
                                    data_strings.append(decoded_string)
                        except:
                            pass
                    # some Qt binaries use the Qt resource system,
                    # containing images, text, etc.
                    # Sometimes these end up in an ELF section.
                    if b'qrc:/' in header.body:
                        pass
                elif header.name == '.gopclntab':
                    # https://medium.com/walmartglobaltech/de-ofuscating-golang-functions-93f610f4fb76
                    pass
                elif header.name == '.gosymtab':
                    # Go symbol table
                    pass
                elif header.name.startswith('.gresource.'):
                    # GNOME/glib GVariant database
                    pass
                elif header.name == '.interp':
                    # store the location of the dynamic linker
                    metadata['linker'] = header.body.split(b'\x00', 1)[0].decode()
                elif header.name == '.itablink':
                    # Go
                    pass
                elif header.name == '.noptrdata':
                    # Go pointer free data
                    pass
                elif header.name == '.qml_compile_hash':
                    pass
                elif header.name == '.qtmetadata':
                    pass
                elif header.name == '.qtversion':
                    pass
                elif header.name == '.tm_clone_table':
                    # something related to transactional memory
                    # http://gcc.gnu.org/wiki/TransactionalMemory
                    pass
                elif header.name == '.typelink':
                    # Go
                    pass
                elif header.name == '.VTGData':
                    # VirtualBox tracepoint generated data
                    # https://www.virtualbox.org/browser/vbox/trunk/include/VBox/VBoxTpG.h
                    pass
                elif header.name == '.VTGPrLc':
                    pass
                elif header.name == '.rol4re_elf_aux':
                    labels.append('l4')
                elif header.name == '.sbat':
                    # systemd, example linuxx64.elf.stub
                    pass
                elif header.name == '.sdmagic':
                    # systemd, example linuxx64.elf.stub
                    pass
                elif header.name == 'sw_isr_table':
                    # Zephyr
                    labels.append('zephyr')

            if header.type == elf.Elf.ShType.dynamic:
                is_dynamic_elf = True
                for entry in header.body.entries:
                    pass
            elif header.type == elf.Elf.ShType.strtab:
                if header.name in GUILE_STRTAB_SECTIONS:
                    for entry in header.body.entries:
                        pass
                else:
                    for entry in header.body.entries:
                        pass
            elif header.type == elf.Elf.ShType.dynsym:
                for entry in header.body.entries:
                    pass
            elif header.type == elf.Elf.ShType.note:
                if header.name == '.note.go.buildid':
                    labels.append('go')

                # Although not common notes sections can be merged
                # with eachother. Example: .notes in Linux kernel images
                for entry in header.body.entries:
                    notes.append((entry.name.decode(), entry.type))
                    if entry.name == b'GNU' and entry.type == 1:
                        # https://raw.githubusercontent.com/wiki/hjl-tools/linux-abi/linux-abi-draft.pdf
                        # normally in .note.ABI.tag
                        major_version = int.from_bytes(entry.descriptor[4:8],
                                                       byteorder=metadata['endian'])
                        patchlevel = int.from_bytes(entry.descriptor[8:12],
                                                    byteorder=metadata['endian'])
                        sublevel = int.from_bytes(entry.descriptor[12:],
                                                  byteorder=metadata['endian'])
                        metadata['linux_version'] = (major_version, patchlevel, sublevel)
                    elif entry.name == b'GNU' and entry.type == 3:
                        # normally in .note.gnu.build-id
                        # https://access.redhat.com/documentation/en-us/red_hat_enterprise_linux/6/html/developer_guide/compiling-build-id
                        buildid = binascii.hexlify(entry.descriptor).decode()
                        metadata['build-id'] = buildid
                        if len(buildid) == 40:
                            metadata['build-id hash'] = 'sha1'
                        elif len(buildid) == 32:
                            metadata['build-id hash'] = 'md5'
                    elif entry.name == b'GNU' and entry.type == 4:
                        # normally in .note.gnu.gold-version
                        metadata['gold-version'] = entry.descriptor.split(b'\x00', 1)[0].decode()
                    elif entry.name == b'GNU' and entry.type == 5:
                        # normally in .note.gnu.property
                        pass
                    elif entry.name == b'Go' and entry.type == 4:
                        # normally in .note.go.buildid
                        # there are four hashes concatenated
                        # https://golang.org/pkg/cmd/internal/buildid/#FindAndHash
                        # http://web.archive.org/web/20210113145647/https://utcc.utoronto.ca/~cks/space/blog/programming/GoBinaryStructureNotes
                        pass
                    elif entry.name == b'Crashpad' and entry.type == 0x4f464e49:
                        # https://chromium.googlesource.com/crashpad/crashpad/+/refs/heads/master/util/misc/elf_note_types.h
                        pass
                    elif entry.name == b'stapsdt' and entry.type == 3:
                        # SystemTap probe descriptors
                        labels.append('SystemTap')
                    elif entry.name == b'Linux':
                        # .note.Linux as seen in some Linux kernel modules
                        labels.append('linux kernel')
                        if entry.type == 0x100:
                            # LINUX_ELFNOTE_BUILD_SALT
                            # see BUILD_SALT in init/Kconfig
                            try:
                                linux_kernel_module_info['kernel build id salt'] = entry.descriptor.decode()
                            except:
                                pass
                        elif entry.type == 0x101:
                            # LINUX_ELFNOTE_LTO_INFO
                            pass
                    elif entry.name == b'FDO' and entry.type == 0xcafe1a7e:
                        # https://fedoraproject.org/wiki/Changes/Package_information_on_ELF_objects
                        # https://systemd.io/COREDUMP_PACKAGE_METADATA/
                        # extract JSON and store it
                        try:
                            metadata['package note'] = json.loads(entry.descriptor.decode().split('\x00')[0].strip())
                        except:
                            pass
                    elif entry.name == b'FreeBSD':
                        labels.append('freebsd')
                    elif entry.name == b'OpenBSD':
                        labels.append('openbsd')
                    elif entry.name == b'NetBSD':
                        # https://www.netbsd.org/docs/kernel/elf-notes.html
                        labels.append('netbsd')
                    elif entry.name == b'Android' and entry.type == 1:
                        # https://android.googlesource.com/platform/ndk/+/master/parse_elfnote.py
                        labels.append('android')
                        metadata['android ndk'] = int.from_bytes(entry.descriptor, byteorder='little')
                    elif entry.name == b'Xen':
                        # http://xenbits.xen.org/gitweb/?p=xen.git;a=blob;f=xen/include/public/elfnote.h;h=181cbc4ec71c4af298e40c3604daff7d3b48d52f;hb=HEAD
                        # .note.Xen in FreeBSD kernel
                        # .notes in Linux kernel)
                        labels.append('xen')
                    elif entry.name == b'NaCl':
                        labels.append('Google Native Client')

        metadata['dynamic_symbols'] = dynamic_symbols
        metadata['guile_symbols'] = guile_symbols
        metadata['needed'] = needed
        metadata['notes'] = notes
        metadata['rpath'] = rpath
        metadata['runpath'] = runpath
        metadata['security'].sort()
        metadata['soname'] = self.soname
        metadata['strings'] = data_strings
        metadata['symbols'] = symbols
        metadata['telfhash'] = ''
        metadata['section_information'] = section_information

        if linux_kernel_module_info != {}:
            metadata['linux_kernel_module'] = linux_kernel_module_info

        if metadata['type'] in ['executable', 'shared']:
            try:
                telfhash_result = telfhash.telfhash(str(to_meta_directory.file_path))
                if telfhash_result != []:
                    telfhash_res = telfhash_result[0]['telfhash'].upper()
                    if telfhash_res != 'TNULL' and telfhash_res != '-':
                        metadata['telfhash'] = telfhash_res
            except UnicodeEncodeError:
                pass

        if is_dynamic_elf:
            labels.append('dynamic')
        else:
            if metadata['type'] == 'core':
                labels.append('core')
            else:
                labels.append('static')
        return(labels, metadata)
