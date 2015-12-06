"""
This file implements the code-generator for parallel-vectorize.

ParallelUFunc is the platform independent base class for generating
the thread dispatcher.  This thread dispatcher launches threads
that execute the generated function of UFuncCore.
UFuncCore is subclassed to specialize for the input/output types.
The actual workload is invoked inside the function generated by UFuncCore.
UFuncCore also defines a work-stealing mechanism that allows idle threads
to steal works from other threads.
"""
from __future__ import print_function, absolute_import
import sys
import os
import multiprocessing
import numpy as np
import llvmlite.llvmpy.core as lc
import llvmlite.binding as ll
from numba.npyufunc import ufuncbuilder
from numba.numpy_support import as_dtype
from numba import types, utils, cgutils

NUM_CPU = max(1, multiprocessing.cpu_count())


class ParallelUFuncBuilder(ufuncbuilder.UFuncBuilder):
    def build(self, cres, sig):
        _launch_threads()

        # Buider wrapper for ufunc entry point
        ctx = cres.target_context
        signature = cres.signature
        library = cres.library
        llvm_func = library.get_function(cres.fndesc.llvm_func_name)
        wrapper = build_ufunc_wrapper(library, ctx, llvm_func, signature)
        ptr = library.get_pointer_to_function(wrapper.name)
        # Get dtypes
        dtypenums = [np.dtype(a.name).num for a in signature.args]
        dtypenums.append(np.dtype(signature.return_type.name).num)
        keepalive = ()
        return dtypenums, ptr, keepalive


def build_ufunc_wrapper(library, ctx, lfunc, signature):
    innerfunc = ufuncbuilder.build_ufunc_wrapper(library, ctx, lfunc, signature,
                                                 objmode=False, env=None,
                                                 envptr=None)
    lfunc = build_ufunc_kernel(library, ctx, innerfunc, signature)
    library.add_ir_module(lfunc.module)
    return lfunc


def build_ufunc_kernel(library, ctx, innerfunc, sig):
    """Wrap the original CPU ufunc with a parallel dispatcher.

    Args
    ----
    ctx
        numba's codegen context

    innerfunc
        llvm function of the original CPU ufunc

    sig
        type signature of the ufunc

    Details
    -------

    Generate a function of the following signature:

    void ufunc_kernel(char **args, npy_intp *dimensions, npy_intp* steps,
                      void* data)

    Divide the work equally across all threads and let the last thread take all
    the left over.


    """
    # Declare types and function
    byte_t = lc.Type.int(8)
    byte_ptr_t = lc.Type.pointer(byte_t)

    intp_t = ctx.get_value_type(types.intp)

    fnty = lc.Type.function(lc.Type.void(), [lc.Type.pointer(byte_ptr_t),
                                             lc.Type.pointer(intp_t),
                                             lc.Type.pointer(intp_t),
                                             byte_ptr_t])

    mod = library.create_ir_module('parallel.ufunc.wrapper')
    lfunc = mod.add_function(fnty, name=".kernel")
    innerfunc = mod.add_function(fnty, name=innerfunc.name)

    bb_entry = lfunc.append_basic_block('')

    # Function body starts
    builder = lc.Builder.new(bb_entry)

    args, dimensions, steps, data = lfunc.args

    # Distribute work
    total = builder.load(dimensions)
    ncpu = lc.Constant.int(total.type, NUM_CPU)

    count = builder.udiv(total, ncpu)

    count_list = []
    remain = total

    for i in range(NUM_CPU):
        space = builder.alloca(intp_t)
        count_list.append(space)

        if i == NUM_CPU - 1:
            # Last thread takes all leftover
            builder.store(remain, space)
        else:
            builder.store(count, space)
            remain = builder.sub(remain, count)

    # Array count is input signature plus 1 (due to output array)
    array_count = len(sig.args) + 1

    # Get the increment step for each array
    steps_list = []
    for i in range(array_count):
        ptr = builder.gep(steps, [lc.Constant.int(lc.Type.int(), i)])
        step = builder.load(ptr)
        steps_list.append(step)

    # Get the array argument set for each thread
    args_list = []
    for i in range(NUM_CPU):
        space = builder.alloca(byte_ptr_t,
                               size=lc.Constant.int(lc.Type.int(), array_count))
        args_list.append(space)

        for j in range(array_count):
            # For each array, compute subarray pointer
            dst = builder.gep(space, [lc.Constant.int(lc.Type.int(), j)])
            src = builder.gep(args, [lc.Constant.int(lc.Type.int(), j)])

            baseptr = builder.load(src)
            base = builder.ptrtoint(baseptr, intp_t)
            multiplier = lc.Constant.int(count.type, i)
            offset = builder.mul(steps_list[j], builder.mul(count, multiplier))
            addr = builder.inttoptr(builder.add(base, offset), baseptr.type)

            builder.store(addr, dst)

    # Declare external functions
    add_task_ty = lc.Type.function(lc.Type.void(), [byte_ptr_t] * 5)
    empty_fnty = lc.Type.function(lc.Type.void(), ())
    add_task = mod.get_or_insert_function(add_task_ty, name='numba_add_task')
    synchronize = mod.get_or_insert_function(empty_fnty,
                                             name='numba_synchronize')
    ready = mod.get_or_insert_function(empty_fnty, name='numba_ready')

    # Add tasks for queue; one per thread
    as_void_ptr = lambda arg: builder.bitcast(arg, byte_ptr_t)

    for each_args, each_dims in zip(args_list, count_list):
        innerargs = [as_void_ptr(x) for x
                     in [innerfunc, each_args, each_dims, steps, data]]

        builder.call(add_task, innerargs)

    # Signal worker that we are ready
    builder.call(ready, ())
    # Wait for workers
    builder.call(synchronize, ())
    builder.ret_void()

    return lfunc


# ---------------------------------------------------------------------------

class ParallelGUFuncBuilder(ufuncbuilder.GUFuncBuilder):
    def __init__(self, py_func, signature, identity=None, targetoptions={}):
        # Force nopython mode
        targetoptions.update(dict(nopython=True))
        super(ParallelGUFuncBuilder, self).__init__(py_func=py_func,
                                                    signature=signature,
                                                    identity=identity,
                                                    targetoptions=targetoptions)

    def build(self, cres):
        """
        Returns (dtype numbers, function ptr, EnvironmentObject)
        """
        _launch_threads()
        # Build wrapper for ufunc entry point
        ctx = cres.target_context
        library = cres.library
        signature = cres.signature
        llvm_func = library.get_function(cres.fndesc.llvm_func_name)
        wrapper, env = build_gufunc_wrapper(library, ctx, llvm_func,
                                            signature, self.sin, self.sout,
                                            fndesc=cres.fndesc,
                                            env=cres.environment)

        ptr = library.get_pointer_to_function(wrapper.name)

        # Get dtypes
        dtypenums = []
        for a in signature.args:
            if isinstance(a, types.Array):
                ty = a.dtype
            else:
                ty = a
            dtypenums.append(as_dtype(ty).num)

        return dtypenums, ptr, env


def build_gufunc_wrapper(library, ctx, llvm_func, signature, sin, sout, fndesc,
                         env):
    innerfunc, env = ufuncbuilder.build_gufunc_wrapper(library, ctx, llvm_func,
                                                       signature, sin, sout,
                                                       fndesc=fndesc, env=env)
    sym_in = set(sym for term in sin for sym in term)
    sym_out = set(sym for term in sout for sym in term)
    inner_ndim = len(sym_in | sym_out)

    lfunc = build_gufunc_kernel(library, ctx, innerfunc, signature, inner_ndim)
    library.add_ir_module(lfunc.module)
    return lfunc, env


def build_gufunc_kernel(library, ctx, innerfunc, sig, inner_ndim):
    """Wrap the original CPU gufunc with a parallel dispatcher.

    Args
    ----
    ctx
        numba's codegen context

    innerfunc
        llvm function of the original CPU gufunc

    sig
        type signature of the gufunc

    inner_ndim
        inner dimension of the gufunc

    Details
    -------

    Generate a function of the following signature:

    void ufunc_kernel(char **args, npy_intp *dimensions, npy_intp* steps,
                      void* data)

    Divide the work equally across all threads and let the last thread take all
    the left over.


    """
    # Declare types and function
    byte_t = lc.Type.int(8)
    byte_ptr_t = lc.Type.pointer(byte_t)

    intp_t = ctx.get_value_type(types.intp)

    fnty = lc.Type.function(lc.Type.void(), [lc.Type.pointer(byte_ptr_t),
                                             lc.Type.pointer(intp_t),
                                             lc.Type.pointer(intp_t),
                                             byte_ptr_t])

    mod = library.create_ir_module('parallel.gufunc.wrapper')
    lfunc = mod.add_function(fnty, name=".kernel")
    innerfunc = mod.add_function(fnty, name=innerfunc.name)

    bb_entry = lfunc.append_basic_block('')

    # Function body starts
    builder = lc.Builder.new(bb_entry)

    args, dimensions, steps, data = lfunc.args

    # Distribute work
    total = builder.load(dimensions)
    ncpu = lc.Constant.int(total.type, NUM_CPU)

    count = builder.udiv(total, ncpu)

    count_list = []
    remain = total

    for i in range(NUM_CPU):
        space = cgutils.alloca_once(builder, intp_t, size=inner_ndim + 1)
        cgutils.memcpy(builder, space, dimensions,
                       count=lc.Constant.int(intp_t, inner_ndim + 1))
        count_list.append(space)

        if i == NUM_CPU - 1:
            # Last thread takes all leftover
            builder.store(remain, space)
        else:
            builder.store(count, space)
            remain = builder.sub(remain, count)

    # Array count is input signature plus 1 (due to output array)
    array_count = len(sig.args) + 1

    # Get the increment step for each array
    steps_list = []
    for i in range(array_count):
        ptr = builder.gep(steps, [lc.Constant.int(lc.Type.int(), i)])
        step = builder.load(ptr)
        steps_list.append(step)

    # Get the array argument set for each thread
    args_list = []
    for i in range(NUM_CPU):
        space = builder.alloca(byte_ptr_t,
                               size=lc.Constant.int(lc.Type.int(), array_count))
        args_list.append(space)

        for j in range(array_count):
            # For each array, compute subarray pointer
            dst = builder.gep(space, [lc.Constant.int(lc.Type.int(), j)])
            src = builder.gep(args, [lc.Constant.int(lc.Type.int(), j)])

            baseptr = builder.load(src)
            base = builder.ptrtoint(baseptr, intp_t)
            multiplier = lc.Constant.int(count.type, i)
            offset = builder.mul(steps_list[j], builder.mul(count, multiplier))
            addr = builder.inttoptr(builder.add(base, offset), baseptr.type)

            builder.store(addr, dst)

    # Declare external functions
    add_task_ty = lc.Type.function(lc.Type.void(), [byte_ptr_t] * 5)
    empty_fnty = lc.Type.function(lc.Type.void(), ())
    add_task = mod.get_or_insert_function(add_task_ty, name='numba_add_task')
    synchronize = mod.get_or_insert_function(empty_fnty,
                                             name='numba_synchronize')
    ready = mod.get_or_insert_function(empty_fnty, name='numba_ready')

    # Add tasks for queue; one per thread
    as_void_ptr = lambda arg: builder.bitcast(arg, byte_ptr_t)

    for each_args, each_dims in zip(args_list, count_list):
        innerargs = [as_void_ptr(x) for x
                     in [innerfunc, each_args, each_dims, steps, data]]
        builder.call(add_task, innerargs)

    # Signal worker that we are ready
    builder.call(ready, ())
    # Wait for workers
    builder.call(synchronize, ())

    builder.ret_void()

    return lfunc


# ---------------------------------------------------------------------------


class _ProtectEngineDestroy(object):
    def __init__(self, set_cas, engine):
        self.set_cas = set_cas
        self.engine = engine

    def __del__(self):
        """
        We need to set the CAS function to NULL to prevent the worker threads to
        execute this function as LLVM is releasing the memory of the function.
        """
        self.set_cas(0)


_keepalive = []


def _make_cas_function():
    """
    Generate a compare-and-swap function for portability sake.
    """
    # Generate IR
    mod = lc.Module.new('generate-cas')
    llint = lc.Type.int()
    llintp = lc.Type.pointer(llint)
    fnty = lc.Type.function(llint, [llintp, llint, llint])
    fn = mod.add_function(fnty, name='.numba.parallel.ufunc.cas')
    ptr, old, repl = fn.args
    bb = fn.append_basic_block('')
    builder = lc.Builder.new(bb)
    outpack = builder.cmpxchg(ptr, old, repl, ordering='monotonic')
    out = builder.extract_value(outpack, 0)
    failed = builder.extract_value(outpack, 1)
    builder.ret(builder.select(failed, old, out))

    # Build & Link
    llmod = ll.parse_assembly(str(mod))
    llfn = llmod.get_function(fn.name)

    target = ll.Target.from_default_triple()
    tm = target.create_target_machine()
    engine = ll.create_mcjit_compiler(llmod, tm)
    ptr = engine.get_pointer_to_function(llfn)
    return engine, ptr


def _launch_threads():
    """
    Initialize work queues and workers
    """
    from . import workqueue as lib
    from ctypes import CFUNCTYPE, c_int

    launch_threads = CFUNCTYPE(None, c_int)(lib.launch_threads)
    launch_threads(NUM_CPU)


def _init():
    from . import workqueue as lib
    from ctypes import CFUNCTYPE, c_void_p

    ll.add_symbol('numba_add_task', lib.add_task)
    ll.add_symbol('numba_synchronize', lib.synchronize)
    ll.add_symbol('numba_ready', lib.ready)

    set_cas = CFUNCTYPE(None, c_void_p)(lib.set_cas)

    engine, cas_ptr = _make_cas_function()
    set_cas(c_void_p(cas_ptr))

    _keepalive.append(_ProtectEngineDestroy(set_cas, engine))


_init()

_DYLD_WORKAROUND_SET = 'NUMBA_DYLD_WORKAROUND' in os.environ
_DYLD_WORKAROUND_VAL = int(os.environ.get('NUMBA_DYLD_WORKAROUND', 0))

if _DYLD_WORKAROUND_SET and _DYLD_WORKAROUND_VAL:
    _launch_threads()

elif not _DYLD_WORKAROUND_SET:
    # Do it automatically for python2.6 linux
    if (sys.version_info[:2] == (2, 6) and
            sys.platform.startswith('linux') and
                utils.MACHINE_BITS == 64):
        _launch_threads()
