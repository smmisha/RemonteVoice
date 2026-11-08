// Original RemoteVoice development bridge; not yet accepted on a real driver target.
#include <ntifs.h>
#include "rv_pcm_bridge.h"

void RvPcmBridge::ClearQueue() {
    RtlSecureZeroMemory(buffer, sizeof(buffer));
    read_offset = write_offset = queued = 0;
    last_frame = 0;
}

void RvPcmBridge::ClearOwner() {
    ClearQueue();
    owner = NULL;
    session = last_request = 0;
    RtlSecureZeroMemory(nonce, sizeof(nonce));
}

void RvPcmBridge::Initialize() {
    KeInitializeSpinLock(&lock);
    ClearOwner();
}

void RvPcmBridge::Reset() {
    KIRQL previous;
    KeAcquireSpinLock(&lock, &previous);
    ClearOwner();
    KeReleaseSpinLock(&lock, previous);
}

NTSTATUS RvPcmBridge::Property(PPCPROPERTY_REQUEST request) {
    // PortCls owns the property buffers. Validate before copying a fixed message;
    // no user address is stored, mapped or dereferenced by the stream's DPC.
    if (KeGetCurrentIrql() != PASSIVE_LEVEL || !request || !request->Irp ||
        request->Verb != KSPROPERTY_TYPE_SET || request->InstanceSize != 0 ||
        request->ValueSize != sizeof(RvMicMessage) || !request->Value) {
        return STATUS_INVALID_PARAMETER;
    }
    RvMicMessage message;
    RtlCopyMemory(&message, request->Value, sizeof(message));
    UCHAR nonzero = 0;
    for (ULONG i = 0; i < sizeof(message.nonce); ++i) nonzero |= message.nonce[i];
    if (message.version != 1 || message.command < 1 || message.command > 4 ||
        !message.session || !nonzero || message.reserved ||
        message.byte_count != (message.command == 2 ? sizeof(message.pcm) : 0)) {
        RtlSecureZeroMemory(&message, sizeof(message));
        return STATUS_INVALID_PARAMETER;
    }
    const HANDLE caller = ULongToHandle(IoGetRequestorProcessId(request->Irp));
    if (!caller) {
        RtlSecureZeroMemory(&message, sizeof(message));
        return STATUS_ACCESS_DENIED;
    }
    const ULONGLONG now = KeQueryInterruptTime();
    KIRQL previous;
    KeAcquireSpinLock(&lock, &previous);
    if (owner && now - last_request > 20'000'000ULL) ClearOwner(); // Two-second lease.
    NTSTATUS status = STATUS_SUCCESS;
    const bool same = owner == caller && session == message.session &&
        RtlCompareMemory(nonce, message.nonce, sizeof(nonce)) == sizeof(nonce);
    if (message.command == 1) {
        if (owner && !same) status = STATUS_SHARING_VIOLATION;
        else {
            if (!owner) {
                ClearQueue();
                owner = caller;
                session = message.session;
                RtlCopyMemory(nonce, message.nonce, sizeof(nonce));
            }
            last_request = now;
        }
    } else if (!same) {
        status = STATUS_ACCESS_DENIED;
    } else if (message.command == 3) {
        ClearOwner();
    } else {
        last_request = now;
        // Old buffered voice must not replay when the recorder starts later.
        if (queued && now - last_frame > 2'000'000ULL) ClearQueue(); // 200 ms.
        if (message.command == 2) {
            if (queued + sizeof(message.pcm) > sizeof(buffer)) status = STATUS_DEVICE_BUSY;
            else {
                for (ULONG i = 0; i < sizeof(message.pcm); ++i) {
                    buffer[write_offset] = message.pcm[i];
                    write_offset = (write_offset + 1) % sizeof(buffer);
                }
                queued += sizeof(message.pcm);
                last_frame = now;
            }
        }
    }
    KeReleaseSpinLock(&lock, previous);
    RtlSecureZeroMemory(&message, sizeof(message));
    return status;
}

void RvPcmBridge::Read(PUCHAR output, ULONG size) {
    if (!output || !size) return;
    RtlZeroMemory(output, size);
    if (size % 2) return; // PCM16 must preserve complete samples.
    const ULONGLONG now = KeQueryInterruptTime();
    KIRQL previous;
    KeAcquireSpinLock(&lock, &previous);
    if (owner && now - last_request > 20'000'000ULL) ClearOwner();
    if (queued && now - last_frame > 2'000'000ULL) ClearQueue();
    const ULONG count = min(size, queued);
    for (ULONG i = 0; i < count; ++i) {
        output[i] = buffer[read_offset];
        buffer[read_offset] = 0;
        read_offset = (read_offset + 1) % sizeof(buffer);
    }
    queued -= count;
    KeReleaseSpinLock(&lock, previous);
}
