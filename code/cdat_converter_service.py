#***************************************************
#** ViSUS Visualization Project                    **
#** Copyright (c) 2010 University of Utah          **
#** Scientific Computing and Imaging Institute     **
#** 72 S Central Campus Drive, Room 3750           **
#** Salt Lake City, UT 84112                       **
#**                                                **
#** For information about this project see:        **
#** http:#www.pascucci.org/visus/                 **
#**                                                **
#**      or contact: pascucci@sci.utah.edu         **
#**                                                **
#****************************************************
#
#
# cdat_converter.py
#
# 1) Listens for http requests to convert a box of a resolution of a timestep of a field of a dataset.
# 2) Ensures there are not already conversions in progress.
# 3) Converts data.
# 4) Signals caller upon completion.
#
#****************************************************

import time
import sqlite3
import visuspy as Visus
import SocketServer
import BaseHTTPServer
import fcntl
import cdms2
import urlparse
import os
import socket
from sys import stdout
from shutil import rmtree
import cdat_to_idx
#import convert_query


RESULT_SUCCESS=200; RESULT_INVALID=400; RESULT_NOTFOUND=404; RESULT_ERROR=500; RESULT_BUSY=503

class cdatConverter(BaseHTTPServer.BaseHTTPRequestHandler):
    """http request handler for cdat to idx conversion requests"""

    nqueries_=0

    def do_GET(self):
        url=urlparse.urlparse(self.path)
        if url.path=='/convert':
            query_id=cdatConverter.nqueries_; cdatConverter.nqueries_+=1
            t1=time.time()
            print "("+str(query_id)+")",url.query
            stdout.flush()
            result,response=convert_query(url.query)
            print "("+str(query_id)+") complete ("+str((time.time()-t1)*1000)+"ms): ["+str(response)+"] "+result
            stdout.flush()
            self.send_response(response)
            self.send_header('Content-type','text/html')
            self.end_headers()
            self.wfile.write("<html><head></head><body>"+result+"</body></html>")
            return
        if url.path=='/create':
            print "request received: "+url.query
            result,response=create(url.query)
            self.send_response(response)
            if response != RESULT_SUCCESS:
                self.send_header('Content-type','text/html')
            else:
                self.send_header('Content-type','application/xml; charset=utf-8')
            self.end_headers()
            if response != RESULT_SUCCESS:
                self.wfile.write("<html><head></head><body>"+result+"</body></html>")
            else:
                self.wfile.write(result)
            stdout.flush()
            return
        if url.path=='/clear':
            result,response=clear_cache()
            self.send_response(response)
            self.send_header('Content-type','text/html')
            self.end_headers()
            self.wfile.write("<html><head></head><body>"+result+"</body></html>")
            stdout.flush()
            return


def clear_cache():
    """clear a specific cache directory. Be careful with this!!!"""
    result="sample cache cleared."
    try:
        rmtree("/for_ganzberger1/idx/idx/acme-test.ORNL.HIGHRES.init.all.v1-lon_lat_ilev_time")
        rmtree("/for_ganzberger1/idx/idx/acme-test.ORNL.HIGHRES.init.all.v1-lon_lat_lev_time")
        rmtree("/for_ganzberger1/idx/idx/acme-test.ORNL.HIGHRES.init.all.v1-lon_lat_time")
    except OSError as e:
        result="OSError: "+str(e)
    return result,RESULT_SUCCESS


def lookup_cdat_path(idxpath):
    """lookup dataset corresponding to idxpath"""

    db=sqlite3.connect(dbpath)
    cur=db.cursor()
    cur.execute("SELECT ds_id from idxfiles where pathname=\"%s\"" % idxpath)
    cdatpath=cur.fetchall()
    assert(len(cdatpath)<=1)
    if len(cdatpath)>0:
        cur.execute("SELECT pathname from datasets where ds_id=%d" % cdatpath[0])
        cdatpath=cur.fetchone()[0]

        # <warning>
        # nasty hack to work around bug in cdms2 when using opendap:
        # solution is to run converter service from xml directory and
        # to load xml files from local paths, not explicit paths
        # (e.g. "filename.xml", not "/path/to/filename.xml".
        cdatpath=os.path.basename(cdatpath)

        return cdatpath,True
    return "",False


def parse_query(query):
    """parse the cdat to idx conversion query string"""

    job=urlparse.parse_qs(query)
    idxpath=None
    field=None
    timestep=0
    box=None
    hz=-1
    if job.has_key("idx"):
        idxpath=job["idx"][0]
    if job.has_key("time"):
        timestep=int(job["time"][0])
    if job.has_key("field"):
        field=job["field"][0]
        idx=field.find('?')
        if idx>=0:                  # field time overrides query time
            field=field[:idx]
            arg=field[idx+1:]
            if arg.startswith("time="):
                timestep=int(arg[arg.find('=')+1:])
    if job.has_key("box"):
        box=job["box"][0]
    if job.has_key("hz"):
        hz=int(job["hz"][0])
    return idxpath,field,timestep,box,hz


def read_cdat_data(cdatpath,field,timestep):
    """open and read a field from a cdat dataset"""

    f=cdms2.open(cdatpath)
    if not f.variables.has_key(field):
        raise ConvertError(RESULT_NOTFOUND,"Field %s not found in cdat volume %s."%(field,cdatpath))
    print cdatpath,"opened. Reading field",field,"at timestep",timestep
    v=f.variables[field]

    data=None
    has_time=v.getAxisList()[0].id.startswith("time")
    if has_time:
        if len(v) <= timestep or timestep<0:
            raise ConvertError(RESULT_NOTFOUND,"Timestep %d out of range for field %s."%(timestep,field))
        data=v[timestep]
    else:
        data=v
    print "finished reading field",field,"at timestep",timestep,"of",cdatpath

    #"flatten" masked data by inserting missing_value everywhere mask is invalid
    if isinstance(data,cdms2.tvariable.TransientVariable):
        data=data.filled()

    return data


def get_timesteps(idxpath):
    """open idx, returns num timesteps"""

    global dbpath
    idxpath=os.path.dirname(dbpath)+"/"+idxpath
    dataset=Visus.Dataset.loadDataset(idxpath);
    if not dataset:
        raise ConvertError(RESULT_ERROR,"Error: could not load IDX dataset "+idxpath)
    return dataset.getTimesteps().asVector()

def create_idx_query(idxpath,field,timestep,box,hz):
    """open idx, validate inputs, create write query"""

    global dbpath
    idxpath=os.path.dirname(dbpath)+"/"+idxpath
    dataset=Visus.Dataset.loadDataset(idxpath);
    if not dataset:
        raise ConvertError(RESULT_ERROR,"Error creating IDX query: could not load dataset "+idxpath)
    visus_field=dataset.getFieldByName(field);
    if not visus_field:
        raise ConvertError(RESULT_ERROR,"Error creating IDX query: could not find field "+field)
    access=dataset.createAccess()
    logic_box=dataset.getLogicBox()

    if box or hz>=0:
        pass #print "TODO: handle subregion queries and resolution selection (box=%s,hz=%d)"%(box,hz)

    # convert the field
    query=Visus.Query(dataset,ord('w'))
    query.setLogicPosition(Visus.Position(logic_box))
    query.setField(visus_field)
    query.setTime(timestep)
    query.setAccess(access)
    query.begin()
    return dataset,access,query  # IMPORTANT: need to return dataset,access because otherwise they go out of scope and query fails

class ConvertError(Exception):
    """Exception raised for errors during converstion.

    Attributes:
        ret  -- http return code for the exception
        msg  -- explanation of the error
    """
    def __init__(self, code, msg):
        self.code = code
        self.msg = msg
    def __str__(self):
        return "error "+self.code+": "+self.msg


def convert_query(query):
    """Converts a timestep of a field of a cdat dataset to idx, using the idxpath to find the matching cdat volume."""

    # parse query request
    idxpath,field,timestep,box,hz=parse_query(query)
    if not idxpath or not field:
        return ("Invalid query: %s"%query,RESULT_INVALID)
    return convert(idxpath,field,timestep,box,hz)

def convert(idxpath,field,timestep,box,hz):
    """Converts a timestep of a field of a cdat dataset to idx, using the idxpath to find the matching cdat volume."""

    t1  = time.time()
    pt1 = time.clock()

    # lookup dataset corresponding to idxpath
    cdatpath,success=lookup_cdat_path(idxpath)
    if not success:
        return ("Database does not list associated cdat dataset for %s"%idxpath,RESULT_NOTFOUND)

    # try to read lock file (note: this is unix-only)
    lockfilename="/tmp/"+idxpath+"-"+field+"-"+str(timestep)+".lock" #+"-"+str(box)+"-"+str(hz)+".lock" (for now, regions are ignored)
    lock=None
    result=RESULT_SUCCESS
    result_str="Success!"
    try:
        # get file lock
        lock=os.open(lockfilename,os.O_CREAT|os.O_EXCL)

        #import pdb; pdb.set_trace()

        # open cdat, read the data
        data=read_cdat_data(cdatpath,field,timestep)

        # open idx and create query
        print "creating idx query for field",field,"at time",timestep,"of",cdatpath
        dataset,access,query=create_idx_query(idxpath,field,timestep,box,hz)

        # validate bounds
        if query.end() or data.size!=query.getNumberOfSamples().innerProduct():
            raise ConvertError(RESULT_ERROR,"Invalid IDX query.")
            
        # validate shape
        shape=data.shape[::-1]
        for i in range(len(shape)):
            if shape[i]!=query.getNumberOfSamples()[i]:
                raise ConvertError(RESULT_ERROR,"Invalid query dimensions.")
                
        # convert data
        print "converting field",field,"at time",timestep,"of",cdatpath,"to idx..."
        visusarr=Visus.Array.fromNumPyArray(data)
        visusarrptr=Visus.ArrayPtr(visusarr)
        query.setBuffer(visusarrptr)
        ret=query.execute()
        if not ret:
            raise ConvertError(RESULT_ERROR,"Error executing IDX query.")
        print "done converting field",field,"at time",timestep,"of",cdatpath
            
    except IOError as e:
        if e.errno==None:
            result=RESULT_ERROR
            result_str="Error reading data. Please ensure cdms2 is working and NetCDF data is accessible."
        else:
            result=RESULT_ERROR
            result_str="An unknown i/o error has occured (e.errno="+os.strerror(e.errno)+")"
    except cdms2.CDMSError as e:
        result=RESULT_ERROR
        result_str="CDMSError: %s"%e
    except ConvertError as e:
        result=e.code
        result_str=e.msg
    except MemoryError as e:
        result_str="MemoryError: please try again ("+str(e)+")"
        result=RESULT_ERROR
    except Exception as e:
        if e.errno==17:
            result=RESULT_BUSY
            result_str="Conversion of field",field,"at time",timestep,"in progress. Duplicate request ignored. (e.errno="+os.strerror(e.errno)+")"
        else:
            result=RESULT_ERROR
            result_str="unknown error occurred during convert ("+str(e)+")"
    finally:
        if lock:
            os.close(lock)

    proctime=time.clock()-pt1
    interval=time.time()-t1
    if result==RESULT_SUCCESS:
        print("Total time to convert field",field,"at time",timestep,"of",cdatpath,"was %d msec (proc_time: %d msec)"  % (interval*1000,proctime*1000))

    return (result_str,result)


def create(query):
    """Create idx volumes corresponding to cdat dataset (xml or nc)."""

    t1 = time.clock()

    result_str="An unknown error occurred."
    result=RESULT_ERROR
    try:
        # parse query request
        job=urlparse.parse_qs(query)
        if not job.has_key("dataset"):
            raise ConvertError(RESULT_INVALID,"Query must specify a valid and accessible .xml or .nc file and destination path")

        global xml_path,idx_path,ondemand_service_address,dbpath,visusserver,visusserver_username,visusserver_password
        cdatpath=xml_path+"/"+job["dataset"][0]
        idxpath=idx_path
        if job.has_key("destination"):
            idxpath=idx_path+"/"+job["destination"][0]
        server=visusserver
        if job.has_key("server"):
            server=job["server"][0]
        username=visusserver_username
        if job.has_key("username"):
            username=job["username"][0]
        password=visusserver_password
        if job.has_key("password"):
            password=job["password"][0]
        force=False
        if job.has_key("force"):
            if job["force"][0]=="True" or job["force"][0]=="1" or job["force"][0]=="true":
                print "forcing job!!! (force="+job["force"][0]+")"
                force=True

        # create idx volumes from climate dataset
        import cdat_to_idx
        result_str=cdat_to_idx.generate_idx(inputfile=cdatpath,outputdir=idxpath,database=dbpath,server=server,username=username,password=password,service=ondemand_service_address,force=force)
        result=RESULT_SUCCESS

    except ConvertError as e:
        result=e.code
        result_str=e.msg
    except Exception as e:
        result=RESULT_ERROR
        result_str="unknown error occurred during create ("+str(e)+")"

    return (result_str,result)


def init(database,hostname,port,xmlpath,idxpath,visus_server,username,password):
    global visus_app
    visus_app=Visus.Application()

    global dbpath
    dbpath=database
    if not dbpath:
        dbpath=idxpath+"/idx.db"

    global ondemand_service_address
    ondemand_service_address="http://"+hostname+":"+str(port)

    global xml_path
    xml_path=xmlpath

    global idx_path
    idx_path=idxpath

    global visusserver
    visusserver=visus_server

    global visusserver_username
    visusserver_username=username

    global visusserver_password
    visusserver_password=password

#note: doesn't seem to be any huge reason in our case to prefer forking over theading, but both work fine
class OnDemandSocketServer(SocketServer.ThreadingTCPServer):
#class OnDemandSocketServer(SocketServer.ForkingTCPServer):
    """This is just to override handle_error to be less annoying when disconnections occur
    """
    def handle_error(self, request, client_address):
        """Handle an error gracefully.  May be overridden.
        The default is to print a traceback and continue.
        """
        if False:
            print '-'*40
            print 'Exception happened during processing of request from',
            print client_address
            import traceback
            traceback.print_exc() # XXX But this goes to stderr!
            print '-'*40


def start_server(hostname,port):
    # start server
    SocketServer.ThreadingTCPServer.allow_reuse_address = True
    #SocketServer.ForkingTCPServer.allow_reuse_address = True
    httpd = OnDemandSocketServer((hostname, port),cdatConverter)
    print "serving at port", port
    stdout.flush()
    try:
        httpd.serve_forever()
    except:
        pass

    httpd.shutdown()

# ############################
if __name__ == '__main__':

    # converter service default
    default_idx_db_path="/for_ganzberger1/idx/idx/idx.db"
    default_port=42299
    default_host="localhost"

    # cdat_to_idx defaults
    default_server="http://localhost:10000/mod_visus"
    default_xml_path="/data/xml/"
    default_idx_path="/data/idx/"

    import argparse
    parser = argparse.ArgumentParser(description="Convert CDAT data to IDX format.")
    parser.add_argument("-p","--port"    ,default=default_port,type=int,help="listen on port")
    parser.add_argument("-l","--hostname",default=default_host,help="ip address or hostname on which to listen")
    parser.add_argument("-x","--xmlpath",default=default_xml_path,help="path to cdat xml files created with uv-cdat cdscan utility")
    parser.add_argument("-i","--idxpath",default=default_idx_path,help="path to place newly created idx volumes")
    parser.add_argument("-d","--database",help="path to cdat-to-database (default is $IDX_PATH/idx.db)")
    parser.add_argument("-s","--visusserver",default=default_server,help="visus server with which to register newly created idx volumes")
    parser.add_argument("--username",default="root",help="username to register newly created idx volumes with server")
    parser.add_argument("--password",default="visus",help="password to register newly created idx volumes with server")
    args = parser.parse_args()

    init(args.database,args.hostname,args.port,args.xmlpath,args.idxpath,args.visusserver,args.username,args.password)

    print "Starting server http://"+args.hostname+":"+str(args.port)+"..."
    print "\txml path: "+xml_path
    print "\tidx path: "+idx_path
    print "\tdatabase: "+dbpath
    print "\tvisus server: "+visusserver
    start_server(args.hostname,args.port)

    print "done!"
