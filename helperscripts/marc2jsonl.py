#!/usr/bin/python3

import sys
import json

from es2json import ArrayOrSingleValue, eprint,litter,isint
from pymarc import MARCReader
from six.moves import zip_longest as izip_longest

def transpose_to_ldj(record):
    json_record={}
    json_record['_LEADER'] = record.leader
    json_record['_FORMAT'] = "MarcXchange"
    json_record['_TYPE'] = "Bibliographic"
    for field in record:
        if isint(field.tag):
            if field.is_control_field():
                json_record[field.tag]=[field.data]
            else:
                ind="".join(field.indicators).replace(" ","_")
                ind_obj=[]
                for k,v in izip_longest(*[iter(field.subfields)] * 2):
                    if "." in ind:
                        ind = ind.replace(".","_")
                    if "." in k or k.isspace():
                        k="_"
                    ind_obj.append({k:v})
                if not field.tag in json_record:
                    json_record[field.tag]=[]
                json_record[field.tag].append({ind:ind_obj})
    return json_record

def main():
    try:
        for record in MARCReader(sys.stdin.buffer.read(), to_unicode=True):
            sys.stdout.write(json.dumps(transpose_to_ldj(record))+"\n")
            sys.stdout.flush()
    except UnicodeDecodeError as e:
        eprint("unicode decode error: {}".format(e))
        eprint(record)

if __name__ == "__main__":
    main()
