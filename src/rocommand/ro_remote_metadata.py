# ro_metadata.py

"""
Research Object metadata (manifest and annotations) access class
"""

import sys
import os
import os.path
import re
import urllib
import urlparse
import logging

log = logging.getLogger(__name__)

import MiscLib.ScanDirectories

import rdflib
import rdflib.namespace
#from rdflib import Namespace, URIRef, BNode, Literal
# Set up to use SPARQL
#rdflib.plugin.register(
#    'sparql', rdflib.query.Processor,
#    'rdfextras.sparql.processor', 'Processor')
#rdflib.plugin.register(
#    'sparql', rdflib.query.Result,
#    'rdfextras.sparql.query', 'SPARQLQueryResult')

import ro_settings
from ro_namespaces import RDF, RO, ORE, AO, DCTERMS
from ro_uriutils import isFileUri, resolveUri, resolveFileAsUri, getFilenameFromUri, isLiveUri, retrieveUri
import ro_manifest
import ro_annotation

# Class for ROSRS errors

class ROSRS_Error(Exception):

    def __init__(self, msg="ROSRS_Error", value=None, srsuri=None):
        self._msg    = msg
        self._value  = value
        self._srsuri = srsuri
        return

    def __str__(self):
        txt = self._msg
        if self._srsuri: txt += " for "+str(self._srsuri)
        if self._value:  txt += ": "+repr(self._value)
        return txt

    def __repr__(self):
        return ( "ROSRS_Error(%s, value=%s, srsuri=%s)"%
                 (repr(self._msg), repr(self._value), repr(self._srsuri)))


class ro_remote_metadata(object):
    """
    Class for accessing metadata of an RO stored by a ROSR service
    """

    def __init__(self, roconfig, roref, dummysetupfortest=False):
        """
        Initialize: read manifest from object at given directory into local RDF graph

        roconfig    is the research object manager configuration, supplied as a dictionary
        roref       a URI reference that refers to the Research Object to be accessed
        """
        self.roconfig = roconfig
        self.roref    = roref
        self.manifestgraph = None
        self.roannotations = None
        uri = resolveFileAsUri(roref)
        if not uri.endswith("/"): uri += "/"
        self.rouri    = rdflib.URIRef(uri)
        self.manifesturi  = self.getManifestUri()
        self.dummyfortest = dummysetupfortest
        self._loadManifest()
        # Get RO URI from manifest
        # May be different from computed value if manifest has absolute URI
        self.rouri = self.manifestgraph.value(None, RDF.type, RO.ResearchObject)
        return

    def _loadManifest(self):
        if self.manifestgraph: return self.manifestgraph
        self.manifestgraph = rdflib.Graph()
        if self.dummyfortest:
            # Fake minimal manifest graph for testing
            self.manifestgraph.add( (self.rouri, RDF.type, RO.ResearchObject) )
        else:
            # Read manifest graph
            self.manifestgraph.parse(self.manifesturi)
        return self.manifestgraph

    def _loadAnnotations(self):
        if self.roannotations: return self.roannotations
        # Assemble annotation graph
        # NOTE: the manifest itself is included as an annotation by the RO setup
        self._loadManifest()
        self.roannotations = rdflib.Graph()
        for (ann_node, subject) in self.manifestgraph.subject_objects(predicate=RO.annotatesAggregatedResource):
            ann_uri   = self.manifestgraph.value(subject=ann_node, predicate=AO.body)
            self._readAnnotationBody(ann_uri, self.roannotations)
        log.debug("roannotations graph:\n"+self.roannotations.serialize())
        return self.roannotations
    
    def _doRequest(self, uripath, method="GET", body=None, ctype=None, accept=None, reqheaders=None):
        """
        Perform HTTP request to ROSRS
        Return status, reason(text), response headers, response body
        """
        # Sort out path to use in HTTP request: request may be path or full URI or rdflib.URIRef
        uripath = str(uripath)        # get URI string from rdflib.URIRef
        log.debug("ROSRS_Session.doRequest uripath:  "+str(uripath))
        uriparts = urlparse.urlsplit(urlparse.urljoin(self._srspath,uripath))
        log.debug("ROSRS_Session.doRequest uriparts: "+str(uriparts))
        if uriparts.scheme:
            if self._srsscheme != uriparts.scheme:
                raise ROSRS_Error(
                    "ROSRS URI scheme mismatch",
                    value=uriparts.scheme,
                    srsuri=self._srsuri)
        if uriparts.netloc:
            if self._srshost != uriparts.netloc:
                raise ROSRS_Error(
                    "ROSRS URI host:port mismatch",
                    value=uriparts.netloc,
                    srsuri=self._srsuri)
        path = uriparts.path
        if uriparts.query: path += "?"+uriparts.query
        # Assemble request headers
        if not reqheaders:
            reqheaders = {}
        reqheaders["authorization"] = "Bearer "+self._key
        if ctype:
            reqheaders["content-type"] = ctype
        if accept:
            reqheaders["accept"] = accept
        # Execute request
        log.debug("ROSRS_Session.doRequest method:     "+method)
        log.debug("ROSRS_Session.doRequest path:       "+path)
        log.debug("ROSRS_Session.doRequest reqheaders: "+repr(reqheaders))
        log.debug("ROSRS_Session.doRequest body:       "+repr(body))
        self._httpcon.request(method, path, body, reqheaders)
        # Pick out elements of response
        response = self._httpcon.getresponse()
        status   = response.status
        reason   = response.reason
        headerlist = [ (h.lower(),v) for (h,v) in response.getheaders() ]
        headers  = dict(headerlist)   # dict(...) keeps last result of multiple keys
        headers["_headerlist"] = headerlist
        data = response.read()
        log.debug("ROSRS_Session.doRequest response: "+str(status)+" "+reason)
        log.debug("ROSRS_Session.doRequest headers:  "+repr(headers))
        log.debug("ROSRS_Session.doRequest data:     "+repr(data))
        return (status, reason, headers, data)

    def _doRequestRDF(self, uripath, method="GET", body=None, ctype=None, reqheaders=None):
        """
        Perform HTTP request with RDF response.
        If requests succeeds, return response as RDF graph,
        or return fake 9xx status if RDF cannot be parsed
        otherwise return responbse and content per request.
        Thus, only 2xx responses include RDF data.

        NOTE: this method has been adapted from TestApi_ROSRS
        """
        (status, reason, headers, data) = self.doRequest(uripath,
            method=method, body=body,
            ctype=ctype, accept="application/rdf+xml", reqheaders=reqheaders)
        if status >= 200 and status < 300:
            if headers["content-type"].lower() == "application/rdf+xml":
                rdfgraph = rdflib.graph.Graph()
                try:
                    rdfgraph.parse(data=data, format="xml")
                    data = rdfgraph
                except Exception, e:
                    status   = 902
                    reason   = "RDF parse failure"
            else:
                status   = 901
                reason   = "Non-RDF content-type returned"
        return (status, reason, headers, data)


#    def updateManifest(self):
#        """
#        Write updated manifest file for research object
#        """
#        self.manifestgraph.serialize(
#            destination=self.getManifestFilename(), format='xml',
#            base=self.rouri, xml_base="..")
#        return

#    def addAggregatedResources(self, ro_file, recurse=True):
#        """
#        Scan a local directory and add files found to the RO aggregation
#        """
#        def notHidden(f):
#            return re.match("\.|.*/\.", f) == None
#        log.debug("addAggregatedResources: ref %s, file %s"%(self.roref, ro_file))
#        self.getRoFilename()  # Check that we have one
#        if ro_file.endswith(os.path.sep):
#            ro_file = ro_file[0:-1]
#        rofiles = [ro_file]
#        if os.path.isdir(ro_file):
#            rofiles = filter( notHidden,
#                                MiscLib.ScanDirectories.CollectDirectoryContents(
#                                    os.path.abspath(ro_file), baseDir=os.path.abspath(self.roref),
#                                    listDirs=False, listFiles=True, recursive=recurse, appendSep=False)
#                            )
#        s = self.getRoUri()
#        for f in rofiles:
#            log.debug("- file %s"%f)
#            stmt = (s, ORE.aggregates, self.getComponentUri(f))
#            if stmt not in self.manifestgraph: self.manifestgraph.add(stmt)
#        self.updateManifest()
#        return

    def getAggregatedResources(self):
        """
        Returns iterator over all resources aggregated by a manifest.

        Each value returned by the iterator is an aggregated resource URI
        """
        log.debug("getAggregatedResources: uri %s"%(self.rouri))
        for r in self.manifestgraph.objects(subject=self.rouri, predicate=ORE.aggregates):
            yield r
        return
    
    def isAggregatedResource(self, resuri):
        '''
        Returns true if the manifest says that the research object aggregates the
        resource. Resource URI is resolved against the RO URI unless it's absolute.
        '''
        log.debug("isAggregatedResource: ro uri %s res uri %s"%(self.rouri, resuri))
        return (self.rouri, ORE.aggregates, resuri) in self.manifestgraph

    def isInternalResource(self, rouri, resuri):
        '''
        Check if the resource is internal, i.e. should the resource content be uploaded
        to the ROSR service. Returns true if the resource URI has the RO URI as a prefix.
        '''
        return rouri.startswith(resuri)

    def isExternalResource(self, resuri):
        '''
        Check if the resource is external, i.e. can be aggregated as a URI reference.
        Returns true if the URI has 'http' or 'https' scheme.
        '''
        parseduri = urlparse.urlsplit(resuri)
        return parseduri.scheme in ["http", "https"]

    def aggregateResourceInt(
            self, rouri, respath, ctype="application/octet-stream", body=None):
        """
        Aggegate internal resource
        Return (status, reason, proxyuri, resuri), where status is 201

        NOTE: this method has been adapted from TestApi_ROSRS
        """
        # POST (empty) proxy value to RO ...
        reqheaders = { "slug": respath }
        proxydata = ("""
            <rdf:RDF
              xmlns:ore="http://www.openarchives.org/ore/terms/"
              xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" >
              <ore:Proxy>
              </ore:Proxy>
            </rdf:RDF>
            """)
        (status, reason, headers, data) = self.doRequest(rouri,
            method="POST", ctype="application/vnd.wf4ever.proxy",
            reqheaders=reqheaders, body=proxydata)
        if status != 201:
            raise self.error("Error creating aggregation proxy",
                            "%d03 %s (%s)"%(status, reason, respath))
        proxyuri = rdflib.URIRef(headers["location"])
        links    = self.parseLinks(headers)
        if "http://www.openarchives.org/ore/terms/proxyFor" not in links:
            raise self.error("No ore:proxyFor link in create proxy response",
                            "Proxy URI %s"%str(proxyuri))
        resuri   = rdflib.URIRef(links["http://www.openarchives.org/ore/terms/proxyFor"])
        # PUT resource content to indicated URI
        (status, reason, headers, data) = self.doRequest(resuri,
            method="PUT", ctype=ctype, body=body)
        if status not in [200,201]:
            raise self.error("Error creating aggregated resource content",
                "%d03 %s (%s)"%(status, reason, respath))
        return (status, reason, proxyuri, resuri)

    def updateResourceInt(
            self, resuri, ctype="application/octet-stream", body=None):
        """
        Update an already aggregated internal resource
        Return (status, reason, None, resuri), where status is 200

        NOTE: this method has been adapted from TestApi_ROSRS
        """
        # PUT resource content to indicated URI
        (status, reason, headers, data) = self.doRequest(resuri,
            method="PUT", ctype=ctype, body=body)
        if status != 200:
            raise self.error("Error updating aggregated resource content",
                "%d03 %s (%s)"%(status, reason, resuri))
        return (status, reason, headers, resuri)

    def aggregateResourceExt(self, rouri, resuri):
        """
        Aggegate internal resource
        Return (status, reason, proxyuri, resuri), where status is 200 or 201

        NOTE: this method has been adapted from TestApi_ROSRS
        """
        # Aggegate external resource: POST proxy ...
        proxydata = ("""
            <rdf:RDF
              xmlns:ore="http://www.openarchives.org/ore/terms/"
              xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" >
              <ore:Proxy>
                <ore:proxyFor rdf:resource="%s" />
              </ore:Proxy>
            </rdf:RDF>
            """)%str(resuri)
        (status, reason, headers, data) = self.doRequest(rouri,
            method="POST", ctype="application/vnd.wf4ever.proxy",
            body=proxydata)
        if status != 201:
            raise self.error("Error creating aggregation proxy",
                "%d03 %s (%s)"%(status, reason, str(resuri)))
        proxyuri = rdflib.URIRef(headers["location"])
        return (status, reason, proxyuri, rdflib.URIRef(resuri))
    
    def deaggregateResource(self, rouri, resuri):
        """
        Deaggregate an aggregated resource. In case of an internal resource, its
        content is deleted.
        Return (status, reason, None, resuri), where status is 204 or 404
        """
#        # Aggegate external resource: POST proxy ...
#        proxydata = ("""
#            <rdf:RDF
#              xmlns:ore="http://www.openarchives.org/ore/terms/"
#              xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" >
#              <ore:Proxy>
#                <ore:proxyFor rdf:resource="%s" />
#              </ore:Proxy>
#            </rdf:RDF>
#            """)%str(resuri)
#        (status, reason, headers, data) = self.doRequest(rouri,
#            method="POST", ctype="application/vnd.wf4ever.proxy",
#            body=proxydata)
#        if status != 201:
#            raise self.error("Error creating aggregation proxy",
#                "%d03 %s (%s)"%(status, reason, str(resuri)))
#        proxyuri = rdflib.URIRef(headers["location"])
#        return (status, reason, proxyuri, rdflib.URIRef(resuri))

    def getHead(self, resuriref):
        """
        Retrieve resource from RO
        Return (status, reason, headers, data), where status is 200 or 404
        """
        resuri = str(resuriref)
        (status, reason, headers, data) = self._doRequest(resuri,
            method="HEAD", None, None)
        if status in [200, 404]:
            return (status, reason, headers, data if status == 200 else None)
        raise self.error("Error retrieving RO resource", "%03d %s (%s)"%(status, reason, resuriref))

#    def _createAnnotationBody(self, roresource, attrdict, defaultType="string"):
#        """
#        Create a new annotation body for a single resource in a research object, based
#        on a supplied graph value.
#
#        Existing annotations for the same resource are not touched; if an annotation is being
#        added or replaced, it is the calling program'sresponsibility to update the manifest to
#        reference the active annotations.  A new name is allocated for the created annotation,
#        graph which is returned as the result of this function.
#
#        roresource  is the name of the Research Object component to be annotated,
#                    possibly relative to the RO root directory.
#        attrdict    is a dictionary of attributes to be saved in the annotation body.
#                    Dictionary keys are attribute names that can be resolved via
#                    ro_annotation.getAnnotationByName.
#
#        Returns the name of the annotation body created relative to the RO directory.
#        """
#        af = ro_annotation.createAnnotationBody(
#            self.roconfig, self.getRoFilename(), roresource, attrdict, defaultType)
#        return os.path.join(ro_settings.MANIFEST_DIR+"/", af)

#    def _createAnnotationGraphBody(self, roresource, anngraph):
#        """
#        Create a new annotation body for a single resource in a research object, based
#        on a supplied graph value.
#
#        Existing annotations for the same resource are not touched; if an annotation is being
#        added or replaced, it is the calling program'sresponsibility to update the manifest to
#        reference the active annotations.  A new name is allocated for the created annotation,
#        graph which is returned as the result of this function.
#
#        roresource  is the name of the Research Object component to be annotated,
#                    possibly relative to the RO root directory.
#        anngraph    is an annotation graph that is to be saved.
#
#        Returns the name of the annotation body created relative to the RO
#        manifest and metadata directory.
#        """
#        af = ro_annotation.createAnnotationGraphBody(
#            self.roconfig, self.getRoFilename(), roresource, anngraph)
#        return os.path.join(ro_settings.MANIFEST_DIR+"/", af)

#    def _readAnnotationBody(self, annotationref, anngr=None):
#        """
#        Read annotation body from indicated resource, return RDF Graph of annotation values.
#
#        annotationref   is a URI reference of an annotation, possibly relative to the RO base URI
#                        (e.g. as returned by _createAnnotationBody method).
#        """
#        log.debug("_readAnnotationBody %s"%(annotationref))
#        annotationuri    = self.getComponentUri(annotationref)
#        annotationformat = "xml"
#        # Look at file extension to figure format
#        # (rdflib.Graph.parse says;
#        #   "used if format can not be determined from the source")
#        if re.search("\.(ttl|n3)$", annotationuri): annotationformat="n3"
#        if anngr == None:
#            log.debug("_readAnnotationBody: new graph")
#            anngr = rdflib.Graph()
#        try:
#            anngr.parse(annotationuri, format=annotationformat)
#            log.debug("_readAnnotationBody parse %s, len %i"%(annotationuri, len(anngr)))
#        except IOError, e:
#            log.debug("_readAnnotationBody "+annotationref+", "+repr(e))
#            anngr = None
#        return anngr

#    def _addAnnotationBodyToRoGraph(self, rofile, annfile):
#        """
#        Add a new annotation body to an RO graph
#
#        rofile      is the research object file being annotated
#        annfile     is the file name of the annotation body to be added,
#                    possibly relative to the RO URI
#        """
#        # <ore:aggregates>
#        #   <ro:AggregatedAnnotation>
#        #     <ro:annotatesAggregatedResource rdf:resource="data/UserRequirements-astro.ods" />
#        #     <ao:body rdf:resource=".ro/(annotation).rdf" />
#        #   </ro:AggregatedAnnotation>
#        # </ore:aggregates>
#        log.debug("_addAnnotationBodyToRoGraph annfile %s"%(annfile))
#        ann = rdflib.BNode()
#        self.manifestgraph.add((ann, RDF.type, RO.AggregatedAnnotation))
#        self.manifestgraph.add((ann, RO.annotatesAggregatedResource,
#            self.getComponentUri(rofile)))
#        self.manifestgraph.add((ann, AO.body,
#            self.getComponentUri(annfile)))
#        self.manifestgraph.add((self.getRoUri(), ORE.aggregates, ann))
#        return

#    def _removeAnnotationBodyFromRoGraph(self, annbody):
#        """
#        Remove references to an annotation body from an RO graph
#
#        annbody     is the the annotation body node to be removed
#        """
#        self.manifestgraph.remove((annbody, None,           None   ))
#        self.manifestgraph.remove((None,    ORE.aggregates, annbody))
#        return

#    def addSimpleAnnotation(self, rofile, attrname, attrvalue, defaultType="string"):
#        """
#        Add a simple annotation to a file in a research object.
#
#        rofile      names the file or resource to be annotated, possibly relative to the RO.
#        attrname    names the attribute in a form recognized by getAnnotationByName
#        attrvalue   is a value to be associated with the attribute
#        """
#        ro_dir   = self.getRoFilename()
#        annfile  = self._createAnnotationBody(rofile, {attrname: attrvalue}, defaultType)
#        self._addAnnotationBodyToRoGraph(rofile, annfile)
#        self.updateManifest()
#        return

#    def removeSimpleAnnotation(self, rofile, attrname, attrvalue):
#        """
#        Remove a simple annotation or multiple matching annotations a research object.
#
#        rofile      names the annotated file or resource, possibly relative to the RO.
#        attrname    names the attribute in a form recognized by getAnnotationByName
#        attrvalue   is the attribute value to be deleted, or Nomne to delete all vaues
#        """
#        ro_dir    = self.getRoFilename()
#        ro_graph  = self._loadManifest()
#        subject     = self.getComponentUri(rofile)
#        (predicate,valtype) = ro_annotation.getAnnotationByName(self.roconfig, attrname)
#        val         = attrvalue and ro_annotation.makeAnnotationValue(attrvalue, valtype)
#        add_annotations    = []
#        remove_annotations = []
#        log.debug("removeSimpleAnnotation subject %s, predicate %s, val %s"%
#                  (str(subject), str(predicate), val))
#        # Scan for annotation graph resourcxes containing this annotation
#        for ann_node in ro_graph.subjects(predicate=RO.annotatesAggregatedResource, object=subject):
#            ann_uri   = ro_graph.value(subject=ann_node, predicate=AO.body)
#            log.debug("removeSimpleAnnotation ann_uri %s"%(str(ann_uri)))
#            if self.isRoMetadataRef(ann_uri):
#                ann_graph = self._readAnnotationBody(self.getComponentUriRel(ann_uri))
#                log.debug("removeSimpleAnnotation ann_graph %s"%(ann_graph))
#                if (subject, predicate, val) in ann_graph:
#                    ann_graph.remove((subject, predicate, val))
#                    if (subject, None, None) in ann_graph:
#                        # Triples remain in annotation body: write new body and update RO graph
#                        ann_name = self._createAnnotationGraphBody(rofile, ann_graph)
#                        remove_annotations.append(ann_node)
#                        add_annotations.append(ann_name)
#                    else:
#                        # Remove annotation from RO graph
#                        remove_annotations.append(ann_node)
#        # Update RO manifest graph if needed
#        if add_annotations or remove_annotations:
#            for a in remove_annotations:
#                self._removeAnnotationBodyFromRoGraph(a)
#            for a in add_annotations:
#                self._addAnnotationBodyToRoGraph(rofile, a)
#            self.updateManifest()
#        return

#    def replaceSimpleAnnotation(self, rofile, attrname, attrvalue):
#        """
#        Replace a simple annotation in a research object.
#
#        rofile      names the file or resource to be annotated, possibly relative to the RO.
#        attrname    names the attribute in a form recognized by getAnnotationByName
#        attrvalue   is a new value to be associated with the attribute
#        """
#        ro_dir   = self.getRoFilename()
#        ro_graph = self._loadManifest()
#        subject  = self.getComponentUri(rofile)
#        (predicate,valtype) = ro_annotation.getAnnotationByName(self.roconfig, attrname)
#        log.debug("Replace annotation: subject %s, predicate %s, value %s"%
#                  (repr(subject), repr(predicate), repr(attrvalue)))
#        ro_graph.remove((subject, predicate, None))
#        ro_graph.add((subject, predicate,
#                      ro_annotation.makeAnnotationValue(attrvalue, valtype)))
#        self.updateManifest()
#        return

#    def addGraphAnnotation(self, rofile, graph):
#        """
#        Add an annotation graph for a named resource.  Unlike addSimpleAnnotation, this
#        method adds an annotation to the manifest using an existing RDF graph (which is
#        presumably itself in the RO structure).
#
#        rofile      names the file or resource to be annotated, possibly relative to the RO.
#                    Note that no checks are performed to ensure that the graph itself actually
#                    refers to this resource - that's up the the creator of the graph.  This
#                    identifies the resourfce with which the annotation body is associated in
#                    the RO manifest.
#        graph       names the file or resource containing the annotation body.
#        """
#        ro_graph = self._loadManifest()
#        ann = rdflib.BNode()
#        ro_graph.add((ann, RDF.type, RO.AggregatedAnnotation))
#        ro_graph.add((ann, RO.annotatesAggregatedResource, self.getComponentUri(rofile)))
#        ro_graph.add((ann, AO.body, self.getComponentUri(graph)))
#        ro_graph.add((self.getRoUri(), ORE.aggregates, ann))
#        self.updateManifest()
#        return

    def iterateAnnotations(self, subject=None, property=None):
        """
        Returns an iterator over annotations of the current RO that match th
        supplied subject and/or property.
        """
        log.debug("iterateAnnotations s:%s, p:%s"%(str(subject),str(property)))
        ann_graph = self._loadAnnotations()
        for (s, p, v) in ann_graph.triples((subject, property, None)):
            if not isinstance(s, rdflib.BNode):
                log.debug("Triple: %s %s %s"%(s,p,v))
                yield (s, p, v)
        return

    def getRoAnnotations(self):
        """
        Returns iterator over annotations applied to the RO as an entity.

        Each value returned by the iterator is a (subject,predicate,object) triple.
        """
        return self.iterateAnnotations(subject=self.getRoUri())

    def getFileAnnotations(self, rofile):
        """
        Returns iterator over annotations applied to a specified component in the RO

        Each value returned by the iterator is a (subject,predicate,object) triple.
        """
        return self.iterateAnnotations(subject=self.getComponentUri(rofile))

    def getAllAnnotations(self):
        """
        Returns iterator over all annotations associated with the RO

        Each value returned by the iterator is a (subject,predicate,object) triple.
        """
        return self.iterateAnnotations()

    def getAnnotationValues(self, rofile, attrname):
        """
        Returns iterator over annotation values for given subject and attribute
        """
        (predicate,valtype) = ro_annotation.getAnnotationByName(self.roconfig, attrname)
        return ( v for (s,p,v) in self.iterateAnnotations(subject=self.getComponentUri(rofile), property=predicate) )

    def queryAnnotations(self, query, initBindings={}):
        """
        Runs a query over the combined annotation graphs (including the manifest)
        and returns True or False (for ASK queries) or a list of doctionaries of
        query results (for SELECT queries).
        """
        ann_gr = self._loadAnnotations()
        resp = ann_gr.query(query,initBindings=initBindings)
        if resp.type == 'ASK':
            return resp.askAnswer
        elif resp.type == 'SELECT':
            return resp.bindings
        else:
            assert False, "Unexpected query response type %s"%resp.type
        return None

    def showAnnotations(self, annotations, outstr):
        ro_annotation.showAnnotations(self.roconfig, self.getRoFilename(), annotations, outstr)
        return

    # Support methods for accessing the manifest graph

    def _getRoManifestGraph(self):
        """
        Returns RDF graph containing RO manifest
        """
        return self.manifestgraph

    def roManifestContains(self, stmt):
        """
        Returns True if the RO manifest contains a statement matching the supplied triple.
        """
        return stmt in self.manifestgraph

    def getResourceValue(self, resource, predicate):
        """
        Returns value for resource whose URI is supplied assocfiated with indicated predicate
        """
        return self.manifestgraph.value(subject=resource, predicate=predicate, object=None)

    def getResourceType(self, resource):
        """
        Returns type of resource whose URI is supplied
        """
        return self.getResourceValue(resource, RDF.type)

    def getRoMetadataDict(self):
        """
        Returns dictionary of metadata about the RO from the manifest graph
        """
        strsubject = ""
        if isinstance(self.rouri, rdflib.URIRef): strsubject = str(self.rouri)
        manifestDict = {
            'ropath':         self.getRoFilename(),
            'rouri':          strsubject,
            'roident':        self.getResourceValue(self.rouri, DCTERMS.identifier  ),
            'rotitle':        self.getResourceValue(self.rouri, DCTERMS.title       ),
            'rocreator':      self.getResourceValue(self.rouri, DCTERMS.creator     ),
            'rocreated':      self.getResourceValue(self.rouri, DCTERMS.created     ),
            'rodescription':  self.getResourceValue(self.rouri, DCTERMS.description ),
            }
        return manifestDict

    # Support methods for accessing RO and component URIs

    def getRoRef(self):
        """
        Returns RO URI reference supplied (which may be a local file directory string)
        """
        return self.roref

    def getRoUri(self):
        return self.rouri

    def getComponentUri(self, path):
        return rdflib.URIRef(urlparse.urljoin(str(self.getRoUri()), path))

    def getComponentUriRel(self, path):
        file_uri = urlparse.urlunsplit(urlparse.urlsplit(str(self.getComponentUri(path))))
        ro_uri   = urlparse.urlunsplit(urlparse.urlsplit(str(self.getRoUri())))
        if ro_uri is not None and file_uri.startswith(ro_uri):
            file_uri_rel = file_uri.replace(ro_uri, "", 1)
        else:
            file_uri_rel = path
        return rdflib.URIRef(file_uri_rel)

    def isRoMetadataRef(self, uri):
        """
        Test if supplied URI is a reference to the current RO metadata area
        """
        urirel = self.getComponentUriRel(uri)
        return str(urirel).startswith(ro_settings.MANIFEST_DIR+"/")

    def getManifestUri(self):
        return self.getComponentUri(ro_settings.MANIFEST_DIR+"/"+ro_settings.MANIFEST_FILE)

    def isLocalFileRo(self):
        """
        Test current RO URI to see if it is a local file system reference
        """
        return isFileUri(self.getRoUri())

    def getRoFilename(self):
        return getFilenameFromUri(self.getRoUri())

    def getManifestFilename(self):
        """
        Return manifesrt file name: used for local updates
        """
        return os.path.join(self.getRoFilename(), ro_settings.MANIFEST_DIR+"/",
                            ro_settings.MANIFEST_FILE)

# End.

