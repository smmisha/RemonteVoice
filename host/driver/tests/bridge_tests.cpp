#include "rv_pcm_bridge.h"
#include <cstdio>
#include <cstdlib>

#define CHECK(x) do { if (!(x)) { std::fprintf(stderr, "FAILED line %d: %s\n", __LINE__, #x); std::exit(1); } } while (0)

static NTSTATUS send(RvPcmBridge& bridge, RvMicMessage& message, uint32_t caller = 71) {
    PCPROPERTY_REQUEST request{&caller, KSPROPERTY_TYPE_SET, 0, sizeof(message), &message};
    return bridge.Property(&request);
}
static RvMicMessage msg(uint32_t command, uint8_t value = 0x2a) {
    RvMicMessage m{}; m.version = 1; m.command = command;
    m.session = 0x1122334455667788ULL; m.nonce[0] = 0x92;
    m.byte_count = command == 2 ? sizeof(m.pcm) : 0;
    std::memset(m.pcm, value, sizeof(m.pcm)); return m;
}
static bool uniform(const uint8_t* p, size_t n, uint8_t value) {
    for (size_t i = 0; i < n; ++i) if (p[i] != value) return false;
    return true;
}
int main() {
    RvPcmBridge bridge; bridge.Initialize();
    uint8_t output[1920]; auto begin = msg(1), pcm = msg(2), end = msg(3), ping = msg(4);
    bridge.Read(output, sizeof(output)); CHECK(uniform(output, sizeof(output), 0));
    CHECK(send(bridge, pcm) == STATUS_ACCESS_DENIED); // No voice before explicit begin.
    CHECK(send(bridge, begin) == STATUS_SUCCESS);
    CHECK(send(bridge, pcm) == STATUS_SUCCESS);
    bridge.Read(output, sizeof(output));
    CHECK(uniform(output, 960, 0x2a)); CHECK(uniform(output + 960, 960, 0));
    CHECK(send(bridge, pcm, 72) == STATUS_ACCESS_DENIED);
    CHECK(send(bridge, begin, 72) == STATUS_SHARING_VIOLATION);
    auto wrong = pcm; wrong.nonce[0] ^= 1;
    CHECK(send(bridge, wrong) == STATUS_ACCESS_DENIED);
    wrong = pcm; ++wrong.session; CHECK(send(bridge, wrong) == STATUS_ACCESS_DENIED);
    for (int i = 0; i < 10; ++i) CHECK(send(bridge, pcm) == STATUS_SUCCESS);
    CHECK(send(bridge, pcm) == STATUS_DEVICE_BUSY); // Bounded 100 ms, no overwrite.
    for (int i = 0; i < 10; ++i) {
        bridge.Read(output, 960); CHECK(uniform(output, 960, 0x2a));
        CHECK(send(bridge, pcm) == STATUS_SUCCESS); // Wrap through the ring.
    }
    CHECK(send(bridge, end) == STATUS_SUCCESS);
    bridge.Read(output, sizeof(output)); CHECK(uniform(output, sizeof(output), 0));
    CHECK(send(bridge, begin) == STATUS_SUCCESS); CHECK(send(bridge, pcm) == STATUS_SUCCESS);
    rv_test_now += 2'000'001; CHECK(send(bridge, ping) == STATUS_SUCCESS);
    bridge.Read(output, sizeof(output)); CHECK(uniform(output, sizeof(output), 0));
    CHECK(send(bridge, pcm) == STATUS_SUCCESS); rv_test_now += 2'000'001;
    bridge.Read(output, sizeof(output)); CHECK(uniform(output, sizeof(output), 0));
    rv_test_now += 20'000'001; CHECK(send(bridge, pcm) == STATUS_ACCESS_DENIED);
    CHECK(send(bridge, begin, 72) == STATUS_SUCCESS); CHECK(send(bridge, pcm, 72) == STATUS_SUCCESS);
    bridge.Reset(); CHECK(send(bridge, pcm, 72) == STATUS_ACCESS_DENIED);
    bridge.Read(output, sizeof(output)); CHECK(uniform(output, sizeof(output), 0));
    CHECK(send(bridge, begin) == STATUS_SUCCESS);
    for (int field = 0; field < 7; ++field) {
        auto bad = pcm;
        if (field == 0) bad.version = 2;
        if (field == 1) bad.command = 0;
        if (field == 2) bad.session = 0;
        if (field == 3) std::memset(bad.nonce, 0, sizeof(bad.nonce));
        if (field == 4) bad.reserved = 1;
        if (field == 5) --bad.byte_count;
        if (field == 6) bad.command = 5;
        CHECK(send(bridge, bad) == STATUS_INVALID_PARAMETER);
    }
    CHECK(bridge.Property(nullptr) == STATUS_INVALID_PARAMETER);
    uint32_t caller = 71;
    PCPROPERTY_REQUEST request{&caller, 2, 0, sizeof(pcm), &pcm};
    --request.ValueSize; CHECK(bridge.Property(&request) == STATUS_INVALID_PARAMETER);
    request.ValueSize = sizeof(pcm); request.InstanceSize = 1;
    CHECK(bridge.Property(&request) == STATUS_INVALID_PARAMETER);
    request.InstanceSize = 0; request.Verb = 1;
    CHECK(bridge.Property(&request) == STATUS_INVALID_PARAMETER);
    request.Verb = 2; request.Value = nullptr;
    CHECK(bridge.Property(&request) == STATUS_INVALID_PARAMETER);
    rv_test_irql = 2; CHECK(send(bridge, pcm) == STATUS_INVALID_PARAMETER); rv_test_irql = 0;
    CHECK(send(bridge, pcm, 0) == STATUS_ACCESS_DENIED);
    CHECK(send(bridge, pcm) == STATUS_SUCCESS);
    bridge.Read(output, 3); CHECK(uniform(output, 3, 0)); // Reject split sample, retain packet.
    bridge.Read(output, 960); CHECK(uniform(output, 960, 0x2a));
    bridge.Read(nullptr, 960); bridge.Read(output, 0);
    std::puts("PASS: actual bridge PCM/silence, ownership, ring capacity/wrap, stop/reset, TTL/lease, malformed request and sample alignment");
}
