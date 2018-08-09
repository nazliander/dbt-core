from dbt.api import APIObject
from dbt.contracts.graph.unparsed import UNPARSED_NODE_CONTRACT
from dbt.contracts.graph.parsed import PARSED_NODE_CONTRACT, \
    PARSED_MACRO_CONTRACT, PARSED_DOCUMENTATION_CONTRACT, ParsedNode
from dbt.contracts.graph.compiled import COMPILED_NODE_CONTRACT, CompiledNode
from dbt.exceptions import ValidationException
from dbt.node_types import NodeType
from dbt.logger import GLOBAL_LOGGER as logger
import dbt.utils

# We allow either parsed or compiled nodes, as some 'compile()' calls in the
# runner actually just return the original parsed node they were given.
COMPILE_RESULT_NODE_CONTRACT = {
    'anyOf': [PARSED_NODE_CONTRACT, COMPILED_NODE_CONTRACT]
}


COMPILE_RESULT_NODES_CONTRACT = {
    'type': 'object',
    'additionalProperties': False,
    'description': (
        'A collection of the parsed nodes, stored by their unique IDs.'
    ),
    'patternProperties': {
        '.*': COMPILE_RESULT_NODE_CONTRACT
    },
}


PARSED_MACROS_CONTRACT = {
    'type': 'object',
    'additionalProperties': False,
    'description': (
        'A collection of the parsed macros, stored by their unique IDs.'
    ),
    'patternProperties': {
        '.*': PARSED_MACRO_CONTRACT
    },
}


PARSED_DOCUMENTATIONS_CONTRACT = {
    'type': 'object',
    'additionalProperties': False,
    'description': (
        'A collection of the parsed docs, stored by their uniqe IDs.'
    ),
    'patternProperties': {
        '.*': PARSED_DOCUMENTATION_CONTRACT,
    },
}


NODE_EDGE_MAP = {
    'type': 'object',
    'additionalProperties': False,
    'description': 'A map of node relationships',
    'patternProperties': {
        '.*': {
            'type': 'array',
            'items': {
                'type': 'string',
                'description': 'A node name',
            }
        }
    }
}


PARSED_MANIFEST_CONTRACT = {
    'type': 'object',
    'additionalProperties': False,
    'description': (
        'The full parsed manifest of the graph, with both the required nodes'
        ' and required macros.'
    ),
    'properties': {
        'nodes': COMPILE_RESULT_NODES_CONTRACT,
        'macros': PARSED_MACROS_CONTRACT,
        'docs': PARSED_DOCUMENTATIONS_CONTRACT,
        'generated_at': {
            'type': 'string',
            'format': 'date-time',
        },
        'parent_map': NODE_EDGE_MAP,
        'child_map': NODE_EDGE_MAP,
    },
    'required': ['nodes', 'macros', 'docs'],
}


class CompileResultNode(CompiledNode):
    SCHEMA = COMPILE_RESULT_NODE_CONTRACT


def build_edges(nodes):
    """Build the forward and backward edges on the given list of ParsedNodes
    and return them as two separate dictionaries, each mapping unique IDs to
    lists of edges.
    """
    backward_edges = {}
    # pre-populate the forward edge dict for simplicity
    forward_edges = {node.unique_id: [] for node in nodes}
    for node in nodes:
        backward_edges[node.unique_id] = node.depends_on_nodes[:]
        for unique_id in node.depends_on_nodes:
            forward_edges[unique_id].append(node.unique_id)
    return forward_edges, backward_edges


class Manifest(APIObject):
    SCHEMA = PARSED_MANIFEST_CONTRACT
    """The manifest for the full graph, after parsing and during compilation.
    Nodes may be either ParsedNodes or CompiledNodes or a mix, depending upon
    the current state of the compiler. Macros will always be ParsedMacros and
    docs will always be ParsedDocumentations.
    """
    def __init__(self, nodes, macros, docs, generated_at):
        """The constructor. nodes and macros are dictionaries mapping unique
        IDs to ParsedNode/CompiledNode and ParsedMacro objects, respectively.
        docs is a dictionary mapping unique IDs to ParsedDocumentation objects.
        generated_at is a text timestamp in RFC 3339 format.
        """
        self.nodes = nodes
        self.macros = macros
        self.docs = docs
        self.generated_at = generated_at
        self._contents = {}
        super(Manifest, self).__init__()

    def serialize(self):
        """Convert the parsed manifest to a nested dict structure that we can
        safely serialize to JSON.
        """
        forward_edges, backward_edges = build_edges(self.nodes.values())

        return {
            'nodes': {k: v.serialize() for k, v in self.nodes.items()},
            'macros': {k: v.serialize() for k, v in self.macros.items()},
            'docs': {k: v.serialize() for k, v in self.docs.items()},
            'parent_map': backward_edges,
            'child_map': forward_edges,
            'generated_at': self.generated_at,
        }

    def _find_by_name(self, name, package, subgraph, nodetype):
        """

        Find a node by its given name in the appropriate sugraph. If package is
        None, all pacakges will be searched.
        nodetype should be a list of NodeTypes to accept.
        """
        if subgraph == 'nodes':
            search = self.nodes
        elif subgraph == 'macros':
            search = self.macros
        else:
            raise NotImplementedError(
                'subgraph search for {} not implemented'.format(subgraph)
            )
        return dbt.utils.find_in_subgraph_by_name(
            search,
            name,
            package,
            nodetype)

    def find_docs_by_name(self, name, package=None):
        for unique_id, doc in self.docs.items():
            parts = unique_id.split('.')
            if len(parts) != 2:
                msg = "documentation names cannot contain '.' characters"
                dbt.exceptions.raise_compiler_error(msg, doc)

            found_package, found_node = parts

            if (name == found_node and package in {None, found_package}):
                return doc
        return None

    def find_operation_by_name(self, name, package):
        """Find a macro in the graph by its name and package name, or None for
        any package.
        """
        return self._find_by_name(name, package, 'macros',
                                  [NodeType.Operation])

    def find_macro_by_name(self, name, package):
        """Find a macro in the graph by its name and package name, or None for
        any package.
        """
        return self._find_by_name(name, package, 'macros', [NodeType.Macro])

    def find_refable_by_name(self, name, package):
        """Find any valid target for "ref()" in the graph by its name and
        package name, or None for any package.
        """
        return self._find_by_name(name, package, 'nodes', NodeType.refable())

    def get_materialization_macro(self, materialization_name,
                                  adapter_type=None):
        macro_name = dbt.utils.get_materialization_macro_name(
            materialization_name=materialization_name,
            adapter_type=adapter_type,
            with_prefix=False)

        macro = self.find_macro_by_name(
            macro_name,
            None)

        if adapter_type not in ('default', None) and macro is None:
            macro_name = dbt.utils.get_materialization_macro_name(
                materialization_name=materialization_name,
                adapter_type='default',
                with_prefix=False)
            macro = self.find_macro_by_name(
                macro_name,
                None)

        return macro

    def add_nodes(self, new_nodes):
        """Add the given dict of new nodes to the manifest."""
        for unique_id, node in new_nodes.items():
            if unique_id in self.nodes:
                raise_duplicate_resource_name(node, self.nodes[unique_id])
            self.nodes[unique_id] = node

    def patch_nodes(self, patches):
        """Patch nodes with the given dict of patches. Note that this consumes
        the input!
        """
        # because we don't have any mapping from node _names_ to nodes, and we
        # only have the node name in the patch, we have to iterate over all the
        # nodes looking for matching names. We could use _find_by_name if we
        # were ok with doing an O(n*m) search (one nodes scan per patch)
        for node in self.nodes.values():
            if node.resource_type != NodeType.Model:
                continue
            patch = patches.pop(node.name, None)
            if not patch:
                continue
            node.patch(patch)

        # log debug-level warning about nodes we couldn't find
        if patches:
            for patch in patches.values():
                # since patches aren't nodes, we can't use the existing
                # target_not_found warning
                logger.debug((
                    'WARNING: Found documentation for model "{}" which was '
                    'not found or is disabled').format(patch.name)
                )

    def to_flat_graph(self):
        """Convert the parsed manifest to the 'flat graph' that the compiler
        expects.

        Kind of hacky note: everything in the code is happy to deal with
        macros as ParsedMacro objects (in fact, it's been changed to require
        that), so those can just be returned without any work. Nodes sadly
        require a lot of work on the compiler side.

        Ideally in the future we won't need to have this method.
        """
        return {
            'nodes': {k: v.to_shallow_dict() for k, v in self.nodes.items()},
            'macros': self.macros,
        }

    def __getattr__(self, name):
        raise AttributeError("'{}' object has no attribute '{}'".format(
            type(self).__name__, name)
        )