// Exercises the actual bridge implementation with a fake clock/request/IRQL.
// This does not emulate PortCls, kernel scheduling, ACLs or driver installation.
#pragma once
#include <algorithm>
#include <atomic>
#include <cstdint>
#include <cstring>
#include <mutex>
using ULONG = uint32_t;
using UCHAR = uint8_t;
using PUCHAR = UCHAR*;
using ULONGLONG = uint64_t;
using HANDLE = void*;
using KIRQL = unsigned char;
using NTSTATUS = int32_t;
using KSPIN_LOCK = std::mutex;
struct GUID { uint32_t a; uint16_t b, c; uint8_t d[8]; };
struct PCPROPERTY_REQUEST {
    void* Irp;
    ULONG Verb, InstanceSize, ValueSize;
    void* Value;
};
using PPCPROPERTY_REQUEST = PCPROPERTY_REQUEST*;
constexpr KIRQL PASSIVE_LEVEL = 0;
constexpr ULONG KSPROPERTY_TYPE_SET = 2;
constexpr NTSTATUS STATUS_SUCCESS = 0, STATUS_INVALID_PARAMETER = -1,
    STATUS_ACCESS_DENIED = -2, STATUS_SHARING_VIOLATION = -3, STATUS_DEVICE_BUSY = -4;
inline std::atomic<uint64_t> rv_test_now{1};
inline KIRQL rv_test_irql = 0;
inline KIRQL KeGetCurrentIrql() { return rv_test_irql; }
inline uint64_t KeQueryInterruptTime() { return rv_test_now; }
inline void KeInitializeSpinLock(KSPIN_LOCK*) {}
inline void KeAcquireSpinLock(KSPIN_LOCK* lock, KIRQL* previous) {
    *previous = rv_test_irql; lock->lock();
}
inline void KeReleaseSpinLock(KSPIN_LOCK* lock, KIRQL) { lock->unlock(); }
inline void* ULongToHandle(uint32_t id) { return reinterpret_cast<void*>(static_cast<uintptr_t>(id)); }
inline void RtlZeroMemory(void* data, size_t size) { std::memset(data, 0, size); }
inline void RtlSecureZeroMemory(void* data, size_t size) {
    auto p = static_cast<volatile uint8_t*>(data);
    while (size--) *p++ = 0;
}
inline void RtlCopyMemory(void* to, const void* from, size_t size) { std::memcpy(to, from, size); }
inline size_t RtlCompareMemory(const void* a, const void* b, size_t size) {
    const auto x = static_cast<const uint8_t*>(a), y = static_cast<const uint8_t*>(b);
    size_t i = 0; while (i < size && x[i] == y[i]) ++i; return i;
}
using std::min;
