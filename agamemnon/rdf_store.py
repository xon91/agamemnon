
from rdflib.store import Store, NO_STORE, VALID_STORE
from rdflib.plugin import register
from rdflib.namespace import Namespace, split_uri, urldefrag
from rdflib import URIRef, Literal
from agamemnon.factory import load_from_settings
from agamemnon.exceptions import NodeNotFoundException
import pycassa
import json
import logging

register('Agamemnon', Store, 
                'agamemnon.rdf_store', 'AgamemnonStore')

log = logging.getLogger(__name__)

class AgamemnonStore(Store):
    """
    An agamemnon based triple store.
    
    This triple store uses agamemnon as the underlying graph representation.
    
    """

    node_namespace_base = "https://github.com/globusonline/agamemnon/nodes/"
    relationship_namespace_base = "https://github.com/globusonline/agamemnon/nodes/"
    
    def __init__(self, configuration=None, identifier=None, data_store=None):
        super(AgamemnonStore, self).__init__(configuration)
        self.identifier = identifier

        if configuration:
            self._process_config(configuration)

        if data_store:
            self.data_store = data_store

        # namespace and prefix indexes
        self.__namespace = {}
        self.__prefix = {}

        self._ignored_node_types = set(['reference'])

    def open(self, configuration=None, create=False, repl_factor = 1):
        if configuration:
            self._process_config(configuration)

        keyspace = self.configuration['agamemnon.keyspace']
        if create and keyspace != "memory":
            hostlist = json.loads(self.configuration['agamemnon.host_list'])
            system_manager = pycassa.SystemManager(hostlist[0])
            try:
                log.info("Attempting to drop keyspace")
                system_manager.drop_keyspace(keyspace)
            except pycassa.cassandra.ttypes.InvalidRequestException:
                log.warn("Keyspace didn't exist")
            finally:
                log.info("Creating keyspace")
                system_manager.create_keyspace(keyspace, replication_factor=repl_factor)

        self.data_store = load_from_settings(self.configuration)
        return VALID_STORE

    @property
    def data_store(self):
        return self._ds

    @data_store.setter
    def data_store(self, ds):
        self._ds = ds
        #self.load_namespaces()

    @property
    def ignore_reference_nodes(self):
        return "reference" in self._ignored_node_types

    @ignore_reference_nodes.setter
    def ignore_reference_nodes(self, value):
        if value:
            self.ignore('reference')
        else:
            self.unignore('reference')

    def ignore(self, node_type):
        self._ignored_node_types.add(node_type)

    def unignore(self, node_type):
        self._ignored_node_types.remove(node_type)

    def _process_config(self, configuration):
        self.configuration = configuration
        config_prefix = "agamemnon.rdf_"
        for key, value in configuration.items():
            if key.startswith(config_prefix):
                setattr(self, key[len(config_prefix):], value)

    def add(self, (subject, predicate, object), context, quoted=False):
        if isinstance(subject, Literal):
            raise TypeError("Subject can't be literal")

        if isinstance(predicate, Literal):
            raise TypeError("Predicate can't be literal")

        p_rel_type = self.uri_to_rel_type(predicate) 
        s_node = self.uri_to_node(subject, True)

        #inline literals as attributes
        if isinstance(object, Literal):
            log.debug("Setting %r on %r" % (p_rel_type, s_node))
            s_node[p_rel_type] = object.toPython()
        else:
            o_node = self.uri_to_node(object, True)

            log.debug("Creating relationship of type %s from %s on %s" % (p_rel_type, s_node, o_node))
            self.data_store.create_relationship(str(p_rel_type), s_node, o_node)

    def remove(self, triple, context=None):
        for (subject, predicate, object), c in self.triples(triple):
            log.debug("start delete")
            s_node = self.uri_to_node(subject)
            p_rel_type = self.uri_to_rel_type(predicate)
            if isinstance(object, Literal):
                if p_rel_type in s_node.attributes:
                    if s_node[p_rel_type] == object.toPython():
                        log.debug("Deleting %s from %s" % (p_rel_type, s_node))
                        del s_node[p_rel_type]
                        s_node.commit()
            else:
                o_node_type, o_node_id = self.uri_to_node_def(object) 
                if o_node_type in self._ignored_node_types: return
                for rel in getattr(s_node, p_rel_type).relationships_with(o_node_id):
                    if rel.target_node.type == o_node_type:
                        log.debug("Deleting %s" % rel)
                        rel.delete()
                        log.debug("Deleted %s" % rel)

    def triples(self, (subject, predicate, object), context=None):
        log.debug("Looking for triple %s, %s, %s" % (subject, predicate, object))
        if isinstance(subject, Literal) or isinstance(predicate, Literal):
            # subject and predicate can't be literal silly rabbit
            return (c for c in []) # TODO: best way to return empty generator

        # Determine what mechanism to use to do lookup
        try:
            if predicate is not None:
                if subject is not None:
                    if object is not None:
                        return self._triples_by_spo(subject, predicate, object)
                    else:
                        return self._triples_by_sp(subject, predicate)
                else:
                    if object is not None:
                        return self._triples_by_po(predicate, object)
                    else:
                        return self._triples_by_p(predicate)
            else:
                if subject is not None:
                    if object is not None:
                        return self._triples_by_so(subject, object)
                    else:
                        return self._triples_by_s(subject)
                else:
                    if object is not None:
                        return self._triples_by_o(object)
                    else:
                        return self._all_triples()
        except NodeNotFoundException:
            # exit generator as we found no triples
            log.debug("Failed to find any triples.")
            return (c for c in []) # TODO: best way to return empty generator

    def _triples_by_spo(self, subject, predicate, object):
        log.debug("Finding triple by spo")
        p_rel_type = self.uri_to_rel_type(predicate) 
        s_node = self.uri_to_node(subject)
        if s_node.type in self._ignored_node_types: return
        if isinstance(object, Literal):
            if p_rel_type in s_node.attributes:
                if s_node[p_rel_type] == object.toPython():
                    log.debug("Found %s, %s, %s" % (subject, predicate, object))
                    yield (subject, predicate, object), None
        else:
            o_node_type, o_node_id = self.uri_to_node_def(object) 
            if o_node_type in self._ignored_node_types: return
            for rel in getattr(s_node, p_rel_type).relationships_with(o_node_id):
                if rel.target_node.type == o_node_type:
                    log.debug("Found %s, %s, %s" % (subject, predicate, object))
                    yield (subject, predicate, object), None

    def _triples_by_sp(self, subject, predicate):
        log.debug("Finding triple by sp")
        p_rel_type = self.uri_to_rel_type(predicate) 
        s_node = self.uri_to_node(subject) 
        if s_node.type in self._ignored_node_types: return
        for rel in getattr(s_node, p_rel_type).outgoing:
            object = self.node_to_uri(rel.target_node)
            log.debug("Found %s, %s, %s" % (subject, predicate, object))
            yield (subject, predicate, object), None

        if p_rel_type in s_node.attributes:
            object = Literal(s_node[p_rel_type])
            log.debug("Found %s, %s, %s" % (subject, predicate, object))
            yield (subject, predicate, object), None

    def _triples_by_po(self, predicate, object):
        log.debug("Finding triple by po")
        p_rel_type = self.uri_to_rel_type(predicate) 
        if isinstance(object, Literal):
            log.warn("Your query requires full graph traversal do to Agamemnon datastructure.")
            for s_node in self._all_nodes():
                subject = self.node_to_uri(s_node)
                if p_rel_type in s_node.attributes:
                    if s_node[p_rel_type] == object.toPython():
                        log.debug("Found %s, %s, %s" % (subject, predicate, object))
                        yield (subject, predicate, object), None
        else:
            o_node = self.uri_to_node(object) 
            for rel in getattr(o_node, p_rel_type).incoming:
                subject = self.node_to_uri(rel.source_node)
                log.debug("Found %s, %s, %s" % (subject, predicate, object))
                yield (subject, predicate, object), None

    def _triples_by_so(self, subject, object):
        log.debug("Finding triple by so.")
        s_node = self.uri_to_node(subject) 
        if s_node.type in self._ignored_node_types: return
        if isinstance(object, Literal):
            for p_rel_type in s_node.attributes.keys():
                if p_rel_type.startswith("__"): continue #ignore special names
                if s_node[p_rel_type] == object.toPython():
                    predicate = self.rel_type_to_uri(p_rel_type)
                    log.debug("Found %s, %s, %r" % (subject, predicate, object))
                    yield (subject, predicate, object), None
        else:
            o_node = self.uri_to_node(object) 
            if o_node.type in self._ignored_node_types: return
            for rel in s_node.relationships.outgoing:
                if rel.target_node == o_node:
                    predicate = self.rel_type_to_uri(rel.type)
                    log.debug("Found %s, %s, %s" % (subject, predicate, object))
                    yield (subject, predicate, object), None

    def _triples_by_s(self, subject):
        log.debug("Finding triple by s")
        s_node = self.uri_to_node(subject) 
        if s_node.type in self._ignored_node_types: return
        for rel in s_node.relationships.outgoing:
            if rel.target_node.type in self._ignored_node_types: continue
            predicate = self.rel_type_to_uri(rel.type)
            object = self.node_to_uri(rel.target_node)
            log.debug("Found %s, %s, %s" % (subject, predicate, object))
            yield (subject, predicate, object), None

        for p_rel_type in s_node.attributes.keys():
            if p_rel_type.startswith("__"): continue #ignore special names
            predicate = self.rel_type_to_uri(p_rel_type)
            object = Literal(s_node[p_rel_type])
            log.debug("Found %s, %s, %r" % (subject, predicate, object))
            yield (subject, predicate, object), None

    def _triples_by_p(self, predicate):
        log.debug("Finding triple by p")
        log.warn("Your query requires full graph traversal do to Agamemnon datastructure.")

        p_rel_type = self.uri_to_rel_type(predicate) 
        for s_node in self._all_nodes():
            if s_node.type in self._ignored_node_types: continue
            subject = self.node_to_uri(s_node)
            for rel in getattr(s_node, p_rel_type).outgoing:
                if rel.target_node.type in self._ignored_node_types: continue
                object = self.node_to_uri(rel.target_node)
                log.debug("Found %s, %s, %s" % (subject, predicate, object))
                yield (subject, predicate, object), None

            if p_rel_type in s_node.attributes:
                object = Literal(s_node[p_rel_type])
                log.debug("Found %s, %s, %s" % (subject, predicate, object))
                yield (subject, predicate, object ), None

    def _triples_by_o(self, object):
        log.debug("Finding triple by o")
        if isinstance(object, Literal):
            log.warn("Your query requires full graph traversal do to Agamemnon datastructure.")
            for s_node in self._all_nodes():
                if s_node.type in self._ignored_node_types: continue
                subject = self.node_to_uri(s_node)
                for p_rel_type in s_node.attributes.keys():
                    if p_rel_type.startswith("__"): continue #ignore special names
                    if s_node[p_rel_type] == object.toPython():
                        predicate = self.rel_type_to_uri(p_rel_type)
                        log.debug("Found %s, %s, %s" % (subject, predicate, object))
                        yield (subject, predicate, object), None

        else:
            o_node = self.uri_to_node(object) 
            for rel in o_node.relationships.incoming:
                if rel.source_node.type in self._ignored_node_types: continue
                predicate = self.rel_type_to_uri(rel.type)
                subject = self.node_to_uri(rel.source_node)
                log.debug("Found %s, %s, %s" % (subject, predicate, object))
                yield (subject, predicate, object), None

    def _all_triples(self):
        log.debug("Finding all triples.")
        log.warn("Your query requires full graph traversal do to Agamemnon datastructure.")
        for s_node in self._all_nodes():
            if s_node.type in self._ignored_node_types: continue
            subject = self.node_to_uri(s_node)
            for rel in s_node.relationships.outgoing:
                if rel.target_node.type in self._ignored_node_types: continue
                predicate = self.rel_type_to_uri(rel.type)
                object = self.node_to_uri(rel.target_node)
                log.debug("Found %s, %s, %s" % (subject, predicate, object))
                yield (subject, predicate, object), None

            for p_rel_type in s_node.attributes.keys():
                if p_rel_type.startswith("__"): continue #ignore special names
                predicate = self.rel_type_to_uri(p_rel_type)
                object = Literal(s_node[p_rel_type])
                log.debug("Found %s, %s, %s" % (subject, predicate, object))
                yield (subject, predicate, object), None

    def _all_nodes(self):
        ref_ref_node = self.data_store.get_reference_node()
        for ref in ref_ref_node.instance.outgoing:
            if ref.target_node.key in self._ignored_node_types: continue
            for instance in ref.target_node.instance.outgoing:
                yield instance.target_node

    def node_to_uri(self, node):
        ns = self.namespace(node.type)
        if ns is None:
            ns = self.unmunge_node_type(node.type)
            self.bind(node.type, ns)
        uri = ns[node.key]
        log.debug("Converted node %s to uri %s" % (node, uri))
        return uri

    def uri_to_node(self, uri, create=False):
        node_type, node_id = self.uri_to_node_def(uri)
        try:
            log.debug("Looking up node: %s => %s" % (node_type,node_id))
            return self.data_store.get_node(str(node_type), str(node_id))
        except NodeNotFoundException:
            if create:
                node = self.data_store.create_node(str(node_type), str(node_id))
                log.debug("Created node: %s" % node)
            else:
                raise
            return node

    def uri_to_node_def(self, uri):
        namespace, node_id = split_uri(uri)
        prefix = self.prefix(namespace)
        if prefix is None:
            node_type = self.munge_namespace(namespace)
            self.bind(node_type, namespace)
        else:
            node_type = prefix
        return node_type, node_id

    def munge_namespace(self,namespace):
        """ We want to remove the base name if applicable and remove all /'s"""
        if namespace.startswith(self.node_namespace_base):
            namespace = namespace[len(self.node_namespace_base):]
        return namespace[:-1].replace("/","_")

    def unmunge_node_type(self, node_type):
        ns = node_type.replace("_","/") + "#"
        if "://" not in ns:
            ns = self.node_namespace_base + ns
        return Namespace(ns)

    def rel_type_to_uri(self, rel_type):
        return URIRef(rel_type)

    def uri_to_rel_type(self, uri):
        return URIRef(uri)

    def bind(self, prefix, namespace):
        self.__prefix[Namespace(namespace)] = unicode(prefix)
        self.__namespace[prefix] = Namespace(namespace)


    def namespace(self, prefix):
        return self.__namespace.get(prefix, None)

    def prefix(self, namespace):
        return self.__prefix.get(Namespace(namespace), None)

    def namespaces(self):
        for prefix, namespace in self.__namespace.iteritems():
            yield prefix, namespace

    def __contexts(self):
        return (c for c in []) # TODO: best way to return empty generator

    def __len__(self, context=None):
        return len(self._all_triples)
