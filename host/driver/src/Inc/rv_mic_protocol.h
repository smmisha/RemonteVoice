// RemoteVoice development PCM ingress ABI. No embedded pointers or variable buffers.
#pragma once
#include <stdint.h>

struct RvMicMessage {
    uint32_t version;
    uint32_t command;  // 1 begin, 2 signed PCM16 mono packet, 3 end, 4 keepalive
    uint64_t session;
    uint8_t nonce[16];
    uint32_t byte_count;
    uint32_t reserved;
    uint8_t pcm[960];  // Exactly 480 mono frames at 48 kHz for command 2.
};
static_assert(sizeof(RvMicMessage) == 1000, "PCM ingress layout changed");
