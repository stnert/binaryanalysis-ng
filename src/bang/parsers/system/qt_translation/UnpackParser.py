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

from bang.UnpackParser import UnpackParser, check_condition
from bang.UnpackParserException import UnpackParserException
from kaitaistruct import ValidationFailedError
from . import qt_translation


class QtTranslationUnpackParser(UnpackParser):
    extensions = []
    signatures = [
        (0, b'\x3c\xb8\x64\x18\xca\xef\x9c\x95\xcd\x21\x1c\xbf\x60\xa1\xbd\xdd')
    ]
    pretty_name = 'qt_translation'

    def parse(self):
        try:
            self.data = qt_translation.QtTranslation.from_io(self.infile)
        except (Exception, ValidationFailedError) as e:
            raise UnpackParserException(e.args)

        for t in self.data.tags:
            if t.tag == qt_translation.QtTranslation.TranslatorTags.messages:
                for m in t.data.messages:
                    if m.tag == qt_translation.QtTranslation.TagTypes.translation:
                        try:
                            m.payload.data.decode('utf-16be')
                        except UnicodeDecodeError as e:
                            raise UnpackParserException(e.args)

    labels = ['qt', 'translation', 'resource']
    metadata = {}
