/*
 * oclburn.c — burn GPU OpenCL avec vérification des résultats (BenchSuite).
 *
 * Charge en continu le cœur de calcul (entiers exacts, FP32 FMA, FP16 si disponible) ET la mémoire du GPU (motifs écrits puis
 * relus sur ~90 % de la VRAM), simultanément, sur Intel / AMD / NVIDIA (tout GPU doté d'un pilote OpenCL).
 * Chaque résultat est comparé à une référence : une erreur de calcul ou de mémoire est comptée et signalée.
 *
 * Aucune dépendance de compilation hors gcc : l'API OpenCL est chargée avec dlopen("libOpenCL.so.1") et déclarée ici
 * (pas besoin des en-têtes CL/cl.h). Compilation :  gcc -O2 -pthread -o oclburn oclburn.c -ldl
 *
 * Usage :  oclburn -l                       liste les périphériques
 *          oclburn [-d 0,1] [-t 300] [-m 90] [-M all|power|compute|memory] [-i 5] [-w 4096] [-F 1|2|4] [--cpu]
 *          -M power : FP32 FMA pur en continu sans concurrence mémoire (puissance maximale plate au TDP + vérification d'erreurs)
 *          -F : fréquence du contrôle des noyaux flottants (1 = chaque série de FMA, détection maximale ; 4 = charge maximale, détection réduite)
 * Sortie (lignes « clé=valeur ») : DEVICE, START, CALIB, STAT, ERROR, RESULT, SUMMARY. Code de retour : 0 = aucune erreur,
 * 3 = erreurs de calcul/mémoire détectées, 2 = échec (pas de GPU, compilation OpenCL…), 1 = usage.
 * Licence : code BenchSuite (libre), sans reprise de code tiers.
 */
#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <stddef.h>
#include <dlfcn.h>
#include <pthread.h>
#include <signal.h>
#include <time.h>
#include <unistd.h>

/* ───────────── OpenCL minimal (chargé dynamiquement) ───────────── */
typedef int32_t cl_int; typedef uint32_t cl_uint; typedef uint64_t cl_ulong; typedef cl_ulong cl_bitfield;
typedef float cl_float; typedef intptr_t cl_prop;
typedef void *cl_platform_id, *cl_device_id, *cl_context, *cl_command_queue, *cl_mem, *cl_program, *cl_kernel, *cl_event;
#define CL_SUCCESS 0
#define CL_DEVICE_TYPE_CPU 2ULL
#define CL_DEVICE_TYPE_GPU 4ULL
#define CL_DEVICE_TYPE_ACCELERATOR 8ULL
#define CL_DEVICE_TYPE_ALL 0xFFFFFFFFULL
#define CL_DEVICE_TYPE 0x1000
#define CL_DEVICE_MAX_COMPUTE_UNITS 0x1002
#define CL_DEVICE_MAX_CLOCK_FREQUENCY 0x100C
#define CL_DEVICE_MAX_MEM_ALLOC_SIZE 0x1010
#define CL_DEVICE_GLOBAL_MEM_SIZE 0x101F
#define CL_DEVICE_NAME 0x102B
#define CL_DEVICE_EXTENSIONS 0x1030
#define CL_DEVICE_HOST_UNIFIED_MEMORY 0x1035
#define CL_PLATFORM_NAME 0x0902
#define CL_CONTEXT_PLATFORM 0x1084
#define CL_MEM_READ_WRITE 1ULL
#define CL_PROGRAM_BUILD_LOG 0x1183
#define CL_EVENT_COMMAND_EXECUTION_STATUS 0x11D3
#define CL_DEVICE_PCI_BUS_INFO_KHR 0x410F
#define CL_DEVICE_PCI_BUS_ID_NV 0x4008
#define CL_DEVICE_PCI_SLOT_ID_NV 0x4009
#define CL_DEVICE_TOPOLOGY_AMD 0x4037

#define CL_FUNCS(X) \
 X(cl_int, clGetPlatformIDs, (cl_uint, cl_platform_id*, cl_uint*)) \
 X(cl_int, clGetPlatformInfo, (cl_platform_id, cl_uint, size_t, void*, size_t*)) \
 X(cl_int, clGetDeviceIDs, (cl_platform_id, cl_bitfield, cl_uint, cl_device_id*, cl_uint*)) \
 X(cl_int, clGetDeviceInfo, (cl_device_id, cl_uint, size_t, void*, size_t*)) \
 X(cl_context, clCreateContext, (const cl_prop*, cl_uint, const cl_device_id*, void*, void*, cl_int*)) \
 X(cl_command_queue, clCreateCommandQueue, (cl_context, cl_device_id, cl_bitfield, cl_int*)) \
 X(cl_mem, clCreateBuffer, (cl_context, cl_bitfield, size_t, void*, cl_int*)) \
 X(cl_program, clCreateProgramWithSource, (cl_context, cl_uint, const char**, const size_t*, cl_int*)) \
 X(cl_int, clBuildProgram, (cl_program, cl_uint, const cl_device_id*, const char*, void*, void*)) \
 X(cl_int, clGetProgramBuildInfo, (cl_program, cl_device_id, cl_uint, size_t, void*, size_t*)) \
 X(cl_kernel, clCreateKernel, (cl_program, const char*, cl_int*)) \
 X(cl_int, clSetKernelArg, (cl_kernel, cl_uint, size_t, const void*)) \
 X(cl_int, clEnqueueNDRangeKernel, (cl_command_queue, cl_kernel, cl_uint, const size_t*, const size_t*, const size_t*, cl_uint, const cl_event*, cl_event*)) \
 X(cl_int, clEnqueueReadBuffer, (cl_command_queue, cl_mem, cl_uint, size_t, size_t, void*, cl_uint, const cl_event*, cl_event*)) \
 X(cl_int, clEnqueueWriteBuffer, (cl_command_queue, cl_mem, cl_uint, size_t, size_t, const void*, cl_uint, const cl_event*, cl_event*)) \
 X(cl_int, clFlush, (cl_command_queue)) \
 X(cl_int, clFinish, (cl_command_queue)) \
 X(cl_int, clWaitForEvents, (cl_uint, const cl_event*)) \
 X(cl_int, clGetEventInfo, (cl_event, cl_uint, size_t, void*, size_t*)) \
 X(cl_int, clReleaseEvent, (cl_event)) \
 X(cl_int, clReleaseMemObject, (cl_mem)) \
 X(cl_int, clReleaseKernel, (cl_kernel)) \
 X(cl_int, clReleaseProgram, (cl_program)) \
 X(cl_int, clReleaseCommandQueue, (cl_command_queue)) \
 X(cl_int, clReleaseContext, (cl_context))
#define CL_DECL(ret, name, args) static ret (*p_##name) args;
CL_FUNCS(CL_DECL)

static int cl_load(void) {
    void *h = dlopen("libOpenCL.so.1", RTLD_NOW);
    if (!h) h = dlopen("libOpenCL.so", RTLD_NOW);
    if (!h) { fprintf(stderr, "NOOPENCL libOpenCL.so.1 introuvable (sudo apt install ocl-icd-libopencl1 + pilote OpenCL du GPU)\n"); return -1; }
#define CL_LOAD(ret, name, args) p_##name = (ret (*) args)dlsym(h, #name); if (!p_##name) { fprintf(stderr, "NOOPENCL symbole manquant : %s\n", #name); return -1; }
    CL_FUNCS(CL_LOAD)
    return 0;
}

/* ───────────── état global ───────────── */
static volatile sig_atomic_t g_stop = 0;
static double g_start;
static void on_sig(int s) { (void)s; g_stop = 1; }
static double now_s(void) { struct timespec ts; clock_gettime(CLOCK_MONOTONIC, &ts); return ts.tv_sec + ts.tv_nsec * 1e-9; }
#define ADD(p, v) __atomic_fetch_add((p), (uint64_t)(v), __ATOMIC_RELAXED)
#define GET(p) __atomic_load_n((p), __ATOMIC_RELAXED)

typedef struct {
    int idx;                      /* index dans la liste affichée par -l */
    cl_platform_id plat; cl_device_id dev; cl_context ctx;
    char name[160], plat_name[160], exts[4096], type[8], pci[16];
    cl_uint cu, mhz; cl_ulong gmem, maxalloc; int igpu, fp16;
    size_t global;                /* work-items par lancement */
    int do_compute, do_memory, power_only; int vram_pct;
    uint64_t fp32_flops, fp16_flops, int_ops, mem_bytes, c_err, m_err, launches, passes, mem_tested;
    int failed; int c_ready; char fail_msg[400];
    pthread_t th_c, th_m; int run_c, run_m;
} Dev;
static pthread_mutex_t g_print = PTHREAD_MUTEX_INITIALIZER;
static int g_errors_printed = 0;

static void dev_fail(Dev *d, const char *what, cl_int e) {
    pthread_mutex_lock(&g_print);
    __atomic_store_n(&d->failed, 1, __ATOMIC_RELEASE);
    snprintf(d->fail_msg, sizeof d->fail_msg, "%s (code OpenCL %d)", what, e);
    printf("FAIL dev=%d what=\"%s\" code=%d\n", d->idx, what, e); fflush(stdout);
    pthread_mutex_unlock(&g_print);
}
static void report_error(Dev *d, const char *kind, const char *detail, uint64_t count) {
    pthread_mutex_lock(&g_print);
    if (g_errors_printed < 200) {
        g_errors_printed++;
        printf("ERROR dev=%d kind=%s t=%.1f count=%llu detail=\"%s\"\n", d->idx, kind, now_s() - g_start, (unsigned long long)count, detail);
        fflush(stdout);
    }
    pthread_mutex_unlock(&g_print);
}

/* ───────────── noyaux OpenCL C ───────────── */
static const char *SRC_COMMON =
"__kernel void k_int(__global uint *out, uint seed, uint iters){\n"
" uint i=get_global_id(0);\n"
" uint a=i*2654435761u^seed, b=(i+1u)*40503u+seed, c=a^0x9E3779B9u, d=b+0x7F4A7C15u;\n"
" for(uint k=0;k<iters;k++){\n"
"  a=a*1664525u+1013904223u; b^=(a<<13)|(a>>19); c+=b*2246822519u; d=(d^c)*3266489917u+(a>>7);\n"
"  a=rotate(a,5u)^d; b+=c^(d>>3); c=rotate(c,11u)+a; d^=b*668265263u;\n"
" }\n"
" out[i]=a^b^c^d;\n"
"}\n"
"__kernel void k_cmp(__global const uint *a, __global const uint *b, __global uint *err, uint base){\n"
" uint i=get_global_id(0); uint x=a[i], y=b[i];\n"
" if(x!=y){ uint c=atomic_inc(&err[base]); if(c<4u){ err[base+1u+3u*c]=i; err[base+2u+3u*c]=y; err[base+3u+3u*c]=x; } }\n"
"}\n";

/* FP32 / FP16 : 8 accumulateurs vectoriels indépendants, 4 FMA par accumulateur et par itération. L'addende c est tiré d'un LCG entier
   (mantisse aléatoire dans [1,2) : les bits basculent au maximum, donc consommation maximale) et m = 0,5 garde les valeurs bornées.
   La récurrence étant contractante, une erreur transitoire s'effacerait en quelques FMA : à chaque itération, TOUS les bits des
   accumulateurs sont donc repliés dans une somme de contrôle entière (xor / rotation / multiplication) qui conserve toute erreur. */
static int g_fold = 1;      /* repli de la somme de contrôle toutes les g_fold séries de FMA (1, 2 ou 4) : 1 = détection maximale */
static char *gen_fma_src(int half) {
    size_t cap = 24576; char *s = malloc(cap); size_t n = 0;
    const char *T = half ? "half4" : "float4", *S = half ? "half" : "float";
    n += snprintf(s + n, cap - n, "%s__kernel void %s(__global uint *out, float s0, uint iters){\n uint i=get_global_id(0); float f=(float)(i&1023u)*1e-4f+s0;\n",
                  half ? "#pragma OPENCL EXTENSION cl_khr_fp16 : enable\n" : "", half ? "k_half" : "k_fma");
    n += snprintf(s + n, cap - n, " %s a0=(%s)((%s)f,(%s)(f+.1f),(%s)(f+.2f),(%s)(f+.3f));\n", T, T, S, S, S, S);
    for (int k = 1; k < 8; k++) n += snprintf(s + n, cap - n, " %s a%d=a0+(%s)(%d.0f*.01f);\n", T, k, S, k);
    n += snprintf(s + n, cap - n, " const %s m=(%s)(%s)(.5f);\n uint sd=i*2654435761u+0x1234567u, h=2166136261u;\n", T, T, S);
    n += snprintf(s + n, cap - n, " for(uint k=0;k<iters;k++){\n");
    for (int r = 0; r < 4; r++) {
        n += snprintf(s + n, cap - n, "  sd=sd*1664525u+1013904223u; %s c%d=(%s)((%s)as_float((sd>>9)|0x3F800000u),(%s)as_float((rotate(sd,7u)>>9)|0x3F800000u),"
                      "(%s)as_float((rotate(sd,13u)>>9)|0x3F800000u),(%s)as_float((rotate(sd,21u)>>9)|0x3F800000u));\n", T, r, T, S, S, S, S);
        for (int k = 0; k < 8; k++) n += snprintf(s + n, cap - n, "  a%d=fma(a%d,m,c%d);", k, k, r);
        n += snprintf(s + n, cap - n, "\n");
        if (((r + 1) % g_fold) == 0) {          /* repli entier de tous les bits des accumulateurs */
            if (half) {
                n += snprintf(s + n, cap - n, "  { uint4 t=as_uint4(convert_float4(a0))^as_uint4(convert_float4(a1))^as_uint4(convert_float4(a2))^as_uint4(convert_float4(a3))"
                              "^as_uint4(convert_float4(a4))^as_uint4(convert_float4(a5))^as_uint4(convert_float4(a6))^as_uint4(convert_float4(a7));\n");
            } else {
                n += snprintf(s + n, cap - n, "  { uint4 t=as_uint4(a0)^as_uint4(a1)^as_uint4(a2)^as_uint4(a3)^as_uint4(a4)^as_uint4(a5)^as_uint4(a6)^as_uint4(a7);\n");
            }
            n += snprintf(s + n, cap - n, "  h=(h^t.x^rotate(t.y,7u)^rotate(t.z,13u)^rotate(t.w,19u))*16777619u+k; }\n");
        }
    }
    n += snprintf(s + n, cap - n, " }\n");
    n += snprintf(s + n, cap - n, " %s r=a0+a1+a2+a3+a4+a5+a6+a7; out[i]=h^as_uint((float)r.x+(float)r.y+(float)r.z+(float)r.w);\n}\n", T);
    return s;
}

/* Noyau FP32 FMA ultra-dense pour charge électrique maximale et plate (mode -M power) :
   16 vagues de 8 FMA float4 par itération (128 FMA float4 = 1024 FLOPs/itération), zéro concurrence mémoire
   et un seul repli entier par itération pour détecter toute erreur de calcul sans perturber le TDP. */
static char *gen_power_src(void) {
    size_t cap = 32768; char *s = malloc(cap); size_t n = 0;
    n += snprintf(s + n, cap - n,
        "__kernel void k_power(__global uint *out, float s0, uint iters){\n"
        " uint i=get_global_id(0); float f=(float)(i&1023u)*1e-4f+s0;\n"
        " float4 a0=(float4)(f, f+0.13f, -f-0.27f, f+0.41f);\n");
    for (int k = 1; k < 8; k++) {
        n += snprintf(s + n, cap - n, " float4 a%d=a0+(float4)(%.3ff, %.3ff, %.3ff, %.3ff);\n",
                      k, k * 0.017f, -k * 0.023f, k * 0.031f, -k * 0.019f);
    }
    n += snprintf(s + n, cap - n,
        " const float4 m=(float4)(0.5f, -0.5f, 0.5f, -0.5f);\n"
        " uint sd=i*2654435761u+0x1234567u, h=2166136261u;\n"
        " for(uint k=0;k<iters;k++){\n");
    for (int r = 0; r < 16; r++) {
        if ((r % 4) == 0) {
            n += snprintf(s + n, cap - n,
                "  sd=sd*1664525u+1013904223u; float4 c%d=(float4)(as_float((sd>>9)|0x3F800000u),"
                "-as_float((rotate(sd,7u)>>9)|0x3F800000u),as_float((rotate(sd,13u)>>9)|0x3F800000u),"
                "-as_float((rotate(sd,21u)>>9)|0x3F800000u));\n", r / 4);
        }
        int ci = r / 4;
        for (int k = 0; k < 8; k++) {
            n += snprintf(s + n, cap - n, "  a%d=fma(a%d,m,c%d);", k, k, ci);
        }
        n += snprintf(s + n, cap - n, "\n");
    }
    n += snprintf(s + n, cap - n,
        "  { uint4 t=as_uint4(a0)^as_uint4(a1)^as_uint4(a2)^as_uint4(a3)^as_uint4(a4)^as_uint4(a5)^as_uint4(a6)^as_uint4(a7);\n"
        "    h=(h^t.x^rotate(t.y,7u)^rotate(t.z,13u)^rotate(t.w,19u))*16777619u+k; }\n"
        " }\n"
        " float4 r=a0+a1+a2+a3+a4+a5+a6+a7; out[i]=h^as_uint(r.x+r.y+r.z+r.w);\n"
        "}\n");
    return s;
}
static const char *SRC_MEM =
"__kernel void k_mw(__global uint *b, ulong n, uint pat, uint mode){\n"
" for(ulong i=get_global_id(0); i<n; i+=get_global_size(0)){ uint x=((uint)i)*2654435761u^pat; b[i]=(mode==0u)?x:((mode==1u)?~x:pat); }\n"
"}\n"
"__kernel void k_mc(__global const uint *b, ulong n, uint pat, uint mode, __global uint *err){\n"
" for(ulong i=get_global_id(0); i<n; i+=get_global_size(0)){ uint x=((uint)i)*2654435761u^pat; uint e=(mode==0u)?x:((mode==1u)?~x:pat); uint v=b[i];\n"
"  if(v!=e){ uint c=atomic_inc(&err[0]); if(c<4u){ err[1u+3u*c]=(uint)i; err[2u+3u*c]=e; err[3u+3u*c]=v; } } }\n"
"}\n";

/* Référence CPU du noyau entier (calcul exact) */
static uint32_t rotl32(uint32_t x, uint32_t n) { return (x << n) | (x >> (32 - n)); }
static uint32_t ref_int(uint32_t i, uint32_t seed, uint32_t iters) {
    uint32_t a = i * 2654435761u ^ seed, b = (i + 1u) * 40503u + seed, c = a ^ 0x9E3779B9u, d = b + 0x7F4A7C15u;
    for (uint32_t k = 0; k < iters; k++) {
        a = a * 1664525u + 1013904223u; b ^= (a << 13) | (a >> 19); c += b * 2246822519u; d = (d ^ c) * 3266489917u + (a >> 7);
        a = rotl32(a, 5) ^ d; b += c ^ (d >> 3); c = rotl32(c, 11) + a; d ^= b * 668265263u;
    }
    return a ^ b ^ c ^ d;
}

/* ───────────── aides OpenCL ───────────── */
static cl_program build_prog(Dev *d, const char *src, int quiet) {
    cl_int e; const char *p[1] = { src };
    cl_program pr = p_clCreateProgramWithSource(d->ctx, 1, p, NULL, &e);
    if (e != CL_SUCCESS) { dev_fail(d, "clCreateProgramWithSource", e); return NULL; }
    e = p_clBuildProgram(pr, 1, &d->dev, "", NULL, NULL);
    if (e != CL_SUCCESS) {
        char log[2048] = ""; p_clGetProgramBuildInfo(pr, d->dev, CL_PROGRAM_BUILD_LOG, sizeof log - 1, log, NULL);
        if (!quiet) { char m[2400]; snprintf(m, sizeof m, "compilation OpenCL : %.1900s", log); for (char *q = m; *q; q++) if (*q == '"' || *q == '\n') *q = ' '; dev_fail(d, m, e); }
        p_clReleaseProgram(pr); return NULL;
    }
    return pr;
}
static cl_mem mkbuf(Dev *d, size_t bytes, cl_int *e) { return p_clCreateBuffer(d->ctx, CL_MEM_READ_WRITE, bytes, NULL, e); }
static const cl_uint ZEROS[64] = {0};

/* Attend la fin d'un lot et vérifie qu'il s'est bien exécuté (reset du GPU, CL_OUT_OF_RESOURCES…) : 1 si OK, 0 sinon */
static int wait_ok(cl_event ev, cl_int *code) {
    cl_int w = p_clWaitForEvents(1, &ev), st = 0;
    if (w == CL_SUCCESS) p_clGetEventInfo(ev, CL_EVENT_COMMAND_EXECUTION_STATUS, sizeof st, &st, NULL);
    p_clReleaseEvent(ev);
    *code = (w != CL_SUCCESS) ? w : st;
    return w == CL_SUCCESS && st >= 0;
}

/* ───────────── worker calcul ───────────── */
typedef struct { cl_kernel k; int id; cl_uint iters; cl_mem gold; double flops_it; const char *name; } KD;

static void set_args(KD *kd, cl_mem out, cl_uint iters) {
    cl_uint seed = 0x1234ABCDu; cl_float s = 0.3718f;
    p_clSetKernelArg(kd->k, 0, sizeof(cl_mem), &out);
    if (kd->id == 0) p_clSetKernelArg(kd->k, 1, sizeof seed, &seed); else p_clSetKernelArg(kd->k, 1, sizeof s, &s);
    p_clSetKernelArg(kd->k, 2, sizeof iters, &iters);
}
static double run_timed(Dev *d, cl_command_queue q, KD *kd, cl_mem out, cl_uint iters, cl_int *e) {
    size_t g = d->global; set_args(kd, out, iters);
    double t0 = now_s();
    *e = p_clEnqueueNDRangeKernel(q, kd->k, 1, NULL, &g, NULL, 0, NULL, NULL);
    if (*e == CL_SUCCESS) *e = p_clFinish(q);
    return now_s() - t0;
}

typedef struct { cl_event ev; cl_uint err[48]; } CSlot;

static void *compute_worker(void *arg) {
    Dev *d = arg; cl_int e; size_t g = d->global, bytes = g * 4;
    char *fsrc = d->power_only ? gen_power_src() : gen_fma_src(0);
    size_t l = strlen(SRC_COMMON) + strlen(fsrc) + 1; char *all = malloc(l);
    snprintf(all, l, "%s%s", SRC_COMMON, fsrc); free(fsrc);
    cl_program pr = build_prog(d, all, 0); free(all);
    if (!pr) return NULL;
    cl_program prh = NULL;
    if (!d->power_only && d->fp16) { char *hs = gen_fma_src(1); prh = build_prog(d, hs, 1); free(hs); if (!prh) d->fp16 = 0; }
    cl_command_queue q = p_clCreateCommandQueue(d->ctx, d->dev, 0, &e);
    if (e != CL_SUCCESS) { dev_fail(d, "clCreateCommandQueue (calcul)", e); return NULL; }
    KD kd[3]; int nk = 0;
    if (d->power_only) {
        kd[nk++] = (KD){ p_clCreateKernel(pr, "k_power", &e), 1, 32, NULL, 1024.0, "k_power" };
    } else {
        kd[nk++] = (KD){ p_clCreateKernel(pr, "k_int", &e), 0, 32, NULL, 24.0, "k_int" };
        kd[nk++] = (KD){ p_clCreateKernel(pr, "k_fma", &e), 1, 32, NULL, 256.0, "k_fma" };
        if (prh) kd[nk++] = (KD){ p_clCreateKernel(prh, "k_half", &e), 2, 32, NULL, 256.0, "k_half" };
    }
    cl_kernel kcmp = p_clCreateKernel(pr, "k_cmp", &e);
    cl_mem out = mkbuf(d, bytes, &e); if (e != CL_SUCCESS) { dev_fail(d, "clCreateBuffer (sortie)", e); return NULL; }
    cl_mem errb[2] = { mkbuf(d, sizeof(cl_uint) * 48, &e), mkbuf(d, sizeof(cl_uint) * 48, &e) };
    for (int j = 0; j < nk; j++) { kd[j].gold = mkbuf(d, bytes, &e); if (e != CL_SUCCESS) { dev_fail(d, "clCreateBuffer (référence)", e); return NULL; } }

    /* calibration : ~150 ms en mode power (puissance parfaitement plate), ~80 ms en mode mixte */
    for (int j = 0; j < nk; j++) {
        if (g_stop) goto done;
        cl_uint it = 32; double t = 0;
        run_timed(d, q, &kd[j], out, 1, &e);       /* échauffement : la 1re exécution inclut la compilation JIT du pilote */
        for (;;) {
            t = run_timed(d, q, &kd[j], out, it, &e);
            if (e != CL_SUCCESS) { dev_fail(d, "lancement du noyau (calibration)", e); return NULL; }
            if (t >= 0.02 || it >= (1u << 24)) break;
            it *= 2;
        }
        double tgt = d->power_only ? 0.15 : 0.08; double ni = (double)it * tgt / (t > 1e-6 ? t : 1e-6);
        kd[j].iters = ni < 1 ? 1 : (ni > 4e9 ? 4000000000u : (cl_uint)ni);
        for (int r = 0; r < 4; r++) {                                 /* affinage : le 1er réglage peut être faussé par la latence de lancement */
            t = run_timed(d, q, &kd[j], out, kd[j].iters, &e);
            if (t > 0.5 * tgt && t < 1.4 * tgt) break;
            ni = (double)kd[j].iters * tgt / (t > 1e-6 ? t : 1e-6);
            kd[j].iters = ni < 1 ? 1 : (ni > 4e9 ? 4000000000u : (cl_uint)ni);
        }
        /* référence : 1er calcul ; 2e calcul dans « out » comparé côté hôte (instabilité dès le démarrage) */
        run_timed(d, q, &kd[j], kd[j].gold, kd[j].iters, &e);
        double t2 = run_timed(d, q, &kd[j], out, kd[j].iters, &e);
        uint32_t *a = malloc(bytes), *b = malloc(bytes);
        p_clEnqueueReadBuffer(q, kd[j].gold, 1, 0, bytes, a, 0, NULL, NULL);
        p_clEnqueueReadBuffer(q, out, 1, 0, bytes, b, 0, NULL, NULL);
        uint64_t diff = 0; for (size_t i = 0; i < g; i++) if (a[i] != b[i]) diff++;
        if (diff) { ADD(&d->c_err, diff); char m[200]; snprintf(m, sizeof m, "%s : deux calculs identiques donnent des résultats différents (%llu sur %zu) dès la calibration", kd[j].name, (unsigned long long)diff, g); report_error(d, "compute", m, diff); }
        if (kd[j].id == 0) {   /* comparaison à un calcul CPU exact sur les premiers work-items */
            size_t m = g < 4096 ? g : 4096; uint64_t bad = 0;
            for (size_t i = 0; i < m; i++) if (ref_int((uint32_t)i, 0x1234ABCDu, kd[j].iters) != a[i]) bad++;
            if (bad) { ADD(&d->c_err, bad); char mm[200]; snprintf(mm, sizeof mm, "k_int : %llu résultats différents du calcul CPU sur %zu", (unsigned long long)bad, m); report_error(d, "compute", mm, bad); }
        }
        free(a); free(b);
        pthread_mutex_lock(&g_print);
        printf("CALIB dev=%d kernel=%s iters=%u launch_ms=%.1f\n", d->idx, kd[j].name, kd[j].iters, t2 * 1000); fflush(stdout);
        pthread_mutex_unlock(&g_print);
    }

    __atomic_store_n(&d->c_ready, 1, __ATOMIC_RELEASE);   /* calibration terminée : la charge mémoire peut démarrer sans fausser les mesures */
    if (getenv("OCLBURN_INJECT")) {        /* TEST UNIQUEMENT : fausse la référence du noyau entier pour vérifier que la détection fonctionne */
        cl_uint bad = 0xDEADBEEFu; p_clEnqueueWriteBuffer(q, kd[0].gold, 1, 5 * sizeof(cl_uint), sizeof bad, &bad, 0, NULL, NULL);
    }
    CSlot slot[2]; memset(slot, 0, sizeof slot);
    #define ISSUE(S, N) do { \
        p_clEnqueueWriteBuffer(q, errb[N], 0, 0, sizeof(cl_uint) * 48, ZEROS, 0, NULL, NULL); \
        for (int j = 0; j < nk; j++) { size_t gg = g; set_args(&kd[j], out, kd[j].iters); \
            e = p_clEnqueueNDRangeKernel(q, kd[j].k, 1, NULL, &gg, NULL, 0, NULL, NULL); \
            if (e != CL_SUCCESS) { dev_fail(d, "lancement du noyau de calcul", e); goto done; } \
            p_clSetKernelArg(kcmp, 0, sizeof(cl_mem), &out); p_clSetKernelArg(kcmp, 1, sizeof(cl_mem), &kd[j].gold); \
            p_clSetKernelArg(kcmp, 2, sizeof(cl_mem), &errb[N]); cl_uint base = 16u * (cl_uint)j; p_clSetKernelArg(kcmp, 3, sizeof base, &base); \
            e = p_clEnqueueNDRangeKernel(q, kcmp, 1, NULL, &gg, NULL, 0, NULL, NULL); \
            if (e != CL_SUCCESS) { dev_fail(d, "lancement de la comparaison", e); goto done; } } \
        e = p_clEnqueueReadBuffer(q, errb[N], 0, 0, sizeof(cl_uint) * 48, (S)->err, 0, NULL, &(S)->ev); \
        p_clFlush(q); if (e != CL_SUCCESS) { dev_fail(d, "file de commandes (calcul)", e); goto done; } } while (0)
    #define PROCESS(S) do { cl_int wc; int okw = wait_ok((S)->ev, &wc); (S)->ev = NULL; \
        if (!okw) { dev_fail(d, "lot de calcul échoué (reset du GPU ?)", wc); goto done; } \
        for (int j = 0; j < nk; j++) { \
            double fl = (double)g * kd[j].iters * kd[j].flops_it; \
            if (kd[j].id == 0) ADD(&d->int_ops, fl); else if (kd[j].id == 1) ADD(&d->fp32_flops, fl); else ADD(&d->fp16_flops, fl); \
            cl_uint n = (S)->err[16 * j]; \
            if (n) { ADD(&d->c_err, n); char m[240]; \
                snprintf(m, sizeof m, "%s : %u résultat(s) différent(s) de la référence ; 1er : élément %u attendu 0x%08x obtenu 0x%08x", kd[j].name, n, (S)->err[16*j+1], (S)->err[16*j+2], (S)->err[16*j+3]); \
                report_error(d, "compute", m, n); } } \
        ADD(&d->launches, nk); } while (0)

    ISSUE(&slot[0], 0);
    int cur = 0;
    while (!g_stop) {
        ISSUE(&slot[cur ^ 1], cur ^ 1);       /* le lot suivant est déjà en file avant de lire le précédent : le GPU ne s'arrête jamais */
        PROCESS(&slot[cur]);
        cur ^= 1;
    }
    PROCESS(&slot[cur]);
done:
    p_clFinish(q);
    return NULL;
}

/* ───────────── worker mémoire ───────────── */
typedef struct { cl_event ev; cl_uint err[16]; } MSlot;

static void *memory_worker(void *arg) {
    Dev *d = arg; cl_int e;
    while (d->run_c && !__atomic_load_n(&d->c_ready, __ATOMIC_ACQUIRE) && !__atomic_load_n(&d->failed, __ATOMIC_ACQUIRE) && !g_stop) usleep(50000);   /* attend la calibration du calcul */
    if (g_stop) return NULL;
    cl_program pr = build_prog(d, SRC_MEM, 0); if (!pr) return NULL;
    cl_command_queue q = p_clCreateCommandQueue(d->ctx, d->dev, 0, &e);
    if (e != CL_SUCCESS) { dev_fail(d, "clCreateCommandQueue (mémoire)", e); return NULL; }
    cl_kernel kw = p_clCreateKernel(pr, "k_mw", &e), kc = p_clCreateKernel(pr, "k_mc", &e);
    size_t chunk = d->maxalloc < ((size_t)1 << 31) ? (size_t)d->maxalloc : ((size_t)1 << 31);   /* ≤ 2 Gio par tampon */
    cl_ulong reserve = (cl_ulong)768 << 20;                                                      /* marge pour le pilote / l'affichage */
    cl_ulong target = d->gmem / 100 * (cl_ulong)d->vram_pct;
    if (d->gmem > reserve && target > d->gmem - reserve) target = d->gmem - reserve;
    if (chunk > target) chunk = (size_t)target;
    chunk &= ~(size_t)0xFFFF;
    cl_mem bufs[512]; size_t sizes[512]; int nb = 0; cl_ulong total = 0;
    size_t gw = d->global;
    while (total + chunk <= target && nb < 512 && !g_stop) {
        cl_mem b = mkbuf(d, chunk, &e); if (e != CL_SUCCESS) break;
        cl_ulong n = chunk / 4; cl_uint pat = 0, mode = 2;      /* écriture d'essai : force l'allocation réelle */
        p_clSetKernelArg(kw, 0, sizeof b, &b); p_clSetKernelArg(kw, 1, sizeof n, &n); p_clSetKernelArg(kw, 2, sizeof pat, &pat); p_clSetKernelArg(kw, 3, sizeof mode, &mode);
        e = p_clEnqueueNDRangeKernel(q, kw, 1, NULL, &gw, NULL, 0, NULL, NULL); if (e == CL_SUCCESS) e = p_clFinish(q);
        if (e != CL_SUCCESS) { p_clReleaseMemObject(b); break; }
        bufs[nb] = b; sizes[nb] = chunk; nb++; total += chunk;
    }
    if (nb == 0) { dev_fail(d, "aucune allocation mémoire possible pour le test VRAM", e); return NULL; }
    d->mem_tested = total;
    pthread_mutex_lock(&g_print);
    printf("MEMORY dev=%d buffers=%d tested_mb=%llu of_mb=%llu\n", d->idx, nb, (unsigned long long)(total >> 20), (unsigned long long)(d->gmem >> 20)); fflush(stdout);
    pthread_mutex_unlock(&g_print);
    cl_mem errb[2] = { mkbuf(d, sizeof(cl_uint) * 16, &e), mkbuf(d, sizeof(cl_uint) * 16, &e) };
    MSlot slot[2]; memset(slot, 0, sizeof slot);
    uint64_t pass_no = 0; int inject = getenv("OCLBURN_INJECT") != NULL;
    #define MISSUE(S, N) do { \
        p_clEnqueueWriteBuffer(q, errb[N], 0, 0, sizeof(cl_uint) * 16, ZEROS, 0, NULL, NULL); \
        cl_uint mode = (cl_uint)(pass_no % 3); cl_uint base = (cl_uint)(pass_no * 0x9E3779B1u) ^ (mode == 2 ? ((pass_no & 1) ? 0xAAAAAAAAu : 0x55555555u) : 0u); pass_no++; \
        for (int b = 0; b < nb; b++) { cl_ulong n = sizes[b] / 4; cl_uint pat = base + (cl_uint)b * 0x7F4A7C15u; cl_uint cpat = (inject && pass_no == 4 && b == 0) ? pat ^ 1u : pat; \
            p_clSetKernelArg(kw, 0, sizeof(cl_mem), &bufs[b]); p_clSetKernelArg(kw, 1, sizeof n, &n); p_clSetKernelArg(kw, 2, sizeof pat, &pat); p_clSetKernelArg(kw, 3, sizeof mode, &mode); \
            e = p_clEnqueueNDRangeKernel(q, kw, 1, NULL, &gw, NULL, 0, NULL, NULL); \
            if (e != CL_SUCCESS) { dev_fail(d, "lancement de l'écriture mémoire", e); goto mdone; } \
            p_clSetKernelArg(kc, 0, sizeof(cl_mem), &bufs[b]); p_clSetKernelArg(kc, 1, sizeof n, &n); p_clSetKernelArg(kc, 2, sizeof cpat, &cpat); p_clSetKernelArg(kc, 3, sizeof mode, &mode); \
            p_clSetKernelArg(kc, 4, sizeof(cl_mem), &errb[N]); \
            e = p_clEnqueueNDRangeKernel(q, kc, 1, NULL, &gw, NULL, 0, NULL, NULL); \
            if (e != CL_SUCCESS) { dev_fail(d, "lancement de la lecture mémoire", e); goto mdone; } } \
        e = p_clEnqueueReadBuffer(q, errb[N], 0, 0, sizeof(cl_uint) * 16, (S)->err, 0, NULL, &(S)->ev); \
        p_clFlush(q); if (e != CL_SUCCESS) { dev_fail(d, "file de commandes (mémoire)", e); goto mdone; } } while (0)
    #define MPROCESS(S) do { cl_int wc; int okw = wait_ok((S)->ev, &wc); (S)->ev = NULL; \
        if (!okw) { dev_fail(d, "passe mémoire échouée (reset du GPU ?)", wc); goto mdone; } \
        ADD(&d->mem_bytes, 2 * total); ADD(&d->passes, 1); \
        cl_uint n = (S)->err[0]; \
        if (n) { ADD(&d->m_err, n); char m[240]; \
            snprintf(m, sizeof m, "mémoire : %u mot(s) relu(s) différent(s) de ce qui avait été écrit ; 1er : mot %u attendu 0x%08x obtenu 0x%08x", n, (S)->err[1], (S)->err[2], (S)->err[3]); \
            report_error(d, "memory", m, n); } } while (0)
    MISSUE(&slot[0], 0);
    int cur = 0;
    while (!g_stop) { MISSUE(&slot[cur ^ 1], cur ^ 1); MPROCESS(&slot[cur]); cur ^= 1; }
    MPROCESS(&slot[cur]);
mdone:
    p_clFinish(q);
    for (int b = 0; b < nb; b++) p_clReleaseMemObject(bufs[b]);
    return NULL;
}

/* ───────────── énumération ───────────── */
static int enumerate(Dev **out, int want_cpu) {
    cl_uint np = 0; if (p_clGetPlatformIDs(0, NULL, &np) != CL_SUCCESS || np == 0) return 0;
    cl_platform_id *pl = calloc(np, sizeof *pl); p_clGetPlatformIDs(np, pl, NULL);
    Dev *list = calloc(256, sizeof(Dev)); int n = 0;
    for (cl_uint i = 0; i < np; i++) {
        cl_uint nd = 0; cl_bitfield tp = want_cpu ? CL_DEVICE_TYPE_ALL : (CL_DEVICE_TYPE_GPU | CL_DEVICE_TYPE_ACCELERATOR);
        if (p_clGetDeviceIDs(pl[i], tp, 0, NULL, &nd) != CL_SUCCESS || nd == 0) continue;
        cl_device_id *ds = calloc(nd, sizeof *ds); p_clGetDeviceIDs(pl[i], tp, nd, ds, NULL);
        for (cl_uint j = 0; j < nd && n < 256; j++) {
            Dev *d = &list[n]; d->idx = n; d->plat = pl[i]; d->dev = ds[j];
            p_clGetPlatformInfo(pl[i], CL_PLATFORM_NAME, sizeof d->plat_name - 1, d->plat_name, NULL);
            p_clGetDeviceInfo(ds[j], CL_DEVICE_NAME, sizeof d->name - 1, d->name, NULL);
            p_clGetDeviceInfo(ds[j], CL_DEVICE_EXTENSIONS, sizeof d->exts - 1, d->exts, NULL);
            cl_bitfield t = 0; p_clGetDeviceInfo(ds[j], CL_DEVICE_TYPE, sizeof t, &t, NULL);
            strcpy(d->type, (t & CL_DEVICE_TYPE_GPU) ? "GPU" : (t & CL_DEVICE_TYPE_CPU) ? "CPU" : "ACC");
            p_clGetDeviceInfo(ds[j], CL_DEVICE_MAX_COMPUTE_UNITS, sizeof d->cu, &d->cu, NULL);
            p_clGetDeviceInfo(ds[j], CL_DEVICE_MAX_CLOCK_FREQUENCY, sizeof d->mhz, &d->mhz, NULL);
            p_clGetDeviceInfo(ds[j], CL_DEVICE_GLOBAL_MEM_SIZE, sizeof d->gmem, &d->gmem, NULL);
            p_clGetDeviceInfo(ds[j], CL_DEVICE_MAX_MEM_ALLOC_SIZE, sizeof d->maxalloc, &d->maxalloc, NULL);
            cl_uint hu = 0; p_clGetDeviceInfo(ds[j], CL_DEVICE_HOST_UNIFIED_MEMORY, sizeof hu, &hu, NULL);
            d->igpu = hu ? 1 : 0; d->fp16 = strstr(d->exts, "cl_khr_fp16") ? 1 : 0;
            d->pci[0] = 0;       /* adresse PCI : permet à BenchSuite d'associer ce périphérique OpenCL au GPU de sa télémétrie */
            if (strstr(d->exts, "cl_khr_pci_bus_info")) {
                cl_uint v[4] = {0, 0, 0, 0};
                if (p_clGetDeviceInfo(ds[j], CL_DEVICE_PCI_BUS_INFO_KHR, sizeof v, v, NULL) == CL_SUCCESS) snprintf(d->pci, sizeof d->pci, "%04x:%02x:%02x.%x", v[0], v[1], v[2], v[3]);
            }
            if (!d->pci[0] && strstr(d->exts, "cl_nv_device_attribute_query")) {
                cl_uint bus = 0, slot = 0;
                if (p_clGetDeviceInfo(ds[j], CL_DEVICE_PCI_BUS_ID_NV, sizeof bus, &bus, NULL) == CL_SUCCESS && p_clGetDeviceInfo(ds[j], CL_DEVICE_PCI_SLOT_ID_NV, sizeof slot, &slot, NULL) == CL_SUCCESS)
                    snprintf(d->pci, sizeof d->pci, "0000:%02x:%02x.%x", bus & 0xff, (slot >> 3) & 0x1f, slot & 7);
            }
            if (!d->pci[0] && strstr(d->exts, "cl_amd_device_attribute_query")) {
                unsigned char top[24]; memset(top, 0, sizeof top);
                if (p_clGetDeviceInfo(ds[j], CL_DEVICE_TOPOLOGY_AMD, sizeof top, top, NULL) == CL_SUCCESS && top[0] == 1)   /* type 1 = PCIe */
                    snprintf(d->pci, sizeof d->pci, "0000:%02x:%02x.%x", top[21], top[22], top[23]);
            }
            n++;
        }
        free(ds);
    }
    free(pl); *out = list; return n;
}

static void usage(void) {
    fprintf(stderr, "usage : oclburn -l | [-d 0,1] [-t secondes] [-m %%VRAM] [-M all|power|compute|memory] [-i intervalle] [-w work-items/CU] [-F 1|2|4] [--cpu]\n");
}

int main(int argc, char **argv) {
    int list_only = 0, want_cpu = 0, vram_pct = 90, wmul = 4096; double dur = 60, interval = 5; char devsel[256] = ""; char mode[16] = "all";
    for (int i = 1; i < argc; i++) {
        if (!strcmp(argv[i], "-l")) list_only = 1;
        else if (!strcmp(argv[i], "--cpu")) want_cpu = 1;
        else if (!strcmp(argv[i], "-d") && i + 1 < argc) snprintf(devsel, sizeof devsel, "%s", argv[++i]);
        else if (!strcmp(argv[i], "-t") && i + 1 < argc) dur = atof(argv[++i]);
        else if (!strcmp(argv[i], "-m") && i + 1 < argc) vram_pct = atoi(argv[++i]);
        else if (!strcmp(argv[i], "-M") && i + 1 < argc) snprintf(mode, sizeof mode, "%s", argv[++i]);
        else if (!strcmp(argv[i], "-i") && i + 1 < argc) interval = atof(argv[++i]);
        else if (!strcmp(argv[i], "-w") && i + 1 < argc) wmul = atoi(argv[++i]);
        else if (!strcmp(argv[i], "-F") && i + 1 < argc) { g_fold = atoi(argv[++i]); if (g_fold != 2 && g_fold != 4) g_fold = 1; }
        else { usage(); return 1; }
    }
    if (vram_pct < 5) vram_pct = 5;
    if (vram_pct > 95) vram_pct = 95;
    if (wmul < 64) wmul = 64;
    if (cl_load() != 0) return 2;
    Dev *all = NULL; int nall = enumerate(&all, want_cpu || list_only == 2);
    if (list_only) {
        for (int i = 0; i < nall; i++) {
            Dev *d = &all[i];
            printf("DEVICE idx=%d platform=\"%s\" name=\"%s\" type=%s cu=%u mhz=%u mem_mb=%llu maxalloc_mb=%llu fp16=%d igpu=%d pci=%s\n", d->idx, d->plat_name, d->name, d->type, d->cu, d->mhz,
                   (unsigned long long)(d->gmem >> 20), (unsigned long long)(d->maxalloc >> 20), d->fp16, d->igpu, d->pci[0] ? d->pci : "-");
        }
        if (!nall) { printf("NODEVICE\n"); return 2; }
        return 0;
    }
    if (!nall) { printf("NODEVICE aucun GPU OpenCL (pilote OpenCL absent ? clinfo -l)\n"); return 2; }
    /* sélection */
    int sel[256], nsel = 0;
    if (devsel[0] && strcmp(devsel, "all")) {
        for (char *tok = strtok(devsel, ","); tok; tok = strtok(NULL, ",")) {
            int k = atoi(tok), dup = 0;
            for (int z = 0; z < nsel; z++) if (sel[z] == k) dup = 1;      /* un périphérique ne peut être chargé deux fois */
            if (k >= 0 && k < nall && !dup) sel[nsel++] = k;
        }
    } else for (int i = 0; i < nall; i++) sel[nsel++] = i;
    if (!nsel) { printf("NODEVICE sélection vide\n"); return 2; }
    int power_only = strcmp(mode, "power") == 0;
    int do_c = strcmp(mode, "memory") != 0, do_m = strcmp(mode, "compute") != 0 && !power_only;
    signal(SIGINT, on_sig); signal(SIGTERM, on_sig);
    g_start = now_s();
    Dev **act = calloc(nsel, sizeof *act);
    for (int s = 0; s < nsel; s++) {
        Dev *d = &all[sel[s]]; act[s] = d; cl_int e;
        cl_prop props[3] = { CL_CONTEXT_PLATFORM, (cl_prop)d->plat, 0 };
        d->ctx = p_clCreateContext(props, 1, &d->dev, NULL, NULL, &e);
        if (e != CL_SUCCESS) { dev_fail(d, "clCreateContext", e); continue; }
        d->vram_pct = d->igpu ? (vram_pct > 30 ? 30 : vram_pct) : vram_pct;     /* iGPU / CPU : mémoire partagée avec le système */
        d->global = (size_t)d->cu * (size_t)wmul; if (d->global < 1024) d->global = 1024; d->global = (d->global + 255) / 256 * 256;
        d->do_compute = do_c; d->do_memory = do_m; d->power_only = power_only;
        printf("START dev=%d name=\"%s\" platform=\"%s\" type=%s cu=%u mem_mb=%llu fp16=%d mode=%s duration=%.0f vram_pct=%d global=%zu pci=%s\n", d->idx, d->name, d->plat_name, d->type, d->cu,
               (unsigned long long)(d->gmem >> 20), d->fp16, mode, dur, d->vram_pct, d->global, d->pci[0] ? d->pci : "-");
        fflush(stdout);
        if (do_c) d->run_c = pthread_create(&d->th_c, NULL, compute_worker, d) == 0;
        if (do_m) d->run_m = pthread_create(&d->th_m, NULL, memory_worker, d) == 0;
    }
    /* suivi : une ligne STAT par périphérique et par intervalle */
    uint64_t last[256][6]; memset(last, 0, sizeof last); double last_t = g_start;
    while (!g_stop) {
        usleep(200000);
        double t = now_s();
        if (dur > 0 && t - g_start >= dur) { g_stop = 1; break; }
        int any_failed = 0, all_failed = 1;
        for (int s = 0; s < nsel; s++) { if (__atomic_load_n(&act[s]->failed, __ATOMIC_ACQUIRE)) any_failed = 1; else all_failed = 0; }
        (void)any_failed; if (all_failed) { g_stop = 1; break; }
        if (t - last_t >= interval) {
            double dt = t - last_t;
            for (int s = 0; s < nsel; s++) {
                Dev *d = act[s]; if (__atomic_load_n(&d->failed, __ATOMIC_ACQUIRE)) continue;
                uint64_t v[6] = { GET(&d->fp32_flops), GET(&d->fp16_flops), GET(&d->int_ops), GET(&d->mem_bytes), GET(&d->c_err), GET(&d->m_err) };
                pthread_mutex_lock(&g_print);
                printf("STAT t=%.1f dev=%d fp32_gflops=%.1f fp16_gflops=%.1f giops=%.1f mem_gbs=%.1f compute_errors=%llu memory_errors=%llu\n", t - g_start, d->idx,
                       (v[0] - last[s][0]) / dt / 1e9, (v[1] - last[s][1]) / dt / 1e9, (v[2] - last[s][2]) / dt / 1e9, (v[3] - last[s][3]) / dt / 1e9,
                       (unsigned long long)v[4], (unsigned long long)v[5]);
                fflush(stdout); pthread_mutex_unlock(&g_print);
                memcpy(last[s], v, sizeof v);
            }
            last_t = t;
        }
    }
    g_stop = 1;
    for (int s = 0; s < nsel; s++) { Dev *d = act[s]; if (d->run_c) pthread_join(d->th_c, NULL); if (d->run_m) pthread_join(d->th_m, NULL); }
    double tot = now_s() - g_start; uint64_t errs = 0; int failed = 0;
    for (int s = 0; s < nsel; s++) {
        Dev *d = act[s];
        errs += d->c_err + d->m_err; failed |= d->failed;
        printf("RESULT dev=%d name=\"%s\" t=%.1f fp32_gflops=%.1f fp16_gflops=%.1f giops=%.1f mem_gbs=%.1f mem_tested_mb=%llu compute_errors=%llu memory_errors=%llu launches=%llu passes=%llu%s\n",
               d->idx, d->name, tot, d->fp32_flops / tot / 1e9, d->fp16_flops / tot / 1e9, d->int_ops / tot / 1e9, d->mem_bytes / tot / 1e9,
               (unsigned long long)(d->mem_tested >> 20), (unsigned long long)d->c_err, (unsigned long long)d->m_err, (unsigned long long)d->launches,
               (unsigned long long)d->passes, d->failed ? " failed=1" : "");
    }
    printf("SUMMARY status=%s errors=%llu devices=%d duration=%.1f\n", failed ? "FAILED" : (errs ? "FAIL" : "PASS"), (unsigned long long)errs, nsel, tot);
    fflush(stdout);
    return failed ? 2 : (errs ? 3 : 0);
}
