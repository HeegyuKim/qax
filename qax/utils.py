from functools import partial, wraps
from itertools import chain

import jax
from jax import core
from jax import tree_util

from . import implicit_array

def vmap_all_but_one(f, axis, out_ndim=0):
    """
    Repeatedly calls vmap to map over all axes except for `axis.`
    All args will be mapped on the same dimensions.
    """
    @wraps(f)
    def inner(*args):
        n_dim = args[0].ndim
        if axis >= n_dim:
            raise ValueError(f'Axis {axis} is out of bounds for array of dimension {n_dim}')
        fn = f
        vmap_dim = 1
        out_dim = out_ndim
        for i in reversed(range(n_dim)):
            if i == axis:
                vmap_dim = 0
                out_dim = 0
            else:
                fn = jax.vmap(fn, vmap_dim, out_dim)
        return fn(*args)
    return inner

def combine_leaf_predicate(base_fn, is_leaf):
    @wraps(base_fn)
    def new_fn(*args, new_is_leaf=None):
        if new_is_leaf is None:
            combined_is_leaf = is_leaf
        else:
            def combined_is_leaf(arg):
                return is_leaf(arg) or new_is_leaf(arg)
        return base_fn(*args, is_leaf=combined_is_leaf)
    return new_fn

leaf_predicate = lambda x: isinstance(x, implicit_array.ImplicitArray)
tree_map_with_implicit = combine_leaf_predicate(jax.tree_map, leaf_predicate)
tree_map_with_path_with_implicit = combine_leaf_predicate(tree_util.tree_map_with_path, leaf_predicate)
tree_flatten_with_implicit = combine_leaf_predicate(tree_util.tree_flatten, leaf_predicate)
tree_flatten_with_path_with_implicit = combine_leaf_predicate(tree_util.tree_flatten_with_path, leaf_predicate)
tree_leaves_with_implicit = combine_leaf_predicate(tree_util.tree_leaves, leaf_predicate)
tree_structure_with_implicit = combine_leaf_predicate(tree_util.tree_structure, leaf_predicate)

def flatten_one_implicit_layer(tree):
    def is_leaf_below_node(node, x):
        return isinstance(x, implicit_array.ImplicitArray) and x is not node

    def replace_subtree_implicits(node):
        return tree_util.tree_map(lambda _: 1, node, is_leaf=partial(is_leaf_below_node, node))

    prototype = tree_map_with_implicit(replace_subtree_implicits, tree)
    struct = tree_util.tree_structure(prototype)

    leaves = tree_leaves_with_implicit(tree)
    leaves = list(chain.from_iterable(
        tree_util.tree_leaves(leaf, is_leaf=partial(is_leaf_below_node, leaf))
        if isinstance(leaf, implicit_array.ImplicitArray) else
        [leaf] for leaf in leaves
    ))
    return leaves, struct

def implicit_depth(tree):
    leaves = tree_leaves_with_implicit(tree)
    depth = 0
    while True:
        next_leaves = []
        any_implicit = False
        for leaf in leaves:
            if not isinstance(leaf, implicit_array.ImplicitArray):
                continue
            any_implicit = True
            next_leaves.extend(flatten_one_implicit_layer(leaf)[0])

        if not any_implicit:
            return depth

        depth += 1
        leaves = next_leaves

def _map_leaves_with_implicit_path(f, leaves, is_leaf, path_prefix=()):
    mapped_leaves = []
    for idx, leaf in enumerate(leaves):
        path = path_prefix + (idx,)
        if not isinstance(leaf, implicit_array.ImplicitArray) or is_leaf(path, leaf):
            mapped_leaves.append(f(path, leaf))
            continue

        subtree, substruct = flatten_one_implicit_layer(leaf)
        mapped_subtree = _map_leaves_with_implicit_path(
            f,
            subtree,
            is_leaf=is_leaf,
            path_prefix=path
        )
        mapped_leaves.append(tree_util.tree_unflatten(substruct, mapped_subtree))
    return mapped_leaves

def _get_pruning_transform(tree, materialization_paths):
    if not materialization_paths:
        return lambda x: x
    def is_leaf(path, leaf):
        return path in materialization_paths

    def f(path, node):
        while isinstance(node, implicit_array.ImplicitArray):
            node = node._materialize()
        return node

    def materialize_subtrees(tree):
        leaves, struct = tree_flatten_with_implicit(tree)
        mapped_leaves =  _map_leaves_with_implicit_path(f, leaves, is_leaf)
        return tree_util.tree_unflatten(struct, mapped_leaves)

    return materialize_subtrees

class NodeAndPath:
    def __init__(self, node, path):
        self.node = node
        self.path = path

def get_common_prefix_transforms(trees):
    """
    Given an iterable of pytrees which have the same structure after all
    ImplicitArray instances are materialized, return a list of callables
    which will transform each tree into the largest common structure
    obtainable via materialization of ImplicitArrays.
    """
    if len(trees) <= 1:
        return [lambda x: x for _ in trees]

    all_leaves, structures = zip(*(tree_flatten_with_implicit(tree) for tree in trees))
    post_materialization_avals = [core.get_aval(leaf) for leaf in all_leaves[0]]
    for i, (leaves, structure) in enumerate(zip(all_leaves[1:], structures[1:]), 1):
        if structure != structures[0]:
            raise ValueError('Trees do not have the same structure after materialization')

        for leaf, expected_aval in zip(leaves, post_materialization_avals):
            aval = core.get_aval(leaf)
            if not (aval.shape == expected_aval.shape and aval.dtype == expected_aval.dtype):
                raise ValueError(
                    f'Trees do not have the same avals after materialization. Tree 0: {expected_aval}, Tree {i}: {aval}'
                )

    # Stack will contain tuples of (path, nodes)
    # path = a sequence of integers specifying which child
    # was taken at each _flatten_one_implicit_layer call
    # or the first flatten_with_implicit call
    # nodes = one node from each tree
    stack = []

    all_leaves = []
    for tree in trees:
        all_leaves.append(tree_leaves_with_implicit(tree))

    for i, nodes in enumerate(zip(*all_leaves)):
        stack.append(((i,), nodes))

    materialization_paths = set()
    while stack:
        path_prefix, nodes = stack.pop()
        if not any(isinstance(node, implicit_array.ImplicitArray) for node in nodes):
               continue

        all_leaves, all_structures = zip(*(
            flatten_one_implicit_layer(node) for node in nodes
        ))
        node_structures = set(all_structures)
        if len(node_structures) > 1:
            materialization_paths.add(path_prefix)
            continue

        aval_diff = False
        for leaves in zip(*all_leaves):
            first_aval = core.get_aval(leaves[0])
            shape = first_aval.shape
            dtype = first_aval.dtype
            for leaf in leaves[1:]:
                aval = core.get_aval(leaf)
                if not (aval.shape == shape and aval.dtype == dtype):
                    materialization_paths.add(path_prefix)
                    aval_diff = True
            if aval_diff:
                break

        if aval_diff:
            continue

        for i, leaf_group in enumerate(zip(*all_leaves)):
            stack.append((path_prefix + (i,), leaf_group))

    return [_get_pruning_transform(tree, materialization_paths) for tree in trees]
