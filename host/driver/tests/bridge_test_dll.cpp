// Test-only wrapper; never linked into the kernel driver or running Host.
#include "rv_pcm_bridge.h"
static RvPcmBridge bridge;

extern "C" __declspec(dllexport) void rv_test_reset() {
    // Tests call this only after all preceding writer workers have joined.
    rv_test_now.store(1);
    bridge.Initialize();
}
extern "C" __declspec(dllexport) int32_t rv_test_send(void* value, uint32_t size, uint32_t caller) {
    PCPROPERTY_REQUEST request{&caller, KSPROPERTY_TYPE_SET, 0, size, value};
    return bridge.Property(&request);
}
extern "C" __declspec(dllexport) void rv_test_read(uint8_t* output, uint32_t size) {
    bridge.Read(output, size);
}
extern "C" __declspec(dllexport) void rv_test_advance(uint64_t ticks) {
    rv_test_now.fetch_add(ticks);
}
