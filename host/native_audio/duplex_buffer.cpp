// In-process transport prototype, NOT a Windows audio driver or shared-memory ABI.
// One producer and one consumer per direction; join them before destroying a handle.
#include <array>
#include <atomic>
#include <cmath>
#include <cstdint>
#include <new>

namespace {
constexpr std::uint32_t frames = 480;
enum Result { ok = 0, empty = 1, full = 2, invalid = -1, stale = -2, closed = -3 };

// Padding is intentional: independent cursors occupy separate cache lines.
// This private in-process type is never serialized or shared with a driver.
#pragma warning(push)
#pragma warning(disable: 4324)
template<std::size_t channels, std::size_t capacity>
class Ring {
    struct Packet { std::array<float, frames * channels> samples{}; std::uint64_t sequence{}; };
    std::array<Packet, capacity> packets{};
    // Each counter has exactly one writer. A producer never advances the reader.
    alignas(64) std::atomic<std::uint64_t> written{0};
    alignas(64) std::atomic<std::uint64_t> consumed{0};
public:
    int push(const float* data, std::uint64_t sequence) noexcept {
        const auto w = written.load(std::memory_order_relaxed);
        if (w - consumed.load(std::memory_order_acquire) >= capacity) return full;
        auto& packet = packets[w % capacity];
        for (std::size_t i = 0; i < packet.samples.size(); ++i) packet.samples[i] = data[i];
        packet.sequence = sequence;
        written.store(w + 1, std::memory_order_release);
        return ok;
    }
    int pop(float* data, std::uint64_t* sequence) noexcept {
        const auto r = consumed.load(std::memory_order_relaxed);
        if (r == written.load(std::memory_order_acquire)) return empty;
        const auto& packet = packets[r % capacity];
        for (std::size_t i = 0; i < packet.samples.size(); ++i) data[i] = packet.samples[i];
        *sequence = packet.sequence;
        consumed.store(r + 1, std::memory_order_release);
        return ok;
    }
};

struct Duplex {
    const std::uint64_t generation;
    std::atomic<bool> stopped{false};
    Ring<2, 24> system;
    Ring<1, 10> microphone;
    explicit Duplex(std::uint64_t value) : generation(value) {}
};
#pragma warning(pop)
std::uint32_t samples_for(std::uint32_t direction) noexcept {
    return direction == 0 ? frames * 2 : direction == 1 ? frames : 0;
}
}

#define RV_API extern "C" __declspec(dllexport)
RV_API std::uint32_t rv_audio_abi() noexcept { return 1; }
RV_API void* rv_audio_create(std::uint64_t generation) noexcept {
    return generation ? new(std::nothrow) Duplex(generation) : nullptr;
}
RV_API int rv_audio_write(void* handle, std::uint32_t direction, std::uint64_t generation,
                          std::uint64_t sequence, const float* samples, std::uint32_t count) noexcept {
    if (!handle || !samples || !samples_for(direction) || count != samples_for(direction)) return invalid;
    auto* session = static_cast<Duplex*>(handle);
    if (session->generation != generation) return stale;
    if (session->stopped.load(std::memory_order_acquire)) return closed;
    for (std::uint32_t i = 0; i < count; ++i) if (!std::isfinite(samples[i])) return invalid;
    return direction == 0 ? session->system.push(samples, sequence) : session->microphone.push(samples, sequence);
}
RV_API int rv_audio_read(void* handle, std::uint32_t direction, std::uint64_t generation,
                         float* samples, std::uint32_t count, std::uint64_t* sequence) noexcept {
    if (!handle || !samples || !sequence || !samples_for(direction) || count != samples_for(direction)) return invalid;
    // Never expose a previous caller buffer on empty/stale/stopped reads.
    for (std::uint32_t i = 0; i < count; ++i) samples[i] = 0;
    *sequence = 0;
    auto* session = static_cast<Duplex*>(handle);
    if (session->generation != generation) return stale;
    if (session->stopped.load(std::memory_order_acquire)) return closed;
    return direction == 0 ? session->system.pop(samples, sequence) : session->microphone.pop(samples, sequence);
}
RV_API int rv_audio_close(void* handle) noexcept {
    if (!handle) return invalid;
    static_cast<Duplex*>(handle)->stopped.store(true, std::memory_order_release);
    return ok;
}
RV_API void rv_audio_destroy(void* handle) noexcept {
    // Only the owner may destroy, after joining all producer/consumer calls.
    delete static_cast<Duplex*>(handle);
}
