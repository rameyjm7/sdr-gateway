#include <errno.h>
#include <inttypes.h>
#include <pthread.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#include <libhackrf/hackrf.h>

static volatile sig_atomic_t stop_requested = 0;

struct config {
    uint64_t center_freq_hz;
    uint32_t sample_rate_sps;
    uint32_t lna_gain_db;
    uint32_t vga_gain_db;
    uint32_t amp_enable;
    uint32_t baseband_filter_hz;
    uint64_t max_bytes;
};

struct context {
    hackrf_device* device;
    pthread_mutex_t lock;
    struct config current;
    struct config pending;
    int has_pending;
    uint64_t bytes_written;
};

static void handle_signal(int signum) {
    (void)signum;
    stop_requested = 1;
}

static uint64_t parse_u64(const char* text, const char* name) {
    char* end = NULL;
    errno = 0;
    unsigned long long value = strtoull(text, &end, 10);
    if (errno != 0 || end == text || *end != '\0') {
        fprintf(stderr, "invalid %s: %s\n", name, text);
        exit(2);
    }
    return (uint64_t)value;
}

static uint32_t parse_u32(const char* text, const char* name) {
    uint64_t value = parse_u64(text, name);
    if (value > UINT32_MAX) {
        fprintf(stderr, "%s too large: %" PRIu64 "\n", name, value);
        exit(2);
    }
    return (uint32_t)value;
}

static uint64_t json_u64(const char* line, const char* key, uint64_t fallback) {
    char pattern[64];
    snprintf(pattern, sizeof(pattern), "\"%s\"", key);
    const char* p = strstr(line, pattern);
    if (p == NULL) return fallback;
    p = strchr(p + strlen(pattern), ':');
    if (p == NULL) return fallback;
    p++;
    while (*p == ' ' || *p == '\t' || *p == '"') p++;
    errno = 0;
    char* end = NULL;
    unsigned long long value = strtoull(p, &end, 10);
    if (errno != 0 || end == p) return fallback;
    return (uint64_t)value;
}

static int json_bool(const char* line, const char* key, int fallback) {
    char pattern[64];
    snprintf(pattern, sizeof(pattern), "\"%s\"", key);
    const char* p = strstr(line, pattern);
    if (p == NULL) return fallback;
    p = strchr(p + strlen(pattern), ':');
    if (p == NULL) return fallback;
    p++;
    while (*p == ' ' || *p == '\t' || *p == '"') p++;
    if (strncmp(p, "true", 4) == 0 || strncmp(p, "1", 1) == 0) return 1;
    if (strncmp(p, "false", 5) == 0 || strncmp(p, "0", 1) == 0) return 0;
    return fallback;
}

static void usage(const char* argv0) {
    fprintf(stderr,
        "usage: %s --center-freq-hz hz --sample-rate-sps hz "
        "[--lna-gain-db n] [--vga-gain-db n] [--amp-enable 0|1] "
        "[--baseband-filter-hz hz] [--duration-seconds n] [--num-samples n]\n",
        argv0);
}

static int write_all(const uint8_t* data, size_t len) {
    while (len > 0) {
        ssize_t written = write(STDOUT_FILENO, data, len);
        if (written < 0) {
            if (errno == EINTR) continue;
            return -1;
        }
        if (written == 0) return -1;
        data += written;
        len -= (size_t)written;
    }
    return 0;
}

static int rx_callback(hackrf_transfer* transfer) {
    struct context* ctx = (struct context*)transfer->rx_ctx;
    uint64_t remaining_limit = 0;
    size_t to_write = (size_t)transfer->valid_length;

    pthread_mutex_lock(&ctx->lock);
    if (ctx->current.max_bytes > 0) {
        if (ctx->bytes_written >= ctx->current.max_bytes) {
            pthread_mutex_unlock(&ctx->lock);
            stop_requested = 1;
            return -1;
        }
        remaining_limit = ctx->current.max_bytes - ctx->bytes_written;
        if (to_write > remaining_limit) to_write = (size_t)remaining_limit;
    }
    ctx->bytes_written += to_write;
    pthread_mutex_unlock(&ctx->lock);

    if (to_write > 0 && write_all(transfer->buffer, to_write) != 0) {
        stop_requested = 1;
        return -1;
    }
    if (remaining_limit > 0 && to_write >= remaining_limit) {
        stop_requested = 1;
        return -1;
    }
    return stop_requested ? -1 : 0;
}

static int apply_config(struct context* ctx, const struct config* next, int restart_stream) {
    int result;
    if (restart_stream && hackrf_is_streaming(ctx->device) == HACKRF_TRUE) {
        hackrf_stop_rx(ctx->device);
        while (hackrf_is_streaming(ctx->device) == HACKRF_TRUE && !stop_requested) {
            usleep(10000);
        }
    }

    if (restart_stream || next->sample_rate_sps != ctx->current.sample_rate_sps) {
        result = hackrf_set_sample_rate(ctx->device, (double)next->sample_rate_sps);
        if (result != HACKRF_SUCCESS) {
            fprintf(stderr, "hackrf_set_sample_rate failed: %s (%d)\n", hackrf_error_name(result), result);
            return result;
        }
    }
    if ((restart_stream || next->baseband_filter_hz != ctx->current.baseband_filter_hz) && next->baseband_filter_hz > 0) {
        result = hackrf_set_baseband_filter_bandwidth(ctx->device, next->baseband_filter_hz);
        if (result != HACKRF_SUCCESS) {
            fprintf(stderr, "hackrf_set_baseband_filter_bandwidth failed: %s (%d)\n", hackrf_error_name(result), result);
        }
    }

    result = hackrf_set_freq(ctx->device, next->center_freq_hz);
    if (result != HACKRF_SUCCESS) {
        fprintf(stderr, "hackrf_set_freq failed: %s (%d)\n", hackrf_error_name(result), result);
        return result;
    }

    result = hackrf_set_amp_enable(ctx->device, (uint8_t)(next->amp_enable ? 1 : 0));
    if (result != HACKRF_SUCCESS) fprintf(stderr, "hackrf_set_amp_enable failed: %s (%d)\n", hackrf_error_name(result), result);
    result = hackrf_set_lna_gain(ctx->device, next->lna_gain_db);
    if (result != HACKRF_SUCCESS) fprintf(stderr, "hackrf_set_lna_gain failed: %s (%d)\n", hackrf_error_name(result), result);
    result = hackrf_set_vga_gain(ctx->device, next->vga_gain_db);
    if (result != HACKRF_SUCCESS) fprintf(stderr, "hackrf_set_vga_gain failed: %s (%d)\n", hackrf_error_name(result), result);

    ctx->current = *next;
    fprintf(stderr,
        "retuned center_freq_hz=%" PRIu64 " sample_rate_sps=%u baseband_filter_hz=%u lna=%u vga=%u amp=%u\n",
        ctx->current.center_freq_hz,
        ctx->current.sample_rate_sps,
        ctx->current.baseband_filter_hz,
        ctx->current.lna_gain_db,
        ctx->current.vga_gain_db,
        ctx->current.amp_enable);
    fflush(stderr);

    if (restart_stream && !stop_requested) {
        result = hackrf_start_rx(ctx->device, rx_callback, ctx);
        if (result != HACKRF_SUCCESS) {
            fprintf(stderr, "hackrf_start_rx failed: %s (%d)\n", hackrf_error_name(result), result);
            return result;
        }
    }
    return HACKRF_SUCCESS;
}

static void* control_thread(void* arg) {
    struct context* ctx = (struct context*)arg;
    char line[1024];
    while (!stop_requested && fgets(line, sizeof(line), stdin) != NULL) {
        if (strstr(line, "\"retune\"") == NULL) continue;
        pthread_mutex_lock(&ctx->lock);
        struct config next = ctx->current;
        next.center_freq_hz = json_u64(line, "center_freq_hz", next.center_freq_hz);
        next.sample_rate_sps = (uint32_t)json_u64(line, "sample_rate_sps", next.sample_rate_sps);
        next.baseband_filter_hz = (uint32_t)json_u64(line, "baseband_filter_hz", next.baseband_filter_hz);
        next.lna_gain_db = (uint32_t)json_u64(line, "lna_gain_db", next.lna_gain_db);
        next.vga_gain_db = (uint32_t)json_u64(line, "vga_gain_db", next.vga_gain_db);
        next.amp_enable = (uint32_t)json_bool(line, "amp_enable", (int)next.amp_enable);
        next.max_bytes = 0;
        ctx->pending = next;
        ctx->has_pending = 1;
        pthread_mutex_unlock(&ctx->lock);
    }
    return NULL;
}

int main(int argc, char** argv) {
    struct config initial;
    memset(&initial, 0, sizeof(initial));
    initial.sample_rate_sps = 2000000;
    initial.lna_gain_db = 16;
    initial.vga_gain_db = 20;

    uint32_t duration_seconds = 0;
    uint64_t num_samples = 0;

    for (int i = 1; i < argc; i++) {
        if (strcmp(argv[i], "--center-freq-hz") == 0 && i + 1 < argc) initial.center_freq_hz = parse_u64(argv[++i], "center-freq-hz");
        else if (strcmp(argv[i], "--sample-rate-sps") == 0 && i + 1 < argc) initial.sample_rate_sps = parse_u32(argv[++i], "sample-rate-sps");
        else if (strcmp(argv[i], "--lna-gain-db") == 0 && i + 1 < argc) initial.lna_gain_db = parse_u32(argv[++i], "lna-gain-db");
        else if (strcmp(argv[i], "--vga-gain-db") == 0 && i + 1 < argc) initial.vga_gain_db = parse_u32(argv[++i], "vga-gain-db");
        else if (strcmp(argv[i], "--amp-enable") == 0 && i + 1 < argc) initial.amp_enable = parse_u32(argv[++i], "amp-enable");
        else if (strcmp(argv[i], "--baseband-filter-hz") == 0 && i + 1 < argc) initial.baseband_filter_hz = parse_u32(argv[++i], "baseband-filter-hz");
        else if (strcmp(argv[i], "--duration-seconds") == 0 && i + 1 < argc) duration_seconds = parse_u32(argv[++i], "duration-seconds");
        else if (strcmp(argv[i], "--num-samples") == 0 && i + 1 < argc) num_samples = parse_u64(argv[++i], "num-samples");
        else {
            usage(argv[0]);
            return 2;
        }
    }
    if (initial.center_freq_hz == 0 || initial.sample_rate_sps == 0) {
        usage(argv[0]);
        return 2;
    }
    if (num_samples == 0 && duration_seconds > 0) {
        num_samples = (uint64_t)duration_seconds * (uint64_t)initial.sample_rate_sps;
    }
    initial.max_bytes = num_samples > 0 ? num_samples * 2ULL : 0;

    signal(SIGINT, handle_signal);
    signal(SIGTERM, handle_signal);

    int result = hackrf_init();
    if (result != HACKRF_SUCCESS) {
        fprintf(stderr, "hackrf_init failed: %s (%d)\n", hackrf_error_name(result), result);
        return 1;
    }

    struct context ctx;
    memset(&ctx, 0, sizeof(ctx));
    pthread_mutex_init(&ctx.lock, NULL);

    result = hackrf_open(&ctx.device);
    if (result != HACKRF_SUCCESS) {
        fprintf(stderr, "hackrf_open failed: %s (%d)\n", hackrf_error_name(result), result);
        hackrf_exit();
        return 1;
    }

    result = apply_config(&ctx, &initial, 0);
    if (result != HACKRF_SUCCESS) {
        hackrf_close(ctx.device);
        hackrf_exit();
        return 1;
    }
    result = hackrf_start_rx(ctx.device, rx_callback, &ctx);
    if (result != HACKRF_SUCCESS) {
        fprintf(stderr, "hackrf_start_rx failed: %s (%d)\n", hackrf_error_name(result), result);
        hackrf_close(ctx.device);
        hackrf_exit();
        return 1;
    }

    pthread_t control;
    pthread_create(&control, NULL, control_thread, &ctx);
    pthread_detach(control);

    while (!stop_requested && hackrf_is_streaming(ctx.device) == HACKRF_TRUE) {
        struct config next;
        int has_pending = 0;
        pthread_mutex_lock(&ctx.lock);
        if (ctx.has_pending) {
            next = ctx.pending;
            ctx.has_pending = 0;
            has_pending = 1;
        }
        pthread_mutex_unlock(&ctx.lock);
        if (has_pending) {
            int restart = next.sample_rate_sps != ctx.current.sample_rate_sps ||
                next.baseband_filter_hz != ctx.current.baseband_filter_hz;
            result = apply_config(&ctx, &next, restart);
            if (result != HACKRF_SUCCESS) break;
        }
        usleep(20000);
    }

    if (hackrf_is_streaming(ctx.device) == HACKRF_TRUE) {
        hackrf_stop_rx(ctx.device);
    }
    hackrf_close(ctx.device);
    hackrf_exit();
    return 0;
}
