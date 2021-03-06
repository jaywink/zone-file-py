"""
Known limitations:
    * only one $ORIGIN and one $TTL are supported
    * only the IN class is supported
    * PTR records must have a non-empty name
    * currently only supports the following:
    '$ORIGIN', '$TTL', 'SOA', 'NS', 'A', 'AAAA', 'CNAME', 'MX', 'PTR',
    'TXT', 'SRV', 'SPF', 'URI'
"""

import argparse
from collections import defaultdict

from blockstack_zones.configs import SUPPORTED_RECORDS
from blockstack_zones.exceptions import InvalidLineException


class ZonefileLineParser(argparse.ArgumentParser):
    def error(self, message):
        """
        Silent error message
        """
        raise InvalidLineException(message)


def make_rr_subparser(subparsers, rec_type, args_and_types):
    """
    Make a subparser for a given type of DNS record
    """
    sp = subparsers.add_parser(rec_type)

    sp.add_argument("name", type=str)
    sp.add_argument("ttl", type=int, nargs='?')
    sp.add_argument(rec_type, type=str)

    for my_spec in args_and_types:
        (argname, argtype) = my_spec[:2]
        if len(my_spec) > 2:
            nargs = my_spec[2]
            sp.add_argument(argname, type=argtype, nargs=nargs)
        else:
            sp.add_argument(argname, type=argtype)
    return sp


def make_txt_subparser(subparsers):
    sp = subparsers.add_parser("TXT")

    sp.add_argument("name", type=str)
    sp.add_argument("--ttl", type=int)
    sp.add_argument("TXT", type=str)
    sp.add_argument("txt", type=str, nargs='+')
    return sp


def make_parser():
    """
    Make an ArgumentParser that accepts DNS RRs
    """
    line_parser = ZonefileLineParser()
    subparsers = line_parser.add_subparsers()

    # parse $ORIGIN
    sp = subparsers.add_parser("$ORIGIN")
    sp.add_argument("$ORIGIN", type=str)

    # parse $TTL
    sp = subparsers.add_parser("$TTL")
    sp.add_argument("$TTL", type=int)

    # parse each RR
    args_and_types = [
        ("mname", str), ("rname", str), ("serial", int), ("refresh", int),
        ("retry", int), ("expire", int), ("minimum", int)
    ]
    make_rr_subparser(subparsers, "SOA", args_and_types)

    make_rr_subparser(subparsers, "NS", [("host", str)])
    make_rr_subparser(subparsers, "A", [("ip", str)])
    make_rr_subparser(subparsers, "AAAA", [("ip", str)])
    make_rr_subparser(subparsers, "CNAME", [("alias", str)])
    make_rr_subparser(subparsers, "ALIAS", [("host", str)])
    make_rr_subparser(subparsers, "MX", [("preference", str), ("host", str)])
    make_txt_subparser(subparsers)
    make_rr_subparser(subparsers, "PTR", [("host", str)])
    make_rr_subparser(subparsers, "SRV", [("priority", int), ("weight", int), ("port", int), ("target", str)])
    make_rr_subparser(subparsers, "SPF", [("data", str)])
    make_rr_subparser(subparsers, "URI", [("priority", int), ("weight", int), ("target", str)])

    return line_parser


def tokenize_line(line):
    """
    Tokenize a line:
    * split tokens on whitespace
    * treat quoted strings as a single token
    * drop comments
    * handle escaped spaces and comment delimiters
    """
    ret = []
    escape = False
    quote = False
    tokbuf = ""
    ll = list(line)
    while len(ll) > 0:
        c = ll.pop(0)
        if c.isspace():
            if not quote and not escape:
                # end of token
                if len(tokbuf) > 0:
                    ret.append(tokbuf)

                tokbuf = ""
            elif quote:
                # in quotes
                tokbuf += c
            elif escape:
                # escaped space
                tokbuf += c
                escape = False
            else:
                tokbuf = ""

            continue

        if c == '\\':
            escape = True
            continue
        elif c == '"':
            if not escape:
                if quote:
                    # end of quote
                    ret.append(tokbuf)
                    tokbuf = ""
                    quote = False
                    continue
                else:
                    # beginning of quote
                    quote = True
                    continue
        elif c == ';':
            # if there is a ";" in a quoted string it should be treated as valid.
            # DMARC records as an example.
            if not escape and quote is False:
                # comment
                ret.append(tokbuf)
                tokbuf = ""
                break

        # normal character
        tokbuf += c
        escape = False

    if len(tokbuf.strip(" ").strip("\n")) > 0:
        ret.append(tokbuf)

    return ret


def serialize(tokens):
    """
    Serialize tokens:
    * quote whitespace-containing tokens
    * escape semicolons
    """
    ret = []
    for tok in tokens:
        if " " in tok:
            tok = '"%s"' % tok

        if ";" in tok:
            tok = tok.replace(";", "\;")

        ret.append(tok)

    return " ".join(ret)


def remove_comments(text):
    """
    Remove comments from a zonefile
    """
    ret = []
    lines = text.split("\n")
    for line in lines:
        if len(line) == 0:
            continue

        line = serialize(tokenize_line(line))
        ret.append(line)

    return "\n".join(ret)


def flatten(text):
    """
    Flatten the text:
    * make sure each record is on one line.
    * remove parenthesis
    """
    lines = text.split("\n")

    # tokens: sequence of non-whitespace separated by '' where a newline was
    tokens = []
    for l in lines:
        if len(l) == 0:
            continue

        l = l.replace("\t", " ")
        tokens += [x for x in l.split(" ") if len(x) > 0] + ['']

    # find (...) and turn it into a single line ("capture" it)
    capturing = False
    captured = []

    flattened = []
    while len(tokens) > 0:
        tok = tokens.pop(0)
        if not capturing and len(tok) == 0:
            # normal end-of-line
            if len(captured) > 0:
                flattened.append(" ".join(captured))
                captured = []
            continue

        if tok.startswith("("):
            # begin grouping
            tok = tok.lstrip("(")
            capturing = True

        if capturing and tok.endswith(")"):
            # end grouping.  next end-of-line will turn this sequence into a flat line
            tok = tok.rstrip(")")
            capturing = False

        captured.append(tok)

    return "\n".join(flattened)


def remove_class(text):
    """
    Remove the CLASS from each DNS record, if present.
    The only class that gets used today (for all intents
    and purposes) is 'IN'.
    """

    # see RFC 1035 for list of classes
    lines = text.split("\n")
    ret = []
    for line in lines:
        tokens = tokenize_line(line)
        tokens_upper = [t.upper() for t in tokens]

        if "IN" in tokens_upper:
            tokens.remove("IN")
        elif "CS" in tokens_upper:
            tokens.remove("CS")
        elif "CH" in tokens_upper:
            tokens.remove("CH")
        elif "HS" in tokens_upper:
            tokens.remove("HS")

        ret.append(serialize(tokens))

    return "\n".join(ret)


def add_default_name(text):
    """
    Go through each line of the text and ensure that
    a name is defined.  Use '@' if there is none.
    """
    lines = text.split("\n")
    ret = []
    for line in lines:
        tokens = tokenize_line(line)
        if len(tokens) == 0:
            continue

        if tokens[0] in SUPPORTED_RECORDS and not tokens[0].startswith("$"):
            # add back the name
            tokens = ['@'] + tokens

        ret.append(serialize(tokens))

    return "\n".join(ret)


def parse_line(parser, record_token, parsed_records):
    """
    Given the parser, capitalized list of a line's tokens, and the current set of records
    parsed so far, parse it into a dictionary.

    Return the new set of parsed records.
    Raise an exception on error.
    """

    line = " ".join(record_token)

    # match parser to record type
    if len(record_token) >= 2 and record_token[1] in SUPPORTED_RECORDS:
        # with no ttl
        record_token = [record_token[1]] + record_token
    elif len(record_token) >= 3 and record_token[2] in SUPPORTED_RECORDS:
        # with ttl
        record_token = [record_token[2]] + record_token
        if record_token[0] == "TXT":
            record_token = record_token[:2] + ["--ttl"] + record_token[2:]
    try:
        rr, unmatched = parser.parse_known_args(record_token)
        assert len(unmatched) == 0, "Unmatched fields: %s" % unmatched
    except (SystemExit, AssertionError, InvalidLineException):
        # invalid argument
        raise InvalidLineException(line)

    record_dict = rr.__dict__
    if record_token[0] == "TXT" and len(record_dict['txt']) == 1:
        record_dict['txt'] = record_dict['txt'][0]

    # what kind of record? including origin and ttl
    record_type = None
    for key in list(record_dict.keys()):
        if key in SUPPORTED_RECORDS and (key.startswith("$") or record_dict[key] == key):
            record_type = key
            if record_dict[key] == key:
                del record_dict[key]
            break

    assert record_type is not None, "Unknown record type in %s" % rr

    # clean fields
    for field in list(record_dict.keys()):
        if record_dict[field] is None:
            del record_dict[field]

    current_origin = record_dict.get('$ORIGIN', parsed_records.get('$ORIGIN', None))

    # special record-specific fix-ups
    if record_type == 'PTR':
        record_dict['fullname'] = record_dict['name'] + '.' + current_origin

    if len(record_dict) > 0:
        if record_type.startswith("$"):
            # put the value directly
            record_dict_key = record_type.lower()
            parsed_records[record_dict_key] = record_dict[record_type]
        else:
            record_dict_key = record_type.lower()
            parsed_records[record_dict_key].append(record_dict)

    return parsed_records


def parse_lines(text, ignore_invalid=False):
    """
    Parse a zonefile into a dict.
    @text must be flattened--each record must be on one line.
    Also, all comments must be removed.
    """
    json_zone_file = defaultdict(list)
    record_lines = text.split("\n")
    parser = make_parser()

    for record_line in record_lines:
        record_token = tokenize_line(record_line)
        try:
            json_zone_file = parse_line(parser, record_token, json_zone_file)
        except InvalidLineException:
            if ignore_invalid:
                continue
            else:
                raise

    return json_zone_file


def parse_zone_file(text, ignore_invalid=False):
    """
    Parse a zonefile into a dict
    """
    text = remove_comments(text)
    text = flatten(text)
    text = remove_class(text)
    text = add_default_name(text)
    json_zone_file = parse_lines(text, ignore_invalid=ignore_invalid)
    return json_zone_file
