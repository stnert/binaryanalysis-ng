meta:
  id: jffs2
  title: JFFS2
  tags:
    - file system
    - linux
  license: CC0-1.0
  ks-version: 0.9
seq:
  - id: magic
    type: u2be
    enum: endian
    valid:
      any-of:
        - endian::be
        - endian::le
  - id: header
    type: inode_header
  - id: inode_data
    type:
      switch-on: header.inode_type
      cases:
        'inode_type::dirent': dirent
        'inode_type::inode': inode
    size: header.len_inode - header._sizeof - magic._sizeof
types:
  inode_header:
    meta:
      endian:
        switch-on: _root.magic
        cases:
          'endian::le': le
          'endian::be': be
    seq:
      - id: inode_type
        type: u2
        enum: inode_type
        valid:
          any-of:
            - inode_type::dirent
            - inode_type::inode
            - inode_type::clean_marker
            - inode_type::padding
            - inode_type::summary
            - inode_type::xattr
            - inode_type::xref
      - id: len_inode
        type: u4
  dirent:
    meta:
      endian:
        switch-on: _root.magic
        cases:
          'endian::le': le
          'endian::be': be
    seq:
      - id: header_crc
        type: u4
      - id: parent_inode
        type: u4
      - id: inode_version
        type: u4
      - id: inode_number
        type: u4
      - id: mctime
        type: u4
      - id: len_name
        type: u1
      - id: dirent_type
        type: u1
      - id: unused
        size: 2
      - id: node_crc
        type: u4
      - id: name_crc
        type: u4
      - id: name
        size: len_name
  inode:
    meta:
      endian:
        switch-on: _root.magic
        cases:
          'endian::le': le
          'endian::be': be
    seq:
      - id: header_crc
        type: u4
      - id: inode_numbers
        type: u4
      - id: inode_version
        type: u4
      - id: file_mode
        type: u4
enums:
  endian:
    0x8519: le
    0x1985: be
  inode_type:
    0xe001: dirent
    0xe002: inode
    0x2003: clean_marker
    0x2004: padding
    0x2006: summary
    0xe008: xattr
    0xe009: xref
