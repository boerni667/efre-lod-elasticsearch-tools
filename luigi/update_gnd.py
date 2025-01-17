import json
import sys
from datetime import datetime
from dateutil import parser
from requests import get,head
import os
import shutil
import gzip
import subprocess
import argparse

import elasticsearch
from multiprocessing import Pool,current_process
from pyld import jsonld
from es2json import eprint, put_dict
import ijson.backends.yajl2_cffi as ijson


import luigi
import luigi.contrib.esindex
from gluish.task import BaseTask,ClosestDateParameter
from gluish.utils import shellout



def init_mp(c,rf,url,pr,bn):
    global context
    global context_url
    global record_field
    global pathprefix
    global node
    pathprefix=pr
    context=c
    if bn:
        node=True
    else:
        node=False
    if url:
        context_url=url
    else:
        context_url=None
    if rf:
        record_field=rf
    else:
        record_field=None

    
def compact_object(jsonobject):
    dnb_split=True
    if isinstance(jsonobject,list) and len(jsonobject)==1:
        jsonobject=jsonobject[0]
    if isinstance(jsonobject, dict):
        if (record_field and record_field in jsonobject) or (record_field is None):
            compacted = jsonld.compact(jsonobject, context,  {'skipExpansion': True})
            if context_url:
                compacted['@context'] = context_url#
            for date in ["definition"]:
                if isinstance(compacted.get(date),str):
                    compacted.pop(date)
                if isinstance(compacted.get("gndIdentifier"),list):
                    compacted["gndIdentifier"]=compacted.pop("gndIdentifier")[0]
            #for fix in ["definition"]:
            #    if isinstance(compacted.get("fix"),(dict,list)):
            #        compacted.pop(fix)
            if (node and compacted.get("@id") and compacted.get("@id").startswith("_:")) or (node and compacted.get("id") and compacted.get("id").startswith("_:")):
                with gzip.open(pathprefix+str(current_process().name)+"-bnodes.ldj.gz","at") as fileout:           ###avoid raceconditions
                    fileout.write(json.dumps(compacted, indent=None)+"\n")
            else:
                with gzip.open(pathprefix+str(current_process().name)+".ldj.gz","at") as fileout:
                    _id=compacted.pop("id")
                    compacted["id"]=_id.split("/")[-1]
                    fileout.write(json.dumps(compacted, indent=None)+"\n")
            
def yield_obj(path,basepath):
    with gzip.open(path,"r") as fin:
        builder=ijson.common.ObjectBuilder()
        for prefix,event,val in ijson.parse(fin):
            try:
                builder.event(event,val)
            except:
                if hasattr(builder,"value"):
                    print(builder.value)
            if prefix==basepath and event=="end_map":
                if hasattr(builder,"value"):
                    yield builder.value
                builder=ijson.common.ObjectBuilder()



#put this into a function to able to use jsonld2compactjsonldldj as a lib
def process(inputs,record_field,context_url,pathprefix,bnode,worker):
    r=get(context_url)
    if r.ok:
        jsonldcontext=r.json()
        sys.stderr.write("got context from "+context_url+"\n")
    else:
        sys.stderr.write("unable to get context from {}. aborting\n".format(context_url))
        exit(-1)
    
    pool = Pool(worker,initializer=init_mp,initargs=(jsonldcontext,record_field,context_url,pathprefix,bnode,))
    #init_mp(jsonldcontext,record_field,context_url,pathprefix,bnode)
    #item.item = go down 2 (array-)levels as in jsonld-1.1 spec
    for url in inputs:
        input=url.split("/")[-1]
        for obj in yield_obj(input,"item.item"):
            #compact_object(obj)
            pool.apply_async(compact_object,(obj,))
    pool.close()
    pool.join()

class GNDTask(BaseTask):
    """
    Just a base class for GND 
    """
    TAG = 'gnd'

    with open('gnd_config.json') as data_file:    
        config = json.load(data_file)

    def closest(self):
        return daily(date=self.date)

class GNDDownload(GNDTask):
    files=[]
    def run(self):
        for url in self.config.get("urls"):
            fd=url.split("/")[-1]
            cmdstring="wget --user {username} --password {password} -O - {url} | gunzip -c | uconv -x any-nfc | gzip > {file} ".format(**self.config,url=url,file=fd)
            self.files.append(luigi.LocalTarget(fd))
            output = shellout(cmdstring)
        return 0

    def complete(self):
        for url in self.config["urls"]:
            fd=url.split("/")[-1]
            r=head(url,auth=(self.config["username"],self.config["username"]))
            remote=None
            if r.headers.get("Last-Modified"):
                datetime_object=parser.parse(r.headers["Last-Modified"])
                remote=float(datetime_object.timestamp())
            if os.path.isfile(fd):
                statbuf=os.stat(fd)
                here=float(statbuf.st_mtime)
            else:
                return False
            if here<=remote:
                return False
        return True

    def output(self):
        return self.files

class CleanWorkspace(GNDTask):
    
    def complete(self):
        if os.path.exists("chunks") and os.listdir("chunks")==[]:
            return True
        else:
            return False
    def run(self):
        if os.path.exists("chunks"):
            shutil.rmtree("chunks")
        

class GNDcompactedJSONdata(GNDTask):
    
    def requires(self):
        return [GNDDownload()]

    def run(self):
        CleanWorkspace().run()
        os.mkdir("chunks")
        #for url in self.config.get("urls"):
        #    process(url.split("/")[-1],None,self.config.get("context"),"chunks/",True,28)
        process(self.config.get("urls"),None,self.config.get("context"),"chunks/",True,28)

    def output(self):
        return [luigi.LocalTarget("chunks")]



class GNDconcatChunks(GNDTask):
    def requires(self):
        return [ GNDcompactedJSONdata()]
    
    def run(self):
        with gzip.open("records.ldj.gz","wt") as records, gzip.open("bnodes.ldj.gz","wt") as bnodes:
            for f in os.listdir("chunks/"):
                with gzip.open("chunks/" + f,"rt") as chunk:
                    for line in chunk:
                        jline=json.loads(line)
                        for date in ("dateOfBirth","dateOfDeath"):
                            if date in jline and isinstance(jline.get(date),list):
                                for i,item in enumerate(jline[date]):
                                    if isinstance(item,str):
                                        jline[date][i]={"@value":item}
                        if "bnode"in f:                
                            bnodes.write(json.dumps(jline,indent=None)+"\n")
                        else:
                            records.write(json.dumps(jline,indent=None)+"\n")
        
    def output(self):
        return [luigi.LocalTarget("bnodes.ldj.gz"),luigi.LocalTarget("records.ldj.gz")]
    
class GNDUpdate(GNDTask):
    """
    Loads processed GND data into a given ElasticSearch index (with help of esbulk)
    """
    date = datetime.today()
    es = None

    files=None
    def requires(self):
        return GNDconcatChunks()

    def run(self):
        cmd="esbulk -z -verbose -server http://{host}:{port} -w {workers}""".format(**self.config)
        for k,v in self.config.get("indices").items():
            out=shellout("curl -XDELETE http://{host}:{port}/{index}".format(**self.config,index=v))
            put_dict("http://{host}/{index}".format(**self.config,index=v),{"mappings":{k:{"date_detection":False}}})
            out = shellout(cmd+""" -index {index} -type {type} -id id {type}s.ldj.gz""".format(index=v,type=k))
            

    def complete(self):
        self.es=elasticsearch.Elasticsearch([{'host':self.config.get("host")}],port=self.config.get("port"))
        fail=0
        for k,v in self.config.get("indices").items():
            cmd="http://{host}:{port}/{index}/{type}/_search?size=0".format(**self.config,type=k,index=v)
            i=0
            r = get(cmd)
            try:
                with gzip.open(str(k)+"s.ldj.gz","rt") as f:
                    for i,l in enumerate(f):
                        pass
            except FileNotFoundError:
                fail+=1
                i=-100
            i+=1
            if r.ok:
                if i!=r.json().get("hits").get("total"):
                    fail+=1
            else:
                fail+=1
        if fail==0:
            return True
        else:
            return False

