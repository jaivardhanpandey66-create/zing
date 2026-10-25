/* zing C runtime: a tagged value, plus the bits a compiled program needs.
 *
 * This is deliberately small. The compiled code carries the logic; the
 * runtime only provides the value representation and a few helpers that
 * would be tedious to emit inline (printing, string building, bounds
 * checks).
 *
 * Build with: cc -O2 program.c -o program -lm
 */
#ifndef ZING_RUNTIME_H
#define ZING_RUNTIME_H

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <math.h>
#include <stdint.h>
#include <stdarg.h>

typedef enum {
    Z_NULL,
    Z_BOOL,
    Z_INT,
    Z_FLOAT,
    Z_STR,
    Z_LIST,
    Z_MAP
} ZKind;

typedef struct ZValue ZValue;

typedef struct {
    char *data;
    size_t len;
    size_t cap;
} ZStr;

typedef struct {
    ZValue *items;
    size_t len;
    size_t cap;
} ZList;

/* A tiny open-addressing map keyed by string. Enough for program output and
 * simple dynamic maps; not tuned for huge tables. */
typedef struct {
    char **keys;
    ZValue *vals;
    size_t len;
    size_t cap;
} ZMap;

struct ZValue {
    ZKind kind;
    union {
        int b;
        long long i;
        double f;
        ZStr *s;
        ZList *l;
        ZMap *m;
    } as;
};

/* ---- constructors ---- */

static inline ZValue z_null(void) {
    ZValue v; v.kind = Z_NULL; v.as.i = 0; return v;
}
static inline ZValue z_bool(int b) {
    ZValue v; v.kind = Z_BOOL; v.as.b = b ? 1 : 0; return v;
}
static inline ZValue z_int(long long i) {
    ZValue v; v.kind = Z_INT; v.as.i = i; return v;
}
static inline ZValue z_float(double f) {
    ZValue v; v.kind = Z_FLOAT; v.as.f = f; return v;
}

static ZValue z_str(const char *data, size_t len) {
    ZStr *s = (ZStr *)malloc(sizeof(ZStr));
    s->cap = len + 1;
    s->data = (char *)malloc(s->cap);
    memcpy(s->data, data, len);
    s->data[len] = '\0';
    s->len = len;
    ZValue v; v.kind = Z_STR; v.as.s = s; return v;
}
static ZValue z_str_cstr(const char *c) { return z_str(c, strlen(c)); }

static ZValue z_list(void) {
    ZList *l = (ZList *)malloc(sizeof(ZList));
    l->cap = 8;
    l->len = 0;
    l->items = (ZValue *)malloc(sizeof(ZValue) * l->cap);
    ZValue v; v.kind = Z_LIST; v.as.l = l; return v;
}
static void z_list_push(ZValue list, ZValue item) {
    ZList *l = list.as.l;
    if (l->len == l->cap) {
        l->cap *= 2;
        l->items = (ZValue *)realloc(l->items, sizeof(ZValue) * l->cap);
    }
    l->items[l->len++] = item;
}

/* ---- maps: a small string-keyed table ---- */

static ZValue z_map(void) {
    ZMap *m = (ZMap *)malloc(sizeof(ZMap));
    m->cap = 8;
    m->len = 0;
    m->keys = (char **)calloc(m->cap, sizeof(char *));
    m->vals = (ZValue *)malloc(sizeof(ZValue) * m->cap);
    ZValue v; v.kind = Z_MAP; v.as.m = m; return v;
}

static long z_map_find(ZMap *m, const char *key) {
    for (size_t i = 0; i < m->len; i++)
        if (strcmp(m->keys[i], key) == 0) return (long)i;
    return -1;
}

static void z_map_put(ZValue map, const char *key, ZValue val) {
    ZMap *m = map.as.m;
    long at = z_map_find(m, key);
    if (at >= 0) { m->vals[at] = val; return; }
    if (m->len == m->cap) {
        m->cap *= 2;
        m->keys = (char **)realloc(m->keys, sizeof(char *) * m->cap);
        m->vals = (ZValue *)realloc(m->vals, sizeof(ZValue) * m->cap);
    }
    m->keys[m->len] = strdup(key);
    m->vals[m->len] = val;
    m->len++;
}

static int z_map_has(ZValue map, const char *key) {
    return z_map_find(map.as.m, key) >= 0;
}


/* ---- truthiness and equality ---- */

static int z_truthy(ZValue v) {
    switch (v.kind) {
        case Z_NULL: return 0;
        case Z_BOOL: return v.as.b;
        case Z_INT: return v.as.i != 0;
        case Z_FLOAT: return v.as.f != 0.0;
        case Z_STR: return v.as.s->len != 0;
        case Z_LIST: return v.as.l->len != 0;
        case Z_MAP: return v.as.m->len != 0;
    }
    return 0;
}

static int z_eq(ZValue a, ZValue b) {
    if ((a.kind == Z_INT || a.kind == Z_FLOAT) &&
        (b.kind == Z_INT || b.kind == Z_FLOAT)) {
        double x = a.kind == Z_INT ? (double)a.as.i : a.as.f;
        double y = b.kind == Z_INT ? (double)b.as.i : b.as.f;
        return x == y;
    }
    if (a.kind != b.kind) return 0;
    switch (a.kind) {
        case Z_NULL: return 1;
        case Z_BOOL: return a.as.b == b.as.b;
        case Z_STR:
            return a.as.s->len == b.as.s->len &&
                   memcmp(a.as.s->data, b.as.s->data, a.as.s->len) == 0;
        case Z_LIST: {
            if (a.as.l->len != b.as.l->len) return 0;
            for (size_t i = 0; i < a.as.l->len; i++)
                if (!z_eq(a.as.l->items[i], b.as.l->items[i])) return 0;
            return 1;
        }
        default: return a.as.i == b.as.i; /* identity for maps */
    }
}

/* ---- printing ---- */

static void z_print_value(ZValue v);

static void z_print(ZValue v) {
    z_print_value(v);
    printf("\n");
}

static void z_print_value(ZValue v) {
    switch (v.kind) {
        case Z_NULL: printf("null"); break;
        case Z_BOOL: printf("%s", v.as.b ? "true" : "false"); break;
        case Z_INT: printf("%lld", v.as.i); break;
        case Z_FLOAT: {
            double f = v.as.f;
            if (f == floor(f) && isfinite(f))
                printf("%.1f", f);
            else
                printf("%g", f);
            break;
        }
        case Z_STR: fwrite(v.as.s->data, 1, v.as.s->len, stdout); break;
        case Z_LIST: {
            printf("[");
            for (size_t i = 0; i < v.as.l->len; i++) {
                if (i) printf(", ");
                z_print_value(v.as.l->items[i]);
            }
            printf("]");
            break;
        }
        case Z_MAP: {
            printf("{");
            for (size_t i = 0; i < v.as.m->len; i++) {
                if (i) printf(", ");
                printf("%s: ", v.as.m->keys[i]);
                z_print_value(v.as.m->vals[i]);
            }
            printf("}");
            break;
        }
    }
}

/* ---- conversions ---- */

static double z_to_float(ZValue v) {
    switch (v.kind) {
        case Z_INT: return (double)v.as.i;
        case Z_FLOAT: return v.as.f;
        case Z_BOOL: return v.as.b ? 1.0 : 0.0;
        default: return 0.0;
    }
}
static long long z_to_int(ZValue v) { return (long long)z_to_float(v); }

static int z_is_int(ZValue v) {
    return v.kind == Z_INT || v.kind == Z_BOOL;
}

static ZValue z_add(ZValue a, ZValue b) {
    if (a.kind == Z_STR || b.kind == Z_STR) return z_null(); /* use concat */
    if (z_is_int(a) && z_is_int(b))
        return z_int(a.as.i + b.as.i);
    return z_float(z_to_float(a) + z_to_float(b));
}
static ZValue z_sub(ZValue a, ZValue b) {
    if (z_is_int(a) && z_is_int(b))
        return z_int(a.as.i - b.as.i);
    return z_float(z_to_float(a) - z_to_float(b));
}
static ZValue z_mul(ZValue a, ZValue b) {
    if (z_is_int(a) && z_is_int(b))
        return z_int(a.as.i * b.as.i);
    return z_float(z_to_float(a) * z_to_float(b));
}
static ZValue z_div(ZValue a, ZValue b) { return z_float(z_to_float(a) / z_to_float(b)); }
static ZValue z_mod(ZValue a, ZValue b) {
    if (z_is_int(a) && z_is_int(b) && b.as.i != 0)
        return z_int(a.as.i % b.as.i);
    return z_float(fmod(z_to_float(a), z_to_float(b)));
}

/* string concat: zing's + when either side is a string */
static ZValue z_concat(ZValue a, ZValue b);

static size_t z_str_length(ZValue v, char *buf, size_t bufsize) {
    if (v.kind == Z_STR) return v.as.s->len;
    int n = snprintf(buf, bufsize, "%s", "");
    (void)n;
    return 0;
}

/* Turn any value into a fresh heap string, used by `+` when a string is
 * involved and by str(). */
static ZValue z_to_str(ZValue v) {
    if (v.kind == Z_STR) return z_str(v.as.s->data, v.as.s->len);
    char buf[64];
    int n = 0;
    switch (v.kind) {
        case Z_NULL: return z_str_cstr("null");
        case Z_BOOL: return z_str_cstr(v.as.b ? "true" : "false");
        case Z_INT: n = snprintf(buf, sizeof buf, "%lld", v.as.i); break;
        case Z_FLOAT: {
            double f = v.as.f;
            if (f == floor(f) && isfinite(f))
                n = snprintf(buf, sizeof buf, "%.1f", f);
            else
                n = snprintf(buf, sizeof buf, "%g", f);
            break;
        }
        default: return z_str_cstr("");
    }
    return z_str(buf, (size_t)n);
}

static ZValue z_concat(ZValue a, ZValue b) {
    ZValue sa = z_to_str(a);
    ZValue sb = z_to_str(b);
    size_t len = sa.as.s->len + sb.as.s->len;
    char *merged = (char *)malloc(len + 1);
    memcpy(merged, sa.as.s->data, sa.as.s->len);
    memcpy(merged + sa.as.s->len, sb.as.s->data, sb.as.s->len);
    merged[len] = '\0';
    ZValue out = z_str(merged, len);
    free(merged);
    return out;
}

static long long z_len(ZValue v) {
    switch (v.kind) {
        case Z_STR: return (long long)v.as.s->len;
        case Z_LIST: return (long long)v.as.l->len;
        case Z_MAP: return (long long)v.as.m->len;
        default: return 0;
    }
}

/* print(a, b, ...): values separated by a space, then a newline */
static ZValue z_print_multi(int count, ...) {
    va_list ap;
    va_start(ap, count);
    for (int i = 0; i < count; i++) {
        if (i) printf(" ");
        z_print_value(va_arg(ap, ZValue));
    }
    va_end(ap);
    printf("\n");
    return z_null();
}

/* ---- map and index helpers (need z_to_str and z_to_int above) ---- */

static ZValue z_map_get(ZValue map, const char *key) {
    long at = z_map_find(map.as.m, key);
    return at >= 0 ? map.as.m->vals[at] : z_null();
}

/* Any value used as a map key becomes its text form, matching the
 * interpreter's treatment of bare-name keys in a map literal. */
static ZValue z_key_str(ZValue key) {
    return z_to_str(key);
}

static ZValue z_index_get(ZValue target, ZValue key) {
    if (target.kind == Z_LIST) {
        long long i = z_to_int(key);
        long long n = (long long)target.as.l->len;
        if (i < 0) i += n;
        if (i < 0 || i >= n) return z_null();
        return target.as.l->items[i];
    }
    if (target.kind == Z_STR) {
        long long i = z_to_int(key);
        long long n = (long long)target.as.s->len;
        if (i < 0) i += n;
        if (i < 0 || i >= n) return z_null();
        return z_str(target.as.s->data + i, 1);
    }
    return z_null();
}

static void z_index_set(ZValue target, ZValue key, ZValue val) {
    if (target.kind != Z_LIST) return;
    long long i = z_to_int(key);
    long long n = (long long)target.as.l->len;
    if (i < 0) i += n;
    if (i < 0 || i >= n) return;
    target.as.l->items[i] = val;
}

/* range(a, b, step): a list of numbers, matching the interpreter. A single
 * argument means 0..a; the step defaults to 1 and may be negative. */
static ZValue z_range3(long long start, long long stop, long long step) {
    ZValue out = z_list();
    if (step == 0) return out;
    if (step > 0) {
        for (long long i = start; i < stop; i += step)
            z_list_push(out, z_int(i));
    } else {
        for (long long i = start; i > stop; i += step)
            z_list_push(out, z_int(i));
    }
    return out;
}

/* Normalize a slice bound: negative counts from the end, and everything is
 * clamped into [0, size]. A larger-than-size bound just means "the end". */static long long z_slice_bound(long long v, long long size) {
    if (v < 0) v += size;
    if (v < 0) return 0;
    if (v > size) return size;
    return v;
}

/* xs[a:b] on a list or string. `has_start`/`has_stop` say whether the bound
 * was written; a missing bound means 0 or the end respectively. */
static ZValue z_slice(ZValue target, int has_start, long long start,
                      int has_stop, long long stop) {
    if (target.kind == Z_LIST) {
        long long size = (long long)target.as.l->len;
        long long a = has_start ? z_slice_bound(start, size) : 0;
        long long b = has_stop ? z_slice_bound(stop, size) : size;
        if (b < a) b = a;
        ZValue out = z_list();
        for (long long i = a; i < b; i++)
            z_list_push(out, target.as.l->items[i]);
        return out;
    }
    if (target.kind == Z_STR) {
        long long size = (long long)target.as.s->len;
        long long a = has_start ? z_slice_bound(start, size) : 0;
        long long b = has_stop ? z_slice_bound(stop, size) : size;
        if (b < a) b = a;
        return z_str(target.as.s->data + a, (size_t)(b - a));
    }
    return z_null();
}

#endif /* ZING_RUNTIME_H */
