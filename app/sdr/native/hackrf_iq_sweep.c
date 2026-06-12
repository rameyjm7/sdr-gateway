#include <errno.h>
#include <inttypes.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/time.h>
#include <unistd.h>

#include <libhackrf/hackrf.h>

#define FRAME_MAGIC "IQSWP1\0\0"
#define FRAME_HEADER_LEN 40u
#define MAX_SWEEP_BYTES (16u * 1024u * 1024u)

static volatile sig_atomic_t stop_requested = 0;

struct context {
    uint32_t sample_rate_sps;
    uint64_t frames_written;
};

static void handle_signal(int signum) {
    (void)signum;
    stop_requested = 1;
}

static uint64_t now_us(void) {
    struct timeval tv;
    gettimeofday(&tv, NULL);
    return ((uint64_t)tv.tv_sec * 1000000ULL) + (uint64_t)tv.tv_usec;
}

static void put_u32_le(uint8_t* dst, uint32_t value) {
    dst[0] = (uint8_t)(value & 0xffu);
    dst[1] = (uint8_t)((value >> 8) & 0xffu);
    dst[2] = (uint8_t)((value >> 16) & 0xffu);
    dst[3] = (uint8_t)((value >> 24) & 0xffu);
}

static void put_u64_le(uint8_t* dst, uint64_t value) {
    for (int i = 0; i < 8; i++) {
        dst[i] = (uint8_t)((value >> (8 * i)) & 0xffu);
    }
}

static uint64_t get_u64_le(const uint8_t* src) {
    uint64_t value = 0;
    for (int i = 0; i < 8; i++) {
        value |= ((uint64_t)src[i]) << (8 * i);
    }
    return value;
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

static double parse_double(const char* text, const char* name) {
    char* end = NULL;
    errno = 0;
    double value = strtod(text, &end);
    if (errno != 0 || end == text || *end != '\0' || value < 0.0) {
        fprintf(stderr, "invalid %s: %s\n", name, text);
        exit(2);
    }
    return value;
}

static void write_frame(struct context* ctx, uint64_t freq_hz, const uint8_t* payload, uint32_t payload_len) {
    uint8_t header[FRAME_HEADER_LEN];
    memset(header, 0, sizeof(header));
    memcpy(header, FRAME_MAGIC, 8);
    put_u32_le(header + 8, FRAME_HEADER_LEN);
    put_u32_le(header + 12, (uint32_t)(ctx->frames_written & 0xffffffffu));
    put_u64_le(header + 16, freq_hz);
    put_u32_le(header + 24, ctx->sample_rate_sps);
    put_u32_le(header + 28, payload_len);
    put_u64_le(header + 32, now_us());

    if (fwrite(header, 1, sizeof(header), stdout) != sizeof(header)) {
        stop_requested = 1;
        return;
    }
    if (payload_len > 0 && fwrite(payload, 1, payload_len, stdout) != payload_len) {
        stop_requested = 1;
        return;
    }
    fflush(stdout);
    ctx->frames_written++;
}

static int rx_sweep_callback(hackrf_transfer* transfer) {
    struct context* ctx = (struct context*)transfer->rx_ctx;
    uint8_t* ptr = transfer->buffer;
    int remaining = transfer->valid_length;
    const int block_bytes = BYTES_PER_BLOCK;

    while (!stop_requested && remaining >= 10) {
        int this_block = remaining < block_bytes ? remaining : block_bytes;
        if (ptr[0] != 0x7f || ptr[1] != 0x7f || this_block <= 10) {
            break;
        }
        uint64_t freq_hz = get_u64_le(ptr + 2);
        write_frame(ctx, freq_hz, ptr + 10, (uint32_t)(this_block - 10));
        ptr += this_block;
        remaining -= this_block;
    }
    return stop_requested ? -1 : 0;
}

static void usage(const char* argv0) {
    fprintf(stderr,
        "usage: %s --freqs hz,hz | --start-hz hz --stop-hz hz --hop-hz hz "
        "--sample-rate-sps hz [--lna-gain-db n] [--vga-gain-db n] "
        "[--amp-enable 0|1] [--baseband-filter-hz hz] [--chunk-bytes n] [--dwell-s seconds]\n",
        argv0);
}

int main(int argc, char** argv) {
    const char* freqs_text = NULL;
    uint64_t start_hz = 0, stop_hz = 0, hop_hz = 0;
    uint32_t sample_rate_sps = 2000000;
    uint32_t lna_gain_db = 16;
    uint32_t vga_gain_db = 20;
    uint32_t amp_enable = 0;
    uint32_t baseband_filter_hz = 0;
    uint32_t chunk_bytes = BYTES_PER_BLOCK;
    double dwell_s = 0.0;

    for (int i = 1; i < argc; i++) {
        if (strcmp(argv[i], "--freqs") == 0 && i + 1 < argc) freqs_text = argv[++i];
        else if (strcmp(argv[i], "--start-hz") == 0 && i + 1 < argc) start_hz = parse_u64(argv[++i], "start-hz");
        else if (strcmp(argv[i], "--stop-hz") == 0 && i + 1 < argc) stop_hz = parse_u64(argv[++i], "stop-hz");
        else if (strcmp(argv[i], "--hop-hz") == 0 && i + 1 < argc) hop_hz = parse_u64(argv[++i], "hop-hz");
        else if (strcmp(argv[i], "--sample-rate-sps") == 0 && i + 1 < argc) sample_rate_sps = parse_u32(argv[++i], "sample-rate-sps");
        else if (strcmp(argv[i], "--lna-gain-db") == 0 && i + 1 < argc) lna_gain_db = parse_u32(argv[++i], "lna-gain-db");
        else if (strcmp(argv[i], "--vga-gain-db") == 0 && i + 1 < argc) vga_gain_db = parse_u32(argv[++i], "vga-gain-db");
        else if (strcmp(argv[i], "--amp-enable") == 0 && i + 1 < argc) amp_enable = parse_u32(argv[++i], "amp-enable");
        else if (strcmp(argv[i], "--baseband-filter-hz") == 0 && i + 1 < argc) baseband_filter_hz = parse_u32(argv[++i], "baseband-filter-hz");
        else if (strcmp(argv[i], "--chunk-bytes") == 0 && i + 1 < argc) chunk_bytes = parse_u32(argv[++i], "chunk-bytes");
        else if (strcmp(argv[i], "--dwell-s") == 0 && i + 1 < argc) dwell_s = parse_double(argv[++i], "dwell-s");
        else {
            usage(argv[0]);
            return 2;
        }
    }

    uint16_t ranges[MAX_SWEEP_RANGES * 2];
    int range_count = 0;
    if (freqs_text != NULL && freqs_text[0] != '\0') {
        char* copy = strdup(freqs_text);
        if (copy == NULL) return 2;
        char* saveptr = NULL;
        for (char* tok = strtok_r(copy, ",", &saveptr); tok != NULL; tok = strtok_r(NULL, ",", &saveptr)) {
            if (range_count >= MAX_SWEEP_RANGES) {
                fprintf(stderr, "too many frequencies; max %d\n", MAX_SWEEP_RANGES);
                free(copy);
                return 2;
            }
            uint64_t freq = parse_u64(tok, "freq");
            uint64_t half = hop_hz > 0 ? hop_hz / 2 : sample_rate_sps / 2;
            uint64_t lo = freq > half ? freq - half : freq;
            uint64_t hi = freq + half;
            ranges[range_count * 2] = (uint16_t)(lo / 1000000ULL);
            ranges[(range_count * 2) + 1] = (uint16_t)((hi + 999999ULL) / 1000000ULL);
            if (ranges[range_count * 2] >= ranges[(range_count * 2) + 1]) {
                ranges[(range_count * 2) + 1] = ranges[range_count * 2] + 1;
            }
            range_count++;
        }
        free(copy);
    } else {
        if (start_hz == 0 || stop_hz == 0 || hop_hz == 0 || start_hz >= stop_hz) {
            usage(argv[0]);
            return 2;
        }
        ranges[0] = (uint16_t)(start_hz / 1000000ULL);
        ranges[1] = (uint16_t)((stop_hz + 999999ULL) / 1000000ULL);
        range_count = 1;
    }

    if (hop_hz == 0) hop_hz = sample_rate_sps;
    if (dwell_s > 0.0) {
        double dwell_bytes = dwell_s * (double)sample_rate_sps * 2.0;
        if (dwell_bytes > (double)chunk_bytes) {
            chunk_bytes = dwell_bytes > (double)UINT32_MAX ? UINT32_MAX : (uint32_t)dwell_bytes;
        }
    }
    if (chunk_bytes > MAX_SWEEP_BYTES) chunk_bytes = MAX_SWEEP_BYTES;
    if (chunk_bytes < BYTES_PER_BLOCK) chunk_bytes = BYTES_PER_BLOCK;
    chunk_bytes = (chunk_bytes / BYTES_PER_BLOCK) * BYTES_PER_BLOCK;

    signal(SIGINT, handle_signal);
    signal(SIGTERM, handle_signal);

    int result = hackrf_init();
    if (result != HACKRF_SUCCESS) {
        fprintf(stderr, "hackrf_init failed: %s (%d)\n", hackrf_error_name(result), result);
        return 1;
    }

    hackrf_device* device = NULL;
    result = hackrf_open(&device);
    if (result != HACKRF_SUCCESS) {
        fprintf(stderr, "hackrf_open failed: %s (%d)\n", hackrf_error_name(result), result);
        hackrf_exit();
        return 1;
    }

    hackrf_set_amp_enable(device, (uint8_t)(amp_enable ? 1 : 0));
    hackrf_set_lna_gain(device, lna_gain_db);
    hackrf_set_vga_gain(device, vga_gain_db);
    result = hackrf_set_sample_rate(device, (double)sample_rate_sps);
    if (result != HACKRF_SUCCESS) {
        fprintf(stderr, "hackrf_set_sample_rate failed: %s (%d)\n", hackrf_error_name(result), result);
        hackrf_close(device);
        hackrf_exit();
        return 1;
    }
    if (baseband_filter_hz > 0) {
        hackrf_set_baseband_filter_bandwidth(device, baseband_filter_hz);
    }

    result = hackrf_init_sweep(
        device,
        ranges,
        range_count,
        chunk_bytes,
        (uint32_t)hop_hz,
        sample_rate_sps / 2,
        LINEAR);
    if (result != HACKRF_SUCCESS) {
        fprintf(stderr, "hackrf_init_sweep failed: %s (%d)\n", hackrf_error_name(result), result);
        hackrf_close(device);
        hackrf_exit();
        return 1;
    }

    struct context ctx;
    memset(&ctx, 0, sizeof(ctx));
    ctx.sample_rate_sps = sample_rate_sps;

    result = hackrf_start_rx_sweep(device, rx_sweep_callback, &ctx);
    if (result != HACKRF_SUCCESS) {
        fprintf(stderr, "hackrf_start_rx_sweep failed: %s (%d)\n", hackrf_error_name(result), result);
        hackrf_close(device);
        hackrf_exit();
        return 1;
    }

    while (!stop_requested && hackrf_is_streaming(device) == HACKRF_TRUE) {
        usleep(100000);
    }

    hackrf_stop_rx(device);
    hackrf_close(device);
    hackrf_exit();
    return 0;
}
