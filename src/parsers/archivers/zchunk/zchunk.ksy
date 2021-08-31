meta:
  id: zchunk
  title: Zchunk
  file-extension: zck
  license: CC0-1.0
  endian: le
doc-ref: https://github.com/zchunk/zchunk/blob/main/zchunk_format.txt
seq:
  - id: lead
    type: lead
  - id: rest_of_header
    type: rest_of_header
    size: lead.len_header.value
types:
  lead:
    seq:
      - id: magic
        contents: [0, 'ZCK1']
      - id: checksum
        type: compressed_integer
      - id: len_header
        type: compressed_integer
      - id: lead_checksum
        size: len_checksum
    instances:
      checksum_type:
        value: checksum.value
        enum: checksum_types
      len_checksum:
        value: |
            checksum_type == checksum_types::sha1 ? 20 :
            checksum_type == checksum_types::sha256 ? 32 :
            checksum_type == checksum_types::sha512 ? 64 :
            checksum_type == checksum_types::sha512_128 ? 16 :
            0
  rest_of_header:
    seq:
      - id: preface
        type: preface
      - id: index
        type: index
      - id: signatures
        type: signatures
  preface:
    seq:
      - id: checksum
        size: _root.lead.len_checksum
      - id: flags
        type: compressed_integer
      - id: compression_type
        type: compressed_integer
      - id: num_optional_element
        type: compressed_integer
        if: flags.value == 1
      - id: optional_elements
        type: optional_element
        repeat: expr
        repeat-expr: num_optional_element.value
        if: flags.value == 1
  optional_element:
    seq:
      - id: element_id
        type: compressed_integer
      - id: len_data
        type: compressed_integer
      - id: data
        size: len_data.value
  index:
    seq:
      - id: len_index
        type: compressed_integer
      - id: rest_of_index
        size: len_index.value
        type: rest_of_index
  rest_of_index:
    seq:
      - id: checksum
        type: compressed_integer
      - id: num_chunks
        type: compressed_integer
      - id: dict_stream
        type: stream(len_checksum)
        if: _root.rest_of_header.preface.flags.value == 1
      - id: chunk_stream
        type: stream(len_checksum)
        if: _root.rest_of_header.preface.flags.value == 1
    instances:
      checksum_type:
        value: checksum.value
        enum: checksum_types
      len_checksum:
        value: |
            checksum_type == checksum_types::sha1 ? 20 :
            checksum_type == checksum_types::sha256 ? 32 :
            checksum_type == checksum_types::sha512 ? 64 :
            checksum_type == checksum_types::sha512_128 ? 16 :
            0
  stream:
    params:
      - id: len_checksum
        type: u4
    seq:
      - id: stream
        type: compressed_integer
      - id: checksum
        size: len_checksum
      - id: len_stream
        type: compressed_integer
      - id: len_uncompressed
        type: compressed_integer
  signatures:
    seq:
      - id: num_signatures
        type: compressed_integer
      - id: signatures
        type: signature
        repeat: expr
        repeat-expr: num_signatures.value

  signature:
    seq:
      - id: signature_type
        type: compressed_integer
      - id: len_signature
        type: compressed_integer
      - id: signature_data
        size: len_signature.value
  compressed_integer:
    seq:
      - id: groups
        type: group
        repeat: until
        repeat-until: not _.has_next
    types:
      group:
        doc: |
          One byte group, clearly divided into 7-bit "value" chunk and 1-bit "continuation" flag.
        seq:
          - id: b
            type: u1
        instances:
          has_next:
            value: (b & 0b1000_0000) == 0
            doc: If true, then we have more bytes to read
          value:
            value: b & 0b0111_1111
            doc: The 7-bit (base128) numeric value chunk of this group
    instances:
      len:
        value: groups.size
      value:
        value: >-
          groups[0].value
          + (len >= 2 ? (groups[1].value << 7) : 0)
          + (len >= 3 ? (groups[2].value << 14) : 0)
          + (len >= 4 ? (groups[3].value << 21) : 0)
          + (len >= 5 ? (groups[4].value << 28) : 0)
          + (len >= 6 ? (groups[5].value << 35) : 0)
          + (len >= 7 ? (groups[6].value << 42) : 0)
          + (len >= 8 ? (groups[7].value << 49) : 0)
        doc: Resulting value as normal integer

enums:
  checksum_types:
    0: sha1
    1: sha256
    2: sha512
    3: sha512_128 # first 128 bits of sha512 checksum
