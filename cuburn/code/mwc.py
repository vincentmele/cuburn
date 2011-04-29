"""
The multiply-with-carry random number generator.
"""

import time

import pycuda.driver as cuda
from pycuda.compiler import SourceModule
import numpy as np

from jinja2 import Template

from cuburn import code

src = r"""
typedef struct {
    uint32_t    mul;
    uint32_t    state;
    uint32_t    carry;
} mwc_st;

__device__ uint32_t mwc_next(mwc_st *st) {
    asm("{\n\t.reg .u64 val;\n\t"
        "cvt.u64.u32  val, %0;\n\t"
        "mad.wide.u32 val, %1, %2, val;\n\t"
        "mov.b64 {%1, %0}, val;\n\t}\n\t"
        : "=r"(st->carry), "=r"(st->state) : "r"(st->mul));
    return st->state;
}

__device__ float mwc_next_01(mwc_st *st) {
    return mwc_next(st) * (1.0f / 4294967296.0f);
}

__device__ float mwc_next_11(mwc_st *st) {
    return ((int32_t) mwc_next(st)) * (1.0f / 2147483648.0f);
}

"""

testsrc = code.base + src + """
__global__ void test_mwc(mwc_st *msts, uint64_t *sums, float nrounds) {
    mwc_st rctx = msts[gtid()];
    uint64_t sum = 0;
    for (float i = 0; i < nrounds; i++) sum += mwc_next(&rctx);
    sums[gtid()] = sum;
    msts[gtid()] = rctx;
}
"""

def build_mwc_seeds(nthreads, seed=None):
    if seed:
        rand = np.random.RandomState(seed)
    else:
        rand = np.random

    # Load raw big-endian u32 multipliers from primes.bin.
    with open('primes.bin') as primefp:
        dt = np.dtype(np.uint32).newbyteorder('B')
        mults = np.frombuffer(primefp.read(), dtype=dt)

    # Create the seed structures. TODO: check that struct is 4-byte aligned
    seeds = np.empty((3, nthreads), dtype=np.uint32, order='F')

    # Randomness in choosing multipliers is good, but larger multipliers
    # have longer periods, which is also good. This is a compromise.
    mults = np.array(mults[:nthreads*4])
    rand.shuffle(mults)
    seeds[0][:] = mults[:nthreads]

    # Intentionally excludes both 0 and (2^32-1), as they can lead to
    # degenerate sequences of period 0
    seeds[1] = rand.randint(1, 0xffffffff, size=nthreads)
    seeds[2] = rand.randint(1, 0xffffffff, size=nthreads)

    return seeds

def test_mwc():
    rounds = 5000
    nblocks = 64
    nthreads = 512 * nblocks

    seeds = build_mwc_seeds(nthreads, seed = 5)
    dseeds = cuda.to_device(seeds)

    mod = SourceModule(testsrc)

    for trial in range(2):
        print "Trial %d, on CPU: " % trial,
        sums = np.zeros(nthreads, dtype=np.uint64)
        ctime = time.time()
        mults = seeds[0].astype(np.uint64)
        states = seeds[1]
        carries = seeds[2]
        for i in range(rounds):
            step = np.frombuffer((mults * states + carries).data,
                       dtype=np.uint32).reshape((2, nthreads), order='F')
            states[:] = step[0]
            carries[:] = step[1]
            sums += states

        ctime = time.time() - ctime
        print "Took %g seconds." % ctime

        print "Trial %d, on device: " % trial,
        dsums = cuda.mem_alloc(8*nthreads)
        fun = mod.get_function("test_mwc")
        dtime = fun(dseeds, dsums, np.float32(rounds),
                    block=(512,1,1), grid=(nblocks,1), time_kernel=True)
        print "Took %g seconds." % dtime
        dsums = cuda.from_device(dsums, nthreads, np.uint64)
        if not np.all(np.equal(sums, dsums)):
            print "Sum discrepancy!"
            print sums
            print dsums

