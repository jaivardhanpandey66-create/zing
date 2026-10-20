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
        default: return a.as.i == b.as.i; /* identity for heap kinds */
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

#endif /* ZING_RUNTIME_H */
