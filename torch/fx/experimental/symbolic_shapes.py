import torch
from typing import Set, Dict, List, Type, Optional, cast
import sys
import itertools
import operator
import builtins
import math
import functools
import threading
from contextlib import contextmanager
from functools import lru_cache
import traceback
import collections
import textwrap
import logging

# NB: The sym_* functions are used via getattr() and must be imported here.
from torch import SymInt, SymFloat, sym_float, sym_int  # noqa: F401
from torch._guards import ShapeGuard

log = logging.getLogger(__name__)

try:
    import sympy  # type: ignore[import]
    from sympy.printing.precedence import precedence  # type: ignore[import]
    from sympy.printing.str import StrPrinter  # type: ignore[import]
    HAS_SYMPY = True
except ImportError:
    HAS_SYMPY = False

aten = torch._ops.ops.aten  # type: ignore[has-type]

__all__ = [
    "has_symbolic_sizes_strides", "create_contiguous", "ShapeEnv",
    "SymDispatchMode", "FloorDiv", "guard_int", "wrap_node",
]

SYM_FUNCTION_MODE = None

# We don't bother with the metaclass as all of the dispatching logic happens
# entirely from Python
#
# Didn't bother with ancestors for now, unlikely to have multiple modes for
# symints right now


# SymDispatchMode gets invoked whenever an operation is processed on
# a PySymInt.  When this occurs, you get called at __sym_dispatch__
# with the operation in question.  This is symmetric to TorchDispatchMode
# but with some caveats:
#
#   - In TorchDispatchMode, you get the same arguments as what a user
#     invoked your API with; e.g., if you call torch.ops.aten.foo(a, b),
#     you get (a, b) as args to your call.  In SymDispatchMode, if
#     you call a + b (where a and b are SymInts), you will get
#     (a.get_pyobj(), b.get_pyobj()) as your args (these are PySymInts)
#
#   - SymInt/PySymInt don't have FX proxy support (unlike, e.g., Tensor).
#     So you have to manually call Tracer/create_node to write into
#     the graph.  See ProxySymDispatchMode for an example
#
class SymDispatchMode:
    def __sym_dispatch__(self, func, types, args, kwargs):
        raise NotImplementedError()

    def __enter__(self):
        global SYM_FUNCTION_MODE
        old = SYM_FUNCTION_MODE
        if hasattr(self, "inner"):
            raise RuntimeError(f"{self} has already been used as a mode. Please use a fresh version")
        else:
            self.inner = old
        SYM_FUNCTION_MODE = self
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        global SYM_FUNCTION_MODE
        SYM_FUNCTION_MODE = self.inner

def has_symbolic_sizes_strides(elem):
    return elem._has_symbolic_sizes_strides

def create_contiguous(shape):
    strides = [1]
    for dim in reversed(shape[:-1]):
        strides.append(dim * strides[-1])
    return list(reversed(strides))

def _handle_sym_dispatch(func, args, kwargs):
    global SYM_FUNCTION_MODE
    mode = SYM_FUNCTION_MODE
    assert mode
    SYM_FUNCTION_MODE = mode.inner
    try:
        # TODO: properly compute types
        types: List[Type] = []
        return mode.__sym_dispatch__(func, types, args, kwargs)
    finally:
        SYM_FUNCTION_MODE = mode

def guard_int(a):
    if isinstance(a, SymInt):
        return a.node.guard_int("", 0)  # NB: uses Python backtrace
    assert isinstance(a, int)
    return a

# Drop in replacement for math.sqrt
def sym_sqrt(a):
    if hasattr(a, '__sym_sqrt__'):
        return a.__sym_sqrt__()
    return math.sqrt(a)

def to_node(self, num):
    if isinstance(num, (SymInt, SymFloat)):
        return num.node
    elif isinstance(num, int):
        return self.wrap_int(num)
    elif isinstance(num, float):
        return self.wrap_float(num)
    else:
        # NotImplemented is important so that Python tries the
        # other magic method
        return NotImplemented

# Given a GraphModule, return all the FakeTensors for all the placeholders
def fx_placeholder_vals(gm):
    return [n.meta['val'] for n in gm.graph.nodes if n.op == "placeholder"]

# Given a GraphModule and arguments to run it with, evaluate that the guards
# for its associated ShapeEnv are satisfied by the passed arguments.  This
# WILL check for duck sizing.
def eval_guards(gm, *args):
    return gm.shape_env.evaluate_guards_for_args(fx_placeholder_vals(gm), args)

def bind_symbols(gm, *args):
    return gm.shape_env.bind_symbols(fx_placeholder_vals(gm), args)

# TODO: An incomplete list
# 1. Set variables to be equal when we do equality
# 2. Specialize on 0/1 when we do subtraction
class SymNode:
    """
    This is a type erased SymInt/SymFloat which we use to do actual operations.
    End users don't touch this.  Magic methods are NOT defined on this object.
    """
    def __init__(self, expr, shape_env, pytype, constant=None):
        self._expr = expr
        self.shape_env = shape_env
        self.pytype = pytype
        self.constant = constant

    @property
    def expr(self):
        self._update_expr()
        return self._expr

    def _update_expr(self):
        self._expr = self.shape_env.replace(self._expr)

    def is_int(self):
        return self.pytype is int

    def is_float(self):
        return self.pytype is float

    def wrap_int(self, num):
        assert isinstance(num, int)
        return SymNode(sympy.Integer(num), self.shape_env, int, constant=num)

    def wrap_float(self, num):
        assert isinstance(num, float)
        return SymNode(sympy.Float(num), self.shape_env, float, constant=num)

    def clone(self):
        return self

    def str(self):
        return f"{self.expr}"

    def __str__(self):
        return self.str()

    def __repr__(self):
        return self.str()

    # These methods are metaprogrammed in below
    def sym_int(self) -> "SymNode":  # noqa: F811
        ...

    def sym_float(self) -> "SymNode":  # noqa: F811
        ...

    # Today we error on calling int on a symbolic shape, as this is a very accessible footgun.
    def int_(self):
        raise RuntimeError("Trying to extract a concrete int out of a symbolic int")

    # You can manually trigger a guard with this function
    def guard_int(self, file, line):
        # TODO: use the file/line for some useful diagnostic on why a
        # guard occurred
        return int(self.shape_env.evaluate_expr(self.expr))

    def guard_float(self, file, line):
        # TODO: use the file/line for some useful diagnostic on why a
        # guard occurred
        return float(self.shape_env.evaluate_expr(self.expr))

    def bool_(self):
        return bool(self.shape_env.evaluate_expr(self.shape_env.replace(self.expr)))


if HAS_SYMPY:
    class FloorDiv(sympy.Function):
        """
        We maintain this so that:
        1. We can use divisibility guards to simplify FloorDiv(a, b) to a / b.
        2. Printing out the expression is nicer (compared to say, representing a//b as (a - a % b) / b)
        """
        nargs = (2,)

        def _sympystr(self, printer):
            lhs = self.args[0]
            rhs = self.args[1]
            lhs_str = printer._print(lhs)
            rhs_str = printer._print(rhs)
            if precedence(lhs) < precedence(sympy.div):
                lhs_str = f"({lhs_str})"
            if precedence(rhs) < precedence(sympy.div):
                rhs_str = f"({rhs_str})"

            return f"{lhs_str}//{rhs_str}"

        @classmethod
        def eval(cls, base, divisor):
            if base == 0:
                return sympy.Integer(0)
            if divisor == 1:
                return base
            if isinstance(base, sympy.Integer) and isinstance(divisor, sympy.Integer):
                return base // divisor
            if isinstance(base, FloorDiv):
                return FloorDiv(base.args[0], base.args[1] * divisor)

            gcd = sympy.gcd(base, divisor)
            if gcd != 1:
                return FloorDiv(
                    sympy.simplify(base / gcd), sympy.simplify(divisor / gcd)
                )

# Methods that have a `__foo__` as well as `__rfoo__`
reflectable_magic_methods = {
    'add': lambda a, b: a + b,
    'sub': lambda a, b: a - b,
    'mul': lambda a, b: a * b,
    'mod': lambda a, b: a % b,
    'pow': lambda a, b: a ** b,
    'truediv': lambda a, b: a / b,
    'floordiv': lambda a, b: FloorDiv(a, b),
}

magic_methods = {
    **reflectable_magic_methods,
    'eq': lambda a, b: sympy.Eq(a, b),
    'gt': lambda a, b: sympy.Gt(a, b),
    'lt': lambda a, b: sympy.Lt(a, b),
    'le': lambda a, b: sympy.Le(a, b),
    'ge': lambda a, b: sympy.Ge(a, b),
    'floor': lambda a: sympy.floor(a),
    'sym_float': lambda a: a,  # Cannot use sympy.Float(a) here, coz it expects python literals
    'ceil': lambda a: sympy.ceiling(a),
    'neg': lambda a: -a,
    'min': lambda a, b: sympy.Min(a, b),
    'max': lambda a, b: sympy.Max(a, b),
    'sym_sqrt': lambda a: sympy.sqrt(a),
}

unary_magic_methods = {
    'sym_float',
    'ceil',
    'floor',
    'neg',
    'sym_sqrt',
}

magic_methods_on_builtins = {"min", "max"}
magic_methods_on_math = {"ceil", "floor"}
magic_methods_on_submodule = {"sym_float", "sym_sqrt"}

always_float_magic_methods = {"truediv", "sym_float", "sym_sqrt"}
always_int_magic_methods = {"ceil", "floor"}
always_bool_magic_methods = {"eq", "gt", "lt", "le", "ge"}

def wrap_node(x):
    # TODO: let C++ also take advantage of this
    if isinstance(x, SymNode) and x.constant is not None:
        return x.constant
    if x.is_int():
        return SymInt(x)
    elif x.is_float():
        return SymFloat(x)
    else:
        raise AssertionError(f"unrecognized return type {x}")

def _make_node_magic(method, func):
    func = lru_cache(256)(func)

    def binary_magic_impl(self, other):
        if method in magic_methods_on_builtins:
            op = getattr(builtins, method)
        else:
            op = getattr(operator, method)
        if SYM_FUNCTION_MODE:
            r = _handle_sym_dispatch(op, (wrap_node(self), wrap_node(other)), {})
            assert isinstance(r, (SymInt, SymFloat)), type(r)
            return r.node
        assert isinstance(other, SymNode)
        other_expr = other.expr
        # TODO: consider constant prop here
        expr = self.shape_env.replace(self.expr)
        other_expr = self.shape_env.replace(other_expr)
        try:
            out = func(expr, other_expr)
        except Exception:
            log.warning(f"failed to eval {method}({expr}, {other_expr})")
            raise
        out = sympy.expand(out)
        pytype: Type
        if method in always_float_magic_methods:
            pytype = float
        else:
            pytype = self.pytype

        # TODO: relational operators actually technically return a
        # PySymBool, this is a type error
        return SymNode(out, self.shape_env, pytype)

    def unary_magic_impl(self):
        if SYM_FUNCTION_MODE:
            if method in magic_methods_on_math:
                op = getattr(math, method)
            elif method in magic_methods_on_submodule:
                op = getattr(sys.modules[__name__], method)
            else:
                op = getattr(operator, method)
            r = _handle_sym_dispatch(op, (wrap_node(self),), {})
            assert isinstance(r, (SymInt, SymFloat)), type(r)
            return r.node
        # TODO: consider constant prop here
        expr = self.shape_env.replace(self.expr)
        try:
            out = func(expr)
        except Exception:
            log.warning(f"failed to eval {method}({expr})")
            raise
        out = sympy.expand(out)
        pytype: Type
        if method in always_int_magic_methods:
            pytype = int
        elif method in always_float_magic_methods:
            pytype = float
        else:
            pytype = self.pytype

        return SymNode(out, self.shape_env, pytype)

    if method in unary_magic_methods:
        setattr(SymNode, method, unary_magic_impl)
    else:
        setattr(SymNode, method, binary_magic_impl)

for method, func in magic_methods.items():
    _make_node_magic(method, func)

def _make_user_magic(method, user_type):
    # User magic takes care of wrapping the other operand into a node,
    # so that our internal logic can assume everything is nodes

    def unary_magic_impl(self):
        return wrap_node(getattr(self.node, method)())

    def binary_magic_impl(self, other):
        other_node = to_node(self.node, other)
        if other_node is NotImplemented:
            return NotImplemented
        return wrap_node(getattr(self.node, method)(other_node))

    def rbinary_magic_impl(self, other):
        other_node = to_node(self.node, other)
        if other_node is NotImplemented:
            return NotImplemented
        return wrap_node(getattr(other_node, method)(self.node))

    if method in unary_magic_methods:
        setattr(user_type, f"__{method}__", unary_magic_impl)
    else:
        setattr(user_type, f"__{method}__", binary_magic_impl)
        if method in reflectable_magic_methods:
            setattr(user_type, f"__r{method}__", rbinary_magic_impl)

for method, func in magic_methods.items():
    _make_user_magic(method, SymInt)
    _make_user_magic(method, SymFloat)

del method
del func

def _lru_cache(fn, maxsize=None):
    """
    Wrapper around lru_cache that clears when new info about shapes has been
    updated.

    Use lru_cache if the output is always the same, regardless of the
    constraints we know now (i.e. evaluate_expr)

    Use _lru_cache otherwise.
    """
    fn_cache = lru_cache(maxsize)(fn)
    prior_key = None

    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        nonlocal prior_key
        if prior_key != self._get_key():
            prior_key = self._get_key()
            fn_cache.cache_clear()
        return fn_cache(self, *args, **kwargs)

    wrapper.cache_info = fn_cache.cache_info  # type: ignore[attr-defined]
    return wrapper


if HAS_SYMPY:
    # This stub exists so we can easily add metadata to sympy symbols
    # NB: This inherits from Dummy, not Symbol, because Symbols with the same
    # name get interned.  This is bad for us as we want the metadata (snames)
    # to vary across different invocations and not leak.
    class Symbol(sympy.Dummy):
        __slots__: List[str] = ['snames', 'stack']
        snames: List[str]
        stack: Optional[str]

        def __new__(cls, *args, **kwargs):
            self = super().__new__(cls, *args, **kwargs)
            self.snames = []
            self.stack = None
            return self


    class ShapeGuardPrinter(StrPrinter):
        def __init__(
            self,
            symbol_to_source,
        ):
            super().__init__()
            self.symbol_to_source = symbol_to_source

        def _print_Symbol(self, expr) -> str:
            assert isinstance(expr, Symbol), str(type(expr))
            assert expr in self.symbol_to_source, f"{expr} (could be from {expr.snames}) not in {self.symbol_to_source}"
            return self.symbol_to_source[expr][0]



class ShapeEnv(object):
    def __init__(self):
        self.guards: List[ShapeGuard] = []
        # Maps symbolic ints to their original concrete values
        # Currently populated from tensors
        self.var_to_val: Dict["sympy.Symbol", "sympy.Integer"] = {}
        # Maps from sympy ints to expressions representing them
        # Populated from equality guards (i.e. a.shape[0] == b.shape[0])
        self.replacements: Dict["sympy.Symbol", "sympy.Expr"] = {}  #
        # Set holds a % b expressions that evaluate to 0.
        self.divisible: Set["sympy.Expr"] = set()
        # Duck-shaping says that if two input tensors have the same size,
        # they get assigned the same symbolic variable
        self.val_to_var: Dict[int, "sympy.Expr"] = {0: sympy.Integer(0), 1: sympy.Integer(1)}
        self.tls = threading.local()
        self.unbacked_symfloat_counter = itertools.count()
        self.unbacked_symint_counter = itertools.count()

    def _suppress_guards_tls(self):
        return getattr(self.tls, "suppress_guards", False)

    @contextmanager
    def suppress_guards(self):
        self.tls.suppress_guards = True
        try:
            yield
        finally:
            self.tls.suppress_guards = False

    def _get_key(self):
        """
        Defines the current "state" of the guards we've accumulated in this ShapeEnv.
        Determines when we need to invalidate our cache
        """
        return (len(self.replacements), len(self.divisible))

    def create_symbolic_sizes_strides_storage_offset(self, ex: torch.Tensor, *, sname: str):
        """
        Returns a list of symbolic sizes and strides for the given tensor.
        We try our best to express stride in terms of the sizes, so as to not
        introduce new symbolic variables.
        """
        size = [self.create_symbol(val, sname=f"{sname}.size({i})") for i, val in enumerate(ex.size())]
        stride: List[Optional[sympy.Expr]] = [None] * len(size)
        for i, val in enumerate(ex.stride()):
            if val in (0, 1):
                stride[i] = sympy.Integer(val)
        while any(x is None for x in stride):
            candidates = {
                ex.size(i) * ex.stride()[i]: size[i] * stride[i]
                for i in range(len(size))
                if stride[i] is not None and ex.stride()[i] >= 0
            }
            # iterate over unbound strides in sorted order
            val_list = sorted(
                [(ex.stride()[i], i) for i in range(len(stride)) if stride[i] is None]
            )
            for _, i in val_list:
                if stride[i] is None and ex.stride()[i] in candidates:
                    stride[i] = candidates[ex.stride()[i]]
                    candidates[ex.size(i) * ex.stride()[i]] = size[i] * stride[i]
            if any(x is None for x in stride):
                # bind the smallest unbound stride to a new variable
                val, i = min(
                    [
                        (ex.stride()[i], i)
                        for i in range(len(stride))
                        if stride[i] is None
                    ]
                )
                stride[i] = self.create_symbol(val, sname=f"{sname}.stride({i})")
        assert all(x is not None for x in stride)
        sym_size = [self.create_symintnode(i) for i in size]
        sym_stride = []
        for i, stride_expr in enumerate(stride):
            # NB: Don't duck size the stride; instead use the expression
            # we computed
            assert stride_expr is not None
            sym_stride.append(self.create_symintnode(stride_expr))
        sym_storage_offset = self.create_symintnode(self.create_symbol(ex.storage_offset(), sname=f"{sname}.storage_offset()"))
        return sym_size, sym_stride, sym_storage_offset

    def create_symintnode(self, sym: "sympy.Expr"):
        return SymInt(SymNode(sym, self, int))

    def create_unbacked_symfloat(self):
        symbol = Symbol(f"f{next(self.unbacked_symfloat_counter)}")
        symbol.stack = ''.join(traceback.format_list(traceback.extract_stack()[:-1]))
        return SymFloat(SymNode(symbol, self, float))

    def create_unbacked_symint(self):
        symbol = Symbol(f"i{next(self.unbacked_symint_counter)}", integer=True)
        symbol.stack = ''.join(traceback.format_list(traceback.extract_stack()[:-1]))
        return SymInt(SymNode(symbol, self, int))

    # This is guaranteed to return a symbol or its negation is a sympy.Symbol,
    # but there may be a replacement that allows it to be immediately
    # simplified
    def create_symbol(self, val: int, *, sname: str) -> "sympy.Expr":
        assert isinstance(sname, str), f"{type(sname)} {sname}"

        if not HAS_SYMPY:
            raise RuntimeError("Need sympy installed to create symbolic shapes")

        if val < 0:
            return -self.create_symbol(-val, sname=f"-{sname}")

        # Now attempt to duck size this value
        # TODO: Use site has to duck size
        # TODO: Do this duck sizing lazily later

        # Create a duck sized int if necessary
        if val not in self.val_to_var:
            sympy_expr = Symbol(f"s{len(self.var_to_val)}", positive=True, integer=True)
            self.var_to_val[sympy_expr] = sympy.Integer(val)
            self.val_to_var[val] = sympy_expr

        # This implements duck-shaping: input sizes that match are assigned
        # the same symint
        r = self.duck_int(val)
        if isinstance(r, Symbol):
            r.snames.append(sname)
        return r

    # Given a concrete integer value, return the duck sized symbol associated
    # with it; e.g., suppose we already have a tensor of size 3 in scope,
    # which was assigned s3, then shape_env.duck_int(3) we will get back s3.
    # This has some pretty tricky preconditions associated with it, so if
    # you are in a binding context, you probably wanted create_symbol instead.
    def duck_int(self, val):
        assert val in self.val_to_var, (
            "Direct call to duck_int MUST only duck size an integer values "
            "that have already produced by inputs (allocated "
            "by create_symbol), or we risk being unable to instantiate the "
            "symbolic variable later.  However, at time of this call "
            f"val={val} was not duck sized.  Bound duck sized integers: "
            f"{list(self.val_to_var.keys())}"
        )
        return self.val_to_var[val]

    # Generates a Python string which, when evaluated in a context that
    # defines tensors for all the sources, returns True or False depending
    # on if the guards evaluated to True or not.  Primarily used by Dynamo,
    # but this is also helpful for manual testing of guards (see
    # evaluate_guards_for_args)
    def codegen_guards(self, placeholders, sources):
        # It took a lot of sweat to figure out the algorithm here.  Let's
        # explain how it works.
        #
        # The ShapeEnv lifecycle looks something like this:
        #
        # - For each input, you either generate a fresh Sympy symbol (s0) to
        #   represent its value (a binding site), or you reuse some
        #   preexisting symbol or expression, skipping the symbol allocation
        #   (e.g., duck sizing to a preexisting symbol, or expressing a
        #   stride as a multiplication of a separate stride and size.)
        #   Naively, you might expect to bind a fresh Sympy symbol for
        #   every input, but this is fairly wasteful as most of these
        #   symbols immediately simplify away, and if you don't eagerly
        #   specialize, e.g., 0/1 symbols, you end up with very complicated
        #   expressions that are not optimizable in practice.
        #
        # - You perform some compute on these symbols, occasionally
        #   introducing guards on boolean expressions on these symbols.
        #   In particular, whenever we guard on equality (_maybe_guard_eq),
        #   we can simplify shapes; e.g., when s0 == s1 * 2, we can now
        #   replace all occurrences of s0 with s1 * 2.  Sometimes, a
        #   boolean expression evaluation doesn't introduce a guard, as
        #   the guard is already entailed by the simplifications we have
        #   applied.
        #
        # - In the end, you have a bunch of replacements (saying how to
        #   simplify shapes) and a bunch of guards (all the equality guards
        #   are trivial, because they're covered by the replacements).
        #
        # From the ShapeEnv, we must generate a Python expression that, when
        # evaluated on a set of inputs, tells us whether or not these boolean
        # expressions would have evaluated in the same way.  However,
        # we cannot easily compute this, as we elide recording boolean
        # expressions when we think they are vacuously true.  Thus, we seek
        # an approximation: we must generate an expression, if true, would have
        # produced an "equivalent" ShapeEnv, which would answer guard
        # expressions in the same way.
        #
        # Our notion of equivalence is a bit subtle.  For example, consider
        # the ShapeEnv created from an input of size (5, 4) versus (4, 4)
        # (no other guards.)  Duck sizing would generate (s0, s1) in the first
        # case but (s0, s0) in the second.  We do NOT assume that size
        # variables are disjoint; so in fact a graph that assumes the input
        # could be (s0, s1) subsumes (s0, s0) (setting s0 == s1), but not
        # vice versa.  However, consider an analogous case (1,) versus (2,).
        # Duck sizing generates (1,) and (s0,); the (s0,) graph does NOT
        # subsume the (1,) graph because we assume that any size variables
        # is NOT 0/1 (and make simplifications according to this; e.g., if
        # we queried s0 == 0, we would immediately return False without
        # returning a guard.)
        #
        # So, it is perhaps easier to flip things on their head: the guard
        # expressions we generate here say what simplifications are valid,
        # and what are not.  Below, we explain each of the guard expressions
        # we generate

        # TODO: Make this more efficient by binding all the size/stride/offsets
        # to locals before performing tests on them.

        # Actual codegen must be delayed as we don't necessarily know what
        # the symbol mapping is
        input_guards = []

        symbol_to_source = collections.defaultdict(list)

        # How do we know what the value of s0 is?  Fresh variables can only be
        # bound by inputs, so there MUST be some other input which binds the
        # variable.  If there is no such input, this is an error in our
        # system.  We record where all symbols come from, to help you diagnose
        # why those symbols didn't occur.
        #
        # In fact, generally speaking it is only possible for the "outermost"
        # user of a ShapeEnv to evaluate the guards, because some inputs may
        # not be available to inner levels.  For example, Dynamo can guard on
        # tensors that never actually become graph arguments (they are
        # pruned).  In this case, only Dynamo knows about these arguments.
        def track_symint(source, val):
            if isinstance(val, SymInt):
                s = val.node.expr

                if isinstance(s, sympy.Symbol):
                    symbol_to_source[s].append(source)
                elif isinstance(-s, sympy.Symbol):
                    symbol_to_source[-s].append(f"-{source}")

                input_guards.append((source, s))
            else:
                input_guards.append((source, sympy.Integer(val)))

        for t, source in zip(placeholders, sources):
            if t is None:
                continue
            if isinstance(t, SymInt):
                track_symint(source, t)
                continue
            assert isinstance(t, torch.Tensor)
            # TODO: size(i)/stride(i) more efficient
            for i, s in enumerate(t.size()):
                track_symint(f"{source}.size()[{i}]", s)
            for i, s in enumerate(t.stride()):
                track_symint(f"{source}.stride()[{i}]", s)
            track_symint(f"{source}.storage_offset()", t.storage_offset())

        # 1. Every input must equal the final simplified symbolic expression
        #    stored on the placeholder.  Given a placeholder (s0*2, s1),
        #    if we have an input (2, 3), we must show s0*2 == 2 and s1 == 3.
        #    This does a lot of work: it covers duck sizing and equality guards.
        exprs = []
        for source, expr in input_guards:
            sexpr = ShapeGuardPrinter(symbol_to_source).doprint(expr)
            # Small optimization
            if source == sexpr:
                continue
            exprs.append(f"{source} == {sexpr}")

        # 2. Every guard must evaluate to True (but remember many guards
        #    like s0 == s1*2 because trivial due to simplification)
        for g, tb in self.guards:
            if self._maybe_evaluate_static(g) is not None:
                continue
            g = self.simplify(g)
            try:
                exprs.append(ShapeGuardPrinter(symbol_to_source).doprint(g))
            except Exception:
                log.warning(f"Failing guard allocated at: \n{tb}")
                raise

        # 3. Every symbol must not be equal to 0/1
        for sources in symbol_to_source.values():
            assert sources
            # We must assert that each symbol is not zero or one, as we make
            # negative inferences on shape variables
            exprs.append(f"{sources[0]} != 0 and {sources[0]} != 1")

        if exprs:
            return " and ".join(exprs)
        else:
            return "True"

    def evaluate_guards_for_args(self, placeholders, args):
        arg_names = [f"t{i}" for i in range(len(args))]
        code = self.codegen_guards(placeholders, arg_names)
        return eval(code, {}, dict(zip(arg_names, args)))

    def bind_symbols(self, placeholders, args):
        # Given a paired list of placeholders (fake tensors with
        # symbolic sizes) and concrete arguments (regular tensors
        # with real sizes), returns a dictionary mapping each
        # symbol to its real value.  So for example, if you
        # have a placeholder with size (s0, s1), binding
        # (2, 4) to it will give you {s0: 2, s1: 4}.  This is
        # not guaranteed to bind ALL symbols in the ShapeEnv;
        # we can't bind a symbol if it doesn't occur in any placeholder,
        # and symbols that already have replacements won't get bindings.

        # This is a little duplicative with evaluate_guards but
        # it's different enough that it seemed cleanest to make
        # another copy.  This assumes the guards are already checked,
        # though if it's cheap we'll check for shenanigans
        bindings: Dict[sympy.Symbol, int] = {}

        def bind_symint(arg, val):
            if isinstance(val, SymInt):
                s = val.node.expr

                if isinstance(s, sympy.Symbol):
                    if s in bindings:
                        assert bindings[s] == arg, f"{bindings[s]} != {arg}"
                    else:
                        bindings[s] = arg
                elif isinstance(-s, sympy.Symbol):
                    if -s in bindings:
                        assert bindings[-s] == -arg, f"{bindings[-s]} != {-arg}"
                    else:
                        bindings[-s] = -arg

        for t, arg in zip(placeholders, args):
            if t is None:
                continue
            if isinstance(t, SymInt):
                bind_symint(arg, t)
                continue
            assert isinstance(t, torch.Tensor)
            for i, s in enumerate(t.size()):
                bind_symint(arg.size(i), s)
            for i, s in enumerate(t.stride()):
                bind_symint(arg.stride(i), s)
            bind_symint(arg.storage_offset(), t.storage_offset())

        return bindings

    def get_nontrivial_guards(self):
        return [self.simplify(guard.expr) for guard in self.guards if self._maybe_evaluate_static(guard.expr) is None]

    def format_guards(self, verbose=False):
        def format_tb(tb):
            if not verbose:
                return ""
            return f"\n   Guarded at:\n{textwrap.indent(tb, '   ')}"

        return '\n'.join(f" - {guard.expr}{format_tb(guard.stack)}" for guard in self.guards)

    def get_shape_groups(self):
        shape_groups = collections.defaultdict(list)
        for k, v in self.replacements.items():
            shape_groups[v].append(k)
        return shape_groups

    @_lru_cache
    def _maybe_evaluate_static(self, expr: "sympy.Expr") -> "Optional[sympy.Expr]":
        """
        Tries to evaluate expr without introducing guards
        """
        expr = self.simplify(expr)
        # Simplifies assuming that shape vars > 1 (since we cache on 0/1 shape values)
        symbols = list(expr.free_symbols)
        new_shape_env = {
            k: sympy.Symbol(f"shape_{idx}", positive=True, integer=True) + 1
            for idx, k in enumerate(symbols)
            # Do not assume unbacked symints are > 1
            if k in self.var_to_val
        }
        new_expr = expr.xreplace(new_shape_env)
        floor_div_replace = {}
        for atom in new_expr.atoms(FloorDiv):
            floor_div_replace[atom] = sympy.floor(atom.args[0] / atom.args[1])
        new_expr = sympy.expand(new_expr.xreplace(floor_div_replace))
        if len(list(new_expr.free_symbols)) == 0:
            return new_expr
        return None

    @_lru_cache
    def replace(self, expr: "sympy.Expr") -> "sympy.Expr":
        replacements = {s: self._find(cast(sympy.Symbol, s)) for s in expr.free_symbols}
        return sympy.expand(expr.xreplace(replacements))

    @_lru_cache
    def _update_divisible(self):
        new_divisible = set()
        for k in self.divisible:
            res = self.replace(k)
            if len(res.free_symbols) > 0:
                new_divisible.add(k)

        self.divisible = new_divisible

    @_lru_cache
    def simplify(self, expr: "sympy.Expr") -> "sympy.Expr":
        expr = self.replace(expr)
        if expr.has(FloorDiv):
            self._update_divisible()
            div_replacements = {}
            for atom in expr.atoms(FloorDiv):
                base, divisor = atom.args
                if self.replace(base % divisor) in self.divisible:
                    div_replacements[atom] = base / divisor
            expr = expr.xreplace(div_replacements)
            expr = sympy.expand(expr)
        return expr

    @lru_cache(256)
    def size_hint(self, expr: "sympy.Expr"):
        """
        Gets a size hint for a given expression from the underlying shapes we had.
        Does not introduce a guard, so only use this when you can guarantee that
        your code is still valid for arbitrary shapes (such as optimization decisions)
        """
        result_expr = sympy.expand(expr).xreplace(self.var_to_val)
        if len(result_expr.free_symbols) != 0:
            raise self._make_data_dependent_error(result_expr)
        return result_expr

    def _make_data_dependent_error(self, expr):
        # TODO: in a Dynamo context, having user code, and having the
        # name of the local, will be much better
        accesses = '\n\n'.join(
            f"Data dependent variable '{s}' allocated at:\n{s.stack}"
            for s in expr.free_symbols
        )
        return RuntimeError(
            f"\n\n{accesses}\n"
            "RuntimeError: It appears that you're trying to get a value out of symbolic int/float "
            "whose value is data-dependent (and thus we do not know the true value.)  "
            f"The expression we were trying to evaluate is {expr}.  "
            "Scroll up to see where each of these data-dependent accesses originally occurred."
            # TODO: Help text about how to use our runtime tests to fix this
            # problem
        )

    @_lru_cache
    def _find(self, a: "sympy.Symbol") -> "sympy.Expr":
        """
        Implements a DSU-like algorithm to find the variable that represents a
        Also handles transitive non-identity replacements.

        a: b + c
        c: d
        """
        if a not in self.replacements:
            return a
        res = self.replacements[a]
        cur_replace = {s: self._find(s) for s in res.free_symbols}
        self.replacements[a] = self.replacements[a].xreplace(cur_replace)
        return self.replacements[a]

    @lru_cache(256)
    def _maybe_guard_eq(self, expr: "sympy.Eq") -> None:
        """
        Evaluates the result of an eq call. If true, uses information to
        simplify shapes (i.e. a == b or a % 5 == 0)
        """
        concrete_bool = bool(self.size_hint(expr))
        if not concrete_bool:
            return
        free = list(expr.free_symbols)

        assert len(free) > 0, "The expression should not be static by this point"
        # In case of really gnarly expression, we don't blow up
        if len(free) > 5:
            return
        free = sorted(free, key=lambda x: (self.size_hint(x), x.name), reverse=True)  # type: ignore[attr-defined]
        lhs = expr.lhs
        rhs = expr.rhs
        try:
            solutions = sympy.solve(lhs - rhs, free[0], dict=True)
            if len(solutions) != 1:
                return
            solution = solutions[0][free[0]]
            if all(t.is_integer for t in sympy.preorder_traversal(solution)):
                new_var = self._find(solution)
                self.replacements[cast(sympy.Symbol, free[0])] = new_var
        except NotImplementedError:
            if expr.has(sympy.Mod):
                mod_expr = tuple(expr.atoms(sympy.Mod))[0]
                try:
                    solutions = sympy.solve(lhs - rhs, mod_expr, dict=True)
                    if len(solutions) == 1 and solutions[0][mod_expr] == 0:
                        self.divisible.add(mod_expr)
                except NotImplementedError:
                    pass
            return
        except RecursionError:
            log.warning(f"RecursionError in sympy.solve({lhs} - {rhs}, {free[0]})")

    @lru_cache(256)
    def evaluate_expr(self, expr: "sympy.Expr"):
        """
        Given an expression, evaluates it, adding guards if necessary
        """
        if len(expr.free_symbols) == 0:
            return expr
        expr = self.simplify(expr)
        static_expr = self._maybe_evaluate_static(expr)
        if static_expr is not None:
            return static_expr

        if isinstance(expr, sympy.Eq):
            self._maybe_guard_eq(expr)
            # TODO: If we successfully eliminate a symbol via equality, it
            # is not actually necessary to save a guard for the equality,
            # as we will implicitly generate a guard when we match that
            # input against the symbol
        concrete_val = self.size_hint(expr)

        # TODO: optimize this; avoid formatting traces until we need them
        # NB: drop two frames; evaluate_expr and the Sym* function that
        # actually called us
        if not self._suppress_guards_tls():
            stack = ''.join(traceback.format_list(traceback.extract_stack()[:-2]))
            if concrete_val is sympy.true:
                self.guards.append(ShapeGuard(expr, stack))
            elif concrete_val is sympy.false:
                self.guards.append(ShapeGuard(sympy.Not(expr), stack))
            else:
                self.guards.append(
                    ShapeGuard(sympy.Eq(expr, concrete_val), stack))  # type: ignore[arg-type]
        return concrete_val
